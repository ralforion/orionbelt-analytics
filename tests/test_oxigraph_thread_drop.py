"""pyoxigraph results must be freed on the thread that made them.

pyoxigraph results are bound to their thread. A CONSTRUCT result held in the
frame that ran the federation check outlived the call -- rdflib's parse leaves
reference cycles reaching back to that frame -- and the garbage collector
then freed it on another thread. pyoxigraph refuses such a drop ("unsendable,
but is being dropped on another thread") and the result leaks. The server
runs SPARQL in worker threads, so a long-running server leaked a result on a
share of CONSTRUCT queries.
"""

import asyncio
import gc
import sys
from collections.abc import Callable
from typing import Any

import pytest

from src.oxigraph_store import OxigraphStoreManager


@pytest.fixture
def store(tmp_path):
    manager = OxigraphStoreManager(tmp_path / "store")
    manager.store.update("INSERT DATA { <urn:a> <urn:p> <urn:b> }")
    return manager


@pytest.fixture
def unsendable_drops(monkeypatch):
    drops: list[str] = []

    def hook(unraisable: Any) -> None:
        if "unsendable" in str(unraisable.exc_value):
            drops.append(str(unraisable.exc_value))

    monkeypatch.setattr(sys, "unraisablehook", hook)
    return drops


QUERIES: dict[str, Callable[[OxigraphStoreManager], Any]] = {
    "construct": lambda s: s.query_sparql_construct(
        "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"
    ),
    "select": lambda s: s.query_sparql("SELECT ?s WHERE { ?s ?p ?o }"),
    "select with timeout": lambda s: s.query_sparql(
        "SELECT ?s WHERE { ?s ?p ?o }", timeout_seconds=5
    ),
    "ask": lambda s: s.query_sparql_ask("ASK { ?s ?p ?o }"),
}


@pytest.mark.parametrize("form", QUERIES)
async def test_a_query_in_a_worker_leaves_nothing_for_another_thread(
    store, unsendable_drops, form
):
    for _ in range(10):
        result = await asyncio.to_thread(QUERIES[form], store)
        # Collect here, on this thread, as the server's event loop would:
        # anything the worker left in a cycle is dropped on the wrong thread.
        gc.collect()

    assert result
    assert unsendable_drops == []
