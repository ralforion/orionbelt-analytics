"""Server information handler implementation."""

import logging
from typing import Dict, Any

from fastmcp import Context

logger = logging.getLogger(__name__)


async def get_server_info(ctx: Context) -> Dict[str, Any]:
    """Get information about the MCP server and its capabilities."""
    from .. import __version__, __name__ as SERVER_NAME, __description__

    await ctx.info("Server info retrieved; next call should be connect_database to start working")

    return {
        "name": SERVER_NAME,
        "version": __version__,
        "description": __description__,
        "supported_databases": [
            "postgresql", "snowflake", "dremio", "clickhouse",
            "bigquery", "duckdb", "databricks", "mysql",
        ],
        "features": [
            "Database connection management (8 databases)",
            "Schema analysis with relationship mapping",
            "RDF/OWL ontology generation with oba: namespace",
            "Load custom ontologies from file",
            "Semantic name resolution (abbreviation expansion)",
            "Interactive data visualization (charts)",
            "GraphRAG for intelligent schema discovery",
            "Persistent RDF store with SPARQL 1.1 queries",
            "Fan-trap detection and prevention",
            "R2RML mapping generation",
        ],
        "tools": [
            "connect_database",
            "list_schemas",
            "analyze_schema",
            "get_table_details",
            "generate_ontology",
            "suggest_semantic_names",
            "apply_semantic_names",
            "load_my_ontology",
            "download_artifact",
            "sample_table_data",
            "execute_sql_query",
            "generate_chart",
            "graphrag_search",
            "graphrag_query_context",
            "graphrag_find_join_path",
            "store_ontology_in_rdf",
            "query_sparql",
            "add_rdf_knowledge",
            "reset_cache",
            "get_server_info",
        ],
        "next_tool": "connect_database",
    }
