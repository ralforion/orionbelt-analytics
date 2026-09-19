"""The connection handle: how a client without a transport session comes back.

MCP 2026-07-28 removed protocol-level sessions and tells servers that need
state across calls to mint a handle and take it back as an ordinary tool
argument. Here the handle names a ``SessionData``, so it does for a sessionless
client what the MCP session ID does for the others -- including keeping its
ontology state apart from everyone else's on the same database.
"""

import asyncio
import re
import types

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import src.main as main_module
import src.server_state as state_module
from src.exceptions import SessionRequiredError, UnknownConnectionError
from src.handlers import connection as connection_handler
from src.main import mcp
from src.server_state import (
    ServerState,
    begin_connection_scope,
    end_connection_scope,
    get_session_data,
    normalize_handle,
)

HANDLE = re.compile(r"^ob_[a-km-np-z2-9]{6}$")


# FastMCP 4's client speaks the sessionless 2026-07-28 era unless told
# otherwise; FastMCP 3's only knows the handshake era and has no such switch.
def _client_knows_eras() -> bool:
    try:
        Client(mcp, mode="legacy")
    except TypeError:
        return False
    return True


CLIENT_KNOWS_ERAS = _client_knows_eras()


def _handshake_era_client() -> Client:
    """A client that keeps an MCP transport session, on either FastMCP."""
    return Client(mcp, mode="legacy") if CLIENT_KNOWS_ERAS else Client(mcp)


def _ctx(session_id: str | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(session_id=session_id)


@pytest.fixture
def state(monkeypatch) -> ServerState:
    """A private ServerState behind every module-level helper."""
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    yield fresh
    fresh.cleanup()


def _fallback(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        state_module, "_sessionless_fallback_enabled", lambda: mode == "sole_session"
    )


# --- minting ---


def test_every_session_gets_a_short_unambiguous_handle(state):
    handles = {state.get_session(f"s{i}").handle for i in range(50)}

    assert len(handles) == 50
    assert all(HANDLE.match(h) for h in handles)


def test_a_released_sessions_handle_is_dead(state):
    handle = state.get_session("s1").handle

    state.cleanup_session("s1")

    with pytest.raises(UnknownConnectionError):
        state.session_for_handle(handle)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" OB_K2M9QA \n", "ob_k2m9qa"),
        ("", None),
        ("   ", None),
        (None, None),
        (7, None),
    ],
)
def test_handles_are_normalized_before_lookup(raw, expected):
    assert normalize_handle(raw) == expected


# --- resolution order ---


def test_a_handle_wins_over_the_transport_session(state):
    mine = state.get_session("transport-a")
    other = state.get_session("transport-b")

    token = begin_connection_scope(_ctx("transport-a"), other.handle, mint=False)
    try:
        assert get_session_data(_ctx("transport-a")) is other
    finally:
        end_connection_scope(token)
    assert get_session_data(_ctx("transport-a")) is mine


def test_without_session_or_handle_the_sole_sessionless_session_is_used(
    state, monkeypatch, caplog
):
    _fallback(monkeypatch, "sole_session")
    only = state.open_handle_session()

    with caplog.at_level("WARNING"):
        assert get_session_data(_ctx(None)) is only
        assert get_session_data(_ctx(None)) is only

    # Said once per session, and it names the way out.
    notes = [r for r in caplog.records if "SESSIONLESS_FALLBACK=none" in r.message]
    assert len(notes) == 1


def test_a_transport_session_is_never_the_fallback(state, monkeypatch):
    """Its client identifies itself on every request, so a caller who does not
    cannot be that client, however alone that session is."""
    _fallback(monkeypatch, "sole_session")
    state.get_session("transport-a")

    with pytest.raises(SessionRequiredError):
        get_session_data(_ctx(None))

    # Next to it, the one sessionless session is still unambiguous.
    mine = state.open_handle_session()
    assert get_session_data(_ctx(None)) is mine


def test_with_several_sessions_a_bare_request_is_refused_not_guessed(
    state, monkeypatch
):
    _fallback(monkeypatch, "sole_session")
    state.open_handle_session()
    state.open_handle_session()

    with pytest.raises(SessionRequiredError) as raised:
        get_session_data(_ctx(None))

    assert "connect_database" in str(raised.value)
    assert "`connection`" in str(raised.value)


def test_the_fallback_can_be_switched_off(state, monkeypatch):
    _fallback(monkeypatch, "none")
    state.open_handle_session()

    with pytest.raises(SessionRequiredError):
        get_session_data(_ctx(None))


def test_a_wrong_handle_never_lands_in_someone_elses_session(state, monkeypatch):
    """Not even in the sole live one: a handle that was given must match."""
    _fallback(monkeypatch, "sole_session")
    state.get_session("transport-a")

    with pytest.raises(UnknownConnectionError) as raised:
        begin_connection_scope(_ctx(None), "ob_zzzzzz", mint=False)

    assert raised.value.to_response()["error_type"] == "unknown_connection"
    assert "connect_database" in str(raised.value)


def test_connecting_without_a_session_opens_one(state):
    token = begin_connection_scope(_ctx(None), None, mint=True)
    try:
        session = get_session_data(_ctx(None))
        assert HANDLE.match(session.handle)
        assert state.session_for_handle(session.handle) is session
    finally:
        end_connection_scope(token)


def test_connecting_with_a_transport_session_opens_nothing_extra(state):
    token = begin_connection_scope(_ctx("transport-a"), None, mint=True)
    try:
        get_session_data(_ctx("transport-a"))
    finally:
        end_connection_scope(token)

    assert state.session_count == 1


async def test_concurrent_calls_do_not_see_each_others_handle(state):
    first, second = state.get_session("a"), state.get_session("b")
    seen: dict[str, object] = {}

    async def call(name: str, handle: str) -> None:
        token = begin_connection_scope(_ctx(None), handle, mint=False)
        try:
            await asyncio.sleep(0.01)
            seen[name] = get_session_data(_ctx(None))
        finally:
            end_connection_scope(token)

    await asyncio.gather(call("first", first.handle), call("second", second.handle))

    assert seen == {"first": first, "second": second}


# --- the published tool surface ---


async def test_every_tool_takes_an_optional_connection_and_there_are_still_28():
    tools = await mcp.list_tools()

    assert len(tools) == 28
    for tool in tools:
        properties = tool.parameters["properties"]
        assert "connection" in properties, tool.name
        assert "connection" not in tool.parameters.get("required", []), tool.name
        assert "ctx" not in properties, tool.name


# --- end to end, with the transport session switched off ---


@pytest.fixture
def duckdb_workspace(state, monkeypatch, tmp_path):
    """An in-memory DuckDB to connect to, with its workspace in a temp dir."""
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.delenv("DUCKDB_DATABASE_PATH", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    return state


@pytest.fixture
def sessionless_era(duckdb_workspace, monkeypatch):
    """What a 2026-07-28 client looks like to the server: no session ID."""
    monkeypatch.setattr(state_module, "_transport_session_id", lambda _ctx: None)
    monkeypatch.setattr(main_module, "_transport_session_id", lambda _ctx: None)
    _fallback(monkeypatch, "sole_session")
    return duckdb_workspace


def _handle_in(text: str) -> str:
    match = re.search(r"Connection handle: (ob_[a-z0-9]{6})", text)
    assert match, text
    return match.group(1)


async def test_a_sessionless_client_works_through_its_handle(sessionless_era):
    state = sessionless_era
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
        handle = _handle_in(connected.data)

        # The handle brings the client back to its session.
        schemas = await client.call_tool("list_schemas", {"connection": handle})
        assert "main" in schemas.data

        # Alone on the server, even a dropped handle is forgiven.
        assert (await client.call_tool("list_schemas", {})).data == schemas.data

        # Dict results remind the caller of its handle.
        reset = await client.call_tool("reset_cache", {"connection": handle})
        assert reset.data["connection"] == handle

        # A second user connects: a session of their own, on the same database.
        second = _handle_in(
            (await client.call_tool("connect_database", {"db_type": "duckdb"})).data
        )
        assert second != handle
        mine, theirs = state.session_for_handle(handle), state.session_for_handle(
            second
        )
        assert mine is not theirs
        assert mine.runtime is theirs.runtime  # one manager, one schema cache
        assert mine.runtime.holders == 2

        # Ontology state is per user: what one loads, the other does not see.
        mine.set_current_schema("main")
        mine.loaded_ontology = "<my custom ontology>"
        theirs.set_current_schema("main")
        assert theirs.loaded_ontology is None

        # With two users, a bare request is no longer guessable...
        with pytest.raises(ToolError, match="connection"):
            await client.call_tool("list_schemas", {})
        # ...and a mistyped handle is an error, not somebody else's session.
        with pytest.raises(ToolError, match="Unknown or expired"):
            await client.call_tool("list_schemas", {"connection": "ob_zzzzzz"})


async def test_a_client_with_a_transport_session_is_told_its_handle_once(
    duckdb_workspace,
):
    async with _handshake_era_client() as client:
        connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
        handle = _handle_in(connected.data)
        reset = await client.call_tool("reset_cache", {})
        explicit = await client.call_tool("reset_cache", {"connection": handle})

    assert "connection" not in reset.data  # it has a session; no reminders
    assert explicit.data["connection"] == handle


# --- configuration ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "sole_session"), ("NONE", "none"), ("sole_session", "sole_session")],
)
def test_sessionless_fallback_setting(monkeypatch, raw, expected):
    from src.config import ConfigManager

    if raw is None:
        monkeypatch.delenv("SESSIONLESS_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("SESSIONLESS_FALLBACK", raw)

    assert ConfigManager().get_server_config().sessionless_fallback == expected


def test_an_invalid_fallback_setting_keeps_the_default(monkeypatch, caplog):
    from src.config import ConfigManager

    monkeypatch.setenv("SESSIONLESS_FALLBACK", "whoever")

    assert ConfigManager().get_server_config().sessionless_fallback == "sole_session"
    assert "SESSIONLESS_FALLBACK" in caplog.text


# --- telling the eras apart ---


def _era_ctx(version: object, session_id: str = "per-request-uuid"):
    request_context = types.SimpleNamespace(protocol_version=version)
    return types.SimpleNamespace(session_id=session_id, request_context=request_context)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2025-11-25", "per-request-uuid"),  # handshake era: a real session
        ("2026-07-28", None),  # FastMCP 4 mints this ID per request
        ("2027-01-15", None),  # later revisions stay sessionless
        (None, "per-request-uuid"),  # FastMCP 3 does not report a revision
    ],
)
def test_the_session_id_is_disregarded_in_the_sessionless_era(version, expected):
    assert state_module._transport_session_id(_era_ctx(version)) == expected


def test_the_revision_is_also_read_from_the_server_session():
    session = types.SimpleNamespace(protocol_version="2026-07-28")
    ctx = types.SimpleNamespace(
        session_id="per-request-uuid",
        request_context=types.SimpleNamespace(session=session),
    )

    assert state_module._transport_session_id(ctx) is None


def test_a_per_request_id_does_not_open_a_session_per_call(state, monkeypatch):
    """The failure this guards against: every modern call landing in a new,
    empty session because its throwaway ID looked like a real one."""
    _fallback(monkeypatch, "sole_session")
    only = state.open_handle_session()

    for call in range(3):
        assert get_session_data(_era_ctx("2026-07-28", f"uuid-{call}")) is only

    assert state.session_count == 1


@pytest.mark.skipif(
    not CLIENT_KNOWS_ERAS, reason="needs a client that speaks MCP 2026-07-28"
)
async def test_a_real_sessionless_client_works_through_its_handle(duckdb_workspace):
    """The same journey with nothing patched: FastMCP 4 reports a fresh
    ``ctx.session_id`` on every modern request, and it must not be believed."""
    state = duckdb_workspace
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
        handle = _handle_in(connected.data)
        assert state.session_count == 1

        schemas = await client.call_tool("list_schemas", {"connection": handle})
        assert "main" in schemas.data
        assert (await client.call_tool("list_schemas", {})).data == schemas.data
        assert state.session_count == 1  # no session per request

        second = _handle_in(
            (await client.call_tool("connect_database", {"db_type": "duckdb"})).data
        )
        assert second != handle
        with pytest.raises(ToolError, match="connection"):
            await client.call_tool("list_schemas", {})


# --- refusals at the tool boundary ---


async def test_a_refusal_reaches_the_model_verbatim_and_without_a_stack_trace(
    sessionless_era, caplog
):
    """FastMCP wraps and logs a stack trace for any exception but its own
    ToolError. A model that forgets its handle would write one on every call,
    and ``mask_error_details`` would replace the message that says how to
    recover."""
    async with Client(mcp) as client:
        await client.call_tool("connect_database", {"db_type": "duckdb"})
        await client.call_tool("connect_database", {"db_type": "duckdb"})
        with caplog.at_level("DEBUG"):
            with pytest.raises(ToolError) as bare:
                await client.call_tool("list_schemas", {})
            with pytest.raises(ToolError) as unknown:
                await client.call_tool("list_schemas", {"connection": "ob_zzzzzz"})

    assert str(bare.value).startswith("This request carries neither")
    assert str(unknown.value).startswith("Unknown or expired connection handle")
    assert not [r for r in caplog.records if r.exc_info]
