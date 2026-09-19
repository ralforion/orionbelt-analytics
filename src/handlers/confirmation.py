"""Asking the user a yes/no question before a tool does something drastic.

How a server asks depends on the protocol era of the request. From MCP
2026-07-28 on a tool *returns* the question (an ``InputRequiredResult``,
SEP-2322) and is called again with the answer; before that it awaits
``ctx.elicit``. Either way the client must advertise the elicitation
capability, and a client that does not is not asked: the tool then behaves as
it did before it learned to ask.
"""

import logging
from enum import StrEnum
from typing import Any

import mcp.types as mcp_types
from fastmcp import Context

from ..utils import is_stateless_era

logger = logging.getLogger(__name__)

_FIELD = "confirm"


class Confirmation(StrEnum):
    """What came of asking."""

    CONFIRMED = "confirmed"
    DECLINED = "declined"  # said no, dismissed the question, or left it unticked
    UNAVAILABLE = "unavailable"  # this client cannot be asked


def _client_can_be_asked(ctx: Context) -> bool:
    try:
        return bool(
            ctx.session.check_client_capability(
                mcp_types.ClientCapabilities(
                    elicitation=mcp_types.ElicitationCapability()
                )
            )
        )
    except Exception as e:
        logger.debug(f"Could not read the client's elicitation capability: {e}")
        return False


def _question(message: str, field_title: str) -> mcp_types.ElicitRequest:
    # A real field rather than an empty form: clients render an empty form as a
    # dialog with nothing in it, and FastMCP refuses to send one.
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            _FIELD: {"type": "boolean", "title": field_title, "default": False}
        },
        "required": [_FIELD],
    }
    return mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestFormParams(
            message=message, requested_schema=schema
        )
    )


def _confirmed(action: object, value: object) -> Confirmation:
    if action == "accept" and value is True:
        return Confirmation.CONFIRMED
    return Confirmation.DECLINED


async def ask_to_confirm(
    ctx: Context, key: str, message: str, field_title: str
) -> Confirmation | mcp_types.InputRequiredResult:
    """Ask the user to confirm, in whichever way this request allows.

    Args:
        ctx: FastMCP request context.
        key: Names the question within the round trip; use the tool's name.
        message: The question, shown to the user.
        field_title: Label of the checkbox the user ticks to confirm.

    Returns:
        A :class:`Confirmation`, or -- on the first round of a 2026-07-28
        request -- the ``InputRequiredResult`` the tool must return as its own
        result. The client answers it and calls the tool again, and this
        function then returns the confirmation.
    """
    if not _client_can_be_asked(ctx):
        return Confirmation.UNAVAILABLE

    if is_stateless_era(ctx):
        responses = getattr(ctx, "input_responses", None)
        answer = responses.get(key) if isinstance(responses, dict) else None
        if answer is None:
            return mcp_types.InputRequiredResult(
                input_requests={key: _question(message, field_title)}
            )
        content = getattr(answer, "content", None)
        value = content.get(_FIELD) if isinstance(content, dict) else None
        return _confirmed(getattr(answer, "action", None), value)

    result = await ctx.elicit(message, response_type=bool)
    return _confirmed(getattr(result, "action", None), getattr(result, "data", None))
