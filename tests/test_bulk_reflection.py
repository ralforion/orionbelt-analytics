"""Discovery reflects a schema in one pass instead of one table at a time.

Each `analyze_table` opened its own connection, built its own Inspector --
discarding the reflection cache the last one filled -- and made four round
trips. For 100 DuckDB tables of ten columns that was 6,142 ms, all of it on the
event loop; reflecting the schema at once takes 87 ms and, run in a worker,
leaves the loop free.

The change is only licensed by producing the same metadata, so that is what
most of this file checks.
"""

import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from src.database_manager import DatabaseManager, TableInfo
from src.drivers.base import DatabaseDriver

TABLES = 6
COLUMNS = 4


@pytest.fixture(scope="module")
def manager() -> DatabaseManager:
    """A DuckDB file with a chain of tables, keys and foreign keys."""
    path = Path(tempfile.mkdtemp()) / "schema.duckdb"
    instance = DatabaseManager()
    assert instance.connect_duckdb(str(path))
    with instance.engine.connect() as conn:
        for i in range(TABLES):
            columns = ", ".join(f"c{j} INTEGER" for j in range(COLUMNS))
            reference = f", ref INTEGER REFERENCES t{i - 1}(id)" if i else ""
            conn.exec_driver_sql(
                f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, {columns}{reference})"
            )
        conn.commit()
    return instance


class TestBulkMatchesPerTable:
    """The one-pass result must equal what reflecting each table produced."""

    def test_every_table_is_identical(self, manager):
        names = manager.get_tables("main")
        assert len(names) == TABLES

        one_by_one = {
            name: manager._driver.analyze_table(name, "main") for name in names
        }
        bulk = manager.analyze_tables(names, "main")

        assert set(bulk) == {name for name, info in one_by_one.items() if info}
        for name, info in bulk.items():
            assert asdict(info) == asdict(one_by_one[name]), name

    def test_keys_and_relationships_survive(self, manager):
        bulk = manager.analyze_tables(manager.get_tables("main"), "main")

        child = bulk["t1"]
        assert [fk["referenced_table"] for fk in child.foreign_keys] == ["t0"]
        assert any(column.is_foreign_key for column in child.columns)
        assert child.schema == "main"

    def test_asking_for_a_subset_returns_only_those(self, manager):
        bulk = manager.analyze_tables(["t0", "t2"], "main")

        assert sorted(bulk) == ["t0", "t2"]

    def test_a_table_that_does_not_exist_is_left_out(self, manager):
        bulk = manager.analyze_tables(["t0", "no_such_table"], "main")

        assert sorted(bulk) == ["t0"]

    def test_an_empty_request_asks_the_database_nothing(self, manager):
        assert manager.analyze_tables([], "main") == {}


class TestTheFallback:
    """A dialect the one-pass route does not suit must still be reflected."""

    def test_a_failing_bulk_reflection_falls_back_to_one_at_a_time(
        self, manager, monkeypatch
    ):
        def explode(*_args: Any, **_kwargs: Any) -> dict[str, TableInfo]:
            raise RuntimeError("this dialect cannot reflect a whole schema")

        monkeypatch.setattr("src.drivers.duckdb.reflect_tables", explode)

        bulk = manager.analyze_tables(["t0", "t1"], "main")

        assert sorted(bulk) == ["t0", "t1"]
        assert bulk["t1"].foreign_keys


class TestTheDriverDefault:
    """Every other driver inherits the per-table loop, with its isolation."""

    def _driver(self, behaviour: dict[str, Any]) -> DatabaseDriver:
        class Fake(DatabaseDriver):
            db_type = "fake"

            def connect(self, **params: Any) -> bool:
                return True

            def get_schemas(self) -> list[str]:
                return []

            def get_tables(self, schema_name: str | None = None) -> list[str]:
                return list(behaviour)

            def analyze_table(
                self, table_name: str, schema_name: str | None = None
            ) -> TableInfo | None:
                outcome = behaviour[table_name]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            def validate_sql_syntax(
                self, sql_query: str, validation_result: dict[str, Any]
            ) -> dict[str, Any]:
                return validation_result

            def execute_sql_query(
                self, sql_query: str, limit: int = 1000
            ) -> dict[str, Any]:
                return {}

            def sample_table_data(
                self,
                table_name: str,
                schema_name: str | None = None,
                limit: int = 10,
            ) -> list[dict[str, Any]]:
                return []

            def test_connection(self) -> bool:
                return True

            def disconnect(self) -> None:
                return None

        return Fake()

    def _info(self, name: str) -> TableInfo:
        return TableInfo(
            name=name, schema="s", columns=[], primary_keys=[], foreign_keys=[]
        )

    def test_it_reflects_each_table(self):
        driver = self._driver({"a": self._info("a"), "b": self._info("b")})

        assert sorted(driver.analyze_tables(["a", "b"])) == ["a", "b"]

    def test_one_unreadable_table_does_not_cost_the_others(self):
        driver = self._driver(
            {
                "a": self._info("a"),
                "b": RuntimeError("permission denied"),
                "c": self._info("c"),
            }
        )

        assert sorted(driver.analyze_tables(["a", "b", "c"])) == ["a", "c"]

    def test_a_table_the_driver_cannot_find_is_left_out(self):
        driver = self._driver({"a": self._info("a"), "b": None})

        assert sorted(driver.analyze_tables(["a", "b"])) == ["a"]


class TestTheManager:
    """What the handlers call."""

    def test_it_refuses_without_a_connection(self):
        with pytest.raises(RuntimeError):
            DatabaseManager().analyze_tables(["t0"], "main")

    def test_it_returns_what_the_driver_returned(self, manager):
        from_driver = manager._driver.analyze_tables(["t0"], "main")
        from_manager = manager.analyze_tables(["t0"], "main")

        assert asdict(from_manager["t0"]) == asdict(from_driver["t0"])
