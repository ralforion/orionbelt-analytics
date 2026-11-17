"""The SQL drivers send on their own behalf is built, not pasted together.

Each driver's metadata and sample queries come from sqlglot trees (see
src/sql_build.py): names are identifiers and values are literals, written out
with the dialect's own quoting. These tests pin the SQL each driver sends,
and show that a name with a quote in it stays a name.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.drivers.bigquery import BigQueryDriver
from src.drivers.clickhouse import ClickHouseDriver
from src.drivers.databricks import DatabricksDriver
from src.drivers.dremio import DremioDriver
from src.drivers.duckdb import DuckDBDriver
from src.drivers.mysql import MySQLDriver
from src.drivers.postgresql import PostgreSQLDriver
from src.drivers.snowflake import SnowflakeDriver
from src.sql_build import (
    names_except,
    plain,
    qualified_name,
    quoted,
    sample_rows,
    select_where,
    table_path,
)

# --- The builders ------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("postgres", 'SELECT * FROM "a""b"."orders" LIMIT 5'),
        ("duckdb", 'SELECT * FROM "a""b"."orders" LIMIT 5'),
        ("snowflake", 'SELECT * FROM "a""b"."orders" LIMIT 5'),
        ("clickhouse", 'SELECT * FROM "a""b"."orders" LIMIT 5'),
        ("mysql", 'SELECT * FROM `a"b`.`orders` LIMIT 5'),
        ("databricks", 'SELECT * FROM `a"b`.`orders` LIMIT 5'),
    ],
)
def test_a_quote_in_a_name_stays_in_the_name(dialect: str, expected: str) -> None:
    assert sample_rows(dialect, 5, 'a"b', "orders") == expected


def test_backticks_are_doubled_inside_backtick_quotes() -> None:
    assert qualified_name("databricks", "main", "a`b") == "`main`.`a``b`"


def test_paths_deeper_than_three_parts() -> None:
    assert (
        sample_rows("trino", 3, "space", "folder", "sub", "t")
        == 'SELECT * FROM "space"."folder"."sub"."t" LIMIT 3'
    )


def test_empty_parts_are_left_out() -> None:
    assert sample_rows("postgres", 3, None, "t") == 'SELECT * FROM "t" LIMIT 3'
    assert qualified_name("snowflake", "", "S") == '"S"'


def test_values_become_escaped_literals() -> None:
    sql = select_where(
        "trino",
        ["TABLE_NAME"],
        table_path(plain("INFORMATION_SCHEMA"), quoted("TABLES")),
        {"TABLE_SCHEMA": "it's'; DROP TABLE x; --"},
    )
    assert sql == (
        'SELECT TABLE_NAME FROM INFORMATION_SCHEMA."TABLES" '
        "WHERE TABLE_SCHEMA = 'it''s''; DROP TABLE x; --'"
    )


def test_names_except_keeps_fixed_names_unquoted() -> None:
    sql = names_except(
        "postgres",
        "schema_name",
        table_path(plain("information_schema"), plain("schemata")),
        ["information_schema", "x'y"],
        not_like=["pg_temp_%"],
    )
    assert sql == (
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE NOT schema_name IN ('information_schema', 'x''y') "
        "AND NOT schema_name LIKE 'pg_temp_%' ORDER BY schema_name"
    )


# --- What each driver sends --------------------------------------------------


def _capture(driver: Any) -> MagicMock:
    """Give *driver* an engine whose connection records what it executes."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.keys.return_value = []
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    driver.engine = engine
    return conn


def _sent(conn: MagicMock) -> list[str]:
    return [str(call.args[0]) for call in conn.execute.call_args_list]


@pytest.mark.parametrize(
    ("driver_class", "schemas_sql", "sample_sql"),
    [
        (
            PostgreSQLDriver,
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE NOT schema_name IN ('information_schema', 'pg_catalog', "
            "'pg_toast') AND NOT schema_name LIKE 'pg_temp_%' "
            "AND NOT schema_name LIKE 'pg_toast_temp_%' ORDER BY schema_name",
            'SELECT * FROM "sales"."orders" LIMIT 5',
        ),
        (
            MySQLDriver,
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            "WHERE NOT SCHEMA_NAME IN ('information_schema', 'mysql', "
            "'performance_schema', 'sys') ORDER BY SCHEMA_NAME",
            "SELECT * FROM `sales`.`orders` LIMIT 5",
        ),
        (
            ClickHouseDriver,
            "SELECT name FROM system.databases WHERE NOT (name IN "
            "('system', 'INFORMATION_SCHEMA', 'information_schema')) ORDER BY name",
            'SELECT * FROM "sales"."orders" LIMIT 5',
        ),
        (
            SnowflakeDriver,
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE NOT schema_name IN ('INFORMATION_SCHEMA', 'SNOWFLAKE', "
            "'SNOWFLAKE_SAMPLE_DATA') ORDER BY schema_name",
            'SELECT * FROM "sales"."orders" LIMIT 5',
        ),
        (
            DuckDBDriver,
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE NOT schema_name IN ('information_schema', 'pg_catalog') "
            "ORDER BY schema_name",
            'SELECT * FROM "sales"."orders" LIMIT 5',
        ),
    ],
)
def test_sqlalchemy_drivers_send_built_sql(
    driver_class: Any, schemas_sql: str, sample_sql: str
) -> None:
    driver = driver_class()
    conn = _capture(driver)
    driver.get_schemas()
    driver.sample_table_data("orders", "sales", 5)
    assert _sent(conn) == [schemas_sql, sample_sql]


def test_bigquery_sends_built_sql() -> None:
    driver = BigQueryDriver()
    driver._project_id = "my-proj"
    driver._dataset = "shop"
    conn = _capture(driver)
    driver.get_schemas()
    driver.get_tables("shop")
    driver.get_views("shop")
    driver.sample_table_data("orders", "shop", 5)
    assert _sent(conn) == [
        "SELECT schema_name FROM `my-proj`.INFORMATION_SCHEMA.SCHEMATA "
        "WHERE NOT schema_name IN ('INFORMATION_SCHEMA', 'information_schema') "
        "ORDER BY schema_name",
        "SELECT table_name FROM `my-proj`.`shop`.INFORMATION_SCHEMA.TABLES "
        "WHERE table_type = 'BASE TABLE' ORDER BY table_name",
        "SELECT table_name, view_definition FROM "
        "`my-proj`.`shop`.INFORMATION_SCHEMA.VIEWS ORDER BY table_name",
        "SELECT * FROM `my-proj`.`shop`.`orders` LIMIT 5",
    ]


def test_databricks_sends_built_sql() -> None:
    from sqlalchemy.exc import SQLAlchemyError

    driver = DatabricksDriver()
    driver._catalog = "main"
    driver._schema = "sales"
    conn = _capture(driver)
    fallback = MagicMock()
    fallback.fetchall.return_value = []
    # The information_schema query fails, so SHOW SCHEMAS runs instead.
    conn.execute.side_effect = [SQLAlchemyError("no access"), fallback, fallback]
    driver.get_schemas()
    driver.sample_table_data("orders", "sales", 5)
    sent = _sent(conn)
    assert sent[1] == "SHOW SCHEMAS IN `main`"
    assert sent[2] == "SELECT * FROM `main`.`sales`.`orders` LIMIT 5"


def test_snowflake_show_statements_quote_the_schema() -> None:
    driver = SnowflakeDriver()
    conn = _capture(driver)
    driver.prefetch_schema_constraints(
        'sch"x',
        {"database": "DB"},
        cache_get=lambda _key: None,
        cache_store=lambda *_args, **_kwargs: None,
        log_sql=lambda _sql: None,
    )
    shows = [sql for sql in _sent(conn) if sql.startswith("SHOW")]
    assert shows == [
        'SHOW PRIMARY KEYS IN SCHEMA "DB"."sch""x"',
        'SHOW IMPORTED KEYS IN SCHEMA "DB"."sch""x"',
    ]


def test_dremio_sends_built_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    async def execute_query(query: str, *_args: Any) -> dict[str, Any]:
        sent.append(query)
        return {"success": True, "data": []}

    client = MagicMock()
    client.execute_query = AsyncMock(side_effect=execute_query)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    async def create_client() -> Any:
        return client

    driver = DremioDriver()
    monkeypatch.setattr(driver, "_create_dremio_client", create_client)
    driver.get_tables("it's")
    driver.get_views("it's")
    driver.sample_table_data("orders", "space.folder", 5)
    assert sent[:3] == [
        'SELECT TABLE_NAME FROM INFORMATION_SCHEMA."TABLES" '
        "WHERE TABLE_SCHEMA = 'it''s' AND TABLE_TYPE = 'TABLE'",
        "SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM "
        "INFORMATION_SCHEMA.\"VIEWS\" WHERE TABLE_SCHEMA = 'it''s'",
        'SELECT * FROM "space"."folder"."orders" LIMIT 5',
    ]
