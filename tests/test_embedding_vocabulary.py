"""A TF-IDF index keeps the vocabulary its vectors were made against.

A TF-IDF vector only means something against the vocabulary and document
frequencies it was produced with. Those were fitted when a schema was indexed
and lost with the process, so after a restart a query was embedded against a
vocabulary fitted on that query's own words -- a different space, of a different
size, padded to the same width by the store. Nothing failed: the search just
returned plausible nonsense, ranking `customers` first for a question about
orders.

The vocabulary is saved beside the index and restored with it, so the ranking
after a restart is the ranking before it, score for score.
"""

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from src.graphrag.embedder import MODEL_TFIDF, SchemaEmbedder, vocabulary_fingerprint
from src.graphrag.manager import GraphRAGManager

QUERY = "purchase orders placed by customers"

TABLES: list[dict[str, Any]] = [
    {
        "name": "customers",
        "schema": "public",
        "comment": "people who buy things",
        "columns": [
            {"name": "customer_id", "data_type": "INTEGER", "is_primary_key": True},
            {"name": "email", "data_type": "VARCHAR"},
        ],
        "foreign_keys": [],
    },
    {
        "name": "orders",
        "schema": "public",
        "comment": "purchase orders placed by customers",
        "columns": [
            {"name": "order_id", "data_type": "INTEGER", "is_primary_key": True},
            {
                "name": "customer_id",
                "data_type": "INTEGER",
                "is_foreign_key": True,
                "foreign_key_table": "customers",
            },
            {"name": "total_amount", "data_type": "DECIMAL"},
        ],
        "foreign_keys": [
            {
                "column": "customer_id",
                "referenced_table": "customers",
                "referenced_column": "customer_id",
            }
        ],
    },
]


@pytest.fixture
def chroma(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(
        "src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path / "chroma"
    )
    return tmp_path


def _manager(name: str = "restart") -> GraphRAGManager:
    return GraphRAGManager(
        embedding_model=MODEL_TFIDF, connection_id=name, schema_name="public"
    )


def _ranking(manager: GraphRAGManager) -> list[tuple[str, float]]:
    return [
        (result["element"]["name"], round(result["similarity_score"], 4))
        for result in manager.search_schema(QUERY, top_k=3)
    ]


class TestSearchSurvivesARestart:
    """The whole point: the same query gives the same answer afterwards."""

    def test_the_ranking_is_identical(self, chroma):
        first = _manager()
        first.initialize_from_schema(TABLES, schema_name="public")
        first.save_state(chroma / "out")
        before = _ranking(first)
        assert before[0][0] == "orders", before

        restarted = _manager()
        assert restarted.load_state(chroma / "out")

        assert _ranking(restarted) == before

    def test_the_vocabulary_is_written_beside_the_index(self, chroma):
        manager = _manager()
        manager.initialize_from_schema(TABLES, schema_name="public")
        manager.save_state(chroma / "out")

        saved = json.loads(
            (chroma / "out" / "restart" / "embedder_vocabulary.json").read_text()
        )

        assert saved["backend"] == MODEL_TFIDF
        assert len(saved["vocabulary"]) == len(saved["idf"]) > 1
        assert saved["fingerprint"] == vocabulary_fingerprint(saved["vocabulary"])

    def test_a_restarted_embedder_is_in_the_same_space(self, chroma):
        first = _manager()
        first.initialize_from_schema(TABLES, schema_name="public")
        first.save_state(chroma / "out")

        restarted = _manager()
        restarted.load_state(chroma / "out")

        assert restarted.embedder._is_fitted
        assert (
            restarted.embedder.vectorizer.vocabulary_
            == first.embedder.vectorizer.vocabulary_
        )
        assert (
            restarted.embedder._embed_text(QUERY).tolist()
            == first.embedder._embed_text(QUERY).tolist()
        )

    def test_a_schema_accumulated_after_a_restart_stays_in_that_space(self, chroma):
        first = _manager()
        first.initialize_from_schema(TABLES, schema_name="public")
        first.save_state(chroma / "out")
        before = _ranking(first)

        restarted = _manager()
        restarted.load_state(chroma / "out")
        restarted.accumulate_schema(
            [
                {
                    "name": "shipments",
                    "schema": "logistics",
                    "comment": "deliveries",
                    "columns": [{"name": "shipment_id", "data_type": "INTEGER"}],
                    "foreign_keys": [],
                }
            ],
            schema_name="logistics",
        )

        # The new schema did not refit, so the original vectors still compare.
        assert _ranking(restarted)[0] == before[0]


class TestTheSavedStateIsChecked:
    """A vocabulary that cannot be trusted must not be used."""

    def test_a_tampered_vocabulary_is_refused(self):
        embedder = SchemaEmbedder(MODEL_TFIDF)
        embedder.batch_embed_schema(TABLES, [])
        state = embedder.vocabulary_state()
        assert state is not None
        state["vocabulary"] = dict(list(state["vocabulary"].items())[:-1])

        assert SchemaEmbedder(MODEL_TFIDF).load_vocabulary_state(state) is False

    def test_an_incomplete_vocabulary_is_refused(self):
        assert SchemaEmbedder(MODEL_TFIDF).load_vocabulary_state({}) is False
        assert (
            SchemaEmbedder(MODEL_TFIDF).load_vocabulary_state(
                {"vocabulary": {"a": 0}, "idf": []}
            )
            is False
        )

    def test_an_unfitted_embedder_has_nothing_to_save(self):
        assert SchemaEmbedder(MODEL_TFIDF).vocabulary_state() is None

    def test_a_backend_without_a_vocabulary_saves_nothing(self):
        embedder = SchemaEmbedder(MODEL_TFIDF)
        embedder.embedding_model = "minilm"

        assert embedder.vocabulary_state() is None
        assert (
            embedder.load_vocabulary_state({"vocabulary": {"a": 0}, "idf": [1.0]})
            is False
        )

    def test_an_index_without_a_saved_vocabulary_still_loads(self, chroma, caplog):
        """What an index written by an older version looks like."""
        manager = _manager()
        manager.initialize_from_schema(TABLES, schema_name="public")
        manager.save_state(chroma / "out")
        (chroma / "out" / "restart" / "embedder_vocabulary.json").unlink()

        restarted = _manager()
        assert restarted.load_state(chroma / "out")
        assert "No saved TF-IDF vocabulary" in caplog.text


class TestTheFingerprint:
    """Two vocabularies are the same one only if they are identical."""

    def test_the_same_vocabulary_fingerprints_the_same(self):
        first = {"orders": 0, "customers": 1}
        second = {"customers": 1, "orders": 0}

        assert vocabulary_fingerprint(first) == vocabulary_fingerprint(second)

    def test_a_different_index_is_a_different_space(self):
        assert vocabulary_fingerprint({"a": 0, "b": 1}) != vocabulary_fingerprint(
            {"a": 1, "b": 0}
        )

    def test_a_missing_term_changes_it(self):
        assert vocabulary_fingerprint({"a": 0, "b": 1}) != vocabulary_fingerprint(
            {"a": 0}
        )


def test_embedding_before_indexing_says_so(caplog):
    """The remaining way to end up in a one-document vocabulary."""
    embedder = SchemaEmbedder(MODEL_TFIDF)

    embedder._embed_text("a query nobody indexed anything for")

    assert "before any schema was indexed" in caplog.text


def test_state_round_trips_through_a_file():
    """What save_state writes is what load_state reads."""
    embedder = SchemaEmbedder(MODEL_TFIDF)
    embedder.batch_embed_schema(TABLES, [])
    state = embedder.vocabulary_state()
    assert state is not None

    path = Path(tempfile.mkdtemp()) / "vocabulary.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    restored = SchemaEmbedder(MODEL_TFIDF)
    assert restored.load_vocabulary_state(json.loads(path.read_text()))
    assert restored._embed_text(QUERY).tolist() == embedder._embed_text(QUERY).tolist()
