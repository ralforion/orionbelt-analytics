"""GraphRAG initialization and search handler implementations."""

import asyncio
import contextlib
import logging
import os
import time
from functools import partial
from typing import Any, cast

from fastmcp import Context

from ..async_utils import run_db
from ..exceptions import ConnectionError
from ..graphrag import GraphRAGManager
from ..graphrag.identity import qualified, split
from ..handler_context import HandlerContext
from ..lifecycle.artifacts import artifact_family_lock, prune_superseded_artifacts
from ..lifecycle.metadata import (
    get_active_version_number,
    update_schema_version,
    update_workspace_section,
)
from ..ontology_generator import OntologyGenerator
from ..oxigraph_store import OXIGRAPH_AVAILABLE
from ..paths import OUTPUT_DIR, ensure_output_dir, get_connection_dir
from ..relationship_validation import relationship_key
from ..session import GraphRAGState
from ..utils import notify_client, utc_now, write_text_file
from .connection_scope import (
    connection_changed_response,
    pin_connection,
    still_connected,
)
from .ontology_validation import recorded_validations

logger = logging.getLogger(__name__)


class _Pinned:
    """What a piece of GraphRAG work belongs to, fixed when the work starts.

    Background initialisation outlives the tool call that started it, and the
    session it was started from may move to another database meanwhile. Read
    through the session then, ``graphrag_manager`` and ``connection_id`` are
    the *new* database's: the old database's index would be installed as the
    new one's -- for every session sharing it -- and its metadata written into
    the wrong workspace. So the state object and the connection ID are taken
    once, up front, and everything afterwards goes through them.
    """

    def __init__(self, session: Any) -> None:
        state = getattr(session, "graphrag", None)
        # A test double has no GraphRAGState; its own attributes stand in.
        self.graphrag: Any = state if isinstance(state, GraphRAGState) else session
        self.connection_id: str | None = session.connection_id
        # The dialect the database speaks, for the chained ontology's view
        # lineage. Read now for the same reason as the rest: later, the
        # session's manager may be another database's.
        manager = getattr(session, "db_manager", None)
        info = getattr(manager, "connection_info", None)
        self.db_type: str | None = info.get("type") if isinstance(info, dict) else None


def _index_lock(state: Any) -> Any:
    """The lock that serializes indexing on this connection.

    Embedding runs off the event loop now, so two indexings that a blocked loop
    used to serialize can interleave: each would find no manager and build one,
    and the embedder would fit its vocabulary from two threads at once.

    Args:
        state: The GraphRAGState the work belongs to, or a test double.

    Returns:
        An async context manager to hold for the whole index step.
    """
    lock = getattr(state, "index_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        with contextlib.suppress(AttributeError):  # a read-only double
            state.index_lock = lock
    return lock


async def _save_graphrag_state(
    session: Any, schema_name: str, version: int | None, pinned: _Pinned | None = None
) -> None:
    """Persist GraphRAG state and record its half of a specific version.

    *version* is the generation this work was started for, resolved by the
    caller before the task was scheduled -- not looked up here. Initialization
    can run for a long time, and a second ``discover_schema`` for the same
    schema during that window opens a newer version; resolving on completion
    would stamp this run's snapshots and vector counts onto a generation they
    do not belong to.

    A missing version -- no connection, or a schema discovered before version
    recording existed -- just means no snapshot; the unversioned current-state
    files that restore reads are written either way.

    Args:
        session: Session data holding the GraphRAG manager and connection id.
        schema_name: Schema whose version to record against.
        version: The version number this work belongs to, if known.
        pinned: The state and connection this work was started for, when the
            caller is a background task; taken from the session otherwise.
    """
    pinned = pinned or _Pinned(session)
    manager = pinned.graphrag.graphrag_manager
    output_dir = ensure_output_dir()

    snapshot_files = await asyncio.to_thread(
        manager.save_state, output_dir, version, schema_name
    )

    if version is None or not pinned.connection_id:
        return

    try:
        await update_schema_version(
            connection_id=pinned.connection_id,
            output_dir=OUTPUT_DIR,
            schema_name=schema_name,
            updates={
                "graphrag_vector_count": manager.vector_count,
                "graphrag_collection": manager.vector_collection_name,
                "graphrag_files": snapshot_files,
            },
            version=version,
        )
    except Exception as e:
        logger.warning(f"Failed to record GraphRAG version state: {e}")


def _table_info_to_dict(table_info: Any) -> dict[str, Any]:
    """Convert a TableInfo object to a dictionary for GraphRAG/ontology consumption."""
    return {
        "name": table_info.name,
        "schema": table_info.schema,
        "columns": [
            {
                "name": col.name,
                "data_type": col.data_type,
                "is_nullable": col.is_nullable,
                "is_primary_key": getattr(col, "is_primary_key", False),
                "is_foreign_key": getattr(col, "is_foreign_key", False),
                "foreign_key_table": getattr(col, "foreign_key_table", None),
                "foreign_key_column": getattr(col, "foreign_key_column", None),
                "comment": col.comment,
            }
            for col in table_info.columns
        ],
        "primary_keys": table_info.primary_keys,
        "foreign_keys": [
            {
                "column": fk["column"],
                "referenced_table": fk["referenced_table"],
                "referenced_column": fk["referenced_column"],
            }
            for fk in table_info.foreign_keys
        ],
        "comment": table_info.comment,
        "row_count": getattr(table_info, "row_count", None),
    }


# Confidence levels an inferred relationship needs to become a join edge. The
# ontology keeps low-confidence ones too, where OBQC only uses them to check a
# join someone wrote; a join path is a suggestion, and one built on a guess the
# naming barely supports would be followed.
_INFERRED_EDGE_CONFIDENCE = frozenset({"high", "medium"})


def _existing_column(
    table: dict[str, Any], proposed: str, source_column: str
) -> str | None:
    """The column an inferred key can actually join to, if there is one.

    Inference names the target's primary key, or ``id`` when the table
    declares none -- a column that may not exist. A join path through it would
    be SQL that fails. Without the proposed column, a column of the same name
    as the key (``customer_id`` on both sides) is the usual convention.

    Args:
        table: The referenced table.
        proposed: The column inference proposed.
        source_column: The referencing column.

    Spelling decides first. Quoted identifiers can differ in case alone --
    ``"id"`` and ``"ID"`` are two columns -- so a case-insensitive match is
    accepted only when it is the only one.

    Returns:
        The column's name as the table spells it, or None.
    """
    names: list[str] = [c["name"] for c in table.get("columns", [])]
    for wanted in (proposed, source_column):
        if wanted in names:
            return wanted
    for wanted in (proposed, source_column):
        folded = [n for n in names if n.casefold() == wanted.casefold()]
        if len(folded) == 1:
            return folded[0]
    return None


def _tables_to_dicts(tables_info: list[Any]) -> list[dict[str, Any]]:
    """Tables as GraphRAG indexes them, with inferred foreign keys added.

    GraphRAG's join graph is built from foreign keys. A schema that declares
    none -- a lakehouse layer, ClickHouse, most of BigQuery -- would have no
    join paths at all, although the generated ontology finds its relationships
    from the column names. Those are added here, marked as inferred and with
    their confidence, so a join path says what it rests on. A key the database
    declares is never replaced by one inferred.

    CPU-bound on a large schema; call it off the event loop.

    Args:
        tables_info: The tables of one schema, as discovery returned them.

    Returns:
        One dictionary per table.
    """
    tables = [_table_info_to_dict(t) for t in tables_info]
    try:
        inferred = OntologyGenerator().infer_relationships(tables_info)
    except Exception as e:
        logger.warning(f"Could not infer relationships for GraphRAG: {e}")
        return tables

    by_name = {t["name"]: t for t in tables}
    added = 0
    for rel in inferred:
        if rel.confidence not in _INFERRED_EDGE_CONFIDENCE:
            continue
        table = by_name.get(rel.source_table)
        target = by_name.get(rel.target_table)
        if table is None or target is None:
            continue
        target_column = _existing_column(target, rel.target_column, rel.column)
        if target_column is None:
            continue
        table["foreign_keys"].append(
            {
                "column": rel.column,
                "referenced_table": rel.target_table,
                "referenced_column": target_column,
                "inferred": True,
                "confidence": rel.confidence,
            }
        )
        added += 1
    if added:
        logger.info(f"GraphRAG: {added} relationships inferred from column names")
    return tables


# "Take the connection from the session": None is a real value (no connection).
_FROM_SESSION: Any = object()


async def _auto_generate_ontology_background(
    schema_name: str,
    tables_info: list[Any],
    session: Any,
    ctx: Context,
    version: int | None = None,
    connection_id: Any = _FROM_SESSION,
    views: list[Any] | None = None,
    db_type: str | None = None,
) -> None:
    """Background task: Auto-generate ontology after GraphRAG completes.

    Uses direct schema state access (not convenience properties) to avoid
    race conditions when the user switches schemas during background work.

    *version* is the generation this work was started for. It is recorded
    against explicitly, because a second discover_schema for the same schema
    can open a newer version while this task is still running.
    """
    from ..config import config_manager

    # Fixed now: the session may be on another database by the time this ends.
    # When GraphRAG initialisation chains into this, it passes the connection
    # *it* was pinned to: the session may have moved while the index was built,
    # and reading it again here would pin this work to the new database while
    # it holds the old database's tables.
    if connection_id is _FROM_SESSION:
        connection_id = session.connection_id
    try:
        start_time = time.time()
        logger.info(f"Auto-generating ontology for schema '{schema_name}'...")

        config = config_manager.get_server_config()
        base_uri = config.ontology_base_uri

        ontology_generator = OntologyGenerator(base_uri=base_uri)
        from .ontology_generation import _views_for_ontology

        ontology_ttl = await asyncio.to_thread(
            partial(
                ontology_generator.generate_from_schema,
                tables_info,
                views_info=_views_for_ontology(
                    session, schema_name, views=views, db_type=db_type
                ),
            )
        )
        from .ontology_validation import reapply_recorded

        ontology_ttl = await asyncio.to_thread(
            reapply_recorded,
            ontology_generator,
            connection_id,
            schema_name,
            ontology_ttl,
        )

        conn_dir = (
            get_connection_dir(connection_id) if connection_id else ensure_output_dir()
        )

        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        # Serialize produce -> record -> prune for this family; a concurrent
        # generation for the same schema must not have its file pruned as stale.
        async with artifact_family_lock(conn_dir, f"ontology_{schema_name}"):
            ontology_file = conn_dir / f"ontology_{schema_name}_{timestamp}.ttl"
            await write_text_file(ontology_file, ontology_ttl)

            # The file above belongs to the database this work was started for.
            # The session's ontology pointer and its RDF store belong to
            # whatever database the session is on *now*. If it has moved on,
            # they are not ours to touch: this ontology would be loaded into
            # another database's RDF store and named as that database's
            # ontology.
            session_moved_on = session.connection_id != connection_id
            previous_ontology_file = None
            if session_moved_on:
                logger.info(
                    f"Session left connection {str(connection_id)[:8]}... while "
                    f"the ontology for '{schema_name}' was generated; the file "
                    "and its metadata are kept, the session is left alone"
                )
            else:
                # Write to the specific schema's state (not current schema)
                schema_state = session.get_or_create_schema_state(schema_name)
                previous_ontology_file = schema_state.ontology.ontology_file
                schema_state.ontology.ontology_file = ontology_file.name
                schema_state.ontology.rdf_graph_uri = f"{base_uri}{schema_name}"

            graph_uri = ""
            triple_count = 0
            if OXIGRAPH_AVAILABLE and not session_moved_on:
                try:
                    # Direct store access for background task (connection-scoped)
                    if session.oxigraph_store:
                        graph_uri = f"{base_uri}{schema_name}"
                        triple_count = session.oxigraph_store.load_ontology(
                            ontology_ttl, graph_uri, schema_name
                        )
                        logger.info(
                            f"Stored {triple_count} triples in RDF store (graph: {graph_uri})"
                        )
                except Exception as e:
                    logger.warning(f"Failed to store in RDF: {e}")

            elapsed = time.time() - start_time
            logger.info(f"Ontology auto-generated successfully ({elapsed:.2f}s)")
            logger.info(f"Saved to: {ontology_file.name}")

            # Record the same way the foreground handler does. discover_schema
            # opened a version before this task was scheduled, so without this
            # an auto-generated ontology leaves that version with no TTL file,
            # no graph URI and a zero triple count -- and retention could never
            # clean up the graph it loaded.
            if connection_id:
                try:
                    await update_workspace_section(
                        connection_id=connection_id,
                        output_dir=OUTPUT_DIR,
                        schema_name=schema_name,
                        section="ontology",
                        data={
                            "ontology_file": ontology_file.name,
                            "enriched": False,
                            "graph_uri": graph_uri,
                            "persisted_to_rdf": bool(graph_uri),
                            "generated_at": utc_now().isoformat(),
                        },
                    )
                    await update_schema_version(
                        connection_id=connection_id,
                        output_dir=OUTPUT_DIR,
                        schema_name=schema_name,
                        updates={
                            "ontology_ttl_file": ontology_file.name,
                            "ontology_graph_uri": graph_uri,
                            "ontology_triple_count": triple_count,
                        },
                        version=version,
                    )
                except Exception as e:
                    logger.warning(f"Failed to write workspace metadata: {e}")

            # Pruned last, once metadata durably names the new file, and never
            # touching what the session previously pointed at.
            await prune_superseded_artifacts(
                ontology_file,
                protect=[previous_ontology_file] if previous_ontology_file else [],
            )

    except Exception as e:
        logger.error(f"Ontology auto-generation failed: {type(e).__name__}: {e}")
        logger.debug("Ontology auto-gen traceback:", exc_info=True)


def _view_info_to_dict(view_info: Any) -> dict[str, Any]:
    """Convert a ViewInfo (or already-dict) into the embedder's input shape."""
    if isinstance(view_info, dict):
        return view_info
    return {
        "name": view_info.name,
        "definition": view_info.definition,
        "comment": getattr(view_info, "comment", None),
    }


async def _auto_initialize_graphrag_background(
    schema_name: str,
    tables_info: list[Any],
    session: Any,
    ctx: Context,
    version: int | None = None,
    views_info: list[Any] | None = None,
    pinned: _Pinned | None = None,
) -> None:
    """Background task: Auto-initialize or accumulate GraphRAG after schema analysis.

    GraphRAG is connection-scoped and accumulative. If already initialized,
    new schema tables are added to the existing graph and vector store.

    *version* is the generation discover_schema opened before scheduling this
    task. It is threaded through rather than resolved on completion so a
    rediscovery of the same schema mid-run cannot capture this run's output.
    """
    # Pinned by whoever created this task, while it still held the session's
    # binding lock. The body of a task runs only when the loop first schedules
    # it -- after the creating tool has returned and released that lock -- so a
    # pin taken here could already be the database a `connect_database` queued
    # behind the tool had moved the session to.
    pinned = pinned or _Pinned(session)
    graphrag = pinned.graphrag
    try:
        start_time = time.time()
        tables_dict = await asyncio.to_thread(_tables_to_dicts, tables_info)
        views_dict = [_view_info_to_dict(v) for v in views_info or []]

        # Held across the whole step: the decision to create a manager and the
        # writes that follow the off-loop embedding must not interleave with
        # another schema's indexing on this connection.
        async with _index_lock(graphrag):
            accumulate = graphrag.graphrag_manager is not None
            if not accumulate:
                # First schema — initialize from scratch
                logger.info(f"Initializing GraphRAG for schema '{schema_name}'...")
                graphrag.graphrag_manager = GraphRAGManager(
                    connection_id=pinned.connection_id,
                    schema_name=schema_name,
                )
            else:
                # Additional schema — accumulate into existing graph
                logger.info(
                    f"Accumulating schema '{schema_name}' into existing GraphRAG..."
                )
            await graphrag.graphrag_manager.aindex_schema(
                tables_info=tables_dict,
                schema_name=schema_name,
                views_info=views_dict,
                accumulate=accumulate,
            )

        await _save_graphrag_state(session, schema_name, version, pinned=pinned)

        elapsed = time.time() - start_time
        graphrag.graphrag_initialized = True

        total_tables = graphrag.graphrag_manager.graph_retriever.graph.number_of_nodes()
        schemas = graphrag.graphrag_manager._schema_names
        logger.info(
            f"GraphRAG auto-initialized successfully ({elapsed:.2f}s) — "
            f"{total_tables} tables across schemas: {schemas}"
        )

        # Write workspace metadata for graphrag section
        if pinned.connection_id:
            try:
                stats = graphrag.graphrag_manager.vector_store.get_statistics()
                await update_workspace_section(
                    connection_id=pinned.connection_id,
                    output_dir=OUTPUT_DIR,
                    schema_name=schema_name,
                    section="graphrag",
                    data={
                        "initialized": True,
                        "table_count": len(tables_dict),
                        "embedding_count": stats.get("total_elements", 0),
                        "schemas": schemas,
                        "initialized_at": utc_now().isoformat(),
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to write workspace metadata: {e}")

        # Chain to ontology generation if enabled
        auto_ontology = os.getenv("AUTO_ONTOLOGY", "false").lower()
        if auto_ontology == "true":
            logger.info("Chaining to ontology auto-generation...")
            await _auto_generate_ontology_background(
                schema_name=schema_name,
                tables_info=tables_info,
                session=session,
                ctx=ctx,
                version=version,
                connection_id=pinned.connection_id,
                # Not re-read from the session: by now it may hold another
                # database's views, and speak another dialect.
                views=views_info,
                db_type=pinned.db_type,
            )

    except Exception as e:
        logger.exception(
            f"GraphRAG auto-initialization failed: {type(e).__name__}: {e}",
        )
        graphrag.graphrag_initialized = False


async def initialize_graphrag(
    ctx: Context,
    schema_name: str | None,
    embedding_model: str,
    services: "HandlerContext",
) -> str:
    """Initialize GraphRAG for intelligent schema navigation and retrieval."""
    session = services.get_session_data(ctx)
    db_manager = services.get_session_db_manager(ctx)

    if not db_manager.has_engine():
        return cast(
            str,
            ConnectionError(
                "No database connection. Please use connect_database tool first."
            ).to_response(),
        )

    # Reflection, the view fetch and embedding all await. The session's cache
    # and GraphRAG are the runtime's, shared by every session on the database,
    # so what this computes is published only into the runtime it came from.
    pinned = pin_connection(session)
    graph_pin = _Pinned(session)

    effective_schema = schema_name
    if not effective_schema:
        effective_schema = session.get_last_analyzed_schema()
        if effective_schema:
            logger.info(f"Using last analyzed schema: {effective_schema}")

    # Set current schema for per-schema state isolation
    session.set_current_schema(effective_schema or "default")

    tables_info = session.get_cached_schema(effective_schema or "")

    if not tables_info:
        try:
            tables = await run_db(db_manager.get_tables, effective_schema)
            logger.info(
                f"Found {len(tables)} tables in schema '{effective_schema or 'default'}'"
            )

            if effective_schema:
                await run_db(db_manager.prefetch_schema_constraints, effective_schema)

            analyzed = await run_db(db_manager.analyze_tables, tables, effective_schema)
            tables_info = [analyzed[name] for name in tables if name in analyzed]

            if not still_connected(session, pinned):
                return cast(
                    str,
                    connection_changed_response(
                        services,
                        f"schema '{effective_schema or 'default'}' was being "
                        "analyzed",
                        "initialize_graphrag()",
                    ),
                )
            session.cache_schema_analysis(effective_schema or "", tables_info)

        except Exception as e:
            return cast(
                str,
                services.create_error_response(
                    f"Failed to fetch schema: {e!s}", "database_error"
                ),
            )

    if not tables_info:
        return cast(
            str,
            services.create_error_response(
                f"No tables found in schema '{effective_schema or 'default'}'",
                "data_error",
            ),
        )

    # Convert TableInfo objects to dictionaries
    tables_dict = await asyncio.to_thread(_tables_to_dicts, tables_info)

    # Views come from the discovery cache, or straight from the database when
    # this tool is the entry point -- which it is whenever AUTO_GRAPHRAG is
    # false or a client calls it directly. Without this the manual path
    # indexes tables only, and views reach GraphRAG on the auto path alone.
    # Asked by "was this discovered?", not "is it non-empty?": a schema with no
    # views read the same as one nobody had looked at, so every call went back
    # to the database. The empty answer is cached for the same reason.
    if session.has_cached_views(effective_schema or ""):
        views_info = session.get_cached_views(effective_schema or "")
    else:
        try:
            views_info = await run_db(db_manager.get_views, effective_schema)
            if not still_connected(session, pinned):
                return cast(
                    str,
                    connection_changed_response(
                        services, "views were being read", "initialize_graphrag()"
                    ),
                )
            session.cache_views(effective_schema or "", views_info)
        except Exception as e:
            logger.warning(f"Could not fetch views for GraphRAG: {e}")
            views_info = []
    views_dict = [_view_info_to_dict(v) for v in views_info]

    eff_schema = effective_schema or "default"

    # Bound to the generation current when this call started; embedding a large
    # schema is slow enough for a rediscovery to land before it finishes.
    target_version: int | None = None
    if graph_pin.connection_id:
        try:
            target_version = await get_active_version_number(
                graph_pin.connection_id, OUTPUT_DIR, eff_schema
            )
        except Exception as e:
            logger.warning(f"Failed to read active version: {e}")

    try:
        async with _index_lock(graph_pin.graphrag):
            accumulate = graph_pin.graphrag.graphrag_manager is not None
            if not accumulate:
                graph_pin.graphrag.graphrag_manager = GraphRAGManager(
                    embedding_model=embedding_model,
                    embedding_dimension=384,
                    connection_id=graph_pin.connection_id,
                    schema_name=eff_schema,
                )
            await graph_pin.graphrag.graphrag_manager.aindex_schema(
                tables_info=tables_dict,
                schema_name=eff_schema,
                views_info=views_dict,
                accumulate=accumulate,
            )

        graph_pin.graphrag.graphrag_initialized = True

        await _save_graphrag_state(
            session, eff_schema, target_version, pinned=graph_pin
        )

        total_tables = (
            graph_pin.graphrag.graphrag_manager.graph_retriever.graph.number_of_nodes()
        )
        schemas = graph_pin.graphrag.graphrag_manager._schema_names

        # Write workspace metadata for graphrag section
        if graph_pin.connection_id:
            try:
                stats = (
                    graph_pin.graphrag.graphrag_manager.vector_store.get_statistics()
                )
                await update_workspace_section(
                    connection_id=graph_pin.connection_id,
                    output_dir=OUTPUT_DIR,
                    schema_name=eff_schema,
                    section="graphrag",
                    data={
                        "initialized": True,
                        "table_count": len(tables_dict),
                        "embedding_count": stats.get("total_elements", 0),
                        "schemas": schemas,
                        "initialized_at": utc_now().isoformat(),
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to write workspace metadata: {e}")

        # The index went into the database it was built from. If the session
        # has moved on meanwhile, saying "initialized" would describe the wrong
        # database to the caller.
        if not still_connected(session, pinned):
            return cast(
                str,
                connection_changed_response(
                    services,
                    f"schema '{eff_schema}' was being indexed",
                    "initialize_graphrag()",
                ),
            )

        await notify_client(
            ctx,
            f"GraphRAG initialized for schema '{eff_schema}' with {len(tables_dict)} tables "
            f"(total: {total_tables} tables across {len(schemas)} schema(s))",
        )

        return (
            f"GraphRAG initialized successfully!\n\n"
            f"Schema: {eff_schema}\n"
            f"Tables added: {len(tables_dict)}\n"
            f"Total tables in graph: {total_tables}\n"
            f"Schemas: {', '.join(schemas)}\n"
            f"Embedding model: {embedding_model}\n\n"
            f"You can now use:\n"
            f"- graphrag_search() for semantic search across all schemas\n"
            f"- graphrag_search(overview=True) for schema statistics\n"
            f"- graphrag_query_context() for optimized query context\n"
            f"- graphrag_find_join_path() for cross-schema relationship discovery"
        )

    except Exception as e:
        logger.exception(f"GraphRAG initialization failed: {e}")
        return cast(
            str,
            services.create_error_response(
                f"GraphRAG initialization failed: {e!s}", "graphrag_error"
            ),
        )


async def graphrag_search(
    ctx: Context,
    query: str,
    top_k: int,
    element_type: str | None,
    services: "HandlerContext",
) -> dict[str, Any]:
    """Search schema using natural language via GraphRAG semantic search."""
    session = services.get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        err: dict[str, Any] = services.create_error_response(
            "GraphRAG not initialized. Please call discover_schema() first.",
            "graphrag_not_initialized",
        )
        return err

    try:
        # Embedded in a worker, searched here. The manager is read once, so the
        # answer describes the database this call was made on.
        results = await session.graphrag_manager.asearch_schema(
            query=query, top_k=top_k, element_type=element_type
        )

        await notify_client(ctx, f"Found {len(results)} results for query: {query}")

        return {
            "success": True,
            "query": query,
            "result_count": len(results),
            "results": results,
        }

    except Exception as e:
        logger.exception(f"GraphRAG search failed: {e}")
        err = services.create_error_response(
            f"GraphRAG search failed: {e!s}", "graphrag_error"
        )
        return err


async def graphrag_add_semantic_context(
    ctx: Context,
    target: str,
    context: str,
    services: "HandlerContext",
) -> dict[str, Any]:
    """Index client-supplied business context for a schema element."""
    session = services.get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        err: dict[str, Any] = services.create_error_response(
            "GraphRAG not initialized. Please call discover_schema() first.",
            "graphrag_not_initialized",
        )
        return err

    try:
        result = session.graphrag_manager.add_semantic_context(
            target=target, context=context
        )

        if result.get("searchable"):
            await notify_client(ctx, f"Indexed semantic context for {target}")
        else:
            await notify_client(
                ctx,
                f"Stored semantic context for {target}, but it is not "
                "searchable under the current embedding backend",
            )

        return {
            "success": True,
            **result,
            "note": (
                "Indexed for semantic search only. Not written to the ontology "
                "or RDF store, and discarded if the index is rebuilt."
            ),
        }

    except ValueError as e:
        err = services.create_error_response(str(e), "invalid_argument")
        return err
    except Exception as e:
        logger.exception(f"Adding semantic context failed: {e}")
        err = services.create_error_response(
            f"Adding semantic context failed: {e!s}", "graphrag_error"
        )
        return err


async def graphrag_query_context(
    ctx: Context,
    query: str,
    max_tables: int,
    max_columns: int,
    services: "HandlerContext",
) -> dict[str, Any]:
    """Get optimized context for SQL query generation using GraphRAG."""
    session = services.get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        err: dict[str, Any] = services.create_error_response(
            "GraphRAG not initialized. Please call discover_schema() first.",
            "graphrag_not_initialized",
        )
        return err

    try:
        context = await session.graphrag_manager.aget_query_context(
            query=query,
            max_tables=max_tables,
            max_columns=max_columns,
            retriever=await _join_graph(ctx, session, services),
        )

        await notify_client(
            ctx,
            f"Generated context: {len(context['relevant_tables'])} tables, "
            f"{len(context['relevant_columns'])} columns, "
            f"~{context['token_estimate']} tokens",
        )

        return {
            "success": True,
            "query": query,
            "context": context,
            "usage_guidance": (
                "Use this context for SQL generation. "
                "It includes only relevant schema elements, reducing token usage by 85-95%."
            ),
        }

    except Exception as e:
        logger.exception(f"GraphRAG query context failed: {e}")
        err = services.create_error_response(
            f"GraphRAG query context failed: {e!s}", "graphrag_error"
        )
        return err


async def graphrag_find_join_path(
    ctx: Context,
    from_table: str,
    to_table: str,
    max_hops: int,
    services: "HandlerContext",
) -> dict[str, Any]:
    """Find join path between two tables using GraphRAG graph traversal."""
    session = services.get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        err: dict[str, Any] = services.create_error_response(
            "GraphRAG not initialized. Please call discover_schema() first.",
            "graphrag_not_initialized",
        )
        return err

    try:
        retriever = await _join_graph(ctx, session, services)
        current = getattr(session, "current_schema", None)

        # A name that fits two schemas is reported, not guessed at. The model
        # sends bare names, and picking whichever schema was indexed last is
        # how a join path came to cross into the wrong copy of a table.
        for given in (from_table, to_table):
            found = retriever.resolve_name(given, current)
            if found.ambiguous:
                return {
                    "success": False,
                    "from": from_table,
                    "to": to_table,
                    "error_type": "ambiguous_table",
                    "candidates": found.candidates,
                    "message": (
                        f"'{given}' names a table in more than one schema: "
                        f"{', '.join(found.candidates)}. Ask again with the "
                        f"one you mean."
                    ),
                }

        from_table = retriever.identity_for(from_table, current)
        to_table = retriever.identity_for(to_table, current)

        join_path = retriever.find_join_path(
            from_table=from_table, to_table=to_table, max_hops=max_hops
        )

        if join_path is None:
            return {
                "success": False,
                "from": from_table,
                "to": to_table,
                "message": f"No path found between {from_table} and {to_table} within {max_hops} hops",
            }

        def tables_of(joins: list[dict[str, Any]]) -> list[str]:
            tables = [from_table]
            for join in joins:
                if join["to_table"] not in tables:
                    tables.append(join["to_table"])
            return tables

        alternatives = retriever.find_alternative_join_paths(
            from_table, to_table, chosen=join_path
        )

        await notify_client(
            ctx, f"Found {len(join_path)}-hop path from {from_table} to {to_table}"
        )

        response: dict[str, Any] = {
            "success": True,
            "from": from_table,
            "to": to_table,
            "hops": len(join_path),
            "path": tables_of(join_path),
            "joins": join_path,
            # Always present, so "unambiguous" is an answer and not an absence.
            "ambiguous": bool(alternatives),
        }
        verdicts = await asyncio.to_thread(
            recorded_validations, getattr(session, "connection_id", None)
        )
        _attach_validations(join_path, verdicts)
        for alternative in alternatives:
            _attach_validations(alternative, verdicts)
        if any(
            join.get("source") == "inferred"
            and (join.get("validation") or {}).get("status") != "confirmed"
            for join in join_path
        ):
            response["inferred_joins_note"] = (
                "Some joins on this path are inferred from column names, not "
                "declared by the database (each says so, with its confidence). "
                "Check them with validate_relationship before relying on them."
            )
        doubtful = [
            join
            for join in join_path
            if (join.get("validation") or {}).get("status")
            in ("partial", "refuted", "target_not_unique")
        ]
        if doubtful:
            response["validation_warning"] = (
                f"{len(doubtful)} join(s) on this path failed or only partly "
                "passed validate_relationship (see each join's validation). "
                "A refuted join is probably not a relationship; a partial one "
                "drops unmatched rows; a non-unique target multiplies rows."
            )
        if alternatives:
            response["alternatives"] = [
                {"path": tables_of(joins), "joins": joins} for joins in alternatives
            ]
            response["ambiguity_note"] = (
                f"{len(alternatives)} other path(s) are exactly as short. They "
                "generally answer different questions (e.g. a customer's region "
                "vs. a warehouse's region). Do not pick silently: say which "
                "route the question implies, or ask the user."
            )
        return response

    except Exception as e:
        logger.exception(f"GraphRAG find join path failed: {e}")
        err = services.create_error_response(
            f"GraphRAG find join path failed: {e!s}", "graphrag_error"
        )
        return err


def _ontology_table(
    name: str, ontology_schema: Any, retriever: Any, current_schema: str | None
) -> str | None:
    """The graph node an ontology's table name stands for, if one is certain.

    Args:
        name: The table as the ontology's ``oba:tableName`` gives it.
        ontology_schema: What OBQC extracted from the ontology.
        retriever: The join graph.
        current_schema: The session's schema, to break a tie.

    Returns:
        The node's identity, or None if it is not indexed, is ambiguous, or
        the ontology places it in a schema that is not indexed.
    """
    indexed = retriever.indexed_name(name)
    if indexed is None:
        return None
    table = ontology_schema.tables.get(name.lower())
    if table is not None and getattr(table, "schema_declared", True):
        # The ontology says where the table is. A table of the same name in
        # another schema is a different table, and a relationship asserted
        # about one says nothing about the other.
        identity = qualified(table.schema_name, indexed)
        return identity if identity in retriever.graph else None
    found = retriever.resolve_name(indexed, current_schema)
    return None if found.ambiguous else found.identity


async def _join_graph(ctx: Context, session: Any, services: "HandlerContext") -> Any:
    """The join graph this session should read.

    The connection's graph is shared, and holds the keys the database declares
    plus those inferred from column names. A user who loaded an ontology of
    their own gets a copy extended with its relationships -- the joins they
    defined for a schema whose keys are not declared -- without anyone else on
    the connection seeing them. The copy is kept until the graph, or the
    ontology, changes.

    Args:
        ctx: FastMCP request context.
        session: The calling session.
        services: Request-scoped services.

    Returns:
        A graph retriever, the shared one unless the loaded ontology adds to it.
    """
    manager = session.graphrag_manager
    base = manager.graph_retriever
    if getattr(session, "loaded_ontology", None) is None or not services.provides(
        "aget_session_obqc_validator"
    ):
        return base
    pinned = pin_connection(session)
    try:
        validator = await services.aget_session_obqc_validator(ctx)
        # The validator was awaited for. A reconnect in between would pair
        # this graph with the next database's ontology: leave it unextended.
        if (
            validator is None
            or not still_connected(session, pinned)
            or session.graphrag_manager is not manager
            or manager.graph_retriever is not base
        ):
            return base
        held = session.ontology_join_graph
        if (
            held is not None
            and held[0] is base
            and held[1] == base.generation
            and held[2] is validator
        ):
            return held[3]
        ontology_schema = validator.prepared_ontology().schema
    except Exception as e:
        logger.warning(f"Loaded ontology not used for join discovery: {e}")
        return base

    current = getattr(session, "current_schema", None)
    relationships = []
    for rel in ontology_schema.relationships.values():
        # An edge runs from the table holding the key to the one it references,
        # and reachability reads its direction as many-to-one. Only a
        # many_to_one relationship states that. Its one_to_many inverse names
        # the tables the other way round and does not say which one holds the
        # key column, so it is left out rather than guessed at -- an ontology
        # with both directions loses nothing.
        if rel.relationship_type != "many_to_one":
            continue
        source = _ontology_table(rel.from_table, ontology_schema, base, current)
        target = _ontology_table(rel.to_table, ontology_schema, base, current)
        if source is not None and target is not None:
            relationships.append((source, rel.from_column, target, rel.to_column))
    extended = base.with_relationships(relationships)
    session.ontology_join_graph = (base, base.generation, validator, extended)
    return extended


def _attach_validations(
    joins: list[dict[str, Any]], verdicts: dict[str, dict[str, Any]]
) -> None:
    """Add each join's recorded validate_relationship verdict, if any.

    A join may walk a relationship against its direction, so both readings
    are looked up.

    Args:
        joins: Join specifications, changed in place.
        verdicts: Recorded verdicts keyed by relationship.
    """
    if not verdicts:
        return
    for join in joins:
        left_schema, left = split(join["from_table"])
        right_schema, right = split(join["to_table"])
        for key in (
            relationship_key(
                left_schema,
                left,
                join["from_column"],
                right_schema,
                right,
                join["to_column"],
            ),
            relationship_key(
                right_schema,
                right,
                join["to_column"],
                left_schema,
                left,
                join["from_column"],
            ),
        ):
            verdict = verdicts.get(key)
            if verdict:
                join["validation"] = {
                    "status": verdict.get("status"),
                    "match_ratio": verdict.get("match_ratio"),
                    "checked_at": verdict.get("checked_at"),
                }
                break


def _resolve_table_for_session(
    retriever: Any, name: str, session: Any, services: "HandlerContext"
) -> tuple[str | None, dict[str, Any] | None]:
    """Which table a tool's argument means, with the session's schema in mind.

    The same rules join-path discovery uses: an exact identity, a unique bare
    name, then the schema the session is working in. With `sales.orders` and
    `archive.orders` both indexed and the session in `sales`, `orders` is the
    sales one; with no current schema it is reported as ambiguous, candidates
    listed, rather than as "not found" -- which is what it used to say.

    Args:
        retriever: The graph retriever.
        name: The table as the caller wrote it.
        session: The calling session.
        services: Request-scoped services.

    Returns:
        The identity to use and no error, or no identity and the error to
        return.
    """
    found = retriever.resolve_name(name, getattr(session, "current_schema", None))
    if found.ambiguous:
        err: dict[str, Any] = services.create_error_response(
            f"'{name}' names a table in more than one schema: "
            f"{', '.join(found.candidates)}. Ask again with the one you mean.",
            "ambiguous_table",
        )
        err["candidates"] = found.candidates
        return None, err
    return found.identity or name, None


async def reachable_from(
    ctx: Context,
    table: str,
    max_hops: int | None,
    services: "HandlerContext",
) -> dict[str, Any]:
    """Dimension-capable tables for a query anchored on ``table`` (many-to-one closure)."""
    session = services.get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        err: dict[str, Any] = services.create_error_response(
            "GraphRAG not initialized. Please call discover_schema() first.",
            "graphrag_not_initialized",
        )
        return err

    try:
        retriever = await _join_graph(ctx, session, services)
        resolved, problem = _resolve_table_for_session(
            retriever, table, session, services
        )
        if problem is not None:
            return problem
        result = retriever.reachable_from(resolved, max_hops=max_hops)
        if not result["exists"]:
            err = services.create_error_response(
                f"Table '{table}' not found in the schema graph.", "data_error"
            )
            return err

        await notify_client(
            ctx,
            f"{len(result['tables'])} dimension-capable tables reachable from '{table}'",
        )
        return {
            "success": True,
            "table": table,
            "resolved_table": resolved,
            "direction": "many_to_one",
            "capability": "dimension",
            "reachable_tables": result["tables"],
            "by_hop": result["by_hop"],
            "guidance": (
                f"These coarser-grain tables can be joined from '{table}' without "
                "row multiplication (each join is many-to-one / functional), so their "
                "columns are safe to use as dimensions (GROUP BY / filter)."
            ),
        }

    except Exception as e:
        logger.exception(f"reachable_from failed: {e}")
        err = services.create_error_response(
            f"reachable_from failed: {e!s}", "graphrag_error"
        )
        return err


async def measurable_from(
    ctx: Context,
    table: str,
    max_hops: int | None,
    services: "HandlerContext",
) -> dict[str, Any]:
    """Measure-capable tables for a query anchored on ``table`` (one-to-many closure)."""
    session = services.get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        err: dict[str, Any] = services.create_error_response(
            "GraphRAG not initialized. Please call discover_schema() first.",
            "graphrag_not_initialized",
        )
        return err

    try:
        retriever = await _join_graph(ctx, session, services)
        resolved, problem = _resolve_table_for_session(
            retriever, table, session, services
        )
        if problem is not None:
            return problem
        result = retriever.measurable_from(resolved, max_hops=max_hops)
        if not result["exists"]:
            err = services.create_error_response(
                f"Table '{table}' not found in the schema graph.", "data_error"
            )
            return err

        await notify_client(
            ctx, f"{len(result['tables'])} measure-capable tables for anchor '{table}'"
        )
        return {
            "success": True,
            "table": table,
            "resolved_table": resolved,
            "direction": "one_to_many",
            "capability": "measure",
            "measurable_tables": result["tables"],
            "by_hop": result["by_hop"],
            "guidance": (
                f"These finer-grain tables fan out '{table}' (one-to-many), so their "
                "values must be aggregated into measures (SUM/COUNT/...) and must NOT "
                f"be used as dimensions at the grain of '{table}' — doing so is a fan-trap."
            ),
        }

    except Exception as e:
        logger.exception(f"measurable_from failed: {e}")
        err = services.create_error_response(
            f"measurable_from failed: {e!s}", "graphrag_error"
        )
        return err


async def plan_composite_query(
    ctx: Context,
    facts: list[str],
    dimensions: list[str] | None,
    services: "HandlerContext",
) -> dict[str, Any]:
    """Advise a Composite Fact Layer (CFL) decomposition for a multi-fact query.

    Detects whether the requested facts are independent grains (disjoint
    siblings) that require a UNION ALL composite, and computes the leg
    structure: per-leg dimensions, conformed (shared) GROUP BY keys, and the
    NULL-pad set for each leg. Advisory only — OBA does not compile SQL; OBSL
    owns CFL compilation.
    """
    session = services.get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        err: dict[str, Any] = services.create_error_response(
            "GraphRAG not initialized. Please call discover_schema() first.",
            "graphrag_not_initialized",
        )
        return err

    if not facts:
        err = services.create_error_response(
            "Provide at least one fact (measure-source) table.", "parameter_error"
        )
        return err

    retriever = await _join_graph(ctx, session, services)
    current = getattr(session, "current_schema", None)

    # Names arrive as a model wrote them, bare or qualified. Resolving them to
    # identities first keeps every set operation below comparing like with
    # like, and an ambiguous name is reported rather than picked.
    def _resolved(
        names: list[str], label: str
    ) -> tuple[list[str], dict[str, Any] | None]:
        resolved: list[str] = []
        for name in names:
            found = retriever.resolve_name(name, current)
            if found.ambiguous:
                return [], services.create_error_response(
                    f"'{name}' names a table in more than one schema: "
                    f"{', '.join(found.candidates)}. Ask again with the one "
                    f"you mean.",
                    "ambiguous_table",
                )
            resolved.append(found.identity or name)
        missing_names = [n for n in resolved if n not in retriever.graph]
        if missing_names:
            return [], services.create_error_response(
                f"{label} not found in schema graph: {', '.join(missing_names)}",
                "data_error",
            )
        return resolved, None

    facts, problem = _resolved(facts, "Tables")
    if problem:
        return problem

    # Validate explicit dimensions too — an unknown dimension would otherwise be
    # silently null-padded into every leg and mislead downstream SQL planning.
    if dimensions:
        dimensions, problem = _resolved(dimensions, "Dimensions")
        if problem:
            return problem

    facts = list(dict.fromkeys(facts))  # de-dupe, preserve order

    # Dimension-capable set reachable from each fact (many-to-one closure).
    reach = {f: set(retriever.reachable_from(f)["tables"]) for f in facts}

    # Leg-root facts = facts that are NOT reachable from another fact. A fact
    # reachable from another sits on that fact's grain chain (a coarser table),
    # so it is a dimension of it, not an independent leg.
    leg_roots = [f for f in facts if not any(f in reach[g] for g in facts if g != f)]
    leg_roots = list(dict.fromkeys(leg_roots))

    cfl_required = len(leg_roots) >= 2

    # Requested dimensions: explicit list, else the union of all reachable dims.
    if dimensions:
        requested = list(dict.fromkeys(dimensions))
    else:
        requested = sorted(set().union(*reach.values()) if reach else set())

    # Conformed dims = reachable from every leg root → safe GROUP BY keys.
    if leg_roots:
        conformed_set = set.intersection(*[reach[f] for f in leg_roots])
    else:
        conformed_set = set()
    conformed = [d for d in requested if d in conformed_set]

    legs = []
    for root in leg_roots:
        leg_dims = [d for d in requested if d in reach[root]]
        null_pad = [d for d in requested if d not in reach[root]]
        legs.append(
            {
                "root": root,
                "dimensions": leg_dims,
                "null_pad": null_pad,
            }
        )

    if cfl_required:
        guidance = (
            "These facts are independent grains (disjoint siblings). Emit one "
            "UNION ALL leg per leg root, aggregating its own measures; project the "
            "conformed dimensions in every leg as GROUP BY keys and CAST(NULL AS "
            "<type>) for each leg's null_pad dimensions. OBA advises only — when "
            "OrionBelt Semantic Layer is connected, defer the actual CFL compilation "
            "to it."
        )
    elif leg_roots:
        guidance = (
            f"Single grain '{leg_roots[0]}' — a normal star join suffices, no "
            "Composite Fact Layer needed. The other facts sit on this grain's "
            "chain and act as dimensions."
        )
    else:
        guidance = "Could not determine a leg root."

    await notify_client(
        ctx, f"CFL decomposition: cfl_required={cfl_required}, {len(legs)} leg(s)"
    )
    return {
        "success": True,
        "cfl_required": cfl_required,
        "facts": facts,
        "leg_roots": leg_roots,
        "conformed_dimensions": conformed,
        "legs": legs,
        "guidance": guidance,
    }


async def graphrag_overview(
    ctx: Context,
    services: "HandlerContext",
) -> dict[str, Any]:
    """Get GraphRAG schema overview with statistics and communities."""
    session = services.get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        err: dict[str, Any] = services.create_error_response(
            "GraphRAG not initialized. Please call discover_schema() first.",
            "graphrag_not_initialized",
        )
        return err

    try:
        overview = session.graphrag_manager.get_schema_overview()

        await notify_client(
            ctx, f"Generated schema overview for: {overview['schema_name']}"
        )

        return {"success": True, "overview": overview}

    except Exception as e:
        logger.exception(f"GraphRAG overview failed: {e}")
        err = services.create_error_response(
            f"GraphRAG overview failed: {e!s}", "graphrag_error"
        )
        return err
