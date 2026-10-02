"""Every tool says what it does to its environment.

MCP tool annotations let a client, or an approval layer in front of it, tell
the tools that only read from those that change state and from those that
delete -- without a rule written for this server. The tests read them as a
client does, from the tool list.
"""

from fastmcp import Client

from src.main import mcp

DESTRUCTIVE = {
    "reset_cache",
    "cleanup_workspace",
    "cleanup_old_versions",
    "save_semantic_model",
}
MUST_BE_READ_ONLY = {
    "execute_sql_query",
    "sample_table_data",
    "query_sparql",
    "list_schemas",
    "graphrag_find_join_path",
}


async def _annotations() -> dict[str, dict]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return {
        tool.name: (
            tool.annotations.model_dump(by_alias=True) if tool.annotations else {}
        )
        for tool in tools
    }


async def test_every_tool_declares_whether_it_reads_or_writes():
    annotations = await _annotations()

    unannotated = [
        name for name, a in annotations.items() if a.get("readOnlyHint") is None
    ]
    assert unannotated == []


async def test_exactly_the_tools_that_delete_are_destructive():
    annotations = await _annotations()

    destructive = {name for name, a in annotations.items() if a.get("destructiveHint")}
    assert destructive == DESTRUCTIVE


async def test_queries_are_read_only():
    annotations = await _annotations()

    for name in MUST_BE_READ_ONLY:
        assert annotations[name]["readOnlyHint"] is True, name


async def test_a_read_only_tool_is_never_marked_destructive():
    annotations = await _annotations()

    for name, a in annotations.items():
        if a.get("readOnlyHint"):
            assert not a.get("destructiveHint"), name


async def test_no_tool_claims_the_open_world():
    annotations = await _annotations()

    assert {
        name for name, a in annotations.items() if a.get("openWorldHint") is not False
    } == set()
