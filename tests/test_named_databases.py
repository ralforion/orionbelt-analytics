"""Databases are configured by name, so a person can say which one they mean.

A server held one connection per database type, and nothing told the model
what that connection was. "Analyse my finance-2025 database" could not be
mapped to anything. Named connections are declared in the environment,
listed by list_databases with what they hold, and connected to by name.
"""

import re

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
