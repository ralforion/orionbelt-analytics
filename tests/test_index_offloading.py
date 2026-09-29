"""Indexing a schema embeds off the event loop, and one schema at a time.

Embedding is 99% of indexing on MiniLM, the default backend: 8.2 s of the 8.3 s
a 60-table schema costs. It ran on the event loop, so every other session on
the server was frozen for that whole time. It runs in a worker now, and only the
writes to the shared graph and vector store happen back on the loop.

That changes what can interleave, which is the other half of these tests: two
indexings on one connection used to be serialized by the blocked loop, and are
now serialized by a lock -- otherwise each would find no manager and build one.
"""

import asyncio
import threading
from typing import Any
from unittest.mock import Mock

import pytest

from src.graphrag.manager import GraphRAGManager
from src.handlers import graphrag as handler
from src.session import GraphRAGState


def _tables(count: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "name": f"t{i}",
            "schema": "public",
            "columns": [
                {"name": "id", "data_type": "INTEGER", "is_primary_key": True},
                {"name": "value", "data_type": "VARCHAR"},
            ],
            "foreign_keys": [],
        }
        for i in range(count)
    ]


@pytest.fixture
def manager(tmp_path, monkeypatch) -> GraphRAGManager:
    monkeypatch.setattr(
        "src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path / "chroma"
    )
    return GraphRAGManager(
        embedding_model="tfidf", connection_id="testconn", schema_name="public"
    )


class TestEmbeddingLeavesTheLoop:
    """The loop must keep serving while a schema is embedded."""

    async def test_the_loop_runs_while_embedding(self, manager, monkeypatch):
        embedding = threading.Event()
        release = threading.Event()
        real = manager.embedder.batch_embed_schema

        def slow(tables, views=None):
            embedding.set()
            assert release.wait(timeout=10)
            return real(tables, views)

        monkeypatch.setattr(manager.embedder, "batch_embed_schema", slow)
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while not release.is_set():
                ticks += 1
                await asyncio.sleep(0)

        indexing = asyncio.create_task(
            manager.aindex_schema(_tables(), "public", accumulate=False)
        )
        ticking = asyncio.create_task(tick())
        # Wait for the worker to reach the embedder without blocking the loop.
        assert await asyncio.to_thread(embedding.wait, 10), "embedding never started"
        release.set()
        await indexing
        await ticking

        assert ticks > 0
        assert manager.graph_retriever.graph.number_of_nodes() == 2

    async def test_embedding_runs_in_another_thread(self, manager, monkeypatch):
        threads: list[int] = []
        real = manager.embedder.batch_embed_schema

        def record(tables, views=None):
            threads.append(threading.get_ident())
            return real(tables, views)

        monkeypatch.setattr(manager.embedder, "batch_embed_schema", record)

        await manager.aindex_schema(_tables(), "public", accumulate=False)

        assert threads and threads[0] != threading.get_ident()

    async def test_the_graph_is_never_seen_half_built(self, manager):
        """Publishing is synchronous, so a reader sees before or after."""
        await manager.aindex_schema(_tables(3), "public", accumulate=False)
        seen: list[int] = []

        async def watch() -> None:
            for _ in range(200):
                seen.append(manager.graph_retriever.graph.number_of_nodes())
                await asyncio.sleep(0)

        watching = asyncio.create_task(watch())
        await manager.aindex_schema(_tables(5), "public", accumulate=False)
        await watching

        # 3 before the rebuild, 5 after; never 0 from the clear, never partial.
        assert set(seen) <= {3, 5}


class TestResultsMatchTheSyncPath:
    """Off-loop indexing must index exactly what the sync method did."""

    async def test_the_same_elements_and_graph(self, manager, tmp_path, monkeypatch):
        tables = _tables(4)
        await manager.aindex_schema(tables, "public", accumulate=False)
        async_stats = manager.vector_store.get_statistics()["total_elements"]
        async_nodes = manager.graph_retriever.graph.number_of_nodes()
        async_schemas = list(manager._schema_names)

        monkeypatch.setattr(
            "src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path / "chroma2"
        )
        sync = GraphRAGManager(
            embedding_model="tfidf", connection_id="testconn2", schema_name="public"
        )
        sync.initialize_from_schema(tables, schema_name="public")

        assert async_stats == sync.vector_store.get_statistics()["total_elements"]
        assert async_nodes == sync.graph_retriever.graph.number_of_nodes()
        assert async_schemas == sync._schema_names
        assert manager._initialized is True

    async def test_accumulating_adds_rather_than_replaces(self, manager):
        await manager.aindex_schema(_tables(2), "public", accumulate=False)
        other = [
            dict(t, name=f"other_{t['name']}", schema="analytics") for t in _tables(2)
        ]

        await manager.aindex_schema(other, "analytics", accumulate=True)

        assert manager.graph_retriever.graph.number_of_nodes() == 4
        assert manager._schema_names == ["public", "analytics"]


class TestOneIndexingAtATime:
    """The lock is what a blocked loop used to provide."""

    async def test_two_background_inits_build_one_manager(self, monkeypatch):
        state = GraphRAGState()
        built: list[Any] = []
        indexed: list[str] = []

        class Recording:
            def __init__(self, connection_id=None, schema_name=None, **_kw):
                built.append(schema_name)
                self.graph_retriever = Mock()
                self.graph_retriever.graph.number_of_nodes.return_value = 1
                self.vector_store = Mock()
                self.vector_store.get_statistics.return_value = {"total_elements": 1}
                self._schema_names: list[str] = []

            async def aindex_schema(self, schema_name="", accumulate=False, **_kw):
                # Suspend where the embedding would be, so the other call runs.
                await asyncio.sleep(0.01)
                indexed.append(f"{schema_name}:{'add' if accumulate else 'new'}")
                self._schema_names.append(schema_name)

        session = Mock()
        session.graphrag = state
        session.connection_id = "conn-1"
        monkeypatch.setattr(handler, "GraphRAGManager", Recording)
        monkeypatch.setattr(handler, "_save_graphrag_state", _noop)
        monkeypatch.setattr(handler, "update_workspace_section", _noop)
        monkeypatch.setattr(handler, "_table_info_to_dict", lambda t: t)
        monkeypatch.setattr(handler, "_view_info_to_dict", lambda v: v)
        monkeypatch.setenv("AUTO_ONTOLOGY", "false")

        await asyncio.gather(
            handler._auto_initialize_graphrag_background(
                "public", _tables(1), session, Mock()
            ),
            handler._auto_initialize_graphrag_background(
                "analytics", _tables(1), session, Mock()
            ),
        )

        assert len(built) == 1
        assert indexed in (
            ["public:new", "analytics:add"],
            ["analytics:new", "public:add"],
        )


async def _noop(*_args: Any, **_kwargs: Any) -> Any:
    return None
