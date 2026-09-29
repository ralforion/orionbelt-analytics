"""Async/sync bridge utilities for OrionBelt Analytics.

Provides a single utility for running async code from synchronous contexts,
replacing the 8+ duplicated ThreadPoolExecutor patterns in database_manager.py.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from .constants import CONNECTION_TIMEOUT

logger = logging.getLogger(__name__)


def run_async[T](coro: Coroutine[Any, Any, T], timeout: int = CONNECTION_TIMEOUT) -> T:
    """Run an async coroutine from a synchronous context.

    Handles the common case in MCP servers where tool handlers are sync
    but need to call async code (e.g., Dremio REST client).

    If an event loop is already running (MCP server context), runs the
    coroutine in a separate thread. Otherwise creates a new event loop.

    Args:
        coro: The coroutine to execute
        timeout: Maximum seconds to wait for completion

    Returns:
        The coroutine's return value

    Raises:
        TimeoutError: If execution exceeds timeout
        Exception: Any exception raised by the coroutine
    """
    try:
        asyncio.get_running_loop()
        # Already in async context (MCP server) - run in separate thread
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result(timeout=timeout)
    except RuntimeError:
        # No event loop running - safe to use asyncio.run directly
        return asyncio.run(coro)


async def run_db[T](call: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking database call in a worker, serialized per connection.

    Database work -- a connection handshake, metadata reflection, a query --
    ran on the event loop, so one slow warehouse call froze every other
    session on the server. It runs in a thread here.

    Serialized on the owner's ``query_lock``, because moving the call off the
    loop is also what first allows two of them at once on one connection: every
    driver takes a fresh pooled connection per call, but an in-memory DuckDB
    engine shares a single connection through ``StaticPool``. The lock keeps the
    ordering a blocked loop used to give for free; a dialect with a real pool
    could later be allowed more.

    Args:
        call: A bound method of the database manager (or a test double).
        *args: Positional arguments for it.
        **kwargs: Keyword arguments for it.

    Returns:
        Whatever the call returns.
    """
    work = partial(call, *args, **kwargs)
    lock = getattr(getattr(call, "__self__", None), "query_lock", None)
    if not isinstance(lock, asyncio.Lock):
        # A double, or a manager built before this existed: nothing to protect.
        return await asyncio.to_thread(work)
    async with lock:
        return await asyncio.to_thread(work)
