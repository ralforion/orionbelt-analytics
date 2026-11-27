"""Custom exception hierarchy for OrionBelt Analytics.

Provides structured error handling with consistent error types
that replace ad-hoc string-based error_type fields.
"""

from enum import StrEnum
from typing import ClassVar


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
    SIGN_IN = "data_source_sign_in"


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


class SignInError(OrionBeltError):
    """Logging in to a database as the user failed; what the user must do.

    The response carries the sign-in ``code`` (``SIGN_IN_REQUIRED``,
    ``DATA_SOURCE_SIGN_IN_REFUSED``, ``DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED``,
    ``DATA_SOURCE_SIGN_IN_UNAVAILABLE``) and, for an identity provider that
    wants the user at its sign-in page, its claims challenge.
    """

    error_type = ErrorType.SIGN_IN

    _SUGGESTIONS: ClassVar[dict[str, list[str]]] = {
        "SIGN_IN_REQUIRED": [
            "This database runs queries as the signed-in user: sign in to the "
            "server with your identity provider, then try again."
        ],
        "DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED": [
            "Sign in again in your MCP client (multi-factor authentication, a "
            "consent or an access policy asks for it), then try again."
        ],
        "DATA_SOURCE_SIGN_IN_REFUSED": [
            "Your account may not have access to this database; ask its "
            "administrator."
        ],
        "DATA_SOURCE_SIGN_IN_UNAVAILABLE": [
            "The database's sign-in service did not answer; try again shortly."
        ],
    }

    def __init__(self, message: str, code: str, claims: str | None = None):
        super().__init__(message, suggestions=list(self._SUGGESTIONS.get(code, [])))
        self.code = code
        self.claims = claims

    def to_response(self) -> dict:
        response = super().to_response()
        response["code"] = self.code
        if self.claims:
            response["claims"] = self.claims
        return response
