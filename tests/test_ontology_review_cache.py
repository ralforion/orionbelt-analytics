"""suggest_semantic_names parses its ontology at most once per file.

Parsing dominates the tool: 1.7s of the 1.9s a 400-table ontology costs. A
2026-07-28 request runs the whole tool body once per round, so the naive
version paid that twice for one answer, and every repeat call paid it again.
"""

import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.handler_context import HandlerContext
from src.handlers import ontology_semantic as handler
from src.session import ConnectionRuntime

TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix oba: <https://w3id.org/oba#> .
<http://x/Acctbal> a owl:Class ; rdfs:label "acctbal" ; oba:tableName "acctbal" .
"""


@pytest.fixture
def ontology(tmp_path) -> Path:
    path = tmp_path / "ontology_main.ttl"
    path.write_text(TTL, encoding="utf-8")
    return path


def _services(session) -> HandlerContext:
    return HandlerContext(get_session_data=lambda _ctx: session)


def _session_on(runtime: ConnectionRuntime | None):
    return types.SimpleNamespace(runtime=runtime)


_REAL_GENERATOR = handler.OntologyGenerator


def _count_parses(monkeypatch) -> list[int]:
    """Patch the generator once, and return the list it appends a mark to."""
    parses: list[int] = []

    class Counting(_REAL_GENERATOR):  # type: ignore[misc, valid-type]
        def load_from_file(self, path):
            parses.append(1)
            return super().load_from_file(path)

    monkeypatch.setattr(handler, "OntologyGenerator", Counting)
    return parses


async def _review(ontology: Path, session):
    return await handler._names_for_review(Mock(), ontology, _services(session))


async def test_the_same_file_is_parsed_once(ontology, monkeypatch):
    runtime = ConnectionRuntime("conn-1")
    session = _session_on(runtime)
    parses = _count_parses(monkeypatch)

    first = await _review(ontology, session)
    second = await _review(ontology, session)

    assert len(parses) == 1
    assert first == second
    assert first["summary"] == second["summary"]


async def test_a_rewritten_file_is_parsed_again(ontology, monkeypatch):
    runtime = ConnectionRuntime("conn-1")
    session = _session_on(runtime)
    parses = _count_parses(monkeypatch)

    await _review(ontology, session)
    ontology.write_text(
        TTL + '<http://x/Bnknm> a owl:Class ; rdfs:label "bnknm" .\n', encoding="utf-8"
    )
    again = await _review(ontology, session)

    assert len(parses) == 2
    assert len(again["classes"]) == 2


async def test_another_file_does_not_answer_for_this_one(
    ontology, monkeypatch, tmp_path
):
    runtime = ConnectionRuntime("conn-1")
    session = _session_on(runtime)
    parses = _count_parses(monkeypatch)
    other = tmp_path / "ontology_other.ttl"
    other.write_text(TTL, encoding="utf-8")

    await _review(ontology, session)
    await _review(other, session)

    assert len(parses) == 2


async def test_callers_cannot_edit_what_the_next_one_reads(ontology, monkeypatch):
    runtime = ConnectionRuntime("conn-1")
    session = _session_on(runtime)
    parses = _count_parses(monkeypatch)

    first = await _review(ontology, session)
    first["classes"].clear()
    first["summary"]["total_classes"] = 99
    second = await _review(ontology, session)

    assert len(parses) == 1
    assert second["classes"]
    assert second["summary"]["total_classes"] != 99


async def test_a_session_sharing_nothing_still_works(ontology, monkeypatch):
    """No runtime bound (tests, batch use): parse every time, cache nothing."""
    session = _session_on(None)
    parses = _count_parses(monkeypatch)

    await _review(ontology, session)
    await _review(ontology, session)

    assert len(parses) == 2


async def test_sessions_on_one_database_share_the_parse(ontology, monkeypatch):
    runtime = ConnectionRuntime("conn-1")
    parses = _count_parses(monkeypatch)

    await _review(ontology, _session_on(runtime))
    await _review(ontology, _session_on(runtime))

    assert len(parses) == 1
