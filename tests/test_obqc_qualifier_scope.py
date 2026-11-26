"""A column's qualifier must name a table or alias the reference can see.

``SELECT products.category FROM orders`` names a table of the schema that the
query does not read. No database runs it, yet OBQC judged the column against
the table of that name, found it, and passed. Not every dotted name is a
table's column, though -- structs, lambda parameters, UNNEST elements -- so
each of those is let be.
"""

import pytest

from src.database_manager import ColumnInfo, TableInfo
from src.obqc_validator import OBQCIssueType, OBQCValidator
from src.ontology_generator import OntologyGenerator

VOCAB = "http://example.com/ontology/"


def _table(name: str, columns: str, *refs: tuple[str, str]) -> TableInfo:
    """*columns* as "name:type ..."; the first is the primary key."""
    cols = [c.split(":") for c in columns.split()]
    keys = {column for column, _ in refs}
    return TableInfo(
        name=name,
        schema="main",
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
        # address is a struct, as DuckDB, BigQuery and Snowflake have them.
        _table("customers", "id:integer name:varchar address:struct"),
        _table("products", "id:integer category:varchar"),
        _table(
            "orders",
            "id:integer customer_id:integer total:decimal",
            ("customer_id", "customers"),
        ),
        _table(
            "order_items",
            "id:integer order_id:integer product_id:integer quantity:integer",
            ("order_id", "orders"),
            ("product_id", "products"),
        ),
    ]
    generator = OntologyGenerator(base_uri=VOCAB)
    generator.generate_from_schema(tables)
    v = OBQCValidator()
    v.load_ontology(generator.graph, VOCAB)
    return v


def _stray(validator: OBQCValidator, sql: str, dialect: str = "duckdb") -> list:
    result = validator.validate(sql, dialect=dialect)
    return [
        i
        for i in result.issues
        if i.issue_type == OBQCIssueType.TABLE_NOT_FOUND
        and "names no table" in i.message
    ]


def test_a_table_the_query_does_not_read(validator: OBQCValidator) -> None:
    result = validator.validate("SELECT products.category FROM orders")
    assert not result.is_valid
    (issue,) = [i for i in result.issues if i.severity.value == "error"]
    assert issue.message == (
        "'products' in 'products.category' names no table or alias this part "
        "of the query reads: products is a table of the schema, but not in "
        "this SELECT's FROM or joins"
    )
    assert issue.suggestion == (
        "Join products, or qualify category by a table of the FROM"
    )


def test_the_name_in_sight_that_has_the_column_is_offered(
    validator: OBQCValidator,
) -> None:
    """A sketch's legs filled in with the table where the CTE's name belongs."""
    sql = (
        "WITH legs AS (SELECT p.category, oi.quantity AS q FROM order_items oi "
        "JOIN products p ON p.id = oi.product_id) "
        "SELECT products.category, SUM(q) FROM legs GROUP BY products.category"
    )
    issue, *more = _stray(validator, sql)
    assert not more
    assert issue.did_you_mean == ["legs.category"]
    assert issue.suggestion == "Did you mean legs.category?"


@pytest.mark.parametrize(
    "sql",
    [
        # An alias no relation has.
        "SELECT zz.total FROM orders",
        # A CTE body cannot see the query that declares it.
        "WITH x AS (SELECT o.total FROM orders) SELECT SUM(total) FROM x",
        # An alias of a subquery, used outside it.
        "SELECT o.total FROM orders WHERE EXISTS (SELECT 1 FROM order_items o)",
        # In ORDER BY as much as in the select list.
        "SELECT o.id FROM orders o ORDER BY p.category",
    ],
)
def test_a_qualifier_out_of_sight_is_an_error(
    validator: OBQCValidator, sql: str
) -> None:
    assert _stray(validator, sql)
    assert not validator.validate(sql, dialect="duckdb").is_valid


def test_reported_once_not_also_as_a_missing_column(validator: OBQCValidator) -> None:
    result = validator.validate("SELECT products.bogus FROM orders")
    errors = [i for i in result.issues if i.severity.value == "error"]
    assert len(errors) == 1 and "names no table" in errors[0].message


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("duckdb", "SELECT o.total FROM orders o"),
        ("duckdb", "SELECT orders.total FROM orders"),
        (
            "duckdb",
            "SELECT o.id FROM orders o WHERE EXISTS "
            "(SELECT 1 FROM order_items oi WHERE oi.order_id = o.id)",
        ),
        ("duckdb", "WITH x AS (SELECT total FROM orders) SELECT x.total FROM x"),
        ("duckdb", "SELECT d.t FROM (SELECT total AS t FROM orders) d"),
        ("duckdb", "SELECT d.e FROM (SELECT total FROM orders) AS d(e)"),
        ("duckdb", "SELECT u.x FROM orders, UNNEST([1, 2]) AS u(x)"),
        ("duckdb", "SELECT v.a FROM (VALUES (1)) AS v(a)"),
        ("duckdb", "SELECT l.t FROM orders o, LATERAL (SELECT o.total AS t) l"),
        ("duckdb", "SELECT main.orders.total FROM main.orders"),
        ("duckdb", "SELECT c.column_name FROM information_schema.columns c"),
        (
            "duckdb",
            "SELECT t.total FROM (orders AS t JOIN order_items AS i ON i.order_id = t.id)",
        ),
        # A struct column, bare or through its table's alias.
        ("duckdb", "SELECT address.city FROM customers"),
        ("duckdb", "SELECT c.address.city FROM customers c"),
        # A lambda's parameter.
        ("duckdb", "SELECT list_transform([{'a': 1}], x -> x.a) FROM orders"),
        ("duckdb", "SELECT g.generate_series FROM generate_series(1, 3) g"),
        ("clickhouse", "SELECT a.x FROM orders ARRAY JOIN arr AS a"),
        (
            "snowflake",
            "SELECT f.value FROM orders o, LATERAL FLATTEN(input => o.total) f",
        ),
        (
            "snowflake",
            "SELECT f.value FROM orders o, TABLE(FLATTEN(input => o.total)) f",
        ),
        # An UNNEST element is a value; x.f reads a field of it.
        ("bigquery", "SELECT x.f FROM orders o, UNNEST(o.arr) AS x"),
        ("postgresql", "SELECT r.a FROM orders o, jsonb_to_recordset(o.j) AS r(a int)"),
        ("spark", "SELECT e.col FROM orders o LATERAL VIEW explode(o.arr) e AS col"),
        (
            "duckdb",
            "SELECT p.total FROM orders PIVOT (SUM(total) FOR customer_id IN (1, 2)) AS p",
        ),
    ],
)
def test_what_a_qualifier_may_also_name_is_let_be(
    validator: OBQCValidator, dialect: str, sql: str
) -> None:
    assert _stray(validator, sql, dialect) == []


def test_a_suggestion_is_quoted_as_the_query_wrote_the_names(
    validator: OBQCValidator,
) -> None:
    """A quoted mixed-case CTE is offered quoted, or PostgreSQL folds it away."""
    sql = (
        'WITH "Legs" AS (SELECT p.category AS "Category" FROM products p) '
        'SELECT products."Category" FROM "Legs"'
    )
    (issue,) = _stray(validator, sql, "postgresql")
    assert issue.did_you_mean == ['"Legs"."Category"']
    assert 'products."Category"' in issue.message


# --- What a reference sees: scopes around a CTE, and only those ---------------------


def test_a_cte_in_a_subquery_sees_what_encloses_it(validator: OBQCValidator) -> None:
    """A correlated CTE: its body reads the outer o, which DuckDB runs."""
    sql = (
        "SELECT o.id, (WITH x AS (SELECT o.total AS total) "
        "SELECT SUM(total) FROM x) FROM orders o"
    )
    result = validator.validate(sql, dialect="duckdb")
    assert _stray(validator, sql) == []
    assert result.is_valid, [i.message for i in result.issues]


def test_a_cte_does_not_see_the_from_of_the_query_declaring_it(
    validator: OBQCValidator,
) -> None:
    sql = (
        "SELECT (WITH x AS (SELECT s.total AS total) SELECT SUM(total) "
        "FROM x, orders s) FROM customers c"
    )
    (issue,) = _stray(validator, sql)
    assert issue.message.startswith("'s' in 's.total'")


@pytest.mark.parametrize(
    "sql",
    [
        # A derived table without a name still has outputs, a struct among them.
        "SELECT address.city FROM (SELECT address FROM customers)",
        "SELECT address.city FROM (SELECT address FROM customers) d",
    ],
)
def test_a_struct_of_a_derived_table_is_let_be(
    validator: OBQCValidator, sql: str
) -> None:
    assert _stray(validator, sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        # An aliased CTE reference has its CTE's outputs, none of them zz.
        "WITH x AS (SELECT total FROM orders) SELECT zz.total FROM x y",
        "WITH x AS (SELECT total FROM orders) SELECT zz.total FROM x",
        # Another subquery's alias list is out of the reference's sight...
        "SELECT zz.total FROM orders WHERE EXISTS "
        "(SELECT 1 FROM (SELECT id FROM orders) d(zz))",
        # ...and so is a relation of the same name elsewhere.
        "SELECT zz.total FROM (SELECT total FROM orders) d "
        "WHERE EXISTS (SELECT 1 FROM (SELECT 1 AS zz) d)",
    ],
)
def test_only_what_the_reference_sees_excuses_it(
    validator: OBQCValidator, sql: str
) -> None:
    (issue,) = _stray(validator, sql)
    assert issue.message.startswith("'zz' in 'zz.total'")
