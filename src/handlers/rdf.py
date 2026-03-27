"""Oxigraph RDF store and SPARQL handler implementations."""

import logging
from typing import Optional, Dict, Any

from fastmcp import Context

from ..exceptions import (
    DependencyError,
    StoreNotInitializedError,
    RDFError,
    ValidationError,
)
from ..oxigraph_store import OXIGRAPH_AVAILABLE
from ..paths import ensure_output_dir

logger = logging.getLogger(__name__)


async def store_ontology_in_rdf(
    ctx: Context,
    schema_name: Optional[str],
    graph_uri: Optional[str],
    get_session_data,
    get_oxigraph_store,
    create_error_response,
) -> str:
    """Store current session ontology in persistent RDF store with SPARQL access."""
    if not OXIGRAPH_AVAILABLE:
        return DependencyError(
            "pyoxigraph not installed. Install with: pip install pyoxigraph"
        ).to_response()

    store = get_oxigraph_store(ctx)
    if store is None:
        return StoreNotInitializedError("Failed to initialize Oxigraph store").to_response()

    session = get_session_data(ctx)
    effective_schema = schema_name or session.get_last_analyzed_schema() or "default"

    if not session.ontology_file:
        return ValidationError(
            "No ontology generated. Please call generate_ontology() first."
        ).to_response()

    try:
        output_dir = ensure_output_dir()
        ontology_path = output_dir / session.ontology_file

        if not ontology_path.exists():
            return ValidationError(
                f"Ontology file not found: {session.ontology_file}"
            ).to_response()

        ontology_ttl = ontology_path.read_text(encoding="utf-8")

        if not graph_uri:
            graph_uri = f"http://example.com/ontology/{effective_schema}"

        triple_count = store.load_ontology(
            ontology_ttl=ontology_ttl, graph_uri=graph_uri, schema_name=effective_schema
        )

        await ctx.info(
            f"Stored ontology for schema '{effective_schema}' in RDF store: {triple_count} triples"
        )

        return (
            f"Ontology stored successfully in RDF store!\n\n"
            f"Schema: {effective_schema}\n"
            f"Graph URI: <{graph_uri}>\n"
            f"Triples loaded: {triple_count}\n\n"
            f"You can now query using:\n"
            f"- query_sparql() - Execute SPARQL SELECT queries\n"
            f"- query_sparql_ask() - Execute SPARQL ASK queries\n"
            f"- list_tables_sparql() - List tables via SPARQL\n"
            f"- find_columns_by_type_sparql() - Find columns by data type"
        )

    except Exception as e:
        logger.error(f"Failed to store ontology in RDF: {e}", exc_info=True)
        return RDFError(f"Failed to store ontology: {str(e)}").to_response()


async def query_sparql(
    ctx: Context,
    sparql_query: str,
    timeout_seconds: int,
    get_oxigraph_store,
    create_error_response,
) -> Dict[str, Any]:
    """Execute SPARQL SELECT query against stored ontologies."""
    if not OXIGRAPH_AVAILABLE:
        return DependencyError("pyoxigraph not installed").to_response()

    store = get_oxigraph_store(ctx)
    if store is None:
        return StoreNotInitializedError("Oxigraph store not initialized").to_response()

    try:
        results = store.query_sparql(sparql_query, timeout_seconds=timeout_seconds)
        await ctx.info(f"SPARQL query returned {len(results)} results")

        return {
            "success": True,
            "result_count": len(results),
            "results": results,
            "query": sparql_query,
        }

    except Exception as e:
        logger.error(f"SPARQL query failed: {e}", exc_info=True)
        return RDFError(f"SPARQL query failed: {str(e)}").to_response()


async def query_sparql_ask(
    ctx: Context,
    sparql_query: str,
    get_oxigraph_store,
    create_error_response,
) -> Dict[str, Any]:
    """Execute SPARQL ASK query (returns true/false)."""
    if not OXIGRAPH_AVAILABLE:
        return DependencyError("pyoxigraph not installed").to_response()

    store = get_oxigraph_store(ctx)
    if store is None:
        return StoreNotInitializedError("Oxigraph store not initialized").to_response()

    try:
        result = store.query_sparql_ask(sparql_query)

        return {"success": True, "result": result, "query": sparql_query}

    except Exception as e:
        logger.error(f"SPARQL ASK query failed: {e}", exc_info=True)
        return RDFError(f"SPARQL ASK query failed: {str(e)}").to_response()


async def add_rdf_knowledge(
    ctx: Context,
    subject: str,
    predicate: str,
    object_value: str,
    metadata: Optional[Dict[str, Any]],
    get_oxigraph_store,
    create_error_response,
) -> str:
    """Add custom knowledge/metadata to the RDF store."""
    if not OXIGRAPH_AVAILABLE:
        return DependencyError("pyoxigraph not installed").to_response()

    store = get_oxigraph_store(ctx)
    if store is None:
        return StoreNotInitializedError("Oxigraph store not initialized").to_response()

    try:
        store.add_knowledge(
            subject=subject, predicate=predicate, object=object_value, metadata=metadata
        )

        await ctx.info(f"Added knowledge triple: <{subject}> <{predicate}> {object_value}")

        return (
            f"Knowledge added successfully!\n\n"
            f"Subject: <{subject}>\n"
            f"Predicate: <{predicate}>\n"
            f"Object: {object_value}"
        )

    except Exception as e:
        logger.error(f"Failed to add knowledge: {e}", exc_info=True)
        return RDFError(f"Failed to add knowledge: {str(e)}").to_response()


async def list_tables_sparql(
    ctx: Context,
    schema_graph: Optional[str],
    get_session_data,
    get_oxigraph_store,
    create_error_response,
) -> Dict[str, Any]:
    """List all tables from stored ontology using SPARQL."""
    if not OXIGRAPH_AVAILABLE:
        return DependencyError("pyoxigraph not installed").to_response()

    store = get_oxigraph_store(ctx)
    if store is None:
        return StoreNotInitializedError("Oxigraph store not initialized").to_response()

    try:
        if not schema_graph:
            session = get_session_data(ctx)
            schema_name = session.get_last_analyzed_schema() or "default"
            schema_graph = f"http://example.com/schema/{schema_name}"

        tables = store.list_tables_sparql(schema_graph)

        await ctx.info(f"Found {len(tables)} tables via SPARQL")

        return {
            "success": True,
            "table_count": len(tables),
            "tables": tables,
            "graph": schema_graph,
        }

    except Exception as e:
        logger.error(f"SPARQL table listing failed: {e}", exc_info=True)
        return RDFError(f"Failed to list tables: {str(e)}").to_response()


async def find_columns_by_type_sparql(
    ctx: Context,
    data_type: str,
    schema_graph: Optional[str],
    get_oxigraph_store,
    create_error_response,
) -> Dict[str, Any]:
    """Find columns by data type using SPARQL."""
    if not OXIGRAPH_AVAILABLE:
        return DependencyError("pyoxigraph not installed").to_response()

    store = get_oxigraph_store(ctx)
    if store is None:
        return StoreNotInitializedError("Oxigraph store not initialized").to_response()

    try:
        columns = store.find_columns_by_type(data_type, schema_graph)

        await ctx.info(f"Found {len(columns)} {data_type} columns via SPARQL")

        return {
            "success": True,
            "data_type": data_type,
            "column_count": len(columns),
            "columns": columns,
        }

    except Exception as e:
        logger.error(f"SPARQL column search failed: {e}", exc_info=True)
        return RDFError(f"Failed to find columns: {str(e)}").to_response()


async def get_rdf_store_stats(
    ctx: Context,
    get_oxigraph_store,
    create_error_response,
) -> Dict[str, Any]:
    """Get statistics about the persistent RDF store."""
    if not OXIGRAPH_AVAILABLE:
        return DependencyError("pyoxigraph not installed").to_response()

    store = get_oxigraph_store(ctx)
    if store is None:
        return StoreNotInitializedError("Oxigraph store not initialized").to_response()

    try:
        stats = store.get_ontology_stats()

        return {"success": True, "stats": stats}

    except Exception as e:
        logger.error(f"Failed to get store stats: {e}", exc_info=True)
        return RDFError(f"Failed to get stats: {str(e)}").to_response()
