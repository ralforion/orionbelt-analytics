"""A table dropped from the database leaves the graph and the index.

Rediscovery is how the server notices a table is gone, and nothing acted on
it: the node kept offering join paths through a table SQL can no longer name,
and its vectors kept turning up in search. Nothing could even delete from the
vector store -- it could only add and replace.

Two things must survive the cleanup: another schema's table that happens to
share the name, and semantic context a person wrote.
"""

from typing import Any

import pytest

from src.graphrag.manager import GraphRAGManager


def _table(name: str, schema: str, references: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "schema": schema,
        "comment": f"the {name} table",
        "columns": [
            {"name": "id", "data_type": "INTEGER", "is_primary_key": True},
            {"name": "label", "data_type": "VARCHAR"},
        ]
        + ([{"name": "parent_id", "data_type": "INTEGER"}] if references else []),
        "foreign_keys": (
            [
                {
                    "column": "parent_id",
                    "referenced_table": references,
                    "referenced_column": "id",
                }
            ]
            if references
            else []
        ),
    }


@pytest.fixture
def manager(tmp_path, monkeypatch) -> GraphRAGManager:
    monkeypatch.setattr(
        "src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path / "chroma"
    )
    instance = GraphRAGManager(
        embedding_model="tfidf", connection_id="dropped", schema_name="public"
    )
    instance.initialize_from_schema(
        [
            _table("customers", "public"),
            _table("orders", "public", references="customers"),
        ],
        schema_name="public",
    )
    return instance


def _ids(manager: GraphRAGManager) -> set[str]:
    got = manager.vector_store.collection.get()
    return set(got["ids"])


class TestTheGraph:
    """A node for a table that no longer exists must not offer joins."""

    def test_the_node_and_its_edges_go(self, manager):
        assert manager.graph_retriever.find_join_path("orders", "customers")

        manager.accumulate_schema([_table("customers", "public")], "public")

        assert "orders" not in manager.graph_retriever.graph
        assert manager.graph_retriever.find_join_path("orders", "customers") is None

    def test_the_remaining_tables_are_untouched(self, manager):
        manager.accumulate_schema([_table("customers", "public")], "public")

        assert "public.customers" in manager.graph_retriever.graph
        assert manager.graph_retriever._tables_info.keys() == {"public.customers"}

    def test_a_replacing_discovery_also_cleans_up(self, manager):
        """initialize_from_schema clears the graph but never the index."""
        manager.initialize_from_schema([_table("customers", "public")], "public")

        assert "public.orders" not in manager.graph_retriever.graph
        assert not any(i.startswith("public.orders") for i in _ids(manager))


class TestTheIndex:
    """Vectors for a dropped table must stop turning up in search."""

    def test_the_table_its_columns_and_its_relationships_go(self, manager):
        before = _ids(manager)
        assert "public.orders" in before
        assert "public.orders.label" in before
        assert any("__to__" in element for element in before)

        manager.accumulate_schema([_table("customers", "public")], "public")

        after = _ids(manager)
        assert "public.orders" not in after
        assert not any(element.startswith("public.orders") for element in after)
        assert not any("orders" in element for element in after)
        assert "public.customers" in after
        assert "public.customers.label" in after

    def test_search_stops_returning_it(self, manager):
        manager.accumulate_schema([_table("customers", "public")], "public")

        found = manager.search_schema("the orders table", top_k=5)

        assert all(
            result["element"]["name"] != "orders"
            and not result["element"]["id"].startswith("orders")
            for result in found
        )

    def test_deleting_nothing_deletes_nothing(self, manager):
        before = _ids(manager)

        assert manager.vector_store.delete_tables([]) == 0
        assert _ids(manager) == before


class TestWhatMustSurvive:
    """The two things a cleanup must not take with it."""

    def test_another_schema_keeps_its_same_named_table(self, manager):
        """The collision case, now two tables rather than one shared node.

        `archive.orders` arrives beside `public.orders`, then archive is
        rediscovered without it: archive's goes, public's stays, and public's
        was never described by archive's comment in the first place.
        """
        manager.accumulate_schema([_table("orders", "archive")], "archive")
        assert {"public.orders", "archive.orders"} <= set(manager.graph_retriever.graph)

        manager.accumulate_schema([_table("stock", "archive")], "archive")

        assert "public.orders" in manager.graph_retriever.graph
        assert "archive.orders" not in manager.graph_retriever.graph
        assert "public.orders" in _ids(manager)
        assert "archive.orders" not in _ids(manager)

    def test_a_schema_rediscovery_leaves_other_schemas_alone(self, manager):
        manager.accumulate_schema([_table("shipments", "logistics")], "logistics")

        manager.accumulate_schema([_table("customers", "public")], "public")

        assert "logistics.shipments" in manager.graph_retriever.graph
        assert "logistics.shipments" in _ids(manager)

    def test_semantic_context_is_not_deleted(self, manager):
        manager.add_semantic_context("orders", "revenue is the sum of order totals")
        assert "semantic_context:orders" in _ids(manager)

        manager.accumulate_schema([_table("customers", "public")], "public")

        # The table is gone, but what a person wrote about it is theirs.
        assert "orders" not in _ids(manager)
        assert "semantic_context:orders" in _ids(manager)


class TestTheGraphHelpers:
    """The two pieces the cleanup is built from."""

    def test_tables_of_schema_reports_only_that_schema(self, manager):
        manager.accumulate_schema([_table("shipments", "logistics")], "logistics")

        assert manager.graph_retriever.tables_of_schema("public") == {
            "public.customers",
            "public.orders",
        }
        assert manager.graph_retriever.tables_of_schema("logistics") == {
            "logistics.shipments"
        }
        assert manager.graph_retriever.tables_of_schema("nowhere") == set()

    def test_removing_a_table_invalidates_the_undirected_snapshot(self, manager):
        stale = manager.graph_retriever._undirected_snapshot()

        manager.graph_retriever.remove_tables({"public.orders"})

        assert manager.graph_retriever._undirected_snapshot() is not stale
        assert "public.orders" not in manager.graph_retriever._undirected_snapshot()

    def test_removing_a_table_that_is_not_there_is_harmless(self, manager):
        assert manager.graph_retriever.remove_tables({"nosuchtable"}) == 0
