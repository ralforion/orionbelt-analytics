"""Bridge tables, and totals grouped so that a fan-out repeats nothing.

A bridge links tables many to many (student_courses between students and
courses). Joining through one repeats rows of both sides, so OBQC blocks a
total over either side -- rightly for the total, wrongly for credits summed
per student, which counts each course once per student. The rule now asks
whether the grouping keeps each measured row once per group, from declared
keys and join equalities, and the fan-trap suggestion names the key to group
by.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from fastmcp import Client

import src.main as main_module
import src.server_state as state_module
from src.bridge_tables import MAX_LISTED_BRIDGES, find_bridges, listed_bridges
from src.database_manager import ColumnInfo, TableInfo
from src.group_grain import Keyed, kept_once
from src.handlers import connection as connection_handler
from src.main import mcp
from src.obqc_validator import OBQCIssueType, OBQCValidator
from src.ontology_generator import OntologyGenerator
from src.server_state import ServerState


def _col(name: str, data_type: str = "INTEGER", pk: bool = False) -> ColumnInfo:
    return ColumnInfo(name, data_type, True, pk, name.endswith("_id"))


def _fk(column: str, table: str, constraint: str | None = None) -> dict[str, Any]:
    key: dict[str, Any] = {
        "column": column,
        "referenced_table": table,
        "referenced_column": "id",
    }
    if constraint:
        key["constraint_name"] = constraint
    return key


def _table(
    name: str,
    columns: list[str],
    pk: list[str] | None = None,
    fks: list[dict[str, Any]] | None = None,
    types: dict[str, str] | None = None,
) -> TableInfo:
    keys = ["id"] if pk is None else pk
    return TableInfo(
        name,
        "main",
        [_col(c, (types or {}).get(c, "INTEGER"), c in keys) for c in columns],
        keys,
        fks or [],
    )


STUDENTS = _table(
    "students", ["id", "full_name", "tuition"], types={"full_name": "VARCHAR"}
)
COURSES = _table("courses", ["id", "title", "credits"], types={"title": "VARCHAR"})
ENROLLED = _table(
    "student_courses",
    ["student_id", "course_id"],
    pk=["student_id", "course_id"],
    fks=[_fk("student_id", "students"), _fk("course_id", "courses")],
)
CLUBS = _table("clubs", ["id", "fee"])
# A bridge without a key on the pair: a student may be listed twice.
MEMBERS = _table(
    "student_clubs",
    ["student_id", "club_id"],
    pk=[],
    fks=[_fk("student_id", "students"), _fk("club_id", "clubs")],
)
ORDERS = _table("orders", ["id", "total"])
ITEMS = _table(
    "order_items", ["id", "order_id", "qty"], fks=[_fk("order_id", "orders")]
)
SCHOOL = [STUDENTS, COURSES, ENROLLED, CLUBS, MEMBERS, ORDERS, ITEMS]

# --- Which tables are bridges --------------------------------------------------------


def _bridges(tables: list[TableInfo], **kwargs: Any) -> dict[str, Any]:
    return {
        b.table: (b.connects, b.pairs_unique, b.inferred)
        for b in find_bridges(tables, **kwargs)
    }


def test_a_table_of_keys_only_is_a_bridge() -> None:
    assert _bridges(SCHOOL) == {
        "student_courses": (("students", "courses"), True, False),
        "student_clubs": (("students", "clubs"), False, False),
    }


def test_a_surrogate_key_and_bookkeeping_columns_do_not_count() -> None:
    table = _table(
        "tags_posts",
        ["id", "tag_id", "post_id", "created_at", "etl_batch_id"],
        fks=[_fk("tag_id", "tags"), _fk("post_id", "posts")],
    )
    # The surrogate key says nothing of the pair: it may repeat.
    assert _bridges([table]) == {"tags_posts": (("tags", "posts"), False, False)}


def test_a_table_linking_one_table_to_itself_is_a_bridge() -> None:
    table = _table(
        "also_bought",
        ["product_id", "also_product_id"],
        pk=["product_id", "also_product_id"],
        fks=[_fk("product_id", "products"), _fk("also_product_id", "products")],
    )
    assert _bridges([table]) == {"also_bought": (("products",), True, False)}


@pytest.mark.parametrize(
    "table",
    [
        # Holds a grade of its own: an entity, not a link.
        _table(
            "enrollments",
            ["student_id", "course_id", "grade"],
            pk=["student_id", "course_id"],
            fks=[_fk("student_id", "students"), _fk("course_id", "courses")],
        ),
        # A fact with a measure.
        _table(
            "sales",
            ["id", "customer_id", "product_id", "amount"],
            fks=[_fk("customer_id", "customers"), _fk("product_id", "products")],
        ),
        # One key only.
        _table("order_items", ["id", "order_id"], fks=[_fk("order_id", "orders")]),
        # One composite key is one key.
        _table(
            "shipment_lines",
            ["order_id", "line_no"],
            pk=["order_id", "line_no"],
            fks=[
                _fk("order_id", "order_lines", "fk_line"),
                _fk("line_no", "order_lines", "fk_line"),
            ],
        ),
    ],
)
def test_other_tables_are_no_bridges(table: TableInfo) -> None:
    assert _bridges([table]) == {}


def test_keys_inferred_from_names_make_a_bridge_too() -> None:
    table = _table(
        "student_courses", ["student_id", "course_id"], pk=["student_id", "course_id"]
    )
    assert _bridges([table]) == {}
    inferred = [
        ("student_courses", "student_id", "students"),
        ("student_courses", "course_id", "courses"),
    ]
    assert _bridges([table], inferred=inferred) == {
        "student_courses": (("students", "courses"), True, True)
    }


def test_discovery_lists_a_few_bridges_and_counts_the_rest() -> None:
    section = listed_bridges(find_bridges(SCHOOL))
    assert section["bridge_tables"] == [
        {
            "table": "student_courses",
            "connects": ["students", "courses"],
            "pairs_unique": True,
        },
        {
            "table": "student_clubs",
            "connects": ["students", "clubs"],
            "pairs_unique": False,
        },
    ]
    assert "bridge_tables_note" in section
    assert listed_bridges([]) == {}
    many = [
        _table(
            f"links_{i:02d}",
            ["a_id", "b_id"],
            pk=[],
            fks=[_fk("a_id", "a"), _fk("b_id", "b")],
        )
        for i in range(30)
    ]
    section = listed_bridges(find_bridges(many))
    assert len(section["bridge_tables"]) == MAX_LISTED_BRIDGES
    assert section["more_bridge_tables"] == 30 - MAX_LISTED_BRIDGES


# --- The ontology --------------------------------------------------------------------


def _ontology(tables: list[TableInfo]) -> OntologyGenerator:
    generator = OntologyGenerator(base_uri="http://example.com/ontology/")
    generator.generate_from_schema(tables)
    return generator


def test_the_ontology_records_what_each_bridge_links() -> None:
    generator = _ontology(SCHOOL)
    graph = generator.graph
    linked = {
        str(graph.value(s, generator.oba_ns.tableName)): sorted(
            str(o) for o in graph.objects(s, generator.oba_ns.bridgeBetween)
        )
        for s in set(graph.subjects(generator.oba_ns.bridgeBetween, None))
    }
    assert linked == {
        "student_courses": ["courses", "students"],
        "student_clubs": ["clubs", "students"],
    }


def test_a_bridge_of_inferred_keys_is_recorded() -> None:
    tables = [
        STUDENTS,
        COURSES,
        _table(
            "student_courses",
            ["student_id", "course_id"],
            pk=["student_id", "course_id"],
        ),
    ]
    generator = _ontology(tables)
    assert {
        str(o) for o in generator.graph.objects(None, generator.oba_ns.bridgeBetween)
    } == {"students", "courses"}


def test_an_ontology_with_bridges_conforms_to_the_shapes() -> None:
    from src.shacl_validator import shacl_available, validate_ontology

    if not shacl_available():
        pytest.skip("pyshacl not installed")
    ttl = OntologyGenerator(
        base_uri="http://example.com/ontology/"
    ).generate_from_schema(SCHOOL)
    report = validate_ontology(ttl)
    assert report["conforms"], report.get("report")


# --- OBQC: totals per key of the other side ------------------------------------------


@pytest.fixture(scope="module")
def school() -> OBQCValidator:
    validator = OBQCValidator()
    validator.load_ontology(_ontology(SCHOOL).graph, "http://example.com/ontology/")
    return validator


THROUGH = (
    "FROM students s JOIN student_courses sc ON sc.student_id = s.id "
    "JOIN courses c ON c.id = sc.course_id "
)


def _fan_traps(validator: OBQCValidator, sql: str) -> list[Any]:
    return [
        i
        for i in validator.validate(sql, dialect="duckdb").issues
        if i.issue_type == OBQCIssueType.FAN_TRAP_DETECTED
    ]


@pytest.mark.parametrize(
    "sql",
    [
        # Each course once per student, each student once per course.
        "SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY s.id",
        "SELECT s.id, s.full_name, SUM(c.credits) "
        + THROUGH
        + "GROUP BY s.id, s.full_name",
        "SELECT c.id, SUM(s.tuition) " + THROUGH + "GROUP BY c.id",
        "SELECT sc.course_id, SUM(s.tuition) " + THROUGH + "GROUP BY sc.course_id",
        # One course, fixed by WHERE.
        "SELECT c.title, SUM(s.tuition) " + THROUGH + "WHERE c.id = 3 GROUP BY c.title",
        # GROUP BY ALL groups by the outputs: plain columns and stars.
        "SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY ALL",
        "SELECT s.id AS sid, (s.full_name), SUM(c.credits) " + THROUGH + "GROUP BY ALL",
        "SELECT s.*, SUM(c.credits) " + THROUGH + "GROUP BY ALL",
        "SELECT oi.id, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id GROUP BY ALL",
        # Grouped at the child's grain: one order per item.
        "SELECT oi.id, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id GROUP BY oi.id",
        # An order without items is one row, still one per group.
        "SELECT oi.id, SUM(o.total) FROM orders o "
        "LEFT JOIN order_items oi ON oi.order_id = o.id GROUP BY oi.id",
    ],
)
def test_a_total_grouped_so_that_nothing_repeats_passes(
    school: OBQCValidator, sql: str
) -> None:
    assert _fan_traps(school, sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        # Totals over one side: each row once per row linked to it.
        "SELECT SUM(s.tuition) " + THROUGH,
        "SELECT s.id, SUM(s.tuition) " + THROUGH + "GROUP BY s.id",
        # Grouped by what two rows may share.
        "SELECT s.full_name, SUM(c.credits) " + THROUGH + "GROUP BY s.full_name",
        "SELECT c.title, SUM(s.tuition) " + THROUGH + "GROUP BY c.title",
        # Groups that add up across others.
        "SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY ROLLUP (s.id)",
        "SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY ALL ROLLUP (s.id)",
        # GROUP BY ALL over outputs that fix no key: what two rows may share,
        # an expression, a star that leaves the key out.
        "SELECT s.full_name, SUM(c.credits) " + THROUGH + "GROUP BY ALL",
        "SELECT s.id + 0 AS sid, SUM(c.credits) " + THROUGH + "GROUP BY ALL",
        "SELECT s.* EXCLUDE (id), SUM(c.credits) " + THROUGH + "GROUP BY ALL",
        "SELECT s.* REPLACE (0 AS id), SUM(c.credits) " + THROUGH + "GROUP BY ALL",
        # A window sums across the groups GROUP BY ALL makes, too.
        "SELECT s.id, SUM(c.credits), SUM(SUM(c.credits)) OVER () "
        + THROUGH
        + "GROUP BY ALL",
        # A second fan-out the grouping does not fix.
        "SELECT s.id, SUM(c.credits) "
        + THROUGH
        + "JOIN student_clubs k ON k.student_id = s.id GROUP BY s.id",
        # No key on the pair: a membership may be listed twice.
        "SELECT s.id, SUM(cl.fee) FROM students s "
        "JOIN student_clubs k ON k.student_id = s.id "
        "JOIN clubs cl ON cl.id = k.club_id GROUP BY s.id",
        # An OR alternative holds for some rows, not all.
        "SELECT s.id, SUM(c.credits) FROM students s "
        "JOIN student_courses sc ON sc.student_id = s.id "
        "JOIN courses c ON c.id = sc.course_id OR c.id = 0 GROUP BY s.id",
        # USING folds columns without saying whose.
        "SELECT s.id, SUM(c.credits) FROM students s "
        "JOIN student_courses sc USING (student_id) "
        "JOIN courses c ON c.id = sc.course_id GROUP BY s.id",
        "SELECT o.id, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id GROUP BY o.id",
        # A window sums across the groups: one order, two items, twice.
        "SELECT oi.id, o.total, SUM(o.total) OVER () AS t FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id GROUP BY oi.id, o.total",
        # The rows an ANTI join keeps fail its condition: oi.id = 1 fixes nothing.
        "SELECT oi.id, SUM(o.total) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "ANTI JOIN orders x ON x.id = oi.order_id AND oi.id = 1 GROUP BY oi.id",
        # Renamed columns: this oi.id is the quantity, no key at all.
        "SELECT oi.id, SUM(o.total) FROM orders o "
        "JOIN order_items AS oi(qty, order_id, id) ON oi.order_id = o.id "
        "GROUP BY oi.id",
    ],
)
def test_a_total_that_repeats_rows_is_still_blocked(
    school: OBQCValidator, sql: str
) -> None:
    assert _fan_traps(school, sql)
    assert not school.validate(sql, dialect="duckdb").is_valid


def test_the_suggestion_names_the_key_to_group_by(school: OBQCValidator) -> None:
    (finding,) = _fan_traps(
        school, "SELECT c.title, SUM(s.tuition) " + THROUGH + "GROUP BY c.title"
    )
    assert "student_courses links students to courses many to many" in (
        finding.suggestion or ""
    )
    assert "GROUP BY student_courses.course_id" in (finding.suggestion or "")
    # Without a key on the pair, grouping cannot fix it: aggregate first.
    (finding,) = _fan_traps(
        school,
        "SELECT cl.id, SUM(s.tuition) FROM students s "
        "JOIN student_clubs k ON k.student_id = s.id "
        "JOIN clubs cl ON cl.id = k.club_id GROUP BY cl.id",
    )
    assert "No grouping keeps each" in (finding.suggestion or "")
    assert "GROUP BY" not in (finding.suggestion or "")


def test_a_bridge_of_three_keys_has_no_pair_key_unless_its_key_is_a_pair() -> None:
    def three(primary: list[str]) -> TableInfo:
        return _table(
            "enrolments",
            ["student_id", "course_id", "club_id"],
            pk=primary,
            fks=[
                _fk("student_id", "students"),
                _fk("course_id", "courses"),
                _fk("club_id", "clubs"),
            ],
        )

    def suggestion(table: TableInfo, by: str) -> str:
        validator = OBQCValidator()
        validator.load_ontology(
            _ontology([STUDENTS, COURSES, CLUBS, table]).graph,
            "http://example.com/ontology/",
        )
        (finding,) = _fan_traps(
            validator,
            "SELECT cl.id, SUM(s.tuition) FROM students s "
            f"JOIN enrolments e ON e.student_id = s.id JOIN {by} GROUP BY cl.id",
        )
        return finding.suggestion or ""

    # Keyed by all three: a student comes once per course in a club.
    every = three(["student_id", "course_id", "club_id"])
    assert _bridges([every])["enrolments"][1] is False
    assert "No grouping keeps each" in suggestion(
        every, "clubs cl ON cl.id = e.club_id"
    )
    # Keyed by the student and the course: per course, not per club.
    pair = three(["student_id", "course_id"])
    assert "GROUP BY enrolments.course_id" in suggestion(
        pair, "clubs cl ON cl.id = e.club_id"
    )


def test_the_advice_follows_the_key_the_query_joins_by() -> None:
    # Two keys to students: the student's own, in the pair key, and the
    # mentor's, beside it. Joined by mentor_id, two students sharing a mentor
    # count the mentor twice per course: grouping by course is no help.
    mentored = _table(
        "student_courses",
        ["student_id", "course_id", "mentor_id"],
        pk=["student_id", "course_id"],
        fks=[
            _fk("student_id", "students"),
            _fk("course_id", "courses"),
            _fk("mentor_id", "students"),
        ],
    )
    validator = OBQCValidator()
    validator.load_ontology(
        _ontology([STUDENTS, COURSES, mentored]).graph, "http://example.com/ontology/"
    )
    sql = (
        "SELECT c.id, SUM(s.tuition) FROM students s "
        "JOIN student_courses sc ON sc.{} = s.id "
        "JOIN courses c ON c.id = sc.course_id GROUP BY c.id"
    )
    (finding,) = _fan_traps(validator, sql.format("mentor_id"))
    assert "No grouping keeps each" in (finding.suggestion or "")
    assert "GROUP BY" not in (finding.suggestion or "")
    result = validator.validate(sql.format("mentor_id"), dialect="duckdb")
    assert all("_via" not in f for f in result.fan_trap_findings)
    # By student_id the grouping keeps each student once per course.
    assert _fan_traps(validator, sql.format("student_id")) == []


def test_the_advice_needs_a_key_the_join_compares_in_full(
    school: OBQCValidator,
) -> None:
    # OR s.id = 0: student 0 joins every enrolment, so no grouping helps.
    (finding,) = _fan_traps(
        school,
        "SELECT c.id, SUM(s.tuition) FROM students s "
        "JOIN student_courses sc ON sc.student_id = s.id OR s.id = 0 "
        "JOIN courses c ON c.id = sc.course_id GROUP BY c.id",
    )
    assert "No grouping keeps each" in (finding.suggestion or "")
    assert "GROUP BY" not in (finding.suggestion or "")


def test_the_advice_names_the_bridges_own_column() -> None:
    # Two keys to courses, the pair key by course_id. Joined by the alternate,
    # courses.id would count a student once per shared alternate; the
    # bridge's course_id is right however courses is joined.
    alternate = _table(
        "enrollments",
        ["student_id", "course_id", "alternate_course_id"],
        pk=["student_id", "course_id"],
        fks=[
            _fk("student_id", "students"),
            _fk("course_id", "courses"),
            _fk("alternate_course_id", "courses"),
        ],
    )
    validator = OBQCValidator()
    validator.load_ontology(
        _ontology([STUDENTS, COURSES, alternate]).graph, "http://example.com/ontology/"
    )
    joined = (
        "FROM students s JOIN enrollments e ON e.student_id = s.id "
        "JOIN courses c ON c.id = e.alternate_course_id "
    )
    (finding,) = _fan_traps(
        validator, "SELECT c.id, SUM(s.tuition) " + joined + "GROUP BY c.id"
    )
    assert "GROUP BY enrollments.course_id" in (finding.suggestion or "")
    # Followed, the advice passes.
    assert (
        _fan_traps(
            validator,
            "SELECT e.course_id, SUM(s.tuition) " + joined + "GROUP BY e.course_id",
        )
        == []
    )


def test_a_key_longer_than_the_pair_is_no_pair_key() -> None:
    # (student_id, course_id, version): a pair once per version. Grouping by
    # the course does not help, so the suggestion does not offer it.
    versioned = _table(
        "student_courses",
        ["student_id", "course_id", "version"],
        pk=["student_id", "course_id", "version"],
        fks=[_fk("student_id", "students"), _fk("course_id", "courses")],
    )
    assert _bridges([versioned]) == {
        "student_courses": (("students", "courses"), False, False)
    }
    validator = OBQCValidator()
    validator.load_ontology(
        _ontology([STUDENTS, COURSES, versioned]).graph, "http://example.com/ontology/"
    )
    sql = "SELECT c.id, SUM(s.tuition) " + THROUGH + "GROUP BY c.id"
    (finding,) = _fan_traps(validator, sql)
    assert "No grouping keeps each" in (finding.suggestion or "")
    assert "GROUP BY" not in (finding.suggestion or "")


# --- The rule on its own -------------------------------------------------------------


def _catalog(node: Any) -> Keyed | None:
    table = {t.name: t for t in SCHOOL}.get(node.name)
    if table is None:
        return None
    return Keyed(
        tuple(k.lower() for k in table.primary_keys),
        tuple(c.name.lower() for c in table.columns),
    )


def _kept(sql: str, dialect: str = "duckdb") -> set[str]:
    select = sqlglot.parse_one(sql, dialect=dialect)
    assert isinstance(select, sqlglot.exp.Select)
    return kept_once(select, _catalog)


def test_an_outer_join_carries_what_is_known_toward_its_nulls_only() -> None:
    # The equality written either way round.
    assert "orders" in _kept(
        "SELECT oi.id, SUM(o.total) FROM orders o "
        "LEFT JOIN order_items oi ON o.id = oi.order_id GROUP BY oi.id"
    )
    # RIGHT JOIN keeps the bridge: from its course_id to the course.
    right = (
        "SELECT sc.course_id, SUM(s.tuition) FROM courses c "
        "RIGHT JOIN student_courses sc ON sc.course_id = c.id "
        "JOIN students s ON s.id = sc.student_id GROUP BY {}"
    )
    assert "students" in _kept(right.format("sc.course_id"))
    # Grouped by the course, the enrolments of courses that do not exist meet
    # in one NULL group, where a student may come twice.
    assert "students" not in _kept(
        right.format("c.id").replace("sc.course_id,", "c.id,")
    )


def test_a_window_reading_no_measured_table_changes_nothing() -> None:
    assert "courses" in _kept(
        "SELECT s.id, SUM(c.credits), ROW_NUMBER() OVER (ORDER BY s.id) "
        + THROUGH
        + "GROUP BY s.id"
    )
    assert "courses" not in _kept(
        "SELECT s.id, SUM(c.credits), SUM(SUM(c.credits)) OVER () "
        + THROUGH
        + "GROUP BY s.id"
    )


def test_a_grouping_key_may_be_a_position_or_an_output_name() -> None:
    assert "courses" in _kept("SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY 1")
    assert "courses" in _kept(
        "SELECT s.id AS sid, SUM(c.credits) " + THROUGH + "GROUP BY sid"
    )
    # Behind a star a position's place is open: here 2 is the orders'
    # total, not oi.id.
    for star in ("o.*", "(o.*)", "((o.*))"):
        assert "orders" not in _kept(
            f"SELECT {star}, oi.id AS x, SUM(o.total) FROM orders o "
            "JOIN order_items oi ON oi.order_id = o.id GROUP BY o.id, o.total, 2"
        ), star
    # An expression fixes no column.
    assert "courses" not in _kept(
        "SELECT s.id + 0, SUM(c.credits) " + THROUGH + "GROUP BY s.id + 0"
    )


@pytest.mark.parametrize(
    "sql",
    [
        # A FULL join carries nothing either way.
        "SELECT s.id, SUM(c.credits) FROM students s "
        "FULL JOIN student_courses sc ON sc.student_id = s.id "
        "FULL JOIN courses c ON c.id = sc.course_id GROUP BY s.id",
        # A CTE, a derived table, a table the catalog lacks.
        "WITH x AS (SELECT * FROM courses) SELECT s.id, SUM(x.credits) "
        "FROM students s JOIN student_courses sc ON sc.student_id = s.id "
        "JOIN x ON x.id = sc.course_id GROUP BY s.id",
        "SELECT s.id, SUM(d.credits) FROM students s "
        "JOIN student_courses sc ON sc.student_id = s.id "
        "JOIN (SELECT * FROM courses) d ON d.id = sc.course_id GROUP BY s.id",
        "SELECT s.id, SUM(u.n) FROM students s JOIN unknown_t u ON u.id = s.id "
        "GROUP BY s.id",
        # No grouping at all.
        "SELECT SUM(c.credits) " + THROUGH,
        # Joins that match rows by other rules than every row's equality.
        "SELECT s.id, SUM(c.credits) " + THROUGH + "ANTI JOIN clubs k ON k.id = s.id "
        "GROUP BY s.id",
        "SELECT s.id, SUM(c.credits) " + THROUGH + "SEMI JOIN clubs k ON k.id = s.id "
        "GROUP BY s.id",
        "SELECT s.id, SUM(c.credits) FROM students s "
        "JOIN student_courses sc ON sc.student_id = s.id "
        "ASOF JOIN courses c ON c.id = sc.course_id AND c.credits >= 0 GROUP BY s.id",
        "SELECT s.id, SUM(c.credits) FROM students s POSITIONAL JOIN courses c "
        "GROUP BY s.id",
    ],
)
def test_what_is_not_certain_keeps_nothing(sql: str) -> None:
    assert _kept(sql) == set()


def test_a_key_naming_no_table_of_the_select_fixes_nothing() -> None:
    # Only the bridge itself is kept: the finest grain, which no join repeats.
    assert _kept("SELECT SUM(c.credits) " + THROUGH + "GROUP BY x.id") == {
        "student_courses"
    }


def test_group_by_all_keys_are_its_plain_outputs() -> None:
    """Every dialect with GROUP BY ALL groups by the unaggregated outputs."""
    every = "SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY ALL"
    for dialect in ("duckdb", "snowflake", "databricks", "bigquery", "clickhouse"):
        assert "courses" in _kept(every, dialect=dialect), dialect
    # A bare star stands for every table's columns.
    assert "courses" in _kept("SELECT *, SUM(c.credits) " + THROUGH + "GROUP BY ALL")
    # ALL before keys is the set quantifier, not every output.
    assert (
        _kept("SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY ALL s.id") == set()
    )
    # No output fixes a key: only the bridge, the finest grain, which no join
    # repeats -- as when an explicit key fixes nothing.
    for outputs in ("", "s.id + 0, "):
        assert _kept(f"SELECT {outputs}SUM(c.credits) " + THROUGH + "GROUP BY ALL") == {
            "student_courses"
        }


def _blocked_on_clickhouse(validator: OBQCValidator, sql: str) -> bool:
    result = validator.validate(sql, dialect="clickhouse")
    return not result.is_valid and any(
        i.issue_type == OBQCIssueType.FAN_TRAP_DETECTED for i in result.issues
    )


@pytest.mark.parametrize(
    "sql",
    [
        # ClickHouse's totals row sums over all students: Math counts twice.
        "SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY ALL WITH TOTALS",
        "SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY s.id WITH TOTALS",
    ],
)
def test_with_totals_adds_a_row_over_every_group(
    school: OBQCValidator, sql: str
) -> None:
    assert _kept(sql, dialect="clickhouse") == set()
    assert _blocked_on_clickhouse(school, sql)


@pytest.mark.parametrize(
    "sql",
    [
        # ClickHouse reads student_id as the alias -- an aggregate, one group
        # -- before the bridge's column.
        "SELECT MAX(s.id) AS student_id, student_id AS sid, SUM(c.credits) "
        + THROUGH
        + "GROUP BY ALL",
        "SELECT MAX(s.id) AS student_id, student_id, SUM(c.credits) "
        + THROUGH
        + "GROUP BY 2",
        # Grouped by name there, by the bridge's column elsewhere.
        "SELECT s.full_name AS student_id, SUM(c.credits) "
        + THROUGH
        + "GROUP BY student_id",
    ],
)
def test_a_name_that_is_also_an_alias_must_keep_rows_once_read_either_way(
    school: OBQCValidator, sql: str
) -> None:
    assert "courses" not in _kept(sql, dialect="clickhouse")
    assert _blocked_on_clickhouse(school, sql)


@pytest.mark.parametrize(
    "sql",
    [
        # A scalar WITH alias is read before the bridge's column: a constant.
        "WITH 0 AS student_id SELECT student_id, SUM(c.credits) "
        + THROUGH
        + "GROUP BY ALL",
        "WITH 0 AS student_id SELECT student_id, SUM(c.credits) "
        + THROUGH
        + "GROUP BY student_id",
        # ... and reaches subqueries.
        "WITH 0 AS student_id SELECT * FROM (SELECT student_id, SUM(c.credits) "
        + THROUGH
        + "GROUP BY ALL)",
        # An equality over an alias's name compares the alias: 0 = 0 here.
        "SELECT 0 AS student_id, SUM(c.credits) "
        + THROUGH
        + "WHERE student_id = 0 GROUP BY ALL",
        "SELECT s.full_name AS student_id, SUM(c.credits) "
        + THROUGH
        + "WHERE student_id = 1 GROUP BY s.full_name",
    ],
)
def test_scalar_aliases_and_equalities_read_names_both_ways(
    school: OBQCValidator, sql: str
) -> None:
    inner = sqlglot.parse_one(sql, dialect="clickhouse")
    (select,) = [s for s in inner.find_all(sqlglot.exp.Select) if s.args.get("group")]
    assert "courses" not in kept_once(select, _catalog)
    assert _blocked_on_clickhouse(school, sql)


def test_a_scalar_alias_is_seen_only_where_its_with_reaches() -> None:
    # Inside the subquery's WITH only: the outer student_id is the bridge's.
    outer = (
        "SELECT student_id, SUM(c.credits) "
        + THROUGH
        + "WHERE s.id IN (WITH 1 AS student_id SELECT student_id FROM student_courses) "
        "GROUP BY student_id"
    )
    assert "courses" in _kept(outer, dialect="clickhouse")
    # A WITH reaches its own query, the queries inside it, and a CTE's body.
    for sql in (
        "SELECT * FROM (WITH 0 AS student_id SELECT student_id, SUM(c.credits) "
        + THROUGH
        + "GROUP BY ALL)",
        "WITH 0 AS student_id, t AS (SELECT student_id, SUM(c.credits) "
        + THROUGH
        + "GROUP BY ALL) SELECT * FROM t",
        "WITH 0 AS student_id SELECT student_id, SUM(c.credits) "
        + THROUGH
        + "GROUP BY ALL UNION ALL SELECT 1, 2",
    ):
        select = next(
            s
            for s in sqlglot.parse_one(sql, dialect="clickhouse").find_all(
                sqlglot.exp.Select
            )
            if s.args.get("group")
        )
        assert "courses" not in kept_once(select, _catalog), sql


def test_the_query_around_a_subquery_with_its_own_with_passes(
    school: OBQCValidator,
) -> None:
    sql = (
        "SELECT student_id, SUM(c.credits) "
        + THROUGH
        + "WHERE s.id IN (WITH 1 AS student_id SELECT student_id FROM student_courses) "
        "GROUP BY student_id"
    )
    assert not _blocked_on_clickhouse(school, sql)


def test_a_row_generator_multiplies_rows_before_grouping(
    school: OBQCValidator,
) -> None:
    # Every row twice, so each course twice per student: sums double.
    sql = (
        "SELECT s.id, SUM(c.credits) "
        + THROUGH
        + "WHERE arrayJoin([1, 2]) > 0 GROUP BY s.id"
    )
    assert _kept(sql, dialect="clickhouse") == set()
    assert _blocked_on_clickhouse(school, sql)
    # A generator inside a subquery multiplies that subquery's rows only.
    sql = (
        "SELECT s.id, SUM(c.credits) "
        + THROUGH
        + "WHERE s.id IN (SELECT arrayJoin([1, 2])) GROUP BY s.id"
    )
    assert "courses" in _kept(sql, dialect="clickhouse")


def test_an_alias_naming_nothing_the_query_reads_changes_nothing() -> None:
    for sql in (
        "WITH 0 AS zero SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY ALL",
        "SELECT s.id, SUM(c.credits) AS credits_total "
        + THROUGH
        + "WHERE sc.student_id = 1 GROUP BY s.full_name",
    ):
        assert "courses" in _kept(sql, dialect="clickhouse"), sql


def test_a_name_read_either_way_as_the_same_student_passes(
    school: OBQCValidator,
) -> None:
    # The bridge's student_id or s.id: equal by the join, one student either way.
    for sql in (
        "SELECT s.id AS student_id, SUM(c.credits) " + THROUGH + "GROUP BY student_id",
        "SELECT s.id AS student_id, student_id AS sid, SUM(c.credits) "
        + THROUGH
        + "GROUP BY ALL",
    ):
        for dialect in ("clickhouse", "duckdb"):
            assert "courses" in _kept(sql, dialect=dialect), (sql, dialect)
        assert not _blocked_on_clickhouse(school, sql), sql


def test_mysqls_with_rollup_adds_groups_too() -> None:
    sql = "SELECT s.id, SUM(c.credits) " + THROUGH + "GROUP BY s.id WITH ROLLUP"
    assert _kept(sql, dialect="mysql") == set()


# --- Through the tools ---------------------------------------------------------------


@pytest.fixture
def school_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    import duckdb

    path = tmp_path / "school.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE students (id INTEGER PRIMARY KEY, full_name VARCHAR, "
        "tuition INTEGER)"
    )
    con.execute(
        "CREATE TABLE courses (id INTEGER PRIMARY KEY, title VARCHAR, credits INTEGER)"
    )
    con.execute(
        "CREATE TABLE student_courses (student_id INTEGER REFERENCES students(id), "
        "course_id INTEGER REFERENCES courses(id), PRIMARY KEY (student_id, course_id))"
    )
    con.execute("INSERT INTO students VALUES (1, 'Ada', 100), (2, 'Bo', 200)")
    con.execute("INSERT INTO courses VALUES (10, 'Math', 5), (11, 'Art', 3)")
    con.execute("INSERT INTO student_courses VALUES (1, 10), (1, 11), (2, 10)")
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
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(path))
    yield tmp_path
    fresh.cleanup()


def _data(result: Any) -> Any:
    return result.data if not isinstance(result.data, str) else json.loads(result.data)


async def test_through_the_tools(school_db: Path) -> None:
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
        assert found["bridge_tables"] == [
            {
                "table": "student_courses",
                "connects": ["students", "courses"],
                "pairs_unique": True,
            }
        ]
        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})

        async def run(sql: str) -> Any:
            return _data(
                await client.call_tool(
                    "execute_sql_query",
                    {**on, "checklist_completed": True, "sql_query": sql},
                )
            )

        through = (
            "FROM main.students s JOIN main.student_courses sc ON sc.student_id = s.id "
            "JOIN main.courses c ON c.id = sc.course_id "
        )
        per_student = await run(
            "SELECT s.id, SUM(c.credits) AS credits " + through + "GROUP BY s.id "
            "ORDER BY s.id"
        )
        assert per_student["success"] is True, per_student
        # Ada takes Math and Art, Bo takes Math: right, not inflated.
        assert [row["credits"] for row in per_student["data"]] == [8, 5]
        # The same, grouped by every output.
        every_output = await run(
            "SELECT s.id, SUM(c.credits) AS credits " + through + "GROUP BY ALL "
            "ORDER BY s.id"
        )
        assert every_output["success"] is True, every_output
        assert [row["credits"] for row in every_output["data"]] == [8, 5]
        by_name = await run(
            "SELECT s.full_name, SUM(c.credits) AS credits " + through + "GROUP BY ALL"
        )
        assert by_name["success"] is False  # two students may share a name

        total = await run("SELECT SUM(s.tuition) AS t " + through)
        assert total["success"] is False  # Ada's tuition would count twice
        assert any("GROUP BY" in issue for issue in total["obqc_issues"])
