"""notify_client is the single seam for server-to-client messages."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from src.utils import notify_client

SRC = Path(__file__).resolve().parent.parent / "src"


async def test_sends_at_info_level_by_default():
    ctx = Mock()
    ctx.info = AsyncMock()

    await notify_client(ctx, "working")

    ctx.info.assert_awaited_once_with("working")


async def test_sends_at_error_level_when_asked():
    ctx = Mock()
    ctx.error = AsyncMock()

    await notify_client(ctx, "broken", level="error")

    ctx.error.assert_awaited_once_with("broken")


async def test_a_failed_send_never_aborts_the_tool():
    """A client that already disconnected must not cost the caller its result."""
    ctx = Mock()
    ctx.info = AsyncMock(side_effect=RuntimeError("stream closed"))

    await notify_client(ctx, "working")  # must not raise


async def test_no_context_means_nothing_to_send():
    await notify_client(None, "background work")  # must not raise


def test_no_handler_talks_to_the_client_directly():
    """Every message goes through notify_client, so the MCP Logging
    deprecation (2026-07-28) is handled in one place."""
    direct = re.compile(r"\bctx\.(info|debug|warning|error)\(")
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in SRC.rglob("*.py")
        if path != SRC / "utils.py"
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if direct.search(line)
    ]
    assert offenders == []
