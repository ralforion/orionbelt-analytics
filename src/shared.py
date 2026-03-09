"""Shared instances and utilities for OrionBelt Analytics.

WARNING: The global database manager in this module is DEPRECATED.
Use get_session_db_manager(ctx) from main.py for proper session isolation.
"""

import logging
import warnings
from typing import Optional, Dict, Any

from .database_manager import DatabaseManager

logger = logging.getLogger(__name__)

# DEPRECATED: Global database manager - violates session isolation!
# Use get_session_db_manager(ctx) from main.py instead.
_db_manager: Optional[DatabaseManager] = None


def get_db_manager() -> DatabaseManager:
    """Get or create the global database manager instance.

    DEPRECATED: This function creates a SHARED database manager that violates
    session isolation. Use get_session_db_manager(ctx) from main.py instead.

    This global instance can cause:
    - Cross-session data leakage
    - Connection state bleeding between users
    - Unpredictable behavior in multi-user scenarios

    Returns:
        DatabaseManager: A shared (non-isolated) database manager instance.
    """
    warnings.warn(
        "get_db_manager() is deprecated and violates session isolation. "
        "Use get_session_db_manager(ctx) from main.py instead.",
        DeprecationWarning,
        stacklevel=2
    )
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
        logger.warning(
            "DEPRECATION: Global database manager created. "
            "This violates session isolation - use get_session_db_manager(ctx) instead."
        )
    return _db_manager


def create_error_response(message: str, error_type: str, details: str = None) -> Dict[str, Any]:
    """Create a standardized error response.

    DEPRECATED: Use the typed exception classes in ``src/exceptions.py`` instead.

    Each exception subclass produces the same ``{"success": False, ...}`` dict
    via its ``.to_response()`` method, but adds type safety and avoids free-form
    ``error_type`` strings scattered across the codebase.

    Example migration::

        # Before (deprecated):
        return create_error_response("Not connected", "connection_error")

        # After (preferred):
        from .exceptions import ConnectionError
        return ConnectionError("Not connected").to_response()

    This function is retained only for backward compatibility with
    ``src/tools/`` modules.  It will be removed in a future release.
    """
    warnings.warn(
        "create_error_response() is deprecated. "
        "Use exception classes from src.exceptions instead (e.g. ConnectionError('msg').to_response()).",
        DeprecationWarning,
        stacklevel=2,
    )
    error_response = {
        "success": False,
        "error": message,
        "error_type": error_type
    }
    if details:
        error_response["details"] = details
    return error_response