"""Near-copies: tables with nearly the same columns, named where tables are shown.

A current table and its archive, yearly copies, staging beside production: a
query on the wrong one runs and returns plausible rows. discover_schema,
get_table_details and graphrag_query_context name each table's near-copies so
the model can check which one the question means.
"""

import json
import random
import re
import threading
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import src.graphrag.manager as manager_module
import src.main as main_module
import src.server_state as state_module
from src.database_manager import ColumnInfo, TableInfo
from src.graphrag.manager import GraphRAGManager
from src.graphrag.near_copies import (
    MAX_LISTED_COLUMNS,
    CopyIndex,
    _similar,
    is_bookkeeping,
    listed,
)
from src.handlers import connection as connection_handler
from src.handlers.schema import MAX_NEAR_COPY_PAIRS, _near_copy_pairs
from src.main import mcp
from src.server_state import ServerState

ORDER = ["id", "customer_id", "order_date", "status", "amount", "currency"]


# --- The rule -----------------------------------------------------------------


def _copies(tables: dict[str, list[str]], of: str) -> list[str]:
    return [copy.table for copy in CopyIndex(tables).of(of)]


def test_an_archive_with_one_more_column_is_a_near_copy() -> None:
    tables = {"orders": ORDER, "orders_2019": [*ORDER, "archived_at"]}
    (copy,) = CopyIndex(tables).of("orders")
    assert copy.table == "orders_2019"
    assert copy.shared == 6
    assert copy.only_here == ()
    assert copy.only_there == ("archived_at",)


def test_ids_and_audit_columns_alone_make_no_copy() -> None:
    tables = {
        "orders": ["id", "created_at", "amount", "status"],
        "customers": ["id", "created_at", "name", "email"],
    }
    assert _copies(tables, "orders") == []


def test_bookkeeping_columns_are_no_evidence() -> None:
    # Three shared columns and Jaccard 0.6 -- all of them bookkeeping.
    tables = {
        "categories": ["id", "created_at", "updated_at", "name"],
        "payments": ["id", "created_at", "updated_at", "amount"],
    }
    assert _copies(tables, "categories") == []
    # A copy is still one with its bookkeeping columns around it.
    audited = [*ORDER, "created_at", "updated_at", "tenant_id"]
    tables = {"orders": audited, "orders_2019": [*audited, "archived_at"]}
    (copy,) = CopyIndex(tables).of("orders")
    assert copy.shared == len(audited)  # reported: every column both have
    assert copy.only_there == ("archived_at",)


@pytest.mark.parametrize(
    "column",
    [
        "id",
        "ID",
        "uuid",
        "created_at",
        "CREATED_AT",
        "updated_by",
        "modified_on",
        "last_modified",
        "last_updated_by",
        "date_created",
        "creation_date",
        "update_time",
        "deleted_at",
        "is_deleted",
        "version",
        "tenant_id",
        "_loaded_at",
        "_fivetran_synced",
        "etl_batch_id",
        "dw_insert_ts",
        "record_source",
        "load_date",
    ],
)
def test_bookkeeping_names(column: str) -> None:
    assert is_bookkeeping(column)


@pytest.mark.parametrize(
    "column",
    [
        "customer_id",
        "order_date",
        "amount",
        "name",
        "status",
        "archived_at",
        "loaded_quantity",
        "load_weight",
        "batch_number",
        "version_name",
        "created_by_name",
    ],
)
def test_content_names(column: str) -> None:
    assert not is_bookkeeping(column)


def test_a_lookup_copied_into_a_wide_table_is_no_copy() -> None:
    # All three of the lookup's columns are in the fact table, which is
    # more than twice as wide: a denormalised copy of a column, not of a table.
    wide = ["country_code", "country_name", "region"] + [f"m{i}" for i in range(10)]
    tables = {"countries": ["country_code", "country_name", "region"], "facts": wide}
    assert _copies(tables, "countries") == []


def test_most_of_the_narrower_table_is_enough_at_similar_widths() -> None:
    # 8 of 10 shared, the other has 18: Jaccard 8/20 = 0.4 alone would not do.
    narrow = [f"c{i}" for i in range(10)]
    wide = [f"c{i}" for i in range(8)] + [f"w{i}" for i in range(10)]
    assert _copies({"narrow": narrow, "wide": wide}, "narrow") == ["wide"]
    assert not _similar(shared=8, width=10, other_width=21)  # over twice as wide


def test_names_are_compared_without_case() -> None:
    tables = {"pg.orders": ORDER, "sf.ORDERS": [c.upper() for c in ORDER]}
    (copy,) = CopyIndex(tables).of("pg.orders")
    assert copy.table == "sf.ORDERS" and copy.shared == len(ORDER)


def test_closest_first_and_at_most_three() -> None:
    tables = {
        "orders": ORDER,
        "orders_copy": ORDER,
        "orders_2019": [*ORDER, "archived_at"],
        "orders_2018": [*ORDER, "archived_at", "batch"],
        "orders_2017": [*ORDER, "archived_at", "batch", "note"],
    }
    assert _copies(tables, "orders") == ["orders_copy", "orders_2019", "orders_2018"]


def test_pairs_are_listed_once() -> None:
    tables = {"orders": ORDER, "orders_2019": [*ORDER, "archived_at"]}
    pairs = CopyIndex(tables).pairs(["orders", "orders_2019"])
    assert [(a, c.table) for a, c in pairs] == [("orders", "orders_2019")]


def test_discovery_counts_every_pair_it_leaves_out() -> None:
    # Ten identical tables are 45 pairs, not three per table.
    tables = [
        TableInfo(
            f"copy_{i}",
            "main",
            [ColumnInfo(c, "VARCHAR", True, False, False) for c in ORDER],
            [],
            [],
        )
        for i in range(10)
    ]
    catalog = {f"main.{t.name}": t for t in tables}
    assert len(CopyIndex(dict.fromkeys(catalog, ORDER)).pairs(catalog)) == 45
    section = _near_copy_pairs(catalog, tables)
    assert len(section["near_copies"]) == MAX_NEAR_COPY_PAIRS
    assert section["near_copies_omitted"] == 45 - MAX_NEAR_COPY_PAIRS


def test_long_column_lists_are_cut() -> None:
    columns = tuple(f"c{i}" for i in range(MAX_LISTED_COLUMNS + 3))
    assert listed(columns)[-1] == "... and 3 more"
    assert len(listed(columns)) == MAX_LISTED_COLUMNS + 1
    assert listed(columns[:2]) == ["c0", "c1"]


@pytest.mark.parametrize("seed", range(8))
def test_the_index_finds_what_comparing_every_pair_finds(seed: int) -> None:
    # The prefix filter must miss nothing: compare with the brute force on
    # random catalogs with shared audit columns and families of copies.
    rng = random.Random(seed)
    vocabulary = [f"c{i}" for i in range(60)]
    common = ["id", "created_at", "updated_at"]
    tables: dict[str, list[str]] = {}
    for t in range(80):
        base = rng.sample(vocabulary, rng.randint(1, 14))
        tables[f"t{t}"] = rng.sample(common, rng.randint(0, 3)) + base
        if rng.random() < 0.3:  # a near-copy of it, with a few changes
            copy = [c for c in tables[f"t{t}"] if rng.random() > 0.2]
            tables[f"t{t}_copy"] = copy + rng.sample(vocabulary, rng.randint(0, 3))
    content = {
        k: {c.lower() for c in cols if not is_bookkeeping(c)}
        for k, cols in tables.items()
    }
    expected = {
        frozenset((a, b))
        for a, b in combinations(tables, 2)
        if _similar(len(content[a] & content[b]), len(content[a]), len(content[b]))
    }
    index = CopyIndex(tables)
    found = {
        frozenset((a, copy.table))
        for a in tables
        for copy in index.of(a, limit=len(tables))
    }
    assert expected, "the catalog should have some near-copies"
    assert found == expected


# --- GraphRAG -----------------------------------------------------------------


def _table(schema: str, name: str, columns: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "schema": schema,
        "columns": [{"name": c, "data_type": "VARCHAR"} for c in columns],
        "primary_keys": ["id"],
        "foreign_keys": [],
        "comment": None,
    }


def test_query_context_names_the_near_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPHRAG_EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(manager_module, "CHROMADB_AVAILABLE", False)
    mgr = GraphRAGManager(connection_id="test-copies", schema_name="main")
    mgr.initialize_from_schema(
        tables_info=[
            _table("main", "orders", ORDER),
            _table("main", "orders_2019", [*ORDER, "archived_at"]),
            _table("main", "customers", ["id", "name", "email", "country"]),
        ],
        schema_name="main",
    )
    context = mgr.get_query_context("orders amount", max_tables=3, max_columns=3)
    tables = {t["name"]: t for t in context["relevant_tables"]}
    assert tables["orders"]["near_copies"] == [
        {
            "table": "main.orders_2019",
            "shared_columns": 6,
            "only_here": [],
            "only_there": ["archived_at"],
        }
    ]
    assert "near_copies_note" in context
    if "customers" in tables:
        assert "near_copies" not in tables["customers"]

    # A table added later is found too: the index follows the graph.
    mgr.graph_retriever.add_to_graph([_table("staging", "orders", ORDER[:5])])
    context = mgr.get_query_context("orders amount", max_tables=3, max_columns=3)
    near = {
        c["table"]
        for t in context["relevant_tables"]
        if t["name"] == "orders"
        for c in t.get("near_copies", [])
    }
    assert "staging.orders" in near


async def test_query_context_matches_copies_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHRAG_EMBEDDING_MODEL", "tfidf")
    monkeypatch.setattr(manager_module, "CHROMADB_AVAILABLE", False)
    mgr = GraphRAGManager(connection_id="test-copies-loop", schema_name="main")
    mgr.initialize_from_schema(
        tables_info=[
            _table("main", "orders", ORDER),
            _table("main", "orders_2019", [*ORDER, "archived_at"]),
        ],
        schema_name="main",
    )
    built_on: list[int] = []
    original = CopyIndex.__init__

    def recording(self: CopyIndex, tables: Any) -> None:
        built_on.append(threading.get_ident())
        original(self, tables)

    matched_on: list[int] = []
    of = CopyIndex.of

    def recording_of(self: CopyIndex, identity: str, limit: Any = 3) -> Any:
        matched_on.append(threading.get_ident())
        return of(self, identity, limit)

    monkeypatch.setattr(CopyIndex, "__init__", recording)
    monkeypatch.setattr(CopyIndex, "of", recording_of)
    loop_thread = threading.get_ident()
    context = await mgr.aget_query_context("orders amount", 3, 3)
    tables = {t["name"]: t for t in context["relevant_tables"]}
    assert tables["orders"]["near_copies"][0]["table"] == "main.orders_2019"
    assert built_on and matched_on
    assert loop_thread not in built_on + matched_on


# --- Through the tools ----------------------------------------------------------


@pytest.fixture
def shop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    import duckdb

    path = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(path))
    columns = (
        "id INTEGER, customer_id INTEGER, order_date DATE, status VARCHAR, "
        "amount DECIMAL(10,2), currency VARCHAR"
    )
    con.execute(f"CREATE TABLE orders ({columns})")
    con.execute(f"CREATE TABLE orders_2019 ({columns}, archived_at TIMESTAMP)")
    con.execute("CREATE TABLE customers (id INTEGER, name VARCHAR, email VARCHAR)")
    con.execute("CREATE SCHEMA staging")
    con.execute(
        "CREATE TABLE staging.orders (id INTEGER, customer_id INTEGER, "
        "order_date DATE, status VARCHAR, amount DECIMAL(10,2))"
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
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(path))
    yield tmp_path
    fresh.cleanup()


def _data(result: Any) -> Any:
    return result.data if not isinstance(result.data, str) else json.loads(result.data)


async def _connect(client: Client) -> dict[str, str]:
    connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
    text = (
        connected.data
        if isinstance(connected.data, str)
        else json.dumps(connected.data)
    )
    match = re.search(r"(ob_[a-z0-9]{6})", text)
    assert match
    return {"connection": match.group(1)}


def _pairs(result: dict[str, Any]) -> set[frozenset[str]]:
    return {frozenset(p["tables"]) for p in result.get("near_copies", [])}


async def test_discovery_and_table_details_name_near_copies(shop: Path) -> None:
    async with Client(mcp) as client:
        on = await _connect(client)
        main = _data(
            await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        )
        assert _pairs(main) == {frozenset(("main.orders", "main.orders_2019"))}
        (pair,) = main["near_copies"]
        assert pair["shared_columns"] == 6
        assert pair["own_columns"]["main.orders_2019"] == ["archived_at"]
        assert "near_copies_note" in main

        # A second schema is compared with the first: staging beside main.
        staging = _data(
            await client.call_tool(
                "discover_schema",
                {**on, "schema_name": "staging", "lightweight": True},
            )
        )
        assert frozenset(("staging.orders", "main.orders")) in _pairs(staging)

        details = _data(
            await client.call_tool(
                "get_table_details",
                {**on, "table_name": "orders", "schema_name": "main"},
            )
        )
        assert [c["table"] for c in details["near_copies"]] == [
            "main.orders_2019",
            "staging.orders",
        ]
        customers = _data(
            await client.call_tool(
                "get_table_details",
                {**on, "table_name": "customers", "schema_name": "main"},
            )
        )
        assert "near_copies" not in customers
