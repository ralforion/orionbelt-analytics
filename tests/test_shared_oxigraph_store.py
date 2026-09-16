"""The Oxigraph store is shared per connection, not opened per session.

RocksDB allows exactly one open handle per directory, even inside a single
process. A client that reconnects gets a fresh MCP session on the same
connection; before ``ServerState`` shared the handle, that session could not
open the store while the previous session still held it, so every SPARQL-backed
tool answered "Failed to initialize Oxigraph store" until the old session was
evicted -- 30 minutes by default.
"""

import types
from unittest.mock import Mock, patch

import pytest

from src.oxigraph_store import OXIGRAPH_AVAILABLE, OxigraphStoreManager
from src.server_state import (
    ServerState,
    _clear_session_state,
    _server_state,
    get_oxigraph_store,
)

pytestmark = pytest.mark.skipif(not OXIGRAPH_AVAILABLE, reason="pyoxigraph missing")

ASK_ANY = "ASK { ?s ?p ?o }"


def _ctx(session_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(session_id=session_id)


def test_rocksdb_refuses_a_second_open_of_the_same_directory(tmp_path):
    """The constraint the registry exists for: one handle per directory."""
    first = OxigraphStoreManager(store_path=tmp_path)
    try:
        with pytest.raises(OSError):
            OxigraphStoreManager(store_path=tmp_path)
    finally:
        first.close()


def test_two_sessions_share_one_handle(tmp_path):
    state = ServerState()

    first = state.acquire_oxigraph_store(tmp_path)
    second = state.acquire_oxigraph_store(tmp_path)

    assert second is first
    assert state.oxigraph_store_refcount(tmp_path) == 2

    state.release_oxigraph_store(first)
    assert state.oxigraph_store_refcount(tmp_path) == 1
    assert second.query_sparql_ask(ASK_ANY) is False  # still open

    state.release_oxigraph_store(second)
    assert state.oxigraph_store_refcount(tmp_path) == 0

    # The last release closed the store: the directory can be opened again.
    reopened = state.acquire_oxigraph_store(tmp_path)
    assert reopened is not first
    state.release_oxigraph_store(reopened)


def test_reconnecting_session_gets_the_open_store(tmp_path):
    """Regression: a second session on the same connection must not get None."""
    old_ctx, new_ctx = _ctx("shared-store-old"), _ctx("shared-store-new")
    with patch("src.server_state.get_oxigraph_store_dir", return_value=tmp_path):
        try:
            for ctx in (old_ctx, new_ctx):
                _server_state.get_session(ctx.session_id).connection_id = "conn-1"

            old = get_oxigraph_store(old_ctx)
            new = get_oxigraph_store(new_ctx)

            assert old is not None
            assert new is old

            # Evicting the old session leaves the store open for the new one.
            _server_state.cleanup_session(old_ctx.session_id)
            assert _server_state.oxigraph_store_refcount(tmp_path) == 1
            assert new.query_sparql_ask(ASK_ANY) is False
        finally:
            _server_state.cleanup_session(old_ctx.session_id)
            _server_state.cleanup_session(new_ctx.session_id)

    assert _server_state.oxigraph_store_refcount(tmp_path) == 0


def test_connection_change_releases_only_that_sessions_reference(tmp_path):
    ctx_a, ctx_b = _ctx("shared-store-a"), _ctx("shared-store-b")
    with patch("src.server_state.get_oxigraph_store_dir", return_value=tmp_path):
        try:
            for ctx in (ctx_a, ctx_b):
                _server_state.get_session(ctx.session_id).connection_id = "conn-1"
            store = get_oxigraph_store(ctx_a)
            assert get_oxigraph_store(ctx_b) is store

            session_a = _server_state.get_session(ctx_a.session_id)
            _clear_session_state(session_a, reason="connection change")

            assert session_a.oxigraph_store is None
            assert _server_state.oxigraph_store_refcount(tmp_path) == 1
            assert store.query_sparql_ask(ASK_ANY) is False
        finally:
            _server_state.cleanup_session(ctx_a.session_id)
            _server_state.cleanup_session(ctx_b.session_id)


def test_discard_detaches_every_session(tmp_path):
    state = ServerState()
    session_a, session_b = state.get_session("a"), state.get_session("b")
    for session in (session_a, session_b):
        session.oxigraph_store = state.acquire_oxigraph_store(tmp_path)
        session.oxigraph_initialized = True

    state.discard_oxigraph_store(tmp_path)

    assert session_a.oxigraph_store is None
    assert session_b.oxigraph_store is None
    assert session_a.oxigraph_initialized is False
    assert state.oxigraph_store_refcount(tmp_path) == 0

    # Discarding an unknown path is a no-op, and the directory is free again.
    state.discard_oxigraph_store(tmp_path)
    fresh = state.acquire_oxigraph_store(tmp_path)
    state.release_oxigraph_store(fresh)


def test_release_of_an_unregistered_store_closes_it_directly():
    state = ServerState()
    store = Mock(name="oxigraph_store")

    state.release_oxigraph_store(store)

    store.close.assert_called_once_with()


async def test_cleanup_workspace_discards_the_shared_store(tmp_path, monkeypatch):
    """Deleting the store directory must first detach every session from it."""
    from unittest.mock import AsyncMock

    from src.handler_context import HandlerContext
    from src.handlers import workspace as workspace_handler
    from src.paths import get_oxigraph_store_dir

    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(workspace_handler, "OUTPUT_DIR", tmp_path)

    state = ServerState()
    cleaning, other = state.get_session("cleaning"), state.get_session("other")
    store_path = get_oxigraph_store_dir("conncleanup")
    for session in (cleaning, other):
        session.connection_id = "conncleanup"
        session.oxigraph_store = state.acquire_oxigraph_store(store_path)
    assert state.oxigraph_store_refcount(store_path) == 2

    ctx = Mock()
    ctx.info = AsyncMock()
    services = HandlerContext(
        get_session_data=lambda _ctx: cleaning, server_state=state
    )

    response = await workspace_handler.cleanup_workspace(ctx, services)

    assert "Oxigraph RDF store" in response
    assert not store_path.exists()
    assert cleaning.oxigraph_store is None
    assert other.oxigraph_store is None
    assert state.oxigraph_store_refcount(store_path) == 0


def test_close_releases_the_directory_lock(tmp_path):
    """pyoxigraph has no close(); the manager must drop the Store to free LOCK."""
    manager = OxigraphStoreManager(store_path=tmp_path)

    manager.close()

    assert manager.is_closed
    with pytest.raises(RuntimeError, match="closed"):
        _ = manager.store
    manager.close()  # idempotent
    reopened = OxigraphStoreManager(store_path=tmp_path)  # would raise if held
    reopened.close()
