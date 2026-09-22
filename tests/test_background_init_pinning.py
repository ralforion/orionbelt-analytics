"""Background work stays with the database it was started for.

GraphRAG initialisation and AUTO_ONTOLOGY generation outlive the tool call
that started them. The session they were started from can connect to another
database meanwhile, and its facade then points at the *new* database's state --
state every session on that database shares. Read lazily through the session,
the old database's index would become the new database's GraphRAG for
everyone, its ontology would be loaded into the new database's RDF store, and
its metadata written into the wrong workspace.

Each test lets the session move while the work is held up on its first await.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from src.database_manager import ColumnInfo, TableInfo
from src.handlers import graphrag as graphrag_handler
from src.server_state import ServerState, _clear_session_state

OLD, NEW = "conn-old-database", "conn-new-database"


def _tables() -> list[TableInfo]:
    return [
        TableInfo(
            name="orders",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                    comment=None,
                )
            ],
            primary_keys=["id"],
            foreign_keys=[],
            row_count=1,
        )
    ]


class _Manager:
    """Stands in for DatabaseManager."""

    def is_connected(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass


@pytest.fixture
def state(monkeypatch) -> ServerState:
    fresh = ServerState()
    monkeypatch.setattr("src.server_state._server_state", fresh)
    return fresh


def _move_to_new_database(state: ServerState, session) -> None:
    """What connect_database does when the fingerprint changes."""
    _clear_session_state(session, reason="connection change")
    session.connection_id = NEW
    state.bind_session(session, NEW, _Manager())


async def test_graphrag_init_lands_in_the_database_it_was_started_for(
    state, monkeypatch, tmp_path
):
    saving, proceed = threading.Event(), threading.Event()

    class SlowGraphRAG:
        def __init__(self, connection_id, schema_name):
            self.connection_id = connection_id
            self._schema_names = [schema_name]
            self.vector_count = 1
            self.vector_collection_name = "c"
            self.graph_retriever = Mock()
            self.graph_retriever.graph.number_of_nodes.return_value = 1
            self.vector_store = Mock()
            self.vector_store.get_statistics.return_value = {"total_elements": 1}

        def initialize_from_schema(self, **_kwargs):
            pass

        def save_state(self, *_args):
            saving.set()
            assert proceed.wait(timeout=10)
            return []

    recorded = AsyncMock()
    monkeypatch.setattr(graphrag_handler, "GraphRAGManager", SlowGraphRAG)
    monkeypatch.setattr(graphrag_handler, "update_workspace_section", recorded)
    monkeypatch.setattr(graphrag_handler, "update_schema_version", AsyncMock())
    monkeypatch.setattr(graphrag_handler, "ensure_output_dir", lambda: tmp_path)
    monkeypatch.setenv("AUTO_ONTOLOGY", "false")

    mover, stays = state.get_session("mover"), state.get_session("stays")
    for session in (mover, stays):
        session.connection_id = OLD
        state.bind_session(session, OLD, _Manager())

    task = asyncio.create_task(
        graphrag_handler._auto_initialize_graphrag_background(
            schema_name="public", tables_info=_tables(), session=mover, ctx=None
        )
    )
    assert await asyncio.to_thread(saving.wait, 10)
    _move_to_new_database(state, mover)  # while the index is being saved
    proceed.set()
    await task

    # The colleague who stayed on the old database gets the index...
    assert stays.graphrag_initialized is True
    assert stays.graphrag_manager.connection_id == OLD
    # ...and the new database, which the mover now shares, does not.
    assert mover.connection_id == NEW
    assert mover.graphrag_manager is None
    assert mover.graphrag_initialized is False
    assert state.get_runtime(NEW).graphrag.graphrag_manager is None
    # Its metadata went into the old database's workspace.
    assert recorded.await_args.kwargs["connection_id"] == OLD


async def test_auto_ontology_leaves_a_session_that_moved_on_alone(
    state, monkeypatch, tmp_path
):
    generating, proceed = threading.Event(), threading.Event()

    class SlowGenerator:
        def __init__(self, base_uri):
            pass

        def generate_from_schema(self, _tables, views_info=None):
            generating.set()
            assert proceed.wait(timeout=10)
            return "@prefix ex: <http://example.com/> .\n"

    old_dir = tmp_path / OLD
    old_dir.mkdir()
    section, version = AsyncMock(), AsyncMock()
    monkeypatch.setattr(graphrag_handler, "OntologyGenerator", SlowGenerator)
    monkeypatch.setattr(
        graphrag_handler, "get_connection_dir", lambda cid: tmp_path / cid
    )
    monkeypatch.setattr(graphrag_handler, "update_workspace_section", section)
    monkeypatch.setattr(graphrag_handler, "update_schema_version", version)
    monkeypatch.setattr(graphrag_handler, "OXIGRAPH_AVAILABLE", True)

    session = state.get_session("mover")
    session.connection_id = OLD
    state.bind_session(session, OLD, _Manager())

    task = asyncio.create_task(
        graphrag_handler._auto_generate_ontology_background(
            schema_name="public", tables_info=_tables(), session=session, ctx=None
        )
    )
    assert await asyncio.to_thread(generating.wait, 10)
    _move_to_new_database(state, session)
    new_store = Mock(name="rdf-store-of-the-new-database")
    session.oxigraph_store = new_store
    proceed.set()
    await task

    # The old database's ontology is not loaded into the new database's store,
    new_store.load_ontology.assert_not_called()
    # and is not named as the session's ontology for a schema of the same name.
    session.set_current_schema("public")
    assert session.ontology_file is None
    # The file and its metadata are still written, where they belong.
    assert list(old_dir.glob("ontology_public_*.ttl"))
    assert not (tmp_path / NEW).exists()
    assert section.await_args.kwargs["connection_id"] == OLD
    assert version.await_args.kwargs["connection_id"] == OLD
    assert version.await_args.kwargs["updates"]["ontology_graph_uri"] == ""


async def test_auto_ontology_still_updates_a_session_that_stayed(
    state, monkeypatch, tmp_path
):
    class Generator:
        def __init__(self, base_uri):
            pass

        def generate_from_schema(self, _tables, views_info=None):
            return "@prefix ex: <http://example.com/> .\n"

    (tmp_path / OLD).mkdir()
    monkeypatch.setattr(graphrag_handler, "OntologyGenerator", Generator)
    monkeypatch.setattr(
        graphrag_handler, "get_connection_dir", lambda cid: tmp_path / cid
    )
    monkeypatch.setattr(graphrag_handler, "update_workspace_section", AsyncMock())
    monkeypatch.setattr(graphrag_handler, "update_schema_version", AsyncMock())
    monkeypatch.setattr(graphrag_handler, "OXIGRAPH_AVAILABLE", True)
    session = state.get_session("stays")
    session.connection_id = OLD
    state.bind_session(session, OLD, _Manager())
    store = Mock()
    store.load_ontology.return_value = 7
    session.oxigraph_store = store

    await graphrag_handler._auto_generate_ontology_background(
        schema_name="public", tables_info=_tables(), session=session, ctx=None
    )

    store.load_ontology.assert_called_once()
    session.set_current_schema("public")
    assert session.ontology_file.startswith("ontology_public_")


async def test_the_chained_ontology_stays_with_the_old_database_too(
    state, monkeypatch, tmp_path
):
    """AUTO_ONTOLOGY chains ontology generation onto GraphRAG initialisation.
    If the session moved while the index was built, the chained work must not
    pin itself to the new database: it holds the old database's tables."""
    saving, proceed = threading.Event(), threading.Event()

    class SlowGraphRAG:
        def __init__(self, connection_id, schema_name):
            self._schema_names = [schema_name]
            self.vector_count = 1
            self.vector_collection_name = "c"
            self.graph_retriever = Mock()
            self.graph_retriever.graph.number_of_nodes.return_value = 1
            self.vector_store = Mock()
            self.vector_store.get_statistics.return_value = {"total_elements": 1}

        def initialize_from_schema(self, **_kwargs):
            pass

        def save_state(self, *_args):
            saving.set()
            assert proceed.wait(timeout=10)
            return []

    class Generator:
        def __init__(self, base_uri):
            pass

        def generate_from_schema(self, _tables, views_info=None):
            return "@prefix ex: <http://example.com/> .\n"

    (tmp_path / OLD).mkdir()
    version = AsyncMock()
    monkeypatch.setattr(graphrag_handler, "GraphRAGManager", SlowGraphRAG)
    monkeypatch.setattr(graphrag_handler, "OntologyGenerator", Generator)
    monkeypatch.setattr(
        graphrag_handler, "get_connection_dir", lambda cid: tmp_path / cid
    )
    monkeypatch.setattr(graphrag_handler, "ensure_output_dir", lambda: tmp_path)
    monkeypatch.setattr(graphrag_handler, "update_workspace_section", AsyncMock())
    monkeypatch.setattr(graphrag_handler, "update_schema_version", version)
    monkeypatch.setattr(graphrag_handler, "OXIGRAPH_AVAILABLE", True)
    monkeypatch.setenv("AUTO_ONTOLOGY", "true")

    session = state.get_session("mover")
    session.connection_id = OLD
    state.bind_session(session, OLD, _Manager())
    # Another holder, so the move does not cancel the work.
    stays = state.get_session("stays")
    stays.connection_id = OLD
    state.bind_session(stays, OLD, _Manager())

    task = asyncio.create_task(
        graphrag_handler._auto_initialize_graphrag_background(
            schema_name="public", tables_info=_tables(), session=session, ctx=None
        )
    )
    assert await asyncio.to_thread(saving.wait, 10)
    _move_to_new_database(state, session)
    new_store = Mock(name="rdf-store-of-the-new-database")
    session.oxigraph_store = new_store
    proceed.set()
    await task

    assert list((tmp_path / OLD).glob("ontology_public_*.ttl"))
    assert not (tmp_path / NEW).exists()
    new_store.load_ontology.assert_not_called()
    session.set_current_schema("public")
    assert session.ontology_file is None
    ontology_records = [
        call.kwargs
        for call in version.await_args_list
        if "ontology_ttl_file" in call.kwargs["updates"]
    ]
    assert [r["connection_id"] for r in ontology_records] == [OLD]
