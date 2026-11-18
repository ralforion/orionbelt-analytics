"""Dates stored as text or numbers: inferred, annotated, and checked.

A column holding ``'20150601'`` compared with ``'2015-06-01'`` compares text:
``=`` never matches and ``>=`` returns the wrong rows, and the query runs and
looks right. Discovery samples date-like columns to learn the format, the
ontology records it as ``oba:dateFormat``, and OBQC warns when a query writes
a date another way.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from rdflib import Literal, Namespace

import src.main as main_module
import src.server_state as state_module
from src.database_manager import ColumnInfo, TableInfo
from src.date_formats import (
    example,
    infer_date_format,
    is_candidate,
    matches,
    rewrite,
)
from src.handlers import connection as connection_handler
from src.main import mcp
from src.obqc_validator import OBQCIssueType, OBQCValidator
from src.ontology_generator import OntologyGenerator
from src.server_state import ServerState

OBA = Namespace("https://ralforion.com/ns/oba#")
DAYS = ["20150601", "20150602", "20151231", "20160101", "20140715"]

# --- Inference ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (DAYS, "%Y%m%d"),
        ([int(d) for d in DAYS], "%Y%m%d"),
        (
            ["2015-06-01", "2015-06-02", "2015-12-31", "2016-01-01", "2014-07-15"],
            "%Y-%m-%d",
        ),
        (
            ["06/13/2015", "12/31/2015", "01/02/2016", "07/15/2014", "02/28/2015"],
            "%m/%d/%Y",
        ),
        (
            ["13/06/2015", "31/12/2015", "02/01/2016", "15/07/2014", "28/02/2015"],
            "%d/%m/%Y",
        ),
        (["950324", "950325", "960101", "991231", "000229"], "%y%m%d"),
        (
            ["01-Jun-2015", "15-Jul-2014", "31-Dec-2015", "02-Jan-2016", "28-Feb-2015"],
            "%d-%b-%Y",
        ),
        (
            [
                "2015-06-01 10:00:00.500000",
                "2015-06-02 11:00:00.000000",
                "2015-12-31 00:00:00.250000",
                "2016-01-01 00:00:00.000001",
                "2014-07-15 09:30:00.999999",
            ],
            "%Y-%m-%d %H:%M:%S.%f",
        ),
    ],
)
def test_a_format_every_value_reads_in_is_found(
    values: list[Any], expected: str
) -> None:
    assert infer_date_format(values) == expected


@pytest.mark.parametrize(
    "values",
    [
        # Day-first and month-first both read these, as different dates.
        ["01/02/2015", "03/04/2015", "05/06/2015", "07/08/2015", "09/10/2015"],
        ["20150601", "2015-06-02", "20151231", "20160101", "20140715"],  # mixed
        ["20150601", "20150602"],  # too few to tell
        ["2015061", "20150602", "20151231", "20160101", "20140715"],  # short value
        ["A100", "B200", "C300", "D400", "E500"],  # not dates
        [None, "", None, "", None],
        # Read by %Y-%m-%d, but not written that way: '2015-06-01' would
        # match none of them.
        ["2015-6-1", "2015-6-2", "2015-6-3", "2015-6-4", "2015-6-5"],
        # Fractions of a second written short, and zones written as Z: the
        # format would write them differently.
        [
            "2015-06-01 10:00:00.5",
            "2015-06-02 10:00:00.5",
            "2015-06-03 10:00:00.5",
            "2015-06-04 10:00:00.5",
            "2015-06-05 10:00:00.5",
        ],
        [
            "2015-06-01T10:00:00Z",
            "2015-06-02T10:00:00Z",
            "2015-06-03T10:00:00Z",
            "2015-06-04T10:00:00Z",
            "2015-06-05T10:00:00Z",
        ],
        # A number with a fraction is no date: it rules the column out.
        [20150601, 20150602, 20151231, 20160101, 20140715, 1.5],
        # Stored with spaces: '20150601' would match none of them.
        [" 20150601 ", " 20150602 ", " 20151231 ", " 20160101 ", " 20140715 "],
    ],
)
def test_no_format_is_claimed_without_a_clear_one(values: list[Any]) -> None:
    assert infer_date_format(values) is None


@pytest.mark.parametrize(
    ("name", "data_type", "expected"),
    [
        ("order_day", "VARCHAR(8)", True),
        ("created_at", "BIGINT", True),
        ("invoice_ymd", "INTEGER", True),
        ("order_day", "DATE", False),  # native: needs no format
        ("updated", "TIMESTAMP", False),
        ("customer_id", "INTEGER", False),  # not a date-like name
        ("start_day", "BOOLEAN", False),
    ],
)
def test_only_date_like_text_and_integer_columns_are_sampled(
    name: str, data_type: str, expected: bool
) -> None:
    assert is_candidate(name, data_type) is expected


def test_literals_are_checked_and_rewritten_in_the_columns_form() -> None:
    assert matches("%Y%m%d", "20150601")
    assert matches("%Y%m%d", 20150601)
    assert not matches("%Y%m%d", "2015-06-01")
    assert rewrite("%Y%m%d", "2015-06-01") == "20150601"
    assert rewrite("%Y%m%d", "01/02/2015") is None  # which date is it?
    assert example("%y%m%d") == "150601"


# --- OBQC --------------------------------------------------------------------


def _validator() -> OBQCValidator:
    columns = [
        ColumnInfo("id", "INTEGER", False, True, False),
        ColumnInfo("order_day", "VARCHAR(8)", True, False, False, date_format="%Y%m%d"),
        ColumnInfo("invoice_ymd", "INTEGER", True, False, False, date_format="%Y%m%d"),
        ColumnInfo("shipped", "DATE", True, False, False),
        ColumnInfo("booked_on", "VARCHAR", True, False, False, date_format="%d.%m.%Y"),
        ColumnInfo("issued_ymd", "INTEGER", True, False, False, date_format="%y%m%d"),
    ]
    table = TableInfo("orders", "main", columns, ["id"], [])
    generator = OntologyGenerator(base_uri="http://example.com/ontology/")
    generator.generate_from_schema([table])
    validator = OBQCValidator()
    validator.load_ontology(generator.graph, "http://example.com/ontology/")
    return validator


def _date_warnings(sql: str) -> list[Any]:
    result = _validator().validate(sql, dialect="duckdb")
    return [
        i for i in result.issues if i.issue_type == OBQCIssueType.DATE_FORMAT_MISMATCH
    ]


def test_the_ontology_records_the_format() -> None:
    columns = [
        ColumnInfo("order_day", "VARCHAR(8)", True, False, False, date_format="%Y%m%d")
    ]
    generator = OntologyGenerator(base_uri="http://example.com/ontology/")
    generator.generate_from_schema([TableInfo("orders", "main", columns, [], [])])
    assert Literal("%Y%m%d") in set(generator.graph.objects(None, OBA.dateFormat))


@pytest.mark.parametrize(
    ("sql", "suggestion"),
    [
        ("SELECT id FROM orders WHERE order_day >= '2015-06-01'", "'20150601'"),
        ("SELECT id FROM orders o WHERE o.order_day = '2015-06-01'", "'20150601'"),
        (
            "SELECT id FROM orders WHERE order_day BETWEEN '2015-06-01' AND '20150630'",
            "'20150601'",
        ),
        ("SELECT id FROM orders WHERE order_day IN ('2015-06-01')", "'20150601'"),
        # An integer column: the value is written as a number.
        ("SELECT id FROM orders WHERE invoice_ymd = '2015-06-01'", "20150601"),
    ],
)
def test_a_date_in_another_form_is_reported(sql: str, suggestion: str) -> None:
    (warning,) = _date_warnings(sql)
    assert (
        "orders.order_day" in warning.message or "orders.invoice_ymd" in warning.message
    )
    assert warning.suggestion == f"Write the value as {suggestion}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders WHERE order_day >= '20150601'",
        "SELECT id FROM orders WHERE invoice_ymd = 20150601",
        "SELECT id FROM orders WHERE shipped >= '2015-06-01'",  # native DATE
        "SELECT id FROM orders WHERE order_day = 'unknown'",  # not a date at all
    ],
)
def test_a_date_in_the_columns_form_is_not_reported(sql: str) -> None:
    assert _date_warnings(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders WHERE booked_on >= '2015-06-01'",
        # In the column's own form and still wrong: '01.06.2015' sorts after
        # '31.05.2016' as text.
        "SELECT id FROM orders WHERE booked_on >= '01.06.2015'",
        "SELECT id FROM orders o WHERE o.booked_on BETWEEN '01.06.2015' AND '30.06.2015'",
    ],
)
def test_a_range_on_a_form_that_does_not_sort_converts_the_column(sql: str) -> None:
    (warning,) = _date_warnings(sql)
    assert "do not sort in date order" in warning.message
    # The conversion in the query's dialect, and the value as a date.
    assert "STRPTIME(" in warning.suggestion and "'%d.%m.%Y'" in warning.suggestion
    assert "DATE '2015-06-01'" in warning.suggestion


def test_equality_on_a_form_that_does_not_sort_rewrites_the_value() -> None:
    (warning,) = _date_warnings("SELECT id FROM orders WHERE booked_on = '2015-06-01'")
    assert warning.suggestion == "Write the value as '01.06.2015'"
    assert _date_warnings("SELECT id FROM orders WHERE booked_on = '01.06.2015'") == []


def test_dremio_paths_are_sampled_part_by_part() -> None:
    from unittest.mock import MagicMock

    from src.database_manager import DatabaseManager

    db = DatabaseManager()
    db.connection_info = {"type": "dremio"}
    db.execute_sql_query = MagicMock(return_value={"success": True, "data": []})
    table = TableInfo(
        "orders",
        "space.folder",
        [ColumnInfo("order_day", "VARCHAR", True, False, False)],
        [],
        [],
    )
    db.infer_date_formats([table], "space.folder")
    sql = db.execute_sql_query.call_args.args[0]
    assert 'FROM "space"."folder"."orders"' in sql


# --- End to end, against a DuckDB file ---------------------------------------


@pytest.fixture
def shop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    import duckdb

    path = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE orders (id INTEGER, order_day VARCHAR, invoice_ymd INTEGER, "
        "shipped DATE, note VARCHAR)"
    )
    for i, day in enumerate(DAYS):
        con.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, 'x')",
            [i, day, int(day), f"{day[:4]}-{day[4:6]}-{day[6:]}"],
        )
    con.close()
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    for module in ("src.paths", "src.handlers.ontology_validation"):
        monkeypatch.setattr(f"{module}.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setenv("AUTO_GRAPHRAG", "false")
    monkeypatch.setenv("OBA_SHACL_VALIDATE", "true")
    monkeypatch.delenv("OBA_DATABASES", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(path))
    yield tmp_path
    fresh.cleanup()


def _data(result: Any) -> Any:
    return result.data if not isinstance(result.data, str) else json.loads(result.data)


def _ontology_text(workdir: Path) -> str:
    return "\n".join(p.read_text() for p in workdir.rglob("ontology_*.ttl"))


async def _connect(client: Client) -> dict[str, str]:
    connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
    text = (
        connected.data
        if isinstance(connected.data, str)
        else json.dumps(connected.data)
    )
    return {"connection": re.search(r"(ob_[a-z0-9]{6})", text).group(1)}


async def test_discovery_finds_formats_and_queries_are_warned(shop: Path) -> None:
    async with Client(mcp) as client:
        on = await _connect(client)
        await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        details = _data(
            await client.call_tool("get_table_details", {**on, "table_name": "orders"})
        )
        formats = {c["name"]: c.get("date_format") for c in details["columns"]}
        assert formats == {
            "id": None,
            "order_day": "%Y%m%d",
            "invoice_ymd": "%Y%m%d",
            "shipped": None,  # a native DATE needs no format
            "note": None,
        }

        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})
        ttl = _ontology_text(shop)
        assert '"%Y%m%d"' in ttl and "dateFormat" in ttl

        result = _data(
            await client.call_tool(
                "execute_sql_query",
                {
                    **on,
                    "sql_query": "SELECT count(*) AS n FROM orders "
                    "WHERE order_day >= '2015-06-01'",
                    "checklist_completed": True,
                },
            )
        )
        assert any("Write the value as '20150601'" in w for w in result["warnings"])


async def test_inference_can_be_switched_off(
    shop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBA_INFER_DATE_FORMATS", "false")
    async with Client(mcp) as client:
        on = await _connect(client)
        await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        session = main_module._server_state.session_for_handle(on["connection"])
        (orders,) = session.get_cached_schema("main")
        assert all(column.date_format is None for column in orders.columns)


def test_an_ontology_with_a_date_format_conforms_to_the_shapes() -> None:
    from src.shacl_validator import shacl_available, validate_ontology

    if not shacl_available():
        pytest.skip("pyshacl not installed")
    columns = [
        ColumnInfo("id", "INTEGER", False, True, False),
        ColumnInfo("order_day", "VARCHAR(8)", True, False, False, date_format="%Y%m%d"),
    ]
    generator = OntologyGenerator(base_uri="http://example.com/ontology/")
    ttl = generator.generate_from_schema(
        [TableInfo("orders", "main", columns, ["id"], [])]
    )
    report = validate_ontology(ttl)
    assert report["conforms"], report.get("report")


def test_graphrag_carries_the_format_with_the_column() -> None:
    from src.graphrag.embedder import SchemaEmbedder

    element = SchemaEmbedder("tfidf")._describe_column(
        table_name="orders",
        column_name="order_day",
        data_type="VARCHAR(8)",
        schema="main",
        date_format="%Y%m%d",
    )
    assert element.metadata["date_format"] == "%Y%m%d"
    assert "date stored as 20150601" in element.description
    plain = SchemaEmbedder("tfidf")._describe_column(
        table_name="orders", column_name="note", data_type="VARCHAR", schema="main"
    )
    assert "date_format" not in plain.metadata  # vector stores take no None


def test_a_range_bound_keeps_its_time_of_day() -> None:
    (warning,) = _date_warnings(
        "SELECT id FROM orders WHERE booked_on >= '2015-06-01 12:00:00'"
    )
    # Midnight on June 1 is before noon: a DATE bound would let it in.
    assert "TIMESTAMP '2015-06-01 12:00:00'" in warning.suggestion
    assert "DATE '2015-06-01'" not in warning.suggestion


def test_an_equality_rewrite_that_would_drop_the_time_is_declined() -> None:
    (warning,) = _date_warnings(
        "SELECT id FROM orders WHERE booked_on = '2015-06-01 12:00:00'"
    )
    assert warning.suggestion == "Write dates in that form, like '01.06.2015'"
    assert rewrite("%y%m%d", "2075-01-01") is None  # would read back as 1975


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders WHERE issued_ymd < 10101",
        "SELECT id FROM orders WHERE issued_ymd < 10101.00",
        "SELECT id FROM orders WHERE 10101 > issued_ymd",
        "SELECT id FROM orders WHERE issued_ymd < '10101'",
    ],
)
def test_a_number_that_lost_its_leading_zero_is_still_a_date(sql: str) -> None:
    # 10101 is 010101 in %y%m%d, 2001-01-01, yet as a number it is below
    # every date of the 1990s.
    (warning,) = _date_warnings(sql)
    assert "do not sort in date order as numbers" in warning.message
    assert "LPAD(" in warning.suggestion
    assert "DATE '2001-01-01'" in warning.suggestion


def test_numbers_in_the_columns_form_match_without_their_zeros() -> None:
    for sql in (
        "SELECT id FROM orders WHERE issued_ymd = 10101",
        "SELECT id FROM orders WHERE issued_ymd = 950324.00",
        "SELECT id FROM orders WHERE issued_ymd IN (10101, 950324)",
    ):
        assert _date_warnings(sql) == [], sql
    (warning,) = _date_warnings("SELECT id FROM orders WHERE issued_ymd = '2001-01-01'")
    assert warning.suggestion == "Write the value as 10101"
    # Not a whole number: no date, and no business of this rule.
    assert _date_warnings("SELECT id FROM orders WHERE issued_ymd = 10101.5") == []


def test_padded_reads_a_number_as_the_conversion_does() -> None:
    from src.date_formats import padded

    assert padded("10101", "%y%m%d") == "010101"
    assert padded("10101.00", "%y%m%d") == "010101"
    assert padded(10101, "%Y%m%d") == "00010101"
    assert padded("10101.5", "%y%m%d") == "10101.5"  # no whole number
    assert padded("-10101", "%y%m%d") == "-10101"
    assert padded("2015-06-01", "%y%m%d") == "2015-06-01"
    assert padded("10101", "%d.%m.%y") == "10101"  # writes more than digits


def test_a_number_bound_on_a_two_digit_year_column_is_warned_live() -> None:
    import duckdb
    from sqlglot import exp

    from src.date_formats import to_date_sql

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders AS SELECT id, issued_ymd FROM (VALUES "
        "(1, 950324), (2, 960101), (3, 971105), (4, 980615), (5, 991231)"
        ") v(id, issued_ymd)"
    )
    sampled = [r[0] for r in con.execute("SELECT issued_ymd FROM orders").fetchall()]
    assert infer_date_format(sampled) == "%y%m%d"

    # As numbers, every one of them is >= 10101: the query finds none.
    query = "SELECT count(*) FROM orders WHERE issued_ymd < 10101"
    assert con.execute(query).fetchone() == (0,)
    (warning,) = _date_warnings(query)
    # Compared as dates, all five are before 2001-01-01.
    converted = to_date_sql(exp.column("issued_ymd"), "%y%m%d", "duckdb", True)
    fixed = f"SELECT count(*) FROM orders WHERE {converted} < DATE '2001-01-01'"
    assert converted is not None and converted in warning.suggestion
    assert con.execute(fixed).fetchone() == (5,)


def test_a_decimal_column_converts_the_way_it_was_read() -> None:
    import duckdb
    from sqlglot import exp

    from src.date_formats import to_date_sql

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES (950324.00), (950325.00), "
        "(960101.00), (991231.00), (980615.00), (10101.00)) v(d)"
    )
    sampled = [row[0] for row in con.execute("SELECT d FROM t LIMIT 5").fetchall()]
    assert infer_date_format(sampled) == "%y%m%d"
    converted = to_date_sql(exp.column("d"), "%y%m%d", "duckdb", numeric=True)
    days = sorted(
        str(row[0]) for row in con.execute(f"SELECT {converted} FROM t").fetchall()
    )
    # .00 dropped, 10101 padded to 010101, and 95 read as 1995, 01 as 2001.
    assert days == [
        "1995-03-24",
        "1995-03-25",
        "1996-01-01",
        "1998-06-15",
        "1999-12-31",
        "2001-01-01",
    ]


def test_two_digit_years_get_their_century_written_out() -> None:
    from sqlglot import exp

    from src.date_formats import to_date_sql

    # Databricks reads yy as 20yy: 95 would be 2095. The century is written
    # into the value instead, as inference read it, and parsed as yyyy.
    sql = to_date_sql(exp.column("d"), "%y%m%d", "databricks", database="databricks")
    assert sql is not None
    assert "'yyyyMMdd'" in sql and "'yyMMdd'" not in sql
    assert "THEN '20' ELSE '19'" in sql


def test_dremio_gets_its_own_conversion() -> None:
    from sqlglot import exp

    from src.date_formats import to_date_sql

    result = _validator().validate(
        "SELECT id FROM orders WHERE booked_on >= '2015-06-01'", dialect="dremio"
    )
    (warning,) = [
        i for i in result.issues if i.issue_type == OBQCIssueType.DATE_FORMAT_MISMATCH
    ]
    assert "TO_DATE(booked_on, 'DD.MM.YYYY')" in warning.suggestion
    assert "DATE_PARSE" not in warning.suggestion
    # No Dremio token for microseconds: no conversion rather than a wrong one.
    assert (
        to_date_sql(exp.column("t"), "%d.%m.%Y %H:%M:%S.%f", "trino", database="dremio")
        is None
    )
