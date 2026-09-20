"""cleanup_workspace asks before it deletes, where the client can be asked.

It removes every ontology version, the RDF store and the saved semantic models
of a connection, for everyone using that database, and until now it did so on
a model's say-so alone. How a server asks depends on the protocol era: from MCP
2026-07-28 on the tool returns the question and is called again with the
answer; before that it awaits ``ctx.elicit``. A client without the elicitation
capability is not asked and keeps the behaviour it always had.
"""

import asyncio
import types
from unittest.mock import Mock

import mcp.types as mcp_types
import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

import src.main as main_module
import src.server_state as state_module
from src.handlers import connection as connection_handler
from src.handlers import workspace as workspace_handler
from src.handlers.confirmation import Confirmation, ask_to_confirm
from src.main import mcp
from src.server_state import ServerState

KEY = "cleanup_workspace"


def _ctx(revision: str, can_elicit: bool, responses: object = None, elicit=None):
    return types.SimpleNamespace(
        request_context=types.SimpleNamespace(protocol_version=revision),
        session=types.SimpleNamespace(check_client_capability=lambda _c: can_elicit),
        input_responses=responses,
        elicit=elicit,
    )


def _answer(action: str, confirm: object = None):
    content = None if confirm is None else {"confirm": confirm}
    return types.SimpleNamespace(action=action, content=content)


async def _ask(ctx):
    return await ask_to_confirm(ctx, KEY, "Really?", "Yes")


# --- the helper ---


async def test_a_client_that_cannot_be_asked_is_not_asked():
    for revision in ("2026-07-28", "2025-11-25"):
        assert await _ask(_ctx(revision, can_elicit=False)) is Confirmation.UNAVAILABLE


async def test_modern_era_round_one_returns_the_question():
    outcome = await _ask(_ctx("2026-07-28", can_elicit=True))

    assert isinstance(outcome, mcp_types.InputRequiredResult)
    schema = outcome.input_requests[KEY].params.requested_schema
    assert schema["required"] == ["confirm"]
    assert schema["properties"]["confirm"]["type"] == "boolean"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (_answer("accept", True), Confirmation.CONFIRMED),
        (_answer("accept", False), Confirmation.DECLINED),  # box left unticked
        (_answer("accept"), Confirmation.DECLINED),  # accepted an empty form
        (_answer("decline"), Confirmation.DECLINED),
        (_answer("cancel"), Confirmation.DECLINED),
    ],
)
async def test_modern_era_round_two_reads_the_answer(answer, expected):
    ctx = _ctx("2026-07-28", can_elicit=True, responses={KEY: answer})

    assert await _ask(ctx) is expected


async def test_an_answer_to_a_different_question_is_not_a_confirmation():
    ctx = _ctx(
        "2026-07-28", can_elicit=True, responses={"renames": _answer("accept", True)}
    )

    assert isinstance(await _ask(ctx), mcp_types.InputRequiredResult)


@pytest.mark.parametrize(
    ("action", "data", "expected"),
    [
        ("accept", True, Confirmation.CONFIRMED),
        ("accept", False, Confirmation.DECLINED),
        ("decline", None, Confirmation.DECLINED),
        ("cancel", None, Confirmation.DECLINED),
    ],
)
async def test_handshake_era_awaits_the_answer(action, data, expected):
    async def elicit(message, response_type):
        assert response_type is bool
        return types.SimpleNamespace(action=action, data=data)

    ctx = _ctx("2025-11-25", can_elicit=True, elicit=elicit)

    assert await _ask(ctx) is expected


# --- the real tool ---


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """A connected DuckDB whose workspace directory exists, in a private state."""
    state = ServerState()
    monkeypatch.setattr(state_module, "_server_state", state)
    monkeypatch.setattr(main_module, "_server_state", state)
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(workspace_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.delenv("DUCKDB_DATABASE_PATH", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    yield tmp_path
    state.cleanup()


def _handler(action: str, confirm: bool | None = None):
    """A user who answers the form as a real client would: by filling in the
    one field the schema names, whatever the era calls it."""

    async def answer(message, response_type, params, context):
        assert "permanently deletes" in message
        if confirm is None:
            return ElicitResult(action=action)
        (field,) = params.requested_schema["properties"]
        return ElicitResult(action=action, content={field: confirm})

    return answer


def _workspace_dirs(root) -> list:
    return [p for p in root.iterdir() if p.is_dir() and (p / "metadata.json").exists()]


async def _connect(client: Client) -> dict:
    connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
    return {"connection": connected.data.split("Connection handle: ")[1].split()[0]}


@pytest.mark.parametrize("mode", [None, "legacy"], ids=["modern", "handshake"])
async def test_declining_keeps_the_workspace(workspace, mode):
    kwargs = {} if mode is None else {"mode": mode}
    async with Client(mcp, elicitation_handler=_handler("decline"), **kwargs) as c:
        handle = await _connect(c)
        assert _workspace_dirs(workspace)
        result = await c.call_tool("cleanup_workspace", handle)

    assert "Nothing was deleted" in result.data
    assert _workspace_dirs(workspace)


@pytest.mark.parametrize("mode", [None, "legacy"], ids=["modern", "handshake"])
async def test_confirming_deletes_it(workspace, mode):
    kwargs = {} if mode is None else {"mode": mode}
    handler = _handler("accept", confirm=True)
    async with Client(mcp, elicitation_handler=handler, **kwargs) as c:
        handle = await _connect(c)
        result = await c.call_tool("cleanup_workspace", handle)

    assert "Workspace Cleaned" in result.data
    assert not _workspace_dirs(workspace)


async def test_accepting_without_ticking_the_box_is_not_a_yes(workspace):
    handler = _handler("accept", confirm=False)
    async with Client(mcp, elicitation_handler=handler) as c:
        handle = await _connect(c)
        result = await c.call_tool("cleanup_workspace", handle)

    assert "Nothing was deleted" in result.data
    assert _workspace_dirs(workspace)


@pytest.mark.parametrize("mode", [None, "legacy"], ids=["modern", "handshake"])
async def test_a_client_that_cannot_be_asked_cleans_up_as_before(workspace, mode):
    kwargs = {} if mode is None else {"mode": mode}
    async with Client(mcp, **kwargs) as c:
        handle = await _connect(c)
        result = await c.call_tool("cleanup_workspace", handle)

    assert "Workspace Cleaned" in result.data
    assert not _workspace_dirs(workspace)


async def test_the_question_is_asked_outside_the_writer_lock(workspace):
    """In the handshake era the question blocks until a person answers. Nobody
    else's discover_schema on that database should wait for them."""
    asked, answer_now = asyncio.Event(), asyncio.Event()
    lock_was_free: list[bool] = []

    async def slow_person(message, response_type, params, context):
        asked.set()
        await answer_now.wait()
        return ElicitResult(action="decline")

    async with Client(mcp, mode="legacy", elicitation_handler=slow_person) as c:
        handle = await _connect(c)
        state = state_module._server_state
        session = state.session_for_handle(handle["connection"])
        call = asyncio.create_task(c.call_tool("cleanup_workspace", handle))
        await asyncio.wait_for(asked.wait(), timeout=10)
        lock_was_free.append(not session.runtime.lock.locked())
        answer_now.set()
        await call

    assert lock_was_free == [True]


# --- an approval names what it approved ---


def _url_only_ctx(revision: str = "2026-07-28"):
    """A client that can only open a URL, not render a form."""
    capabilities = mcp_types.ClientCapabilities(
        elicitation=mcp_types.ElicitationCapability(
            url=mcp_types.UrlElicitationCapability()
        )
    )
    session = types.SimpleNamespace(
        client_params=types.SimpleNamespace(capabilities=capabilities),
        # The SDK's own check only tests that *some* elicitation was declared.
        check_client_capability=lambda _capability: True,
    )
    return types.SimpleNamespace(
        request_context=types.SimpleNamespace(protocol_version=revision),
        session=session,
        input_responses=None,
    )


@pytest.mark.parametrize("revision", ["2026-07-28", "2025-11-25"])
async def test_a_url_only_client_is_not_sent_a_form(revision):
    """It would render nothing the user can answer, so the question would
    hang unanswered instead of protecting anything."""
    outcome = await ask_to_confirm(
        _url_only_ctx(revision), KEY, "Delete?", "Yes", scope="conn"
    )

    assert outcome is Confirmation.UNAVAILABLE


async def test_a_client_offering_both_kinds_is_asked():
    capabilities = mcp_types.ClientCapabilities(
        elicitation=mcp_types.ElicitationCapability(
            form=mcp_types.FormElicitationCapability(),
            url=mcp_types.UrlElicitationCapability(),
        )
    )
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(protocol_version="2026-07-28"),
        session=types.SimpleNamespace(
            client_params=types.SimpleNamespace(capabilities=capabilities)
        ),
        input_responses=None,
    )

    assert isinstance(
        await ask_to_confirm(ctx, KEY, "Delete?", "Yes"),
        mcp_types.InputRequiredResult,
    )


async def test_a_client_predating_the_distinction_is_asked():
    """Bare `elicitation: {}` meant the form, before URL elicitation existed."""
    capabilities = mcp_types.ClientCapabilities(
        elicitation=mcp_types.ElicitationCapability()
    )
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(protocol_version="2025-11-25"),
        session=types.SimpleNamespace(
            client_params=types.SimpleNamespace(capabilities=capabilities)
        ),
        elicit=lambda message, response_type: _accepted(),
        input_responses=None,
    )

    assert await ask_to_confirm(ctx, KEY, "Delete?", "Yes") is Confirmation.CONFIRMED


async def _accepted():
    return types.SimpleNamespace(action="accept", data=True)


async def test_the_question_carries_what_it_is_about():
    outcome = await ask_to_confirm(
        _ctx("2026-07-28", can_elicit=True), KEY, "Delete?", "Yes", scope="conn-old"
    )

    assert isinstance(outcome, mcp_types.InputRequiredResult)
    assert outcome.request_state == "conn-old"


async def test_an_answer_about_another_connection_is_not_a_yes():
    """The second round is a separate request. If the session connected
    elsewhere in between, the approval was for the database that is gone."""
    ctx = _ctx("2026-07-28", can_elicit=True, responses={KEY: _answer("accept", True)})
    ctx.request_state = "conn-old"

    assert await ask_to_confirm(ctx, KEY, "Delete?", "Yes", scope="conn-new") is (
        Confirmation.STALE
    )


async def test_an_answer_about_this_connection_is_a_yes():
    ctx = _ctx("2026-07-28", can_elicit=True, responses={KEY: _answer("accept", True)})
    ctx.request_state = "conn-old"

    assert await ask_to_confirm(ctx, KEY, "Delete?", "Yes", scope="conn-old") is (
        Confirmation.CONFIRMED
    )


@pytest.mark.parametrize("mode", [None, "legacy"], ids=["modern", "handshake"])
async def test_reconnecting_while_the_question_is_open_deletes_nothing(
    workspace, monkeypatch, mode
):
    """The reviewer's reproduction: approval for the old database must not
    delete the workspace of the one the session moved to."""
    deleted: list[str] = []

    async def record_only(ctx, services):
        deleted.append(services.get_session_data(ctx).connection_id)
        return "simulated deletion"

    monkeypatch.setattr(main_module._h_workspace, "cleanup_workspace", record_only)

    def move_elsewhere(session):
        state = state_module._server_state
        state.bind_session(session, "another-database", Mock(is_connected=lambda: True))
        session.connection_id = "another-database"

    asked: list[str] = []

    async def answer_then_reconnect(message, response_type, params, context):
        asked.append(message)
        state = state_module._server_state
        move_elsewhere(state.session_for_handle(handle["connection"]))
        (field,) = params.requested_schema["properties"]
        return ElicitResult(action="accept", content={field: True})

    kwargs = {} if mode is None else {"mode": mode}
    async with Client(mcp, elicitation_handler=answer_then_reconnect, **kwargs) as c:
        handle = await _connect(c)
        original = state_module._server_state.session_for_handle(
            handle["connection"]
        ).connection_id
        result = await c.call_tool("cleanup_workspace", handle)

    assert asked and original[:8] in asked[0]
    assert deleted == []
    assert "connection changed" in result.data
    assert _workspace_dirs(workspace)


@pytest.mark.parametrize("mode", [None, "legacy"], ids=["modern", "handshake"])
async def test_reconnecting_while_queued_for_the_writer_lock_deletes_nothing(
    workspace, monkeypatch, mode
):
    """The question is asked before the connection's writer lock is taken, so
    a human answer cannot block other clients. That leaves a second window:
    the wait for the lock itself, which lasts as long as another writer needs.
    A connect_database landing in it must not spend the approval elsewhere."""
    state = state_module._server_state
    deleted: list[str] = []

    async def record_only(ctx, services, approved_connection_id=None):
        deleted.append(services.get_session_data(ctx).connection_id)
        return "simulated deletion"

    monkeypatch.setattr(main_module._h_workspace, "cleanup_workspace", record_only)

    lock_wanted = asyncio.Event()
    real_writer_lock = state.writer_lock

    def announce_then_wait(session):
        lock_wanted.set()
        return real_writer_lock(session)

    monkeypatch.setattr(state, "writer_lock", announce_then_wait)

    kwargs = {} if mode is None else {"mode": mode}
    async with Client(mcp, elicitation_handler=_handler("accept", True), **kwargs) as c:
        handle = await _connect(c)
        session = state.session_for_handle(handle["connection"])
        approved = session.connection_id

        # Another writer holds the lock this cleanup needs.
        original = session.runtime
        await original.lock.acquire()
        call = asyncio.create_task(c.call_tool("cleanup_workspace", handle))
        await asyncio.wait_for(lock_wanted.wait(), timeout=10)
        assert not call.done()  # approved, and queued behind the other writer

        state.bind_session(session, "replacement-db", Mock(is_connected=lambda: True))
        session.connection_id = "replacement-db"
        original.lock.release()  # the queued cleanup proceeds
        result = await call

    assert approved != "replacement-db"
    assert deleted == []
    assert "connection changed" in result.data
    assert _workspace_dirs(workspace)
