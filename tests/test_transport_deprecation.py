"""MCP_TRANSPORT=sse still works but announces its deprecation at startup."""

import logging

import pytest

from src.config import ConfigManager


@pytest.mark.parametrize(
    ("transport", "warns"), [("sse", True), ("http", False)], ids=["sse", "http"]
)
def test_sse_transport_logs_a_deprecation_warning(
    monkeypatch, caplog, transport, warns
):
    monkeypatch.setenv("MCP_TRANSPORT", transport)
    manager = ConfigManager()

    with caplog.at_level(logging.WARNING, logger="src.config"):
        manager.validate_config()

    deprecations = [r for r in caplog.records if "deprecated" in r.getMessage()]
    assert bool(deprecations) is warns
    if warns:
        assert "MCP_TRANSPORT=http" in deprecations[0].getMessage()
