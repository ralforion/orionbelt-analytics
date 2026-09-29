"""Keeping a tool's results with the database they were computed from.

Discovery, ontology generation, semantic naming and loading an ontology all
await work -- reflection in a worker, an rdflib parse in a thread, a file
read -- and then publish what they found: into the session, into the runtime's
shared cache, into the connection's workspace on disk. A ``connect_database``
landing in between binds the session to another runtime. Reading the
connection *after* the await then writes one database's results into
another's cache and another's directory, for every session sharing it.

The writer lock does not prevent this: it belongs to the runtime being
written, and a reconnect binds a different one. So each of these tools pins
its connection before the first await and checks it again before publishing.
Results for a connection the session has since left are discarded, and the
caller is told to ask again -- never handed another database's answer.
"""

from typing import TYPE_CHECKING, Any, NamedTuple, cast

if TYPE_CHECKING:
    from ..handler_context import HandlerContext


class PinnedConnection(NamedTuple):
    """The connection a piece of work belongs to, fixed when it starts."""

    connection_id: str | None
    runtime: Any


def pin_connection(session: Any) -> PinnedConnection:
    """Record which connection this work is for, before anything is awaited.

    Args:
        session: The session the tool runs in.

    Returns:
        Its connection id and the runtime object, to check against later.
    """
    # getattr throughout: tools are also handed stand-in sessions that carry
    # neither attribute, and for those there is no connection to change.
    return PinnedConnection(
        getattr(session, "connection_id", None), getattr(session, "runtime", None)
    )


def still_connected(session: Any, pinned: PinnedConnection) -> bool:
    """Whether the session is still on the connection the work was done for.

    The runtime object is compared as well as the id, because reconnecting to
    the *same* database replaces the runtime too, and the old one's lock no
    longer guards what the new one holds.

    Args:
        session: The session the tool runs in.
        pinned: What :func:`pin_connection` returned.

    Returns:
        True when the results still describe the database the caller is on.
    """
    return (
        getattr(session, "connection_id", None) == pinned.connection_id
        and getattr(session, "runtime", None) is pinned.runtime
    )


def connection_changed_response(
    services: "HandlerContext", what: str, retry: str
) -> dict[str, Any]:
    """The answer when the connection changed while the work was running.

    Args:
        services: Request-scoped services.
        what: What was being done, for the message ("schema 'sales' was being
            analyzed").
        retry: The call to make again ("discover_schema()").

    Returns:
        An error response telling the caller nothing was written and why.
    """
    return cast(
        dict[str, Any],
        services.create_error_response(
            f"The connection changed while {what}, so the results were "
            f"discarded rather than written into the database now connected. "
            f"Call {retry} again.",
            "connection_changed",
        ),
    )
