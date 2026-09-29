"""Database calls run in a worker, one at a time per connection.

A connection handshake, metadata reflection and a query all ran on the event
loop, so one slow warehouse call froze every other session on the server. They
run in threads now, which is also what first allows two of them at once on one
connection: every driver takes a fresh pooled connection per call, but an
in-memory DuckDB engine shares a single connection through StaticPool. The
manager's lock keeps the ordering a blocked loop used to give for free.
"""

import asyncio
import threading
from typing import Any
from unittest.mock import Mock

import pytest

from src.async_utils import run_db
from src.database_manager import DatabaseManager


class _Manager(DatabaseManager):
    """A manager with instrumented calls. Methods must be on the class: a
    function stuck on the instance is not a bound method, and ``run_db`` finds
    the connection's lock through ``__self__``."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[int] = []
        self.order: list[Any] = []
        self.active = 0
        self.overlapped = False
        self.inside = threading.Event()
        self.release = threading.Event()

    def record(self) -> int:
        self.threads.append(threading.get_ident())
        return 42

    def slow(self) -> str:
        self.inside.set()
        assert self.release.wait(timeout=10)
        return "done"

    def work(self, tag: Any = None, *, b: int = 0) -> Any:
        self.active += 1
        self.overlapped = self.overlapped or self.active > 1
        self.order.append(tag)
        threading.Event().wait(0.01)
        self.active -= 1
        return tag if b == 0 else tag + b

    def blocking(self) -> None:
        self.inside.set()
        assert self.release.wait(timeout=10)

    def quick(self) -> str:
        return "not waiting for the other manager"

    def boom(self) -> None:
        raise RuntimeError("database said no")

    def fine(self) -> str:
        return "ok"


class TestTheCallLeavesTheLoop:
    """The loop must keep serving while the database is busy."""

    async def test_the_call_runs_in_another_thread(self):
        manager = _Manager()

        assert await run_db(manager.record) == 42

        assert manager.threads[0] != threading.get_ident()

    async def test_the_loop_runs_while_the_call_is_in_flight(self):
        manager = _Manager()
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while not manager.release.is_set():
                ticks += 1
                await asyncio.sleep(0)

        call = asyncio.create_task(run_db(manager.slow))
        ticking = asyncio.create_task(tick())
        assert await asyncio.to_thread(
            manager.inside.wait, 10
        ), "the call never started"
        manager.release.set()

        assert await call == "done"
        await ticking
        assert ticks > 0


class TestOneCallAtATimePerConnection:
    """Two calls on one manager must not be inside the database together."""

    async def test_calls_on_one_manager_do_not_overlap(self):
        manager = _Manager()

        await asyncio.gather(*(run_db(manager.work, f"c{i}") for i in range(5)))

        assert manager.overlapped is False

    async def test_calls_keep_their_order(self):
        manager = _Manager()

        await asyncio.gather(*(run_db(manager.work, i) for i in range(5)))

        assert manager.order == [0, 1, 2, 3, 4]

    async def test_two_managers_are_independent(self):
        first, second = _Manager(), _Manager()

        held = asyncio.create_task(run_db(first.blocking))
        assert await asyncio.to_thread(first.inside.wait, 10)

        assert await run_db(second.quick) == "not waiting for the other manager"

        first.release.set()
        await held


class TestEdges:
    """Doubles, keyword arguments and failures."""

    async def test_a_double_without_a_lock_still_works(self):
        double = Mock()
        double.get_tables.return_value = ["orders"]

        assert await run_db(double.get_tables, "public") == ["orders"]
        double.get_tables.assert_called_once_with("public")

    async def test_keyword_arguments_are_passed_through(self):
        manager = _Manager()

        assert await run_db(manager.work, 1, b=2) == 3

    async def test_an_error_propagates_and_frees_the_lock(self):
        manager = _Manager()

        with pytest.raises(RuntimeError, match="database said no"):
            await run_db(manager.boom)

        assert await run_db(manager.fine) == "ok"
        assert not manager.query_lock.locked()


class TestAgainstARealDatabase:
    """In-memory DuckDB is the engine that shares one connection."""

    async def test_a_query_returns_the_same_as_calling_it_directly(self):
        manager = DatabaseManager()
        assert manager.connect_duckdb(":memory:")

        sql = "SELECT i FROM range(3) AS t(i)"
        direct = manager.execute_sql_query(sql, limit=10)
        offloaded = await run_db(manager.execute_sql_query, sql, 10)

        assert offloaded["success"] is True, offloaded["error"]
        assert offloaded["data"] == direct["data"] == [{"i": 0}, {"i": 1}, {"i": 2}]

    async def test_concurrent_queries_on_one_memory_database_all_succeed(self):
        manager = DatabaseManager()
        assert manager.connect_duckdb(":memory:")

        results: list[Any] = await asyncio.gather(
            *(
                run_db(
                    manager.execute_sql_query,
                    f"SELECT i + {i} AS n FROM range(1) AS t(i)",
                    10,
                )
                for i in range(8)
            )
        )

        assert [r["data"][0]["n"] for r in results] == list(range(8))
        assert all(r["success"] for r in results)
