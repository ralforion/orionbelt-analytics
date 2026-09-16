"""Tool output schemas must admit the error dicts the handlers return.

FastMCP derives a tool's output schema from the wrapper's return annotation. A
wrapper declared ``-> str`` publishes ``{"result": {"type": "string"}}``, and a
client that validates structured content then rejects an error response such
as ``{"success": false, "error": ...}`` outright: the caller sees
"is not of type 'string'" instead of the error message.
"""

import pytest
from fastmcp import Client

from src.main import _h_rdf, mcp

RETURNS_DICT_ON_ERROR = [
    "connect_database",
    "generate_ontology",
    "apply_semantic_names",
    "cleanup_workspace",
    "store_ontology_in_rdf",
    "add_rdf_knowledge",
]


async def _tool(name: str):
    tools = {t.name: t for t in await mcp.list_tools()}
    return tools[name]


def _result_types(schema: dict) -> set[str]:
    result = schema["properties"]["result"]
    return {option["type"] for option in result.get("anyOf", [result])}


@pytest.mark.parametrize("name", RETURNS_DICT_ON_ERROR)
async def test_output_schema_admits_string_and_error_dict(name):
    tool = await _tool(name)

    assert tool.output_schema is not None
    assert _result_types(tool.output_schema) == {"string", "object"}


async def test_generate_chart_publishes_no_output_schema():
    """Image mode returns content blocks, which no structured schema can carry."""
    tool = await _tool("generate_chart")

    assert tool.output_schema is None


async def test_store_error_reaches_the_client_intact(monkeypatch):
    error = {
        "success": False,
        "error": "Failed to initialize Oxigraph store",
        "error_type": "store_not_initialized",
    }

    async def failing_handler(ctx, schema_name, graph_uri, services):
        return error

    monkeypatch.setattr(_h_rdf, "store_ontology_in_rdf", failing_handler)

    async with Client(mcp) as client:
        result = await client.call_tool("store_ontology_in_rdf", {})

    assert result.data == error
