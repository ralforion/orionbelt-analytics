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
    manager.disconnect()
    assert manager.working_schema is None


def test_a_postgres_schema_is_configurable_and_shown():
    env = {
        "POSTGRES_HOST": "db",
        "POSTGRES_DATABASE": "shop",
        "POSTGRES_SCHEMA": "sales",
    }

    (entry,) = configured_databases(env)

    assert entry.getenv("POSTGRES_SCHEMA") == "sales"
    assert entry.describe()["schema"] == "sales"
