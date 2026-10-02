"""Databases are configured by name, so a person can say which one they mean.

A server held one connection per database type, and nothing told the model
what that connection was. "Analyse my finance-2025 database" could not be
mapped to anything. Named connections are declared in the environment,
listed by list_databases with what they hold, and connected to by name.
"""

import re
from typing import Any

import pytest
from fastmcp import Client

import src.main as main_module
import src.server_state as state_module
from src.database_registry import (
    DatabaseConfigError,
    configured_databases,
    env_key,
    find_database,
)
from src.handlers import connection as connection_handler
from src.main import mcp
from src.server_state import ServerState

FINANCE = {
    "OBA_DATABASES": "finance-2025, sales",
    "DB_FINANCE_2025_TYPE": "databricks",
    "DB_FINANCE_2025_DESCRIPTION": "Finance actuals 2025",
    "DB_FINANCE_2025_DATABRICKS_CATALOG": "finance",
    "DB_FINANCE_2025_DATABRICKS_SCHEMA": "gold",
    "DB_SALES_TYPE": "databricks",
    "DB_SALES_DATABRICKS_SCHEMA": "sales_gold",
    # Shared by both: the workspace and its token.
    "DATABRICKS_SERVER_HOSTNAME": "adb-1.azuredatabricks.net",
    "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc",
    "DATABRICKS_ACCESS_TOKEN": "dapi-secret-token",
    "DATABRICKS_CATALOG": "main",
}


class TestTheRegistry:
    def test_named_connections_come_in_the_order_listed(self):
        names = [e.name for e in configured_databases(FINANCE)]

        assert names == ["finance-2025", "sales"]

    def test_a_setting_falls_back_to_the_unprefixed_variable(self):
        finance, sales = configured_databases(FINANCE)

        assert finance.getenv("DATABRICKS_CATALOG") == "finance"
        assert sales.getenv("DATABRICKS_CATALOG") == "main"
        assert sales.getenv("DATABRICKS_SCHEMA") == "sales_gold"
        assert sales.getenv("DATABRICKS_ACCESS_TOKEN") == "dapi-secret-token"
        assert sales.getenv("DATABRICKS_NOPE", "fallback") == "fallback"

    def test_a_description_never_shows_credentials(self):
        described = [e.describe() for e in configured_databases(FINANCE)]

        assert described[0] == {
            "name": "finance-2025",
            "type": "databricks",
            "description": "Finance actuals 2025",
            "catalog": "finance",
            "schema": "gold",
        }
        assert "dapi-secret-token" not in str(described)
        assert "adb-1" not in str(described)

    def test_a_type_configured_without_a_name_is_still_listed(self):
        env = {"POSTGRES_HOST": "db", "POSTGRES_DATABASE": "shop"}

        (entry,) = configured_databases(env)

        assert (entry.name, entry.db_type, entry.named) == (
            "postgresql",
            "postgresql",
            False,
        )
        assert entry.describe()["database"] == "shop"

    def test_names_match_the_way_people_write_them(self):
        entries = configured_databases(FINANCE)

        for spoken in ("finance-2025", "Finance 2025", "FINANCE_2025"):
            found = find_database(spoken, entries)
            assert found is not None and found.name == "finance-2025"
        assert find_database("hr", entries) is None

    def test_the_variable_prefix_is_derived_from_the_name(self):
        assert env_key("finance-2025") == "FINANCE_2025"
        assert env_key("Sales EU / gold") == "SALES_EU_GOLD"

    @pytest.mark.parametrize(
        ("env", "message"),
        [
            ({"OBA_DATABASES": "hr"}, "has no type"),
            ({"OBA_DATABASES": "hr", "DB_HR_TYPE": "oracle"}, "unsupported type"),
            (
                {"OBA_DATABASES": "hr,HR", "DB_HR_TYPE": "duckdb"},
                "appears twice",
            ),
            (
                {"OBA_DATABASES": "sales.eu,sales/eu", "DB_SALES_EU_TYPE": "duckdb"},
                "would both read DB_SALES_EU_",
            ),
            ({"OBA_DATABASES": "--"}, "no letters or digits"),
        ],
    )
    def test_a_broken_declaration_is_reported(self, env, message):
        with pytest.raises(DatabaseConfigError, match=message):
            configured_databases(env)


@pytest.fixture
def two_duckdb_files(monkeypatch, tmp_path):
    """Two named DuckDB databases, with the workspace in a temp dir."""
    import duckdb

    for name in ("finance", "sales"):
        con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
        con.execute(f"CREATE TABLE {name}_facts (id INTEGER)")
        con.close()

    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    for variable in ("DUCKDB_DATABASE_PATH", "MOTHERDUCK_TOKEN", "POSTGRES_HOST"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OBA_DATABASES", "finance-2025,sales")
    monkeypatch.setenv("DB_FINANCE_2025_TYPE", "duckdb")
    monkeypatch.setenv("DB_FINANCE_2025_DESCRIPTION", "Finance actuals 2025")
    monkeypatch.setenv(
        "DB_FINANCE_2025_DUCKDB_DATABASE_PATH", str(tmp_path / "finance.duckdb")
    )
    monkeypatch.setenv("DB_SALES_TYPE", "duckdb")
    monkeypatch.setenv("DB_SALES_DUCKDB_DATABASE_PATH", str(tmp_path / "sales.duckdb"))
    yield fresh
    fresh.cleanup()


def _clear_other_types(monkeypatch) -> None:
    for variable in (
        "MYSQL_HOST",
        "SNOWFLAKE_ACCOUNT",
        "DREMIO_URI",
        "DREMIO_HOST",
        "CLICKHOUSE_HOST",
        "BIGQUERY_PROJECT_ID",
        "DATABRICKS_SERVER_HOSTNAME",
    ):
        monkeypatch.delenv(variable, raising=False)


class TestConnectingByName:
    async def test_the_model_can_see_what_is_there(self, two_duckdb_files, monkeypatch):
        _clear_other_types(monkeypatch)
        async with Client(mcp) as client:
            listed = (await client.call_tool("list_databases", {})).data

        assert [d["name"] for d in listed["databases"]] == ["finance-2025", "sales"]
        assert listed["databases"][0]["description"] == "Finance actuals 2025"

    async def test_each_name_reaches_its_own_database(self, two_duckdb_files):
        async with Client(mcp) as client:
            finance = await client.call_tool(
                "connect_database", {"database": "Finance 2025"}
            )
            assert "finance-2025 (duckdb)" in finance.data
            handle = re.search(r"(ob_[a-z0-9]{6})", finance.data).group(1)
            on = {"connection": handle, "schema_name": "main"}
            tables = await client.call_tool("discover_schema", on)
            assert "finance_facts" in str(tables.data)

            # The same session moves to the other database.
            await client.call_tool(
                "connect_database", {"database": "sales", "connection": handle}
            )
            tables = await client.call_tool("discover_schema", on)
            assert "sales_facts" in str(tables.data)
            assert "finance_facts" not in str(tables.data)

    async def test_an_unknown_name_lists_the_real_ones(self, two_duckdb_files):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "connect_database", {"database": "hr"}, raise_on_error=False
            )

        assert "finance-2025, sales" in str(result.data or result.content)

    async def test_with_several_configured_it_asks_which(self, two_duckdb_files):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "connect_database", {}, raise_on_error=False
            )

        assert "Say which database" in str(result.data or result.content)

    async def test_with_one_configured_no_argument_is_needed(
        self, two_duckdb_files, monkeypatch
    ):
        _clear_other_types(monkeypatch)
        monkeypatch.setenv("OBA_DATABASES", "sales")
        async with Client(mcp) as client:
            result = await client.call_tool("connect_database", {})

        assert "sales (duckdb)" in result.data


class TestCredentialsAreNotShared:
    """Two tokens for one target are two connections, not one."""

    def _connected(self, monkeypatch: Any, token: str, user: str | None) -> Any:
        from types import SimpleNamespace

        import src.drivers.databricks as databricks_driver
        from src.database_manager import DatabaseManager

        class FakeDriver:
            def __init__(self, **_kwargs: Any) -> None:
                self.engine = None

            def connect(self, **_kwargs: Any) -> bool:
                return True

            def disconnect(self) -> None:
                pass

        monkeypatch.setattr(databricks_driver, "DatabricksDriver", FakeDriver)
        manager = DatabaseManager()
        monkeypatch.setattr(
            manager, "_sync_engine_from_driver", lambda: None, raising=False
        )
        monkeypatch.setattr(
            manager, "_query_principal", lambda _sql: f"user:{user}" if user else None
        )
        assert manager.connect_databricks(
            server_hostname="adb-1.azuredatabricks.net",
            http_path="/sql/1.0/warehouses/abc",
            access_token=token,
            catalog="finance",
            schema="gold",
        )
        return SimpleNamespace(manager=manager)

    def test_two_users_on_one_target_get_two_fingerprints(self, monkeypatch):
        from src.server_state import _get_connection_fingerprint

        admin = self._connected(monkeypatch, "dapi-admin", "admin@corp").manager
        restricted = self._connected(monkeypatch, "dapi-ro", "analyst@corp").manager

        assert _get_connection_fingerprint(admin) != _get_connection_fingerprint(
            restricted
        )

    def test_a_rotated_token_for_the_same_user_keeps_its_workspace(self, monkeypatch):
        from src.server_state import _get_connection_fingerprint

        before = self._connected(monkeypatch, "dapi-old", "analyst@corp").manager
        after = self._connected(monkeypatch, "dapi-new", "analyst@corp").manager

        assert _get_connection_fingerprint(before) == _get_connection_fingerprint(after)

    def test_without_a_principal_the_token_tells_them_apart(self, monkeypatch):
        from src.server_state import _get_connection_fingerprint

        one = self._connected(monkeypatch, "dapi-one", None).manager
        two = self._connected(monkeypatch, "dapi-two", None).manager

        assert _get_connection_fingerprint(one) != _get_connection_fingerprint(two)
        # Never the token itself.
        assert "dapi-one" not in str(one.connection_info)
        assert "dapi-one" not in (one.auth_identity or "")

    def test_the_fingerprint_never_contains_the_identity(self, monkeypatch):
        from src.server_state import _get_connection_fingerprint

        manager = self._connected(monkeypatch, "dapi-x", "analyst@corp").manager

        assert "analyst" not in _get_connection_fingerprint(manager)
        assert "analyst" not in str(manager.connection_info)

    def test_disconnecting_forgets_the_identity(self, monkeypatch):
        manager = self._connected(monkeypatch, "dapi-x", "analyst@corp").manager

        manager.disconnect()

        assert manager.auth_identity is None

    def test_a_service_account_is_identified_by_its_email(self, tmp_path):
        import json

        from src.database_manager import _bigquery_principal

        key = json.dumps({"client_email": "etl@proj.iam.gserviceaccount.com"})
        path = tmp_path / "key.json"
        path.write_text(key)

        assert _bigquery_principal(None, key) == (
            "user:etl@proj.iam.gserviceaccount.com"
        )
        assert _bigquery_principal(str(path), None) == _bigquery_principal(None, key)
        # Application default credentials: one identity per process.
        assert _bigquery_principal(None, None) is None

    def test_the_key_file_wins_over_an_inline_key_as_the_driver_does(self, tmp_path):
        import json

        from src.database_manager import _bigquery_principal

        admin = tmp_path / "admin.json"
        admin.write_text(json.dumps({"client_email": "admin@p.iam"}))
        reader = tmp_path / "reader.json"
        reader.write_text(json.dumps({"client_email": "reader@p.iam"}))
        shared_inline = json.dumps({"client_email": "shared@p.iam"})

        # An inline key shared by every profile must not mask each one's file.
        assert _bigquery_principal(str(admin), shared_inline) == "user:admin@p.iam"
        assert _bigquery_principal(str(reader), shared_inline) == "user:reader@p.iam"
        # Empty inline keys are unset, not an identity.
        assert _bigquery_principal(str(admin), "") == "user:admin@p.iam"
        assert _bigquery_principal("", "") is None
        assert _bigquery_principal(None, shared_inline) == "user:shared@p.iam"

    def test_empty_key_files_still_tell_profiles_apart(self, tmp_path):
        from src.database_manager import _bigquery_principal

        one, two = tmp_path / "one.json", tmp_path / "two.json"
        one.write_text("")
        two.write_text("")

        assert _bigquery_principal(str(one), None) != _bigquery_principal(
            str(two), None
        )

    def test_the_principal_is_asked_without_the_user_sql_validator(self, monkeypatch):
        from types import SimpleNamespace

        from sqlalchemy import create_engine

        from src.database_manager import DatabaseManager

        manager = DatabaseManager()
        manager._driver = SimpleNamespace(engine=create_engine("sqlite://"))

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the user-SQL path was used")

        monkeypatch.setattr(manager, "execute_sql_query", refuse)
        monkeypatch.setattr(manager, "validate_sql_syntax", refuse)

        principal = manager._query_principal("SELECT 'analyst@corp'")

        assert principal == "user:analyst@corp"

    def test_databricks_identity_is_the_user_when_the_warehouse_says(self, monkeypatch):
        from sqlalchemy import create_engine

        import src.drivers.databricks as databricks_driver
        from src.database_manager import DatabaseManager

        class SqliteDriver:
            """Answers current_user() the way a warehouse would."""

            def __init__(self, **_kwargs: Any) -> None:
                self.engine = create_engine("sqlite://")

            def connect(self, **_kwargs: Any) -> bool:
                return True

            def disconnect(self) -> None:
                pass

        monkeypatch.setattr(databricks_driver, "DatabricksDriver", SqliteDriver)
        identities = []
        for token in ("dapi-old", "dapi-new"):
            manager = DatabaseManager()
            monkeypatch.setattr(
                manager,
                "_query_principal",
                lambda sql, m=manager: DatabaseManager._query_principal(
                    m, sql.replace("current_user()", "'analyst@corp'")
                ),
            )
            assert manager.connect_databricks("h", "/p", token, "c", "s")
            identities.append(manager.auth_identity)

        assert identities == ["user:analyst@corp", "user:analyst@corp"]
