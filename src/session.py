"""Session state management for OrionBelt Analytics.

Provides decomposed, focused state objects for per-session isolation.
Each MCP session gets its own SessionData instance containing:
- ConnectionState: database connection tracking
- OntologyState: ontology generation and loading
- SchemaCache: cached schema analysis results
- GraphRAGState: GraphRAG integration with init tracking
- RDFStoreState: Oxigraph RDF store with init tracking
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)


class ConnectionState:
    """Database connection tracking."""

    def __init__(self):
        self.connection_id: Optional[str] = None
        self.connected_at: Optional[datetime] = None
        self.db_manager: Optional[Any] = None  # DatabaseManager (avoid circular import)


class OntologyState:
    """Ontology generation and loading state."""

    def __init__(self):
        self.ontology_file: Optional[str] = None
        self.r2rml_file: Optional[str] = None
        self.loaded_ontology: Optional[str] = None  # TTL content
        self.loaded_ontology_path: Optional[str] = None  # File path
        self.obqc_validator: Optional[Any] = None  # OBQCValidator (avoid circular import)


class SchemaCache:
    """Cached schema analysis results."""

    def __init__(self):
        self._cached_schema: Optional[Dict[str, List[Any]]] = None  # schema_name -> List[TableInfo]
        self._last_analyzed_schema: Optional[str] = None
        self.schema_file: Optional[str] = None

    def cache_schema_analysis(self, schema_name: str, tables_info: List[Any]) -> None:
        """Cache schema analysis results for reuse."""
        if self._cached_schema is None:
            self._cached_schema = {}
        cache_key = schema_name or "_default_"
        self._cached_schema[cache_key] = tables_info
        self._last_analyzed_schema = schema_name
        logger.debug(f"Cached schema analysis for '{cache_key}': {len(tables_info)} tables")

    def get_cached_schema(self, schema_name: str) -> Optional[List[Any]]:
        """Get cached schema analysis results if available."""
        if self._cached_schema is None:
            return None
        cache_key = schema_name or "_default_"
        cached = self._cached_schema.get(cache_key)
        if cached:
            logger.debug(f"Using cached schema for '{cache_key}': {len(cached)} tables")
        return cached

    def clear(self) -> None:
        """Clear cached schema analysis (e.g., on reconnect)."""
        self._cached_schema = None
        self._last_analyzed_schema = None
        self.schema_file = None
        logger.debug("Cleared schema cache")

    def get_last_analyzed_schema(self) -> Optional[str]:
        """Get the name of the last analyzed schema."""
        return self._last_analyzed_schema


class GraphRAGState:
    """GraphRAG integration state with Future-based init tracking."""

    def __init__(self):
        self.graphrag_manager: Optional[Any] = None  # GraphRAGManager
        self.graphrag_initialized: bool = False
        self._init_task: Optional[asyncio.Task] = None


class RDFStoreState:
    """Oxigraph RDF store state with Future-based init tracking."""

    def __init__(self):
        self.oxigraph_store: Optional[Any] = None  # OxigraphStoreManager
        self.oxigraph_initialized: bool = False
        self._init_task: Optional[asyncio.Task] = None


class SessionData:
    """Per-session data storage with decomposed state objects.

    Composes focused state objects for clean separation of concerns.
    """

    def __init__(self):
        self.connection = ConnectionState()
        self.ontology = OntologyState()
        self.schema_cache = SchemaCache()
        self.graphrag = GraphRAGState()
        self.rdf_store = RDFStoreState()
        # Activity tracking for idle eviction
        self.created_at: datetime = datetime.now()
        self.last_activity: datetime = datetime.now()

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = datetime.now()

    # --- Convenience accessors for backward compatibility ---
    # These will be used during migration; handler code should use
    # the decomposed state objects directly.

    @property
    def db_manager(self):
        return self.connection.db_manager

    @db_manager.setter
    def db_manager(self, value):
        self.connection.db_manager = value

    @property
    def connection_id(self):
        return self.connection.connection_id

    @connection_id.setter
    def connection_id(self, value):
        self.connection.connection_id = value

    @property
    def connected_at(self):
        return self.connection.connected_at

    @connected_at.setter
    def connected_at(self, value):
        self.connection.connected_at = value

    @property
    def ontology_file(self):
        return self.ontology.ontology_file

    @ontology_file.setter
    def ontology_file(self, value):
        self.ontology.ontology_file = value

    @property
    def r2rml_file(self):
        return self.ontology.r2rml_file

    @r2rml_file.setter
    def r2rml_file(self, value):
        self.ontology.r2rml_file = value

    @property
    def loaded_ontology(self):
        return self.ontology.loaded_ontology

    @loaded_ontology.setter
    def loaded_ontology(self, value):
        self.ontology.loaded_ontology = value

    @property
    def loaded_ontology_path(self):
        return self.ontology.loaded_ontology_path

    @loaded_ontology_path.setter
    def loaded_ontology_path(self, value):
        self.ontology.loaded_ontology_path = value

    @property
    def obqc_validator(self):
        return self.ontology.obqc_validator

    @obqc_validator.setter
    def obqc_validator(self, value):
        self.ontology.obqc_validator = value

    @property
    def schema_file(self):
        return self.schema_cache.schema_file

    @schema_file.setter
    def schema_file(self, value):
        self.schema_cache.schema_file = value

    @property
    def graphrag_manager(self):
        return self.graphrag.graphrag_manager

    @graphrag_manager.setter
    def graphrag_manager(self, value):
        self.graphrag.graphrag_manager = value

    @property
    def graphrag_initialized(self):
        return self.graphrag.graphrag_initialized

    @graphrag_initialized.setter
    def graphrag_initialized(self, value):
        self.graphrag.graphrag_initialized = value

    @property
    def oxigraph_store(self):
        return self.rdf_store.oxigraph_store

    @oxigraph_store.setter
    def oxigraph_store(self, value):
        self.rdf_store.oxigraph_store = value

    @property
    def oxigraph_initialized(self):
        return self.rdf_store.oxigraph_initialized

    @oxigraph_initialized.setter
    def oxigraph_initialized(self, value):
        self.rdf_store.oxigraph_initialized = value

    # --- Delegated methods ---

    def cache_schema_analysis(self, schema_name: str, tables_info: List[Any]) -> None:
        """Cache schema analysis results for reuse."""
        self.schema_cache.cache_schema_analysis(schema_name, tables_info)

    def get_cached_schema(self, schema_name: str) -> Optional[List[Any]]:
        """Get cached schema analysis results if available."""
        return self.schema_cache.get_cached_schema(schema_name)

    def clear_schema_cache(self) -> None:
        """Clear cached schema analysis (e.g., on reconnect)."""
        self.schema_cache.clear()

    def get_last_analyzed_schema(self) -> Optional[str]:
        """Get the name of the last analyzed schema."""
        return self.schema_cache.get_last_analyzed_schema()
