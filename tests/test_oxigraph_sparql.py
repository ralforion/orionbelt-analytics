"""Regression tests for Oxigraph SPARQL read/write round-trips.

These cover the code paths that broke against pyoxigraph 0.5.x (issue #38):
- ``add_triple`` (default and named graph) — must build a ``Quad``.
- ``query_sparql`` SELECT — ``QuerySolution`` no longer exposes ``.items()``.
- ``query_sparql_ask`` ASK — ``QueryBoolean`` is not iterable.
- ``add_knowledge`` — exercises ``add_triple`` plus metadata triples.

They run against the real, pinned pyoxigraph (no mocks) so an API drift like
the 0.3.x -> 0.5.x break is caught instead of silently shipping.
"""

import shutil
import tempfile
from pathlib import Path

import pytest

from src.oxigraph_store import OXIGRAPH_AVAILABLE, OxigraphStoreManager

pytestmark = pytest.mark.skipif(not OXIGRAPH_AVAILABLE, reason="Oxigraph not available")

EX = "http://example.com/"


@pytest.fixture
def store():
    """A fresh on-disk Oxigraph store, cleaned up after the test."""
    temp_dir = Path(tempfile.mkdtemp())
    yield OxigraphStoreManager(store_path=temp_dir)
    shutil.rmtree(temp_dir)


class TestAddTripleAndSelect:
    """add_triple (write) + query_sparql (SELECT read) round-trips."""

    def test_add_uri_triple_default_graph_select(self, store):
        store.add_triple(f"{EX}s", f"{EX}p", f"{EX}o")

        results = store.query_sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o }")

        assert results == [{"s": f"{EX}s", "p": f"{EX}p", "o": f"{EX}o"}]

    def test_add_literal_triple_select(self, store):
        store.add_triple(
            f"{EX}customers",
            "http://www.w3.org/2000/01/rdf-schema#label",
            "Customer Master Data",
            object_is_literal=True,
        )

        results = store.query_sparql(
            "SELECT ?label WHERE { ?s "
            "<http://www.w3.org/2000/01/rdf-schema#label> ?label }"
        )

        assert results == [{"label": "Customer Master Data"}]

    def test_add_triple_named_graph_select(self, store):
        graph = f"{EX}graph1"
        store.add_triple(f"{EX}s", f"{EX}p", f"{EX}o", graph_uri=graph)

        # The triple is in the named graph, not the default graph.
        in_graph = store.query_sparql(
            f"SELECT ?s WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}"
        )
        assert in_graph == [{"s": f"{EX}s"}]

    def test_select_unbound_variable_is_omitted(self, store):
        store.add_triple(f"{EX}s", f"{EX}p", f"{EX}o")

        results = store.query_sparql(
            "SELECT ?s ?missing WHERE { ?s ?p ?o OPTIONAL { ?s "
            f"<{EX}none> ?missing }} }}"
        )

        assert results == [{"s": f"{EX}s"}]

    def test_select_empty_result(self, store):
        results = store.query_sparql("SELECT ?s WHERE { ?s ?p ?o }")
        assert results == []


class TestAsk:
    """query_sparql_ask round-trips."""

    def test_ask_true(self, store):
        store.add_triple(f"{EX}s", f"{EX}p", f"{EX}o")
        assert store.query_sparql_ask("ASK { ?s ?p ?o }") is True

    def test_ask_false(self, store):
        assert store.query_sparql_ask("ASK { ?s ?p ?o }") is False


class TestAddKnowledge:
    """add_knowledge writes the main triple plus metadata triples."""

    def test_add_knowledge_roundtrip(self, store):
        subject = f"{EX}pattern/sales"
        predicate = f"{EX}schema#hasSQL"
        sql = "SELECT customer_id FROM orders"

        # add_knowledge writes into its default named graph.
        kg = "http://example.com/knowledge"
        store.add_knowledge(
            subject,
            predicate,
            sql,
            metadata={"confidence": "0.95"},
        )

        results = store.query_sparql(
            f"SELECT ?o WHERE {{ GRAPH <{kg}> {{ <{subject}> <{predicate}> ?o }} }}"
        )
        assert results == [{"o": sql}]

        # Metadata is stored as an additional triple on the same subject.
        meta = store.query_sparql(
            f"SELECT ?c WHERE {{ GRAPH <{kg}> {{ <{subject}> "
            f"<{EX}metadata#confidence> ?c }} }}"
        )
        assert meta == [{"c": "0.95"}]
