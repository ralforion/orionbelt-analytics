"""OBQC's "did you mean" for a table or column the query got wrong.

A wrong name was answered with the first ten tables or columns the ontology
happened to store, so the one the query meant was often not among them.
Now the names it most likely meant come first, by spelling, word forms,
abbreviation and the business names apply_semantic_names gives them.
"""

from collections.abc import Iterator
from typing import Any

import duckdb
import pytest

from src.database_manager import ColumnInfo, TableInfo
from src.name_suggestions import closest, did_you_mean, listing, similarity, sql_name
from src.obqc_validator import OBQCIssue, OBQCIssueType, OBQCValidator
from src.ontology_generator import OntologyGenerator

BASE = "http://example.com/ontology/"

# --- Matching names ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wrong", "meant"),
    [
        ("OrderItems", "order_items"),  # written differently
        ("customer", "customers"),  # another number
        ("category", "categories"),
        ("items_order", "order_items"),  # another order
        ("cust_id", "customer_id"),  # abbreviated
        ("qty", "quantity"),
        ("price", "unit_price"),  # a word left out
        ("customer_name", "name"),  # a word added
        ("custmer", "customers"),  # misspelt
        ("Customer Master", "customer_master"),
    ],
)
def test_a_name_meant_is_close(wrong: str, meant: str) -> None:
    assert similarity(wrong, meant) >= 0.75


@pytest.mark.parametrize(
    ("wrong", "other"),
    [
        ("id", "ip"),
        ("order_id", "id"),  # "id" alone says nothing
        ("client", "customers"),  # synonyms are not found
        ("xyz", "orders"),
    ],
)
def test_an_unrelated_name_is_not(wrong: str, other: str) -> None:
    assert similarity(wrong, other) < 0.75


def _names(*names: str) -> list[tuple[str, str, str | None]]:
    return [(n, n, None) for n in names]


def test_the_best_come_first_and_the_far_behind_are_left_out() -> None:
    # customers is the same word; customer_types only shares one.
    found = closest("customer", _names("customer_types", "customers", "orders"))
    assert [m.name for m in found] == ["customers"]
    # Equally close names tie by name, at most three.
    found = closest("ab_c", _names("ab_cd", "ab_ce", "ab_cf", "ab_cg"))
    assert [m.name for m in found] == ["ab_cd", "ab_ce", "ab_cf"]


def test_a_business_name_that_matches_better_is_shown() -> None:
    (match,) = closest("customer", [("t_0042", "t_0042", "Customer")])
    assert (match.name, match.label) == ("t_0042", "Customer")
    assert str(match) == 't_0042 ("Customer")'
    # The name itself abbreviates the words, which is as good a reason.
    (match,) = closest(
        "customer_master", [("cust_mstr", "cust_mstr", "Customer Master")]
    )
    assert str(match) == 'cust_mstr ("Customer Master")'
    (match,) = closest("customer_master", [("cust_mstr", "cust_mstr", "Customer")])
    assert match.label is None
    # When the name matches as well, the label is not worth showing.
    (match,) = closest("customers", [("customers", "customers", "Clients")])
    assert match.label is None


def test_the_wording() -> None:
    one, two, three = (closest(w, _names(w + "s"))[0] for w in ("ab", "cd", "ef"))
    assert did_you_mean([]) is None
    assert did_you_mean([one]) == "Did you mean abs?"
    assert did_you_mean([one, two]) == "Did you mean abs or cds?"
    assert did_you_mean([one, two, three]) == "Did you mean abs, cds or efs?"
    assert listing("tables", ["b", "a", "C"]) == "Available tables: a, b, C"
    # By name, wherever a quote puts it.
    assert listing("tables", ['"a b"', "b", "a"]) == 'Available tables: a, "a b", b'
    assert listing("tables", [f"t{i:02}" for i in range(12)], shown=3) == (
        "Available tables: t00, t01, t02 (12 in all)"
    )


@pytest.mark.parametrize(
    ("name", "dialect", "exact", "sql"),
    [
        ("total", "duckdb", True, "total"),
        ("order date", "duckdb", True, '"order date"'),
        ("order date", "mysql", True, "`order date`"),
        ("order", "duckdb", True, '"order"'),  # reserved
        # A case the dialect would fold away, when the case is the database's.
        ("OrderDate", "postgres", True, '"OrderDate"'),
        ("ORDER_DATE", "snowflake", True, "ORDER_DATE"),
        ("total", "snowflake", True, '"total"'),
        # A case that was lost is not asked for.
        ("total", "snowflake", False, "total"),
    ],
)
def test_a_name_is_written_so_the_dialect_reads_it_back(
    name: str, dialect: str, exact: bool, sql: str
) -> None:
    assert sql_name(name, dialect, exact) == sql


# --- In OBQC's errors -------------------------------------------------------------------


def _col(name: str, data_type: str = "INTEGER", pk: bool = False) -> ColumnInfo:
    return ColumnInfo(name, data_type, True, pk, name.endswith("_id"))


def _table(
    name: str, columns: list[str], fks: list[dict[str, Any]] | None = None
) -> TableInfo:
    return TableInfo(
        name, "main", [_col(c, pk=c == "id") for c in columns], ["id"], fks or []
    )


def _fk(column: str, table: str) -> dict[str, Any]:
    return {"column": column, "referenced_table": table, "referenced_column": "id"}


# Twenty tables, so the one a query means is easily not among the first ten.
FILLERS = [_table(f"aa_table_{i:02}", ["id", "value"]) for i in range(16)]
SHOP = [
    *FILLERS,
    _table("cust_mstr", ["id", "cust_nm", "region"]),
    _table(
        "orders",
        ["id", "cust_mstr_id", "order_date", "total", "order_status"],
        [_fk("cust_mstr_id", "cust_mstr")],
    ),
    _table("shipments", ["id", "order_id", "status"], [_fk("order_id", "orders")]),
    _table("order_items", ["id", "order_id", "quantity"], [_fk("order_id", "orders")]),
    _table("order lines", ["id", "order date", "amount"]),
    _table("staff", ["id", "full_name", "boss_id"], [_fk("boss_id", "staff")]),
]
VIEWS = {"open_orders": "SELECT id AS order_id, total FROM orders"}


@pytest.fixture(scope="module")
def validator() -> OBQCValidator:
    generator = OntologyGenerator(base_uri=BASE)
    generator.generate_from_schema(SHOP)
    generator.apply_semantic_names(
        {
            "classes": [{"original_name": "cust_mstr", "suggested_name": "Customer"}],
            "properties": [
                {
                    "original_name": "cust_nm",
                    "table_name": "cust_mstr",
                    "suggested_name": "Customer Name",
                }
            ],
        }
    )
    checker = OBQCValidator()
    checker.load_ontology(generator.graph, BASE)
    checker.load_views({"open_orders": {"order_id", "total"}})
    return checker


@pytest.fixture(scope="module")
def database() -> Iterator[duckdb.DuckDBPyConnection]:
    """The same tables, empty, to run a query with a suggestion applied."""
    con = duckdb.connect()
    for table in SHOP:
        columns = ", ".join(f'"{c.name}" INTEGER' for c in table.columns)
        con.execute(f'CREATE TABLE "{table.name}" ({columns})')
    for name, body in VIEWS.items():
        con.execute(f"CREATE VIEW {name} AS {body}")
    yield con
    con.close()


def _not_found(checker: OBQCValidator, sql: str) -> OBQCIssue:
    (issue,) = [
        i
        for i in checker.validate(sql, dialect="duckdb").issues
        if i.issue_type
        in (OBQCIssueType.TABLE_NOT_FOUND, OBQCIssueType.COLUMN_NOT_FOUND)
    ]
    return issue


def _runs_with_each(
    database: duckdb.DuckDBPyConnection, sql: str, wrong: str, meant: list[str]
) -> None:
    for suggestion in meant:
        database.execute(sql.replace(wrong, suggestion, 1)).fetchall()


@pytest.mark.parametrize(
    ("sql", "wrong", "meant"),
    [
        # Past the first ten tables the old listing showed.
        ("SELECT * FROM order", "order", ["orders"]),
        ("SELECT * FROM shipment", "shipment", ["shipments"]),
        ("SELECT * FROM OrderItems", "OrderItems", ["order_items"]),
        # By its business name.
        ("SELECT * FROM customer", "customer", ["cust_mstr"]),
        # A view is a table a query may mean.
        ("SELECT * FROM open_order", "open_order", ["open_orders"]),
        # A name only quoted can be.
        ("SELECT * FROM order_lines", "order_lines", ['"order lines"']),
    ],
)
def test_an_unknown_table_says_which_was_meant(
    validator: OBQCValidator,
    database: duckdb.DuckDBPyConnection,
    sql: str,
    wrong: str,
    meant: list[str],
) -> None:
    issue = _not_found(validator, sql)
    assert issue.did_you_mean == meant
    assert issue.suggestion is not None
    assert issue.suggestion.startswith(f"Did you mean {meant[0]}")
    _runs_with_each(database, sql, wrong, meant)


def test_a_business_name_is_shown_beside_the_table(validator: OBQCValidator) -> None:
    issue = _not_found(validator, "SELECT * FROM customer")
    assert issue.suggestion == 'Did you mean cust_mstr ("Customer")?'


@pytest.mark.parametrize(
    ("sql", "wrong", "meant"),
    [
        # Misspelt, in its own table, under the alias the query gave it: with
        # one, the table's own name is out of sight.
        ("SELECT o.totl FROM orders o", "o.totl", ["o.total"]),
        # Without an alias, under the name.
        ("SELECT orders.totl FROM orders", "orders.totl", ["orders.total"]),
        # Abbreviated.
        ("SELECT o.order_dt FROM orders o", "o.order_dt", ["o.order_date"]),
        # By its business name.
        ("SELECT c.customer_name FROM cust_mstr c", "c.customer_name", ["c.cust_nm"]),
        # The right column behind the wrong qualifier: shipments has status.
        # orders' own order_status is as likely, and named too.
        (
            "SELECT o.status FROM orders o JOIN shipments s ON s.order_id = o.id",
            "o.status",
            ["s.status", "o.order_status"],
        ),
        # Unqualified, among every table the reference could see.
        (
            "SELECT quantty FROM orders o JOIN order_items i ON i.order_id = o.id",
            "quantty",
            ["i.quantity"],
        ),
        # One table under two aliases: either is meant.
        (
            "SELECT ful_name FROM staff a JOIN staff b ON b.id = a.boss_id",
            "ful_name",
            ["a.full_name", "b.full_name"],
        ),
        # An outer alias, from inside a correlated subquery.
        (
            "SELECT o.id FROM orders o WHERE EXISTS "
            "(SELECT 1 FROM shipments s WHERE s.order_id = o.id AND o.totl > 0)",
            "o.totl",
            ["o.total"],
        ),
        # A name only quoted can be.
        ('SELECT l.order_dat FROM "order lines" l', "l.order_dat", ['l."order date"']),
        # A view's columns.
        ("SELECT v.totals FROM open_orders v", "v.totals", ["v.total"]),
    ],
)
def test_an_unknown_column_says_which_was_meant(
    validator: OBQCValidator,
    database: duckdb.DuckDBPyConnection,
    sql: str,
    wrong: str,
    meant: list[str],
) -> None:
    assert _not_found(validator, sql).did_you_mean == meant
    _runs_with_each(database, sql, wrong, meant)


def test_a_table_out_of_sight_is_not_offered(validator: OBQCValidator) -> None:
    # The subquery's table cannot answer for the outer reference, so it is
    # not offered for it either.
    issue = _not_found(
        validator,
        "SELECT quantty FROM orders WHERE id IN (SELECT order_id FROM order_items)",
    )
    assert issue.did_you_mean == []


def test_without_a_close_name_the_names_are_listed_in_order(
    validator: OBQCValidator,
) -> None:
    issue = _not_found(validator, "SELECT * FROM zzz")
    assert issue.did_you_mean == []
    assert issue.suggestion == (
        "Available tables: aa_table_00, aa_table_01, aa_table_02, aa_table_03, "
        "aa_table_04, aa_table_05, aa_table_06, aa_table_07, aa_table_08, "
        "aa_table_09 (22 in all)"
    )
    issue = _not_found(validator, "SELECT xyz FROM shipments")
    assert issue.suggestion == (
        "Available columns: shipments.id, shipments.order_id, shipments.status"
    )
    issue = _not_found(validator, "SELECT s.xyz FROM shipments s")
    assert issue.suggestion == "Available columns: id, order_id, status"


def test_the_result_carries_the_names_for_clients(validator: OBQCValidator) -> None:
    issues = validator.validate("SELECT * FROM order").to_dict()["obqc_issues"]
    (issue,) = [i for i in issues if i["type"] == "table_not_found"]
    assert issue["did_you_mean"] == ["orders"]
    # Only where there is something to say.
    issues = validator.validate("SELECT * FROM zzz").to_dict()["obqc_issues"]
    (issue,) = [i for i in issues if i["type"] == "table_not_found"]
    assert "did_you_mean" not in issue
