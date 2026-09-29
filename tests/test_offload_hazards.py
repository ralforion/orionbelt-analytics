"""What moving work off the event loop made possible, and must not.

Three things became reachable once database work ran in a worker, all found in
review of the offloading commits:

* A `connect_database` between a discovery's reflection and its publication
  wrote one database's tables into the replacement runtime's shared cache --
  which every session on that database reads.
* Cancelling a call released the per-connection lock while the thread was
  still inside the database, letting the next call in beside it. That lock
  exists because an in-memory DuckDB engine shares one connection.
* Unrelated to threads but found alongside: the row cap replaced a limit it
  could not evaluate, so `LIMIT (SELECT 3)` and `FETCH FIRST 3 ROWS ONLY`
  became `LIMIT 10` -- a safety limit that made the result bigger.
"""

import asyncio
import threading
import time
from typing import Any
from unittest.mock import Mock

import pytest

from src.async_utils import run_db
from src.database_manager import ColumnInfo, DatabaseManager, TableInfo
from src.handler_context import HandlerContext
from src.handlers import schema as schema_handler
from src.result_limits import apply_row_limit
from src.session import ConnectionRuntime, SessionData


def _table(name: str) -> TableInfo:
    return TableInfo(
        name=name,
        schema="public",
        columns=[
            ColumnInfo(
                name="id",
                data_type="INTEGER",
                is_nullable=False,
                is_primary_key=True,
                is_foreign_key=False,
            )
        ],
        primary_keys=["id"],
        foreign_keys=[],
    )


class TestDiscoveryStaysWithItsDatabase:
    """A reconnect mid-discovery must not redirect the results."""

    def _services(self, session: SessionData, db: Mock) -> HandlerContext:
        return HandlerContext(
            get_session_data=lambda _ctx: session,
            get_session_db_manager=lambda _ctx: db,
            get_session_safe_filename=lambda _ctx, kind, schema: f"{kind}_{schema}",
            create_error_response=lambda message, code=None, *rest: {
                "success": False,
                "error": message,
                "error_type": code,
            },
            auto_initialize_graphrag_background=Mock(),
        )

    def _db(self, session: SessionData, on_analyze: Any = None) -> Mock:
        db = Mock()
        db.has_engine.return_value = True
        db.get_tables.return_value = ["old_only"]
        db.get_views.return_value = []
        db.prefetch_schema_constraints.return_value = None

        def analyze(names, schema=None):
            if on_analyze is not None:
                on_analyze()
            return {name: _table(name) for name in names}

        db.analyze_tables.side_effect = analyze
        return db

    async def test_a_reconnect_discards_the_results(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTO_GRAPHRAG", "false")
        monkeypatch.setattr(schema_handler, "ensure_output_dir", lambda: tmp_path)

        session = SessionData()
        old = ConnectionRuntime("old-database")
        session.bind_runtime(old)
        session.connection_id = "old-database"

        def reconnect() -> None:
            # What `connect_database` does: a different database, a different
            # runtime, while this discovery is in its worker.
            session.unbind_runtime()
            new = ConnectionRuntime("new-database")
            session.bind_runtime(new)
            session.connection_id = "new-database"

        db = self._db(session, on_analyze=reconnect)
        result = await schema_handler.discover_schema(
            Mock(), "public", True, self._services(session, db)
        )

        assert result["success"] is False
        assert result["error_type"] == "connection_changed"
        # The replacement database's cache was not written.
        assert session.get_cached_schema("public") is None

    async def test_an_unchanged_connection_publishes_as_before(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("AUTO_GRAPHRAG", "false")
        monkeypatch.setattr(schema_handler, "ensure_output_dir", lambda: tmp_path)

        session = SessionData()
        runtime = ConnectionRuntime("stable")
        session.bind_runtime(runtime)
        session.connection_id = "stable"

        db = self._db(session)
        result = await schema_handler.discover_schema(
            Mock(), "public", True, self._services(session, db)
        )

        assert result.get("success") is not False
        cached = session.get_cached_schema("public")
        assert [t.name for t in cached] == ["old_only"]


class TestCancellationKeepsTheLockHeld:
    """A cancelled await does not stop a thread already in the database."""

    class _Manager(DatabaseManager):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.overlapped = False
            self.inside = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def slow(self) -> None:
            self.active += 1
            self.overlapped = self.overlapped or self.active > 1
            self.inside.set()
            assert self.release.wait(timeout=10)
            self.active -= 1
            self.finished.set()

        def quick(self) -> str:
            self.active += 1
            self.overlapped = self.overlapped or self.active > 1
            time.sleep(0.02)
            self.active -= 1
            return "done"

    async def test_a_second_call_waits_for_the_cancelled_one(self):
        manager = self._Manager()

        first = asyncio.create_task(run_db(manager.slow))
        assert await asyncio.to_thread(manager.inside.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(run_db(manager.quick))
        # The worker is still in the database; the second call must not start.
        await asyncio.sleep(0.05)
        assert manager.active == 1

        manager.release.set()
        assert await second == "done"
        assert manager.overlapped is False

    async def test_the_lock_is_free_once_the_worker_ends(self):
        manager = self._Manager()

        first = asyncio.create_task(run_db(manager.slow))
        assert await asyncio.to_thread(manager.inside.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        manager.release.set()
        assert await asyncio.to_thread(manager.finished.wait, 5)
        await asyncio.sleep(0)

        assert await run_db(manager.quick) == "done"
        assert manager.query_lock.locked() is False


class TestTheCapNeverWidensAResult:
    """A limit the tool cannot evaluate must be left alone."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT i FROM t LIMIT (SELECT 3)",
            "SELECT i FROM t FETCH FIRST 3 ROWS ONLY",
        ],
    )
    def test_an_unevaluated_or_fetch_limit_is_preserved(self, sql):
        rewritten, applied = apply_row_limit(sql, 10, "postgresql")

        assert rewritten == sql
        assert applied is False

    def test_a_smaller_literal_limit_still_wins(self):
        sql = "SELECT i FROM t LIMIT 3"

        assert apply_row_limit(sql, 10, "postgresql") == (sql, False)

    def test_a_larger_fetch_is_narrowed(self):
        rewritten, applied = apply_row_limit(
            "SELECT i FROM t FETCH FIRST 50 ROWS ONLY", 10, "postgresql"
        )

        assert applied is True
        assert "10" in rewritten

    def test_a_statement_without_a_limit_gets_one(self):
        rewritten, applied = apply_row_limit("SELECT i FROM t", 10, "postgresql")

        assert applied is True
        assert rewritten.endswith("LIMIT 10")

    def test_duckdb_really_returns_the_smaller_count(self):
        manager = DatabaseManager()
        assert manager.connect_duckdb(":memory:")

        result = manager.execute_sql_query(
            "SELECT i FROM range(100) AS t(i) FETCH FIRST 3 ROWS ONLY", limit=10
        )

        assert result["success"] is True, result.get("error")
        assert result["row_count"] == 3
