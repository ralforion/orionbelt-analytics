"""The strategy seam behind suggest_semantic_names.

MCP deprecated Sampling in its 2026-07-28 revision and FastMCP 4 removes
``ctx.sample`` in every protocol era. The tool therefore picks one of three
paths through a single seam, and must land on the review path cleanly whenever
the better ones are not on offer.
"""

import json
import logging
import types
from unittest.mock import AsyncMock, Mock

import pytest

from src.config import ConfigManager, _resolve_semantic_naming_mode
from src.handlers import ontology_semantic as handler
from src.handlers.ontology_semantic import NamingStrategy, _select_naming_strategy

CRYPTIC = {
    "cryptic_classes": ["acctbal"],
    "cryptic_props_by_table": {"acctbal": ["bankid"]},
    "cryptic_relationships": [],
}
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


def _sampling_ctx(text: str = MODEL_ANSWER) -> Mock:
    ctx = Mock()
    ctx.sample = AsyncMock(return_value=types.SimpleNamespace(text=text, model="m"))
    return ctx


def _use_mode(monkeypatch, mode: str) -> None:
    config = types.SimpleNamespace(semantic_naming_mode=mode)
    monkeypatch.setattr(handler.config_manager, "get_server_config", lambda: config)


# --- strategy selection ---


def test_auto_uses_client_sampling_when_the_context_offers_it():
    assert _select_naming_strategy(_sampling_ctx(), "auto") is (
        NamingStrategy.CLIENT_SAMPLING
    )


def test_auto_lands_on_review_when_the_context_has_no_sample():
    """What FastMCP 4 looks like before the input-required path exists."""
    ctx = types.SimpleNamespace()

    assert _select_naming_strategy(ctx, "auto") is NamingStrategy.REVIEW


def test_review_mode_never_samples_even_if_the_client_could():
    assert _select_naming_strategy(_sampling_ctx(), "review") is NamingStrategy.REVIEW


def test_input_required_is_not_served_by_fastmcp_3_and_says_so(caplog):
    with caplog.at_level(logging.WARNING, logger=handler.logger.name):
        strategy = _select_naming_strategy(_sampling_ctx(), "input_required")

    assert strategy is NamingStrategy.REVIEW
    assert "FastMCP 4" in caplog.text


# --- dispatch ---


async def test_client_sampling_returns_the_normalized_suggestions(monkeypatch):
    _use_mode(monkeypatch, "auto")
    ctx = _sampling_ctx()

    suggestions = await handler._request_rename_suggestions(ctx, **CRYPTIC)

    ctx.sample.assert_awaited_once()
    assert suggestions is not None
    assert suggestions["classes"][0]["suggested_name"] == "AccountBalance"


async def test_a_context_without_sample_falls_back_without_raising(monkeypatch):
    _use_mode(monkeypatch, "auto")

    suggestions = await handler._request_rename_suggestions(
        types.SimpleNamespace(), **CRYPTIC
    )

    assert suggestions is None


async def test_review_mode_does_not_call_the_client_model(monkeypatch):
    _use_mode(monkeypatch, "review")
    ctx = _sampling_ctx()

    suggestions = await handler._request_rename_suggestions(ctx, **CRYPTIC)

    assert suggestions is None
    ctx.sample.assert_not_awaited()


async def test_a_failing_sample_call_falls_back_to_review(monkeypatch):
    _use_mode(monkeypatch, "auto")
    ctx = Mock()
    ctx.sample = AsyncMock(side_effect=RuntimeError("client has no sampling"))

    assert await handler._request_rename_suggestions(ctx, **CRYPTIC) is None


async def test_unusable_model_output_falls_back_to_review(monkeypatch):
    _use_mode(monkeypatch, "auto")

    suggestions = await handler._request_rename_suggestions(
        _sampling_ctx(text="sorry, no JSON here"), **CRYPTIC
    )

    assert suggestions is None


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
