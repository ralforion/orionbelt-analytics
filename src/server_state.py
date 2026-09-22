"""Server session state and per-request helpers.

Holds the connection/session lifecycle that used to live inline in
``src/main.py``: the :class:`ServerState` registry (per-session isolation +
idle eviction) and the context helpers the tool layer hands to handlers
(session accessors, error responses, Oxigraph store init).

Kept free of any MCP tool/resource registration so ``main.py`` can stay a thin
registration layer that imports from here.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager, contextmanager, nullcontext
from contextvars import ContextVar, Token
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from fastmcp import Context
from pydantic import BaseModel

from .database_manager import DatabaseManager
from .exceptions import SessionRequiredError, UnknownConnectionError
from .obqc_validator import OBQCValidator
from .ontology_generator import OntologyGenerator
from .oxigraph_store import OXIGRAPH_AVAILABLE, OxigraphStoreManager
from .paths import (
    adopt_legacy_connection_dirs,
    ensure_output_dir,
    get_connection_dir,
    get_oxigraph_store_dir,
)
from .session import ConnectionRuntime, SessionData
from .utils import utc_now
from .workspace import workspace_identity

logger = logging.getLogger(__name__)


# A connection handle looks like ``ob_k2m9qa``. The alphabet leaves out the
# characters a model or a person confuses when copying one (l/1, o/0).
_HANDLE_PREFIX = "ob_"
_HANDLE_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
_HANDLE_LENGTH = 6
# Key prefix of a session opened by a client without a transport session.
_HANDLE_SESSION_PREFIX = "handle:"

# The handle the current tool call runs under, set by the registration layer
# for the length of the call. A context variable, because handlers resolve
# their session from ``ctx`` alone and every request runs in its own context.
_requested_handle: ContextVar[str | None] = ContextVar(
    "orionbelt_connection_handle", default=None
)


def normalize_handle(raw: object) -> str | None:
    """Trim and lowercase a handle argument; blank or missing means none."""
    if not isinstance(raw, str):
        return None
    handle = raw.strip().lower()
    return handle or None


# First MCP revision without protocol-level sessions. Revisions are ISO dates,
# so they compare as strings.
_SESSIONLESS_SINCE = "2026-07-28"


def _protocol_version(ctx: Context) -> str | None:
    """The MCP protocol revision this request was made under, if known."""
    try:
        request_context = getattr(ctx, "request_context", None)
    except Exception as e:  # outside a request, some FastMCP versions raise
        logger.debug(f"No request context to read the protocol version from: {e}")
        return None
    version = getattr(request_context, "protocol_version", None)
    if not isinstance(version, str):
        session = getattr(request_context, "session", None)
        version = getattr(session, "protocol_version", None)
    return version if isinstance(version, str) else None


def _transport_session_id(ctx: Context) -> str | None:
    """The MCP session ID of this request, or None if it has no real session.

    From revision 2026-07-28 on there is no transport session, but FastMCP 4
    still reports a ``ctx.session_id`` there: a fresh UUID on every request
    (verified against 4.0.5; its upgrade guide says ``None``). Trusting it would
    give each call a new, empty session and never reach the connection handle
    or the actionable error. The protocol revision is the dependable signal, so
    in the sessionless era the ID is disregarded.
    """
    version = _protocol_version(ctx)
    if version is not None and version >= _SESSIONLESS_SINCE:
        return None
    session_id = getattr(ctx, "session_id", None)
    return str(session_id) if session_id else None


def get_session_id(ctx: Context) -> str:
    """Get the identifier that keys this request's server-side state.

    Fails rather than guessing. All per-client state hangs off this value, so a
    shared fallback would hand one client another client's database manager,
    ontology and GraphRAG state. FastMCP 3 always supplies a session ID inside a
    request; a request without one is what the sessionless 2026-07-28 protocol
    era looks like, and it must not be folded into a common bucket. The
    object-identity fallback is gone for the same reason: a memory address is
    reused once its session is collected, so it could resurrect a stranger's
    state.

    Args:
        ctx: FastMCP request context.

    Returns:
        The MCP session ID.

    Raises:
        SessionRequiredError: If the request carries no session ID.
    """
    session_id = _transport_session_id(ctx)
    if session_id is not None:
        return session_id
    raise SessionRequiredError(
        "This request carries no MCP session, so the server cannot tell whose "
        "state it belongs to. Pass the connection handle from connect_database "
        "as the `connection` argument."
    )


# Exact key names, and substrings, whose value is a credential rather than a
# part of the database's identity. Kept out of the fingerprint so a rotated
# password does not orphan a workspace, and so no secret is hashed into a
# directory name. "pat" is matched exactly on purpose: as a substring it also
# occurs in "database_path", which *is* DuckDB's identity.
_SECRET_KEY_NAMES = frozenset({"pat", "password", "token", "secret", "credentials"})
_SECRET_KEY_MARKERS = ("password", "secret", "token", "credential", "private_key")


def _identifies_the_database(key: str) -> bool:
    """Whether a connection_info field belongs in the fingerprint."""
    lowered = key.lower()
    if lowered in _SECRET_KEY_NAMES:
        return False
    return not any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def _get_connection_fingerprint(db_manager: DatabaseManager) -> str:
    """Stable identity of the database a manager is connected to.

    Every non-secret field the driver reports is included, because what
    identifies a database differs per driver: a file path for DuckDB, a project
    and dataset for BigQuery, a URI for Dremio with a token, host/port/database
    for the server-based ones. The previous version read ``database_type``,
    ``host``, ``port``, ``database`` and ``schema``; no driver writes
    ``database_type`` (they write ``type``), and the rest are absent from
    exactly the drivers that need something else. Two DuckDB files, two
    BigQuery projects or two Dremio endpoints therefore hashed to the same
    value -- which shared a workspace, and, since sessions share a connection
    runtime, one database's open manager.

    Args:
        db_manager: A connected manager.

    Returns:
        16 hex characters, or ``"no_connection"``.
    """
    conn_info = db_manager.connection_info
    if not conn_info:
        return "no_connection"

    identity = {
        key: value for key, value in conn_info.items() if _identifies_the_database(key)
    }
    # sort_keys, so a driver reordering its dict does not rename a workspace;
    # default=str, so a value the driver stores as an object still hashes.
    fingerprint_data = json.dumps(identity, sort_keys=True, default=str)
    return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]


# The only fields the old fingerprint read that carry any information. A
# workspace may be followed back to its database only when all of them are
# filled in: the old id then encodes where the database lives and what it is
# called, and the type recorded in the workspace settles the rest. With any of
# them missing, the old id was the same for every database of that driver --
# every DuckDB file, every BigQuery dataset, every Dremio endpoint, every
# Snowflake account -- and so is the name the driver records, which for Dremio
# is the constant "DREMIO". Nothing then tells one from another, and moving a
# workspace on a guess hands a stranger's ontologies to whoever connects first.
_LEGACY_IDENTIFYING_KEYS = ("host", "port", "database")


def _legacy_fingerprint_is_specific(conn_info: dict[str, Any]) -> bool:
    """Whether the old id pinned this database down, or every one of its kind."""
    return all(conn_info.get(key) for key in _LEGACY_IDENTIFYING_KEYS)


def _legacy_connection_fingerprint(db_manager: DatabaseManager) -> str:
    """What :func:`_get_connection_fingerprint` returned before it was fixed.

    Only to find directories a previous release left behind; see
    :func:`~src.paths.adopt_legacy_connection_dirs`.
    """
    conn_info = db_manager.connection_info
    if not conn_info:
        return "no_connection"

    fingerprint_data = (
        f"{conn_info.get('database_type', '')}://"
        f"{conn_info.get('host', '')}:{conn_info.get('port', '')}/"
        f"{conn_info.get('database', '')}"
        f"@{conn_info.get('schema', '')}"
    )
    return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]


def adopt_legacy_workspace(
    db_manager: DatabaseManager,
    connection_id: str,
    db_type: str,
    db_name: str,
) -> list[str]:
    """Take over the workspace a previous release left under the old id.

    Only when that workspace can be *shown* to belong to this database. The old
    fingerprint is exactly the thing that could not tell databases apart, so
    following it blindly would hand one database's ontologies, caches and
    triples to another -- and leave the owner unable to find them. Two tests,
    both of which must hold: the old id must have encoded this database's own
    coordinates rather than being the one every database of its driver got, and
    the workspace must record which database it belongs to, and record this
    one. Anything less is left where it is, and said so in the log.

    Args:
        db_manager: The manager that just connected.
        connection_id: Fingerprint in use now.
        db_type: Database type as ``connect_database`` reports it.
        db_name: Database name as ``connect_database`` reports it.

    Returns:
        Names of the directories that were adopted, for the log.
    """
    legacy_id = _legacy_connection_fingerprint(db_manager)
    if not legacy_id or legacy_id == connection_id:
        return []

    conn_info = db_manager.connection_info or {}
    if not _legacy_fingerprint_is_specific(conn_info):
        logger.info(
            "The previous connection id was the same for every "
            f"{conn_info.get('type', 'database')} of this kind, so a workspace "
            "under it cannot be shown to belong to this one. Leaving it where "
            "it is; move it by hand if it is yours."
        )
        return []

    identity = workspace_identity(legacy_id)
    if identity is None:
        logger.debug(
            f"Workspace {legacy_id} records no connection; leaving it where it is"
        )
        return []
    if identity != (db_type, db_name):
        logger.info(
            f"Workspace {legacy_id} belongs to {identity[0]}:{identity[1]}, not to "
            f"{db_type}:{db_name}; leaving it where it is. The previous "
            "connection id could not tell these databases apart."
        )
        return []

    return adopt_legacy_connection_dirs(legacy_id, connection_id)


def _calculate_schema_hash(tables_info: list[Any]) -> str:
    """Calculate deterministic hash of schema structure."""
    schema_structure: dict[str, list[dict[str, Any]]] = {"tables": []}

    sorted_tables = sorted(tables_info, key=lambda t: t.name)
    for table in sorted_tables:
        table_data = {
            "name": table.name,
            "schema": table.schema,
            "columns": [],
            "primary_keys": sorted(table.primary_keys) if table.primary_keys else [],
            "foreign_keys": [],
        }

        sorted_columns = sorted(table.columns, key=lambda c: c.name)
        for col in sorted_columns:
            table_data["columns"].append(
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "nullable": col.is_nullable,
                }
            )

        if table.foreign_keys:
            sorted_fks = sorted(table.foreign_keys, key=lambda f: f["column"])
            for fk in sorted_fks:
                table_data["foreign_keys"].append(
                    {
                        "column": fk["column"],
                        "referenced_table": fk["referenced_table"],
                        "referenced_column": fk["referenced_column"],
                    }
                )

        schema_structure["tables"].append(table_data)

    json_str = json.dumps(schema_structure, sort_keys=True)
    return hashlib.sha256(json_str.encode()).hexdigest()


def _clear_session_state(
    session: SessionData, reason: str = "connection change"
) -> None:
    """Clear all session state caches and indexes."""
    logger.info(f"Clearing session state ({reason})")

    # Leave the shared runtime before clearing anything: the caches below are
    # shared objects while bound, and clearing them in place would empty them
    # for every other session on the old connection.
    _server_state.unbind_session(session)

    session.clear_schema_cache()

    # Clear all per-schema state (ontology for every schema)
    session.clear_all_schema_states()

    # Clear connection-scoped state
    session.graphrag_manager = None
    session.graphrag_initialized = False
    if session.oxigraph_store is not None:
        try:
            _server_state.release_oxigraph_store(session.oxigraph_store)
        except Exception as e:
            logger.warning(f"Error releasing Oxigraph store ({reason}): {e}")
    session.oxigraph_store = None
    session.oxigraph_initialized = False

    logger.info("Session state cleared")


async def aclear_session_state(
    session: SessionData, reason: str = "connection change"
) -> None:
    """Clear session state, waiting for its background init tasks to stop first.

    Preferred over :func:`_clear_session_state` wherever a loop is running, for
    the reason :meth:`ServerState.aclose_session` exists: a cancelled task
    keeps running until its next suspension point, and clearing releases the
    Oxigraph store. Releasing it while a cancelled task is still unwinding is
    how that task ends up holding a store nobody can reopen.

    Args:
        session: Session whose state to clear, typically because it is
            connecting to a different database.
        reason: For the log.
    """
    pending = _server_state.cancel_init_tasks_of(session)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _clear_session_state(session, reason)


class _StoreHandle:
    """A shared Oxigraph store plus the number of sessions holding it."""

    __slots__ = ("manager", "refcount")

    def __init__(self, manager: OxigraphStoreManager) -> None:
        self.manager = manager
        self.refcount = 0


class ServerState:
    """Manages server state with per-session isolation and idle eviction.

    Oxigraph stores are shared per store directory rather than opened per
    session. The directory is connection-scoped, and RocksDB allows exactly one
    open handle per directory -- even inside one process. Without sharing, a
    client that reconnects (a new MCP session on the same database) cannot
    open the store while the previous session still holds it, and every
    SPARQL-backed tool fails until that session is evicted.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionData] = {}
        self._eviction_task: asyncio.Task[None] | None = None
        self._stores: dict[Path, _StoreHandle] = {}
        self._removing_stores: dict[Path, int] = {}
        self._runtimes: dict[str, ConnectionRuntime] = {}
        self._handles: dict[str, str] = {}  # connection handle -> session key

    # --- Connection handles ---

    def _mint_handle(self) -> str:
        while True:
            handle = _HANDLE_PREFIX + "".join(
                secrets.choice(_HANDLE_ALPHABET) for _ in range(_HANDLE_LENGTH)
            )
            if handle not in self._handles:
                return handle

    def _register(self, session_key: str) -> SessionData:
        session = SessionData()
        session.handle = self._mint_handle()
        self._handles[session.handle] = session_key
        self._sessions[session_key] = session
        return session

    def open_handle_session(self) -> SessionData:
        """Start a session for a client that has no transport session.

        Its handle is the only name it has; the client passes it back as the
        ``connection`` tool argument.
        """
        handle = self._mint_handle()
        session = SessionData()
        session.handle = handle
        key = f"{_HANDLE_SESSION_PREFIX}{handle}"
        self._handles[handle] = key
        self._sessions[key] = session
        self._ensure_eviction_task()
        logger.debug(f"Opened handle session: {handle}")
        return session

    def session_for_handle(self, handle: str) -> SessionData:
        """The live session a handle names.

        Raises:
            UnknownConnectionError: If no live session has this handle. Never
                falls back to another session: a mistyped handle must not land
                in someone else's state.
        """
        key = self._handles.get(handle)
        if key is None or key not in self._sessions:
            raise UnknownConnectionError(
                f"Unknown or expired connection handle '{handle}'. Call "
                "connect_database to get a new one, then pass it as the "
                "`connection` argument."
            )
        return self.get_session(key)

    def peek_session(self, session_key: str) -> SessionData | None:
        """An existing session, without creating one or counting as activity."""
        return self._sessions.get(session_key)

    def peek_handle(self, handle: str) -> SessionData | None:
        """The session a handle names, or None; never raises."""
        key = self._handles.get(handle)
        return self._sessions.get(key) if key is not None else None

    def sole_session(self) -> SessionData | None:
        """The only live session a caller without a transport session can be.

        Only sessions that were themselves opened without a transport session
        are candidates. A session that belongs to a transport session has a
        client that identifies itself on every request, so a caller who does
        not cannot be that client, however alone that session is.

        Returns:
            The session, if exactly one candidate is live.
        """
        candidates = [
            session
            for key, session in self._sessions.items()
            if key.startswith(_HANDLE_SESSION_PREFIX)
        ]
        if len(candidates) != 1:
            return None
        session = candidates[0]
        session.touch()
        return session

    # --- Shared connection runtimes ---

    def bind_session(
        self, session: SessionData, connection_id: str, db_manager: Any
    ) -> ConnectionRuntime:
        """Bind ``session`` to the shared runtime of ``connection_id``.

        ``db_manager`` is the manager the session just connected with. The
        first session on a connection donates it to the runtime. A later
        session finds a manager already there and its own becomes redundant:
        it is disconnected, unless the shared one has lost its connection, in
        which case the fresh one replaces it.

        Args:
            session: The session that connected.
            connection_id: Fingerprint of the database it connected to.
            db_manager: The connected manager it used.

        Returns:
            The runtime the session now shares.
        """
        if (
            session.runtime is not None
            and session.runtime.connection_id != connection_id
        ):
            self.unbind_session(session)

        runtime = session.runtime
        if runtime is None:
            runtime = self._runtimes.get(connection_id)
            if runtime is None:
                runtime = ConnectionRuntime(connection_id)
                self._runtimes[connection_id] = runtime
                logger.info(f"Created connection runtime {connection_id[:8]}...")
            runtime.holders += 1
            session.bind_runtime(runtime)

        current = runtime.db_manager
        if current is None or current is db_manager:
            runtime.db_manager = db_manager
        elif self._manager_is_healthy(current):
            self._disconnect_manager(db_manager, "redundant")
        else:
            runtime.db_manager = db_manager
            self._disconnect_manager(current, "stale")
        return runtime

    def unbind_session(self, session: SessionData, detach: bool = True) -> None:
        """Detach ``session`` from its runtime; close it after the last holder.

        Args:
            session: Session to detach. A session with no runtime is a no-op.
            detach: Passed to :meth:`SessionData.unbind_runtime`. False when
                the session itself is being torn down.
        """
        runtime = session.runtime
        # isinstance, not a None check: a test double's attribute is a Mock.
        if not isinstance(runtime, ConnectionRuntime):
            return
        session.unbind_runtime(detach=detach)
        runtime.holders -= 1
        if runtime.holders > 0:
            return
        self._runtimes.pop(runtime.connection_id, None)
        # Nobody is left to read what these would produce.
        for task in list(runtime.graphrag.init_tasks):
            if not task.done():
                task.cancel()
        if runtime.db_manager is not None:
            self._disconnect_manager(runtime.db_manager, "last holder left")
            runtime.db_manager = None
        logger.info(f"Closed connection runtime {runtime.connection_id[:8]}...")

    def writer_lock(self, session: Any) -> AbstractAsyncContextManager[Any]:
        """The lock a tool must hold while it rewrites shared connection state.

        Args:
            session: The calling session.

        Returns:
            The runtime's lock, or a no-op context for a session that shares
            nothing (unbound, or a test double).
        """
        runtime = getattr(session, "runtime", None)
        if isinstance(runtime, ConnectionRuntime):
            return runtime.lock
        return nullcontext()

    def get_runtime(self, connection_id: str) -> ConnectionRuntime | None:
        """The live runtime for ``connection_id``, if any session holds one."""
        return self._runtimes.get(connection_id)

    @staticmethod
    def _manager_is_healthy(db_manager: Any) -> bool:
        try:
            return bool(db_manager.is_connected())
        except Exception as e:
            logger.debug(f"Shared database manager health check failed: {e}")
            return False

    @staticmethod
    def _disconnect_manager(db_manager: Any, why: str) -> None:
        try:
            db_manager.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting {why} database manager: {e}")

    # --- Shared Oxigraph stores ---

    def acquire_oxigraph_store(self, store_path: Path) -> OxigraphStoreManager:
        """Open the store at ``store_path``, or share the handle already open.

        Each call must be balanced by :meth:`release_oxigraph_store`.

        Args:
            store_path: Store directory, from :func:`get_oxigraph_store_dir`.

        Returns:
            The store manager for that directory, shared across sessions.
        """
        if store_path in self._removing_stores:
            raise RuntimeError(
                f"Oxigraph store at {store_path} is being removed by "
                "cleanup_workspace; retry once it has finished"
            )
        handle = self._stores.get(store_path)
        if handle is None:
            handle = _StoreHandle(OxigraphStoreManager(store_path=store_path))
            self._stores[store_path] = handle
        handle.refcount += 1
        return handle.manager

    def release_oxigraph_store(self, manager: Any) -> None:
        """Drop one session's reference; close the store when the last one goes.

        A manager the registry does not know (a test double, or a store that
        :meth:`discard_oxigraph_store` already dropped) is closed outright, as
        nothing else can be holding it.

        Args:
            manager: The store manager returned by :meth:`acquire_oxigraph_store`.
        """
        for store_path, handle in self._stores.items():
            if handle.manager is manager:
                handle.refcount -= 1
                if handle.refcount <= 0:
                    del self._stores[store_path]
                    manager.close()
                    logger.debug(f"Closed Oxigraph store at: {store_path}")
                return
        manager.close()

    def discard_oxigraph_store(self, store_path: Path) -> None:
        """Close the shared store at ``store_path`` and detach it everywhere.

        For callers about to delete the store directory: every session sharing
        the handle is reset so its next access reopens a fresh store instead of
        using a closed one.

        Args:
            store_path: Store directory to close.
        """
        handle = self._stores.pop(store_path, None)
        if handle is None:
            return
        for session in self._sessions.values():
            if session.rdf_store.oxigraph_store is handle.manager:
                session.rdf_store.oxigraph_store = None
                session.rdf_store.oxigraph_initialized = False
        handle.manager.close()
        logger.info(f"Discarded shared Oxigraph store at: {store_path}")

    @contextmanager
    def removing_oxigraph_store(self, store_path: Path) -> Iterator[None]:
        """Discard the store at ``store_path`` and keep it closed for the block.

        For deleting the directory. Discarding alone is not enough: the
        deletion awaits in a thread, and another session on the same
        connection can reopen the directory in between, so the files would be
        removed under a live, registered handle and its later writes lost.
        While the block is open, :meth:`acquire_oxigraph_store` refuses the
        path. Blocks nest: overlapping removals of the same directory keep it
        refused until the last one has exited.

        Args:
            store_path: Store directory about to be deleted.
        """
        self.discard_oxigraph_store(store_path)
        self._removing_stores[store_path] = self._removing_stores.get(store_path, 0) + 1
        try:
            yield
        finally:
            remaining = self._removing_stores[store_path] - 1
            if remaining:
                self._removing_stores[store_path] = remaining
            else:
                del self._removing_stores[store_path]

    def oxigraph_store_refcount(self, store_path: Path) -> int:
        """Number of sessions currently sharing the store at ``store_path``."""
        handle = self._stores.get(store_path)
        return handle.refcount if handle else 0

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)

    def get_session(self, session_id: str) -> SessionData:
        """Get or create session data for a given session ID."""
        if session_id not in self._sessions:
            self._register(session_id)
            logger.debug(f"Created new session: {session_id}")
        session = self._sessions[session_id]
        session.touch()
        self._ensure_eviction_task()
        return session

    def get_ontology_generator(
        self, base_uri: str = "http://example.com/ontology/"
    ) -> OntologyGenerator:
        """Create a new ontology generator instance."""
        return OntologyGenerator(base_uri=base_uri)

    def _cancel_init_tasks(self, session_id: str) -> list["asyncio.Task[Any]"]:
        """Request cancellation of a session's background init tasks.

        Cancellation is only *scheduled* here -- a cancelled task keeps running
        until it next reaches a suspension point. Callers that can await should
        use :meth:`aclose_session`, which waits before tearing down the
        resources those tasks are still touching.

        Args:
            session_id: Session whose tasks should be cancelled.

        Returns:
            The tasks that were still running, for the caller to await.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return []
        return self.cancel_init_tasks_of(session)

    def cancel_init_tasks_of(self, session: SessionData) -> list["asyncio.Task[Any]"]:
        """Request cancellation of the init tasks only this session still needs.

        The tasks belong to the shared runtime. While other sessions hold it
        they are still waiting for the initialisation to finish, so nothing is
        cancelled; the last holder leaving is what ends them.

        Args:
            session: The session that is leaving its connection.

        Returns:
            The tasks that were still running, for the caller to await.
        """
        runtime = session.runtime
        if isinstance(runtime, ConnectionRuntime) and runtime.holders > 1:
            return []

        pending = [t for t in session.graphrag.init_tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            logger.debug(f"Cancelled {len(pending)} pending GraphRAG init task(s)")
        return pending

    def _release_session(self, session_id: str) -> None:
        """Close a session's resources and drop it. Assumes tasks are done."""
        session = self._sessions.get(session_id)
        if session is None:
            return

        if session.runtime is not None:
            # Shared manager: the runtime disconnects it with its last holder.
            # No detach: the session is going away, and work it started should
            # still land in the state the remaining sessions share.
            self.unbind_session(session, detach=False)
        elif session.db_manager:
            try:
                session.db_manager.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting db for session {session_id}: {e}")
        if session.rdf_store.oxigraph_store:
            try:
                self.release_oxigraph_store(session.rdf_store.oxigraph_store)
            except Exception as e:
                logger.warning(f"Error closing Oxigraph for session {session_id}: {e}")

        handle = getattr(session, "handle", None)
        if isinstance(handle, str):
            self._handles.pop(handle, None)
        del self._sessions[session_id]
        logger.debug(f"Cleaned up session: {session_id}")

    async def aclose_session(self, session_id: str) -> None:
        """Clean up a session, waiting for its background tasks to stop first.

        Preferred over :meth:`cleanup_session` wherever a loop is running. A
        background init task can be mid ``save_state`` or mid metadata write;
        closing the Oxigraph store and dropping the session out from under it
        is how a cancelled-but-not-yet-stopped task ends up writing to a closed
        store.

        Args:
            session_id: Session to close.
        """
        pending = self._cancel_init_tasks(session_id)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._release_session(session_id)

    def cleanup_session(self, session_id: str) -> None:
        """Clean up a session from synchronous context.

        Best-effort: cancellation cannot be awaited here, so a background task
        may still be running when the store closes. Used from the atexit hook,
        where the loop is already gone. Prefer :meth:`aclose_session`.

        Args:
            session_id: Session to close.
        """
        self._cancel_init_tasks(session_id)
        self._release_session(session_id)

    def cleanup(self) -> None:
        """Clean up all resources from synchronous context (atexit)."""
        if self._eviction_task and not self._eviction_task.done():
            self._eviction_task.cancel()
            logger.debug("Cancelled session eviction task")
        for session_id in list(self._sessions.keys()):
            self.cleanup_session(session_id)

    # --- Idle eviction ---

    def _ensure_eviction_task(self) -> None:
        """Lazily start the eviction background task if not already running."""
        if self._eviction_task is not None and not self._eviction_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._eviction_task = loop.create_task(self._eviction_loop())
            logger.info("Started session eviction background task")
        except RuntimeError:
            pass  # No event loop (e.g. tests or sync context)

    async def _eviction_loop(self) -> None:
        """Periodically scan for and evict idle sessions."""
        from .config import config_manager

        config = config_manager.get_server_config()
        idle_timeout = config.session_idle_timeout
        scan_interval = config.session_scan_interval

        if idle_timeout <= 0:
            logger.info("Session idle eviction disabled (timeout=0)")
            return

        logger.info(
            f"Session eviction active: timeout={idle_timeout}s, "
            f"scan_interval={scan_interval}s"
        )

        while True:
            try:
                await asyncio.sleep(scan_interval)
                await self._evict_idle_sessions(idle_timeout)
            except asyncio.CancelledError:
                logger.info("Session eviction task cancelled")
                break
            except Exception as e:
                logger.exception(f"Error in session eviction loop: {e}")
                await asyncio.sleep(scan_interval)

    async def _evict_idle_sessions(self, idle_timeout: int) -> None:
        """Scan sessions and evict those idle beyond the timeout.

        Awaits each session's background tasks before releasing its resources,
        so an in-flight GraphRAG init cannot outlive the store it writes to.
        """
        now = utc_now()
        cutoff = now - timedelta(seconds=idle_timeout)

        to_evict = []
        for session_id, session in self._sessions.items():
            if session.last_activity < cutoff:
                idle_secs = (now - session.last_activity).total_seconds()
                to_evict.append((session_id, idle_secs))

        total = len(self._sessions)
        evicting = len(to_evict)
        if total > 0:
            logger.debug(
                f"Session scan: {total} total, {evicting} idle "
                f"(timeout={idle_timeout}s)"
            )

        for session_id, idle_secs in to_evict:
            logger.info(
                f"Evicting idle session {session_id} "
                f"(idle {idle_secs:.0f}s, timeout={idle_timeout}s)"
            )
            await self.aclose_session(session_id)

        if evicting > 0:
            logger.info(
                f"Evicted {evicting} idle session(s). "
                f"Remaining: {len(self._sessions)}"
            )


# Global server state
_server_state = ServerState()


def get_session_data(ctx: Context) -> SessionData:
    """Resolve the session this request belongs to.

    In order: the connection handle the call was made with, the MCP transport
    session, then -- unless ``SESSIONLESS_FALLBACK=none`` -- the only live
    session if there is exactly one. The last rule forgives a model that drops
    its handle on a single-user server; with several sessions it would be a
    guess, so it is an error instead.

    Raises:
        UnknownConnectionError: The handle names no live session.
        SessionRequiredError: Nothing identifies the caller.
    """
    handle = _requested_handle.get()
    if handle is not None:
        return _server_state.session_for_handle(handle)

    session_id = _transport_session_id(ctx)
    if session_id is not None:
        return _server_state.get_session(session_id)

    if _sessionless_fallback_enabled():
        sole = _server_state.sole_session()
        if sole is not None:
            if not sole.fallback_noted:
                sole.fallback_noted = True
                logger.warning(
                    f"A request with neither an MCP session nor a connection "
                    f"handle was placed in the only sessionless session "
                    f"({sole.handle}). Fine on a single-user server; if several "
                    "people share this one, set SESSIONLESS_FALLBACK=none so "
                    "nobody lands in another user's session by omission."
                )
            return sole

    raise SessionRequiredError(
        "This request carries neither an MCP session nor a connection handle, "
        "so the server cannot tell whose state it belongs to. Call "
        "connect_database and pass the connection handle it returns as the "
        "`connection` argument of every following call."
    )


def _sessionless_fallback_enabled() -> bool:
    from .config import config_manager

    return config_manager.get_server_config().sessionless_fallback == "sole_session"


def begin_connection_scope(
    ctx: Context, handle_argument: object, mint: bool
) -> "Token[str | None]":
    """Run the current tool call under the connection handle it was given.

    Args:
        ctx: FastMCP request context.
        handle_argument: The tool's ``connection`` argument, if any.
        mint: True for ``connect_database``: a caller with neither a handle
            nor a transport session gets a new session and handle, because
            it is about to need one.

    Returns:
        Token for :func:`end_connection_scope`.

    Raises:
        UnknownConnectionError: The handle names no live session. Raised here,
            before the tool has done anything.
    """
    handle = normalize_handle(handle_argument)
    if handle is not None:
        _server_state.session_for_handle(handle)
    elif mint and _transport_session_id(ctx) is None:
        handle = _server_state.open_handle_session().handle
    return _requested_handle.set(handle)


def end_connection_scope(token: "Token[str | None]") -> None:
    """Leave the scope opened by :func:`begin_connection_scope`."""
    _requested_handle.reset(token)


def peek_current_session(ctx: Context) -> SessionData | None:
    """The session this request resolved to, without creating or raising."""
    handle = _requested_handle.get()
    if handle is not None:
        return _server_state.peek_handle(handle)
    session_id = _transport_session_id(ctx)
    if session_id is not None:
        return _server_state.peek_session(session_id)
    if _sessionless_fallback_enabled():
        return _server_state.sole_session()
    return None


def get_session_db_manager(ctx: Context) -> DatabaseManager:
    """Get or create a DatabaseManager for the current session."""
    session = get_session_data(ctx)
    if session.db_manager is None:
        session.db_manager = DatabaseManager()
        logger.debug(f"Created new DatabaseManager for session: {session.handle}")
    return cast(DatabaseManager, session.db_manager)


def get_session_obqc_validator(ctx: Context) -> OBQCValidator | None:
    """Get or create OBQC validator for the current session."""
    session = get_session_data(ctx)

    has_generated_ontology = session.ontology_file is not None
    has_loaded_ontology = session.loaded_ontology is not None

    if not has_generated_ontology and not has_loaded_ontology:
        return None

    if session.obqc_validator is None:
        session.obqc_validator = OBQCValidator()

        base_uri = os.getenv("ONTOLOGY_BASE_URI", "http://example.com/ontology/")
        ontology_generator = OntologyGenerator(base_uri)

        if session.ontology_file is not None:
            conn_dir = (
                get_connection_dir(session.connection_id)
                if session.connection_id
                else ensure_output_dir()
            )
            ontology_path = conn_dir / session.ontology_file
            if ontology_path.exists():
                ontology_generator.load_from_file(str(ontology_path))
                logger.debug(
                    f"OBQC loaded ontology from session file: {session.ontology_file}"
                )
        elif session.loaded_ontology is not None:
            ontology_generator.load_from_string(session.loaded_ontology)
            logger.debug(
                f"OBQC loaded ontology from session's loaded ontology: {session.loaded_ontology_path}"
            )

        session.obqc_validator.load_ontology(ontology_generator.graph, base_uri)
        logger.debug(f"Initialized OBQC validator for session: {session.handle}")

    # Registered outside the creation branch, and on every call: views may be
    # discovered after the validator was built, and without them every query
    # against a view is rejected for a table the ontology was never meant to
    # describe. load_views_from_definitions() re-parses only on change.
    # Registered unconditionally, including when the list is empty: an empty
    # set has to be able to *clear* a previously registered one. Gating on
    # truthiness let views survive a reconnect or a rediscovery that found
    # none, leaving OBQC accepting objects from a schema no longer loaded.
    views = session.get_all_cached_views()
    db_type = "postgresql"
    if session.db_manager is not None:
        db_type = session.db_manager.connection_info.get("type", "postgresql")
    session.obqc_validator.load_views_from_definitions(
        {view.name: view.definition for view in views}, dialect=db_type
    )

    return cast(OBQCValidator | None, session.obqc_validator)


def get_session_safe_filename(ctx: Context, prefix: str, suffix: str = "") -> str:
    """Generate a connection-safe filename to prevent cross-database file collisions."""
    session = get_session_data(ctx)
    connection_prefix = (
        session.connection_id[:8]
        if session.connection_id and len(session.connection_id) >= 8
        else "default"
    )
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S%f")
    if suffix:
        return f"{prefix}_{connection_prefix}_{suffix}_{timestamp}"
    return f"{prefix}_{connection_prefix}_{timestamp}"


def load_ontology_from_session(ctx: Context) -> tuple[OntologyGenerator, str]:
    """Load ontology from session state."""
    session = get_session_data(ctx)
    filename = session.ontology_file
    if not filename:
        raise ValueError(
            "No ontology file in session state. Run generate_ontology first."
        )

    conn_dir = (
        get_connection_dir(session.connection_id)
        if session.connection_id
        else ensure_output_dir()
    )
    ontology_path = conn_dir / filename

    if not ontology_path.exists():
        raise ValueError(f"Ontology file not found: {filename}")

    generator = _server_state.get_ontology_generator()
    generator.load_from_file(str(ontology_path))

    return generator, filename


class ErrorResponse(BaseModel):
    """Standardized error response format."""

    error: str
    error_type: str = "unknown"
    details: str | None = None


def create_error_response(
    error_msg: str, error_type: str = "unknown", details: str | None = None
) -> dict[str, Any]:
    """Create a standardized error response.

    DEPRECATED: Use exceptions from src.exceptions instead.
    Example: ConnectionError("message").to_response()

    This function is kept for backward compatibility but new code should
    use the exception hierarchy in src/exceptions.py.
    """
    response = ErrorResponse(error=error_msg, error_type=error_type, details=details)
    return response.model_dump()


def get_oxigraph_store(ctx: Context) -> OxigraphStoreManager | None:
    """Get the connection-scoped Oxigraph store for the session.

    The store is shared with every other session on the same connection (see
    :class:`ServerState`); the session only holds a reference to it.
    """
    session = get_session_data(ctx)

    if not OXIGRAPH_AVAILABLE:
        logger.warning("pyoxigraph not available - SPARQL features disabled")
        return None

    if session.oxigraph_store is None:
        try:
            store_path = get_oxigraph_store_dir(connection_id=session.connection_id)
            session.oxigraph_store = _server_state.acquire_oxigraph_store(store_path)
            session.oxigraph_initialized = True

            if session.connection_id:
                logger.info(
                    f"Initialized connection-scoped Oxigraph store at: {store_path}"
                )
            else:
                logger.info(
                    f"Initialized Oxigraph store at: {store_path} (legacy mode)"
                )

        except Exception as e:
            logger.error(f"Failed to initialize Oxigraph store: {e}")
            return None

    return cast(OxigraphStoreManager | None, session.oxigraph_store)
