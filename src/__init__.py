"""OrionBelt Analytics - Ontology-based MCP server for your Text-2-SQL convenience."""

__version__ = "1.7.2"
__author__ = "OrionBelt Analytics Contributors"
__email__ = "contributors@example.com"
__description__ = "OrionBelt Analytics - the Ontology-based MCP server for your Text-2-SQL convenience"
__name__ = "OrionBelt Analytics"

from .config import config_manager
from .constants import SUPPORTED_DB_TYPES

# Export main components for easier imports
from .database_manager import ColumnInfo, DatabaseManager, TableInfo
from .ontology_generator import OntologyGenerator
from .session import SessionData

__all__ = [
    "DatabaseManager",
    "TableInfo",
    "ColumnInfo",
    "OntologyGenerator",
    "SessionData",
    "config_manager",
    "SUPPORTED_DB_TYPES",
    "__version__",
    "__name__",
    "__description__",
]
