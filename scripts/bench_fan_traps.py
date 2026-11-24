#!/usr/bin/env python3
"""Fan-trap benchmark: does the SQL check stop queries that return a wrong number?

Every case in ``benchmarks/fan_traps/cases.toml`` is a question, a candidate
query for it and a "truth" query that answers it correctly. Each candidate
runs, and names only tables and columns that exist -- the kind of query a
check of names lets through. Whether it is *wrong* is not asserted by hand:
both queries run on the same data, and a candidate is wrong when its numbers
differ from the truth's. The benchmark then asks the check what it makes of
each candidate:

- **blocked**: ``execute_sql_query`` would refuse it (an OBQC error);
- **warned**: it would run, with a fan-out warning attached for the model;
- **passed**: it would run without one.

Three ways the ontology learns the keys, the same data each time:

- **declared**: the database declares primary and foreign keys;
- **inferred**: primary keys only, as warehouses often have; relationships
  are guessed from column names, and a fan-trap resting on a guess warns
  rather than blocks;
- **inferred + checked**: the same guesses, each checked on the data first
  (what ``AUTO_VALIDATE_RELATIONSHIPS=true`` or ``validate_relationship``
  does), so a confirmed one counts like a declared key.

And a baseline: a check of names alone (every table and column exists, as
sqlglot's qualifier resolves them), which is what a SQL gate without an
ontology can do.

Everything is built in a temporary directory -- a DuckDB file per variant,
fixed data from a seeded generator -- and nothing reads ``.env`` or a
workspace. Nothing here needs a database or a network.

    uv run python scripts/bench_fan_traps.py               # summary + cases
    uv run python scripts/bench_fan_traps.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = ROOT / "benchmarks" / "fan_traps" / "cases.toml"
BASE_URI = "http://example.com/ontology/"
SCENARIOS = ("declared", "inferred", "inferred + checked")
BLOCKED, WARNED, PASSED = "blocked", "warned", "passed"

# --- Schema and data -------------------------------------------------------------

# (table, columns, primary key, foreign keys as (column, table, column))
TABLES: list[tuple[str, str, str, list[tuple[str, str, str]]]] = [
    ("regions", "id INTEGER, name VARCHAR", "id", []),
    (
        "customers",
        "id INTEGER, name VARCHAR, region_id INTEGER, credit_limit DECIMAL(10, 2)",
        "id",
        [("region_id", "regions", "id")],
    ),
    (
        "products",
        "id INTEGER, name VARCHAR, category VARCHAR, price DECIMAL(10, 2)",
        "id",
        [],
    ),
    (
        "orders",
        "id INTEGER, customer_id INTEGER, order_date DATE, total DECIMAL(12, 2)",
        "id",
        [("customer_id", "customers", "id")],
    ),
    (
        "order_items",
        "id INTEGER, order_id INTEGER, product_id INTEGER, quantity INTEGER, "
        "price DECIMAL(10, 2)",
        "id",
        [("order_id", "orders", "id"), ("product_id", "products", "id")],
    ),
    (
        "payments",
        "id INTEGER, order_id INTEGER, amount DECIMAL(12, 2)",
        "id",
        [("order_id", "orders", "id")],
    ),
    (
        "shipments",
        "id INTEGER, order_id INTEGER, cost DECIMAL(10, 2)",
        "id",
        [("order_id", "orders", "id")],
    ),
    ("students", "id INTEGER, name VARCHAR, tuition DECIMAL(10, 2)", "id", []),
    ("courses", "id INTEGER, title VARCHAR, credits INTEGER", "id", []),
    (
        "student_courses",
        "student_id INTEGER, course_id INTEGER",
        "student_id, course_id",
        [("student_id", "students", "id"), ("course_id", "courses", "id")],
    ),
]


def _cents(value: int) -> Decimal:
    return Decimal(value) / 100


def generate_data(seed: int = 7) -> dict[str, list[tuple[Any, ...]]]:
    """The benchmark's rows, the same for every run with the same seed.

    Built so that the traps show: customers with several orders, orders with
    several items and payments, some customers and orders with none, one
    shipment per order (many allowed), two students of one name, students in
    several courses, and one customer with two orders of equal value.
    """
    rng = random.Random(seed)
    regions = [(1, "North"), (2, "South"), (3, "East"), (4, "West")]
    customers = [
        (
            i,
            f"Customer {i}",
            rng.randint(1, 4),
            _cents(rng.randrange(1000, 10000, 500) * 100),
        )
        for i in range(1, 41)
    ]
    categories = ["books", "electronics", "garden"]
    products = [
        (
            i,
            f"Product {i}",
            categories[(i - 1) % 3],
            _cents(rng.randrange(500, 20000, 25)),
        )
        for i in range(1, 13)
    ]
    prices = {p[0]: p[3] for p in products}
    orders: list[tuple[Any, ...]] = []
    items: list[tuple[Any, ...]] = []
    payments: list[tuple[Any, ...]] = []
    shipments: list[tuple[Any, ...]] = []
    for order_id in range(1, 121):
        if order_id <= 2:
            # Customer 1: two orders of the same two items, the same total.
            customer, lines = 1, [(1, 2), (5, 1)]
        else:
            customer = rng.randint(1, 36)  # customers 37-40 never order
            lines = [
                (rng.randint(1, 12), rng.randint(1, 5))
                for _ in range(rng.choice([0, 1, 1, 2, 2, 3, 4]))
            ]
        total = Decimal(0)
        for product, quantity in lines:
            items.append((len(items) + 1, order_id, product, quantity, prices[product]))
            total += quantity * prices[product]
        day = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        orders.append((order_id, customer, day, total))
        if total:
            # Paid in one to three instalments that add up to the total.
            parts = rng.choice([1, 1, 2, 3])
            cents = int(total * 100)
            share = cents // parts
            for k in range(parts):
                amount = share if k < parts - 1 else cents - share * (parts - 1)
                payments.append((len(payments) + 1, order_id, _cents(amount)))
        shipments.append((order_id, order_id, _cents(rng.randrange(300, 1500))))
    students = [
        (
            i,
            "Kim Lee" if i in (5, 6) else f"Student {i}",
            _cents(rng.randrange(2000, 8000, 250) * 100),
        )
        for i in range(1, 21)
    ]
    courses = [(i, f"Course {i}", rng.randint(2, 6)) for i in range(1, 9)]
    enrolments = sorted(
        (student, course)
        for student in range(1, 21)
        for course in rng.sample(range(1, 9), rng.randint(1, 4))
    )
    return {
        "regions": regions,
        "customers": customers,
        "products": products,
        "orders": orders,
        "order_items": items,
        "payments": payments,
        "shipments": shipments,
        "students": students,
        "courses": courses,
        "student_courses": enrolments,
    }


def create_database(
    path: Path, data: dict[str, list[tuple[Any, ...]]], declared: bool
) -> None:
    """A DuckDB file with the benchmark's tables and rows.

    Primary keys always; foreign keys only when *declared*.
    """
    import duckdb

    con = duckdb.connect(str(path))
    try:
        for table, columns, key, references in TABLES:
            constraints = [f"PRIMARY KEY ({key})"]
            if declared:
                constraints += [
                    f"FOREIGN KEY ({column}) REFERENCES {target}({target_column})"
                    for column, target, target_column in references
                ]
            con.execute(f"CREATE TABLE {table} ({columns}, {', '.join(constraints)})")
            rows = data[table]
            if rows:
                marks = ", ".join("?" * len(rows[0]))
                con.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    finally:
        con.close()


# --- Ontology, as the server builds it --------------------------------------------


def ontology(path: Path) -> Any:
    """The ontology generate_ontology would build for the DuckDB file."""
    from src.database_manager import DatabaseManager
    from src.ontology_generator import OntologyGenerator

    manager = DatabaseManager()
    if not manager.connect_duckdb(str(path)):
        raise RuntimeError(f"could not open {path}")
    try:
        names = sorted(manager.get_tables("main"))
        tables = manager.analyze_tables(names, "main")
    finally:
        manager.disconnect()
    generator = OntologyGenerator(base_uri=BASE_URI)
    generator.generate_from_schema([tables[name] for name in names if name in tables])
    return generator.graph


def check_inferred(graph: Any, path: Path) -> Counter[str]:
    """Check every inferred relationship on the data and record the verdicts.

    The same two queries ``validate_relationship`` runs, on a read-only
    connection.
    """
    import duckdb

    from src.relationship_validation import (
        ValidationRecord,
        build_check_queries,
        classify,
        inferred_relationships_to_check,
        record_in_graph,
    )

    verdicts: Counter[str] = Counter()
    con = duckdb.connect(str(path), read_only=True)
    try:
        for ref in inferred_relationships_to_check(graph):
            coverage_sql, uniqueness_sql = build_check_queries(ref, "duckdb")
            checked, matched = con.execute(coverage_sql).fetchone() or (0, 0)
            rows, distinct = con.execute(uniqueness_sql).fetchone() or (0, 0)
            status, ratio = classify(
                int(checked or 0), int(matched or 0), int(rows or 0), int(distinct or 0)
            )
            record_in_graph(
                graph,
                ValidationRecord(
                    from_schema=ref.from_schema,
                    from_table=ref.from_table,
                    column=ref.column,
                    to_schema=ref.to_schema,
                    to_table=ref.to_table,
                    to_column=ref.to_column,
                    status=status,
                    match_ratio=ratio,
                    checked_rows=int(checked or 0),
                    matched_rows=int(matched or 0),
                    target_rows=int(rows or 0),
                    target_distinct=int(distinct or 0),
                    checked_at="2026-01-01T00:00:00+00:00",
                ),
            )
            verdicts[status] += 1
    finally:
        con.close()
    return verdicts


def obqc(graph: Any) -> Any:
    from src.obqc_validator import OBQCValidator

    validator = OBQCValidator()
    validator.load_ontology(graph, BASE_URI)
    return validator


def judge(validator: Any, sql: str) -> tuple[str, list[str]]:
    """What execute_sql_query would do with *sql*, and the findings why."""
    from src.obqc_validator import OBQCIssueType, OBQCSeverity

    result = validator.validate(sql, dialect="duckdb")
    findings = sorted(
        {
            f"{issue.severity.value}:{issue.issue_type.value}"
            for issue in result.issues
            if issue.severity in (OBQCSeverity.ERROR, OBQCSeverity.WARNING)
        }
    )
    if not result.is_valid:
        return BLOCKED, findings
    fan_out = any(
        issue.issue_type == OBQCIssueType.FAN_TRAP_DETECTED
        and issue.severity == OBQCSeverity.WARNING
        for issue in result.issues
    )
    return (WARNED if fan_out else PASSED), findings


def names_only(sql: str, catalog: dict[str, dict[str, str]]) -> str:
    """A check of names alone: every table and column must exist."""
    import sqlglot
    from sqlglot.errors import OptimizeError
    from sqlglot.optimizer.qualify import qualify

    try:
        qualify(
            sqlglot.parse_one(sql, dialect="duckdb"),
            schema=catalog,
            dialect="duckdb",
            validate_qualify_columns=True,
        )
    except (OptimizeError, sqlglot.errors.ParseError):
        return BLOCKED
    return PASSED


# --- Running the cases -------------------------------------------------------------


def _value(value: Any) -> Any:
    if isinstance(value, (Decimal, float)):
        return round(float(value), 4)
    return value


def _rows(con: Any, sql: str) -> list[tuple[Any, ...]]:
    return sorted(
        (tuple(_value(v) for v in row) for row in con.execute(sql).fetchall()),
        key=repr,
    )


def _error(got: list[tuple[Any, ...]], truth: list[tuple[Any, ...]]) -> float | None:
    """The largest relative difference between matching numbers, by first column."""
    by_key = {row[0]: row[1:] for row in truth}
    worst: float | None = None
    for row in got:
        expected = by_key.get(row[0]) if len(row) > 1 else truth[0] if truth else None
        values = row[1:] if len(row) > 1 else row
        if expected is None:
            continue
        for a, b in zip(values, expected, strict=False):
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b:
                diff = abs(a - b) / abs(b)
                worst = diff if worst is None else max(worst, diff)
    return worst


@dataclass
class CaseResult:
    id: str
    pattern: str
    question: str
    #: What the data shows: "wrong" when the numbers differ from the truth.
    label: str
    #: What cases.toml expects the data to show.
    expect: str
    #: Largest relative error of a wrong answer, e.g. 0.38 for 38% off.
    error: float | None
    names_only: str
    #: Per scenario: blocked, warned or passed.
    outcome: dict[str, str] = field(default_factory=dict)
    findings: dict[str, list[str]] = field(default_factory=dict)


def run(seed: int = 7) -> dict[str, Any]:
    """Run every case in every scenario.

    Returns:
        ``cases`` (one CaseResult dict each), ``summary`` per scenario and
        for the names-only baseline, and the inferred relationships'
        verdicts.
    """
    import duckdb

    cases = tomllib.loads(CASES.read_text())["case"]
    data = generate_data(seed)
    with tempfile.TemporaryDirectory(prefix="oba-fan-traps-") as tmp:
        declared_db = Path(tmp) / "declared.duckdb"
        inferred_db = Path(tmp) / "inferred.duckdb"
        create_database(declared_db, data, declared=True)
        create_database(inferred_db, data, declared=False)

        declared = ontology(declared_db)
        guessed = ontology(inferred_db)
        checked = ontology(inferred_db)
        verdicts = check_inferred(checked, inferred_db)
        validators = {
            "declared": obqc(declared),
            "inferred": obqc(guessed),
            "inferred + checked": obqc(checked),
        }

        con = duckdb.connect(str(declared_db), read_only=True)
        catalog: dict[str, dict[str, str]] = {}
        for table, column, data_type in con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns"
        ).fetchall():
            catalog.setdefault(table, {})[column] = data_type
        results: list[CaseResult] = []
        try:
            for case in cases:
                got = _rows(con, case["sql"])
                truth = _rows(con, case["truth"])
                wrong = got != truth
                result = CaseResult(
                    id=case["id"],
                    pattern=case["pattern"],
                    question=case["question"],
                    label="wrong" if wrong else "right",
                    expect=case["expect"],
                    error=_error(got, truth) if wrong else None,
                    names_only=names_only(case["sql"], catalog),
                )
                for scenario, validator in validators.items():
                    outcome, findings = judge(validator, case["sql"])
                    result.outcome[scenario] = outcome
                    result.findings[scenario] = findings
                results.append(result)
        finally:
            con.close()

    summary: dict[str, dict[str, dict[str, int]]] = {}
    for scenario in ("names only", *SCENARIOS):
        tally: dict[str, dict[str, int]] = {
            label: {BLOCKED: 0, WARNED: 0, PASSED: 0} for label in ("wrong", "right")
        }
        for r in results:
            outcome = r.names_only if scenario == "names only" else r.outcome[scenario]
            tally[r.label][outcome] += 1
        summary[scenario] = tally
    return {
        "cases": [asdict(r) for r in results],
        "summary": summary,
        "verdicts": dict(verdicts),
    }


# --- Report ------------------------------------------------------------------------


def report(outcome: dict[str, Any]) -> str:
    cases = outcome["cases"]
    wrong = [c for c in cases if c["label"] == "wrong"]
    right = [c for c in cases if c["label"] == "right"]
    lines = [
        f"{len(cases)} queries that run and name only real tables and columns: "
        f"{len(wrong)} return a wrong number, {len(right)} the right one.",
        "",
        "| Check | Wrong: blocked | warned | passed | Right: passed | warned | blocked |",
        "|---|---|---|---|---|---|---|",
    ]
    for scenario, tally in outcome["summary"].items():
        w, r = tally["wrong"], tally["right"]
        lines.append(
            f"| {scenario} | {w[BLOCKED]} | {w[WARNED]} | {w[PASSED]} "
            f"| {r[PASSED]} | {r[WARNED]} | {r[BLOCKED]} |"
        )
    lines += [
        "",
        f"Inferred relationships checked on the data: {outcome['verdicts']}",
        "",
        "| Case | Pattern | Data | Off by | Names only | "
        + " | ".join(SCENARIOS)
        + " |",
        "|---|---|---|---|---|" + "---|" * len(SCENARIOS),
    ]
    for c in cases:
        off = (
            f"{c['error']:.0%}"
            if c["error"] is not None
            else ("rows" if c["label"] == "wrong" else "")
        )
        lines.append(
            f"| {c['id']} | {c['pattern']} | {c['label']} | {off} | {c['names_only']} | "
            + " | ".join(c["outcome"][s] for s in SCENARIOS)
            + " |"
        )
    mismatched = [c["id"] for c in cases if c["label"] != c["expect"]]
    if mismatched:
        lines += [
            "",
            f"Cases whose data no longer shows what they expect: {mismatched}",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path, help="also write the results here")
    args = parser.parse_args()
    outcome = run(args.seed)
    print(report(outcome))
    if args.json:
        args.json.write_text(json.dumps(outcome, indent=2, default=str))
    return 1 if any(c["label"] != c["expect"] for c in outcome["cases"]) else 0


if __name__ == "__main__":
    sys.exit(main())
