"""Rediscovering a schema replaces what changed, rather than adding to it.

A schema is rediscovered precisely when it has changed, and the derived data
must follow. Two halves used to keep the old state: the vector store wrote
with ChromaDB's `add`, which keeps the first write for an id it already holds,
so a re-commented table kept answering searches with its old description; and
the relationship graph merged foreign keys, so a constraint dropped from the
database kept offering a join path forever.
"""

import types

import numpy as np
import pytest

from src.graphrag.retriever import GraphRetriever
from src.graphrag.vector_store_chromadb import ChromaDBVectorStore


def _table(
    name: str,
    foreign_keys: tuple[tuple[str, str], ...] = (),
    schema: str = "public",
) -> dict:
    return {
        "name": name,
        "schema": schema,
        "columns": [],
        "foreign_keys": [
            {"column": column, "referenced_table": target, "referenced_column": "id"}
            for column, target in foreign_keys
        ],
    }


# --- the relationship graph ---


def test_a_dropped_foreign_key_stops_offering_a_join_path():
    retriever = GraphRetriever()
    retriever.add_to_graph(
        [_table("orders", (("customer_id", "customers"),)), _table("customers")]
    )
    assert sorted(retriever.graph.edges()) == [("public.orders", "public.customers")]

    retriever.add_to_graph([_table("orders"), _table("customers")])

    assert sorted(retriever.graph.edges()) == []


def test_a_changed_foreign_key_target_moves_the_edge():
    retriever = GraphRetriever()
    retriever.add_to_graph(
        [
            _table("orders", (("party_id", "customers"),)),
            _table("customers"),
            _table("parties"),
        ]
    )

    retriever.add_to_graph(
        [
            _table("orders", (("party_id", "parties"),)),
            _table("customers"),
            _table("parties"),
        ]
    )

    assert sorted(retriever.graph.edges()) == [("public.orders", "public.parties")]


def test_tables_outside_this_discovery_keep_their_relationships():
    """Discovery is per schema, and GraphRAG accumulates across them. Another
    schema's edges are none of this discovery's business."""
    retriever = GraphRetriever()
    retriever.add_to_graph(
        [
            _table("orders", (("customer_id", "customers"),)),
            _table("customers"),
            _table("shipments", (("order_id", "orders"),)),
        ]
    )

    retriever.add_to_graph([_table("orders"), _table("customers")])

    assert sorted(retriever.graph.edges()) == [("public.shipments", "public.orders")]


def test_an_unchanged_schema_keeps_its_relationships():
    retriever = GraphRetriever()
    tables = [_table("orders", (("customer_id", "customers"),)), _table("customers")]
    retriever.add_to_graph(tables)

    retriever.add_to_graph(tables)

    assert sorted(retriever.graph.edges()) == [("public.orders", "public.customers")]


# --- the vector store ---


@pytest.fixture
def store(monkeypatch, tmp_path) -> ChromaDBVectorStore:
    monkeypatch.setattr("src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path)
    return ChromaDBVectorStore(dimension=8, connection_id="c", schema_name="main")


def _element(element_id: str, description: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        element_id=element_id,
        embedding=np.ones(8, dtype=float),
        element_type="table",
        name=element_id,
        description=description,
        metadata={},
    )


def test_a_changed_description_replaces_the_old_one(store):
    store.add_elements_batch([_element("orders", "original comment")])

    store.add_elements_batch([_element("orders", "changed comment")])

    assert store.get_by_id("orders").description == "changed comment"


def test_rediscovery_does_not_duplicate_an_element(store):
    store.add_elements_batch([_element("orders", "first")])

    store.add_elements_batch([_element("orders", "second")])

    assert store.collection.count() == 1


def test_other_elements_survive_a_rediscovery(store):
    store.add_elements_batch([_element("orders", "o"), _element("customers", "c")])

    store.add_elements_batch([_element("orders", "changed")])

    assert store.get_by_id("customers").description == "c"
    assert store.collection.count() == 2


def test_discovering_another_schema_does_not_delete_this_ones_relationships():
    """Two schemas holding an `orders` are two nodes, so `sales.orders` keeps
    its foreign keys when `archive` is discovered.

    This used to need a guard -- keep the edges of a name last seen under a
    different schema -- because both schemas shared one node. Identity carries
    the schema now, so the replacement simply does not reach the other one."""
    retriever = GraphRetriever()
    retriever.add_to_graph(
        [
            _table("orders", (("customer_id", "customers"),), schema="sales"),
            _table("customers", schema="sales"),
        ]
    )

    retriever.add_to_graph(
        [_table("orders", schema="archive"), _table("invoices", schema="archive")]
    )

    assert ("sales.orders", "sales.customers") in retriever.graph.edges()
    assert "archive.orders" in retriever.graph
    assert list(retriever.graph.out_edges("archive.orders")) == []


def test_rediscovering_the_same_schema_still_replaces():
    """The guard above must not disable the replacement in the normal case."""
    retriever = GraphRetriever()
    retriever.add_to_graph(
        [
            _table("orders", (("customer_id", "customers"),), schema="sales"),
            _table("customers", schema="sales"),
        ]
    )

    retriever.add_to_graph(
        [_table("orders", schema="sales"), _table("customers", schema="sales")]
    )

    assert sorted(retriever.graph.edges()) == []
