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
