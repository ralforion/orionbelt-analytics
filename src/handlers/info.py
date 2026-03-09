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
        "supported_databases": ["postgresql", "snowflake", "dremio"],
        "features": [
            "Database connection management",
            "Schema analysis",
            "Table relationship mapping",
            "RDF/OWL ontology generation",
            "Load custom ontologies from file",
            "Semantic name resolution (abbreviation expansion)",
            "Interactive data visualization (charts)",
        ],
        "tools": [
            "connect_database",
            "list_schemas",
            "analyze_schema",
            "generate_ontology",
            "suggest_semantic_names",
            "apply_semantic_names",
            "load_my_ontology",
            "sample_table_data",
            "validate_sql_syntax",
            "execute_sql_query",
            "generate_chart",
            "get_server_info",
        ],
        "next_tool": "connect_database",
    }
