"""Custom exception hierarchy for OrionBelt Analytics.

Provides structured error handling with consistent error types
that replace ad-hoc string-based error_type fields.
"""

from enum import StrEnum


class ErrorType(StrEnum):
    """Enumeration of all error types for consistent error reporting."""

    VALIDATION = "validation_error"
    PARAMETER = "parameter_error"
    CONNECTION = "connection_error"
    DATABASE = "database_error"
    SECURITY = "security_error"
    SYNTAX = "syntax_error"
    FORBIDDEN = "forbidden_operation"
    INTERNAL = "internal_error"
    RDF = "rdf_error"
    STORE = "store_not_initialized"
    DEPENDENCY = "dependency_error"
    OBQC = "obqc_error"
    SESSION = "session_required"
    UNKNOWN_CONNECTION = "unknown_connection"
    BUSY = "connection_busy"


class OrionBeltError(Exception):
    """Base exception for all OrionBelt Analytics errors."""

    error_type: ErrorType = ErrorType.INTERNAL

    def __init__(
        self,
        message: str,
        details: str | None = None,
        suggestions: list[str] | None = None,
    ):
        self.message = message
        self.details = details
        self.suggestions = suggestions or []
        super().__init__(message)

    def to_response(self) -> dict:
        """Convert exception to a standardized error response dict."""
        response = {
            "success": False,
            "error": self.message,
            "error_type": self.error_type.value,
        }
        if self.details:
            response["details"] = self.details
        if self.suggestions:
            response["suggestions"] = self.suggestions
        return response


class ConnectionError(OrionBeltError):
    """Database connection failures."""

    error_type = ErrorType.CONNECTION


class DatabaseError(OrionBeltError):
    """Database operation failures (query execution, schema analysis)."""

    error_type = ErrorType.DATABASE


class ValidationError(OrionBeltError):
    """Input validation failures."""

    error_type = ErrorType.VALIDATION


class ParameterError(OrionBeltError):
    """Missing or invalid tool parameters."""

    error_type = ErrorType.PARAMETER


class RDFError(OrionBeltError):
    """RDF/ontology store errors."""

    error_type = ErrorType.RDF


class StoreNotInitializedError(OrionBeltError):
    """RDF or vector store not initialized."""

    error_type = ErrorType.STORE


class SessionRequiredError(OrionBeltError):
    """The request cannot be attributed to any client's state."""

    error_type = ErrorType.SESSION


class UnknownConnectionError(OrionBeltError):
    """A connection handle that names no live session."""

    error_type = ErrorType.UNKNOWN_CONNECTION


class DependencyError(OrionBeltError):
    """Missing optional dependency."""

    error_type = ErrorType.DEPENDENCY


class ConnectionBusyError(OrionBeltError):
    """Too many calls are already waiting on one database connection.

    Calls on a connection run one at a time, so a slow query holds everything
    queued behind it. Without a bound, a burst of requests queued without
    limit -- each one a tool call nobody hears back from until the whole queue
    ahead of it drains. Refusing at the door is the answer a caller can act on.
    """

    error_type = ErrorType.BUSY
