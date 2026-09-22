"""A connection id must identify the database, because sessions share by it.

The workspace on disk and, since sessions share a `ConnectionRuntime`, the
open database manager are both keyed by this fingerprint. Two different
databases that hash alike therefore share one manager: the second connection's
queries are answered by the first database.

What identifies a database differs per driver -- a file path for DuckDB, a
project and dataset for BigQuery, a URI for Dremio with a token -- so every
non-secret field the driver reports goes into the hash.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.database_manager import DatabaseManager
from src.lifecycle.metadata import VersionMetadataManager
from src.paths import adopt_legacy_connection_dirs, connection_dirs
from src.server_state import (
    ServerState,
    _get_connection_fingerprint,
    _legacy_connection_fingerprint,
    adopt_legacy_workspace,
)


def _manager(**connection_info) -> Mock:
    manager = Mock()
    manager.connection_info = connection_info
    manager.is_connected.return_value = True
    return manager


# --- what the fingerprint tells apart ---


@pytest.mark.parametrize(
    ("kind", "first", "second"),
    [
        (
            "DuckDB files",
            {"type": "duckdb", "database_path": "/data/sales.duckdb"},
            {"type": "duckdb", "database_path": "/data/hr.duckdb"},
        ),
        (
            "BigQuery datasets",
            {"type": "bigquery", "project_id": "acme", "dataset": "sales"},
            {"type": "bigquery", "project_id": "acme", "dataset": "hr"},
        ),
        (
            "Dremio endpoints reached with a token",
            {"type": "dremio", "uri": "https://sales.dremio", "auth_method": "PAT"},
            {"type": "dremio", "uri": "https://hr.dremio", "auth_method": "PAT"},
        ),
        (
            "database engines on one host and port",
            {"type": "postgresql", "host": "db", "port": 5432, "database": "app"},
            {"type": "mysql", "host": "db", "port": 5432, "database": "app"},
        ),
        (
            "Databricks catalogs",
            {"type": "databricks", "server_hostname": "x", "catalog": "sales"},
            {"type": "databricks", "server_hostname": "x", "catalog": "hr"},
        ),
    ],
)
def test_different_databases_get_different_ids(kind, first, second):
    assert _get_connection_fingerprint(_manager(**first)) != (
        _get_connection_fingerprint(_manager(**second))
    ), kind


def test_the_same_database_keeps_its_id_when_a_password_is_rotated():
    """Otherwise a rotated credential would orphan the workspace."""
    before = _manager(
        type="postgresql", host="db", port=5432, database="app", password="old"
    )
    after = _manager(
        type="postgresql", host="db", port=5432, database="app", password="new"
    )

    assert _get_connection_fingerprint(before) == _get_connection_fingerprint(after)


@pytest.mark.parametrize(
    "secret", ["password", "pat", "token", "motherduck_token", "client_secret"]
)
def test_no_secret_is_hashed_into_a_directory_name(secret):
    plain = _manager(type="duckdb", database_path="/data/sales.duckdb")
    with_secret = _manager(
        type="duckdb", database_path="/data/sales.duckdb", **{secret: "s3cret"}
    )

    assert _get_connection_fingerprint(plain) == _get_connection_fingerprint(
        with_secret
    )


def test_a_duckdb_path_is_not_mistaken_for_a_credential():
    """`database_path` contains "pat", which a substring match would drop --
    and it is the whole of DuckDB's identity."""
    assert _get_connection_fingerprint(
        _manager(type="duckdb", database_path="/data/sales.duckdb")
    ) != _get_connection_fingerprint(_manager(type="duckdb"))


def test_field_order_does_not_change_the_id():
    assert _get_connection_fingerprint(
        _manager(type="duckdb", database_path="/a.duckdb", read_only=False)
    ) == _get_connection_fingerprint(
        _manager(read_only=False, database_path="/a.duckdb", type="duckdb")
    )


def test_a_manager_that_never_connected_has_no_id():
    manager = Mock()
    manager.connection_info = {}

    assert _get_connection_fingerprint(manager) == "no_connection"


def test_two_real_duckdb_files_do_not_share_a_manager(tmp_path):
    """The reviewer's reproduction, with real drivers: before the fix both
    files hashed alike, so the second session was handed the first file's
    manager and every query went to the wrong database."""
    state = ServerState()
    sales, hr = DatabaseManager(), DatabaseManager()
    try:
        assert sales.connect_duckdb(str(tmp_path / "sales.duckdb"))
        assert hr.connect_duckdb(str(tmp_path / "hr.duckdb"))
        sales_id = _get_connection_fingerprint(sales)
        hr_id = _get_connection_fingerprint(hr)
        assert sales_id != hr_id

        first, second = state.get_session("a"), state.get_session("b")
        state.bind_session(first, sales_id, sales)
        state.bind_session(second, hr_id, hr)

        assert first.db_manager is sales
        assert second.db_manager is hr
        assert second.runtime is not first.runtime
    finally:
        state.cleanup()
        sales.disconnect()
        hr.disconnect()


# --- taking over a workspace named by the previous fingerprint ---


def _populate(root: Path, connection_id: str, db_type: str, db_name: str) -> None:
    """A workspace as a previous release left it: directories and metadata."""
    for directory in connection_dirs(connection_id):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "marker.txt").write_text(connection_id, encoding="utf-8")
    VersionMetadataManager(connection_id, root).update_workspace_connection(
        db_type=db_type, db_name=db_name
    )


@pytest.fixture
def output_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("src.workspace.OUTPUT_DIR", tmp_path)
    return tmp_path


def test_a_workspace_from_a_previous_release_is_adopted(output_dir):
    manager = _manager(type="postgresql", host="db", port=5432, database="app")
    legacy_id = _legacy_connection_fingerprint(manager)
    current_id = _get_connection_fingerprint(manager)
    _populate(output_dir, legacy_id, "postgresql", "app")

    adopted = adopt_legacy_workspace(manager, current_id, "postgresql", "app")

    assert len(adopted) == 3
    for directory in connection_dirs(current_id):
        assert (directory / "marker.txt").read_text(encoding="utf-8") == legacy_id
    assert not (output_dir / legacy_id).exists()


def test_another_databases_workspace_is_left_alone(output_dir):
    """The reviewer's reproduction. The old fingerprint could not tell a
    DuckDB file from a BigQuery dataset, so following it blindly would hand
    one database's ontologies to another and lose them for their owner."""
    duckdb = _manager(type="duckdb", database_path="/data/sales.duckdb")
    bigquery = _manager(type="bigquery", project_id="cloud", dataset="warehouse")
    legacy_id = _legacy_connection_fingerprint(duckdb)
    assert legacy_id == _legacy_connection_fingerprint(bigquery)  # the collision
    _populate(output_dir, legacy_id, "duckdb", "/data/sales.duckdb")
    bigquery_id = _get_connection_fingerprint(bigquery)

    adopted = adopt_legacy_workspace(bigquery, bigquery_id, "bigquery", "warehouse")

    assert adopted == []
    assert (output_dir / legacy_id / "marker.txt").exists()
    assert not (output_dir / bigquery_id).exists()

    # Nor does its real owner take it: the old id was the same for every
    # DuckDB file, so nothing on disk says which one this workspace was for.
    # Left where it is, for the operator to move if they know.
    assert (
        adopt_legacy_workspace(
            duckdb, _get_connection_fingerprint(duckdb), "duckdb", "/data/sales.duckdb"
        )
        == []
    )


def test_two_dremio_endpoints_do_not_take_each_others_workspace(output_dir):
    """The reviewer's reproduction. Every Dremio connection records the name
    "DREMIO", and the old id was the same for all of them, so matching what the
    workspace records is not enough to show whose it is."""
    sales = _manager(type="dremio", uri="https://sales.dremio", auth_method="PAT")
    hr = _manager(type="dremio", uri="https://hr.dremio", auth_method="PAT")
    legacy_id = _legacy_connection_fingerprint(sales)
    assert legacy_id == _legacy_connection_fingerprint(hr)  # the collision
    _populate(output_dir, legacy_id, "dremio", "DREMIO")

    hr_id = _get_connection_fingerprint(hr)
    assert adopt_legacy_workspace(hr, hr_id, "dremio", "DREMIO") == []
    assert (output_dir / legacy_id / "marker.txt").exists()
    assert not (output_dir / hr_id).exists()

    # Not even its real owner: nothing on disk says which endpoint it was.
    sales_id = _get_connection_fingerprint(sales)
    assert adopt_legacy_workspace(sales, sales_id, "dremio", "DREMIO") == []


@pytest.mark.parametrize(
    ("kind", "connection_info"),
    [
        ("DuckDB", {"type": "duckdb", "database_path": "/data/sales.duckdb"}),
        ("BigQuery", {"type": "bigquery", "project_id": "acme", "dataset": "sales"}),
        ("Snowflake", {"type": "snowflake", "account": "a", "database": "sales"}),
        ("Databricks", {"type": "databricks", "server_hostname": "x", "schema": "s"}),
    ],
)
def test_drivers_the_old_id_could_not_tell_apart_are_left_alone(
    output_dir, kind, connection_info
):
    """None of these put host, port and database in the old id, so every
    database of that kind got the same one."""
    manager = _manager(**connection_info)
    _populate(output_dir, _legacy_connection_fingerprint(manager), kind.lower(), "db")

    assert (
        adopt_legacy_workspace(
            manager, _get_connection_fingerprint(manager), kind.lower(), "db"
        )
        == []
    )


@pytest.mark.parametrize("kind", ["postgresql", "mysql", "clickhouse"])
def test_drivers_the_old_id_did_identify_are_adopted(output_dir, kind):
    """Host, port and database were all in the old id, and the workspace
    records the type, so ownership can be shown."""
    manager = _manager(type=kind, host="db.internal", port=5432, database="app")
    legacy_id = _legacy_connection_fingerprint(manager)
    _populate(output_dir, legacy_id, kind, "app")

    adopted = adopt_legacy_workspace(
        manager, _get_connection_fingerprint(manager), kind, "app"
    )

    assert len(adopted) == 3


def test_a_workspace_that_records_no_connection_is_left_alone(output_dir):
    """Ownership cannot be shown, and the old id cannot be trusted to show it."""
    manager = _manager(type="postgresql", host="db", port=5432, database="app")
    legacy_id = _legacy_connection_fingerprint(manager)
    for directory in connection_dirs(legacy_id):
        directory.mkdir(parents=True, exist_ok=True)

    assert (
        adopt_legacy_workspace(
            manager, _get_connection_fingerprint(manager), "postgresql", "app"
        )
        == []
    )
    assert (output_dir / legacy_id).exists()


def test_an_existing_workspace_is_never_overwritten(output_dir):
    manager = _manager(type="postgresql", host="db", port=5432, database="app")
    legacy_id = _legacy_connection_fingerprint(manager)
    current_id = _get_connection_fingerprint(manager)
    _populate(output_dir, legacy_id, "postgresql", "app")
    _populate(output_dir, current_id, "postgresql", "app")

    assert adopt_legacy_workspace(manager, current_id, "postgresql", "app") == []
    assert (output_dir / current_id / "marker.txt").read_text(
        encoding="utf-8"
    ) == current_id
    assert (output_dir / legacy_id / "marker.txt").exists()  # nothing deleted


def test_nothing_to_adopt_is_not_an_error(output_dir):
    manager = _manager(type="duckdb", database_path="/data/sales.duckdb")

    assert (
        adopt_legacy_workspace(
            manager,
            _get_connection_fingerprint(manager),
            "duckdb",
            "/data/sales.duckdb",
        )
        == []
    )


def test_an_unchanged_id_is_left_alone(output_dir):
    _populate(output_dir, "same-id", "postgresql", "app")

    assert adopt_legacy_connection_dirs("same-id", "same-id") == []
    assert (output_dir / "same-id" / "marker.txt").exists()
