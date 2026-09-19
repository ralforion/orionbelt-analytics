"""When the model overrides a fan-trap block, the person is asked.

OBQC blocks a query that sums a measure across a one-to-many join, because the
total comes out inflated. ``allow_fan_out=True`` lets the *model* override
that, and the inflated number then arrives with only a warning attached.
Whether inflated totals are acceptable is for the person reading them to say,
so a client that can be asked is asked. OBQC stays deterministic throughout:
the answer never changes what is detected, only whether the override stands.

The shop below has two orders worth 150 in total; joined to their four items
the sum reads 350.
"""

import duckdb
import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

import src.main as main_module
import src.server_state as state_module
from src.handlers import connection as connection_handler
from src.main import mcp
from src.server_state import ServerState

FAN_TRAP = (
    "SELECT SUM(o.amount) AS total FROM main.orders o "
    "JOIN main.order_items i ON i.order_id = o.id"
)
NO_FAN_TRAP = "SELECT SUM(o.amount) AS total FROM main.orders o"
ERAS = pytest.mark.parametrize("mode", [None, "legacy"], ids=["modern", "handshake"])


@pytest.fixture
def shop(monkeypatch, tmp_path):
    database = tmp_path / "shop.duckdb"
    connection = duckdb.connect(str(database))
    connection.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount DECIMAL(10,2))"
    )
    connection.execute(
        "CREATE TABLE order_items (id INTEGER PRIMARY KEY, "
        "order_id INTEGER REFERENCES orders(id), qty INTEGER)"
    )
    connection.execute("INSERT INTO orders VALUES (1, 100), (2, 50)")
    connection.execute(
        "INSERT INTO order_items VALUES (1, 1, 1), (2, 1, 2), (3, 1, 1), (4, 2, 5)"
    )
    connection.close()

    output = tmp_path / "out"
    output.mkdir()
    state = ServerState()
    monkeypatch.setattr(state_module, "_server_state", state)
    monkeypatch.setattr(main_module, "_server_state", state)
    monkeypatch.setattr("src.paths.OUTPUT_DIR", output)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", output)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(database))
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("AUTO_GRAPHRAG", "false")
    yield
    state.cleanup()


def _user(confirm: bool | None, asked: list[str]):
    """A person at the client. ``None`` dismisses the question."""

    async def answer(message, response_type, params, context):
        asked.append(message)
        if confirm is None:
            return ElicitResult(action="cancel")
        (field,) = params.requested_schema["properties"]
        return ElicitResult(action="accept", content={field: confirm})

    return answer


async def _run(client: Client, sql: str, allow_fan_out: bool) -> dict:
    connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
    handle = connected.data.split("Connection handle: ")[1].split()[0]
    shared = {"connection": handle}
    await client.call_tool("discover_schema", {"schema_name": "main", **shared})
    await client.call_tool(
        "generate_ontology", {"schema_name": "main", "auto_persist": False, **shared}
    )
    result = await client.call_tool(
        "execute_sql_query",
        {
            "sql_query": sql,
            "checklist_completed": True,
            "allow_fan_out": allow_fan_out,
            **shared,
        },
    )
    return result.data


def _client(mode, handler=None) -> Client:
    kwargs = {} if mode is None else {"mode": mode}
    if handler is not None:
        kwargs["elicitation_handler"] = handler
    return Client(mcp, **kwargs)


@ERAS
async def test_the_user_can_refuse_the_models_override(shop, mode):
    asked: list[str] = []
    async with _client(mode, _user(False, asked)) as client:
        result = await _run(client, FAN_TRAP, allow_fan_out=True)

    assert len(asked) == 1
    assert "order_items" in asked[0] and "inflated" in asked[0]
    assert result["success"] is False
    assert result["data"] == []
    assert result["obqc_fan_trap"]["blocking"] is True
    assert "declined" in result["obqc_issues"][0]
    assert "Do not retry the override" in result["obqc_issues"][0]


@ERAS
async def test_dismissing_the_question_is_a_no(shop, mode):
    asked: list[str] = []
    async with _client(mode, _user(None, asked)) as client:
        result = await _run(client, FAN_TRAP, allow_fan_out=True)

    assert asked
    assert result["success"] is False


@ERAS
async def test_the_user_can_accept_inflated_totals(shop, mode):
    asked: list[str] = []
    async with _client(mode, _user(True, asked)) as client:
        result = await _run(client, FAN_TRAP, allow_fan_out=True)

    assert len(asked) == 1
    assert result["success"] is True
    assert float(result["data"][0]["total"]) == 350.0  # inflated, knowingly
    assert result["obqc_fan_trap"]["detected"] is True
    assert any("accepted by the user" in w for w in result["warnings"])


@ERAS
async def test_a_client_that_cannot_be_asked_keeps_the_old_override(shop, mode):
    async with _client(mode) as client:
        result = await _run(client, FAN_TRAP, allow_fan_out=True)

    assert result["success"] is True
    assert any("accepted via allow_fan_out" in w for w in result["warnings"])


@ERAS
async def test_nobody_is_asked_when_nothing_was_overridden(shop, mode):
    asked: list[str] = []
    async with _client(mode, _user(True, asked)) as client:
        blocked = await _run(client, FAN_TRAP, allow_fan_out=False)
    async with _client(mode, _user(True, asked)) as client:
        clean = await _run(client, NO_FAN_TRAP, allow_fan_out=True)

    assert asked == []
    assert blocked["success"] is False  # OBQC blocked it on its own
    assert float(clean["data"][0]["total"]) == 150.0  # the true total
