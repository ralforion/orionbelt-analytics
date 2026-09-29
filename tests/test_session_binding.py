"""A session stays on one database for as long as one of its tools is running.

Two review rounds found tools publishing results into whichever database the
session had moved to while they awaited a worker; an audit then found seven
more. Guarding each publish point was losing, because the cause was one
thing: `connect_database` took no lock before rebinding a session, and each
tool's writer lock resolved to whichever runtime the session was on when the
tool acquired it.

The fix is structural, and these tests pin its parts:

* a per-session binding lock, held by `connect_database` and by every
  publishing tool, taken *before* the runtime's writer lock so the runtime
  that lock resolves to cannot change underneath the tool;
* background work pinned when it is *created*, while the creator still holds
  that lock -- a task's body only runs after the tool has returned;
* a bound on how many calls may wait for one connection;
* a database closed only once the worker inside it has left.
"""

import asyncio
import threading
from typing import Any
from unittest.mock import Mock

import pytest

import src.main as main_module
from src.async_utils import run_db
from src.database_manager import DatabaseManager
from src.exceptions import ConnectionBusyError
from src.handlers import graphrag as graphrag_handler
from src.handlers.graphrag import _Pinned
from src.handlers.ontology_generation import _views_for_ontology
from src.server_state import ServerState
from src.session import ConnectionRuntime, GraphRAGState, SessionData


def _bound(state: ServerState, name: str, connection_id: str) -> SessionData:
    session = state.get_session(name)
    manager = Mock()
    manager.is_connected.return_value = True
    state.bind_session(session, connection_id, manager)
    session.connection_id = connection_id
    return session


class TestTheBindingLock:
    """A rebind waits for the session's writers, and they wait for it."""

    async def test_a_rebind_waits_for_a_running_writer(self, monkeypatch):
        state = ServerState()
        session = _bound(state, "s", "database-a")
        monkeypatch.setattr(main_module, "_server_state", state)
        monkeypatch.setattr(main_module, "get_session_data", lambda _ctx: session)
        order: list[str] = []
        writing = asyncio.Event()
        finish = asyncio.Event()

        async def writer() -> None:
            async with main_module._writer_lock(Mock()):
                order.append("writer holds")
                writing.set()
                await finish.wait()
                order.append("writer done")

        async def rebind() -> None:
            async with state.binding_lock(session):
                order.append("rebind")

        running = asyncio.create_task(writer())
        await writing.wait()
        waiting = asyncio.create_task(rebind())
        await asyncio.sleep(0.02)
        assert order == ["writer holds"]  # the rebind is still waiting

        finish.set()
        await asyncio.gather(running, waiting)
        assert order == ["writer holds", "writer done", "rebind"]

    async def test_the_writer_lock_is_the_runtime_it_started_on(self, monkeypatch):
        """Resolved after the binding is fixed, never before."""
        state = ServerState()
        session = _bound(state, "s", "database-a")
        first_runtime = session.runtime
        monkeypatch.setattr(main_module, "_server_state", state)
        monkeypatch.setattr(main_module, "get_session_data", lambda _ctx: session)

        async with main_module._writer_lock(Mock()):
            assert first_runtime.lock.locked()
            assert session.binding_lock.locked()

        assert not first_runtime.lock.locked()
        assert not session.binding_lock.locked()

    async def test_other_sessions_are_not_held_up(self, monkeypatch):
        state = ServerState()
        mine = _bound(state, "mine", "database-a")
        theirs = _bound(state, "theirs", "database-b")

        async with state.binding_lock(mine):
            # Another session's rebind does not wait for this one.
            await asyncio.wait_for(
                _acquire_and_release(state.binding_lock(theirs)), timeout=1
            )

    def test_a_double_without_a_lock_gets_a_no_op(self):
        state = ServerState()

        context = state.binding_lock(Mock(spec=[]))

        assert not isinstance(context, asyncio.Lock)


async def _acquire_and_release(context: Any) -> None:
    async with context:
        pass


class TestBackgroundWorkIsPinnedAtCreation:
    """A task's body runs only after the tool that created it has returned."""

    async def test_indexing_lands_in_the_database_it_was_started_for(self, monkeypatch):
        state = ServerState()
        session = _bound(state, "s", "database-a")
        original: GraphRAGState = session.graphrag
        indexed_into: list[Any] = []

        class Recording:
            def __init__(self, connection_id=None, schema_name=None, **_kw):
                self.connection_id = connection_id
                self.graph_retriever = Mock()
                self.graph_retriever.graph.number_of_nodes.return_value = 1
                self.vector_store = Mock()
                self.vector_store.get_statistics.return_value = {"total_elements": 1}
                self._schema_names: list[str] = []

            async def aindex_schema(self, **_kw):
                indexed_into.append(self.connection_id)

        async def nothing(*_a, **_k):
            return None

        monkeypatch.setattr(graphrag_handler, "GraphRAGManager", Recording)
        monkeypatch.setattr(graphrag_handler, "_save_graphrag_state", nothing)
        monkeypatch.setattr(graphrag_handler, "update_workspace_section", nothing)
        monkeypatch.setenv("AUTO_ONTOLOGY", "false")

        # Created while the session is on A...
        task = graphrag_handler._auto_initialize_graphrag_background(
            "public", [], session, Mock(), pinned=_Pinned(session)
        )
        # ...and the session moves to B before the body ever runs.
        state.unbind_session(session)
        _bound_again = state.bind_session(session, "database-b", Mock())
        session.connection_id = "database-b"
        await task

        assert indexed_into == ["database-a"]
        assert original.graphrag_manager is not None
        assert session.graphrag.graphrag_manager is None  # B untouched
        assert _bound_again is session.runtime

    def test_the_pin_records_the_dialect_as_well(self):
        session = SessionData()
        session.connection_id = "database-a"
        session.db_manager = Mock(connection_info={"type": "snowflake"})

        pin = _Pinned(session)
        session.db_manager = Mock(connection_info={"type": "duckdb"})

        assert pin.db_type == "snowflake"


class TestTheOntologyChainUsesWhatItWasGiven:
    """Views and dialect from creation time, not from the session later."""

    def test_given_views_win_over_the_sessions_current_ones(self):
        session = SessionData()
        session.set_current_schema("public")
        theirs = Mock()
        theirs.name, theirs.definition = "v_other_database", "SELECT 1"
        session.cache_views("public", [theirs])
        mine = Mock()
        mine.name, mine.definition = "v_revenue", "SELECT amount FROM orders"

        views = _views_for_ontology(session, "public", views=[mine], db_type="duckdb")

        assert [view.name for view in views] == ["v_revenue"]
        assert views[0].source_tables == ["orders"]


class TestTheQueueIsBounded:
    """A burst of calls behind a slow query is refused, not stacked forever."""

    class _Manager(DatabaseManager):
        def __init__(self) -> None:
            super().__init__()
            self.inside = threading.Event()
            self.release = threading.Event()

        def slow(self) -> str:
            self.inside.set()
            assert self.release.wait(timeout=10)
            return "slow"

        def quick(self) -> str:
            return "quick"

    async def test_calls_past_the_bound_are_refused(self):
        manager = self._Manager()
        manager.max_queued_calls = 2

        running = asyncio.create_task(run_db(manager.slow))
        assert await asyncio.to_thread(manager.inside.wait, 5)
        queued = [asyncio.create_task(run_db(manager.quick)) for _ in range(2)]
        await asyncio.sleep(0.02)
        assert manager.query_waiters == 2

        with pytest.raises(ConnectionBusyError) as refused:
            await run_db(manager.quick)
        assert refused.value.error_type.value == "connection_busy"

        manager.release.set()
        assert await running == "slow"
        assert await asyncio.gather(*queued) == ["quick", "quick"]
        assert manager.query_waiters == 0

    async def test_an_idle_connection_admits_everything(self):
        """One at a time, nothing ever waits, so no bound is ever reached."""
        manager = self._Manager()
        manager.max_queued_calls = 1

        results = [await run_db(manager.quick) for _ in range(5)]

        assert results == ["quick"] * 5
        assert manager.query_waiters == 0

    async def test_zero_means_no_bound(self):
        manager = self._Manager()
        manager.max_queued_calls = 0

        running = asyncio.create_task(run_db(manager.slow))
        assert await asyncio.to_thread(manager.inside.wait, 5)
        queued = [asyncio.create_task(run_db(manager.quick)) for _ in range(10)]
        await asyncio.sleep(0.02)

        manager.release.set()
        await running
        assert await asyncio.gather(*queued) == ["quick"] * 10

    def test_the_bound_is_configurable(self, monkeypatch):
        monkeypatch.setenv("DB_MAX_QUEUED_CALLS", "7")

        assert DatabaseManager().max_queued_calls == 7


class TestADatabaseClosesAfterItsQuery:
    """Disposal waits for the worker inside the database."""

    async def test_a_busy_manager_closes_once_its_query_ends(self):
        manager = TestTheQueueIsBounded._Manager()
        closed = asyncio.Event()
        manager.disconnect = lambda: closed.set()  # type: ignore[method-assign]

        running = asyncio.create_task(run_db(manager.slow))
        assert await asyncio.to_thread(manager.inside.wait, 5)

        ServerState._disconnect_manager(manager, "test")
        await asyncio.sleep(0.02)
        assert not closed.is_set()  # still querying

        manager.release.set()
        await running
        await asyncio.wait_for(closed.wait(), timeout=2)

    def test_an_idle_manager_closes_at_once(self):
        manager = DatabaseManager()
        closed: list[bool] = []
        manager.disconnect = lambda: closed.append(True)  # type: ignore[method-assign]

        ServerState._disconnect_manager(manager, "test")

        assert closed == [True]

    async def test_the_last_holder_leaving_mid_query_does_not_break_it(self):
        state = ServerState()
        manager = TestTheQueueIsBounded._Manager()
        closed = asyncio.Event()
        manager.disconnect = lambda: closed.set()  # type: ignore[method-assign]
        manager.is_connected = lambda: True  # type: ignore[method-assign]
        session = state.get_session("s")
        runtime: ConnectionRuntime = state.bind_session(session, "database-a", manager)
        assert runtime.db_manager is manager

        running = asyncio.create_task(run_db(manager.slow))
        assert await asyncio.to_thread(manager.inside.wait, 5)
        state.unbind_session(session)  # last holder gone, query still running

        await asyncio.sleep(0.02)
        assert not closed.is_set()
        manager.release.set()
        assert await running == "slow"
        await asyncio.wait_for(closed.wait(), timeout=2)


class TestBusyIsReportedAsBusy:
    """A refused call says the connection is busy, not that the server broke."""

    async def test_execute_sql_query_reports_connection_busy(self, monkeypatch):
        from src.handler_context import HandlerContext
        from src.handlers import query as query_handler

        async def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionBusyError(
                "32 calls are already waiting on this database connection"
            )

        monkeypatch.setattr(query_handler, "run_db", refuse)
        session = SessionData()
        services = HandlerContext(
            get_session_data=lambda _ctx: session,
            get_session_db_manager=lambda _ctx: Mock(),
            get_session_obqc_validator=lambda _ctx: None,
            create_error_response=lambda *a, **k: {"error_type": "internal_error"},
        )

        result = await query_handler.execute_sql_query(
            Mock(),
            "SELECT 1 FROM t",
            10,
            True,  # checklist_completed
            None,  # query_intent
            services,
        )

        assert result["error_type"] == "connection_busy"
        assert "waiting" in result["error"]
        assert result["obqc_fan_trap"]["evaluated"] is False
