"""The connection's working schema is known, announced and used.

Without a real schema name a client cannot qualify tables as schema.table, as
the server instructions ask; discover_schema() without a schema reported
"default", so the model went looking with list_schemas. PostgreSQL had no
schema setting at all: a POSTGRES_SCHEMA line was silently ignored.
"""

import re
from typing import Any

import pytest
from fastmcp import Client

import src.main as main_module
import src.server_state as state_module
from src.database_registry import configured_databases
from src.handlers import connection as connection_handler
from src.main import mcp
from src.server_state import ServerState


@pytest.fixture
def duck(monkeypatch, tmp_path):
    import duckdb

    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE things (id INTEGER PRIMARY KEY)")
    con.execute("CREATE SCHEMA archive")
    con.execute("CREATE TABLE archive.old_things (id INTEGER PRIMARY KEY)")
    con.close()
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setenv("AUTO_GRAPHRAG", "false")
    monkeypatch.delenv("OBA_DATABASES", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(path))
    yield fresh
    fresh.cleanup()


def _text(result: Any) -> str:
    return result.data if isinstance(result.data, str) else str(result.data)


async def test_connecting_names_the_working_schema(duck):
    async with Client(mcp) as client:
        connected = _text(
            await client.call_tool("connect_database", {"db_type": "duckdb"})
        )

    assert "Working schema: main" in connected
    assert "main.<table>" in connected


async def test_discovering_without_a_schema_uses_its_real_name(duck):
    async with Client(mcp) as client:
        connected = _text(
            await client.call_tool("connect_database", {"db_type": "duckdb"})
        )
        handle = re.search(r"(ob_[a-z0-9]{6})", connected).group(1)
        discovered = (
            await client.call_tool("discover_schema", {"connection": handle})
        ).data

    assert discovered["schema"] == "main"
    session = duck.session_for_handle(handle)
    assert session.current_schema == "main"


def test_the_database_names_its_current_schema():
    from src.database_manager import DatabaseManager

    manager = DatabaseManager()
    assert manager.connect_duckdb(":memory:")

    assert manager.resolve_working_schema() == "main"


def test_a_postgres_schema_is_configurable_and_shown():
    env = {
        "POSTGRES_HOST": "db",
        "POSTGRES_DATABASE": "shop",
        "POSTGRES_SCHEMA": "sales",
    }

    (entry,) = configured_databases(env)

    assert entry.getenv("POSTGRES_SCHEMA") == "sales"
    assert entry.describe()["schema"] == "sales"


def _handle(text: str) -> str:
    return re.search(r"(ob_[a-z0-9]{6})", text).group(1)


async def test_each_session_keeps_its_own_working_schema(duck):
    async with Client(mcp) as client:
        first = _handle(
            _text(await client.call_tool("connect_database", {"db_type": "duckdb"}))
        )
        second = _handle(
            _text(await client.call_tool("connect_database", {"db_type": "duckdb"}))
        )
        one, two = duck.session_for_handle(first), duck.session_for_handle(second)
        assert one.runtime is two.runtime  # one shared database manager
        # As if two named connections differed only in their schema.
        two.working_schema = "archive"

        mine = (await client.call_tool("discover_schema", {"connection": first})).data
        theirs = (
            await client.call_tool("discover_schema", {"connection": second})
        ).data

    assert (mine["schema"], theirs["schema"]) == ("main", "archive")


async def test_a_fresh_ontology_is_generated_for_the_working_schema(duck):
    async with Client(mcp) as client:
        handle = _handle(
            _text(await client.call_tool("connect_database", {"db_type": "duckdb"}))
        )
        await client.call_tool(
            "generate_ontology", {"connection": handle, "auto_persist": False}
        )

    session = duck.session_for_handle(handle)
    assert session.current_schema == "main"
    assert session.ontology_file and "_main_" in session.ontology_file


async def test_restore_keeps_the_working_schema_and_says_what_it_covers(
    duck, monkeypatch
):
    import sys

    from src.workspace import detect_workspace

    # Every module that captured OUTPUT_DIR at import writes or reads the
    # workspace this test needs; point them all at the test directory.
    output_dir = connection_handler.OUTPUT_DIR
    for name, module in list(sys.modules.items()):
        if name.startswith("src") and hasattr(module, "OUTPUT_DIR"):
            monkeypatch.setattr(module, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(connection_handler, "detect_workspace", detect_workspace)
    async with Client(mcp) as client:
        first = _handle(
            _text(await client.call_tool("connect_database", {"db_type": "duckdb"}))
        )
        await client.call_tool(
            "generate_ontology",
            {"connection": first, "schema_name": "archive", "auto_persist": False},
        )
        # A later connection finds archive's workspace.
        reconnected = _text(
            await client.call_tool("connect_database", {"db_type": "duckdb"})
        )

    session = duck.session_for_handle(_handle(reconnected))
    assert session.current_schema == "main"
    assert "working schema 'main' has nothing restored" in reconnected


async def test_after_restore_generation_targets_the_working_schema(duck, monkeypatch):
    import sys

    from src.workspace import detect_workspace

    output_dir = connection_handler.OUTPUT_DIR
    for name, module in list(sys.modules.items()):
        if name.startswith("src") and hasattr(module, "OUTPUT_DIR"):
            monkeypatch.setattr(module, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(connection_handler, "detect_workspace", detect_workspace)
    async with Client(mcp) as client:
        first = _handle(
            _text(await client.call_tool("connect_database", {"db_type": "duckdb"}))
        )
        # A fully discovered archive workspace: schema and ontology.
        await client.call_tool(
            "discover_schema",
            {"connection": first, "schema_name": "archive", "lightweight": False},
        )
        await client.call_tool(
            "generate_ontology",
            {"connection": first, "schema_name": "archive", "auto_persist": False},
        )

    # A server restart: nothing in memory, the workspace on disk.
    duck.cleanup()
    restarted = ServerState()
    monkeypatch.setattr(state_module, "_server_state", restarted)
    monkeypatch.setattr(main_module, "_server_state", restarted)
    async with Client(mcp) as client:
        second = _handle(
            _text(await client.call_tool("connect_database", {"db_type": "duckdb"}))
        )
        await client.call_tool(
            "generate_ontology", {"connection": second, "auto_persist": False}
        )

    session = restarted.session_for_handle(second)
    assert session.get_last_analyzed_schema() == "main"
    assert session.current_schema == "main"
    assert session.ontology_file and "_main_" in session.ontology_file


async def test_reconnecting_a_shared_handle_resets_its_default_target(duck):
    async with Client(mcp) as client:
        mine = _handle(
            _text(await client.call_tool("connect_database", {"db_type": "duckdb"}))
        )
        # Another session on the same database keeps the runtime shared, so
        # reconnecting does not clear the schema cache.
        await client.call_tool("connect_database", {"db_type": "duckdb"})
        await client.call_tool(
            "discover_schema", {"connection": mine, "schema_name": "archive"}
        )
        reconnected = _text(
            await client.call_tool(
                "connect_database", {"db_type": "duckdb", "connection": mine}
            )
        )
        await client.call_tool(
            "generate_ontology", {"connection": mine, "auto_persist": False}
        )

    assert "Working schema: main" in reconnected
    session = duck.session_for_handle(mine)
    assert session.get_last_analyzed_schema() == "main"
    assert session.ontology_file and "_main_" in session.ontology_file
