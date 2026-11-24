"""validate_relationship: check a relationship against the data and record it."""

import asyncio
import json
import logging
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from fastmcp import Context
from rdflib import URIRef
from rdflib.namespace import OWL, RDF

from ..async_utils import run_db
from ..graphrag.identity import split
from ..handler_context import HandlerContext
from ..join_discovery import CONFIRMED_PATTERN
from ..lifecycle.artifacts import artifact_family_lock, prune_superseded_artifacts
from ..lifecycle.metadata import VersionMetadataManager, mutate_workspace_metadata
from ..ontology_generator import InferredRelationship, OntologyGenerator
from ..ontology_review import DRAFT, REVIEWED
from ..ontology_review import STATUSES as REVIEW_STATUSES
from ..ontology_review import Review
from ..ontology_review import inventory as review_inventory
from ..ontology_review import mark as mark_reviews
from ..ontology_review import reapply as reapply_review_records
from ..oxigraph_store import OXIGRAPH_AVAILABLE, schema_graph_uri
from ..paths import OUTPUT_DIR, ensure_output_dir, get_connection_dir
from ..relationship_validation import (
    OBA,
    REASON_AMBIGUOUS_KEY,
    REASON_COMPOSITE_KEY,
    STATUS_CONFIRMED,
    STATUS_NO_DATA,
    STATUS_PARTIAL,
    STATUS_REFUTED,
    STATUS_TARGET_NOT_UNIQUE,
    RelationshipRef,
    ValidationRecord,
    apply_recorded,
    build_check_queries,
    classify,
    find_relationships,
    inferred_relationships_to_check,
    record_in_graph,
    why_not_checkable,
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


async def _measure(db_manager: Any, ref: RelationshipRef) -> ValidationRecord:
    """Run the two check queries for *ref* and classify what they count.

    Args:
        db_manager: The connection's database manager.
        ref: The relationship, with both schemas resolved.

    Returns:
        The verdict, not yet recorded anywhere.

    Raises:
        RuntimeError: A check query failed.
    """
    db_type = (getattr(db_manager, "connection_info", None) or {}).get("type", "")
    coverage_sql, uniqueness_sql = build_check_queries(ref, db_type)
    coverage = _first_row(await run_db(db_manager.execute_sql_query, coverage_sql, 5))
    uniqueness = _first_row(
        await run_db(db_manager.execute_sql_query, uniqueness_sql, 5)
    )
    checked = _count(coverage[0] if coverage else 0)
    matched = _count(coverage[1] if len(coverage) > 1 else 0)
    target_rows = _count(uniqueness[0] if uniqueness else 0)
    target_distinct = _count(uniqueness[1] if len(uniqueness) > 1 else 0)
    status, ratio = classify(checked, matched, target_rows, target_distinct)
    return ValidationRecord(
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


#: Opt-in: check every relationship inferred from column names against the
#: data when generate_ontology runs. Off by default -- each check scans both
#: tables, which on a large warehouse takes as long as a full count.
AUTO_VALIDATE_VARIABLE = "AUTO_VALIDATE_RELATIONSHIPS"


def auto_validation_enabled() -> bool:
    """Whether generate_ontology checks inferred relationships by itself."""
    return os.getenv(AUTO_VALIDATE_VARIABLE, "false").strip().lower() == "true"


async def _measure_unchecked(
    session: Any,
    db_manager: Any,
    graph: Any,
    still_current: Callable[[], bool],
) -> tuple[list[ValidationRecord], list[str]]:
    """Measure every unchecked inferred relationship in *graph* and record it there.

    Args:
        session: The session, to resolve tables' physical schemas.
        db_manager: The connection's database manager.
        graph: The ontology; each verdict is written onto it.
        still_current: False once the work no longer belongs to the session's
            connection; measuring stops there.

    Returns:
        The verdicts, and the relationships whose check failed.
    """
    refs = await asyncio.to_thread(inferred_relationships_to_check, graph)
    records: list[ValidationRecord] = []
    failed: list[str] = []
    for ref in refs:
        resolved = replace(
            ref,
            from_schema=_physical_schema(session, ref.from_table, ref.from_schema),
            to_schema=_physical_schema(session, ref.to_table, ref.to_schema),
        )
        label = (
            f"{resolved.from_table}.{resolved.column} -> "
            f"{resolved.to_table}.{resolved.to_column}"
        )
        try:
            record = await _measure(db_manager, resolved)
        except Exception as e:
            logger.warning(f"Inferred relationship check failed for {label}: {e}")
            failed.append(label)
            continue
        if not still_current():
            break
        record_in_graph(graph, record)
        records.append(record)
    return records, failed


async def _remember_verdicts(
    connection_id: str | None, schema_name: str, records: list[ValidationRecord]
) -> None:
    """File verdicts in the workspace, so a regenerated ontology gets them back."""
    if not records or not connection_id:
        return

    def _record(manager: VersionMetadataManager) -> None:
        schema_ws = manager.get_workspace_schema(schema_name) or {}
        verdicts = dict(schema_ws.get(METADATA_SECTION) or {})
        for record in records:
            verdicts[record.key()] = record.as_dict()
        manager.update_workspace(schema_name, METADATA_SECTION, verdicts)

    try:
        await mutate_workspace_metadata(connection_id, OUTPUT_DIR, _record)
    except Exception as e:
        logger.warning(f"Failed to record the relationship checks: {e}")


def _summary(records: list[ValidationRecord], failed: list[str]) -> dict[str, Any]:
    return {
        "checked": len(records),
        "verdicts": dict(Counter(record.status for record in records)),
        "failed": failed,
    }


async def validate_inferred_relationships(
    ctx: Context,
    services: "HandlerContext",
    generator: OntologyGenerator,
    pinned: Any,
    schema_name: str | None,
) -> dict[str, Any]:
    """Check every unchecked inferred relationship of a freshly generated ontology.

    Each verdict is written onto the ontology in *generator* -- the caller
    saves it with the rest -- and into the workspace, so a regeneration
    keeps it, exactly as validate_relationship records one. A check that
    fails is skipped and reported; it does not stop the others.

    Args:
        ctx: FastMCP request context.
        services: Request-scoped services.
        generator: Holds the new ontology's graph; changed in place.
        pinned: The connection the generation was pinned to.
        schema_name: The ontology's schema.

    Returns:
        ``checked`` (count), ``verdicts`` (count per status) and ``failed``
        (the relationships whose check failed).
    """
    session = services.get_session_data(ctx)
    await notify_client(ctx, "Checking inferred relationships against the data")
    records, failed = await _measure_unchecked(
        session,
        services.get_session_db_manager(ctx),
        generator.graph,
        lambda: still_connected(session, pinned),
    )
    await _remember_verdicts(pinned.connection_id, schema_name or "default", records)
    return _summary(records, failed)


async def validate_inferred_in_background(
    session: Any,
    connection_id: str | None,
    generator: OntologyGenerator,
    schema_name: str,
) -> dict[str, Any]:
    """The same check for an ontology generated in the background (AUTO_ONTOLOGY).

    Skipped when the session has moved to another connection meanwhile: its
    database manager is no longer the one the ontology describes.

    Args:
        session: The session the generation was started for.
        connection_id: The connection it was pinned to.
        generator: Holds the new ontology's graph; changed in place.
        schema_name: The ontology's schema.

    Returns:
        The same summary as :func:`validate_inferred_relationships`.
    """
    db_manager = getattr(session, "db_manager", None)
    if db_manager is None or session.connection_id != connection_id:
        return _summary([], [])
    records, failed = await _measure_unchecked(
        session,
        db_manager,
        generator.graph,
        lambda: session.connection_id == connection_id,
    )
    await _remember_verdicts(connection_id, schema_name, records)
    return _summary(records, failed)


async def validate_cached_ontology(
    ctx: Context, services: "HandlerContext"
) -> dict[str, Any] | None:
    """The same check for the generated ontology generate_ontology returns cached.

    That ontology may have been generated before the setting was turned on,
    or enriched with semantic names since, so it is not regenerated: the
    verdicts are added to it and it is saved as a new version, as
    validate_relationship saves one. An uploaded ontology is left alone.

    Returns:
        The summary, or None when there was nothing to check.
    """
    session = services.get_session_data(ctx)
    if session.loaded_ontology is not None:
        return None
    pinned = pin_connection(session)
    try:
        generator, is_upload = await _active_ontology(ctx, session, services)
    except Exception as e:
        logger.warning(f"Could not load the cached ontology to check it: {e}")
        return None
    if is_upload:
        return None
    records, failed = await _measure_unchecked(
        session,
        services.get_session_db_manager(ctx),
        generator.graph,
        lambda: still_connected(session, pinned),
    )
    if records:
        await notify_client(
            ctx, f"Checked {len(records)} inferred relationship(s) against the data"
        )
        await _save_active_ontology(
            ctx, services, session, pinned, generator, is_upload, records
        )
    if not (records or failed):
        return None
    return _summary(records, failed)


async def _save_active_ontology(
    ctx: Context,
    services: "HandlerContext",
    session: Any,
    pinned: Any,
    generator: OntologyGenerator,
    is_upload: bool,
    records: list[ValidationRecord],
) -> dict[str, Any] | None:
    """Save the active ontology, now carrying *records*, as its new version.

    Writes the file, makes it the session's active ontology, files the
    verdicts in the workspace and refreshes the RDF store.

    Returns:
        ``filename``, ``persisted`` and ``graph_uri``; None if the session
        moved to another connection meanwhile.
    """
    schema_name = (
        session.current_schema or session.get_last_analyzed_schema() or "default"
    )
    schema_safe = schema_name.replace(" ", "_").replace(".", "_")
    base_uri = str(generator.base_uri)
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
            return None

        previous = session.ontology_file
        session.obqc_validator = None
        if is_upload:
            # Still the user's own ontology, now carrying the verdicts.
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
                for record in records:
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
    return {"filename": filename, "persisted": persisted, "graph_uri": graph_uri}


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
    checkable = [
        r
        for r in refs
        if why_not_checkable(generator.graph, URIRef(r.property_uri)) is None
    ]
    if not checkable:
        reason = why_not_checkable(generator.graph, URIRef(refs[0].property_uri))
        if reason == REASON_COMPOSITE_KEY:
            # The check counts one column's values; a composite key is unique
            # as a tuple, so a verdict on one of its columns would be wrong
            # about it -- order_id repeats in every unique (order_id, line_no).
            message = (
                f"{from_table}.{column} is part of a composite key. "
                "validate_relationship checks single-column keys only; a "
                "verdict on one column would misstate the key, so none is "
                "recorded."
            )
        else:
            message = (
                f"The relationship of {from_table}.{column} states several key "
                "or referenced columns, and the ontology does not say which "
                "column references which, or whether they form one key "
                "(ontologies generated before keys were named apart look like "
                "this). A verdict could land on the wrong key, so none is "
                "recorded. Regenerate the ontology with generate_ontology, or "
                "give each key its own property (one oba:constraintName for a "
                "composite key)."
            )
        err = services.create_error_response(message, reason or REASON_AMBIGUOUS_KEY)
        return err
    refs = checkable
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
        record = await _measure(db_manager, ref)
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
    status, ratio, checked = record.status, record.match_ratio, record.checked_rows
    matched, target_rows = record.matched_rows, record.target_rows
    target_distinct = record.target_distinct

    record_in_graph(generator.graph, record)
    saved = await _save_active_ontology(
        ctx, services, session, pinned, generator, is_upload, [record]
    )
    if saved is None:
        return connection_changed_response(
            services, "the verdict was being recorded", "validate_relationship()"
        )
    filename, persisted, graph_uri = (
        saved["filename"],
        saved["persisted"],
        saved["graph_uri"],
    )

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


# Workspace metadata section of the joins confirm_join linked, per schema, so
# a regenerated ontology keeps them.
CONFIRMED_JOINS_SECTION = "confirmed_joins"


def confirmed_joins(
    connection_id: str | None, schema_name: str | None
) -> list[InferredRelationship]:
    """The joins confirm_join linked in *schema_name*, to add when regenerating.

    File access; call it off the event loop.
    """
    if not connection_id:
        return []
    try:
        manager = VersionMetadataManager(connection_id, OUTPUT_DIR)
        schema_ws = manager.get_workspace_schema(schema_name or "default") or {}
    except Exception as e:
        logger.debug(f"No confirmed joins: {e}")
        return []
    joins = schema_ws.get(CONFIRMED_JOINS_SECTION) or {}
    return [
        InferredRelationship(
            source_table=join["table"],
            column=join["column"],
            target_table=join["target_table"],
            target_column=join["target_column"],
            confidence="high",
            pattern_matched=CONFIRMED_PATTERN,
        )
        for join in joins.values()
        if isinstance(join, dict)
        and all(
            join.get(k) for k in ("table", "column", "target_table", "target_column")
        )
    ]


async def _remember_join(
    connection_id: str | None, schema_name: str, record: ValidationRecord
) -> None:
    """File a confirmed join in the workspace, so a regeneration keeps it."""
    if not connection_id:
        return

    def _record(manager: VersionMetadataManager) -> None:
        schema_ws = manager.get_workspace_schema(schema_name) or {}
        joins = dict(schema_ws.get(CONFIRMED_JOINS_SECTION) or {})
        joins[record.key()] = {
            "table": record.from_table,
            "column": record.column,
            "target_table": record.to_table,
            "target_column": record.to_column,
        }
        manager.update_workspace(schema_name, CONFIRMED_JOINS_SECTION, joins)

    try:
        await mutate_workspace_metadata(connection_id, OUTPUT_DIR, _record)
    except Exception as e:
        logger.warning(f"Failed to record the confirmed join: {e}")


@dataclass(frozen=True)
class _OntologyTable:
    #: The table's class, as the ontology names it.
    uri: URIRef
    name: str
    schema: str | None
    #: Lower-cased column name -> as the ontology spells it.
    columns: dict[str, str]
    primary_keys: tuple[str, ...]


def _ontology_tables(graph: Any, name: str, schema: str | None) -> list[_OntologyTable]:
    """The ontology's tables called *name*, in *schema* if given."""
    found = []
    for cls in graph.subjects(RDF.type, OWL.Class):
        table = graph.value(cls, OBA.tableName)
        if table is None or str(table).lower() != name.lower():
            continue
        declared = graph.value(cls, OBA.schemaName)
        if schema and declared is not None and str(declared).lower() != schema.lower():
            continue
        columns = {}
        for prop in graph.subjects(RDF.type, OWL.DatatypeProperty):
            owner = graph.value(prop, OBA.tableName)
            column = graph.value(prop, OBA.columnName)
            if owner is not None and column is not None and str(owner) == str(table):
                columns[str(column).lower()] = str(column)
        found.append(
            _OntologyTable(
                uri=URIRef(str(cls)),
                name=str(table),
                schema=str(declared) if declared is not None else None,
                columns=columns,
                primary_keys=tuple(str(k) for k in graph.objects(cls, OBA.primaryKey)),
            )
        )
    return found


async def confirm_join(
    ctx: Context,
    table: str,
    column: str,
    target_table: str,
    services: "HandlerContext",
    target_column: str | None = None,
    schema_name: str | None = None,
) -> dict[str, Any]:
    """Link a column to another table's key, once the data confirms it.

    For a column generate_ontology's join discovery left to choose (several
    keys hold its values), or any reference the ontology misses. The join is
    checked on all rows, as validate_relationship checks one; only a
    confirmed join is added, as an inferred relationship carrying its
    verdict, so OBQC treats it like a checked key. The ontology is saved as a
    new version, and the join kept when it is regenerated.

    Args:
        ctx: FastMCP request context.
        table: Table holding the column.
        column: The column.
        target_table: The table it references.
        services: Request-scoped services.
        target_column: The referenced column; by default the target's
            one-column primary key, or its ``id``.
        schema_name: The tables' schema, when two schemas hold them.

    Returns:
        Whether it was linked, the verdict and the counts behind it.
    """
    session = services.get_session_data(ctx)
    if session.ontology_file is None and session.loaded_ontology is None:
        err: dict[str, Any] = services.create_error_response(
            "No ontology is active. Call generate_ontology() or load_my_ontology() "
            "first: confirm_join adds a relationship to it.",
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
    graph = generator.graph

    ends = []
    for wanted in (table, target_table):
        tables = _ontology_tables(graph, wanted, schema_name)
        if len(tables) != 1:
            err = services.create_error_response(
                f"The ontology has {'no' if not tables else 'several'} table "
                f"{wanted!r}" + ("" if not tables else "; pass schema_name") + ".",
                "table_not_found" if not tables else "ambiguous_table",
            )
            return err
        ends.append(tables[0])
    source, target = ends
    if source.name == target.name and source.schema == target.schema:
        err = services.create_error_response(
            "A column joined to its own table's key is no join confirm_join "
            "can check: nearly any integer column matches a dense id.",
            "parameter_error",
        )
        return err
    key = source.columns.get(column.lower())
    if key is None:
        err = services.create_error_response(
            f"{source.name} has no column {column!r}.", "column_not_found"
        )
        return err
    if target_column is None:
        primary = list(target.primary_keys)
        target_column = (
            primary[0] if len(primary) == 1 else target.columns.get("id") or ""
        )
    referenced = target.columns.get((target_column or "").lower())
    if not referenced:
        err = services.create_error_response(
            f"{target.name} has no one-column key to join to; pass target_column.",
            "column_not_found",
        )
        return err
    existing = find_relationships(graph, source.name, key, None, source.schema)
    if existing:
        err = services.create_error_response(
            f"{source.name}.{key} already references {existing[0].to_table}; "
            "validate_relationship() checks it.",
            "relationship_exists",
        )
        return err

    ref = RelationshipRef(
        from_table=source.name,
        column=key,
        to_table=target.name,
        to_column=referenced,
        from_schema=_physical_schema(session, source.name, source.schema),
        to_schema=_physical_schema(session, target.name, target.schema),
        property_uri="",
    )
    try:
        record = await _measure(services.get_session_db_manager(ctx), ref)
    except Exception as e:
        err = services.create_error_response(
            f"Checking {ref.from_table}.{ref.column} -> {ref.to_table}."
            f"{ref.to_column} failed: {e!s}",
            "query_error",
        )
        return err
    if not still_connected(session, pinned):
        return connection_changed_response(
            services, "the join was being checked", "confirm_join()"
        )
    label = f"{ref.from_table}.{ref.column} -> {ref.to_table}.{ref.to_column}"
    result: dict[str, Any] = {
        "success": True,
        "relationship": label,
        "status": record.status,
        "meaning": _MEANING[record.status],
        "match_ratio": (
            round(record.match_ratio, 4) if record.match_ratio is not None else None
        ),
        "checked_rows": record.checked_rows,
        "matched_rows": record.matched_rows,
    }
    if record.status != STATUS_CONFIRMED:
        result["linked"] = False
        result["message"] = (
            f"Not linked: the data does not confirm {label} ({record.status})."
        )
        return result

    # Between the classes as the active ontology names them: an upload's
    # need not follow the generator's naming.
    generator.add_inferred_relationship(
        InferredRelationship(
            source_table=ref.from_table,
            column=ref.column,
            target_table=ref.to_table,
            target_column=ref.to_column,
            confidence="high",
            pattern_matched=CONFIRMED_PATTERN,
        ),
        source=source.uri,
        target=target.uri,
    )
    if record_in_graph(graph, record) == 0:
        err = services.create_error_response(
            f"{label} was confirmed, but the ontology could not take it: its "
            "classes do not carry the oba: table annotations it is found by.",
            "ontology_error",
        )
        return err
    saved = await _save_active_ontology(
        ctx, services, session, pinned, generator, is_upload, [record]
    )
    if saved is None:
        return connection_changed_response(
            services, "the join was being recorded", "confirm_join()"
        )
    schema_ws = (
        session.current_schema or session.get_last_analyzed_schema() or "default"
    )
    await _remember_join(pinned.connection_id, schema_ws, record)
    await notify_client(ctx, f"Linked {label}: confirmed on all rows")
    result.update(
        linked=True,
        message=(
            f"Linked {label}: confirmed on {record.checked_rows} rows. OBQC now "
            "treats it like a checked key."
        ),
        ontology_file=saved["filename"],
        persisted_to_rdf=saved["persisted"],
    )
    return result


# Workspace metadata section of the reviews, per schema, so a regenerated
# ontology gets them back.
REVIEWS_SECTION = "reviews"


def recorded_reviews(
    connection_id: str | None, schema_name: str | None
) -> dict[str, dict[str, Any]]:
    """The reviews kept for *schema_name*, by element. File access; off the loop."""
    if not connection_id:
        return {}
    try:
        manager = VersionMetadataManager(connection_id, OUTPUT_DIR)
        schema_ws = manager.get_workspace_schema(schema_name or "default") or {}
    except Exception as e:
        logger.debug(f"No recorded reviews: {e}")
        return {}
    section = schema_ws.get(REVIEWS_SECTION) or {}
    return section if isinstance(section, dict) else {}


def reapply_reviews(
    generator: OntologyGenerator,
    connection_id: str | None,
    schema_name: str | None,
    ontology_ttl: str,
) -> str:
    """A freshly generated ontology with the reviews kept for its schema.

    CPU-bound; call it off the event loop.
    """
    reviews = recorded_reviews(connection_id, schema_name)
    if not reviews or not reapply_review_records(generator.graph, reviews):
        return ontology_ttl
    return generator.serialize_ontology()


async def _remember_reviews(
    connection_id: str | None, schema_name: str, reviews: list[Review]
) -> None:
    """Keep *reviews* in the workspace; a draft removes the element's review."""
    if not connection_id or not reviews:
        return

    def _record(manager: VersionMetadataManager) -> None:
        schema_ws = manager.get_workspace_schema(schema_name) or {}
        kept = dict(schema_ws.get(REVIEWS_SECTION) or {})
        for review in reviews:
            if review.status == DRAFT:
                kept.pop(review.key(), None)
            else:
                kept[review.key()] = review.as_dict()
        manager.update_workspace(schema_name, REVIEWS_SECTION, kept)

    try:
        await mutate_workspace_metadata(connection_id, OUTPUT_DIR, _record)
    except Exception as e:
        logger.warning(f"Failed to record the reviews: {e}")


async def review_ontology(
    ctx: Context,
    items: list[dict[str, Any]] | None,
    services: "HandlerContext",
    reviewer: str | None = None,
) -> dict[str, Any]:
    """Mark tables, columns and relationships as reviewed, rejected or draft.

    Without items, what there is to review: every relationship with where it
    comes from, its latest check on the data and its review status, and how
    many tables, columns and relationships are reviewed.

    Args:
        ctx: FastMCP request context.
        items: Each a ``table``, optional ``column`` and ``to_table`` (for a
            relationship), ``status`` and optional ``note``.
        services: Request-scoped services.
        reviewer: Who reviewed them, as given; stored as oba:reviewedBy.

    Returns:
        What was marked, what was not found, and the counts.
    """
    session = services.get_session_data(ctx)
    if session.ontology_file is None and session.loaded_ontology is None:
        err: dict[str, Any] = services.create_error_response(
            "No ontology is active. Call generate_ontology() or load_my_ontology() "
            "first: review_ontology marks elements of it.",
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
    graph = generator.graph
    if not items:
        listing = await asyncio.to_thread(review_inventory, graph)
        return {"success": True, **listing}

    at = utc_now().isoformat()
    requests: list[Review] = []
    invalid: list[str] = []
    for item in items:
        table = str(item.get("table") or "").strip()
        column = str(item.get("column") or "").strip() or None
        to_table = str(item.get("to_table") or "").strip() or None
        to_column = str(item.get("to_column") or "").strip() or None
        status = str(item.get("status") or REVIEWED).strip().lower()
        note = str(item.get("note") or "").strip() or None
        if (
            not table
            or status not in REVIEW_STATUSES
            or (to_table and not column)
            or (to_column and not to_table)
        ):
            invalid.append(json.dumps(item, default=str)[:200])
            continue
        requests.append(
            Review(
                table=table,
                column=column,
                to_table=to_table,
                to_column=to_column,
                status=status,
                note=note,
                reviewer=(reviewer or "").strip() or None,
                at="" if status == DRAFT else at,
            )
        )
    # One pass over the graph for the whole batch, off the event loop: a
    # few hundred items on a large ontology must not stall other sessions.
    applied, missing = await asyncio.to_thread(mark_reviews, graph, requests)
    if not applied:
        err = services.create_error_response(
            "Nothing was marked: "
            + (f"not in the ontology: {', '.join(missing[:10])}. " if missing else "")
            + (f"invalid: {'; '.join(invalid[:5])}." if invalid else ""),
            "element_not_found" if missing else "parameter_error",
        )
        err["not_found"] = missing
        err["invalid"] = invalid
        return err

    saved = await _save_active_ontology(
        ctx, services, session, pinned, generator, is_upload, []
    )
    if saved is None:
        return connection_changed_response(
            services, "the reviews were being recorded", "review_ontology()"
        )
    schema_ws = (
        session.current_schema or session.get_last_analyzed_schema() or "default"
    )
    await _remember_reviews(pinned.connection_id, schema_ws, applied)
    counts = (await asyncio.to_thread(review_inventory, graph))["counts"]
    await notify_client(ctx, f"Recorded {len(applied)} review(s)")
    return {
        "success": True,
        "marked": [
            {"element": r.label(), "kind": r.kind, "status": r.status} for r in applied
        ],
        "not_found": missing,
        "invalid": invalid,
        "counts": counts,
        "ontology_file": saved["filename"],
        "persisted_to_rdf": saved["persisted"],
    }
