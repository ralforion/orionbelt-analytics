"""A fan-out inside a CTE or derived table is judged where its rows are added up.

``WITH x AS (SELECT o.total FROM orders o JOIN order_items oi ...) SELECT
SUM(total) FROM x`` sums each order once per item. The CTE aggregates nothing
and the outer query joins nothing, so judged SELECT by SELECT it passed. OBQC
now also judges the query with its CTEs and derived tables written into their
readers -- only those that keep their rows, so the patterns that undo a
fan-out (pre-aggregation, DISTINCT, ROW_NUMBER() = 1) still pass.
"""

import pytest
import sqlglot

from src.database_manager import ColumnInfo, TableInfo
from src.derived_inlining import MAX_VARIANTS, inlined, worth_inlining
from src.obqc_validator import OBQCSeverity, OBQCValidator
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


def _validator(upper: bool = False) -> OBQCValidator:
    def name(n: str) -> str:
        return n.upper() if upper else n

    def table(n: str, columns: str, *refs: tuple[str, str]) -> TableInfo:
        cols = " ".join(
            f"{name(c.split(':')[0])}:{c.split(':')[1]}" for c in columns.split()
        )
        return _table(name(n), cols, *((name(c), name(t)) for c, t in refs))

    tables = [
        table("customers", "id:integer name:varchar"),
        table(
            "orders",
            "id:integer customer_id:integer total:decimal",
            ("customer_id", "customers"),
        ),
        table(
            "order_items",
            "id:integer order_id:integer quantity:integer price:decimal",
            ("order_id", "orders"),
        ),
        table(
            "payments",
            "id:integer order_id:integer amount:decimal",
            ("order_id", "orders"),
        ),
    ]
    generator = OntologyGenerator(base_uri=VOCAB)
    generator.generate_from_schema(tables)
    v = OBQCValidator()
    v.load_ontology(generator.graph, VOCAB)
    return v


@pytest.fixture(scope="module")
def validator() -> OBQCValidator:
    return _validator()


FAN_OUT = "FROM orders o JOIN order_items oi ON oi.order_id = o.id"

WRONG = {
    "a CTE summed outside": (
        f"WITH x AS (SELECT o.total {FAN_OUT}) SELECT SUM(total) FROM x"
    ),
    "a derived table in FROM": (
        f"SELECT SUM(t) FROM (SELECT o.total AS t {FAN_OUT}) d"
    ),
    "a derived table joined": (
        "SELECT c.name, SUM(x.total) FROM customers c "
        f"JOIN (SELECT o.customer_id, o.total {FAN_OUT}) x "
        "ON x.customer_id = c.id GROUP BY c.name"
    ),
    "a CTE read twice": (
        f"WITH x AS (SELECT o.total {FAN_OUT}) "
        "SELECT (SELECT SUM(total) FROM x), (SELECT COUNT(*) FROM x)"
    ),
    "chained CTEs": (
        f"WITH x AS (SELECT o.total {FAN_OUT}), y AS (SELECT total FROM x) "
        "SELECT SUM(total) FROM y"
    ),
    "a UNION ALL leg that fans out": (
        "WITH legs AS ("
        f"SELECT o.customer_id, o.total AS t, NULL AS q {FAN_OUT} "
        "UNION ALL SELECT o.customer_id, NULL, oi.quantity "
        "FROM order_items oi JOIN orders o ON o.id = oi.order_id) "
        "SELECT customer_id, SUM(t), SUM(q) FROM legs GROUP BY customer_id"
    ),
    "unqualified columns inside": (
        "WITH x AS (SELECT total, quantity FROM orders "
        "JOIN order_items ON order_id = orders.id) SELECT SUM(total) FROM x"
    ),
    "a star inside": (f"WITH x AS (SELECT * {FAN_OUT}) SELECT SUM(total) FROM x"),
}

RIGHT = {
    "legs that each read their own fact": (
        "WITH legs AS (SELECT o.customer_id, oi.quantity * oi.price AS ordered, "
        "NULL AS paid FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "UNION ALL SELECT o.customer_id, NULL, p.amount FROM payments p "
        "JOIN orders o ON o.id = p.order_id) "
        "SELECT customer_id, SUM(ordered), SUM(paid) FROM legs GROUP BY customer_id"
    ),
    "grouped at the item's grain": (
        f"WITH x AS (SELECT o.total, oi.id AS item {FAN_OUT}) "
        "SELECT item, SUM(total) FROM x GROUP BY item"
    ),
    "one row per order by ROW_NUMBER": (
        "SELECT SUM(total) FROM (SELECT o.total, ROW_NUMBER() OVER "
        f"(PARTITION BY o.id ORDER BY oi.id) AS rn {FAN_OUT}) d WHERE rn = 1"
    ),
    "a DISTINCT CTE": (
        f"WITH x AS (SELECT DISTINCT o.id, o.total {FAN_OUT}) "
        "SELECT SUM(total) FROM x"
    ),
    "pre-aggregated": (
        "WITH i AS (SELECT order_id, COUNT(*) AS n FROM order_items "
        "GROUP BY order_id) SELECT SUM(o.total), SUM(i.n) FROM orders o "
        "JOIN i ON i.order_id = o.id"
    ),
    "the many side summed": (
        f"WITH x AS (SELECT o.customer_id, oi.quantity {FAN_OUT}) "
        "SELECT customer_id, SUM(quantity) FROM x GROUP BY 1"
    ),
    "lookups only": (
        "WITH x AS (SELECT c.name, o.total FROM orders o "
        "JOIN customers c ON c.id = o.customer_id) "
        "SELECT name, SUM(total) FROM x GROUP BY name"
    ),
    "a semi-join inside": (
        "WITH x AS (SELECT o.total FROM orders o WHERE EXISTS "
        "(SELECT 1 FROM order_items oi WHERE oi.order_id = o.id)) "
        "SELECT SUM(total) FROM x"
    ),
    "a CTE named like the table it reads": (
        "WITH orders AS (SELECT * FROM orders WHERE total > 0) "
        "SELECT SUM(o.total) FROM orders o"
    ),
    "an aggregate repeats cannot change": (
        f"WITH x AS (SELECT o.total {FAN_OUT}) SELECT MAX(total) FROM x"
    ),
    "a recursive CTE": (
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r "
        "WHERE n < 3) SELECT SUM(n) FROM r"
    ),
}


@pytest.mark.parametrize("sql", WRONG.values(), ids=WRONG.keys())
def test_a_fan_out_inside_is_blocked_where_it_is_summed(
    validator: OBQCValidator, sql: str
) -> None:
    result = validator.validate(sql, dialect="duckdb")
    assert not result.is_valid and result.fan_trap_blocking
    (issue,) = [
        i
        for i in result.issues
        if i.issue_type.name == "FAN_TRAP_DETECTED" and i.severity == OBQCSeverity.ERROR
    ]
    assert issue.message.endswith(OBQCValidator.INLINED_NOTE)
    assert issue.location == "Inside a CTE or derived table"
    (finding,) = result.to_dict()["obqc_fan_trap"]["findings"]
    assert finding["kind"] == "measure_across_fan_out"
    assert finding["measure_table"] == "orders"
    assert finding["fan_out_table"] == "order_items"


@pytest.mark.parametrize("sql", RIGHT.values(), ids=RIGHT.keys())
def test_what_keeps_each_row_once_still_passes(
    validator: OBQCValidator, sql: str
) -> None:
    result = validator.validate(sql, dialect="duckdb")
    assert result.is_valid, [i.message for i in result.issues]
    assert not result.fan_trap_risk


def test_a_fan_out_the_query_shows_is_reported_once(validator: OBQCValidator) -> None:
    """Written in, the outer query's own fan-out is the same finding."""
    sql = (
        "WITH o AS (SELECT * FROM orders) SELECT SUM(o.total) FROM o "
        "JOIN order_items oi ON oi.order_id = o.id"
    )
    result = validator.validate(sql, dialect="duckdb")
    assert not result.is_valid
    assert len(result.fan_trap_findings) == 1


def test_allow_fan_out_downgrades_it_like_any_other(validator: OBQCValidator) -> None:
    result = validator.validate(WRONG["a CTE summed outside"], allow_fan_out=True)
    assert result.is_valid
    assert result.fan_trap_risk and result.fan_trap_overridden
    assert not result.fan_trap_blocking


def test_upper_case_names_are_followed_too() -> None:
    """Snowflake folds unquoted names to upper case, as its tables are named."""
    result = _validator(upper=True).validate(
        WRONG["a CTE summed outside"], dialect="snowflake"
    )
    assert not result.is_valid and result.fan_trap_blocking


def test_an_outer_join_to_a_derived_table_is_left_as_written(
    validator: OBQCValidator,
) -> None:
    """Not written in: the outer join's rows cannot be moved, so not judged."""
    sql = (
        "SELECT SUM(x.total) FROM customers c "
        f"LEFT JOIN (SELECT o.customer_id, o.total {FAN_OUT}) x "
        "ON x.customer_id = c.id"
    )
    assert validator.validate(sql, dialect="duckdb").is_valid


# --- The rewrite itself -----------------------------------------------------------


def test_only_queries_that_aggregate_over_a_relation_are_rewritten() -> None:
    assert not worth_inlining(sqlglot.parse_one("SELECT SUM(total) FROM orders"))
    assert not worth_inlining(
        sqlglot.parse_one("WITH x AS (SELECT total FROM orders) SELECT total FROM x")
    )
    assert worth_inlining(sqlglot.parse_one(WRONG["a CTE summed outside"]))


def test_the_rewrite_leaves_the_query_alone() -> None:
    parsed = sqlglot.parse_one(WRONG["a CTE read twice"], dialect="duckdb")
    before = parsed.sql()
    (merged,) = inlined(parsed, "duckdb")
    assert parsed.sql() == before
    assert merged.find(sqlglot.exp.With) is None


def test_each_union_branch_is_its_own_variant() -> None:
    parsed = sqlglot.parse_one(WRONG["a UNION ALL leg that fans out"], dialect="duckdb")
    variants = inlined(parsed, "duckdb")
    # One per branch, the union gone from each; what lies outside the union
    # is in every one of them.
    assert len(variants) == 2
    assert all("UNION" not in v.sql() for v in variants)


def test_the_variants_are_capped() -> None:
    legs = " UNION ALL ".join(f"SELECT {i} AS v" for i in range(MAX_VARIANTS * 2))
    parsed = sqlglot.parse_one(f"SELECT SUM(v) FROM ({legs}) u", dialect="duckdb")
    assert len(inlined(parsed, "duckdb")) == MAX_VARIANTS


def test_a_query_it_cannot_qualify_judges_nothing_more() -> None:
    parsed = sqlglot.parse_one(
        "WITH x AS (SELECT a FROM t) SELECT SUM(x.a) FROM x JOIN x USING (a, a)",
        dialect="duckdb",
    )
    # Whatever sqlglot makes of it, no exception reaches the caller.
    assert isinstance(inlined(parsed, "duckdb"), list)


# --- Review findings: evidence, scoping, limits, nesting, cost ---------------------


@pytest.fixture(scope="module")
def evidence() -> OBQCValidator:
    """orders.customer_id is only guessed; orders.referrer_id is declared."""

    def col(name: str, data_type: str = "integer", fk: bool = False) -> ColumnInfo:
        return ColumnInfo(
            name=name,
            data_type=data_type,
            is_nullable=True,
            is_primary_key=name == "id",
            is_foreign_key=fk,
        )

    tables = [
        TableInfo(
            name="customers",
            schema="main",
            columns=[col("id"), col("credit", "decimal")],
            primary_keys=["id"],
            foreign_keys=[],
        ),
        TableInfo(
            name="orders",
            schema="main",
            columns=[
                col("id"),
                col("customer_id"),
                col("referrer_id", fk=True),
                col("total", "decimal"),
            ],
            primary_keys=["id"],
            foreign_keys=[
                {
                    "column": "referrer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                }
            ],
        ),
    ]
    generator = OntologyGenerator(base_uri=VOCAB)
    generator.generate_from_schema(tables)
    v = OBQCValidator()
    v.load_ontology(generator.graph, VOCAB)
    return v


def _severities(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        i.severity.value
        for i in result.issues
        if i.issue_type.name == "FAN_TRAP_DETECTED" and i.severity.value != "info"
    ]


def test_firmer_evidence_inside_replaces_a_guess_outside(
    evidence: OBQCValidator,
) -> None:
    """The same tables, a guessed key outside and a declared one inside."""
    guessed = (
        "SELECT SUM(c.credit) FROM customers c JOIN orders o ON o.customer_id = c.id"
    )
    alone = evidence.validate(guessed, dialect="duckdb")
    assert alone.is_valid and _severities(alone) == ["warning"]
    both = evidence.validate(
        "SELECT SUM(c.credit) + (SELECT SUM(credit) FROM (SELECT c2.credit "
        "FROM customers c2 JOIN orders o2 ON o2.referrer_id = c2.id) d) "
        "FROM customers c JOIN orders o ON o.customer_id = c.id",
        dialect="duckdb",
    )
    assert not both.is_valid and both.fan_trap_blocking
    assert _severities(both) == ["error"]
    (finding,) = both.fan_trap_findings
    assert finding["assumed"] is False
    assert finding["relationship"] == "orders.referrer_id -> customers.id"


def test_a_join_moved_out_of_a_cte_keeps_the_key_it_compares(
    evidence: OBQCValidator,
) -> None:
    """Wrapping orders in a CTE must not trade the guessed key for the declared."""
    result = evidence.validate(
        "WITH o2 AS (SELECT * FROM orders) SELECT SUM(c.credit) FROM customers c "
        "JOIN o2 ON o2.customer_id = c.id",
        dialect="duckdb",
    )
    assert result.is_valid and _severities(result) == ["warning"]
    (finding,) = result.fan_trap_findings
    assert finding["relationship"] == "orders.customer_id -> customers.id"


def test_the_joins_keep_their_conditions_when_reordered() -> None:
    parsed = sqlglot.parse_one(WRONG["a derived table joined"], dialect="duckdb")
    (merged,) = inlined(parsed, "duckdb")
    sql = merged.sql(dialect="duckdb")
    assert "JOIN customers AS c ON o.customer_id = c.id" in sql
    assert "WHERE" not in sql


def test_a_cte_sees_only_the_ctes_declared_before_it(
    validator: OBQCValidator,
) -> None:
    """x reads the table orders; the CTE named orders after it fans out."""
    sql = (
        f"WITH x AS (SELECT total FROM orders), orders AS (SELECT o.total {FAN_OUT}) "
        "SELECT SUM(total) FROM x"
    )
    result = validator.validate(sql, dialect="duckdb")
    assert result.is_valid and not result.fan_trap_risk


def test_an_inner_with_hides_an_outer_name(validator: OBQCValidator) -> None:
    sql = (
        f"WITH x AS (SELECT o.total {FAN_OUT}) "
        "SELECT SUM(total) FROM (WITH x AS (SELECT total FROM orders) "
        "SELECT total FROM x) d"
    )
    assert validator.validate(sql, dialect="duckdb").is_valid


def test_a_union_that_limits_its_rows_is_left_as_written(
    validator: OBQCValidator,
) -> None:
    sql = (
        f"SELECT SUM(total) FROM (SELECT o.total {FAN_OUT} "
        "UNION ALL SELECT total FROM orders LIMIT 1) x"
    )
    assert validator.validate(sql, dialect="duckdb").is_valid


def test_a_union_read_by_a_union_is_written_in_with_it(
    validator: OBQCValidator,
) -> None:
    sql = (
        f"WITH a AS (SELECT o.total AS t {FAN_OUT} UNION ALL SELECT total FROM orders), "
        "b AS (SELECT t FROM a UNION ALL SELECT total FROM orders) "
        "SELECT SUM(t) FROM b"
    )
    result = validator.validate(sql, dialect="duckdb")
    assert not result.is_valid and result.fan_trap_blocking


def test_the_cap_holds_before_the_copies() -> None:
    """128 branches: at most MAX_VARIANTS copies of the query are made."""
    from unittest import mock

    from sqlglot import exp

    from src import derived_inlining

    legs = " UNION ALL ".join(f"SELECT {i} AS v" for i in range(128))
    parsed = sqlglot.parse_one(f"SELECT SUM(v) FROM ({legs}) u", dialect="duckdb")
    copies = {"n": 0}
    original = exp.Expression.copy

    def counting(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(self, exp.Select) and self.parent is None:
            copies["n"] += 1
        return original(self, *args, **kwargs)

    with mock.patch.object(exp.Expression, "copy", counting):
        variants = derived_inlining.inlined(parsed, "duckdb")
    assert len(variants) == MAX_VARIANTS
    # The parse is copied once to qualify it; then one copy per variant.
    assert copies["n"] <= MAX_VARIANTS + 1


@pytest.mark.parametrize(
    "sql",
    [
        f"WITH x AS (SELECT o.total {FAN_OUT}) SELECT SUM(t) FROM x AS y(t)",
        f"WITH x AS (SELECT o.id, o.total {FAN_OUT}) SELECT SUM(u) FROM x AS y(i, u)",
        f"WITH x AS (SELECT o.total {FAN_OUT} UNION ALL SELECT total FROM orders) "
        "SELECT SUM(t) FROM x AS y(t)",
    ],
)
def test_a_reference_renaming_the_outputs_keeps_them(
    validator: OBQCValidator, sql: str
) -> None:
    """FROM x AS y(t): y.t is the body's first output, written in or not."""
    result = validator.validate(sql, dialect="duckdb")
    assert not result.is_valid and result.fan_trap_blocking


def test_a_column_list_the_body_cannot_take_judges_as_written() -> None:
    for sql in (
        # More names than outputs.
        f"WITH x AS (SELECT o.total {FAN_OUT}) SELECT SUM(t) FROM x AS y(t, u)",
        # A star whose columns are not known without a schema.
        f"WITH x AS (SELECT * {FAN_OUT}) SELECT SUM(t) FROM x AS y(t)",
    ):
        assert inlined(sqlglot.parse_one(sql, dialect="duckdb"), "duckdb") == []


def _chain(stages: int) -> str:
    """Each stage joins its predecessor to itself: twice the copies a stage."""
    ctes = [f"s0 AS (SELECT o.total AS t {FAN_OUT})"] + [
        f"s{i} AS (SELECT a.t FROM s{i - 1} a JOIN s{i - 1} b ON a.t = b.t)"
        for i in range(1, stages + 1)
    ]
    return f"WITH {', '.join(ctes)} SELECT SUM(t) FROM s{stages}"


def test_writing_ctes_in_has_a_budget(validator: OBQCValidator) -> None:
    """Ten stages would copy the first body 1,024 times: judged as written."""
    assert inlined(sqlglot.parse_one(_chain(10), dialect="duckdb"), "duckdb") == []
    # Within the budget, the same shape is written in and judged.
    result = validator.validate(_chain(2), dialect="duckdb")
    assert not result.is_valid and result.fan_trap_blocking
