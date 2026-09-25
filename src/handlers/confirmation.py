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
    STALE = "stale"  # answered, but about a different state than the one now


def _declared_elicitation(ctx: Context) -> mcp_types.ElicitationCapability | None:
    """The elicitation capability this client declared, if it can be read."""
    session = getattr(ctx, "session", None)
    client_params = getattr(session, "client_params", None)
    capabilities = getattr(client_params, "capabilities", None)
    if capabilities is None:
        capabilities = getattr(session, "client_capabilities", None)
    declared = getattr(capabilities, "elicitation", None)
    return declared if isinstance(declared, mcp_types.ElicitationCapability) else None


def _client_can_be_asked(ctx: Context) -> bool:
    """Whether this client can answer the form these questions are asked with.

    Elicitation comes in two kinds since MCP 2026-07-28: a form the client
    renders, and a URL it opens. We ask with a form, and the SDK's capability
    check only tests that *some* elicitation was declared -- so a client
    offering URL elicitation alone would be sent a form it cannot render, and
    the question would never be answered.

    A client that declares neither kind predates the distinction, where
    elicitation meant the form; it is asked.
    """
    declared = _declared_elicitation(ctx)
    if declared is not None:
        if declared.url is not None and declared.form is None:
            logger.info(
                "Client offers URL elicitation only, which cannot render this "
                "form; not asking"
            )
            return False
        return True

    # Capabilities could not be read (an older context, or a test double).
    # Fall back to the presence check; an unanswerable form still degrades to
    # "declined", never to a silent yes.
    try:
        # `is True`, not truthiness: FastMCP answers with a real bool, and
        # anything else -- a test double that says yes to everything -- is not
        # a client that can be asked.
        return (
            ctx.session.check_client_capability(
                mcp_types.ClientCapabilities(
                    elicitation=mcp_types.ElicitationCapability()
                )
            )
            is True
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
    ctx: Context,
    key: str,
    message: str,
    field_title: str,
    scope: str | None = None,
) -> Confirmation | mcp_types.InputRequiredResult:
    """Ask the user to confirm, in whichever way this request allows.

    Args:
        ctx: FastMCP request context.
        key: Names the question within the round trip; use the tool's name.
        message: The question, shown to the user.
        field_title: Label of the checkbox the user ticks to confirm.
        scope: What the answer is about, e.g. the connection the question named.
            On a 2026-07-28 request the two rounds are separate requests, so it
            travels with the question and is compared when the answer comes
            back: an answer about something else is :attr:`Confirmation.STALE`,
            never a yes. Callers that can also change state *within* one
            request must compare around the await themselves; see
            ``confirm_cleanup``.

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
                input_requests={key: _question(message, field_title)},
                request_state=scope,
            )
        answered_about = getattr(ctx, "request_state", None)
        if answered_about != scope:
            logger.warning(
                f"Confirmation for {key!r} came back about {answered_about!r}, "
                f"but this request is about {scope!r}; not treating it as an answer"
            )
            return Confirmation.STALE
        content = getattr(answer, "content", None)
        value = content.get(_FIELD) if isinstance(content, dict) else None
        return _confirmed(getattr(answer, "action", None), value)

    result = await ctx.elicit(message, response_type=bool)
    return _confirmed(getattr(result, "action", None), getattr(result, "data", None))
