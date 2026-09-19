"""How suggest_semantic_names gets its rename suggestions.

FastMCP 4 removed ``ctx.sample``. Its replacement is a multi round-trip
request (MCP 2026-07-28, SEP-2322): the tool returns the request instead of a
result, the client fulfils it and calls the tool again with the answer. That
only works for a client that speaks the new era *and* can sample, and it has
to be known before asking, so everyone else gets the review path, where the
client model proposes the names itself.
"""

import json
import logging
import types

import duckdb
import mcp.types as mcp_types
import pytest
from fastmcp import Client

import src.main as main_module
import src.server_state as state_module
from src.config import ConfigManager, _resolve_semantic_naming_mode
from src.handlers import connection as connection_handler
from src.handlers import ontology_semantic as handler
from src.handlers.ontology_semantic import NamingStrategy, _select_naming_strategy
from src.main import mcp
from src.server_state import ServerState

MODEL_ANSWER = json.dumps(
    {
        "classes": [
            {
                "original_name": "acctbal",
                "suggested_name": "AccountBalance",
                "description": "Account balance records",
            }
        ],
        "properties": [],
        "relationships": [],
    }
)


def _ctx(revision: str | None, can_sample: bool, responses: object = None):
    session = types.SimpleNamespace(
        check_client_capability=lambda _capability: can_sample
    )
    return types.SimpleNamespace(
        request_context=types.SimpleNamespace(protocol_version=revision),
        session=session,
        input_responses=responses,
    )


# --- which clients can be asked ---


@pytest.mark.parametrize(
    ("revision", "can_sample", "mode", "expected"),
    [
        ("2026-07-28", True, "auto", NamingStrategy.INPUT_REQUIRED),
        ("2026-07-28", True, "input_required", NamingStrategy.INPUT_REQUIRED),
        # A modern client without a model would fail after round one, on its
        # own side, where the server can no longer fall back.
        ("2026-07-28", False, "auto", NamingStrategy.REVIEW),
        # The result type does not exist for a handshake-era client, whatever
        # it can do: OrionBelt Chat on MCP SDK 1.x is this row.
        ("2025-11-25", True, "auto", NamingStrategy.REVIEW),
        (None, True, "auto", NamingStrategy.REVIEW),
        # The operator's word is final.
        ("2026-07-28", True, "review", NamingStrategy.REVIEW),
    ],
)
def test_strategy_selection(revision, can_sample, mode, expected):
    assert _select_naming_strategy(_ctx(revision, can_sample), mode) is expected


def test_asking_for_input_required_says_why_it_is_not_happening(caplog):
    with caplog.at_level(logging.WARNING, logger=handler.logger.name):
        strategy = _select_naming_strategy(_ctx("2025-11-25", True), "input_required")

    assert strategy is NamingStrategy.REVIEW
    assert "2026-07-28" in caplog.text


def test_a_client_whose_capabilities_cannot_be_read_gets_the_review_path():
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(protocol_version="2026-07-28"),
        session=types.SimpleNamespace(),  # no check_client_capability at all
    )

    assert _select_naming_strategy(ctx, "auto") is NamingStrategy.REVIEW


# --- the two rounds ---


def test_round_one_is_a_sampling_request_with_todays_prompt_and_limits():
    result = handler._ask_client_model(["CLASS  acctbal", "PROP   acctbal.bankid"])

    assert isinstance(result, mcp_types.InputRequiredResult)
    request = result.input_requests[handler._RENAMES_KEY]
    assert isinstance(request, mcp_types.CreateMessageRequest)
    assert request.params.temperature == 0.2
    assert request.params.max_tokens == 8000
    assert "ontology" in request.params.system_prompt
    assert "acctbal.bankid" in request.params.messages[0].content.text


def _answer(text: str):
    return types.SimpleNamespace(
        content=mcp_types.TextContent(type="text", text=text), model="client-model"
    )


def test_round_two_reads_the_answer_under_the_same_key():
    ctx = _ctx("2026-07-28", True, {handler._RENAMES_KEY: _answer(MODEL_ANSWER)})

    answer = handler._client_model_answer(ctx)
    suggestions = handler._suggestions_from_answer(answer)

    assert suggestions["classes"][0]["suggested_name"] == "AccountBalance"


def test_the_first_round_has_no_answer_yet():
    assert handler._client_model_answer(_ctx("2026-07-28", True, None)) is None
    assert handler._client_model_answer(types.SimpleNamespace()) is None


def test_unusable_model_output_falls_back_to_review():
    assert handler._suggestions_from_answer(_answer("sorry, no JSON here")) is None


def test_an_answer_split_over_several_blocks_is_joined():
    half = len(MODEL_ANSWER) // 2
    answer = types.SimpleNamespace(
        content=[
            mcp_types.TextContent(type="text", text=MODEL_ANSWER[:half]),
            mcp_types.TextContent(type="text", text=MODEL_ANSWER[half:]),
        ],
        model="client-model",
    )

    assert handler._suggestions_from_answer(answer)["classes"]


# --- the real tool, end to end ---


@pytest.fixture
def cryptic_database(monkeypatch, tmp_path):
    """A DuckDB file whose names need renaming, behind a private server state."""
    database = tmp_path / "bank.duckdb"
    connection = duckdb.connect(str(database))
    connection.execute("CREATE TABLE banks (bankid INTEGER PRIMARY KEY, bnknm VARCHAR)")
    connection.execute(
        "CREATE TABLE acctbal (acctid INTEGER PRIMARY KEY, bankid INTEGER "
        "REFERENCES banks(bankid), balamt DECIMAL(18,2))"
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
    yield state
    state.cleanup()


def _use_mode(monkeypatch, mode: str) -> None:
    config = handler.config_manager.get_server_config()
    monkeypatch.setattr(config, "semantic_naming_mode", mode)


async def _prepare_ontology(client: Client) -> str:
    connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
    handle = connected.data.split("Connection handle: ")[1].split()[0]
    arguments = {"connection": handle}
    await client.call_tool("discover_schema", {"schema_name": "main", **arguments})
    await client.call_tool(
        "generate_ontology",
        {"schema_name": "main", "auto_persist": False, **arguments},
    )
    return handle


async def test_a_modern_client_with_a_model_gets_prefilled_suggestions(
    cryptic_database, monkeypatch
):
    _use_mode(monkeypatch, "auto")
    prompts: list[str] = []

    async def client_model(messages, params, context):
        prompts.append(messages[0].content.text)
        return MODEL_ANSWER

    async with Client(mcp, sampling_handler=client_model) as client:
        handle = await _prepare_ontology(client)
        result = await client.call_tool(
            "suggest_semantic_names", {"connection": handle}
        )

    assert len(prompts) == 1  # asked once, answered on the second round
    assert "acctbal" in prompts[0]
    assert result.data["suggestions_source"] == "mcp_sampling"
    assert result.data["suggestions"]["classes"][0]["suggested_name"] == (
        "AccountBalance"
    )
    assert result.data["next_tool"] == "apply_semantic_names"


async def test_a_modern_client_without_a_model_gets_the_review_payload(
    cryptic_database, monkeypatch
):
    _use_mode(monkeypatch, "auto")

    async with Client(mcp) as client:
        handle = await _prepare_ontology(client)
        result = await client.call_tool(
            "suggest_semantic_names", {"connection": handle}
        )

    assert "suggestions" not in result.data
    assert result.data["cryptic_classes"]
    assert result.data["next_tool"] == "apply_semantic_names"


async def test_a_handshake_era_client_gets_the_review_payload_even_with_a_model(
    cryptic_database, monkeypatch
):
    """OrionBelt Chat today. It must get a result, not the protocol error that
    returning a multi round-trip request to a 2025-11-25 client would be."""
    _use_mode(monkeypatch, "auto")

    async def client_model(messages, params, context):
        raise AssertionError("a handshake-era client must not be asked")

    async with Client(mcp, mode="legacy", sampling_handler=client_model) as client:
        await client.call_tool("connect_database", {"db_type": "duckdb"})
        await client.call_tool("discover_schema", {"schema_name": "main"})
        await client.call_tool(
            "generate_ontology", {"schema_name": "main", "auto_persist": False}
        )
        result = await client.call_tool("suggest_semantic_names", {})

    assert "suggestions" not in result.data
    assert result.data["cryptic_classes"]


async def test_review_mode_never_asks_even_a_capable_client(
    cryptic_database, monkeypatch
):
    _use_mode(monkeypatch, "review")

    async def client_model(messages, params, context):
        raise AssertionError("SEMANTIC_NAMING_MODE=review must not ask")

    async with Client(mcp, sampling_handler=client_model) as client:
        handle = await _prepare_ontology(client)
        result = await client.call_tool(
            "suggest_semantic_names", {"connection": handle}
        )

    assert "suggestions" not in result.data


# --- configuration ---


@pytest.mark.parametrize(
    ("mode", "legacy", "expected"),
    [
        (None, None, "auto"),
        ("review", None, "review"),
        ("INPUT_REQUIRED", None, "input_required"),
        ("nonsense", None, "auto"),
        (None, "false", "review"),
        (None, "true", "auto"),
        ("auto", "false", "auto"),  # the new variable wins over the alias
    ],
)
def test_mode_resolution(monkeypatch, mode, legacy, expected):
    for name, value in (("SEMANTIC_NAMING_MODE", mode), ("ENABLE_SAMPLING", legacy)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert _resolve_semantic_naming_mode() == expected


def test_the_deprecated_flag_warns_only_when_it_changes_behaviour(monkeypatch, caplog):
    monkeypatch.delenv("SEMANTIC_NAMING_MODE", raising=False)
    monkeypatch.setenv("ENABLE_SAMPLING", "false")

    with caplog.at_level(logging.INFO, logger="src.config"):
        _resolve_semantic_naming_mode()
        monkeypatch.setenv("ENABLE_SAMPLING", "true")
        _resolve_semantic_naming_mode()

    levels = [r.levelno for r in caplog.records if "deprecated" in r.getMessage()]
    assert levels == [logging.WARNING, logging.INFO]


def test_server_config_carries_the_mode(monkeypatch):
    monkeypatch.setenv("SEMANTIC_NAMING_MODE", "review")

    assert ConfigManager().get_server_config().semantic_naming_mode == "review"
