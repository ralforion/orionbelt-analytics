"""Personal data: columns whose names say they hold it, and results that return it.

Tagged from names only, recorded in the ontology as oba:personalData, shown by
discover_schema and get_table_details, and warned of by OBQC when a query's
result includes one, followed back through derived tables, CTEs, set
operations and stars.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import src.main as main_module
import src.server_state as state_module
from src.database_manager import ColumnInfo, TableInfo
from src.handlers import connection as connection_handler
from src.handlers.schema import (
    MAX_LISTED_PERSONAL_TABLES,
    _columns,
    _personal_data_section,
)
from src.main import mcp
from src.obqc_validator import OBQCIssueType, OBQCValidator
from src.ontology_generator import OntologyGenerator
from src.personal_data import DETECT_VARIABLE, category, tagged
from src.server_state import ServerState

# --- Names --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        # Personal anywhere, under any spelling.
        ("customers", "email", "email"),
        ("customers", "EmailAddress", "email"),
        ("customers", "e_mail", "email"),
        ("CUSTOMER", "FIRST_NAME", "name"),
        ("people", "dob", "birth_date"),
        ("users", "IPAddress", "network_id"),
        ("users", "password_hash", "credential"),
        ("payments", "card_number", "payment_card"),
        ("guests", "phone_number_2", "phone"),
        # After words saying whose or which it is.
        ("orders", "customer_email", "email"),
        ("orders", "billing_address", "address"),
        ("orders", "shipping_address_line_2", "address"),
        ("orders", "customer_name", "name"),
        ("contacts", "primary_email", "email"),
        # Personal only of a person: in a table of people, or naming one.
        ("customers", "name", "name"),
        ("DIM_CUSTOMER", "NAME", "name"),
        ("hotel_guests", "name", "name"),
        ("customers", "address", "address"),
        ("customer_addresses", "street", "address"),
        ("employees", "salary", "financial"),
        ("patients", "age", "personal_attribute"),
        ("users", "gender", "personal_attribute"),
        ("users", "lat", "location"),
        ("users", "mobile", "phone"),
        ("customers", "tax_id", "government_id"),
        ("customers", "account_number", "bank_account"),
    ],
)
def test_a_name_says_what_personal_data_it_holds(
    table: str, column: str, expected: str
) -> None:
    assert category(table, column) == expected


@pytest.mark.parametrize(
    ("table", "column"),
    [
        # About a field, not the field.
        ("users", "email_verified"),
        ("users", "has_email"),
        ("users", "email_count"),
        ("users", "password_changed_at"),
        ("users", "last_password_change"),
        ("patients", "age_group"),
        # Something else's.
        ("customer_segments", "name"),
        ("user_roles", "name"),
        ("products", "product_name"),
        ("companies", "legal_name"),
        ("stores", "address"),
        ("stores", "business_address"),
        ("job_postings", "salary"),
        ("inventory", "age"),
        ("products", "gender"),
        ("stores", "lat"),
        ("devices", "mobile"),
        ("suppliers", "tax_id"),
        ("accounts", "account_number"),
        ("map_markers", "pin"),
        # Keys and plain business columns.
        ("customers", "customer_id"),
        ("orders", "order_date"),
        ("users", "username"),
    ],
)
def test_a_name_that_says_nothing_personal_is_not_tagged(
    table: str, column: str
) -> None:
    assert category(table, column) is None


def test_a_boolean_is_a_flag_not_the_data() -> None:
    assert category("users", "email", "BOOLEAN") is None
    assert category("users", "email", "bit") is None
    assert category("users", "email", "VARCHAR(255)") == "email"


def test_tagging_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    columns = [("email", "VARCHAR"), ("id", "INTEGER")]
    assert tagged("customers", columns) == {"email": "email"}
    monkeypatch.setenv(DETECT_VARIABLE, "false")
    assert tagged("customers", columns) == {}


# --- The ontology -------------------------------------------------------------------


def _table(name: str, columns: list[str], pk: str = "id") -> TableInfo:
    return TableInfo(
        name,
        "main",
        [
            ColumnInfo(
                c, "INTEGER" if c.endswith("id") else "VARCHAR", True, c == pk, False
            )
            for c in columns
        ],
        [pk],
        [],
    )


SHOP = [
    _table("customers", ["id", "name", "email", "segment", "created_at"]),
    _table("orders", ["id", "customer_id", "total", "billing_address"]),
]


def _ontology(tables: list[TableInfo]) -> OntologyGenerator:
    generator = OntologyGenerator(base_uri="http://example.com/ontology/")
    generator.generate_from_schema(tables)
    return generator


def _recorded(generator: OntologyGenerator) -> dict[str, str]:
    graph = generator.graph
    return {
        f"{graph.value(s, generator.oba_ns.tableName)}."
        f"{graph.value(s, generator.oba_ns.columnName)}": str(o)
        for s, o in graph.subject_objects(generator.oba_ns.personalData)
    }


def test_the_ontology_records_each_columns_personal_data() -> None:
    assert _recorded(_ontology(SHOP)) == {
        "customers.name": "name",
        "customers.email": "email",
        "orders.billing_address": "address",
    }


def test_an_ontology_with_personal_data_conforms_to_the_shapes() -> None:
    from src.shacl_validator import shacl_available, validate_ontology

    if not shacl_available():
        pytest.skip("pyshacl not installed")
    ttl = OntologyGenerator(
        base_uri="http://example.com/ontology/"
    ).generate_from_schema(SHOP)
    report = validate_ontology(ttl)
    assert report["conforms"], report.get("report")


# --- OBQC ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shop() -> OBQCValidator:
    validator = OBQCValidator()
    validator.load_ontology(_ontology(SHOP).graph, "http://example.com/ontology/")
    return validator


def _returned(validator: OBQCValidator, sql: str) -> list[str]:
    issues = [
        i
        for i in validator.validate(sql, dialect="duckdb").issues
        if i.issue_type == OBQCIssueType.PERSONAL_DATA
    ]
    assert len(issues) <= 1
    return issues[0].related_entities if issues else []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT name, email FROM customers", ["customers.email", "customers.name"]),
        ("SELECT * FROM customers", ["customers.email", "customers.name"]),
        ("SELECT c.* FROM customers c", ["customers.email", "customers.name"]),
        ("SELECT * EXCLUDE (email) FROM customers", ["customers.name"]),
        # Through a derived table, a CTE, a set operation, a scalar subquery.
        (
            "SELECT x.e FROM (SELECT lower(email) AS e FROM customers) x",
            ["customers.email"],
        ),
        (
            "WITH c AS (SELECT * FROM customers) SELECT c.name FROM c",
            ["customers.name"],
        ),
        (
            "SELECT id FROM customers UNION ALL SELECT email FROM customers",
            ["customers.email"],
        ),
        (
            "SELECT o.total, (SELECT c.email FROM customers c "
            "WHERE c.id = o.customer_id) AS e FROM orders o",
            ["customers.email"],
        ),
        # A whole row, and an earlier output used again.
        ("SELECT to_json(c) FROM customers c", ["customers.email", "customers.name"]),
        ("SELECT lower(email) AS e, upper(e) AS f FROM customers", ["customers.email"]),
        (
            "SELECT o.billing_address, c.segment FROM orders o "
            "JOIN customers c ON c.id = o.customer_id",
            ["orders.billing_address"],
        ),
        # The branch an IF returns, not the one it tests.
        ("SELECT IF(email IS NULL, 'x', name) AS v FROM customers", ["customers.name"]),
        (
            "SELECT to_json(c.*) AS j FROM customers c",
            ["customers.email", "customers.name"],
        ),
        # Correlated: the subquery returns the outer table's column.
        (
            "SELECT (SELECT c.email FROM orders o WHERE o.customer_id = c.id "
            "LIMIT 1) AS e FROM customers c",
            ["customers.email"],
        ),
        # A CTE read twice, and one that reads itself.
        (
            "WITH c AS (SELECT id, email FROM customers) "
            "SELECT a.email, b.id FROM c a JOIN c b ON a.id = b.id",
            ["customers.email"],
        ),
        (
            "WITH RECURSIVE r AS (SELECT email, 1 AS n FROM customers UNION ALL "
            "SELECT email, n + 1 FROM r WHERE n < 3) SELECT r.email FROM r",
            ["customers.email"],
        ),
        (
            "SELECT id FROM customers INTERSECT SELECT email FROM customers",
            ["customers.email"],
        ),
        # A star over a table the ontology lacks hides the positions: every
        # branch's columns, under the first branch's names or another's.
        (
            "SELECT * FROM unknown_t UNION ALL SELECT id, email FROM customers",
            ["customers.email"],
        ),
        (
            "WITH u AS (SELECT * FROM unknown_t UNION ALL "
            "SELECT id, email FROM customers) SELECT u.email FROM u",
            ["customers.email"],
        ),
    ],
)
def test_a_result_with_personal_data_is_warned(
    shop: OBQCValidator, sql: str, expected: list[str]
) -> None:
    assert _returned(shop, sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        # Counted, compared, tested or filtered on: no value is returned.
        "SELECT COUNT(DISTINCT email) FROM customers",
        "SELECT approx_count_distinct(email) AS n FROM customers",
        "SELECT segment, COUNT(*) FROM customers "
        "WHERE email LIKE '%@x.com' GROUP BY segment",
        "SELECT CASE WHEN email IS NULL THEN 'none' ELSE 'some' END AS has FROM customers",
        "SELECT row_number() OVER (PARTITION BY email) AS n FROM customers",
        "SELECT o.id FROM orders o WHERE o.customer_id IN "
        "(SELECT id FROM customers WHERE email = 'a')",
        "SELECT COUNT(*) FROM (SELECT email FROM customers) t",
        "SELECT * EXCLUDE (name, email) FROM customers",
        "SELECT segment FROM customers",
        "SELECT r.range FROM range(3) r",
    ],
)
def test_a_result_without_personal_data_is_not_warned(
    shop: OBQCValidator, sql: str
) -> None:
    assert _returned(shop, sql) == []


def test_the_warning_names_each_column_and_its_kind(shop: OBQCValidator) -> None:
    (warning,) = [
        i
        for i in shop.validate("SELECT name, email FROM customers").issues
        if i.issue_type == OBQCIssueType.PERSONAL_DATA
    ]
    assert warning.severity.value == "warning"
    assert warning.message.endswith("customers.email (email), customers.name (name)")
    assert "COUNT(DISTINCT" in (warning.suggestion or "")


def test_a_query_that_cannot_be_scoped_is_judged_by_names(
    shop: OBQCValidator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_: Any) -> Any:
        raise RuntimeError("no scope")

    monkeypatch.setattr("src.obqc_validator.result_columns", fail)
    assert _returned(shop, "SELECT email FROM customers") == ["customers.email"]
    assert _returned(shop, "SELECT * FROM customers") == [
        "customers.email",
        "customers.name",
    ]
    assert _returned(shop, "SELECT segment FROM customers") == []


def test_no_warning_when_switched_off(
    shop: OBQCValidator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DETECT_VARIABLE, "false")
    assert _returned(shop, "SELECT email FROM customers") == []


def test_an_ontology_without_the_tags_warns_of_none() -> None:
    generator = _ontology(SHOP)
    for triple in list(
        generator.graph.triples((None, generator.oba_ns.personalData, None))
    ):
        generator.graph.remove(triple)
    validator = OBQCValidator()
    validator.load_ontology(generator.graph, "http://example.com/ontology/")
    assert _returned(validator, "SELECT email FROM customers") == []


# --- Discovery ----------------------------------------------------------------------


def test_discovery_lists_the_personal_data() -> None:
    section = _personal_data_section(SHOP)
    assert section["personal_data"] == {
        "customers": {"name": "name", "email": "email"},
        "orders": {"billing_address": "address"},
    }
    assert "personal_data_note" in section
    assert _personal_data_section([_table("orders", ["id", "total"])]) == {}


def test_discovery_lists_a_few_tables_and_counts_the_rest() -> None:
    tables = [_table(f"users_{i:03d}", ["id", "email"]) for i in range(40)]
    section = _personal_data_section(tables)
    assert len(section["personal_data"]) == MAX_LISTED_PERSONAL_TABLES
    assert section["more_personal_data_tables"] == 40 - MAX_LISTED_PERSONAL_TABLES


def test_table_details_mark_each_column() -> None:
    columns = {c["name"]: c.get("personal_data") for c in _columns(SHOP[0])}
    assert columns == {
        "id": None,
        "name": "name",
        "email": "email",
        "segment": None,
        "created_at": None,
    }


# --- Through the tools --------------------------------------------------------------


@pytest.fixture
def crm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    import duckdb

    path = tmp_path / "crm.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR, "
        "email VARCHAR, segment VARCHAR)"
    )
    con.execute(
        "INSERT INTO customers VALUES (1, 'Ada', 'ada@example.com', 'A'), "
        "(2, 'Bo', 'bo@example.com', 'B')"
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
    monkeypatch.delenv("OBA_DATABASES", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.delenv(DETECT_VARIABLE, raising=False)
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(path))
    yield tmp_path
    fresh.cleanup()


def _data(result: Any) -> Any:
    return result.data if not isinstance(result.data, str) else json.loads(result.data)


async def test_through_the_tools(crm: Path) -> None:
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
        text = (
            connected.data
            if isinstance(connected.data, str)
            else json.dumps(connected.data)
        )
        match = re.search(r"(ob_[a-z0-9]{6})", text)
        assert match
        on = {"connection": match.group(1)}

        found = _data(
            await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        )
        assert found["personal_data"] == {
            "customers": {"name": "name", "email": "email"}
        }
        details = _data(
            await client.call_tool(
                "get_table_details", {**on, "table_name": "customers"}
            )
        )
        assert {c["name"]: c.get("personal_data") for c in details["columns"]}[
            "email"
        ] == "email"

        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})

        async def run(sql: str) -> Any:
            return _data(
                await client.call_tool(
                    "execute_sql_query",
                    {**on, "checklist_completed": True, "sql_query": sql},
                )
            )

        listed = await run("SELECT name, email FROM main.customers")
        assert listed["success"] is True, listed  # a warning, not a refusal
        assert any(
            "personal data" in w and "customers.email (email)" in w
            for w in listed["warnings"]
        )
        counted = await run("SELECT COUNT(DISTINCT email) AS n FROM main.customers")
        assert counted["success"] is True
        assert not any("personal data" in w for w in counted.get("warnings", []))
