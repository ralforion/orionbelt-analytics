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


# ---------------------------------------------------------------------------
# The second review: the same hazard in the other tools that reflect or write.
# ---------------------------------------------------------------------------


def _bound_session(connection_id: str) -> SessionData:
    session = SessionData()
    session.bind_runtime(ConnectionRuntime(connection_id))
    session.connection_id = connection_id
    return session


def _reconnect(session: SessionData, connection_id: str = "new-database") -> None:
    """What connect_database does to a session, in the middle of a tool."""
    session.unbind_runtime()
    session.bind_runtime(ConnectionRuntime(connection_id))
    session.connection_id = connection_id


def _error_services(session: SessionData, db: Any, **extra: Any) -> HandlerContext:
    return HandlerContext(
        get_session_data=lambda _ctx: session,
        get_session_db_manager=lambda _ctx: db,
        get_session_safe_filename=lambda _ctx, kind, schema: f"{kind}_{schema}",
        create_error_response=lambda message, code=None, *rest: {
            "success": False,
            "error": message,
            "error_type": code,
        },
        **extra,
    )


def _reflecting_db(on_analyze: Any) -> Mock:
    db = Mock()
    db.has_engine.return_value = True
    db.get_tables.return_value = ["old_only"]
    db.get_views.return_value = []
    db.prefetch_schema_constraints.return_value = None

    def analyze(names, schema=None):
        on_analyze()
        return {name: _table(name) for name in names}

    db.analyze_tables.side_effect = analyze
    return db


class TestOntologyGenerationStaysWithItsDatabase:
    """generate_ontology reflects, then writes a file and the session."""

    async def test_a_reconnect_during_reflection_writes_nothing(
        self, monkeypatch, tmp_path
    ):
        from src.handlers import ontology_generation

        monkeypatch.setattr(ontology_generation, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(
            ontology_generation, "get_connection_dir", lambda cid: tmp_path / cid
        )
        monkeypatch.setenv("OBA_SHACL_VALIDATE", "false")
        session = _bound_session("old-database")
        db = _reflecting_db(lambda: _reconnect(session))
        server_state = Mock()
        server_state.get_ontology_generator.return_value.generate_from_schema.return_value = (
            "@prefix ex: <http://example.com/> .\n"
        )

        result = await ontology_generation.generate_ontology(
            Mock(),
            None,  # schema_info: reflect from the database
            "public",
            "http://example.com/ontology/",
            False,  # auto_persist
            None,  # graph_uri
            _error_services(session, db, server_state=server_state),
        )

        assert result["error_type"] == "connection_changed"
        assert session.get_cached_schema("public") is None
        assert session.ontology_file is None
        assert not (tmp_path / "new-database").exists()

    async def test_a_reconnect_during_generation_leaves_the_session_alone(
        self, monkeypatch, tmp_path
    ):
        from src.handlers import ontology_generation

        monkeypatch.setattr(ontology_generation, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(
            ontology_generation, "get_connection_dir", lambda cid: tmp_path / cid
        )
        monkeypatch.setenv("OBA_SHACL_VALIDATE", "false")
        session = _bound_session("old-database")
        session.cache_schema_analysis("public", [_table("orders")])

        def generate(*_args: Any, **_kwargs: Any) -> str:
            _reconnect(session)
            return "@prefix ex: <http://example.com/> .\n"

        server_state = Mock()
        server_state.get_ontology_generator.return_value.generate_from_schema.side_effect = (
            generate
        )

        result = await ontology_generation.generate_ontology(
            Mock(),
            None,  # schema_info: reflect from the database
            "public",
            "http://example.com/ontology/",
            False,  # auto_persist
            None,  # graph_uri
            _error_services(session, Mock(), server_state=server_state),
        )

        assert result["error_type"] == "connection_changed"
        assert session.ontology_file is None
        # Nothing landed in the workspace of the database now connected.
        assert not (tmp_path / "new-database").exists()


class TestGraphRAGInitializationStaysWithItsDatabase:
    """initialize_graphrag reflects, reads views, then indexes."""

    async def test_a_reconnect_during_reflection_writes_nothing(self, monkeypatch):
        from src.handlers import graphrag as graphrag_handler

        built: list[Any] = []
        monkeypatch.setattr(
            graphrag_handler, "GraphRAGManager", lambda **kw: built.append(kw)
        )
        session = _bound_session("old-database")
        db = _reflecting_db(lambda: _reconnect(session))

        result = await graphrag_handler.initialize_graphrag(
            Mock(), "public", "tfidf", _error_services(session, db)
        )

        assert result["error_type"] == "connection_changed"
        assert session.get_cached_schema("public") is None
        assert session.graphrag_manager is None
        assert built == []


class TestPercentagesAreNotRowCounts:
    """20 PERCENT is not 20 rows, and never 10 either."""

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT i FROM t LIMIT 20 PERCENT", "duckdb"),
            ("SELECT i FROM t FETCH FIRST 20 PERCENT ROWS ONLY", "postgresql"),
            ("SELECT TOP 20 PERCENT i FROM t", "snowflake"),
        ],
    )
    def test_a_percentage_is_left_alone(self, sql, dialect):
        rewritten, applied = apply_row_limit(sql, 10, dialect)

        assert applied is False
        assert rewritten == sql

    def test_duckdb_returns_the_percentage(self):
        manager = DatabaseManager()
        assert manager.connect_duckdb(":memory:")

        result = manager.execute_sql_query(
            "SELECT i FROM range(10) AS t(i) LIMIT 20 PERCENT", limit=10
        )

        assert result["success"] is True, result.get("error")
        assert result["row_count"] == 2


class TestLoadingAnOntologyStaysWithItsDatabase:
    """A custom ontology is a user's choice for the database they are on."""

    TTL = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix oba: <https://ralforion.com/ns/oba#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        '<http://x/Orders> a owl:Class ; oba:tableName "orders" .\n'
        '<http://x/orders_id> a owl:DatatypeProperty ; oba:columnName "id" ;\n'
        '    oba:tableName "orders" ; rdfs:domain <http://x/Orders> .\n'
    )

    async def test_a_reconnect_while_loading_adopts_nothing(
        self, monkeypatch, tmp_path
    ):
        from src.handlers import ontology_io

        session = _bound_session("old-database")
        # Without a current schema the getter reads nothing whatever was set,
        # and "adopted nothing" would pass for the wrong reason.
        session.set_current_schema("public")
        real_write = ontology_io.write_text_file

        async def write_then_reconnect(path: Any, content: str) -> None:
            await real_write(path, content)
            _reconnect(session)

        monkeypatch.setattr(ontology_io, "write_text_file", write_then_reconnect)

        result = await ontology_io.load_my_ontology(
            Mock(),
            str(tmp_path),
            False,  # auto_persist
            None,
            _error_services(session, Mock()),
            ontology_content=self.TTL,
            file_name="mine.ttl",
        )

        assert result["error_type"] == "connection_changed"
        assert session.loaded_ontology is None

    async def test_an_unchanged_connection_adopts_it(self, tmp_path):
        from src.handlers import ontology_io

        session = _bound_session("stable")
        session.set_current_schema("public")

        result = await ontology_io.load_my_ontology(
            Mock(),
            str(tmp_path),
            False,
            None,
            HandlerContext(get_session_data=lambda _ctx: session),
            ontology_content=self.TTL,
            file_name="mine.ttl",
        )

        assert result.get("success") is not False, result
        assert session.loaded_ontology == self.TTL


class TestOBQCAndTheDatabaseStayPaired:
    """The third review: a reconnect while the validator is built.

    `execute_sql_query` captured the database manager, then awaited the
    validator. A reconnect in between paired the old database with the new
    session's ontology -- or with none, which skips OBQC: a fan-trap query
    that should have been blocked ran, and returned inflated totals.
    """

    def _services(self, session: SessionData, db: Any, on_validator: Any) -> Any:
        async def validator(_ctx: Any) -> Any:
            return on_validator()

        return HandlerContext(
            get_session_data=lambda _ctx: session,
            get_session_db_manager=lambda _ctx: db,
            aget_session_obqc_validator=validator,
            get_session_obqc_validator=lambda _ctx: None,
            create_error_response=lambda message, code=None, *rest: {
                "success": False,
                "error": message,
                "error_type": code,
            },
        )

    def _db(self) -> Mock:
        db = Mock()
        db.has_engine.return_value = True
        db.connection_info = {"type": "duckdb"}
        return db

    async def test_a_reconnect_during_the_validator_build_refuses_the_query(
        self, monkeypatch
    ):
        from src.handlers import query as query_handler

        executed: list[str] = []

        async def run(call: Any, *args: Any, **kwargs: Any) -> Any:
            executed.append(getattr(call, "__name__", str(call)))
            return {"success": True, "data": [], "row_count": 0}

        monkeypatch.setattr(query_handler, "run_db", run)
        session = _bound_session("old-database")

        def reconnect_then_no_ontology() -> None:
            # The new session has no ontology yet: OBQC would be skipped.
            _reconnect(session)
            return None

        result = await query_handler.execute_sql_query(
            Mock(),
            "SELECT o.id, sum(i.amount) FROM orders o JOIN items i ON i.o = o.id "
            "GROUP BY o.id",
            10,
            True,
            None,
            self._services(session, self._db(), reconnect_then_no_ontology),
        )

        assert result["error_type"] == "connection_changed"
        assert executed == []  # nothing reached the database
        assert result["obqc_fan_trap"]["evaluated"] is False

    async def test_without_a_reconnect_the_query_still_runs(self, monkeypatch):
        from src.handlers import query as query_handler

        executed: list[str] = []

        async def run(call: Any, *args: Any, **kwargs: Any) -> Any:
            executed.append("ran")
            return {"success": True, "data": [], "row_count": 0}

        monkeypatch.setattr(query_handler, "run_db", run)
        session = _bound_session("stable")

        result = await query_handler.execute_sql_query(
            Mock(),
            "SELECT 1 FROM t",
            10,
            True,
            None,
            self._services(session, self._db(), lambda: None),
        )

        assert executed == ["ran"]
        assert result.get("error_type") != "connection_changed"

    async def test_validate_sql_syntax_refuses_a_mixed_answer(self, monkeypatch):
        from src.handlers import query as query_handler

        async def run(call: Any, *args: Any, **kwargs: Any) -> Any:
            return {"is_valid": True, "warnings": [], "suggestions": []}

        monkeypatch.setattr(query_handler, "run_db", run)
        session = _bound_session("old-database")

        def reconnect_then_no_ontology() -> None:
            _reconnect(session)
            return None

        result = await query_handler.validate_sql_syntax(
            Mock(),
            "SELECT 1 FROM t",
            self._services(session, self._db(), reconnect_then_no_ontology),
        )

        assert result["is_valid"] is False
        assert result["error_type"] == "connection_changed"
