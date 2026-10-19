"""MCP_TRANSPORT: streamable HTTP only; the removed SSE transport is refused."""

import pytest

from src.config import ConfigManager


def test_sse_is_refused_at_startup_with_the_way_out(monkeypatch):
    """Silently serving HTTP instead would move the endpoint off /sse."""
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    manager = ConfigManager()
    assert manager.get_server_config().mcp_transport == "sse"

    with pytest.raises(ValueError) as excinfo:
        manager.validate_config()

    message = str(excinfo.value)
    assert "no longer supported" in message
    assert "MCP_TRANSPORT=http" in message
    assert "/mcp" in message


@pytest.mark.parametrize("value", ["http", "HTTP"])
def test_http_is_accepted(monkeypatch, value):
    monkeypatch.setenv("MCP_TRANSPORT", value)
    ConfigManager().validate_config()


def test_unknown_transport_still_falls_back_to_http(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
    manager = ConfigManager()
    assert manager.get_server_config().mcp_transport == "http"
    manager.validate_config()
