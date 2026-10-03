"""validate_relationship: check a relationship against the data and record it."""

import asyncio
import logging
from dataclasses import replace
from functools import partial
from typing import Any

from fastmcp import Context

from ..async_utils import run_db
from ..graphrag.identity import split
from ..handler_context import HandlerContext
from ..lifecycle.artifacts import artifact_family_lock, prune_superseded_artifacts
from ..lifecycle.metadata import VersionMetadataManager, mutate_workspace_metadata
from ..ontology_generator import OntologyGenerator
from ..oxigraph_store import OXIGRAPH_AVAILABLE, schema_graph_uri
from ..paths import OUTPUT_DIR, ensure_output_dir, get_connection_dir
from ..relationship_validation import (
    STATUS_CONFIRMED,
    STATUS_NO_DATA,
    STATUS_PARTIAL,
    STATUS_REFUTED,
    STATUS_TARGET_NOT_UNIQUE,
    ValidationRecord,
    apply_recorded,
    build_check_queries,
    classify,
    find_relationships,
    record_in_graph,
)
from ..utils import notify_client, utc_now, write_text_file
from .connection_scope import (
    connection_changed_response,
    pin_connection,
    still_connected,
)

logger = logging.getLogger(__name__)

# Workspace metadata section the verdicts are kept in, per schema, so a
# regenerated ontology gets them back.
METADATA_SECTION = "relationship_validations"

_MEANING = {
    STATUS_CONFIRMED: "The data supports the relationship: treat it as a key.",
    STATUS_PARTIAL: (
        "Most keys match; joins on it silently drop the rest. Check the "
        "unmatched values before relying on it."
    ),
    STATUS_REFUTED: (
        "Too few keys match: this is probably not a relationship. Do not join on it."
    ),
    STATUS_TARGET_NOT_UNIQUE: (
        "The referenced column repeats values, so the join multiplies rows "
        "instead of looking one up. It is not many-to-one."
    ),
    STATUS_NO_DATA: "The key column holds no values to check.",
}


def recorded_validations(
    connection_id: str | None, schema_name: str | None = None
) -> dict[str, dict[str, Any]]:
    """Verdicts recorded in the workspace, for one schema or all of them.

    Args:
        connection_id: The connection.
        schema_name: A schema, or None for every schema in the workspace.

    Returns:
        Verdicts keyed by relationship.
    """
    if not connection_id:
        return {}
    try:
        manager = VersionMetadataManager(connection_id, OUTPUT_DIR)
        workspace = manager.get_workspace() or {}
    except Exception as e:
        logger.debug(f"No recorded relationship validations: {e}")
        return {}
    schemas = workspace.get("schemas", {}) or {}
    names = [schema_name] if schema_name is not None else list(schemas)
    merged: dict[str, dict[str, Any]] = {}
    for name in names:
        section = (schemas.get(name) or {}).get(METADATA_SECTION) or {}
        merged.update(section)
    return merged


def reapply_recorded(
    generator: OntologyGenerator,
    connection_id: str | None,
    schema_name: str | None,
    ontology_ttl: str,
) -> str:
    """A freshly generated ontology with the verdicts recorded for its schema.

    A regeneration rebuilds every relationship from the schema; without this
    the checks done on them would be gone from the ontology, although the
    workspace still holds them.

    CPU-bound; call it off the event loop.

    Args:
        generator: The generator holding the new ontology's graph.
        connection_id: The connection the ontology belongs to.
        schema_name: Its schema.
        ontology_ttl: The ontology as generated.

    Returns:
        The ontology with the verdicts, or as generated if none applied.
    """
    records = recorded_validations(connection_id, schema_name or "default")
    if not records or not apply_recorded(generator.graph, records):
        return ontology_ttl
    return generator.serialize_ontology()


def _physical_schema(session: Any, table: str, stated: str | None) -> str | None:
    """The schema a table really is in, when the ontology does not say.

    The one schema GraphRAG holds the table in, if exactly one; otherwise the
    schema the session works in.
    """
    if stated:
        return stated
    manager = getattr(session, "graphrag_manager", None)
    if manager is not None:
        schemas = {
            split(identity)[0]
            for identity in manager.graph_retriever.every_table_for(table)
        }
        if len(schemas) == 1:
            only = schemas.pop()
            if only:
                return only
    current = getattr(session, "current_schema", None)
    return str(current) if current else None


def _recorded_graph_uri(connection_id: str | None, schema_name: str) -> str | None:
    """The named graph the workspace records for the schema's ontology."""
    if not connection_id:
        return None
    try:
        manager = VersionMetadataManager(connection_id, OUTPUT_DIR)
        schema_ws = manager.get_workspace_schema(schema_name) or {}
    except Exception as e:
        logger.debug(f"No ontology graph recorded for {schema_name}: {e}")
        return None
    value = (schema_ws.get("ontology") or {}).get("graph_uri")
    return str(value) if value else None


def _first_row(result: dict[str, Any]) -> list[Any]:
    """The first row's values in column order.

    By position, not by name: Snowflake upper-cases unquoted aliases.
    """
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "the check query failed")
    rows = result.get("data") or []
    if not rows:
        return []
    first = rows[0]
    return list(first.values()) if isinstance(first, dict) else list(first)


def _count(value: Any) -> int:
    return int(value) if value is not None else 0


async def _active_ontology(
    ctx: Context, session: Any, services: "HandlerContext"
) -> tuple[OntologyGenerator, bool]:
    """The ontology in force for the session, and whether it is an upload."""
    if session.loaded_ontology is not None:
        generator = OntologyGenerator()
        await asyncio.to_thread(generator.load_from_string, session.loaded_ontology)
        return generator, True
    generator, _ = await asyncio.to_thread(services.load_ontology_from_session, ctx)
    return generator, False


async def validate_relationship(
    ctx: Context,
    from_table: str,
    column: str,
    to_table: str | None,
    services: "HandlerContext",
    from_schema: str | None = None,
) -> dict[str, Any]:
    """Check one relationship against the data and record the verdict.

    Args:
        ctx: FastMCP request context.
        from_table: Table holding the key.
        column: The key column.
        to_table: The referenced table, when the column has several.
        services: Request-scoped services.
        from_schema: The key table's schema, when two schemas hold the table.

    Returns:
        The verdict, the counts behind it, and where it was recorded.
    """
    session = services.get_session_data(ctx)
    if session.ontology_file is None and session.loaded_ontology is None:
        err: dict[str, Any] = services.create_error_response(
            "No ontology is active. Call generate_ontology() or load_my_ontology() "
            "first: validate_relationship checks a relationship the ontology states.",
            "ontology_not_found",
        )
        return err
    pinned = pin_connection(session)

    try:
        generator, is_upload = await _active_ontology(ctx, session, services)
    except Exception as e:
        err = services.create_error_response(
            f"Could not load the active ontology: {e!s}", "ontology_error"
        )
        return err

    refs = find_relationships(
        generator.graph, from_table, column, to_table, from_schema
    )
    if not refs:
        err = services.create_error_response(
            f"The ontology states no relationship from {from_table}.{column}"
            + (f" to {to_table}" if to_table else "")
            + ". Only relationships in the ontology (declared, inferred or "
            "uploaded) can be validated.",
            "relationship_not_found",
        )
        return err
    readings = sorted(
        {
            f"{r.from_schema or ''}.{r.from_table}.{r.column} -> "
            f"{r.to_schema or ''}.{r.to_table}.{r.to_column}"
            for r in refs
        }
    )
    if len(readings) > 1:
        err = services.create_error_response(
            f"{from_table}.{column} names several relationships: "
            f"{'; '.join(readings)}. Pass to_table and/or from_schema.",
            "ambiguous_relationship",
        )
        return err
    # The physical tables, whether or not the ontology names their schema: the
    # query, the recorded verdict and the joins it is shown on must all mean
    # the same table. A schema-less upload otherwise recorded None, which no
    # join in GraphRAG (main.orders) ever matched.
    ref = replace(
        refs[0],
        from_schema=_physical_schema(session, refs[0].from_table, refs[0].from_schema),
        to_schema=_physical_schema(session, refs[0].to_table, refs[0].to_schema),
    )

    db_manager = services.get_session_db_manager(ctx)
    db_type = (getattr(db_manager, "connection_info", None) or {}).get("type", "")
    coverage_sql, uniqueness_sql = build_check_queries(ref, db_type)
    try:
        coverage = _first_row(
            await run_db(db_manager.execute_sql_query, coverage_sql, 5)
        )
        uniqueness = _first_row(
            await run_db(db_manager.execute_sql_query, uniqueness_sql, 5)
        )
    except Exception as e:
        err = services.create_error_response(
            f"Checking {ref.from_table}.{ref.column} -> {ref.to_table}."
            f"{ref.to_column} failed: {e!s}",
            "query_error",
        )
        err["queries"] = [coverage_sql, uniqueness_sql]
        return err
    if not still_connected(session, pinned):
        return connection_changed_response(
            services, "the relationship was being checked", "validate_relationship()"
        )

    checked = _count(coverage[0] if coverage else 0)
    matched = _count(coverage[1] if len(coverage) > 1 else 0)
    target_rows = _count(uniqueness[0] if uniqueness else 0)
    target_distinct = _count(uniqueness[1] if len(uniqueness) > 1 else 0)
    status, ratio = classify(checked, matched, target_rows, target_distinct)
    record = ValidationRecord(
        from_schema=ref.from_schema,
        from_table=ref.from_table,
        column=ref.column,
        to_schema=ref.to_schema,
        to_table=ref.to_table,
        to_column=ref.to_column,
        status=status,
        match_ratio=ratio,
        checked_rows=checked,
        matched_rows=matched,
        target_rows=target_rows,
        target_distinct=target_distinct,
        checked_at=utc_now().isoformat(),
    )

    schema_name = (
        session.current_schema or session.get_last_analyzed_schema() or "default"
    )
    schema_safe = schema_name.replace(" ", "_").replace(".", "_")
    base_uri = str(generator.base_uri)
    record_in_graph(generator.graph, record)
    ontology_ttl = await asyncio.to_thread(generator.serialize_ontology)

    conn_dir = (
        get_connection_dir(pinned.connection_id)
        if pinned.connection_id
        else ensure_output_dir()
    )
    family = f"ontology_{pinned.connection_id or 'default'}_{schema_safe}_validated"
    async with artifact_family_lock(conn_dir, family):
        filename = (
            services.get_session_safe_filename(
                ctx, "ontology", f"{schema_safe}_validated"
            )
            + ".ttl"
        )
        path = conn_dir / filename
        await write_text_file(path, ontology_ttl)
        if not still_connected(session, pinned):
            return connection_changed_response(
                services, "the verdict was being recorded", "validate_relationship()"
            )

        previous = session.ontology_file
        session.obqc_validator = None
        if is_upload:
            # Still the user's own ontology, now carrying the verdict.
            session.loaded_ontology = ontology_ttl
            session.loaded_ontology_path = str(path)
        else:
            session.ontology_file = filename
        if services.provides("remember_prepared_ontology"):
            await asyncio.to_thread(
                partial(
                    services.remember_prepared_ontology,
                    session,
                    generator.graph,
                    base_uri,
                    **({"text": ontology_ttl} if is_upload else {"path": path}),
                )
            )

        if pinned.connection_id:

            def _record(manager: VersionMetadataManager) -> None:
                schema_ws = manager.get_workspace_schema(schema_name) or {}
                verdicts = dict(schema_ws.get(METADATA_SECTION) or {})
                verdicts[record.key()] = record.as_dict()
                manager.update_workspace(schema_name, METADATA_SECTION, verdicts)
                if not is_upload:
                    ontology_ws = dict(schema_ws.get("ontology") or {})
                    ontology_ws["ontology_file"] = filename
                    manager.update_workspace(schema_name, "ontology", ontology_ws)

            try:
                await mutate_workspace_metadata(
                    pinned.connection_id, OUTPUT_DIR, _record
                )
            except Exception as e:
                logger.warning(f"Failed to record the validation in metadata: {e}")

        if not is_upload:
            await prune_superseded_artifacts(
                path, protect=[previous] if previous else []
            )

    # The graph the active ontology lives in: generate_ontology(graph_uri=...)
    # or load_my_ontology may have chosen one. Writing to the schema's default
    # instead left the active graph unvalidated and could overwrite another.
    graph_uri = (
        session.ontology_graph_uri
        or _recorded_graph_uri(pinned.connection_id, schema_name)
        or schema_graph_uri(schema_name)
    )
    persisted = False
    if OXIGRAPH_AVAILABLE and services.provides("get_oxigraph_store"):
        try:
            store = services.get_oxigraph_store(ctx)
            if store is not None:
                await asyncio.to_thread(
                    store.load_ontology, ontology_ttl, graph_uri, schema_name
                )
                session.ontology_graph_uri = graph_uri
                persisted = True
        except Exception as e:
            logger.warning(f"Could not refresh the RDF store: {e}")

    await notify_client(
        ctx,
        f"{ref.from_table}.{ref.column} -> {ref.to_table}.{ref.to_column}: "
        f"{status}"
        + (f" ({ratio:.1%} of {checked} keys match)" if ratio is not None else ""),
    )
    return {
        "success": True,
        "relationship": (
            f"{ref.from_schema + '.' if ref.from_schema else ''}{ref.from_table}"
            f".{ref.column} -> "
            f"{ref.to_schema + '.' if ref.to_schema else ''}{ref.to_table}"
            f".{ref.to_column}"
        ),
        "status": status,
        "meaning": _MEANING[status],
        "match_ratio": round(ratio, 4) if ratio is not None else None,
        "checked_rows": checked,
        "matched_rows": matched,
        "target_rows": target_rows,
        "target_distinct": target_distinct,
        "checked_at": record.checked_at,
        "recorded_in": {
            "ontology_file": filename,
            "active_ontology": "uploaded" if is_upload else "generated",
            "rdf_store": persisted,
            "rdf_graph": graph_uri if persisted else None,
            "kept_across_regeneration": bool(pinned.connection_id),
        },
        "queries": [coverage_sql, uniqueness_sql],
    }
