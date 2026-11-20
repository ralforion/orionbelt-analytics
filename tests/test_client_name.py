"""Each driver names OrionBelt Analytics to its database, as OBSL does.

Where the database client library is installed, the driver runs through
SQLAlchemy and the real dialect, and the test reads what reaches the client
library's own ``connect`` (or, for DuckDB, the live setting).
"""

from typing import Any
from unittest.mock import patch

from clickhouse_driver.connection import Connection as ClickHouseNativeConnection

from src import __version__
from src.client_name import CLIENT_NAME
from src.drivers.clickhouse import ClickHouseDriver
from src.drivers.databricks import DatabricksDriver
from src.drivers.duckdb import DuckDBDriver
from src.drivers.mysql import MySQLDriver
from src.drivers.postgresql import PostgreSQLDriver
from src.drivers.snowflake import SnowflakeDriver


class Refused(Exception):
    """Raised by a patched client library in place of opening a connection."""


def _server(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "host": "db.example",
        "port": 5432,
        "database": "app",
        "username": "u",
        "password": "p",
    }
    params.update(overrides)
    return params


def test_the_name_carries_the_version() -> None:
    assert f"orionbelt-analytics/{__version__}" == CLIENT_NAME


def test_postgresql_sends_it_as_application_name() -> None:
    with patch("psycopg2.connect", side_effect=Refused) as connect:
        assert not PostgreSQLDriver().connect(**_server())

    assert connect.call_args.kwargs["application_name"] == CLIENT_NAME


def test_mysql_sends_it_as_the_program_name_attribute() -> None:
    with patch("pymysql.connect", side_effect=Refused) as connect:
        assert not MySQLDriver().connect(**_server(port=3306))

    assert connect.call_args.kwargs["program_name"] == CLIENT_NAME


def test_clickhouse_over_http_sends_it_as_the_user_agent() -> None:
    with patch("requests.Session.post", side_effect=Refused) as post:
        assert not ClickHouseDriver().connect(**_server(port=8123))

    assert post.call_args.kwargs["headers"]["User-Agent"] == CLIENT_NAME


def test_clickhouse_native_sends_it_as_the_client_name() -> None:
    with patch.object(
        ClickHouseNativeConnection, "connect", autospec=True, side_effect=Refused
    ) as connect:
        assert not ClickHouseDriver().connect(**_server(port=9000, protocol="native"))

    # clickhouse-driver puts "ClickHouse " in front of every client name.
    assert connect.call_args.args[0].client_name == f"ClickHouse {CLIENT_NAME}"


def test_snowflake_sends_no_name_of_its_own() -> None:
    """Snowflake's ``application`` is for its registered partners."""
    from snowflake.sqlalchemy.util import APPLICATION_NAME

    with patch("snowflake.connector.connect", side_effect=Refused) as connect:
        assert not SnowflakeDriver().connect(
            account="acct",
            username="u",
            password="p",
            warehouse="wh",
            database="db",
        )

    # The dialect's own default, as without OrionBelt Analytics.
    assert connect.call_args.kwargs["application"] == APPLICATION_NAME


def test_duckdb_sends_it_as_its_user_agent() -> None:
    driver = DuckDBDriver()
    assert driver.connect(database_path=":memory:")
    try:
        assert driver.engine is not None
        with driver.engine.connect() as conn:
            agent = conn.exec_driver_sql(
                "SELECT current_setting('custom_user_agent')"
            ).scalar_one()
    finally:
        driver.disconnect()

    # duckdb_engine puts its own name first.
    assert agent.startswith("duckdb_engine/")
    assert agent.endswith(f" {CLIENT_NAME}")


def test_databricks_adds_it_to_the_user_agent() -> None:
    """The Databricks dialect is not installed, so only its arguments are read."""
    with patch(
        "src.drivers.databricks.create_engine", side_effect=Refused
    ) as create_engine:
        assert not DatabricksDriver().connect(
            server_hostname="h", http_path="/sql/1.0/warehouses/w", access_token="t"
        )

    connect_args = create_engine.call_args.kwargs["connect_args"]
    assert connect_args["user_agent_entry"] == CLIENT_NAME
