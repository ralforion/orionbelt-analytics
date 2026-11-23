"""Joins found in the data where column names do not say them.

orders.cust holds customers' ids, but names no table: inference from names
links nothing. With OBA_DISCOVER_JOINS_BY_DATA, a column that looks like a
reference is checked against other tables' keys, and linked when exactly one
key holds its sampled values -- or a key named like it, among several.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from rdflib.namespace import OWL

import src.join_discovery as join_discovery
import src.main as main_module
import src.server_state as state_module
from src.database_manager import ColumnInfo, DatabaseManager, TableInfo
from src.handlers import connection as connection_handler
from src.join_discovery import (
    DISCOVER_VARIABLE,
    PATTERN,
    as_inferred,
    candidates,
    held_query,
    kind,
    run,
)
from src.main import mcp
from src.ontology_generator import InferredRelationship, OntologyGenerator
from src.server_state import ServerState

# --- Which columns are candidates ------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        ("INTEGER", "integer"),
        ("BIGINT", "integer"),
        ("Int64", "integer"),
        ("NUMBER(38,0)", "integer"),
        ("NUMBER", "integer"),
        ("DECIMAL(10, 0)", "integer"),
        ("VARCHAR(20)", "text"),
        ("TEXT", "text"),
        ("UUID", "text"),
        ("DECIMAL(10,2)", None),
        ("DOUBLE", None),
        ("DATE", None),
        ("TIMESTAMP", None),
        ("BOOLEAN", None),
        ("INTERVAL", None),
    ],
)
def test_only_types_a_key_may_have_are_compared(
    data_type: str, expected: str | None
) -> None:
    assert kind(data_type) == expected


def _table(
    name: str,
    columns: dict[str, str],
    pk: str | None = "id",
    fks: list[dict[str, str]] | None = None,
) -> TableInfo:
    return TableInfo(
        name,
        "main",
        [ColumnInfo(c, t, True, c == pk, False) for c, t in columns.items()],
        [pk] if pk else [],
        fks or [],
    )


def test_a_candidate_looks_like_a_reference_and_is_linked_nowhere() -> None:
    orders = _table(
        "orders",
        {
            "id": "INTEGER",
            "cust": "VARCHAR",  # named like customers
            "owner_ref": "VARCHAR",  # a key word
            "buyer_id": "INTEGER",
            "customer_id": "VARCHAR",  # linked by its name already
            "store_id": "INTEGER",  # a declared key
            "amount": "INTEGER",  # no reference
            "status": "VARCHAR",
            "created_at": "TIMESTAMP",
            "batch_id": "INTEGER",  # bookkeeping
            "price_code": "DECIMAL(10,2)",  # no key type
        },
        fks=[{"column": "store_id", "referenced_table": "stores"}],
    )
    customers = _table("customers", {"id": "VARCHAR", "name": "VARCHAR"})
    found = candidates([orders, customers], {("orders", "customer_id")})
    assert found == {
        "orders": [("cust", "text"), ("owner_ref", "text"), ("buyer_id", "integer")]
    }


def test_a_key_is_a_tables_one_column_key_or_its_id() -> None:
    keyed = _table("customers", {"id": "VARCHAR", "name": "VARCHAR"})
    keyless = _table("stores", {"id": "INTEGER", "city": "VARCHAR"}, pk=None)
    paired = TableInfo(
        "pairs",
        "main",
        [ColumnInfo(c, "INTEGER", False, True, False) for c in ("a_id", "b_id")],
        ["a_id", "b_id"],
        [],
    )
    dated = _table("days", {"day": "DATE"}, pk="day")
    assert [
        (k.table.name, k.column, k.kind)
        for k in join_discovery._keys([keyed, keyless, paired, dated])
    ] == [("customers", "id", "text"), ("stores", "id", "integer")]


def test_values_go_into_the_lookup_as_literals() -> None:
    sql = held_query("duckdb", ["main", 'odd"table'], "id", ["a'b", "c"])
    assert sql == (
        'SELECT COUNT(DISTINCT "id") FROM "main"."odd""table" '
        "WHERE \"id\" IN ('a''b', 'c')"
    )
    assert "IN (1, 2)" in held_query("postgres", ["t"], "id", [1, 2])


# --- In the data -----------------------------------------------------------------


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import duckdb

    path = tmp_path_factory.mktemp("jd") / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE customers (id VARCHAR PRIMARY KEY, name VARCHAR);
        INSERT INTO customers
          SELECT 'C-' || lpad(i::VARCHAR, 4, '0'), 'n' || i FROM range(1, 201) t(i);
        CREATE TABLE products (id INTEGER PRIMARY KEY, label VARCHAR);
        INSERT INTO products SELECT i, 'p' FROM range(100, 400) t(i);
        CREATE TABLE suppliers (id INTEGER PRIMARY KEY, label VARCHAR);
        INSERT INTO suppliers SELECT i, 's' FROM range(100, 400) t(i);
        CREATE TABLE orders (
          id INTEGER PRIMARY KEY, cust VARCHAR, buyer_id INTEGER,
          supplier_ref INTEGER, status_code INTEGER, amount INTEGER,
          customer_id VARCHAR, owner_ref VARCHAR, parent_ref INTEGER
        );
        INSERT INTO orders SELECT
          i,
          'C-' || lpad(((i % 150) + 1)::VARCHAR, 4, '0'),
          100 + (i % 250),
          100 + (i % 250),
          i % 9,
          100 + (i % 800),
          'C-' || lpad(((i % 150) + 1)::VARCHAR, 4, '0'),
          CASE WHEN i % 2 = 0 THEN 'C-' || lpad(((i % 150) + 1)::VARCHAR, 4, '0')
               ELSE 'X-' || i END,
          500 + (i % 400)
        FROM range(1, 1001) t(i);
        """)
    con.close()
    return path


def _discover(path: Path) -> Any:
    manager = DatabaseManager()
    assert manager.connect_duckdb(database_path=str(path))
    tables = list(
        manager.analyze_tables(
            ["customers", "products", "suppliers", "orders"], "main"
        ).values()
    )
    return run(manager, tables, "main")


def test_a_key_that_alone_holds_the_values_is_found(shop: Path) -> None:
    found = {
        f"{j.table}.{j.column}": (f"{j.target_table}.{j.target_column}", j.named)
        for j in _discover(shop).found
    }
    assert found == {
        # Text ids only customers hold.
        "orders.cust": ("customers.id", True),
        # products and suppliers both hold them; the name picks suppliers.
        "orders.supplier_ref": ("suppliers.id", True),
    }


def test_what_the_values_cannot_settle_stays_unlinked(shop: Path) -> None:
    discovery = _discover(shop)
    linked = {f"{j.table}.{j.column}" for j in discovery.found}
    # Held by products and suppliers alike, named like neither.
    assert discovery.ambiguous == ["orders.buyer_id"]
    # 0..8: shorter than three characters, as in every small key.
    assert "orders.status_code" not in linked
    # Half its values are no customer's.
    assert "orders.owner_ref" not in linked
    # Only the table's own ids hold these: never a match.
    assert "orders.parent_ref" not in linked
    # Linked by its name, and no reference by its name.
    assert "orders.customer_id" not in linked
    assert "orders.amount" not in linked


def test_lookups_stop_at_the_limit(shop: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two lookups: one for cust, whose only text key is customers.id, and
    # one for buyer_id, against products. suppliers, which holds its values
    # too, is skipped: products is no known unique match, and buyer_id is
    # linked to nothing rather than to it.
    monkeypatch.setattr(join_discovery, "MAX_CHECKS", 2)
    discovery = _discover(shop)
    assert discovery.skipped_checks > 0
    assert [f"{j.table}.{j.column}" for j in discovery.found] == ["orders.cust"]
    assert discovery.ambiguous == []


# --- The ontology ----------------------------------------------------------------


def test_the_ontology_records_a_found_join_as_inferred() -> None:
    orders = _table(
        "orders",
        {"id": "INTEGER", "cust": "VARCHAR", "customer_id": "VARCHAR"},
    )
    customers = _table("customers", {"id": "VARCHAR", "name": "VARCHAR"})
    extra = [
        InferredRelationship("orders", "cust", "customers", "id", "high", PATTERN),
        # A column names already link keeps that link.
        InferredRelationship(
            "orders", "customer_id", "products", "id", "medium", PATTERN
        ),
    ]
    generator = OntologyGenerator(base_uri="http://example.com/ontology/")
    generator.generate_from_schema([orders, customers], extra_relationships=extra)
    graph = generator.graph
    patterns = {
        str(graph.value(s, generator.oba_ns.foreignKeyColumn)): (
            str(o),
            str(graph.value(s, generator.oba_ns.referencedTable)),
        )
        for s, o in graph.subject_objects(generator.oba_ns.inferencePattern)
    }
    assert patterns["cust"] == (PATTERN, "customers")
    assert patterns["customer_id"][1] == "customers"


def test_two_inferred_keys_to_one_table_are_two_relationships() -> None:
    # The second was dropped: both were named orders_has_customers.
    orders = _table(
        "orders",
        {
            "id": "INTEGER",
            "billing_customer_id": "INTEGER",
            "shipping_customer_id": "INTEGER",
        },
    )
    customers = _table("customers", {"id": "INTEGER", "name": "VARCHAR"})
    generator = OntologyGenerator(base_uri="http://example.com/ontology/")
    generator.generate_from_schema([orders, customers])
    graph = generator.graph
    keys = {
        str(column): str(prop).rsplit("/", 1)[-1]
        for prop, column in graph.subject_objects(generator.oba_ns.foreignKeyColumn)
    }
    assert set(keys) == {"billing_customer_id", "shipping_customer_id"}
    assert len(set(keys.values())) == 2
    # Each has its own inverse.
    inverses = {
        str(graph.value(prop, OWL.inverseOf))
        for prop in graph.subjects(generator.oba_ns.foreignKeyColumn, None)
    }
    assert len(inverses) == 2


def test_found_joins_are_inferred_relationships() -> None:
    discovery = join_discovery.Discovery(
        found=[
            join_discovery.FoundJoin("orders", "cust", "customers", "id", 1.0, True),
            join_discovery.FoundJoin("orders", "acct", "accounts", "no", 0.95, False),
        ],
        ambiguous=[],
    )
    assert [
        (r.column, r.confidence, r.pattern_matched) for r in as_inferred(discovery)
    ] == [
        ("cust", "high", PATTERN),
        ("acct", "medium", PATTERN),
    ]
    assert as_inferred(None) == []


# --- Through the tools -----------------------------------------------------------


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shop: Path) -> Path:
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
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(shop))
    yield tmp_path
    fresh.cleanup()


def _saved_ontology(folder: Path) -> str:
    return next(folder.rglob("ontology_*.ttl")).read_text()


async def _generate(enabled: bool, monkeypatch: pytest.MonkeyPatch) -> str:
    if enabled:
        monkeypatch.setenv(DISCOVER_VARIABLE, "true")
    else:
        monkeypatch.delenv(DISCOVER_VARIABLE, raising=False)
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
        result = await client.call_tool(
            "generate_ontology", {**on, "schema_name": "main"}
        )
        return str(result.data)


async def test_generate_ontology_links_what_the_data_says(
    connected: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _generate(True, monkeypatch)
    assert "JOINS FOUND IN THE DATA: 2" in result
    assert "orders.cust -> customers.id" in result
    assert "orders.buyer_id" in result  # left unlinked, and said so
    ttl = _saved_ontology(connected)
    assert PATTERN in ttl


async def test_off_by_default(connected: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _generate(False, monkeypatch)
    assert "JOINS FOUND IN THE DATA" not in result
    ttl = _saved_ontology(connected)
    assert PATTERN not in ttl
