"""Cache hints for the tool list, the resource list and resource reads.

MCP 2026-07-28 (SEP-2549) lets a server say how long a client may keep those
results. Everything cacheable here is safe to keep: the tool list and the
skill files change only with a release, and each chart widget has a URI of its
own. Chart reads carry user data, so the scope is always ``private``.
"""

import pytest
from fastmcp import Client

import src.main as main_module
from src.config import DEFAULT_MCP_CACHE_TTL_SECONDS, resolve_mcp_cache_ttl
from src.main import mcp


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_MCP_CACHE_TTL_SECONDS),
        ("", DEFAULT_MCP_CACHE_TTL_SECONDS),
        ("3600", 3600),
        ("0", 0),  # no hint at all
        ("-5", DEFAULT_MCP_CACHE_TTL_SECONDS),
        ("soon", DEFAULT_MCP_CACHE_TTL_SECONDS),
    ],
)
def test_ttl_setting(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MCP_CACHE_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MCP_CACHE_TTL_SECONDS", raw)

    assert resolve_mcp_cache_ttl() == expected


@pytest.mark.skipif(not main_module._CACHE_TTL, reason="hints disabled in this env")
async def test_a_modern_client_is_told_how_long_it_may_keep_the_lists():
    async with Client(mcp) as client:
        tools = await client.list_tools_mcp()
        resources = await client.list_resources_mcp()

    for result in (tools, resources):
        assert result.ttl_ms == main_module._CACHE_TTL * 1000
        assert result.cache_scope == "private"


async def test_a_handshake_era_client_gets_no_hint():
    async with Client(mcp, mode="legacy") as client:
        tools = await client.list_tools_mcp()

    assert not tools.ttl_ms


async def test_the_tool_list_comes_in_a_stable_order():
    """The spec asks for it, so a client can cache the list and an LLM provider
    can reuse the prompt prefix the tool definitions make up."""
    first = [tool.name for tool in await mcp.list_tools()]
    second = [tool.name for tool in await mcp.list_tools()]

    assert first == second
    assert first[0] == "connect_database"
