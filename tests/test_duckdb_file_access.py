"""DuckDB connections read files only where the configuration allows.

These go to the driver directly, past the query checks in front of it, to
show that DuckDB itself refuses: whatever a query looks like, a local DuckDB
connection cannot read a file outside DUCKDB_ALLOWED_DIRECTORIES.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from src.database_manager import DatabaseManager
from src.drivers.duckdb import DuckDBDriver


@pytest.fixture
def files(tmp_path: Path) -> Path:
    """A database with a table and a view over an allowed CSV, plus files."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ok.csv").write_text("a\n1\n")
    (tmp_path / "database").mkdir()  # shares the "data" prefix
    (tmp_path / "database" / "near.csv").write_text("a\n2\n")
    (tmp_path / "secret.csv").write_text("code\nX7Q2\n")
    con = duckdb.connect(str(tmp_path / "shop.duckdb"))
    con.execute(
        "CREATE TABLE t AS SELECT 1 AS a; "
        f"CREATE VIEW v AS SELECT * FROM read_csv('{tmp_path / 'data' / 'ok.csv'}')"
    )
    con.close()
    return tmp_path


def _driver(path: str, allowed: list[str] | None = None) -> DuckDBDriver:
    driver = DuckDBDriver()
    assert driver.connect(database_path=path, allowed_directories=allowed or [])
    return driver


def _run(driver: DuckDBDriver, sql: str) -> dict[str, Any]:
    return driver.execute_sql_query(sql)


def test_files_are_refused_by_duckdb_itself(files: Path) -> None:
    driver = _driver(str(files / "shop.duckdb"))
    secret = files / "secret.csv"
    for sql in (
        f"SELECT * FROM read_csv('{secret}')",
        f"SELECT * FROM '{secret}'",
        f"SELECT * FROM read_text('{secret}')",
        f"SELECT * FROM glob('{files}/*')",
    ):
        result = _run(driver, sql)
        assert not result["success"], sql
        assert "Permission" in str(result["error"]), result
        assert "X7Q2" not in str(result)


def test_tables_still_work_on_every_new_connection(files: Path) -> None:
    driver = _driver(str(files / "shop.duckdb"))
    for _ in range(3):  # file databases open a fresh connection each time
        assert _run(driver, "SELECT a FROM t")["data"] == [{"a": 1}]
        assert not _run(driver, f"SELECT * FROM read_csv('{files / 'secret.csv'}')")[
            "success"
        ]


def test_a_view_over_an_allowed_directory_still_reads(files: Path) -> None:
    driver = _driver(str(files / "shop.duckdb"), [str(files / "data")])
    assert _run(driver, "SELECT a FROM v")["data"] == [{"a": 1}]
    # "data" allows data/, not database/ next to it.
    near = _run(driver, f"SELECT * FROM read_csv('{files / 'database' / 'near.csv'}')")
    assert not near["success"]
    assert not _run(driver, f"SELECT * FROM read_csv('{files / 'secret.csv'}')")[
        "success"
    ]


def test_a_view_outside_the_allowed_directories_fails(files: Path) -> None:
    driver = _driver(str(files / "shop.duckdb"))
    result = _run(driver, "SELECT a FROM v")
    assert not result["success"]
    assert "Permission" in str(result["error"])


def test_in_memory_database_is_restricted_too(files: Path) -> None:
    driver = _driver(":memory:")
    result = _run(driver, f"SELECT * FROM read_csv('{files / 'secret.csv'}')")
    assert not result["success"]
    assert "Permission" in str(result["error"])


def test_the_setting_cannot_be_switched_back_on(files: Path) -> None:
    driver = _driver(str(files / "shop.duckdb"))
    assert driver.engine is not None
    with (
        driver.engine.connect() as conn,
        pytest.raises(Exception, match="external access"),
    ):
        conn.exec_driver_sql("SET enable_external_access = true")


def test_motherduck_is_left_unrestricted() -> None:
    # MotherDuck needs the network; the restriction would cut it off.
    engine = MagicMock()
    with (
        patch("src.drivers.duckdb.create_engine", return_value=engine),
        patch("src.drivers.duckdb._restrict_file_access") as restrict,
    ):
        assert DuckDBDriver().connect(database_path="md:analytics")
    restrict.assert_not_called()


def test_database_manager_passes_the_allowed_directories(files: Path) -> None:
    db = DatabaseManager()
    assert db.connect_duckdb(
        str(files / "shop.duckdb"), allowed_directories=[str(files / "data")]
    )
    assert db.execute_sql_query("SELECT a FROM v")["data"] == [{"a": 1}]


# --- One database, one policy ------------------------------------------------


def test_different_policies_do_not_share_a_connection(files: Path) -> None:
    from src.server_state import _get_connection_fingerprint

    def manager(allowed: list[str]) -> DatabaseManager:
        db = DatabaseManager()
        assert db.connect_duckdb(
            str(files / "shop.duckdb"), allowed_directories=allowed
        )
        return db

    data = str(files / "data")
    plain, allowed = manager([]), manager([data])
    # Sessions bind to a manager by fingerprint: another policy, another one.
    assert _get_connection_fingerprint(plain) != _get_connection_fingerprint(allowed)
    # The same policy spelled differently is the same connection.
    other = manager([str(files / "database"), data + "/"])
    same = manager([data, str(files / "database")])
    assert _get_connection_fingerprint(other) == _get_connection_fingerprint(same)
    # No policy, no new field: a workspace keeps the identity it had.
    assert "allowed_directories" not in plain.connection_info


def test_an_open_database_does_not_lend_its_policy(files: Path) -> None:
    permissive = _driver(str(files / "shop.duckdb"), [str(files / "data")])
    assert permissive.engine is not None
    # While the permissive connection is open, the file's one database
    # instance carries its allowlist; a stricter driver must not inherit it.
    with permissive.engine.connect():
        strict = DuckDBDriver()
        assert not strict.connect(
            database_path=str(files / "shop.duckdb"), allowed_directories=[]
        )
        alike = _driver(str(files / "shop.duckdb"), [str(files / "data")])
        assert _run(alike, "SELECT a FROM v")["data"] == [{"a": 1}]
    # Once it is closed, the strict driver gets the policy it asked for.
    strict = _driver(str(files / "shop.duckdb"))
    assert not _run(strict, "SELECT a FROM v")["success"]


def test_reconnecting_keeps_the_allowed_directories(files: Path) -> None:
    db = DatabaseManager()
    assert db.connect_duckdb(
        str(files / "shop.duckdb"), allowed_directories=[str(files / "data")]
    )
    db._reconnect()
    assert db.execute_sql_query("SELECT a FROM v")["data"] == [{"a": 1}]
    assert db.connection_info["allowed_directories"] == [str(files / "data") + "/"]
