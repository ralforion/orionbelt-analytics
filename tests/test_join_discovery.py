"""Joins found in the data where column names do not say them.

orders.cust holds customers' ids, but names no table: inference from names
links nothing. With OBA_DISCOVER_JOINS_BY_DATA, a column that looks like a
reference -- a key word, or a word naming a table by abbreviation or synonym --
is checked against other tables' keys. One key holding its values, confirmed
on all rows, links it; several are listed for the client to confirm one with
confirm_join.
"""

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastmcp import Client
from rdflib.namespace import OWL

import src.join_discovery as join_discovery
import src.main as main_module
import src.server_state as state_module
from src.database_manager import ColumnInfo, DatabaseManager, TableInfo
from src.handlers import connection as connection_handler
from src.join_discovery import (
    ABBREVIATION,
    CONFIRMED_PATTERN,
    DISCOVER_VARIABLE,
    PATTERN,
    SYNONYM,
    WORD,
    Names,
    abbreviates,
    as_inferred,
    candidates,
    expansions,
    held_query,
    kind,
    run,
)
from src.main import mcp
from src.ontology_generator import InferredRelationship, OntologyGenerator
from src.server_state import ServerState

# --- Names: abbreviations, then synonyms -----------------------------------------


@pytest.mark.parametrize(
    ("short", "word", "expected"),
    [
        ("cust", "customer", True),  # its start
        ("emp", "employee", True),
        ("ord", "order", True),
        ("cstmr", "customer", True),  # its letters in order
        ("acct", "account", True),
        ("cat", "category", True),
        ("cat", "catalog", True),
        ("ct", "category", False),  # too short
        ("cat", "contract", False),  # three letters, not its start
        ("tsuc", "customer", False),  # not in order
        ("ustomer", "customer", False),  # another first letter
        ("customer", "customer", False),  # the word itself, not an abbreviation
    ],
)
def test_an_abbreviation_is_a_words_start_or_its_letters_in_order(
    short: str, word: str, expected: bool
) -> None:
    assert abbreviates(short, word) is expected


TABLES = ["customers", "suppliers", "products", "employees", "categories", "catalogs"]


def _named(column: str, embed: Any = None) -> dict[str, tuple[str, float]]:
    return {
        table: (match.how, match.score)
        for table, match in Names(TABLES, embed)(column).items()
    }


def test_words_and_abbreviations_come_from_the_schema() -> None:
    assert _named("supplier_ref") == {"suppliers": (WORD, 1.0)}
    assert _named("cust") == {"customers": (ABBREVIATION, 1.0)}
    assert _named("CstmrNo") == {"customers": (ABBREVIATION, 1.0)}
    assert _named("emp_key") == {"employees": (ABBREVIATION, 1.0)}
    # One abbreviation, two tables: both named, neither preferred.
    assert _named("cat") == {
        "categories": (ABBREVIATION, 1.0),
        "catalogs": (ABBREVIATION, 1.0),
    }
    # A synonym needs the model: without one, nothing.
    assert _named("vendor") == {}


def test_a_word_expands_to_the_english_words_it_may_stand_for() -> None:
    # From wordfreq, most frequent first: the word itself when it is one,
    # then the words it abbreviates, three-letter ones in order included.
    assert expansions("vendor")[:2] == ("vendor", "vendors")
    assert expansions("vndr")[0] == "vendor"
    assert "payment" in expansions("pmt")
    assert "manager" in expansions("mgr")
    assert len(expansions("pmt")) <= join_discovery.MAX_EXPANSIONS


def test_an_expansion_that_is_a_table_word_names_the_table_without_a_model() -> None:
    names = Names(["managers", "payments", "orders"])
    assert {t: (m.how, m.via) for t, m in names("mgr_id").items()} == {
        "managers": (ABBREVIATION, "manager")
    }
    assert {t: (m.how, m.via) for t, m in names("pmt_ref").items()} == {
        "payments": (ABBREVIATION, "payment")
    }
    # Three letters against the table's own words are not enough: pmt keeps
    # payment's letters in order, but only the dictionary vouches for it.
    assert not abbreviates("pmt", "payment")


def _embedding(similar: dict[str, dict[str, float]]) -> Any:
    """A model whose vectors give exactly the cosines in *similar*.

    Each table is a unit axis; a word is the mix of axes its similarities
    name, the rest of its length on an axis of its own.
    """
    axes = {name: i for i, name in enumerate(TABLES)}

    def embed(texts: list[str]) -> list[np.ndarray]:
        vectors = []
        for text in texts:
            vector = np.zeros(len(TABLES) + 1)
            if text in axes:
                vector[axes[text]] = 1.0
            else:
                for table, score in similar.get(text, {}).items():
                    vector[axes[table]] = score
                rest = 1.0 - float(vector @ vector)
                vector[-1] = rest**0.5 if rest > 0 else 0.0
            vectors.append(vector)
        return vectors

    return embed


def test_a_synonym_is_the_models_clear_favourite() -> None:
    model = _embedding(
        {
            "vendor": {"suppliers": 0.73, "customers": 0.53},
            "buyer": {"customers": 0.48, "products": 0.46},
            "owner": {"employees": 0.43},
        }
    )
    assert _named("vendor", model) == {"suppliers": (SYNONYM, 0.73)}
    # An abbreviation of it reaches the same table through its expansion.
    assert _named("vndr", model) == {"suppliers": (SYNONYM, 0.73)}
    # No clear lead, or too far from every table: no synonym.
    assert _named("buyer_id", model) == {}
    assert _named("owner_ref", model) == {}
    # Abbreviations first: the model is not asked about cust.
    assert _named("cust", model) == {"customers": (ABBREVIATION, 1.0)}


def test_a_model_that_fails_is_not_asked_again() -> None:
    calls = []

    def broken(texts: list[str]) -> list[Any]:
        calls.append(texts)
        raise RuntimeError("no model")

    names = Names(TABLES, broken)
    assert names("vendor") == {}
    assert names("client") == {}
    assert len(calls) == 1


def test_synonyms_come_only_from_a_model_graphrag_has_loaded() -> None:
    from types import SimpleNamespace

    from src.handlers.ontology_generation import _synonyms

    def embedder(model: str) -> Any:
        return SimpleNamespace(embedding_model=model, _embed_texts=lambda t: t)

    def session(manager: Any) -> Any:
        return SimpleNamespace(graphrag_manager=manager)

    # GraphRAG not up: discovery loads no model of its own.
    assert _synonyms(session(None)) is None
    # TF-IDF knows no synonyms.
    assert _synonyms(session(SimpleNamespace(embedder=embedder("tfidf")))) is None
    loaded = embedder("minilm")
    assert _synonyms(session(SimpleNamespace(embedder=loaded))) is loaded._embed_texts


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
            "cust": "VARCHAR",  # abbreviates customers
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
        -- Its first thousand rows reference customers; the rest do not, so a
        -- sample holds it and all rows do not.
        CREATE TABLE visits (id INTEGER PRIMARY KEY, cust_ref VARCHAR);
        INSERT INTO visits SELECT i,
          CASE WHEN i <= 1000 THEN 'C-' || lpad(((i % 150) + 1)::VARCHAR, 4, '0')
               ELSE 'Z-' || i END
        FROM range(1, 3001) t(i);
        """)
    con.close()
    return path


def _discover(path: Path) -> Any:
    manager = DatabaseManager()
    assert manager.connect_duckdb(database_path=str(path))
    tables = list(
        manager.analyze_tables(
            ["customers", "products", "suppliers", "orders", "visits"], "main"
        ).values()
    )
    return run(manager, tables, "main")


def test_a_key_that_alone_holds_the_values_is_confirmed_and_linked(
    shop: Path,
) -> None:
    discovery = _discover(shop)
    assert [
        (f"{j.table}.{j.column}", f"{j.target_table}.{j.target_column}")
        for j in discovery.found
    ] == [("orders.cust", "customers.id")]
    (found,) = discovery.found
    assert found.record.status == "confirmed"
    assert found.record.checked_rows == 1000


def test_what_the_data_cannot_settle_is_left_to_confirm(shop: Path) -> None:
    choices = {
        f"{c.table}.{c.column}": [
            (k.table, k.name.how if k.name else None) for k in c.keys
        ]
        for c in _discover(shop).to_confirm
    }
    assert choices == {
        # Three dense ids hold these numbers alike, named by none.
        "orders.buyer_id": [("products", None), ("suppliers", None), ("visits", None)],
        # Named by a word: suppliers first.
        "orders.supplier_ref": [
            ("suppliers", WORD),
            ("products", None),
            ("visits", None),
        ],
        # 500..899: only visits' ids, 1..3000, hold them -- a coincidence the
        # data confirms on all rows. Numbers no word names are not linked.
        "orders.parent_ref": [("visits", None)],
    }


def test_a_sample_is_no_proof(shop: Path) -> None:
    # visits.cust_ref: its first thousand rows reference customers, all rows
    # do not. One key held the sample; all rows refute it.
    discovery = _discover(shop)
    assert discovery.unconfirmed == [("visits.cust_ref", "customers.id", "refuted")]
    assert "visits.cust_ref" not in {f"{j.table}.{j.column}" for j in discovery.found}


def test_what_the_values_cannot_settle_stays_unlinked(shop: Path) -> None:
    discovery = _discover(shop)
    touched = {f"{j.table}.{j.column}" for j in discovery.found} | {
        f"{c.table}.{c.column}" for c in discovery.to_confirm
    }
    # 0..8: shorter than three characters, as in every small key.
    assert "orders.status_code" not in touched
    # Half its values are no customer's.
    assert "orders.owner_ref" not in touched
    # Its own table's ids hold these too, and are never a match: only
    # visits is offered.
    (parent,) = [c for c in discovery.to_confirm if c.column == "parent_ref"]
    assert [k.table for k in parent.keys] == ["visits"]
    # Linked by its name, and no reference by its name.
    assert "orders.customer_id" not in touched
    assert "orders.amount" not in touched


def test_lookups_stop_at_the_limit(shop: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two lookups: one for cust, whose only text key is customers.id, and
    # one for buyer_id, against products. suppliers, which holds its values
    # too, is skipped: neither is known to be the only key, and buyer_id is
    # neither linked nor listed.
    monkeypatch.setattr(join_discovery, "MAX_CHECKS", 2)
    discovery = _discover(shop)
    assert discovery.skipped_checks > 0
    assert [f"{j.table}.{j.column}" for j in discovery.found] == ["orders.cust"]
    assert discovery.to_confirm == []


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


def test_found_joins_are_inferred_relationships(shop: Path) -> None:
    discovery = _discover(shop)
    assert [
        (r.column, r.confidence, r.pattern_matched) for r in as_inferred(discovery)
    ] == [("cust", "high", PATTERN)]
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


def _ontologies(folder: Path) -> list[str]:
    return [
        p.read_text()
        for p in sorted(folder.rglob("ontology_*.ttl"), key=lambda p: p.stat().st_mtime)
    ]


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


async def test_found_joins_are_linked_and_ties_confirmed_by_the_client(
    connected: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DISCOVER_VARIABLE, "true")
    async with Client(mcp) as client:
        on = await _connect(client)
        await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        generated = await client.call_tool(
            "generate_ontology", {**on, "schema_name": "main"}
        )
        result = str(generated.data)
        assert "JOINS FOUND IN THE DATA: 1" in result
        assert "orders.cust -> customers.id" in result
        assert "JOINS TO CONFIRM: 3" in result
        assert "orders.supplier_ref: suppliers.id (100%, named by word)" in result
        assert "visits.cust_ref -> customers.id (refuted)" in result
        first = _ontologies(connected)[-1]
        assert CONFIRMED_PATTERN not in first
        # The found join carries the check that confirmed it.
        assert PATTERN in first and '"confirmed"' in first

        # The client picks suppliers: checked on all rows, linked.
        linked = _data(
            await client.call_tool(
                "confirm_join",
                {
                    **on,
                    "table": "orders",
                    "column": "supplier_ref",
                    "target_table": "suppliers",
                },
            )
        )
        assert linked["linked"] is True, linked
        assert linked["status"] == "confirmed"
        assert CONFIRMED_PATTERN in _ontologies(connected)[-1]

        # A join the data does not confirm is reported, not added.
        refused = _data(
            await client.call_tool(
                "confirm_join",
                {
                    **on,
                    "table": "orders",
                    "column": "owner_ref",
                    "target_table": "customers",
                },
            )
        )
        assert refused["linked"] is False
        assert refused["status"] in ("partial", "refuted")

        # Regenerated, the confirmed join is still there, with its verdict.
        await client.call_tool("reset_cache", {**on, "cache_type": "ontology"})
        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})
        regenerated = _ontologies(connected)[-1]
        assert CONFIRMED_PATTERN in regenerated
        assert "supplier_ref" in regenerated and '"confirmed"' in regenerated


async def test_off_by_default(connected: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DISCOVER_VARIABLE, raising=False)
    async with Client(mcp) as client:
        on = await _connect(client)
        await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        generated = await client.call_tool(
            "generate_ontology", {**on, "schema_name": "main"}
        )
    assert "JOINS FOUND IN THE DATA" not in str(generated.data)
    assert PATTERN not in _ontologies(connected)[-1]
