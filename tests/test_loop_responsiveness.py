"""The event loop keeps serving while the slow work of any tool is in flight.

The plan's P2 asks for responsiveness tests on four paths the older
blocking-I/O guards never covered: driver calls, embedding, retrieval, and
building a validator lazily. Each test here parks the slow part on an event
and asserts a trivial task keeps getting turns meanwhile -- the property a
blocked loop breaks for every other session on the server.

Two of the four were still on the loop when these were written: retrieval
spent 75 ms of every call embedding the query, and a validator built after a
restart parsed the ontology for 709 ms at 300 tables.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import src.server_state as server_state
from src.async_utils import run_db
from src.database_manager import DatabaseManager
from src.graphrag.manager import GraphRAGManager
from src.session import ConnectionRuntime
from tests.test_obqc_validator import create_sample_ontology_graph


async def _loop_turns_while(
    start: Callable[[], Awaitable[Any]],
    inside: threading.Event,
    release: threading.Event,
) -> tuple[Any, int]:
    """Start the work, count loop turns while it is parked, then let it finish.

    Args:
        start: Makes the awaitable to run.
        inside: Set by the work once it is parked in its slow part.
        release: Set here to let the work finish.

    Returns:
        The work's result, and how many turns a trivial task got meanwhile.
    """
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0)

    work = asyncio.create_task(start())
    assert await asyncio.to_thread(inside.wait, 5), "the slow part never started"
    counting = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    release.set()
    result = await work
    await counting
    return result, ticks


class TestDriverCalls:
    async def test_the_loop_runs_during_a_database_call(self):
        inside, release = threading.Event(), threading.Event()

        class Manager(DatabaseManager):
            def slow(self) -> str:
                inside.set()
                assert release.wait(timeout=5)
                return "rows"

        manager = Manager()
        result, ticks = await _loop_turns_while(
            lambda: run_db(manager.slow), inside, release
        )

        assert result == "rows"
        assert ticks > 0


@pytest.fixture
def graphrag(tmp_path, monkeypatch) -> GraphRAGManager:
    monkeypatch.setattr(
        "src.graphrag.vector_store_chromadb.OUTPUT_DIR", tmp_path / "chroma"
    )
    manager = GraphRAGManager(
        embedding_model="tfidf", connection_id="loop", schema_name="public"
    )
    manager.initialize_from_schema(
        [
            {
                "name": "orders",
                "schema": "public",
                "comment": "purchase orders",
                "columns": [
                    {"name": "id", "data_type": "INTEGER", "is_primary_key": True},
                    {"name": "total", "data_type": "DECIMAL"},
                ],
                "foreign_keys": [],
            }
        ],
        schema_name="public",
    )
    return manager


def _park_embedding(
    manager: GraphRAGManager,
) -> tuple[threading.Event, threading.Event]:
    inside, release = threading.Event(), threading.Event()
    real = manager.embedder._embed_text

    def slow(text: str) -> Any:
        inside.set()
        assert release.wait(timeout=5)
        return real(text)

    manager.embedder._embed_text = slow  # type: ignore[method-assign]
    return inside, release


class TestEmbedding:
    async def test_the_loop_runs_while_a_schema_is_embedded(self, graphrag):
        inside, release = threading.Event(), threading.Event()
        real = graphrag.embedder.batch_embed_schema

        def slow(tables: Any, views: Any = None) -> Any:
            inside.set()
            assert release.wait(timeout=5)
            return real(tables, views)

        graphrag.embedder.batch_embed_schema = slow  # type: ignore[method-assign]
        table = {
            "name": "invoices",
            "schema": "public",
            "columns": [{"name": "id", "data_type": "INTEGER"}],
            "foreign_keys": [],
        }

        _, ticks = await _loop_turns_while(
            lambda: graphrag.aindex_schema([table], "public", accumulate=True),
            inside,
            release,
        )

        assert ticks > 0


class TestRetrieval:
    """Embedding the query is 75 of ~82 ms; it runs in a worker now."""

    async def test_the_loop_runs_while_a_query_context_is_embedded(self, graphrag):
        inside, release = _park_embedding(graphrag)

        context, ticks = await _loop_turns_while(
            lambda: graphrag.aget_query_context("order totals"), inside, release
        )

        assert ticks > 0
        assert context["relevant_tables"]

    async def test_the_loop_runs_while_a_search_is_embedded(self, graphrag):
        inside, release = _park_embedding(graphrag)

        results, ticks = await _loop_turns_while(
            lambda: graphrag.asearch_schema("purchase orders", top_k=3),
            inside,
            release,
        )

        assert ticks > 0
        assert results

    async def test_the_search_itself_stays_on_the_loop(self, graphrag):
        """It reads what indexing rewrites on the loop; from a thread it could
        see that half-way."""
        loop_thread = threading.get_ident()
        searched_on: list[int] = []
        real = graphrag.vector_store.search

        def recording(*args: Any, **kwargs: Any) -> Any:
            searched_on.append(threading.get_ident())
            return real(*args, **kwargs)

        graphrag.vector_store.search = recording  # type: ignore[method-assign]

        await graphrag.aget_query_context("order totals")

        assert searched_on and set(searched_on) == {loop_thread}

    async def test_async_and_sync_retrieval_agree(self, graphrag):
        sync = graphrag.get_query_context("order totals")
        async_ = await graphrag.aget_query_context("order totals")

        assert [t["name"] for t in async_["relevant_tables"]] == [
            t["name"] for t in sync["relevant_tables"]
        ]


class TestLazyValidator:
    """A validator built after a restart parsed the ontology on the loop."""

    @pytest.fixture
    def ontology_session(self, tmp_path, monkeypatch):
        graph, _ = create_sample_ontology_graph()
        (tmp_path / "ontology_main.ttl").write_text(
            graph.serialize(format="turtle"), encoding="utf-8"
        )
        monkeypatch.setattr(server_state, "ensure_output_dir", lambda: tmp_path)
        state = server_state.ServerState()
        session = state.get_session("s")
        session.bind_runtime(ConnectionRuntime("conn"))
        # Ontology state is per schema: without a current one, the file set
        # below would be invisible and every assertion would pass on nothing.
        session.set_current_schema("public")
        session.ontology_file = "ontology_main.ttl"
        assert session.ontology_file == "ontology_main.ttl"
        monkeypatch.setattr(server_state, "get_session_data", lambda _ctx: session)
        return session

    async def test_the_loop_runs_while_an_ontology_is_parsed(
        self, ontology_session, monkeypatch
    ):
        inside, release = threading.Event(), threading.Event()
        real = server_state._parse_and_prepare

        def slow(*args: Any) -> Any:
            inside.set()
            assert release.wait(timeout=5)
            return real(*args)

        monkeypatch.setattr(server_state, "_parse_and_prepare", slow)

        validator, ticks = await _loop_turns_while(
            lambda: server_state.aget_session_obqc_validator(None), inside, release
        )

        assert ticks > 0
        assert validator is not None and validator.is_compatible

    async def test_the_extraction_is_cached_for_the_next_session(
        self, ontology_session, monkeypatch
    ):
        parses: list[int] = []
        real = server_state._parse_and_prepare

        def counting(*args: Any) -> Any:
            parses.append(1)
            return real(*args)

        monkeypatch.setattr(server_state, "_parse_and_prepare", counting)

        await server_state.aget_session_obqc_validator(None)
        ontology_session.obqc_validator = None  # a second session on the runtime
        await server_state.aget_session_obqc_validator(None)

        assert parses == [1]

    async def test_it_builds_what_the_synchronous_getter_builds(self, ontology_session):
        built = await server_state.aget_session_obqc_validator(None)
        sql = "SELECT nosuchcolumn FROM orders"
        via_async = [issue.message for issue in built.validate(sql).issues]

        ontology_session.obqc_validator = None
        again = server_state.get_session_obqc_validator(None)

        assert via_async == [issue.message for issue in again.validate(sql).issues]

    async def test_no_ontology_means_no_validator_and_no_parse(self, monkeypatch):
        state = server_state.ServerState()
        session = state.get_session("empty")
        monkeypatch.setattr(server_state, "get_session_data", lambda _ctx: session)
        parses: list[int] = []
        monkeypatch.setattr(
            server_state, "_parse_and_prepare", lambda *a: parses.append(1)
        )

        assert await server_state.aget_session_obqc_validator(None) is None
        assert parses == []
