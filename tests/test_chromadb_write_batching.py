"""A schema's vectors are written in chunks ChromaDB accepts.

A wide schema reaches thousands of elements -- 500 tables of 20 columns is over
10,000 -- and ChromaDB refuses a single write past its own maximum. Writing
everything in one call therefore failed on exactly the schemas that most need
an index, so the batch writer chunks.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from src.graphrag.vector_store_chromadb import (
    CHROMADB_AVAILABLE,
    DEFAULT_MAX_WRITE_BATCH,
    ChromaDBVectorStore,
)

pytestmark = pytest.mark.skipif(not CHROMADB_AVAILABLE, reason="ChromaDB not available")


@dataclass
class _Element:
    element_type: str
    element_id: str
    name: str
    description: str
    embedding: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path)
    return ChromaDBVectorStore(connection_id="testconn", schema_name="public")


def _elements(count: int) -> list[_Element]:
    return [
        _Element(
            element_type="column",
            element_id=f"t.c{i}",
            name=f"c{i}",
            description=f"column {i}",
            embedding=np.full(384, float(i), dtype=np.float32),
        )
        for i in range(count)
    ]


class TestWritesAreChunked:
    """No single upsert exceeds the bound, and every element still lands."""

    def test_more_elements_than_the_bound_take_several_writes(self, store, monkeypatch):
        sizes: list[int] = []
        original = store.collection.upsert

        def counting(**kwargs: Any) -> Any:
            sizes.append(len(kwargs["ids"]))
            return original(**kwargs)

        monkeypatch.setattr(store.collection, "upsert", counting)
        count = DEFAULT_MAX_WRITE_BATCH + 25
        store.add_elements_batch(_elements(count))

        assert sum(sizes) == count
        assert max(sizes) <= DEFAULT_MAX_WRITE_BATCH
        assert len(sizes) == 2
        assert store.collection.count() == count

    def test_a_small_schema_still_takes_one_write(self, store, monkeypatch):
        sizes: list[int] = []
        original = store.collection.upsert

        def counting(**kwargs: Any) -> Any:
            sizes.append(len(kwargs["ids"]))
            return original(**kwargs)

        monkeypatch.setattr(store.collection, "upsert", counting)
        store.add_elements_batch(_elements(10))

        assert sizes == [10]

    def test_every_element_is_searchable_after_a_chunked_write(self, store):
        count = DEFAULT_MAX_WRITE_BATCH + 25
        store.add_elements_batch(_elements(count))

        # The last chunk's elements are in the collection, not only the first.
        stored = store.collection.get(ids=[f"t.c{count - 1}", "t.c0"])
        assert set(stored["ids"]) == {"t.c0", f"t.c{count - 1}"}


class TestTheBoundComesFromTheClient:
    """The client's own maximum is respected, and capped by ours."""

    def test_a_smaller_client_maximum_wins(self, store, monkeypatch):
        monkeypatch.setattr(store.client, "get_max_batch_size", lambda: 7)

        assert store._max_write_batch() == 7

    def test_a_larger_client_maximum_is_capped(self, store, monkeypatch):
        monkeypatch.setattr(store.client, "get_max_batch_size", lambda: 10**9)

        assert store._max_write_batch() == DEFAULT_MAX_WRITE_BATCH

    def test_a_client_that_cannot_report_falls_back(self, store, monkeypatch):
        def unsupported() -> int:
            raise AttributeError("no such method")

        monkeypatch.setattr(store.client, "get_max_batch_size", unsupported)

        assert store._max_write_batch() == DEFAULT_MAX_WRITE_BATCH

    def test_the_bound_is_never_zero(self, store, monkeypatch):
        monkeypatch.setattr(store.client, "get_max_batch_size", lambda: 0)

        assert store._max_write_batch() == 1
