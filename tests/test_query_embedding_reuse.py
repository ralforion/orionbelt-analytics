"""One query is embedded once, however many searches it drives.

`get_query_context` searches tables and then columns with the same text. Each
search embedded that text itself, so the identical string went through the
model twice per retrieval. The vector depends on the text alone, not on which
element type is being searched.
"""

import pytest

from src.graphrag.manager import GraphRAGManager


def _tables() -> list[dict]:
    return [
        {
            "name": name,
            "schema": "public",
            "columns": [
                {
                    "name": "id",
                    "data_type": "INTEGER",
                    "is_primary_key": True,
                    "is_foreign_key": False,
                    "is_nullable": False,
                },
                {
                    "name": "amount",
                    "data_type": "DECIMAL",
                    "is_primary_key": False,
                    "is_foreign_key": False,
                    "is_nullable": True,
                },
            ],
            "foreign_keys": [],
            "comment": f"{name} table",
        }
        for name in ("orders", "customers", "invoices")
    ]


@pytest.fixture
def manager(monkeypatch, tmp_path) -> GraphRAGManager:
    monkeypatch.setattr("src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path)
    built = GraphRAGManager(connection_id="test", schema_name="main")
    built.initialize_from_schema(tables_info=_tables(), schema_name="main")
    return built


def _count_embeddings(manager: GraphRAGManager) -> list[str]:
    embedded: list[str] = []
    original = manager.embedder._embed_text

    def counting(text):
        embedded.append(text)
        return original(text)

    manager.embedder._embed_text = counting
    return embedded


def test_the_query_is_embedded_once_per_retrieval(manager):
    embedded = _count_embeddings(manager)

    manager.get_query_context("total revenue by customer", max_tables=3, max_columns=5)

    assert len(embedded) == 1


def test_reusing_the_embedding_returns_the_same_results(manager):
    query = "total revenue by customer"

    shared = manager.get_query_context(query, max_tables=3, max_columns=5)
    independently = manager.search_schema(query, top_k=5, element_type="column")

    # The context qualifies a column with its table; the raw search returns the
    # element's own name. The order is what a shared embedding must not change.
    assert [c["column"] for c in shared["relevant_columns"]] == [
        r["element"]["name"] for r in independently
    ]
    assert shared["relevant_tables"], "a retrieval that found nothing proves nothing"


def test_a_search_on_its_own_still_embeds_its_own_query(manager):
    """Only the caller that searches the same text twice passes a vector."""
    embedded = _count_embeddings(manager)

    manager.search_schema("revenue", top_k=3, element_type="table")

    assert embedded == ["revenue"]
