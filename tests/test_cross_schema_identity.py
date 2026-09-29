"""Two schemas may hold a table of the same name without becoming one table.

The graph keyed nodes by bare name and vector ids came from the same names, so
`sales.orders` and `archive.orders` were one node and one set of vectors:
whichever schema was discovered last described both, and a join path from one
could leave through the other's foreign keys.

Identity carries the schema now. A table without one keeps its bare name --
nothing about it is ambiguous, and that is most callers -- so the change is
confined to the case that actually collides.
"""

from typing import Any

import pytest

from src.graphrag.identity import (
    display_name,
    qualified,
    resolve,
    split,
)
from src.graphrag.manager import GraphRAGManager
from src.graphrag.retriever import GraphRetriever


def _table(
    name: str, schema: str | None, references: str | None = None, comment: str = ""
) -> dict[str, Any]:
    return {
        "name": name,
        "schema": schema,
        "comment": comment or f"{schema}.{name}",
        "columns": [
            {"name": "id", "data_type": "INTEGER", "is_primary_key": True},
            {"name": "amount", "data_type": "DECIMAL"},
        ],
        "foreign_keys": (
            [
                {
                    "column": "ref_id",
                    "referenced_table": references,
                    "referenced_column": "id",
                }
            ]
            if references
            else []
        ),
    }


class TestTheIdentity:
    """What a table is called inside the index."""

    def test_a_schema_qualifies_the_name(self):
        assert qualified("sales", "orders") == "sales.orders"

    def test_no_schema_leaves_the_name_alone(self):
        assert qualified(None, "orders") == "orders"
        assert qualified("", "orders") == "orders"

    def test_it_comes_apart_again(self):
        assert split("sales.orders") == ("sales", "orders")
        assert split("orders") == (None, "orders")

    def test_the_display_name_drops_the_schema(self):
        assert display_name("sales.orders") == "orders"
        assert display_name("orders") == "orders"


class TestResolution:
    """Names arrive as a model wrote them."""

    KNOWN = {"sales.orders", "archive.orders", "sales.customers", "stock"}

    def test_an_exact_identity_wins(self):
        assert resolve("sales.orders", self.KNOWN).identity == "sales.orders"

    def test_a_unique_bare_name_resolves(self):
        assert resolve("customers", self.KNOWN).identity == "sales.customers"

    def test_a_schemaless_table_resolves_as_itself(self):
        assert resolve("stock", self.KNOWN).identity == "stock"

    def test_an_ambiguous_name_is_reported_not_picked(self):
        found = resolve("orders", self.KNOWN)

        assert found.identity is None
        assert found.ambiguous is True
        assert found.candidates == ["archive.orders", "sales.orders"]

    def test_the_current_schema_breaks_the_tie(self):
        found = resolve("orders", self.KNOWN, current_schema="archive")

        assert found.identity == "archive.orders"

    def test_a_name_nobody_has_resolves_to_nothing(self):
        found = resolve("nosuchtable", self.KNOWN)

        assert found.identity is None
        assert found.ambiguous is False
        assert found.candidates == []


class TestTheGraphKeepsThemApart:
    """The collision, in the graph."""

    @pytest.fixture
    def retriever(self) -> GraphRetriever:
        instance = GraphRetriever()
        instance.add_to_graph(
            [
                _table(
                    "orders", "sales", references="customers", comment="live orders"
                ),
                _table("customers", "sales"),
            ]
        )
        instance.add_to_graph(
            [
                _table("orders", "archive", comment="orders we no longer bill"),
                _table("invoices", "archive", references="orders"),
            ]
        )
        return instance

    def test_they_are_two_nodes(self, retriever):
        assert "sales.orders" in retriever.graph
        assert "archive.orders" in retriever.graph
        assert retriever.graph.number_of_nodes() == 4

    def test_each_keeps_its_own_description(self, retriever):
        assert retriever.graph.nodes["sales.orders"]["comment"] == "live orders"
        assert (
            retriever.graph.nodes["archive.orders"]["comment"]
            == "orders we no longer bill"
        )

    def test_each_keeps_its_own_relationships(self, retriever):
        assert ("sales.orders", "sales.customers") in retriever.graph.edges()
        assert ("archive.invoices", "archive.orders") in retriever.graph.edges()
        # The archive's invoices must not reach the live customers.
        assert retriever.find_join_path("archive.invoices", "sales.customers") is None

    def test_a_foreign_key_resolves_inside_its_own_schema(self, retriever):
        """`invoices` references `orders` -- its own schema's, not the other's."""
        targets = list(retriever.graph.successors("archive.invoices"))

        assert targets == ["archive.orders"]

    def test_the_display_name_is_still_the_table_name(self, retriever):
        exported = retriever.export_graph_for_visualization()
        labels = {node["id"]: node["label"] for node in exported["nodes"]}

        assert labels["sales.orders"] == "orders"
        assert labels["archive.orders"] == "orders"

    def test_a_bare_name_that_fits_two_schemas_is_reported(self, retriever):
        found = retriever.resolve_name("orders")

        assert found.ambiguous is True
        assert found.candidates == ["archive.orders", "sales.orders"]

    def test_a_bare_name_that_fits_one_still_works(self, retriever):
        path = retriever.find_join_path("orders", "customers")

        # `customers` is unique, `orders` is not, so nothing is guessed.
        assert path is None
        assert retriever.find_join_path("sales.orders", "customers") is not None


class TestTheIndexKeepsThemApart:
    """The collision, in the vector store."""

    @pytest.fixture
    def manager(self, tmp_path, monkeypatch) -> GraphRAGManager:
        monkeypatch.setattr(
            "src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path / "chroma"
        )
        instance = GraphRAGManager(
            embedding_model="tfidf", connection_id="collide", schema_name="sales"
        )
        instance.initialize_from_schema(
            [_table("orders", "sales", comment="live orders")], schema_name="sales"
        )
        instance.accumulate_schema(
            [_table("orders", "archive", comment="archived orders")],
            schema_name="archive",
        )
        return instance

    def test_both_tables_are_indexed(self, manager):
        ids = set(manager.vector_store.collection.get()["ids"])

        assert {"sales.orders", "archive.orders"} <= ids
        assert {"sales.orders.amount", "archive.orders.amount"} <= ids

    def test_each_keeps_its_own_description(self, manager):
        live = manager.vector_store.get_by_id("sales.orders")
        archived = manager.vector_store.get_by_id("archive.orders")

        assert "live orders" in live.description
        assert "archived orders" in archived.description

    def test_a_column_records_the_table_a_query_must_name(self, manager):
        column = manager.vector_store.get_by_id("sales.orders.amount")

        assert column.metadata["table"] == "sales.orders"
        assert column.metadata["table_name"] == "orders"
        assert column.metadata["schema"] == "sales"

    def test_search_results_show_the_bare_name_and_the_identity(self, manager):
        results = manager.search_schema("orders", top_k=4)

        by_id = {r["element"]["id"]: r["element"]["name"] for r in results}
        assert by_id.get("sales.orders") == "orders"
        assert by_id.get("archive.orders") == "orders"


class TestNothingChangesWithoutSchemas:
    """Most callers have no schemas, and nothing about them is ambiguous."""

    def test_nodes_keep_their_bare_names(self):
        retriever = GraphRetriever()
        retriever.build_graph(
            [_table("orders", None, references="customers"), _table("customers", None)]
        )

        assert set(retriever.graph) == {"orders", "customers"}
        assert ("orders", "customers") in retriever.graph.edges()

    def test_element_ids_keep_their_bare_names(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path / "chroma"
        )
        manager = GraphRAGManager(
            embedding_model="tfidf", connection_id="plain", schema_name="default"
        )
        manager.initialize_from_schema([_table("orders", None)], schema_name="default")

        ids = set(manager.vector_store.collection.get()["ids"])

        assert "orders" in ids
        assert "orders.amount" in ids
