"""The read-only gate refuses SELECTs that act like writes.

A statement can be a SELECT and still change state (``setval``), hold the
server (``pg_sleep``), lock rows (``FOR UPDATE``), read files on the host
(DuckDB's ``read_csv`` or ``FROM 'data.csv'``) or run SQL from a string
(``dblink``), and ``EXPLAIN ANALYZE`` runs the statement it explains. None of
that shows in the statement type, and OBQC checks only tables and columns, so
the parsed gate has to look for it. PostgreSQL and MySQL also run the query in
a read-only transaction, for what no list of names can catch.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import duckdb
import pytest

from src.database_manager import DatabaseManager
from src.security import analyze_sql_statement, explain_runs_statement

BLOCKED = [
    # Functions that change state, act on the server or hold a connection.
    ("postgres", "SELECT setval('sales.orders_id_seq', 1)"),
    ("postgres", "SELECT nextval('sales.orders_id_seq')"),
    ("postgres", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"),
    ("postgres", "SELECT pg_sleep(3600)"),
    ("postgres", "SELECT set_config('default_transaction_read_only', 'off', false)"),
    ("postgres", "SELECT pg_advisory_lock(1)"),
    ("mysql", "SELECT SLEEP(10)"),
    ("mysql", "SELECT BENCHMARK(100000000, MD5('a'))"),
    ("mysql", "SELECT GET_LOCK('x', 10)"),
    ("clickhouse", "SELECT sleepEachRow(1) FROM sales.orders"),
    ("snowflake", "SELECT SYSTEM$CANCEL_QUERY('01a2')"),
    # Files on the database host, other databases, the network.
    ("postgres", "SELECT lo_export(1, '/tmp/out')"),
    ("postgres", "SELECT pg_ls_dir('.')"),
    ("postgres", "SELECT * FROM dblink('dbname=x', 'DELETE FROM t') AS r(n int)"),
    ("postgres", "SELECT query_to_xml('SELECT * FROM sales.orders', true, false, '')"),
    ("duckdb", "SELECT * FROM read_csv('/etc/hosts')"),
    ("duckdb", "SELECT * FROM read_text('.env')"),
    ("duckdb", "SELECT * FROM glob('/**')"),
    ("duckdb", "SELECT * FROM query('SELECT 1')"),
    ("duckdb", "SELECT * FROM 'data.csv'"),
    ("duckdb", 'SELECT * FROM "/etc/hosts"'),
    ("duckdb", "SELECT * FROM 's3://bucket/x.parquet'"),
    ("clickhouse", "SELECT * FROM url('http://169.254.169.254/', CSV)"),
    ("clickhouse", "SELECT * FROM file('/etc/hosts', CSV)"),
    ("databricks", "SELECT * FROM read_files('s3://bucket/x')"),
    ("bigquery", "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')"),
    ("duckdb", 'SELECT * FROM "/tmp/probe"."csv"'),
    ("duckdb", 'SELECT * FROM main."/tmp/probe.csv"'),
    # Snowflake advances a sequence through a column-like reference too.
    ("snowflake", "SELECT seq.NEXTVAL FROM DUAL"),
    ("snowflake", "SELECT s.NEXTVAL FROM TABLE(GETNEXTVAL(seq)) s"),
    ("snowflake", "SELECT db.sch.seq.nextval AS id FROM sales.orders"),
    # An alias in a sibling subquery is not visible where seq.nextval stands.
    ("snowflake", "SELECT seq.nextval, (SELECT 1 FROM sales.orders seq) FROM dual"),
    # A CTE binds nothing unless selected from; an outer alias does not reach
    # into a CTE.
    ("snowflake", "WITH seq AS (SELECT 1 AS dummy) SELECT seq.NEXTVAL FROM DUAL"),
    (
        "snowflake",
        "WITH c AS (SELECT o.nextval FROM dual) SELECT 1 FROM sales.orders o",
    ),
    # Quoted "seq" is not unquoted SEQ, and a database qualifier must match.
    ("snowflake", 'SELECT seq.NEXTVAL FROM DUAL AS "seq"'),
    ("snowflake", "SELECT otherdb.sales.seq.NEXTVAL FROM currentdb.sales.seq"),
    # Inside a CTE or subquery is no different.
    ("postgres", "WITH x AS (SELECT pg_sleep(60)) SELECT * FROM x"),
    ("duckdb", "SELECT a FROM t WHERE a IN (SELECT a FROM read_csv('x.csv'))"),
]


@pytest.mark.parametrize(("dialect", "sql"), BLOCKED)
def test_side_effects_are_refused(dialect: str, sql: str) -> None:
    result = analyze_sql_statement(sql, dialect=dialect)
    assert not result["is_read_only"], sql
    assert result["query_type"] == "UNSAFE", result
    assert result["unsafe_operations"]
    assert result["error"].startswith("Not allowed in a read query")


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "SELECT * FROM sales.orders FOR UPDATE"),
        ("postgres", "SELECT * FROM sales.orders FOR SHARE"),
        ("postgres", "SELECT * INTO scratch.copy FROM sales.orders"),
    ],
)
def test_row_locks_and_select_into_are_writes(dialect: str, sql: str) -> None:
    result = analyze_sql_statement(sql, dialect=dialect)
    assert result["query_type"] == "WRITE", result
    assert not result["is_read_only"]


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "EXPLAIN ANALYZE DELETE FROM sales.orders"),
        ("postgres", "EXPLAIN ANALYSE SELECT 1"),
        ("postgres", "EXPLAIN (ANALYZE, BUFFERS) UPDATE sales.orders SET total = 0"),
        ("postgres", "EXPLAIN VERBOSE ANALYZE SELECT 1"),
        ("duckdb", "EXPLAIN ANALYZE DELETE FROM sales.orders"),
        ("trino", "EXPLAIN ANALYZE SELECT * FROM sales.orders"),
        ("mysql", "EXPLAIN ANALYZE SELECT * FROM sales.orders"),
    ],
)
def test_explain_analyze_is_refused(dialect: str, sql: str) -> None:
    # It runs the statement: EXPLAIN ANALYZE DELETE deletes.
    result = analyze_sql_statement(sql, dialect=dialect)
    assert not result["is_read_only"], result
    assert result["query_type"] in ("UNSAFE", "WRITE"), result


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "EXPLAIN DELETE FROM sales.orders"),
        ("postgres", "EXPLAIN SELECT pg_sleep(60)"),
        ("duckdb", "EXPLAIN SELECT * FROM read_csv('/etc/hosts')"),
        ("mysql", "EXPLAIN DELETE FROM sales.orders"),
        ("postgres", "EXPLAIN (FORMAT JSON)"),
    ],
)
def test_explain_must_explain_an_allowed_read(dialect: str, sql: str) -> None:
    result = analyze_sql_statement(sql, dialect=dialect)
    assert not result["is_read_only"], result


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "EXPLAIN SELECT * FROM sales.orders WHERE id = 1"),
        ("postgres", "EXPLAIN (FORMAT JSON) SELECT * FROM sales.orders"),
        ("postgres", "EXPLAIN VERBOSE WITH x AS (SELECT 1 AS n) SELECT n FROM x"),
        ("snowflake", "EXPLAIN USING TEXT SELECT * FROM sales.orders"),
        ("databricks", "EXPLAIN EXTENDED SELECT * FROM sales.orders"),
        ("clickhouse", "EXPLAIN PLAN SELECT * FROM sales.orders"),
        ("mysql", "EXPLAIN FORMAT=TREE SELECT * FROM sales.orders"),
        ("mysql", "DESCRIBE sales.orders"),
        ("postgres", "SHOW search_path"),
    ],
)
def test_metadata_statements_still_pass(dialect: str, sql: str) -> None:
    result = analyze_sql_statement(sql, dialect=dialect)
    assert result["is_read_only"], result
    assert result["query_type"] == "METADATA"


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "VACUUM sales.orders"),
        ("postgres", "LISTEN alerts"),
    ],
)
def test_other_unparsed_commands_are_refused(dialect: str, sql: str) -> None:
    # sqlglot turns what it cannot parse into a Command; only the metadata
    # keywords may pass as one.
    result = analyze_sql_statement(sql, dialect=dialect)
    assert not result["is_read_only"], result


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (
            "postgres",
            "SELECT date_trunc('month', o.created_at) AS m, count(*), "
            "sum(o.total) FROM sales.orders o GROUP BY 1",
        ),
        # Columns that share a name with a forbidden function are columns.
        ("postgres", "SELECT o.url, o.file, o.sleep, o.query FROM sales.orders o"),
        ("postgres", "SELECT read_count, lo_value FROM sales.metrics"),
        ("duckdb", "SELECT name FROM files WHERE name GLOB '*.csv'"),
        ("duckdb", "SELECT * FROM main.orders"),
        ("mysql", "SELECT CONCAT(first_name, ' ', last_name) FROM people"),
        ("snowflake", "SELECT SYSTEM$TYPEOF(1)"),
        ("snowflake", "SELECT o.nextval_at, nextval FROM sales.orders o"),
        # NEXTVAL on a table, alias or CTE in view is a column, not a sequence.
        ("snowflake", "SELECT o.nextval FROM sales.orders o"),
        ("snowflake", "SELECT sales.orders.nextval FROM sales.orders"),
        ("snowflake", "SELECT orders.nextval FROM sales.orders"),
        ("snowflake", "SELECT O.nextval FROM sales.orders o"),
        ("snowflake", 'SELECT "o".nextval FROM sales.orders AS "o"'),
        ("snowflake", "SELECT (SELECT o.nextval) AS n FROM sales.orders o"),
        ("snowflake", "WITH c AS (SELECT 1 AS nextval) SELECT c.nextval FROM c"),
        # Only Snowflake has seq.NEXTVAL; elsewhere it is always a column.
        ("duckdb", "SELECT o.nextval FROM sales.orders o"),
        ("postgres", "SELECT x.nextval FROM sales.orders x"),
    ],
)
def test_ordinary_reads_pass(dialect: str, sql: str) -> None:
    result = analyze_sql_statement(sql, dialect=dialect)
    assert result["is_read_only"], result
    assert result["unsafe_operations"] == []


def test_explain_runs_statement_reads_only_the_options() -> None:
    assert explain_runs_statement("EXPLAIN ANALYZE SELECT 1")
    assert explain_runs_statement("explain (analyze true) select 1")
    assert not explain_runs_statement("EXPLAIN SELECT analyze FROM t")
    assert not explain_runs_statement("SELECT 1")
    assert not explain_runs_statement("ANALYZE t")


# --- Through DatabaseManager, against a real DuckDB file -------------------


@pytest.fixture
def duckdb_manager(tmp_path: Path) -> DatabaseManager:
    path = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE SCHEMA sales; "
        "CREATE TABLE sales.orders AS SELECT range AS id FROM range(10); "
        "CREATE SEQUENCE sales.order_seq START 1; "
        "CREATE VIEW main.orders_view AS SELECT * FROM sales.orders"
    )
    con.close()
    (tmp_path / "data.csv").write_text("code\nX7Q2\n")
    db = DatabaseManager()
    assert db.connect_duckdb(str(path))
    return db


def _count(db: DatabaseManager) -> Any:
    return db.execute_sql_query("SELECT COUNT(*) AS n FROM sales.orders")["data"]


def test_duckdb_refuses_file_reads(
    duckdb_manager: DatabaseManager, tmp_path: Path
) -> None:
    data = tmp_path / "data.csv"
    for sql in (
        f"SELECT * FROM read_csv('{data}')",
        f"SELECT * FROM '{data}'",
        f'SELECT * FROM "{data}"',
    ):
        result = duckdb_manager.execute_sql_query(sql)
        assert not result["success"], sql
        assert result["error_type"] == "forbidden_operation", result
        assert "X7Q2" not in str(result)


def test_duckdb_refuses_a_query_it_cannot_parse(
    duckdb_manager: DatabaseManager, tmp_path: Path
) -> None:
    # Valid DuckDB that sqlglot cannot parse: every check after parsing would
    # be skipped, so the query is refused rather than run unchecked.
    result = duckdb_manager.execute_sql_query(
        f"SELECT * FROM read_text('{tmp_path / 'data.csv'}') "
        "WHERE EXISTS (PIVOT_WIDER (SELECT 1 AS a) ON a)"
    )
    assert not result["success"]
    assert result["error_type"] == "syntax_error", result
    assert "X7Q2" not in str(result)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM data.csv",
        'SELECT * FROM "data"."csv"',
        'SELECT * FROM data."CSV"',
        "SELECT * FROM main.data.csv",
    ],
)
def test_duckdb_refuses_names_outside_its_catalog(
    duckdb_manager: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
) -> None:
    # DuckDB reads a dotted name it cannot resolve as a file in its working
    # directory, joining the parts: data.csv, "data"."csv" and main.data.csv
    # all reach ./data.csv.
    monkeypatch.chdir(tmp_path)
    result = duckdb_manager.execute_sql_query(sql)
    assert not result["success"], sql
    assert result["error_type"] in ("unknown_table", "forbidden_operation"), result
    assert "X7Q2" not in str(result)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) AS n FROM shop.sales.orders",
        "SELECT count(*) AS n FROM shop.orders_view",
        "SELECT count(*) AS n FROM information_schema.tables",
        "WITH data AS (SELECT 1 AS a) SELECT a FROM data",
        "SELECT count(*) AS n FROM range(3)",
    ],
)
def test_duckdb_catalog_names_still_resolve(
    duckdb_manager: DatabaseManager, sql: str
) -> None:
    result = duckdb_manager.execute_sql_query(sql)
    assert result["success"], result


def test_duckdb_cannot_advance_a_sequence(duckdb_manager: DatabaseManager) -> None:
    result = duckdb_manager.execute_sql_query(
        "SELECT nextval('sales.order_seq') FROM sales.orders"
    )
    assert not result["success"]
    assert result["error_type"] == "forbidden_operation"


def test_duckdb_explain_analyze_delete_does_not_delete(
    duckdb_manager: DatabaseManager,
) -> None:
    result = duckdb_manager.execute_sql_query(
        "EXPLAIN ANALYZE DELETE FROM sales.orders"
    )
    assert not result["success"]
    assert _count(duckdb_manager) == [{"n": 10}]


def test_duckdb_ordinary_query_still_runs(duckdb_manager: DatabaseManager) -> None:
    result = duckdb_manager.execute_sql_query(
        "SELECT id FROM sales.orders WHERE id < 3 ORDER BY id"
    )
    assert result["success"], result
    assert [row["id"] for row in result["data"]] == [0, 1, 2]


# --- The read-only transaction ---------------------------------------------


def _driver_with_mock_engine(driver: Any) -> MagicMock:
    conn = MagicMock()
    conn.execute.return_value.returns_rows = False
    conn.execute.return_value.rowcount = 0
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    driver.engine = engine
    return conn


@pytest.mark.parametrize(
    ("driver_path", "statement"),
    [
        ("src.drivers.postgresql.PostgreSQLDriver", "SET TRANSACTION READ ONLY"),
        ("src.drivers.mysql.MySQLDriver", "START TRANSACTION READ ONLY"),
    ],
)
def test_query_runs_in_a_read_only_transaction(
    driver_path: str, statement: str
) -> None:
    module_name, class_name = driver_path.rsplit(".", 1)
    module = __import__(module_name, fromlist=[class_name])
    driver = getattr(module, class_name)()
    conn = _driver_with_mock_engine(driver)

    result = driver.execute_sql_query("SELECT 1")

    assert result["success"], result
    executed = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert executed == [statement, "SELECT 1"]


# --- Schema names written into driver metadata SQL ---------------------------


def _bigquery_driver() -> tuple[Any, MagicMock]:
    from src.drivers.bigquery import BigQueryDriver

    driver = BigQueryDriver()
    driver._project_id = "proj"
    driver._dataset = "shop"
    conn = _driver_with_mock_engine(driver)
    conn.execute.return_value.fetchall.return_value = []
    return driver, conn


@pytest.mark.parametrize("method", ["get_tables", "get_views"])
def test_bigquery_refuses_a_schema_name_that_is_not_a_dataset_name(
    method: str,
) -> None:
    driver, conn = _bigquery_driver()
    crafted = "shop`.INFORMATION_SCHEMA.TABLES; SELECT 1; SELECT 1 FROM `proj.shop"
    assert not getattr(driver, method)(crafted)
    conn.execute.assert_not_called()


@pytest.mark.parametrize("method", ["get_tables", "get_views"])
def test_bigquery_queries_a_valid_dataset(method: str) -> None:
    driver, conn = _bigquery_driver()
    getattr(driver, method)("Sales_2026")
    assert "`proj.Sales_2026`.INFORMATION_SCHEMA" in str(conn.execute.call_args.args[0])


def test_databricks_show_tables_fallback_quotes_the_schema() -> None:
    from sqlalchemy.exc import SQLAlchemyError

    from src.drivers.databricks import DatabricksDriver

    driver = DatabricksDriver()
    driver._catalog = "main"
    conn = _driver_with_mock_engine(driver)
    fallback = MagicMock()
    fallback.fetchall.return_value = []
    conn.execute.side_effect = [SQLAlchemyError("no information_schema"), fallback]

    driver.get_tables("sales` ; SELECT 1 ; `x")

    sql = str(conn.execute.call_args.args[0])
    assert sql == "SHOW TABLES IN `main`.`sales`` ; SELECT 1 ; ``x`"


# --- DuckDB name resolution, checked against DuckDB itself -------------------


def test_duckdb_resolver_agrees_with_duckdb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every reference the resolver accepts, DuckDB finds in its catalog.

    DuckDB reads a reference its catalog misses as a file, so the resolver
    must never accept one DuckDB would miss. Ground truth is EXPLAIN in an
    empty working directory: it succeeds only when the catalog resolves the
    name, since there is no file to fall back on.
    """
    from src.drivers.duckdb import _resolves_in_catalog
    from src.security import ascii_lower

    con = duckdb.connect(str(tmp_path / "data.duckdb"))
    con.execute(
        "CREATE SCHEMA sales; CREATE TABLE sales.csv(a int); "
        "CREATE TABLE sales.orders(a int); CREATE TABLE main.items(a int); "
        "CREATE VIEW main.v AS SELECT 1 AS a; CREATE SCHEMA data; "
        "CREATE TABLE data.data.only_here(a int); CREATE TEMP TABLE tmp1(a int); "
        'CREATE SCHEMA "DÀTA"; CREATE TABLE "DÀTA".csv(a int)'
    )
    con.execute(
        f"ATTACH '{tmp_path / 'other.duckdb'}' AS other; "
        "CREATE SCHEMA other.sales; CREATE TABLE other.sales.csv(a int); "
        "CREATE TABLE other.main.x(a int)"
    )

    def lowered(sql: str) -> set[tuple[str, ...]]:
        return {
            tuple(ascii_lower(str(v)) for v in row)
            for row in con.execute(sql).fetchall()
        }

    known = lowered(
        "SELECT database_name, schema_name, table_name FROM duckdb_tables() "
        "UNION ALL SELECT database_name, schema_name, view_name FROM duckdb_views()"
    )
    schemas = lowered("SELECT database_name, schema_name FROM duckdb_schemas()")
    databases = {
        name for (name,) in lowered("SELECT database_name FROM duckdb_databases()")
    }
    catalog, schema = (
        ascii_lower(str(v))
        for v in con.execute("SELECT current_database(), current_schema()").fetchone()
    )

    names = [
        "csv",
        "orders",
        "items",
        "v",
        "tmp1",
        "x",
        "tables",
        "pg_class",
        "only_here",
        "duckdb_tables",
    ]
    firsts = [
        "data",
        "sales",
        "main",
        "other",
        "temp",
        "system",
        "information_schema",
        "pg_catalog",
        "memory",
        # DuckDB folds ASCII case only: DÀTA and dÀta match, dàta does not.
        "DÀTA",
        "dÀta",
        "dàta",
        "DàTA",
    ]
    references = [(n,) for n in names] + [(f, n) for f in firsts for n in names]
    references += [
        (c, sc, n)
        for c in ["data", "other", "system", "temp"]
        for sc in ["main", "sales", "data", "information_schema", "pg_catalog"]
        for n in names
    ]
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    disagreements = []
    for parts in references:
        quoted = ".".join(f'"{p}"' for p in parts)
        try:
            con.execute(f"EXPLAIN SELECT * FROM {quoted}")
            duckdb_finds = True
        except duckdb.Error:
            duckdb_finds = False
        folded = tuple(ascii_lower(p) for p in parts)
        ours = _resolves_in_catalog(folded, known, schemas, databases, catalog, schema)
        if ours != duckdb_finds:
            disagreements.append((parts, ours, duckdb_finds))
    assert not disagreements
    # The reviewer's case: catalog "data" has sales.csv but no main.csv.
    assert not _resolves_in_catalog(
        ("data", "csv"), known, schemas, databases, catalog, schema
    )


def test_duckdb_catalog_name_does_not_cover_a_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # data.duckdb holds sales.csv; DuckDB reads data.csv as catalog "data",
    # schema main -- which has no csv -- so it falls back to ./data.csv.
    con = duckdb.connect(str(tmp_path / "data.duckdb"))
    con.execute("CREATE SCHEMA sales; CREATE TABLE sales.csv(a int)")
    con.close()
    (tmp_path / "data.csv").write_text("code\nX7Q2\n")
    monkeypatch.chdir(tmp_path)
    db = DatabaseManager()
    assert db.connect_duckdb(str(tmp_path / "data.duckdb"))

    result = db.execute_sql_query("SELECT * FROM data.csv")

    assert not result["success"]
    assert result["error_type"] == "unknown_table", result
    assert "X7Q2" not in str(result)
    assert db.execute_sql_query("SELECT count(*) AS n FROM sales.csv")["success"]


def test_duckdb_unicode_case_does_not_cover_a_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DuckDB folds ASCII case only, so "dàta".csv is not the table "DÀTA".csv:
    # it misses the catalog and is read as ./dàta.csv.
    con = duckdb.connect(str(tmp_path / "u.duckdb"))
    con.execute('CREATE SCHEMA "DÀTA"; CREATE TABLE "DÀTA".csv(a int)')
    con.close()
    (tmp_path / "dàta.csv").write_text("code\nX7Q2\n")
    monkeypatch.chdir(tmp_path)
    db = DatabaseManager()
    assert db.connect_duckdb(str(tmp_path / "u.duckdb"))

    result = db.execute_sql_query('SELECT * FROM "dàta".csv')

    assert not result["success"]
    assert result["error_type"] == "unknown_table", result
    assert "X7Q2" not in str(result)
    assert db.execute_sql_query('SELECT count(*) AS n FROM "dÀta".csv')["success"]
