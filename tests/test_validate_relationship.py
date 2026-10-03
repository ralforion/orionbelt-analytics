"""validate_relationship checks a relationship against the data and records it.

A model asked to confirm inferred relationships ran its own queries and wrote
free-form triples into a side graph nothing reads: the relationship stayed
"inferred, medium", the result was not in the ontology file, and a
regeneration forgot it. The tool measures a relationship the same way every
time and records the verdict in the ontology, in the workspace, and on joins.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from fastmcp import Client
from rdflib import Graph, Literal, Namespace
from sqlglot import exp

import src.main as main_module
import src.server_state as state_module
from src.handlers import connection as connection_handler
from src.handlers.graphrag import _attach_validations
from src.main import mcp
from src.relationship_validation import (
    STATUS_CONFIRMED,
    STATUS_NO_DATA,
    STATUS_PARTIAL,
    STATUS_REFUTED,
    STATUS_TARGET_NOT_UNIQUE,
    RelationshipRef,
    build_check_queries,
    classify,
    relationship_key,
)
from src.server_state import ServerState

OBA = Namespace("https://ralforion.com/ns/oba#")


class TestClassify:
    @pytest.mark.parametrize(
        ("counts", "status"),
        [
            ((100, 100, 10, 10), STATUS_CONFIRMED),
            ((1000, 991, 10, 10), STATUS_CONFIRMED),
            ((100, 95, 10, 10), STATUS_PARTIAL),
            ((100, 40, 10, 10), STATUS_REFUTED),
            ((100, 100, 12, 10), STATUS_TARGET_NOT_UNIQUE),
            ((0, 0, 10, 10), STATUS_NO_DATA),
        ],
    )
    def test_the_verdict_for_measured_counts(self, counts, status):
        assert classify(*counts)[0] == status


class TestQueries:
    REF = RelationshipRef(
        from_table="Purchases",
        column="SupplierID",
        to_table="Suppliers",
        to_column="ID",
        from_schema="Gold",
        to_schema="Gold",
        property_uri="http://e/p",
    )

    def test_identifiers_keep_their_case_in_each_dialect(self):
        postgres, _ = build_check_queries(self.REF, "postgresql")
        mysql, _ = build_check_queries(self.REF, "mysql")
        bigquery, _ = build_check_queries(self.REF, "bigquery")

        assert '"Gold"."Purchases"' in postgres and '"SupplierID"' in postgres
        assert "`Gold`.`Purchases`" in mysql
        assert "`Gold`.`Purchases`" in bigquery

    def test_a_name_from_an_uploaded_ontology_cannot_break_out(self):
        hostile = RelationshipRef(
            from_table='p"; DROP TABLE x; --',
            column="c",
            to_table="t",
            to_column="id",
            from_schema="main",
            to_schema="main",
            property_uri="http://e/p",
        )

        coverage, _ = build_check_queries(hostile, "postgresql")

        # Parsed back: one SELECT, whose source table is the name verbatim.
        statements = sqlglot.parse(coverage, dialect="postgres")
        assert len(statements) == 1
        assert isinstance(statements[0], exp.Select)
        tables = {t.name for t in statements[0].find_all(exp.Table)}
        assert 'p"; DROP TABLE x; --' in tables

    def test_the_match_is_counted_without_a_join(self):
        coverage, uniqueness = build_check_queries(self.REF, "clickhouse")

        assert " IN (SELECT " in coverage and "JOIN" not in coverage
        assert "COUNT(DISTINCT" in uniqueness


def test_joins_show_their_recorded_verdict_in_either_direction():
    verdicts = {
        relationship_key("purchases", "supplier_id", "suppliers"): {
            "status": "confirmed",
            "match_ratio": 1.0,
            "checked_at": "2026-10-03T00:00:00+00:00",
        }
    }
    forward = [
        {
            "from_table": "main.purchases",
            "from_column": "supplier_id",
            "to_table": "main.suppliers",
            "to_column": "id",
        }
    ]
    backward = [
        {
            "from_table": "main.suppliers",
            "from_column": "id",
            "to_table": "main.purchases",
            "to_column": "supplier_id",
        }
    ]

    _attach_validations(forward, verdicts)
    _attach_validations(backward, verdicts)

    assert forward[0]["validation"]["status"] == "confirmed"
    assert backward[0]["validation"]["status"] == "confirmed"


# --- end to end over MCP, against a DuckDB file ---


@pytest.fixture
def shop(monkeypatch, tmp_path):
    """A DuckDB file whose keys are not declared; one holds, one does not."""
    import duckdb

    path = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name VARCHAR)")
    con.execute("INSERT INTO suppliers VALUES (1,'a'),(2,'b'),(3,'c')")
    con.execute("CREATE TABLE purchases (id INTEGER PRIMARY KEY, supplier_id INTEGER)")
    con.execute("INSERT INTO purchases VALUES (10,1),(11,2),(12,3),(13,1),(14,NULL)")
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR)")
    con.execute("INSERT INTO customers VALUES (1,'x'),(2,'y')")
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER)")
    con.execute("INSERT INTO orders VALUES (20,1),(21,7),(22,8),(23,9)")
    con.close()

    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    for module in ("src.paths", "src.handlers.ontology_validation"):
        monkeypatch.setattr(f"{module}.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setenv("AUTO_GRAPHRAG", "false")
    monkeypatch.setenv("OBA_SHACL_VALIDATE", "false")
    monkeypatch.delenv("OBA_DATABASES", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(path))
    yield tmp_path
    fresh.cleanup()


def _text(result: Any) -> str:
    return result.data if isinstance(result.data, str) else json.dumps(result.data)


def _active_ontology(workdir: Path) -> Graph:
    files = sorted(workdir.rglob("ontology_*.ttl"), key=lambda p: p.stat().st_mtime)
    graph = Graph()
    graph.parse(files[-1], format="turtle")
    return graph


def _status_of(graph: Graph, column: str) -> str | None:
    for prop in graph.subjects(OBA.foreignKeyColumn, Literal(column)):
        value = graph.value(prop, OBA.validationStatus)
        if value is not None:
            return str(value)
    return None


async def test_a_verdict_is_measured_recorded_and_kept(shop):
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
        handle = re.search(r"(ob_[a-z0-9]{6})", _text(connected)).group(1)
        on = {"connection": handle}
        await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})

        held = (
            await client.call_tool(
                "validate_relationship",
                {**on, "from_table": "purchases", "column": "supplier_id"},
            )
        ).data
        broken = (
            await client.call_tool(
                "validate_relationship",
                {**on, "from_table": "orders", "column": "customer_id"},
            )
        ).data

        # Measured: NULL keys are not counted, unmatched ones are.
        assert (held["status"], held["checked_rows"], held["matched_rows"]) == (
            STATUS_CONFIRMED,
            4,
            4,
        )
        assert (broken["status"], broken["match_ratio"]) == (STATUS_REFUTED, 0.25)

        # Recorded in the ontology file itself...
        graph = _active_ontology(shop)
        assert _status_of(graph, "supplier_id") == STATUS_CONFIRMED
        assert _status_of(graph, "customer_id") == STATUS_REFUTED

        # ...and still there after the ontology is generated again.
        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})
        regenerated = _active_ontology(shop)
        assert _status_of(regenerated, "supplier_id") == STATUS_CONFIRMED
        assert _status_of(regenerated, "customer_id") == STATUS_REFUTED


async def test_a_relationship_the_ontology_does_not_state_is_refused(shop):
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
        handle = re.search(r"(ob_[a-z0-9]{6})", _text(connected)).group(1)
        on = {"connection": handle}
        await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})

        result = await client.call_tool(
            "validate_relationship",
            {**on, "from_table": "purchases", "column": "nope"},
            raise_on_error=False,
        )

    assert "relationship_not_found" in _text(result)
