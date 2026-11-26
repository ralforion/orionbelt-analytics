"""A blocked fan-trap comes with a sketch of the query to write instead.

The sketch is the shape of the fix -- one leg per fact, or a semi-join -- with
the query's own tables and join columns, and placeholders for what only the
question decides. These tests pin which shape each finding gets, that a
sketch cannot run as it stands, and that filled in, it passes the check that
blocked the original.
"""

import re

import pytest
import sqlglot
from sqlglot.errors import ParseError

from src.database_manager import ColumnInfo, TableInfo
from src.fan_trap_examples import (
    SHAPE_LEGS,
    SHAPE_SEMI_JOIN,
    Link,
    semi_join,
    union_all_legs,
)
from src.obqc_validator import SKETCH_NOTE, OBQCValidator
from src.ontology_generator import OntologyGenerator

VOCAB = "http://example.com/ontology/"


def _table(name: str, columns: str, *refs: tuple[str, str]) -> TableInfo:
    """*columns* as "name:type ..."; the first is the primary key."""
    cols = [c.split(":") for c in columns.split()]
    keys = {column for column, _ in refs}
    return TableInfo(
        name=name,
        schema="shop",
        columns=[
            ColumnInfo(
                name=n,
                data_type=t,
                is_nullable=True,
                is_primary_key=i == 0,
                is_foreign_key=n in keys,
            )
            for i, (n, t) in enumerate(cols)
        ],
        primary_keys=[cols[0][0]],
        foreign_keys=[
            {"column": c, "referenced_table": t, "referenced_column": "id"}
            for c, t in refs
        ],
    )


@pytest.fixture(scope="module")
def validator() -> OBQCValidator:
    tables = [
        _table("customers", "id:integer name:varchar"),
        _table("products", "id:integer category:varchar"),
        _table(
            "orders",
            "id:integer customer_id:integer total:decimal",
            ("customer_id", "customers"),
        ),
        _table(
            "order_items",
            "id:integer order_id:integer product_id:integer quantity:integer "
            "price:decimal",
            ("order_id", "orders"),
            ("product_id", "products"),
        ),
        _table(
            "payments",
            "id:integer order_id:integer amount:decimal",
            ("order_id", "orders"),
        ),
        _table(
            "returns",
            "id:integer order_item_id:integer refund:decimal",
            ("order_item_id", "order_items"),
        ),
    ]
    generator = OntologyGenerator(base_uri=VOCAB)
    generator.generate_from_schema(tables)
    v = OBQCValidator()
    v.load_ontology(generator.graph, VOCAB)
    return v


def _example(validator: OBQCValidator, sql: str) -> dict[str, str] | None:
    result = validator.validate(sql, dialect="duckdb")
    assert not result.is_valid, "expected a blocking fan-trap"
    examples = [f["example"] for f in result.fan_trap_findings if "example" in f]
    return examples[0] if examples else None


def _fill(sketch: str, values: dict[str, str]) -> str:
    """The sketch as a query: comment lines out, each placeholder replaced."""
    lines = [line for line in sketch.splitlines() if not line.startswith("--")]
    sql = "\n".join(lines)
    for placeholder, value in values.items():
        sql = sql.replace(f"<{placeholder}>", value)
    assert "<" not in sql, sql
    return sql


# --- Fact to fact: sales lines and their returns ---------------------------------

SOLD_AND_REFUNDED = """
SELECT p.category, SUM(oi.quantity * oi.price), SUM(r.refund)
FROM order_items oi
JOIN products p ON p.id = oi.product_id
LEFT JOIN returns r ON r.order_item_id = oi.id
GROUP BY p.category
"""


def test_both_facts_summed_get_one_leg_each(validator: OBQCValidator) -> None:
    example = _example(validator, SOLD_AND_REFUNDED)
    assert example is not None and example["shape"] == SHAPE_LEGS
    sql = example["sql"]
    assert "UNION ALL" in sql
    # The returns leg reaches the group keys through its sale, by the key the
    # query joins.
    assert (
        "FROM returns\n    JOIN order_items ON returns.order_item_id = order_items.id"
        in (sql)
    )
    # And the other reading is one line away.
    assert "WHERE EXISTS (SELECT 1 FROM returns WHERE returns.order_item_id" in sql


def test_the_suggestion_points_at_the_sketch(validator: OBQCValidator) -> None:
    result = validator.validate(SOLD_AND_REFUNDED, dialect="duckdb")
    (issue,) = [i for i in result.issues if i.issue_type.name == "FAN_TRAP_DETECTED"]
    assert issue.suggestion is not None
    assert issue.suggestion.startswith("Aggregate order_items and returns separately")
    assert issue.suggestion.endswith(SKETCH_NOTE)
    # And the sketch reaches the caller with the verdict.
    (finding,) = result.to_dict()["obqc_fan_trap"]["findings"]
    assert finding["example"]["shape"] == SHAPE_LEGS


def test_a_sketch_cannot_run_as_it_stands(validator: OBQCValidator) -> None:
    for sql in (
        SOLD_AND_REFUNDED,
        "SELECT SUM(o.total) FROM orders o JOIN order_items oi ON oi.order_id = o.id",
    ):
        example = _example(validator, sql)
        assert example is not None
        with pytest.raises(ParseError):
            sqlglot.parse_one(example["sql"], dialect="duckdb")


def test_the_legs_filled_in_pass_the_check(validator: OBQCValidator) -> None:
    example = _example(validator, SOLD_AND_REFUNDED)
    assert example is not None
    sql = _fill(
        example["sql"],
        {
            "group keys AS names": "products.category AS category",
            "group key names": "category",
            "order_items value": "order_items.quantity * order_items.price",
            "returns value": "returns.refund",
            "many-to-one joins to the group keys": (
                "JOIN products ON products.id = order_items.product_id"
            ),
            "the query's filters that apply to this fact": "",
        },
    )
    sqlglot.parse_one(sql, dialect="duckdb")
    result = validator.validate(sql, dialect="duckdb")
    assert result.is_valid, [i.message for i in result.issues]
    assert not result.fan_trap_risk


# --- A table joined only to filter ----------------------------------------------

ORDERS_WITH_BOOKS = """
SELECT o.customer_id, SUM(o.total)
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
JOIN products p ON p.id = oi.product_id
WHERE p.category = 'books'
GROUP BY o.customer_id
"""


def test_a_table_joined_to_filter_gets_a_semi_join(validator: OBQCValidator) -> None:
    example = _example(validator, ORDERS_WITH_BOOKS)
    assert example is not None and example["shape"] == SHAPE_SEMI_JOIN
    assert "WHERE order_items.order_id = orders.id" in example["sql"]
    assert "UNION ALL" not in example["sql"]
    result = validator.validate(ORDERS_WITH_BOOKS, dialect="duckdb")
    (issue,) = [i for i in result.issues if i.issue_type.name == "FAN_TRAP_DETECTED"]
    assert issue.suggestion is not None
    assert issue.suggestion.startswith("order_items is joined only to filter orders")


def test_the_semi_join_filled_in_passes_the_check(validator: OBQCValidator) -> None:
    example = _example(validator, ORDERS_WITH_BOOKS)
    assert example is not None
    sql = _fill(
        example["sql"],
        {
            "group keys": "orders.customer_id",
            "the query's aggregates of orders": "SUM(orders.total)",
            "many-to-one joins to the group keys": "",
            "its joins and filters, as in the query": (
                "JOIN products ON products.id = order_items.product_id"
            ),
            "AND the query's other filters": "",
        },
    ).replace(
        "WHERE order_items.order_id = orders.id",
        "WHERE order_items.order_id = orders.id AND products.category = 'books'",
    )
    result = validator.validate(sql, dialect="duckdb")
    assert result.is_valid, [i.message for i in result.issues]
    assert not result.fan_trap_risk


def test_grouping_by_a_lookup_of_the_measure_still_gets_a_semi_join(
    validator: OBQCValidator,
) -> None:
    sql = """
    SELECT c.name, SUM(o.total) FROM orders o
    JOIN customers c ON c.id = o.customer_id
    JOIN order_items oi ON oi.order_id = o.id
    GROUP BY 1
    """
    example = _example(validator, sql)
    assert example is not None and example["shape"] == SHAPE_SEMI_JOIN


@pytest.mark.parametrize(
    "sql",
    [
        # Grouped by the table that fans out: no shape keeps that question.
        "SELECT oi.product_id, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id GROUP BY oi.product_id",
        # Grouped through it.
        "SELECT p.category, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id GROUP BY p.category",
        # An outer join filters nothing, so EXISTS would change the answer.
        "SELECT o.customer_id, SUM(o.total) FROM orders o "
        "LEFT JOIN order_items oi ON oi.order_id = o.id GROUP BY o.customer_id",
        # Grouping the sketch cannot read for certain.
        "SELECT o.customer_id, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id GROUP BY ROLLUP (o.customer_id)",
    ],
)
def test_no_semi_join_where_it_would_change_the_question(
    validator: OBQCValidator, sql: str
) -> None:
    result = validator.validate(sql, dialect="duckdb")
    assert not result.is_valid
    assert all("example" not in f for f in result.fan_trap_findings)
    (issue,) = [i for i in result.issues if i.issue_type.name == "FAN_TRAP_DETECTED"]
    assert issue.suggestion is not None
    assert issue.suggestion.startswith("Pre-aggregate order_items")
    assert SKETCH_NOTE not in issue.suggestion


@pytest.mark.parametrize(
    "sql",
    [
        # Grouped by the child: an orders leg would have to join the items again.
        "SELECT oi.product_id, SUM(o.total), SUM(oi.quantity) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id GROUP BY oi.product_id",
        # Grouped by one sibling: the payments leg has no way to the product.
        "SELECT oi.product_id, SUM(oi.quantity), SUM(p.amount) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN payments p ON p.order_id = o.id GROUP BY oi.product_id",
        # Grouped through a third table on the many side of orders.
        "SELECT p.amount, SUM(o.total), SUM(oi.quantity) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN payments p ON p.order_id = o.id GROUP BY p.amount",
        # The child read by an aggregate no fan-out corrupts, or by none at all:
        # in an EXISTS it is out of reach, and COUNT(*) would count orders.
        "SELECT SUM(o.total), MAX(oi.price) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id",
        "SELECT SUM(o.total), COUNT(DISTINCT oi.product_id) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id",
        "SELECT SUM(o.total), COUNT(*) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id",
        "SELECT o.customer_id, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "GROUP BY o.customer_id ORDER BY MAX(oi.price)",
        "SELECT o.customer_id, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "GROUP BY o.customer_id HAVING MAX(oi.price) > 10",
        # A subquery's columns are its own; nothing says which table they read.
        "SELECT SUM(o.total), (SELECT MAX(r.refund) FROM returns r) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id",
    ],
)
def test_no_sketch_when_a_leg_or_the_exists_cannot_answer(
    validator: OBQCValidator, sql: str
) -> None:
    result = validator.validate(sql, dialect="duckdb")
    assert not result.is_valid
    assert all("example" not in f for f in result.fan_trap_findings), [
        f["example"]["shape"] for f in result.fan_trap_findings if "example" in f
    ]


def test_a_lookup_read_by_an_output_keeps_the_semi_join(
    validator: OBQCValidator,
) -> None:
    """A table the measure reaches many-to-one may be read anywhere."""
    sql = """
    SELECT c.name, SUM(o.total), MAX(c.id) FROM orders o
    JOIN customers c ON c.id = o.customer_id
    JOIN order_items oi ON oi.order_id = o.id
    GROUP BY c.name ORDER BY c.name
    """
    example = _example(validator, sql)
    assert example is not None and example["shape"] == SHAPE_SEMI_JOIN


def test_the_legs_filled_in_return_the_right_numbers(
    validator: OBQCValidator,
) -> None:
    """Passing OBQC is not enough: on data, the filled-in legs give the truth."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE products (id INTEGER, category VARCHAR);
        CREATE TABLE order_items (
            id INTEGER, order_id INTEGER, product_id INTEGER,
            quantity INTEGER, price DECIMAL(10, 2));
        CREATE TABLE returns (id INTEGER, order_item_id INTEGER, refund DECIMAL(10, 2));
        INSERT INTO products VALUES (1, 'books'), (2, 'garden');
        INSERT INTO order_items VALUES
            (1, 1, 1, 2, 30), (2, 1, 2, 1, 40), (3, 2, 1, 1, 50);
        -- Line 1 is returned in two parts.
        INSERT INTO returns VALUES (1, 1, 10), (2, 1, 20), (3, 3, 50);
        """)
    truth = sorted(con.execute("""
            SELECT p.category, SUM(oi.quantity * oi.price),
                (SELECT SUM(r.refund) FROM returns r
                 JOIN order_items i ON i.id = r.order_item_id
                 JOIN products q ON q.id = i.product_id
                 WHERE q.category = p.category)
            FROM order_items oi JOIN products p ON p.id = oi.product_id
            GROUP BY p.category
            """).fetchall())
    assert sorted(con.execute(SOLD_AND_REFUNDED).fetchall()) != truth
    example = _example(validator, SOLD_AND_REFUNDED)
    assert example is not None
    sql = _fill(
        example["sql"],
        {
            "group keys AS names": "products.category AS category",
            "group key names": "category",
            "order_items value": "order_items.quantity * order_items.price",
            "returns value": "returns.refund",
            "many-to-one joins to the group keys": (
                "JOIN products ON products.id = order_items.product_id"
            ),
            "the query's filters that apply to this fact": "",
        },
    )
    assert sorted(con.execute(sql).fetchall()) == truth


# --- Two facts sharing a parent: the chasm trap --------------------------------


def test_siblings_get_one_leg_each_through_their_parent(
    validator: OBQCValidator,
) -> None:
    sql = """
    SELECT o.customer_id, SUM(oi.quantity * oi.price), SUM(p.amount)
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.id
    JOIN payments p ON p.order_id = o.id
    GROUP BY o.customer_id
    """
    example = _example(validator, sql)
    assert example is not None and example["shape"] == SHAPE_LEGS
    assert "JOIN orders ON order_items.order_id = orders.id" in example["sql"]
    assert "JOIN orders ON payments.order_id = orders.id" in example["sql"]


def test_a_conditional_count_gets_no_sketch(validator: OBQCValidator) -> None:
    sql = (
        "SELECT SUM(CASE WHEN o.total > 100 THEN 1 ELSE 0 END) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id"
    )
    result = validator.validate(sql, dialect="duckdb")
    (finding,) = result.fan_trap_findings
    assert finding["kind"] == "conditional_row_count"
    assert "example" not in finding


# --- The sketches themselves ------------------------------------------------------


def test_names_are_written_as_the_dialect_reads_them() -> None:
    link = Link("Order Returns", "order lines", (("line id", "id"),))
    legs = union_all_legs(["order lines", "Order Returns"], {}, "postgres")
    assert 'FROM "order lines"' in legs
    assert '"Order Returns_value"' in legs
    sketch = semi_join("order lines", link, "postgres")
    assert 'WHERE "Order Returns"."line id" = "order lines".id' in sketch


def test_without_grouping_the_sketch_has_no_group_keys() -> None:
    link = Link("returns", "order_items", (("order_item_id", "id"),))
    for sketch in (
        union_all_legs(["order_items", "returns"], {"returns": link}, "duckdb", False),
        semi_join("order_items", link, "duckdb", grouped=False),
    ):
        assert "group keys" not in sketch
        assert "GROUP BY" not in sketch


def test_a_composite_key_joins_on_every_column() -> None:
    link = Link("returns", "order_items", (("order_id", "order_id"), ("line", "line")))
    assert link.condition("duckdb") == (
        "returns.order_id = order_items.order_id AND returns.line = order_items.line"
    )


def test_every_leg_has_a_column_for_every_fact() -> None:
    sketch = union_all_legs(["a", "b", "c"], {}, "duckdb")
    # The legs are indented inside the CTE; the outer SELECT is not.
    legs = re.findall(r"^    SELECT <group keys AS names>, (.*)$", sketch, re.MULTILINE)
    assert [leg.count(",") for leg in legs] == [2, 2, 2]
    assert legs[0] == "<a value> AS a_value, NULL AS b_value, NULL AS c_value"
    assert legs[2] == "NULL, NULL, <c value>"
