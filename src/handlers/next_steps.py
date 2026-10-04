"""The most likely next tool call, attached to a tool's result.

Hints existed, but scattered and unequal: some results carried a
``next_steps`` dict, some a ``next_step`` string, and several of the clearest
("next call should be generate_chart") went out only as progress messages,
which most clients never show the model. A model then had to rediscover the
workflow on every turn.

One rule set here, read from the session's state after the tool ran --
connected, the working schema discovered, an ontology active, GraphRAG ready
-- and one shape: ``next_steps``, a list of ``{tool, arguments, why}``, in
dict results, and a "Next step" section in text results. Only successful
results get one; an error already says how to recover.
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

QUESTION = "<the user's question>"


def _step(tool: str, why: str, **arguments: Any) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments, "why": why}


def _schema(session: Any) -> str | None:
    schema = getattr(session, "current_schema", None) or getattr(
        session, "working_schema", None
    )
    return str(schema) if schema else None


def _discovered(session: Any) -> bool:
    schema = _schema(session)
    try:
        return bool(schema) and session.get_cached_schema(schema) is not None
    except Exception as e:
        logger.debug(f"Could not read the schema cache for next steps: {e}")
        return False


def _has_ontology(session: Any) -> bool:
    # Only a real value counts: a generated file name or an upload's text.
    return any(
        isinstance(value, str) and bool(value)
        for value in (
            getattr(session, "ontology_file", None),
            getattr(session, "loaded_ontology", None),
        )
    )


def _answering(session: Any) -> list[dict[str, Any]]:
    """Steps once the schema and ontology are in place.

    GraphRAG only when it is ready: with AUTO_GRAPHRAG=false, or before the
    background build finishes, graphrag_query_context answers
    graphrag_not_initialized, so the hint would send the model into an error.
    No tool builds it on demand, so the alternative is the direct route.
    """
    ready = getattr(session, "graphrag_initialized", False) is True
    first = (
        _step(
            "graphrag_query_context",
            "Get the tables, columns and joins the question needs, then write SQL",
            query=QUESTION,
        )
        if ready
        else _step(
            "get_table_details",
            (
                "Schema search is still being built in the background; until "
                "graphrag_query_context answers, read the tables the question "
                "needs directly"
                if os.getenv("AUTO_GRAPHRAG", "true").lower() == "true"
                else "Schema search is off (AUTO_GRAPHRAG=false); read the tables "
                "the question needs directly"
            ),
            table_name="<table the question is about>",
        )
    )
    return [
        first,
        _step(
            "execute_sql_query",
            "Run the SQL; OBQC checks it against the ontology first",
            sql_query="<SQL qualified with the schema, from the context>",
        ),
    ]


def _after_ontology(session: Any) -> list[dict[str, Any]]:
    steps = _answering(session)
    # suggest_semantic_names reads the generated ontology file, not an upload:
    # offered only while the generated ontology is the active one.
    generated_active = getattr(session, "loaded_ontology", None) is None and (
        isinstance(getattr(session, "ontology_file", None), str)
    )
    if generated_active and not getattr(session, "ontology_enriched", False):
        steps.append(
            _step(
                "suggest_semantic_names",
                "Optional: if table or column names are cryptic, propose business "
                "names; they also make search find them",
            )
        )
    steps.append(
        _step(
            "validate_relationship",
            "Optional: check a join inferred from column names against the data "
            "before relying on it",
            from_table="<table>",
            column="<key column>",
        )
    )
    return steps


def _setup(session: Any) -> list[dict[str, Any]]:
    """Steps from a fresh connection to a session ready for questions."""
    schema = _schema(session)
    if not _discovered(session):
        return [
            _step(
                "discover_schema",
                f"Analyze the working schema{f' {schema}' if schema else ''}: "
                "tables, keys and relationships",
            )
        ]
    if not _has_ontology(session):
        return [
            _step(
                "generate_ontology",
                "Build the ontology from the discovered schema; OBQC and join "
                "checks need it",
            )
        ]
    return _answering(session)


def for_tool(tool: str, session: Any, result: Any) -> list[dict[str, Any]]:
    """The likely next calls after a tool succeeded.

    Args:
        tool: The tool that just ran.
        session: Its session, after the call.
        result: What it returned.

    Returns:
        Steps, most likely first; empty when nothing obviously follows.
    """
    data = result if isinstance(result, dict) else {}
    if tool == "list_databases":
        databases = data.get("databases") or []
        if len(databases) == 1:
            return [
                _step("connect_database", "Connect to the only configured database")
            ]
        return [
            _step(
                "connect_database",
                "Connect to the database the user means; ask if it is unclear",
                database="<name from the list>",
            )
        ]
    if tool == "connect_database":
        return _setup(session)
    if tool == "discover_schema":
        # It just succeeded: the schema is discovered, whatever the cache says.
        # The handler's own hint wins where it gives one: it knows what it
        # found cached.
        if data.get("next_step") == "suggest_semantic_names":
            return [
                _step(
                    "suggest_semantic_names",
                    "The ontology is ready; propose business names for cryptic "
                    "table and column names",
                ),
                *_answering(session),
            ]
        if data.get("next_step") == "generate_ontology" or not _has_ontology(session):
            return [
                _step(
                    "generate_ontology",
                    "Build the ontology from the discovered schema; OBQC and "
                    "join checks need it",
                )
            ]
        return _answering(session)
    if tool == "list_schemas":
        return [
            _step(
                "discover_schema",
                "Analyze the schema the question is about",
                schema_name="<schema from the list>",
            )
        ]
    if tool == "generate_ontology":
        return _after_ontology(session)
    if tool == "load_my_ontology":
        if data.get("activated") is False:
            return [
                _step(
                    "load_my_ontology",
                    "Add the oba: mappings listed under oba_requirements and load "
                    "it again; until then the previous ontology stays active",
                )
            ]
        return _after_ontology(session)
    if tool == "suggest_semantic_names":
        return [
            _step(
                "apply_semantic_names",
                "Apply the reviewed names to the ontology and the search index",
                suggestions="<the reviewed suggestions>",
            )
        ]
    if tool == "apply_semantic_names":
        return _answering(session)
    if tool == "validate_relationship":
        if data.get("status") in ("refuted", "target_not_unique"):
            return [
                _step(
                    "graphrag_find_join_path",
                    "Do not join on this relationship; look for another route "
                    "between the tables",
                    from_table="<table>",
                    to_table="<table>",
                )
            ]
        return _answering(session)
    if tool in (
        "graphrag_query_context",
        "graphrag_find_join_path",
        "plan_composite_query",
        "reachable_from",
        "measurable_from",
    ):
        return [
            _step(
                "execute_sql_query",
                "Write the SQL from these tables and joins and run it; OBQC "
                "checks it first",
                sql_query="<SQL qualified with the schema>",
            )
        ]
    if tool == "execute_sql_query":
        if not data.get("success"):
            return []
        return [
            _step(
                "generate_chart",
                "Optional: chart the result in the chat",
                data_source="<the result rows>",
                chart_type="bar | line | scatter | heatmap",
                x_column="<column>",
            )
        ]
    return []


def _failed(result: Any) -> bool:
    if isinstance(result, dict):
        # By value: a successful result may carry "error": None.
        return result.get("success") is False or bool(
            result.get("error") and result.get("error_type")
        )
    if isinstance(result, str):
        stripped = result.lstrip()
        return stripped.startswith("{") and '"error"' in stripped[:200]
    return True


def _render(steps: list[dict[str, Any]]) -> str:
    lines = ["", "", "## Next step"]
    for step in steps:
        arguments = ", ".join(f"{k}={v!r}" for k, v in step["arguments"].items())
        lines.append(f"- {step['tool']}({arguments}) -- {step['why']}")
    return "\n".join(lines)


def attach(result: Any, steps: list[dict[str, Any]]) -> Any:
    """The result with its next steps, in the result's own form.

    Args:
        result: What the tool returned.
        steps: From :func:`for_tool`.

    Returns:
        A dict with ``next_steps`` (replacing any older ``next_steps`` or
        ``next_step`` hint), a string with a "Next step" section, or the
        result unchanged when it failed, has no steps, or is neither.
    """
    if not steps or _failed(result):
        return result
    if isinstance(result, dict):
        updated = {k: v for k, v in result.items() if k != "next_step"}
        updated["next_steps"] = steps
        return updated
    if isinstance(result, str):
        return result + _render(steps)
    return result
