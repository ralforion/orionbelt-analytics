"""Abstract base class for database drivers."""

import logging
from abc import ABC, abstractmethod
from typing import Any

from ..database_manager import TableInfo

logger = logging.getLogger(__name__)


class DatabaseDriver(ABC):
    """Protocol for database-specific operations.

    Each concrete driver encapsulates all logic specific to one database
    backend (connection building, schema introspection, query validation,
    query execution, sampling).  DatabaseManager delegates to the active
    driver and handles cross-cutting concerns (caching, credentials,
    reconnection).
    """

    # Subclasses must set this to the database type string
    db_type: str = ""

    @abstractmethod
    def connect(self, **params: Any) -> bool:
        """Establish a connection to the database.

        Args:
            **params: Database-specific connection parameters.

        Returns:
            True if the connection was established successfully.
        """

    @abstractmethod
    def get_schemas(self) -> list[str]:
        """Return a list of user-visible schema names."""

    @abstractmethod
    def get_tables(self, schema_name: str | None = None) -> list[str]:
        """Return a list of table names in the given schema."""

    def get_views(self, schema_name: str | None = None) -> dict[str, str | None]:
        """Return ``{view_name: definition_sql}`` for views in the schema.

        Views are deliberately excluded from :meth:`get_tables` (which filters
        to base tables) because they do not belong in the ontology: a view
        pre-joins its sources, so modelling it as an OWL class would duplicate
        concepts the base tables already carry and give the FK/fan-trap
        reasoning an isolated node with no relationships. They are still
        valuable to GraphRAG -- a view body is analyst-authored SQL carrying
        both business vocabulary and validated join conditions.

        Concrete: not abstract. A driver that cannot enumerate views (or whose
        backend has none) inherits the empty default rather than failing, so
        adding this never breaks an existing driver.

        Args:
            schema_name: Schema to inspect, or None for the default schema.

        Returns:
            Mapping of view name to its SQL definition. The definition is None
            when the backend exposes the view but withholds its body (commonly
            a permissions matter, e.g. PostgreSQL returns NULL to non-owners).
        """
        return {}

    @abstractmethod
    def analyze_table(
        self, table_name: str, schema_name: str | None = None
    ) -> TableInfo | None:
        """Analyze a table and return its metadata."""

    def analyze_tables(
        self, table_names: list[str], schema_name: str | None = None
    ) -> dict[str, TableInfo]:
        """Analyze several tables, skipping the ones that fail.

        Concrete, not abstract: the default reflects one table at a time, which
        is what every caller did before this existed. A driver whose backend can
        answer for a whole schema in one round trip overrides it -- discovery
        asks once per schema, so the work belongs where the dialect is known.

        A table that cannot be reflected is left out rather than failing the
        batch: one unreadable table must not cost the schema its discovery.

        Args:
            table_names: Tables to analyze.
            schema_name: Schema they live in, or None for the default.

        Returns:
            The metadata by table name, in the order asked, without the
            tables that could not be read.
        """
        analyzed: dict[str, TableInfo] = {}
        for name in table_names:
            try:
                info = self.analyze_table(name, schema_name)
            except Exception as e:
                logger.warning(f"Failed to analyze table {name}: {e}")
                continue
            if info is not None:
                analyzed[name] = info
        return analyzed

    @abstractmethod
    def validate_sql_syntax(
        self, sql_query: str, validation_result: dict[str, Any]
    ) -> dict[str, Any]:
        """Perform database-level SQL syntax validation.

        Args:
            sql_query: The raw SQL query string.
            validation_result: A pre-populated dict with query_type, warnings, etc.
                The driver should set ``is_valid``, ``error``, ``error_type``,
                ``database_error``, and ``suggestions`` as appropriate.

        Returns:
            The updated ``validation_result`` dict.
        """

    @abstractmethod
    def execute_sql_query(self, sql_query: str, limit: int = 1000) -> dict[str, Any]:
        """Execute a SQL query and return structured results."""

    @abstractmethod
    def sample_table_data(
        self,
        table_name: str,
        schema_name: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return sample rows from a table."""

    @abstractmethod
    def test_connection(self) -> bool:
        """Return True if the connection is healthy."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection and release resources."""
