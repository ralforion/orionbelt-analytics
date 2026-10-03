"""The prefixes query_sparql promises are predeclared, and a bad query is not an error log.

The tool description told the model rdf, rdfs, owl and xsd were "available by
default", but nothing declared them: a query using owl:Class without a PREFIX
line failed with "Prefix not found", and the model needed a second attempt.
Each failure also logged two full tracebacks for what is the query's own
mistake, already returned to the caller.
"""

import logging

import pytest

pytest.importorskip("pyoxigraph")

from src.oxigraph_store import OxigraphStoreManager

TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix oba: <https://ralforion.com/ns/oba#> .
<http://e/Orders> a owl:Class ; rdfs:label "Orders" ; oba:tableName "orders" .
"""


@pytest.fixture
def store() -> OxigraphStoreManager:
    manager = OxigraphStoreManager()
    manager.load_ontology(TTL, "http://e/graph", "gold")
    return manager


def test_the_promised_prefixes_need_no_declaration(store):
    rows = store.query_sparql(
        "SELECT ?t WHERE { ?c a owl:Class ; rdfs:label ?l ; oba:tableName ?t }"
    )

    assert rows == [{"t": "orders"}]


def test_ask_and_construct_get_them_too(store):
    assert store.query_sparql_ask("ASK { ?c a owl:Class }") is True
    assert "Orders" in store.query_sparql_construct(
        "CONSTRUCT { ?c rdfs:label ?l } WHERE { ?c rdfs:label ?l }"
    )


def test_a_prefix_the_query_declares_wins(store):
    rows = store.query_sparql(
        "PREFIX owl: <http://example.com/not-owl#> SELECT ?c WHERE { ?c a owl:Class }"
    )

    assert rows == []


def test_a_bad_query_is_a_warning_without_a_traceback(store, caplog):
    with (
        caplog.at_level(logging.WARNING, logger="src.oxigraph_store"),
        pytest.raises(SyntaxError),
    ):
        store.query_sparql("SELECT ?c WHERE { ?c a undeclared:Thing }")

    records = [r for r in caplog.records if r.name == "src.oxigraph_store"]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert records[0].exc_info is None
    assert "\n" not in records[0].getMessage()
