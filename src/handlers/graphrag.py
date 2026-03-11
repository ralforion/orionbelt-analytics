"""GraphRAG initialization and search handler implementations."""

import logging
import os
import time
from datetime import datetime
from typing import Optional, Dict, List, Any

from fastmcp import Context

from ..exceptions import ConnectionError
from ..graphrag import GraphRAGManager
from ..ontology_generator import OntologyGenerator
from ..oxigraph_store import OXIGRAPH_AVAILABLE
from ..paths import ensure_output_dir

logger = logging.getLogger(__name__)


async def _auto_generate_ontology_background(
    schema_name: str,
    tables_info: List[Any],
    session: Any,
    ctx: Context,
) -> None:
    """Background task: Auto-generate ontology after GraphRAG completes."""
    from ..config import config_manager

    try:
        start_time = time.time()
        logger.info(f"Auto-generating ontology for schema '{schema_name}'...")

        config = config_manager.get_server_config()
        base_uri = config.ontology_base_uri

        schema_data = {"schema": schema_name, "tables": []}

        for table_info in tables_info:
            table_dict = {
                "name": table_info.name,
                "schema": table_info.schema,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "nullable": col.is_nullable,
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
            }
            schema_data["tables"].append(table_dict)

        ontology_generator = OntologyGenerator(base_uri=base_uri)
        ontology_ttl = ontology_generator.generate_ontology(schema_data)

        output_dir = ensure_output_dir()
        connection_dir = output_dir / (session.connection_id or "default")
        connection_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ontology_file = connection_dir / f"ontology_{schema_name}_{timestamp}.ttl"
        ontology_file.write_text(ontology_ttl, encoding="utf-8")
        session.ontology_file = f"{session.connection_id}/{ontology_file.name}"

        if OXIGRAPH_AVAILABLE:
            try:
                from . import rdf as rdf_handler

                # Direct store access for background task
                if session.oxigraph_store:
                    graph_uri = f"{base_uri}{schema_name}"
                    triple_count = session.oxigraph_store.load_ontology(
                        ontology_ttl, graph_uri, schema_name
                    )
                    logger.info(f"Stored {triple_count} triples in RDF store (graph: {graph_uri})")
            except Exception as e:
                logger.warning(f"Failed to store in RDF: {e}")

        elapsed = time.time() - start_time
        logger.info(f"Ontology auto-generated successfully ({elapsed:.2f}s)")
        logger.info(f"Saved to: {ontology_file.name}")

    except Exception as e:
        logger.error(f"Ontology auto-generation failed: {type(e).__name__}: {e}")
        logger.debug("Ontology auto-gen traceback:", exc_info=True)


async def _auto_initialize_graphrag_background(
    schema_name: str,
    tables_info: List[Any],
    session: Any,
    ctx: Context,
) -> None:
    """Background task: Auto-initialize GraphRAG after schema analysis."""
    try:
        start_time = time.time()
        logger.info(f"Auto-initializing GraphRAG for schema '{schema_name}'...")

        if session.graphrag_manager is None:
            session.graphrag_manager = GraphRAGManager(
                embedding_model="tfidf",
                connection_id=session.connection_id,
                schema_name=schema_name,
            )

        tables_dict = []
        for table_info in tables_info:
            table_dict = {
                "name": table_info.name,
                "schema": table_info.schema,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "nullable": col.is_nullable,
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
            }
            tables_dict.append(table_dict)

        session.graphrag_manager.initialize_from_schema(
            tables_info=tables_dict, schema_name=schema_name
        )

        output_dir = ensure_output_dir()
        session.graphrag_manager.save_state(output_dir)

        elapsed = time.time() - start_time
        session.graphrag_initialized = True

        logger.info(f"GraphRAG auto-initialized successfully ({elapsed:.2f}s)")
        logger.info(f"Indexed {len(tables_dict)} tables with their metadata")

        # Chain to ontology generation if enabled
        auto_ontology = os.getenv("AUTO_ONTOLOGY", "false").lower()
        if auto_ontology == "true":
            logger.info("Chaining to ontology auto-generation...")
            await _auto_generate_ontology_background(
                schema_name=schema_name,
                tables_info=tables_info,
                session=session,
                ctx=ctx,
            )

    except Exception as e:
        logger.error(f"GraphRAG auto-initialization failed: {type(e).__name__}: {e}", exc_info=True)
        session.graphrag_initialized = False
        logger.debug("GraphRAG auto-init traceback:", exc_info=True)
        session.graphrag_initialized = False


async def initialize_graphrag(
    ctx: Context,
    schema_name: Optional[str],
    embedding_model: str,
    get_session_data,
    get_session_db_manager,
    create_error_response,
) -> str:
    """Initialize GraphRAG for intelligent schema navigation and retrieval."""
    session = get_session_data(ctx)
    db_manager = get_session_db_manager(ctx)

    if not db_manager.has_engine():
        return ConnectionError(
            "No database connection. Please use connect_database tool first."
        ).to_response()

    effective_schema = schema_name
    if not effective_schema:
        effective_schema = session.get_last_analyzed_schema()
        if effective_schema:
            logger.info(f"Using last analyzed schema: {effective_schema}")

    tables_info = session.get_cached_schema(effective_schema or "")

    if not tables_info:
        try:
            tables = db_manager.get_tables(effective_schema)
            logger.info(f"Found {len(tables)} tables in schema '{effective_schema or 'default'}'")

            if effective_schema:
                db_manager.prefetch_schema_constraints(effective_schema)

            tables_info = []
            for table_name in tables:
                try:
                    table_info = db_manager.analyze_table(table_name, effective_schema)
                    if table_info:
                        tables_info.append(table_info)
                except Exception as e:
                    logger.error(f"Failed to analyze table {table_name}: {e}")

            session.cache_schema_analysis(effective_schema or "", tables_info)

        except Exception as e:
            return create_error_response(f"Failed to fetch schema: {str(e)}", "database_error")

    if not tables_info:
        return create_error_response(
            f"No tables found in schema '{effective_schema or 'default'}'", "data_error"
        )

    # Convert TableInfo objects to dictionaries
    tables_dict = []
    for table_info in tables_info:
        table_dict = {
            "name": table_info.name,
            "schema": table_info.schema,
            "columns": [
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "is_nullable": col.is_nullable,
                    "is_primary_key": col.is_primary_key,
                    "is_foreign_key": col.is_foreign_key,
                    "foreign_key_table": col.foreign_key_table,
                    "foreign_key_column": col.foreign_key_column,
                    "comment": col.comment,
                }
                for col in table_info.columns
            ],
            "primary_keys": table_info.primary_keys,
            "foreign_keys": table_info.foreign_keys,
            "comment": table_info.comment,
            "row_count": table_info.row_count,
        }
        tables_dict.append(table_dict)

    try:
        if session.graphrag_manager is None:
            session.graphrag_manager = GraphRAGManager(
                embedding_model=embedding_model,
                embedding_dimension=384,
                connection_id=session.connection_id,
                schema_name=effective_schema or "default",
            )

        session.graphrag_manager.initialize_from_schema(
            tables_info=tables_dict, schema_name=effective_schema or "default"
        )

        session.graphrag_initialized = True

        output_dir = ensure_output_dir()
        session.graphrag_manager.save_state(output_dir)

        await ctx.info(
            f"GraphRAG initialized for schema '{effective_schema or 'default'}' with {len(tables_dict)} tables"
        )

        return (
            f"GraphRAG initialized successfully!\n\n"
            f"Schema: {effective_schema or 'default'}\n"
            f"Tables: {len(tables_dict)}\n"
            f"Embedding model: {embedding_model}\n\n"
            f"You can now use:\n"
            f"- graphrag_search() for semantic search\n"
            f"- graphrag_query_context() for optimized query context\n"
            f"- graphrag_find_join_path() for relationship discovery\n"
            f"- graphrag_overview() for schema statistics"
        )

    except Exception as e:
        logger.error(f"GraphRAG initialization failed: {e}", exc_info=True)
        return create_error_response(
            f"GraphRAG initialization failed: {str(e)}", "graphrag_error"
        )


async def graphrag_search(
    ctx: Context,
    query: str,
    top_k: int,
    element_type: Optional[str],
    get_session_data,
    create_error_response,
) -> Dict[str, Any]:
    """Search schema using natural language via GraphRAG semantic search."""
    session = get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        return create_error_response(
            "GraphRAG not initialized. Please call initialize_graphrag() first.",
            "graphrag_not_initialized",
        )

    try:
        results = session.graphrag_manager.search_schema(
            query=query, top_k=top_k, element_type=element_type
        )

        await ctx.info(f"Found {len(results)} results for query: {query}")

        return {"success": True, "query": query, "result_count": len(results), "results": results}

    except Exception as e:
        logger.error(f"GraphRAG search failed: {e}", exc_info=True)
        return create_error_response(f"GraphRAG search failed: {str(e)}", "graphrag_error")


async def graphrag_query_context(
    ctx: Context,
    query: str,
    max_tables: int,
    max_columns: int,
    get_session_data,
    create_error_response,
) -> Dict[str, Any]:
    """Get optimized context for SQL query generation using GraphRAG."""
    session = get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        return create_error_response(
            "GraphRAG not initialized. Please call initialize_graphrag() first.",
            "graphrag_not_initialized",
        )

    try:
        context = session.graphrag_manager.get_query_context(
            query=query, max_tables=max_tables, max_columns=max_columns
        )

        await ctx.info(
            f"Generated context: {len(context['relevant_tables'])} tables, "
            f"{len(context['relevant_columns'])} columns, "
            f"~{context['token_estimate']} tokens"
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
        logger.error(f"GraphRAG query context failed: {e}", exc_info=True)
        return create_error_response(
            f"GraphRAG query context failed: {str(e)}", "graphrag_error"
        )


async def graphrag_find_join_path(
    ctx: Context,
    from_table: str,
    to_table: str,
    max_hops: int,
    get_session_data,
    create_error_response,
) -> Dict[str, Any]:
    """Find join path between two tables using GraphRAG graph traversal."""
    session = get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        return create_error_response(
            "GraphRAG not initialized. Please call initialize_graphrag() first.",
            "graphrag_not_initialized",
        )

    try:
        join_path = session.graphrag_manager.graph_retriever.find_join_path(
            from_table=from_table, to_table=to_table, max_hops=max_hops
        )

        if join_path is None:
            return {
                "success": False,
                "from": from_table,
                "to": to_table,
                "message": f"No path found between {from_table} and {to_table} within {max_hops} hops",
            }

        path = [from_table]
        for join in join_path:
            if join["to_table"] not in path:
                path.append(join["to_table"])

        await ctx.info(f"Found {len(join_path)}-hop path from {from_table} to {to_table}")

        return {
            "success": True,
            "from": from_table,
            "to": to_table,
            "hops": len(join_path),
            "path": path,
            "joins": join_path,
        }

    except Exception as e:
        logger.error(f"GraphRAG find join path failed: {e}", exc_info=True)
        return create_error_response(
            f"GraphRAG find join path failed: {str(e)}", "graphrag_error"
        )


async def graphrag_overview(
    ctx: Context,
    get_session_data,
    create_error_response,
) -> Dict[str, Any]:
    """Get GraphRAG schema overview with statistics and communities."""
    session = get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        return create_error_response(
            "GraphRAG not initialized. Please call initialize_graphrag() first.",
            "graphrag_not_initialized",
        )

    try:
        overview = session.graphrag_manager.get_schema_overview()

        await ctx.info(f"Generated schema overview for: {overview['schema_name']}")

        return {"success": True, "overview": overview}

    except Exception as e:
        logger.error(f"GraphRAG overview failed: {e}", exc_info=True)
        return create_error_response(f"GraphRAG overview failed: {str(e)}", "graphrag_error")
