"""Custom exception hierarchy for OrionBelt Analytics.

Provides structured error handling with consistent error types
that replace ad-hoc string-based error_type fields.
"""

from enum import Enum
from typing import Optional, List


class ErrorType(str, Enum):
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
    RUNTIME = "runtime_error"
    CONFIGURATION = "configuration_error"


class OrionBeltError(Exception):
    """Base exception for all OrionBelt Analytics errors."""

    error_type: ErrorType = ErrorType.INTERNAL

    def __init__(self, message: str, details: Optional[str] = None,
                 suggestions: Optional[List[str]] = None):
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


class SecurityError(OrionBeltError):
    """Security violations (SQL injection, forbidden operations)."""
    error_type = ErrorType.SECURITY


class SyntaxError_(OrionBeltError):
    """SQL syntax errors (named with underscore to avoid shadowing builtin)."""
    error_type = ErrorType.SYNTAX


class ForbiddenOperationError(OrionBeltError):
    """Attempted destructive/disallowed operation."""
    error_type = ErrorType.FORBIDDEN


class RDFError(OrionBeltError):
    """RDF/ontology store errors."""
    error_type = ErrorType.RDF


class StoreNotInitializedError(OrionBeltError):
    """RDF or vector store not initialized."""
    error_type = ErrorType.STORE


class DependencyError(OrionBeltError):
    """Missing optional dependency."""
    error_type = ErrorType.DEPENDENCY


class ConfigurationError(OrionBeltError):
    """Invalid or missing configuration."""
    error_type = ErrorType.CONFIGURATION
