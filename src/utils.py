"""Utility functions for OrionBelt Analytics."""

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Timestamps
#
# Workspace metadata is persisted as ISO strings and later compared against the
# current time to decide retention. Naive local timestamps make that comparison
# wrong across DST shifts and meaningless if the workspace moves between
# machines in different zones, so everything written from here on is
# timezone-aware UTC.
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Returns:
        The current UTC time, with tzinfo set.
    """
    return datetime.now(UTC)


def parse_timestamp(value: str) -> datetime:
    """Parse a persisted ISO timestamp, treating naive values as UTC.

    Workspaces written before timestamps became timezone-aware hold naive ISO
    strings. Comparing one of those against an aware ``utc_now()`` raises
    ``TypeError``, which would break retention cleanup on startup for every
    existing workspace on disk. Naive inputs are therefore assumed to be UTC
    rather than rejected.

    Args:
        value: ISO 8601 timestamp, with or without an offset.

    Returns:
        A timezone-aware datetime.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


# ---------------------------------------------------------------------------
# Non-blocking file I/O
#
# Handlers run on the event loop, and the artifacts they touch (ontology TTL,
# schema JSON) are routinely megabytes on a large schema. A plain open()/read()
# there stalls the loop for every concurrent session, so these helpers push the
# whole operation -- including JSON encode/decode, which is CPU-bound in its own
# right -- into a worker thread. Exceptions propagate exactly as the blocking
# calls would.
# ---------------------------------------------------------------------------


async def read_text_file(path: Path | str, encoding: str = "utf-8") -> str:
    """Read a text file without blocking the event loop.

    Args:
        path: File to read.
        encoding: Text encoding.

    Returns:
        The file's contents.
    """
    return await asyncio.to_thread(Path(path).read_text, encoding=encoding)


async def write_text_file(
    path: Path | str, content: str, encoding: str = "utf-8"
) -> None:
    """Write a text file without blocking the event loop.

    Args:
        path: File to write.
        content: Text to write.
        encoding: Text encoding.
    """
    await asyncio.to_thread(Path(path).write_text, content, encoding=encoding)


async def read_json_file(path: Path | str, encoding: str = "utf-8") -> Any:
    """Read and parse a JSON file without blocking the event loop.

    Args:
        path: File to read.
        encoding: Text encoding.

    Returns:
        The decoded JSON payload.
    """

    def _read() -> Any:
        return json.loads(Path(path).read_text(encoding=encoding))

    return await asyncio.to_thread(_read)


async def write_json_file(
    path: Path | str, data: Any, encoding: str = "utf-8", indent: int = 2
) -> None:
    """Serialize data to a JSON file without blocking the event loop.

    Args:
        path: File to write.
        data: JSON-serializable payload.
        encoding: Text encoding.
        indent: Indentation passed to :func:`json.dumps`.
    """

    def _write() -> None:
        Path(path).write_text(
            json.dumps(data, indent=indent, ensure_ascii=False), encoding=encoding
        )

    await asyncio.to_thread(_write)


# First MCP revision without protocol-level sessions and with multi round-trip
# results. Revisions are ISO dates, so they compare as strings.
STATELESS_PROTOCOL_REVISION = "2026-07-28"


def protocol_revision(ctx: Any) -> str | None:
    """The MCP protocol revision a request was made under, if it says.

    Read from ``ctx.request_context.protocol_version``, else from the server
    session. ``None`` for anything that does not report one, such as a test
    double.
    """
    try:
        request_context = getattr(ctx, "request_context", None)
    except Exception as e:  # outside a request, some FastMCP versions raise
        logging.getLogger(__name__).debug(f"No request context to inspect: {e}")
        return None
    version = getattr(request_context, "protocol_version", None)
    if not isinstance(version, str):
        session = getattr(request_context, "session", None)
        version = getattr(session, "protocol_version", None)
    return version if isinstance(version, str) else None


def is_stateless_era(ctx: Any) -> bool:
    """True from MCP 2026-07-28 on: no transport session, multi round trips."""
    revision = protocol_revision(ctx)
    return revision is not None and revision >= STATELESS_PROTOCOL_REVISION


async def notify_client(ctx: Any, message: str, level: str = "info") -> None:
    """Send a progress or error notification to the client, never raising.

    The single seam for server-to-client messages. Every handler reports
    through here rather than calling ``ctx.info`` directly, for two reasons:

    - A notification failing (e.g. ``anyio.ClosedResourceError`` because the
      client already closed the session) must not abort the tool call -- the
      real result still has to flow back through the framework's response
      path. Failures are logged at debug level since they are usually benign
      client disconnects.
    - MCP deprecated its Logging feature in the 2026-07-28 revision, where a
      message may only be sent for a request that opted in. When that lands,
      this function is the one place that changes.

    Args:
        ctx: FastMCP request context, or ``None`` outside a request (background
            work), in which case nothing is sent.
        message: Text for the client.
        level: ``"info"`` or ``"error"``; the ``Context`` method to call.
    """
    if ctx is None:
        return
    try:
        await getattr(ctx, level)(message)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "ctx.%s send failed (%s); continuing", level, type(exc).__name__
        )


def is_client_disconnect(exc: BaseException) -> bool:
    """Return True if *exc* indicates the MCP client closed the session.

    Used to short-circuit error-response writes that would themselves fail
    against a closed transport stream and turn a benign disconnect into a
    crashed task group.
    """
    try:
        from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
    except ImportError:
        return False
    return isinstance(exc, (ClosedResourceError, BrokenResourceError, EndOfStream))


def setup_logging(log_level: str = "INFO", structured: bool = False) -> logging.Logger:
    """
    Setup logging configuration for the application.

    Args:
        log_level: The logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        structured: Whether to use structured logging format (JSON)

    Returns:
        Logger instance for the root logger
    """
    # Convert string log level to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Create formatter based on structured flag
    if structured:
        # Structured format for production (could be JSON in the future)
        formatter = logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}'
        )
    else:
        # Simple format for development and startup
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove any existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)

    # Add handler to root logger
    root_logger.addHandler(console_handler)

    return root_logger


def sanitize_for_logging(data: Any) -> Any:
    """
    Sanitize sensitive data for logging by redacting passwords, secrets, and API keys.

    Args:
        data: Data structure (dict, list, or primitive) to sanitize

    Returns:
        Sanitized copy of the data with sensitive fields redacted
    """
    if isinstance(data, dict):
        sanitized = {}
        sensitive_keys = {
            "password",
            "passwd",
            "pwd",
            "secret",
            "api_key",
            "apikey",
            "token",
            "auth",
            "authorization",
            "credentials",
            "private_key",
        }

        for key, value in data.items():
            # Check if key name suggests sensitive data
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                sanitized[key] = "***REDACTED***"
            elif isinstance(value, (dict, list)):
                # Recursively sanitize nested structures
                sanitized[key] = sanitize_for_logging(value)
            else:
                sanitized[key] = value

        return sanitized
    elif isinstance(data, list):
        return [sanitize_for_logging(item) for item in data]
    else:
        # Return primitives unchanged
        return data


def validate_uri(uri: str) -> bool:
    """
    Validate that a string is a valid HTTP/HTTPS URI.

    Args:
        uri: URI string to validate

    Returns:
        True if valid HTTP/HTTPS URI, False otherwise
    """
    if not uri:
        return False

    try:
        parsed = urlparse(uri)
        # Check scheme is http or https and has a netloc (domain)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        # urlparse raises ValueError on malformed input (e.g. a bad IPv6
        # literal). Anything else is a caller bug and should surface.
        return False


def format_bytes(num_bytes: int) -> str:
    """
    Format bytes into human-readable string with appropriate unit.

    Args:
        num_bytes: Number of bytes

    Returns:
        Formatted string (e.g., "1.5 KB", "2.3 MB")
    """
    if num_bytes == 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    size = float(num_bytes)

    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1

    # Format with 1 decimal place for units beyond bytes
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    else:
        return f"{size:.1f} {units[unit_index]}"
