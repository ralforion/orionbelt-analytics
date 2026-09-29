"""Saving GraphRAG state derives connection-wide data once, not once per schema.

The graph, the community summaries and the vector collection describe the whole
connection, not one schema, but `save_state` recomputed each of them inside the
per-schema loop. Five accumulated schemas therefore ran six whole-graph
exports, six rounds of community summarization and five full serializations of
the same collection: 915 ms, against 206 ms once each is derived once.

What the files must contain is unchanged -- these tests pin that, and that
restore still reads them.
"""

from pathlib import Path
from typing import Any

import pytest

from src.graphrag.manager import GraphRAGManager


@pytest.fixture
def chroma_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path / "chroma"
    )
    return tmp_path


def _schema(name: str, tables: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "name": f"{name}_t{t}",
            "schema": name,
            "comment": f"entity {t}",
            "columns": [
                {
                    "name": f"col_{c}",
                    "data_type": "INTEGER",
                    "is_primary_key": c == 0,
                    "is_foreign_key": False,
                }
                for c in range(3)
            ],
            "foreign_keys": (
                []
                if t == 0
                else [
                    {
                        "column": "col_1",
                        "referenced_table": f"{name}_t0",
                        "referenced_column": "col_0",
                    }
                ]
            ),
        }
        for t in range(tables)
    ]


def _manager(schemas: list[str]) -> GraphRAGManager:
    manager = GraphRAGManager(
        embedding_model="tfidf", connection_id="testconn", schema_name=schemas[0]
    )
    manager.initialize_from_schema(_schema(schemas[0]), schema_name=schemas[0])
    for name in schemas[1:]:
        manager.accumulate_schema(_schema(name), schema_name=name)
    return manager


class TestDerivedDataIsComputedOnce:
    """One export, one summarization, whatever the number of schemas."""

    def test_the_collection_is_serialized_once(self, chroma_dir, monkeypatch):
        manager = _manager(["public", "analytics", "archive"])
        calls: list[Path] = []
        original = manager.vector_store.save

        def counting(path: Path) -> None:
            calls.append(Path(path))
            original(path)

        monkeypatch.setattr(manager.vector_store, "save", counting)
        manager.save_state(chroma_dir / "out")

        assert len(calls) == 1

    def test_the_graph_is_exported_once(self, chroma_dir, monkeypatch):
        manager = _manager(["public", "analytics", "archive"])
        calls = []
        original = manager.graph_retriever.export_graph_for_visualization

        def counting() -> Any:
            calls.append(1)
            return original()

        monkeypatch.setattr(
            manager.graph_retriever, "export_graph_for_visualization", counting
        )
        manager.save_state(chroma_dir / "out")

        assert len(calls) == 1

    def test_communities_are_summarized_once(self, chroma_dir, monkeypatch):
        manager = _manager(["public", "analytics", "archive"])
        assert manager.community_detector is not None
        calls = []
        original = manager.community_detector.get_all_summaries

        def counting() -> Any:
            calls.append(1)
            return original()

        monkeypatch.setattr(manager.community_detector, "get_all_summaries", counting)
        manager.save_state(chroma_dir / "out")

        assert len(calls) == 1


class TestTheFilesAreUnchanged:
    """Every file a previous save produced is still written, with its content."""

    def test_every_schema_gets_its_files(self, chroma_dir):
        schemas = ["public", "analytics", "archive"]
        manager = _manager(schemas)

        manager.save_state(chroma_dir / "out")

        written = chroma_dir / "out" / "testconn"
        assert (written / "graph_combined.json").exists()
        assert (written / "communities_combined.json").exists()
        for name in schemas:
            assert (written / f"vector_store_{name}.json").exists()
            assert (written / f"graph_{name}.json").exists()
            assert (written / f"communities_{name}.json").exists()

    def test_the_vector_exports_are_copies_of_one_export(self, chroma_dir):
        schemas = ["public", "analytics", "archive"]
        manager = _manager(schemas)

        manager.save_state(chroma_dir / "out")

        written = chroma_dir / "out" / "testconn"
        contents = {
            (written / f"vector_store_{name}.json").read_bytes() for name in schemas
        }
        assert len(contents) == 1

    def test_each_schema_graph_keeps_its_own_tables(self, chroma_dir):
        import json

        schemas = ["public", "analytics"]
        manager = _manager(schemas)

        manager.save_state(chroma_dir / "out")

        written = chroma_dir / "out" / "testconn"
        for name in schemas:
            data = json.loads((written / f"graph_{name}.json").read_text())
            names = {table["name"] for table in data["tables_info"]}
            assert names == {f"{name}_t{t}" for t in range(3)}
            # The visualization stays the whole connection's graph.
            assert data["visualization"]["nodes"]

    def test_a_snapshot_is_still_taken_for_its_schema(self, chroma_dir):
        manager = _manager(["public", "analytics"])

        written_names = manager.save_state(
            chroma_dir / "out", version=2, snapshot_schema="analytics"
        )

        assert set(written_names) == {
            "vector_store_analytics_v2.json",
            "graph_analytics_v2.json",
            "communities_analytics_v2.json",
        }
        written = chroma_dir / "out" / "testconn"
        assert (written / "vector_store_analytics_v2.json").read_bytes() == (
            written / "vector_store_analytics.json"
        ).read_bytes()


class TestRestoreStillWorks:
    """The point of the files: a new manager reads them back."""

    def test_state_round_trips(self, chroma_dir):
        schemas = ["public", "analytics"]
        manager = _manager(schemas)
        manager.save_state(chroma_dir / "out")
        tables_before = manager.graph_retriever.graph.number_of_nodes()

        restored = GraphRAGManager(
            embedding_model="tfidf", connection_id="testconn", schema_name="public"
        )
        assert restored.load_state(chroma_dir / "out") is True

        assert restored.graph_retriever.graph.number_of_nodes() == tables_before
        assert set(restored._schema_names) == set(schemas)
