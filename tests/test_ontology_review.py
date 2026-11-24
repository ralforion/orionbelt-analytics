"""Review status: what a person checked in the ontology.

A generated ontology is a draft. review_ontology marks tables, columns and
relationships reviewed or rejected; OBQC treats a reviewed relationship as a
firm key and a rejected one as no join, name suggestions leave reviewed
elements alone, and reviews survive regeneration.
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
from src.main import mcp
from src.obqc_validator import OBQCIssueType, OBQCSeverity, OBQCValidator
from src.ontology_generator import OntologyGenerator
from src.ontology_review import (
    DRAFT,
    OBA,
    REJECTED,
    REVIEWED,
    Review,
    elements,
    inventory,
    reapply,
    record,
    status_of,
)
from src.server_state import ServerState

BASE = "http://example.com/ontology/"


def _table(name: str, columns: dict[str, str], pk: str = "id") -> TableInfo:
    return TableInfo(
        name,
        "main",
        [ColumnInfo(c, t, True, c == pk, False) for c, t in columns.items()],
        [pk],
        [],
    )


# customers.credit summed across orders: a fan-out on a relationship only
# inferred from customer_id's name.
SHOP = [
    _table("customers", {"id": "INTEGER", "nm": "VARCHAR", "credit": "INTEGER"}),
    _table("orders", {"id": "INTEGER", "customer_id": "INTEGER", "total": "INTEGER"}),
]
FAN_TRAP = "SELECT SUM(c.credit) FROM customers c JOIN orders o ON o.customer_id = c.id"


def _generator() -> OntologyGenerator:
    generator = OntologyGenerator(base_uri=BASE)
    generator.generate_from_schema(SHOP)
    return generator


def _review(**kwargs: Any) -> Review:
    defaults: dict[str, Any] = {"column": None, "to_table": None, "status": REVIEWED}
    return Review(**{**defaults, **kwargs})


# --- Marking --------------------------------------------------------------------


def test_a_review_marks_a_table_a_column_or_a_relationship() -> None:
    graph = _generator().graph
    table = _review(table="customers", note="checked", reviewer="analyst")
    column = _review(table="orders", column="total")
    relationship = _review(
        table="orders", column="customer_id", to_table="customers", status=REJECTED
    )
    for review in (table, column, relationship):
        assert record(graph, review) == 1
    assert [table.kind, column.kind, relationship.kind] == [
        "table",
        "column",
        "relationship",
    ]
    (customers,), (total,), (link,) = (
        elements(graph, r) for r in (table, column, relationship)
    )
    assert status_of(graph, customers) == REVIEWED
    assert status_of(graph, total) == REVIEWED
    assert status_of(graph, link) == REJECTED
    assert str(graph.value(customers, OBA.reviewNote)) == "checked"
    assert str(graph.value(customers, OBA.reviewedBy)) == "analyst"
    # Nothing else was marked.
    assert set(graph.subjects(OBA.reviewStatus, None)) == {customers, total, link}


def test_draft_takes_a_review_back_and_unknown_elements_are_left_alone() -> None:
    graph = _generator().graph
    record(graph, _review(table="customers", note="x"))
    assert record(graph, _review(table="customers", status=DRAFT)) == 1
    assert not list(graph.subjects(OBA.reviewStatus, None))
    assert not list(graph.subjects(OBA.reviewNote, None))
    assert record(graph, _review(table="nowhere")) == 0
    assert record(graph, _review(table="orders", column="missing")) == 0


def test_reviews_are_reapplied_by_element() -> None:
    kept = {
        r.key(): r.as_dict()
        for r in (
            _review(table="customers"),
            _review(
                table="orders",
                column="customer_id",
                to_table="customers",
                status=REJECTED,
            ),
            _review(table="gone"),  # no longer in the schema: skipped
        )
    }
    regenerated = _generator().graph
    assert reapply(regenerated, kept) == 2
    (customers,) = elements(regenerated, _review(table="customers"))
    assert status_of(regenerated, customers) == REVIEWED


def test_the_inventory_lists_relationships_with_their_evidence() -> None:
    graph = _generator().graph
    listing = inventory(graph)
    (rel,) = listing["relationships"]
    assert rel == {
        "relationship": "orders.customer_id -> customers",
        "source": "inferred (embedded_table_name)",
        "checked": None,
        "review": DRAFT,
    }
    assert listing["counts"]["tables"] == {REVIEWED: 0, REJECTED: 0, DRAFT: 2}
    record(graph, _review(table="orders", column="customer_id", to_table="customers"))
    assert inventory(graph)["counts"]["relationships"][REVIEWED] == 1


# --- What it changes ------------------------------------------------------------


def _fan_trap(generator: OntologyGenerator) -> Any:
    validator = OBQCValidator()
    validator.load_ontology(generator.graph, BASE)
    return validator.validate(FAN_TRAP, dialect="duckdb")


def test_a_reviewed_relationship_is_a_firm_key_to_obqc() -> None:
    generator = _generator()
    guessed = _fan_trap(generator)
    # Only names back it: warned, not blocked.
    assert guessed.is_valid
    assert any(i.issue_type == OBQCIssueType.FAN_TRAP_DETECTED for i in guessed.issues)
    record(
        generator.graph,
        _review(table="orders", column="customer_id", to_table="customers"),
    )
    reviewed = _fan_trap(generator)
    assert not reviewed.is_valid
    assert any(
        i.issue_type == OBQCIssueType.FAN_TRAP_DETECTED
        and i.severity == OBQCSeverity.ERROR
        for i in reviewed.issues
    )


def test_a_rejected_relationship_is_no_join_to_obqc() -> None:
    generator = _generator()
    record(
        generator.graph,
        _review(
            table="orders", column="customer_id", to_table="customers", status=REJECTED
        ),
    )
    validator = OBQCValidator()
    validator.load_ontology(generator.graph, BASE)
    assert validator._schema_cache is not None
    assert not validator._schema_cache.relationships
    assert ("orders", "customers") in validator._schema_cache.refuted_links
    result = validator.validate(FAN_TRAP, dialect="duckdb")
    assert not any(
        i.issue_type == OBQCIssueType.FAN_TRAP_DETECTED for i in result.issues
    )


def test_reviewed_names_are_neither_suggested_nor_renamed() -> None:
    generator = _generator()
    record(generator.graph, _review(table="customers", column="nm"))
    names = generator.extract_names_for_review()
    (nm,) = [
        p
        for p in names["properties"]
        if p.get("column_name") == "nm" and p.get("table_name") == "customers"
    ]
    assert nm["needs_review"]["is_cryptic"] is False
    assert nm["needs_review"]["reviewed"] is True
    generator.apply_semantic_names(
        {
            "properties": [
                {
                    "original_name": "nm",
                    "table_name": "customers",
                    "suggested_name": "Customer Name",
                },
                {
                    "original_name": "credit",
                    "table_name": "customers",
                    "suggested_name": "Credit Limit",
                },
            ]
        }
    )
    assert generator.skipped_reviewed_names() == ["nm"]
    assert [a["original_name"] for a in generator.applied_semantic_names()] == [
        "credit"
    ]


def test_an_ontology_with_reviews_conforms_to_the_shapes() -> None:
    from src.shacl_validator import shacl_available, validate_ontology

    if not shacl_available():
        pytest.skip("pyshacl not installed")
    generator = _generator()
    record(
        generator.graph,
        _review(
            table="customers",
            note="ok",
            reviewer="analyst",
            at="2026-11-24T10:00:00+00:00",
        ),
    )
    record(
        generator.graph,
        _review(
            table="orders", column="customer_id", to_table="customers", status=REJECTED
        ),
    )
    report = validate_ontology(generator.serialize_ontology())
    assert report["conforms"], report.get("report")


# --- Through the tools ----------------------------------------------------------


@pytest.fixture
def shop_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    import duckdb

    path = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, nm VARCHAR, credit INTEGER);
        INSERT INTO customers VALUES (1, 'Ada', 100), (2, 'Bo', 200);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total INTEGER);
        INSERT INTO orders VALUES (10, 1, 5), (11, 1, 7), (12, 2, 9);
        """)
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


def _latest_ontology(folder: Path) -> str:
    files = sorted(folder.rglob("ontology_*.ttl"), key=lambda p: p.stat().st_mtime)
    return files[-1].read_text()


async def test_through_the_tools(shop_db: Path) -> None:
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
        await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})

        async def run(sql: str) -> Any:
            return _data(
                await client.call_tool(
                    "execute_sql_query",
                    {**on, "checklist_completed": True, "sql_query": sql},
                )
            )

        fan_trap = FAN_TRAP.replace("customers", "main.customers").replace(
            "orders", "main.orders"
        )
        assert (await run(fan_trap))["success"] is True  # a guess: warned only

        listing = _data(await client.call_tool("review_ontology", on))
        assert listing["relationships"][0]["relationship"] == (
            "orders.customer_id -> customers"
        )
        marked = _data(
            await client.call_tool(
                "review_ontology",
                {
                    **on,
                    "items": [
                        {
                            "table": "orders",
                            "column": "customer_id",
                            "to_table": "customers",
                            "note": "checked with finance",
                        },
                        {"table": "customers"},
                        {"table": "nowhere"},
                    ],
                    "reviewer": "analyst",
                },
            )
        )
        assert [m["kind"] for m in marked["marked"]] == ["relationship", "table"]
        assert marked["not_found"] == ["nowhere"]
        assert marked["counts"]["relationships"]["reviewed"] == 1

        blocked = await run(fan_trap)
        assert blocked["success"] is False  # reviewed: a firm key, so it blocks

        # Regenerated, the reviews are still there.
        await client.call_tool("reset_cache", {**on, "cache_type": "ontology"})
        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})
        regenerated = _latest_ontology(shop_db)
        assert '"reviewed"' in regenerated
        assert "checked with finance" in regenerated
        assert (await run(fan_trap))["success"] is False

        # Taken back, it is a guess again.
        await client.call_tool(
            "review_ontology",
            {
                **on,
                "items": [
                    {
                        "table": "orders",
                        "column": "customer_id",
                        "to_table": "customers",
                        "status": "draft",
                    }
                ],
            },
        )
        assert (await run(fan_trap))["success"] is True
