"""OBQC's view of an ontology is extracted once per revision, not per session.

Parsing Turtle and extracting from it is what the first query after an ontology
is generated pays for: 672 ms at 300 tables, against 0.07 ms to load an
extraction already in hand. The extraction depends on the ontology and the base
URI and on nothing else, so it is kept on the connection and shared; the
validator built from it stays per session, because its views and its per-query
state belong to a user.
"""

import copy
import types
from pathlib import Path

import pytest
from rdflib import Graph

import src.server_state as server_state
from src.obqc_validator import OBQCValidator, prepare_ontology
from src.session import ConnectionRuntime, PreparedOntologyCache
from tests.test_obqc_validator import create_sample_ontology_graph

QUERIES = [
    "SELECT * FROM customers",
    "SELECT c.name, o.total FROM customers c JOIN orders o ON c.id = o.customer_id",
    "SELECT customer_id, sum(total) FROM orders GROUP BY customer_id",
    "SELECT nosuchcolumn FROM customers",
    "SELECT * FROM nosuchtable",
    "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent",
]


class TestPreparedGivesTheSameVerdicts:
    """An extraction must validate exactly as the graph it came from did."""

    def test_every_query_gets_the_same_result(self) -> None:
        graph, base_uri = create_sample_ontology_graph()
        from_graph = OBQCValidator()
        from_graph.load_ontology(graph, base_uri)

        from_prepared = OBQCValidator()
        from_prepared.load_prepared(prepare_ontology(graph, base_uri))

        for query in QUERIES:
            expected = from_graph.validate(query)
            actual = from_prepared.validate(query)
            assert actual.to_dict() == expected.to_dict(), query

    def test_compatibility_is_carried_over(self) -> None:
        graph, base_uri = create_sample_ontology_graph()

        prepared = prepare_ontology(graph, base_uri)
        validator = OBQCValidator()
        validator.load_prepared(prepared)

        assert prepared.is_compatible is True
        assert validator.is_compatible is True

    def test_an_ontology_without_oba_annotations_stays_incompatible(self) -> None:
        graph = Graph()
        graph.parse(
            data="""@prefix owl: <http://www.w3.org/2002/07/owl#> .
            <http://x/Thing> a owl:Class .""",
            format="turtle",
        )

        prepared = prepare_ontology(graph, "http://x/")
        validator = OBQCValidator()
        validator.load_prepared(prepared)

        assert prepared.is_compatible is False
        assert validator.is_compatible is False

    def test_preparing_without_an_ontology_is_an_error(self) -> None:
        with pytest.raises(ValueError):
            OBQCValidator().prepared_ontology()


class TestSharedDataIsNotMutated:
    """Two sessions share one extraction, so validation must only read it."""

    def test_validating_leaves_the_extraction_untouched(self) -> None:
        graph, base_uri = create_sample_ontology_graph()
        prepared = prepare_ontology(graph, base_uri)
        before = copy.deepcopy(prepared)

        validator = OBQCValidator()
        validator.load_prepared(prepared)
        for query in QUERIES:
            validator.validate(query)

        assert prepared.schema.tables.keys() == before.schema.tables.keys()
        for key, table in prepared.schema.tables.items():
            assert table.columns == before.schema.tables[key].columns
        assert prepared.disjoint_pairs == before.disjoint_pairs
        assert prepared.views == before.views

    def test_registering_views_does_not_reach_the_extraction(self) -> None:
        graph, base_uri = create_sample_ontology_graph()
        prepared = prepare_ontology(graph, base_uri)
        first, second = OBQCValidator(), OBQCValidator()
        first.load_prepared(prepared)
        second.load_prepared(prepared)

        first.load_views({"v_sales": {"total"}})

        assert "v_sales" not in second._known_views


class TestTheCacheIsBounded:
    """A few revisions are kept, the least recently used dropped."""

    def test_an_entry_comes_back(self) -> None:
        cache = PreparedOntologyCache()
        cache.put(("a",), "first")

        assert cache.get(("a",)) == "first"
        assert cache.get(("b",)) is None

    def test_the_oldest_is_dropped_when_full(self) -> None:
        cache = PreparedOntologyCache(capacity=2)
        cache.put(("a",), 1)
        cache.put(("b",), 2)
        cache.put(("c",), 3)

        assert len(cache) == 2
        assert cache.get(("a",)) is None
        assert cache.get(("c",)) == 3

    def test_reading_an_entry_keeps_it(self) -> None:
        cache = PreparedOntologyCache(capacity=2)
        cache.put(("a",), 1)
        cache.put(("b",), 2)
        cache.get(("a",))
        cache.put(("c",), 3)

        assert cache.get(("a",)) == 1
        assert cache.get(("b",)) is None

    def test_clear_drops_everything(self) -> None:
        cache = PreparedOntologyCache()
        cache.put(("a",), 1)
        cache.clear()

        assert len(cache) == 0


def _session(
    runtime: ConnectionRuntime | None,
    ontology_file: str | None = None,
    loaded_ontology: str | None = None,
    connection_id: str | None = None,
):
    return types.SimpleNamespace(
        runtime=runtime,
        ontology_file=ontology_file,
        loaded_ontology=loaded_ontology,
        loaded_ontology_path=None,
        connection_id=connection_id,
    )


@pytest.fixture
def ontology_file(tmp_path, monkeypatch) -> Path:
    graph, _ = create_sample_ontology_graph()
    path = tmp_path / "ontology_main.ttl"
    path.write_text(graph.serialize(format="turtle"), encoding="utf-8")
    monkeypatch.setattr(server_state, "ensure_output_dir", lambda: tmp_path)
    return path


def _count_parses(monkeypatch) -> list[str]:
    parses: list[str] = []
    real = server_state.OntologyGenerator

    class Counting(real):  # type: ignore[misc, valid-type]
        def load_from_file(self, path):
            parses.append(str(path))
            return super().load_from_file(path)

        def load_from_string(self, content):
            parses.append("<string>")
            return super().load_from_string(content)

    monkeypatch.setattr(server_state, "OntologyGenerator", Counting)
    return parses


BASE = "http://example.com/ontology/"


class TestOneParsePerRevision:
    """The connection's cache is what makes the second session free."""

    def test_two_sessions_on_one_connection_parse_once(
        self, ontology_file, monkeypatch
    ):
        runtime = ConnectionRuntime("conn-1")
        parses = _count_parses(monkeypatch)
        first = _session(runtime, ontology_file="ontology_main.ttl")
        second = _session(runtime, ontology_file="ontology_main.ttl")

        a = server_state._prepared_ontology_for(first, BASE)
        b = server_state._prepared_ontology_for(second, BASE)

        assert len(parses) == 1
        assert a is b

    def test_a_rewritten_ontology_is_parsed_again(self, ontology_file, monkeypatch):
        runtime = ConnectionRuntime("conn-1")
        parses = _count_parses(monkeypatch)
        session = _session(runtime, ontology_file="ontology_main.ttl")

        server_state._prepared_ontology_for(session, BASE)
        ontology_file.write_text(
            ontology_file.read_text() + "\n# a change\n", encoding="utf-8"
        )
        server_state._prepared_ontology_for(session, BASE)

        assert len(parses) == 2

    def test_a_different_base_uri_is_not_the_same_revision(
        self, ontology_file, monkeypatch
    ):
        runtime = ConnectionRuntime("conn-1")
        parses = _count_parses(monkeypatch)
        session = _session(runtime, ontology_file="ontology_main.ttl")

        server_state._prepared_ontology_for(session, BASE)
        server_state._prepared_ontology_for(session, "http://other/")

        assert len(parses) == 2

    def test_an_ontology_loaded_as_text_is_keyed_by_its_content(self, monkeypatch):
        runtime = ConnectionRuntime("conn-1")
        parses = _count_parses(monkeypatch)
        graph, _ = create_sample_ontology_graph()
        content = graph.serialize(format="turtle")
        first = _session(runtime, loaded_ontology=content)
        second = _session(runtime, loaded_ontology=content)
        other = _session(runtime, loaded_ontology=content + "\n# other\n")

        server_state._prepared_ontology_for(first, BASE)
        server_state._prepared_ontology_for(second, BASE)
        server_state._prepared_ontology_for(other, BASE)

        assert len(parses) == 2

    def test_no_ontology_prepares_nothing(self, monkeypatch):
        parses = _count_parses(monkeypatch)
        session = _session(ConnectionRuntime("conn-1"))

        assert server_state._prepared_ontology_for(session, BASE) is None
        assert parses == []

    def test_a_session_without_a_runtime_still_works(self, ontology_file, monkeypatch):
        """Batch use and tests bind no runtime: parse every time, cache nothing."""
        parses = _count_parses(monkeypatch)
        session = _session(None, ontology_file="ontology_main.ttl")

        assert server_state._prepared_ontology_for(session, BASE) is not None
        assert server_state._prepared_ontology_for(session, BASE) is not None
        assert len(parses) == 2


class TestRememberingSkipsTheParseEntirely:
    """What generate_ontology does with the graph it already holds."""

    def test_a_remembered_file_is_never_parsed(self, ontology_file, monkeypatch):
        runtime = ConnectionRuntime("conn-1")
        parses = _count_parses(monkeypatch)
        session = _session(runtime, ontology_file="ontology_main.ttl")
        graph, _ = create_sample_ontology_graph()

        server_state.remember_prepared_ontology(
            session, graph, BASE, path=ontology_file
        )
        prepared = server_state._prepared_ontology_for(session, BASE)

        assert parses == []
        assert prepared is not None
        assert prepared.is_compatible

    def test_a_remembered_text_is_never_parsed(self, monkeypatch):
        runtime = ConnectionRuntime("conn-1")
        parses = _count_parses(monkeypatch)
        graph, _ = create_sample_ontology_graph()
        content = graph.serialize(format="turtle")
        session = _session(runtime, loaded_ontology=content)

        server_state.remember_prepared_ontology(session, graph, BASE, text=content)
        prepared = server_state._prepared_ontology_for(session, BASE)

        assert parses == []
        assert prepared is not None

    def test_a_rewritten_file_is_not_answered_by_the_remembered_one(
        self, ontology_file, monkeypatch
    ):
        runtime = ConnectionRuntime("conn-1")
        session = _session(runtime, ontology_file="ontology_main.ttl")
        graph, _ = create_sample_ontology_graph()
        server_state.remember_prepared_ontology(
            session, graph, BASE, path=ontology_file
        )
        parses = _count_parses(monkeypatch)

        ontology_file.write_text(
            ontology_file.read_text() + "\n# changed\n", encoding="utf-8"
        )
        server_state._prepared_ontology_for(session, BASE)

        assert len(parses) == 1

    def test_a_failure_to_remember_is_not_an_error(self, ontology_file):
        session = _session(ConnectionRuntime("conn-1"))

        server_state.remember_prepared_ontology(
            session, "not a graph", BASE, path=ontology_file
        )

        assert len(session.runtime.obqc_prepared) == 0

    def test_a_session_without_a_runtime_remembers_nothing(self, ontology_file):
        graph, _ = create_sample_ontology_graph()

        server_state.remember_prepared_ontology(
            _session(None), graph, BASE, path=ontology_file
        )
