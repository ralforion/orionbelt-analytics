"""Business names reach the per-question context; questions cross languages.

Business names -- from apply_semantic_names or graphrag_add_semantic_context --
were indexed as entries of their own, but graphrag_query_context searched only
tables and columns, so they never steered it: "revenue" ranked shipments above
orders although orders.net_amt had been named "Net revenue". And MiniLM is
English-only: a German question did not reach English names at all.
"""

from pathlib import Path
from typing import Any

import pytest

import src.graphrag.manager as manager_module
from src.graphrag import multilingual
from src.graphrag.embedder import (
    MODEL_MINILM,
    MODEL_MULTILINGUAL,
    SchemaEmbedder,
    resolve_embedding_model,
)
from src.graphrag.identity import qualified
from src.graphrag.manager import GraphRAGManager, _resolve_target
from src.graphrag.retriever import GraphRetriever


def _col(name: str, data_type: str) -> dict[str, Any]:
    return {
        "name": name,
        "data_type": data_type,
        "is_nullable": True,
        "is_primary_key": name == "id",
        "is_foreign_key": False,
        "foreign_key_table": None,
    }


TABLES = [
    {
        "name": "orders",
        "schema": "gold",
        "columns": [
            _col("id", "INTEGER"),
            _col("net_amt", "DECIMAL"),
            _col("order_date", "DATE"),
        ],
        "foreign_keys": [],
    },
    {
        "name": "shipments",
        "schema": "gold",
        "columns": [_col("id", "INTEGER"), _col("ship_cost", "DECIMAL")],
        "foreign_keys": [],
    },
]


def _minilm_is_cached() -> bool:
    cache = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
    return cache.exists()


def _multilingual_is_cached() -> bool:
    from huggingface_hub import try_to_load_from_cache

    return all(
        isinstance(
            try_to_load_from_cache(
                multilingual.REPOSITORY, name, revision=multilingual.REVISION
            ),
            str,
        )
        for name in multilingual.SHA256
    )


needs_minilm = pytest.mark.skipif(
    not _minilm_is_cached(), reason="MiniLM not cached; avoids a download in CI"
)
needs_multilingual = pytest.mark.skipif(
    not _multilingual_is_cached(),
    reason="multilingual model not cached; avoids a download in CI",
)


def _manager(monkeypatch: Any, model: str) -> GraphRAGManager:
    monkeypatch.setenv("GRAPHRAG_EMBEDDING_MODEL", model)
    monkeypatch.setattr(manager_module, "CHROMADB_AVAILABLE", False)
    mgr = GraphRAGManager(connection_id=f"test-{model}", schema_name="gold")
    mgr.initialize_from_schema(tables_info=TABLES, schema_name="gold")
    return mgr


def _columns(mgr: GraphRAGManager, question: str) -> list[tuple[str, str]]:
    context = mgr.get_query_context(question, max_tables=2, max_columns=2)
    return [(c["table"], c["column"]) for c in context["relevant_columns"]]


@needs_minilm
class TestBusinessNamesSteerTheContext:
    def test_without_a_business_name_the_cryptic_column_loses(self, monkeypatch):
        mgr = _manager(monkeypatch, "minilm")

        assert _columns(mgr, "revenue")[0] == ("gold.shipments", "ship_cost")

    def test_a_business_name_brings_the_column_and_its_table_first(self, monkeypatch):
        mgr = _manager(monkeypatch, "minilm")
        mgr.add_semantic_context("orders.net_amt", "Net revenue", source="ontology")

        context = mgr.get_query_context("revenue", max_tables=2, max_columns=2)

        top_column = context["relevant_columns"][0]
        assert (top_column["table"], top_column["column"]) == ("gold.orders", "net_amt")
        assert top_column["business_name"] == "Net revenue"
        assert top_column["data_type"] == "DECIMAL"
        top_table = context["relevant_tables"][0]
        assert top_table["name"] == "orders"
        assert top_table["matched_business_name"] == "Net revenue"

    def test_a_table_level_business_name_counts_too(self, monkeypatch):
        mgr = _manager(monkeypatch, "minilm")
        mgr.add_semantic_context("shipments", "Freight and delivery costs")

        context = mgr.get_query_context("freight", max_tables=1, max_columns=1)

        assert context["relevant_tables"][0]["name"] == "shipments"


class TestTargets:
    def _graph(self) -> GraphRetriever:
        graph = GraphRetriever()
        graph.build_graph(
            [
                *TABLES,
                {
                    "name": "orders",
                    "schema": "archive",
                    "columns": [_col("id", "INTEGER")],
                    "foreign_keys": [],
                },
            ]
        )
        return graph

    def test_a_column_target_resolves_to_its_table_and_column(self):
        graph = GraphRetriever()
        graph.build_graph(TABLES)

        assert _resolve_target("orders.net_amt", graph) == ("gold.orders", "net_amt")
        assert _resolve_target("orders", graph) == ("gold.orders", None)

    def test_a_name_in_two_schemas_is_not_guessed(self):
        graph = GraphRetriever()
        graph.build_graph(
            [
                *TABLES,
                {
                    "name": "orders",
                    "schema": "archive",
                    "columns": [_col("id", "INTEGER"), _col("net_amt", "DECIMAL")],
                    "foreign_keys": [],
                },
            ]
        )

        # Both schemas' orders have the column: two readings, no answer.
        assert _resolve_target("orders.net_amt", graph) is None
        assert _resolve_target("orders", graph) is None

    def test_only_the_schema_that_has_the_column_is_a_reading(self):
        # archive.orders has no net_amt, so it is not a reading of the target.
        assert _resolve_target("orders.net_amt", self._graph()) == (
            "gold.orders",
            "net_amt",
        )

    def test_a_missing_column_or_table_resolves_to_nothing(self):
        graph = GraphRetriever()
        graph.build_graph(TABLES)

        assert _resolve_target("orders.nope", graph) is None
        assert _resolve_target("nope", graph) is None


def test_tfidf_adds_no_business_matches(monkeypatch):
    mgr = _manager(monkeypatch, "tfidf")
    mgr.add_semantic_context("orders.net_amt", "Net revenue")

    assert mgr.business_name_matches("revenue", 5) == []


class TestMultilingual:
    def test_the_backend_can_be_chosen(self):
        assert resolve_embedding_model("multilingual") == MODEL_MULTILINGUAL

    def test_a_file_with_the_wrong_checksum_is_refused(self, monkeypatch, tmp_path):
        import huggingface_hub

        forged = tmp_path / "forged"
        forged.write_bytes(b"not the model")
        monkeypatch.setattr(multilingual, "_verified", {})
        monkeypatch.setattr(
            huggingface_hub, "hf_hub_download", lambda *a, **k: str(forged)
        )

        with pytest.raises(multilingual.ModelIntegrityError):
            multilingual.model_files()

    def test_a_model_that_cannot_load_falls_back_to_minilm(self, monkeypatch):
        def unavailable() -> None:
            raise OSError("no network")

        monkeypatch.setattr(multilingual, "MultilingualEmbedding", unavailable)
        embedder = SchemaEmbedder.__new__(SchemaEmbedder)
        embedder.embedding_model = MODEL_MULTILINGUAL
        embedder._embedding_function = None
        # Only the fallback decision is under test: stop before MiniLM loads.
        calls: list[str] = []
        original = SchemaEmbedder._initialize_model

        def record(self: SchemaEmbedder) -> None:
            calls.append(self.embedding_model)
            if self.embedding_model == MODEL_MINILM:
                return
            original(self)

        monkeypatch.setattr(SchemaEmbedder, "_initialize_model", record)

        embedder._initialize_model()

        assert calls == [MODEL_MULTILINGUAL, MODEL_MINILM]
        assert embedder.embedding_model == MODEL_MINILM

    @needs_multilingual
    def test_a_german_question_reaches_an_english_business_name(self, monkeypatch):
        mgr = _manager(monkeypatch, "multilingual")
        mgr.add_semantic_context("orders.net_amt", "Net revenue", source="ontology")

        assert _columns(mgr, "Umsatz im letzten Quartal")[0] == (
            "gold.orders",
            "net_amt",
        )


class TestDottedNames:
    """apply_semantic_names joins table and column with a dot, either may hold one."""

    def _graph(self, *tables: dict[str, Any]) -> GraphRetriever:
        graph = GraphRetriever()
        graph.build_graph(list(tables))
        return graph

    def test_a_column_with_a_dot_resolves(self):
        graph = self._graph(
            {
                "name": "orders",
                "schema": "gold",
                "columns": [_col("id", "INTEGER"), _col("net.amount", "DECIMAL")],
                "foreign_keys": [],
            }
        )

        assert _resolve_target("orders.net.amount", graph) == (
            "gold.orders",
            "net.amount",
        )

    def test_a_table_with_a_dot_resolves(self):
        graph = self._graph(
            {
                "name": "sales.eu",
                "schema": "gold",
                "columns": [_col("id", "INTEGER"), _col("amt", "DECIMAL")],
                "foreign_keys": [],
            }
        )

        identity = qualified("gold", "sales.eu")  # quoted: the name holds a dot
        assert _resolve_target("sales.eu.amt", graph) == (identity, "amt")
        assert _resolve_target("sales.eu", graph) == (identity, None)

    def test_two_readings_that_both_fit_are_not_guessed(self):
        graph = self._graph(
            {
                "name": "a",
                "schema": "gold",
                "columns": [_col("b.c", "INTEGER")],
                "foreign_keys": [],
            },
            {
                "name": "a.b",
                "schema": "gold",
                "columns": [_col("c", "INTEGER")],
                "foreign_keys": [],
            },
        )

        assert _resolve_target("a.b.c", graph) is None

    def _table(self, name: str, schema: str, column: str) -> dict[str, Any]:
        return {
            "name": name,
            "schema": schema,
            "columns": [_col(column, "DECIMAL")],
            "foreign_keys": [],
        }

    def test_a_reading_through_a_table_in_two_schemas_still_counts(self):
        # sales.net (in two schemas) . amount  vs  sales . net.amount
        graph = self._graph(
            self._table("sales.net", "gold", "amount"),
            self._table("sales.net", "archive", "amount"),
            self._table("sales", "gold", "net.amount"),
        )

        assert _resolve_target("sales.net.amount", graph) is None

    def test_discovering_another_schema_does_not_flip_a_rejection(self):
        tables = [
            self._table("sales.net", "gold", "amount"),
            self._table("sales", "gold", "net.amount"),
        ]
        before = self._graph(*tables)
        after = self._graph(*tables, self._table("sales.net", "archive", "amount"))

        assert _resolve_target("sales.net.amount", before) is None
        assert _resolve_target("sales.net.amount", after) is None

    def test_an_ambiguous_table_without_the_column_does_not_block(self):
        graph = self._graph(
            self._table("sales.net", "gold", "other"),
            self._table("sales.net", "archive", "other"),
            self._table("sales", "gold", "net.amount"),
        )

        assert _resolve_target("sales.net.amount", graph) == (
            "gold.sales",
            "net.amount",
        )


@needs_minilm
def test_column_matches_are_not_capped_by_the_table_limit(monkeypatch):
    monkeypatch.setenv("GRAPHRAG_EMBEDDING_MODEL", "minilm")
    monkeypatch.setattr(manager_module, "CHROMADB_AVAILABLE", False)
    finance = {
        "name": "fin",
        "schema": "gold",
        "columns": [
            _col("id", "INTEGER"),
            _col("np_amt", "DECIMAL"),
            _col("gm_amt", "DECIMAL"),
            _col("opx_amt", "DECIMAL"),
            _col("tx_amt", "DECIMAL"),
            # Raw names that share the question's words but mean other things.
            _col("profit_center", "VARCHAR"),
            _col("gross_weight", "DECIMAL"),
            _col("operating_unit", "VARCHAR"),
            _col("tax_region", "VARCHAR"),
        ],
        "foreign_keys": [],
    }
    mgr = GraphRAGManager(connection_id="test-cap", schema_name="gold")
    mgr.initialize_from_schema(tables_info=[finance], schema_name="gold")
    for column, name in [
        ("np_amt", "Net profit"),
        ("gm_amt", "Gross margin"),
        ("opx_amt", "Operating expenses"),
        ("tx_amt", "Taxes paid"),
    ]:
        mgr.add_semantic_context(f"fin.{column}", name)

    def columns(max_tables: int) -> list[tuple[str, str | None]]:
        context = mgr.get_query_context(
            "net profit, gross margin, operating expenses and taxes",
            max_tables=max_tables,
            max_columns=4,
        )
        return [
            (c["column"], c.get("business_name")) for c in context["relevant_columns"]
        ]

    # Which columns are relevant does not depend on how many tables are kept.
    assert columns(max_tables=1) == columns(max_tables=5)
    named = [c for c, business in columns(max_tables=1) if business]
    assert len(named) >= 3
