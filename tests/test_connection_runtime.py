"""State derived from a database is shared by every session connected to it.

The database manager connects with the server's own credentials and the schema
cache is a set of facts about that database, so neither belongs to a client.
``ServerState`` keeps one ``ConnectionRuntime`` per connection ID and binds
sessions to it. This is the ground the connection handle (for clients without
a transport session, MCP 2026-07-28) will stand on.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from src.handler_context import HandlerContext
from src.handlers import connection as connection_handler
from src.server_state import ServerState, _clear_session_state, _server_state
from src.session import SessionData


class FakeManager:
    """Stands in for DatabaseManager: tracks connect and disconnect."""

    def __init__(self, database: str = "sales") -> None:
        self.connected = False
        self.disconnects = 0
        self.connection_info: dict[str, object] = {}
        self._database = database

    def connect_postgresql(self, **kwargs: object) -> bool:
        self.connected = True
        self.connection_info = {
            "database_type": "postgresql",
            "host": kwargs["host"],
            "port": kwargs["port"],
            "database": kwargs["database"],
        }
        return True

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1


def _connected(database: str = "sales") -> FakeManager:
    manager = FakeManager(database)
    manager.connected = True
    return manager


# --- the registry ---


def test_sessions_on_one_connection_share_manager_and_schema_cache():
    state = ServerState()
    first, second = state.get_session("a"), state.get_session("b")
    donated, redundant = _connected(), _connected()

    runtime = state.bind_session(first, "conn-1", donated)
    assert state.bind_session(second, "conn-1", redundant) is runtime

    assert runtime.holders == 2
    assert first.db_manager is donated
    assert second.db_manager is donated
    assert redundant.disconnects == 1  # the second session's own is not needed
    assert donated.connected

    first.cache_schema_analysis("public", ["orders", "customers"])
    assert second.get_cached_schema("public") == ["orders", "customers"]


def test_sessions_on_different_connections_share_nothing():
    state = ServerState()
    first, second = state.get_session("a"), state.get_session("b")

    state.bind_session(first, "conn-1", _connected("sales"))
    state.bind_session(second, "conn-2", _connected("hr"))

    first.cache_schema_analysis("public", ["orders"])
    assert second.get_cached_schema("public") is None
    assert first.db_manager is not second.db_manager


def test_leaving_keeps_the_runtime_alive_for_the_others():
    state = ServerState()
    first, second = state.get_session("a"), state.get_session("b")
    manager = _connected()
    state.bind_session(first, "conn-1", manager)
    state.bind_session(second, "conn-1", _connected())
    first.cache_schema_analysis("public", ["orders"])

    state.unbind_session(first)

    assert manager.connected
    assert second.get_cached_schema("public") == ["orders"]
    # The leaver is back to private, empty state.
    assert first.runtime is None
    assert first.db_manager is None
    assert first.get_cached_schema("public") is None


def test_the_last_holder_closes_the_runtime():
    state = ServerState()
    session = state.get_session("a")
    manager = _connected()
    state.bind_session(session, "conn-1", manager)

    state.unbind_session(session)

    assert manager.disconnects == 1
    assert state.get_runtime("conn-1") is None
    state.unbind_session(session)  # unbound: a no-op
    assert manager.disconnects == 1


def test_reconnecting_while_bound_keeps_the_healthy_shared_manager():
    state = ServerState()
    session = state.get_session("a")
    shared, fresh = _connected(), _connected()
    runtime = state.bind_session(session, "conn-1", shared)

    assert state.bind_session(session, "conn-1", fresh) is runtime

    assert runtime.holders == 1  # same session, not a second holder
    assert session.db_manager is shared
    assert fresh.disconnects == 1
    assert shared.disconnects == 0


def test_a_dead_shared_manager_is_replaced_by_the_fresh_one():
    state = ServerState()
    first, second = state.get_session("a"), state.get_session("b")
    dead, fresh = _connected(), _connected()
    state.bind_session(first, "conn-1", dead)
    dead.connected = False  # the database went away underneath it

    state.bind_session(second, "conn-1", fresh)

    assert first.db_manager is fresh
    assert second.db_manager is fresh
    assert dead.disconnects == 1


def test_switching_connection_moves_the_session_between_runtimes():
    state = ServerState()
    session, other = state.get_session("a"), state.get_session("b")
    state.bind_session(session, "conn-1", _connected("sales"))
    state.bind_session(other, "conn-1", _connected("sales"))
    hr = _connected("hr")

    state.bind_session(session, "conn-2", hr)

    assert session.db_manager is hr
    assert state.get_runtime("conn-1").holders == 1
    assert state.get_runtime("conn-2").holders == 1


def test_clearing_one_sessions_state_does_not_empty_the_shared_cache():
    """A connection change used to clear the session's caches in place, which
    would now empty them for everybody still on the old connection."""
    first = _server_state.get_session("runtime-clear-a")
    second = _server_state.get_session("runtime-clear-b")
    try:
        _server_state.bind_session(first, "conn-clear", _connected())
        _server_state.bind_session(second, "conn-clear", _connected())
        first.cache_schema_analysis("public", ["orders"])

        _clear_session_state(first, reason="connection change")

        assert first.runtime is None
        assert second.get_cached_schema("public") == ["orders"]
    finally:
        _server_state.cleanup_session("runtime-clear-a")
        _server_state.cleanup_session("runtime-clear-b")


def test_evicting_a_session_does_not_disconnect_the_shared_manager():
    state = ServerState()
    state.get_session("idle")
    state.get_session("active")
    manager = _connected()
    state.bind_session(state.get_session("idle"), "conn-1", manager)
    state.bind_session(state.get_session("active"), "conn-1", _connected())

    state.cleanup_session("idle")

    assert manager.connected
    assert state.get_session("active").db_manager is manager

    state.cleanup_session("active")
    assert manager.disconnects == 1


def test_an_unbound_session_still_owns_its_manager():
    """No registry involved (tests, batch use): nothing changes."""
    session = SessionData()
    manager = _connected()

    session.db_manager = manager

    assert session.db_manager is manager
    assert session.runtime is None


# --- connect_database ---


@pytest.fixture
def postgres_env(monkeypatch):
    for name, value in {
        "POSTGRES_HOST": "db.internal",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DATABASE": "sales",
        "POSTGRES_USERNAME": "svc",
        "POSTGRES_PASSWORD": "secret",
    }.items():
        monkeypatch.setenv(name, value)


def _services(state: ServerState, session: SessionData, own: FakeManager):
    from src.server_state import _get_connection_fingerprint

    def get_session_db_manager(_ctx):
        if session.db_manager is None:
            session.db_manager = own
        return session.db_manager

    return HandlerContext(
        get_session_data=lambda _ctx: session,
        get_session_db_manager=get_session_db_manager,
        get_connection_fingerprint=_get_connection_fingerprint,
        clear_session_state=_clear_session_state,
        server_state=state,
    )


async def test_connect_database_binds_the_session_to_the_shared_runtime(
    postgres_env, monkeypatch, tmp_path
):
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setattr(connection_handler, "mutate_workspace_metadata", AsyncMock())
    state = ServerState()
    first, second = state.get_session("a"), state.get_session("b")
    first_manager, second_manager = FakeManager(), FakeManager()
    ctx = Mock()
    ctx.info = AsyncMock()

    await connection_handler.connect_database(
        ctx, "postgresql", _services(state, first, first_manager)
    )
    first.cache_schema_analysis("public", ["orders"])
    await connection_handler.connect_database(
        ctx, "postgresql", _services(state, second, second_manager)
    )

    assert first.runtime is second.runtime
    assert second.db_manager is first_manager
    assert second_manager.disconnects == 1
    # The second client joined a warm cache instead of emptying it.
    assert second.get_cached_schema("public") == ["orders"]


async def test_reconnecting_never_touches_the_manager_others_are_using(
    postgres_env, monkeypatch, tmp_path
):
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setattr(connection_handler, "mutate_workspace_metadata", AsyncMock())
    fresh_managers: list[FakeManager] = []

    def fresh_manager() -> FakeManager:
        fresh_managers.append(FakeManager())
        return fresh_managers[-1]

    monkeypatch.setattr(connection_handler, "DatabaseManager", fresh_manager)
    state = ServerState()
    session = state.get_session("a")
    shared = FakeManager()
    ctx = Mock()
    ctx.info = AsyncMock()
    services = _services(state, session, shared)

    await connection_handler.connect_database(ctx, "postgresql", services)
    shared.connect_postgresql = Mock(side_effect=AssertionError("reconnected"))
    await connection_handler.connect_database(ctx, "postgresql", services)

    assert session.db_manager is shared
    assert len(fresh_managers) == 1
    assert fresh_managers[0].disconnects == 1  # redundant, so let go


# --- what is shared (GraphRAG, the writer lock) and what is not (ontology) ---


def _two_sessions_on_one_connection(state: ServerState):
    first, second = state.get_session("a"), state.get_session("b")
    state.bind_session(first, "conn-1", _connected())
    state.bind_session(second, "conn-1", _connected())
    return first, second


def test_each_session_keeps_its_own_ontology_on_a_shared_database():
    """Which ontology is active is a user's choice, not a database fact. One
    user loading or renaming theirs must not swap the other's mid-conversation,
    even for the same schema."""
    state = ServerState()
    first, second = _two_sessions_on_one_connection(state)
    first.set_current_schema("public")
    second.set_current_schema("public")

    first.ontology_file = "ontology_public_v3.ttl"
    first.loaded_ontology = "<generated ttl>"
    first.obqc_validator = Mock(name="validator-for-generated")
    second.loaded_ontology = "<custom ttl from load_my_ontology>"
    second.ontology_enriched = True

    assert first.loaded_ontology == "<generated ttl>"
    assert first.ontology_enriched is False
    assert second.ontology_file is None
    assert second.obqc_validator is None  # builds its own, from its own ontology


def test_the_current_schema_is_per_session():
    state = ServerState()
    first, second = _two_sessions_on_one_connection(state)

    first.set_current_schema("public")
    second.set_current_schema("analytics")

    assert first.current_schema == "public"
    assert second.current_schema == "analytics"


def test_graphrag_is_an_index_of_the_database_and_is_built_once():
    state = ServerState()
    first, second = _two_sessions_on_one_connection(state)
    graphrag = Mock(name="graphrag")

    first.graphrag_manager = graphrag
    first.graphrag_initialized = True

    assert second.graphrag_manager is graphrag
    assert second.graphrag_initialized is True


def test_a_connection_change_leaves_the_others_state_alone():
    first = _server_state.get_session("runtime-onto-a")
    second = _server_state.get_session("runtime-onto-b")
    try:
        _server_state.bind_session(first, "conn-onto", _connected())
        _server_state.bind_session(second, "conn-onto", _connected())
        first.graphrag_manager = Mock(name="graphrag")
        second.set_current_schema("public")
        second.ontology_file = "ontology_public.ttl"

        _clear_session_state(first, reason="connection change")

        assert first.graphrag_manager is None
        assert second.graphrag_manager is not None
        assert second.ontology_file == "ontology_public.ttl"
    finally:
        _server_state.cleanup_session("runtime-onto-a")
        _server_state.cleanup_session("runtime-onto-b")


async def test_shared_init_tasks_outlive_one_leaving_session():
    """Closing one of two sessions must not cancel the GraphRAG init the other
    is waiting for; closing the last one must."""
    import asyncio

    state = ServerState()
    first, second = _two_sessions_on_one_connection(state)
    started = asyncio.Event()

    async def init():
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(init())
    first.graphrag.track_init_task(task)
    await started.wait()

    await state.aclose_session("a")
    assert not task.cancelled()
    assert task in second.graphrag.init_tasks

    await state.aclose_session("b")
    assert task.cancelled()


async def test_work_started_by_a_dying_session_still_lands_in_shared_state():
    state = ServerState()
    first, second = _two_sessions_on_one_connection(state)
    graphrag = Mock(name="graphrag")

    await state.aclose_session("a")
    # A GraphRAG init holding `first` finishes after the session is gone.
    first.graphrag_manager = graphrag
    first.graphrag_initialized = True

    assert second.graphrag_manager is graphrag


async def test_the_writer_lock_serializes_sessions_on_one_connection():
    import asyncio

    state = ServerState()
    first, second = _two_sessions_on_one_connection(state)
    elsewhere = state.get_session("c")
    state.bind_session(elsewhere, "conn-2", _connected("hr"))
    order: list[str] = []

    async def writer(name: str, session: SessionData) -> None:
        async with state.writer_lock(session):
            order.append(f"{name}:in")
            await asyncio.sleep(0.01)
            order.append(f"{name}:out")

    await asyncio.gather(writer("first", first), writer("second", second))
    assert order == ["first:in", "first:out", "second:in", "second:out"]

    # Another connection is not held up, and an unbound session takes no lock.
    async with state.writer_lock(first):
        await asyncio.wait_for(writer("elsewhere", elsewhere), timeout=1)
        await asyncio.wait_for(writer("unbound", SessionData()), timeout=1)


async def test_a_writing_tool_waits_for_the_connections_lock(monkeypatch):
    import asyncio

    import src.main as main_module
    from src.main import _h_schema, reset_cache

    state = ServerState()
    first, second = _two_sessions_on_one_connection(state)
    monkeypatch.setattr(main_module, "_server_state", state)
    monkeypatch.setattr(main_module, "get_session_data", lambda _ctx: second)
    handler = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(_h_schema, "reset_cache", handler)

    async with state.writer_lock(first):
        call = asyncio.create_task(reset_cache(Mock(), "schema"))
        await asyncio.sleep(0.05)
        handler.assert_not_awaited()  # another client is rewriting this state

    assert await call == {"success": True}
    handler.assert_awaited_once()
