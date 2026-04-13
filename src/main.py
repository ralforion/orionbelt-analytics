"""Main MCP server application using FastMCP.

This module is a thin registration layer for MCP tools. The actual
implementation logic lives in src/handlers/ modules.
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Union

from dotenv import load_dotenv
from pydantic import BaseModel
from fastmcp import FastMCP, Context
from fastmcp.apps import AppConfig, ResourceCSP

from .database_manager import DatabaseManager
from .ontology_generator import OntologyGenerator
from .obqc_validator import OBQCValidator
from . import __version__, __name__ as SERVER_NAME
from .oxigraph_store import OxigraphStoreManager, OXIGRAPH_AVAILABLE

# --- Centralized path and env loading (Task 1 & 2) ---
from .paths import (
    get_env_file_path,
    ensure_output_dir,
    get_oxigraph_store_dir,
    get_chart_viewer_path,
    get_skills_dir,
)

# Load environment variables using centralized path resolution (Task 1: C4 fix)
env_path = get_env_file_path()
env_loaded = False
if env_path:
    load_dotenv(str(env_path))
    env_loaded = True

# Configure logging
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Log environment loading info
if env_loaded:
    logger.info(f"Environment loaded from: {env_path}")
else:
    logger.warning("No .env file found - environment variables may not be available")

# Ensure output directory exists (Task 2: S3)
ensure_output_dir()


# --- MCP Apps: Load Chart Viewer HTML (Task 2: S3 - use paths.py) ---
_CHART_VIEWER_HTML_PATH = get_chart_viewer_path()
try:
    CHART_VIEWER_HTML = _CHART_VIEWER_HTML_PATH.read_text(encoding="utf-8")
    logger.info(f"Loaded chart viewer HTML app from {_CHART_VIEWER_HTML_PATH}")
except FileNotFoundError:
    CHART_VIEWER_HTML = None
    logger.warning(f"Chart viewer HTML app not found at {_CHART_VIEWER_HTML_PATH}")


# --- MCP Server Setup ---

mcp = FastMCP(
    name=SERVER_NAME,
    instructions="""
# OrionBelt Analytics - AI-Powered Database Intelligence

Semantic database analysis with ontology-enhanced Text-to-SQL generation.

## Core Capabilities

- **Database Connectivity:** PostgreSQL, Snowflake, Dremio, ClickHouse, BigQuery, DuckDB, Databricks, MySQL
- **Schema Intelligence:** Table/column analysis with relationship mapping
- **Ontology Generation:** RDF/OWL with oba: namespace linking to SQL tables
- **Safe SQL Execution:** Fan-trap detection, injection prevention, query validation
- **Data Visualization:** Interactive charts (Matplotlib, Plotly)

## Recommended Workflow

1. `connect_database()` -> Establish secure connection
2. `list_schemas()` -> Discover available schemas
3. `analyze_schema()` -> Get schema structure with relationships
4. `generate_ontology()` -> Create semantic ontology with oba: annotations
5. `execute_sql_query()` -> Run validated SQL with fan-trap protection
6. `generate_chart()` -> Visualize results

## Critical Guides (Claude Skills)

- **Fan-trap prevention:** `/fan-trap-prevention` - Prevent data multiplication in multi-table queries
- **SQL best practices:** `/sql-best-practices` - Identifier qualification and safe patterns
- **Chart examples:** `/chart-examples` - Visualization guide for all chart types

## Key Features

**Ontology-Enhanced SQL:**
- oba: namespace annotations link ontology classes to SQL tables
- Automatic JOIN condition generation from relationships
- Business-friendly semantic layer over technical schemas

**Security:**
- SQL injection prevention
- Read-only enforcement
- Query timeout protection
- Result size limits (max 5,000 rows)

**Performance:**
- Connection pooling
- Parallel schema analysis
- Cached ontology and schema data

## Important Notes

- Always fully qualify identifiers: `schema.table.column`
- Review foreign_keys from analyze_schema() before complex JOINs
- Use validate_sql_syntax() before executing queries
- For multi-fact aggregation, use UNION ALL pattern (see /fan-trap-prevention)

Version: {__version__}
Supported Databases: PostgreSQL, Snowflake, Dremio, ClickHouse, BigQuery, DuckDB, Databricks, MySQL
Primary Use Case: Semantic database analysis with ontology-enhanced Text-to-SQL
""".format(
        __version__=__version__
    ),
)


# --- MCP Apps: Register Chart Viewer Resource ---
@mcp.resource(
    "ui://orionbelt/chart-viewer",
    app=AppConfig(
        csp=ResourceCSP(
            resource_domains=["https://cdn.plot.ly", "https://unpkg.com"],
            connect_domains=[],
        )
    ),
)
def chart_viewer_resource() -> str:
    """Serve the interactive chart viewer app for MCP Apps."""
    if CHART_VIEWER_HTML is None:
        return """<!DOCTYPE html>
<html><body>
<h1>Chart Viewer Not Available</h1>
<p>The chart viewer HTML app was not found at startup.</p>
</body></html>"""
    return CHART_VIEWER_HTML


# --- MCP Resources: Skills (Task 2: S3 - use get_skills_dir()) ---

@mcp.resource("skill://fan-trap-prevention")
def fan_trap_prevention_skill() -> str:
    """Fan-trap prevention guide - comprehensive patterns and solutions."""
    skills_path = get_skills_dir() / "fan-trap-prevention.md"
    if skills_path.exists():
        return skills_path.read_text()
    return "Fan-trap prevention skill not found. Please ensure .claude/skills/fan-trap-prevention.md exists."


@mcp.resource("skill://sql-best-practices")
def sql_best_practices_skill() -> str:
    """SQL best practices - identifier qualification and common patterns."""
    skills_path = get_skills_dir() / "sql-best-practices.md"
    if skills_path.exists():
        return skills_path.read_text()
    return "SQL best practices skill not found. Please ensure .claude/skills/sql-best-practices.md exists."


@mcp.resource("skill://chart-examples")
def chart_examples_skill() -> str:
    """Chart generation examples - all chart types with complete examples."""
    skills_path = get_skills_dir() / "chart-examples.md"
    if skills_path.exists():
        return skills_path.read_text()
    return "Chart examples skill not found. Please ensure .claude/skills/chart-examples.md exists."


@mcp.resource("skill://analytical-workflow")
def analytical_workflow_skill() -> str:
    """Complete analytical session workflow - optimal tool chain and best practices."""
    skills_path = get_skills_dir() / "analytical-workflow.md"
    if skills_path.exists():
        return skills_path.read_text()
    return "Analytical workflow skill not found. Please ensure .claude/skills/analytical-workflow.md exists."


# --- Session State Management (Task 5: W2 - Decomposed SessionData) ---

from .session import SessionData


def get_session_id(ctx: Context) -> str:
    """Get a unique session identifier from context."""
    if hasattr(ctx, "session_id") and ctx.session_id:
        return str(ctx.session_id)
    if hasattr(ctx, "session") and ctx.session:
        return f"session_{id(ctx.session)}"
    logger.warning("Could not determine session ID from context, using default_session")
    return "default_session"


def _get_connection_fingerprint(db_manager: DatabaseManager) -> str:
    """Generate unique fingerprint for current database connection."""
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


def _calculate_schema_hash(tables_info: List[Any]) -> str:
    """Calculate deterministic hash of schema structure."""
    schema_structure = {"tables": []}

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
                {"name": col.name, "data_type": col.data_type, "nullable": col.is_nullable}
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


def _clear_session_state(session: SessionData, reason: str = "connection change") -> None:
    """Clear all session state caches and indexes."""
    logger.info(f"Clearing session state ({reason})")

    session.clear_schema_cache()
    session.schema_file = None
    session.ontology_file = None
    session.r2rml_file = None
    session.loaded_ontology = None
    session.loaded_ontology_path = None

    session.obqc_validator = None

    session.oxigraph_store = None
    session.oxigraph_initialized = False

    session.graphrag_manager = None
    session.graphrag_initialized = False

    logger.info("Session state cleared")


class ServerState:
    """Manages server state with per-session isolation and idle eviction."""

    def __init__(self):
        self._sessions: Dict[str, SessionData] = {}
        self._eviction_task: Optional[asyncio.Task] = None

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)

    def get_session(self, session_id: str) -> SessionData:
        """Get or create session data for a given session ID."""
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionData()
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

    def cleanup_session(self, session_id: str):
        """Clean up a specific session's resources."""
        if session_id in self._sessions:
            session = self._sessions[session_id]
            if session.db_manager:
                try:
                    session.db_manager.disconnect()
                except Exception as e:
                    logger.warning(f"Error disconnecting db for session {session_id}: {e}")
            if session.rdf_store.oxigraph_store:
                try:
                    session.rdf_store.oxigraph_store.close()
                except Exception as e:
                    logger.warning(f"Error closing Oxigraph for session {session_id}: {e}")
            del self._sessions[session_id]
            logger.debug(f"Cleaned up session: {session_id}")

    def cleanup(self):
        """Clean up all resources."""
        if self._eviction_task and not self._eviction_task.done():
            self._eviction_task.cancel()
            logger.debug("Cancelled session eviction task")
        for session_id in list(self._sessions.keys()):
            self.cleanup_session(session_id)

    # --- Idle eviction ---

    def _ensure_eviction_task(self):
        """Lazily start the eviction background task if not already running."""
        if self._eviction_task is not None and not self._eviction_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._eviction_task = loop.create_task(self._eviction_loop())
            logger.info("Started session eviction background task")
        except RuntimeError:
            pass  # No event loop (e.g. tests or sync context)

    async def _eviction_loop(self):
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
                self._evict_idle_sessions(idle_timeout)
            except asyncio.CancelledError:
                logger.info("Session eviction task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in session eviction loop: {e}", exc_info=True)
                await asyncio.sleep(scan_interval)

    def _evict_idle_sessions(self, idle_timeout: int):
        """Scan sessions and evict those idle beyond the timeout."""
        now = datetime.now()
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
            self.cleanup_session(session_id)

        if evicting > 0:
            logger.info(
                f"Evicted {evicting} idle session(s). "
                f"Remaining: {len(self._sessions)}"
            )


# Global server state
_server_state = ServerState()


def get_session_data(ctx: Context) -> SessionData:
    """Get session data for the current context."""
    session_id = get_session_id(ctx)
    return _server_state.get_session(session_id)


def get_session_db_manager(ctx: Context) -> DatabaseManager:
    """Get or create a DatabaseManager for the current session."""
    session = get_session_data(ctx)
    if session.db_manager is None:
        session.db_manager = DatabaseManager()
        logger.debug(f"Created new DatabaseManager for session: {get_session_id(ctx)}")
    return session.db_manager


def get_session_obqc_validator(ctx: Context) -> Optional[OBQCValidator]:
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

        if has_generated_ontology:
            output_dir = ensure_output_dir()
            ontology_path = output_dir / session.ontology_file
            if ontology_path.exists():
                ontology_generator.load_from_file(str(ontology_path))
                logger.debug(f"OBQC loaded ontology from session file: {session.ontology_file}")
        elif has_loaded_ontology:
            ontology_generator.load_from_string(session.loaded_ontology)
            logger.debug(
                f"OBQC loaded ontology from session's loaded ontology: {session.loaded_ontology_path}"
            )

        session.obqc_validator.load_ontology(ontology_generator.graph, base_uri)
        logger.debug(f"Initialized OBQC validator for session: {get_session_id(ctx)}")

    return session.obqc_validator


def get_session_safe_filename(ctx: Context, prefix: str, suffix: str = "") -> str:
    """Generate a connection-safe filename to prevent cross-database file collisions."""
    session = get_session_data(ctx)
    connection_prefix = (
        session.connection_id[:8]
        if session.connection_id and len(session.connection_id) >= 8
        else "default"
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    if suffix:
        return f"{prefix}_{connection_prefix}_{suffix}_{timestamp}"
    return f"{prefix}_{connection_prefix}_{timestamp}"


def load_ontology_from_session(ctx: Context) -> tuple[OntologyGenerator, str]:
    """Load ontology from session state."""
    session = get_session_data(ctx)
    filename = session.ontology_file
    if not filename:
        raise ValueError("No ontology file in session state. Run generate_ontology first.")

    output_dir = ensure_output_dir()
    ontology_path = output_dir / filename

    if not ontology_path.exists():
        raise ValueError(f"Ontology file not found: {filename}")

    generator = _server_state.get_ontology_generator()
    generator.load_from_file(str(ontology_path))

    return generator, filename


# --- Error Response Helper (Task 4 & 9: W1 partial) ---

class ErrorResponse(BaseModel):
    """Standardized error response format."""
    error: str
    error_type: str = "unknown"
    details: Optional[str] = None


def create_error_response(
    error_msg: str, error_type: str = "unknown", details: Optional[str] = None
) -> str:
    """Create a standardized error response.

    DEPRECATED: Use exceptions from src.exceptions instead.
    Example: ConnectionError("message").to_response()

    This function is kept for backward compatibility but new code should
    use the exception hierarchy in src/exceptions.py.
    """
    response = ErrorResponse(error=error_msg, error_type=error_type, details=details)
    return response.model_dump_json()


# --- Oxigraph Store Helper ---

def get_oxigraph_store(ctx: Context) -> Optional[OxigraphStoreManager]:
    """Get or initialize connection-scoped Oxigraph store for the session."""
    session = get_session_data(ctx)

    if not OXIGRAPH_AVAILABLE:
        logger.warning("pyoxigraph not available - SPARQL features disabled")
        return None

    if session.oxigraph_store is None:
        try:
            store_path = get_oxigraph_store_dir(connection_id=session.connection_id)
            session.oxigraph_store = OxigraphStoreManager(store_path=store_path)
            session.oxigraph_initialized = True

            if session.connection_id:
                logger.info(f"Initialized connection-scoped Oxigraph store at: {store_path}")
            else:
                logger.info(f"Initialized Oxigraph store at: {store_path} (legacy mode)")

        except Exception as e:
            logger.error(f"Failed to initialize Oxigraph store: {e}")
            return None

    return session.oxigraph_store


# --- Handler imports (Task 7: C1/S2) ---

from .handlers import connection as _h_connection
from .handlers import schema as _h_schema
from .handlers import ontology as _h_ontology
from .handlers import query as _h_query
from .handlers import chart as _h_chart
from .handlers import rdf as _h_rdf
from .handlers import graphrag as _h_graphrag
from .handlers import info as _h_info


# ============================================================
# MCP Tool Registration
# ============================================================
# Each tool delegates to its handler module. The @mcp.tool()
# decorators MUST stay here for FastMCP registration.
# ============================================================


@mcp.tool()
async def connect_database(ctx: Context, db_type: str) -> str:
    """Connect to a database using credentials from environment variables.

    Args:
        db_type: Database type - 'postgresql', 'snowflake', 'dremio', 'clickhouse', 'bigquery', 'duckdb', 'databricks', or 'mysql'

    Returns:
        Connection status message or error JSON
    """
    return await _h_connection.connect_database(
        ctx, db_type,
        get_session_db_manager=get_session_db_manager,
        get_session_data=get_session_data,
        create_error_response=create_error_response,
        _get_connection_fingerprint=_get_connection_fingerprint,
        _clear_session_state=_clear_session_state,
    )


@mcp.tool()
async def list_schemas(ctx: Context) -> List[str]:
    """Get a list of available schemas from the connected database.

    Returns:
        List of schema names or error response
    """
    return await _h_connection.list_schemas(ctx, get_session_db_manager=get_session_db_manager)


@mcp.tool()
async def reset_cache(ctx: Context, cache_type: Optional[str] = None) -> Dict[str, Any]:
    """Reset cached schema and/or ontology data to force re-analysis.

    Args:
        cache_type: Type of cache to reset ("schema", "ontology", "all", or None)

    Returns:
        Dictionary with status and cleared cache types
    """
    return await _h_schema.reset_cache(ctx, cache_type, get_session_data=get_session_data)


@mcp.tool()
async def analyze_schema(
    ctx: Context,
    schema_name: Optional[str] = None,
    lightweight: bool = True,
) -> Dict[str, Any]:
    """Analyze database schema and return table metadata with relationships.

    Args:
        schema_name: Schema to analyze (optional, uses default if not specified)
        lightweight: If True (default), return minimal data (table names, FK relationships, fan-trap warnings).
                     If False, return full schema with all column details.

    Returns:
        Schema analysis results
    """
    return await _h_schema.analyze_schema(
        ctx, schema_name, lightweight,
        get_session_data=get_session_data,
        get_session_db_manager=get_session_db_manager,
        get_session_safe_filename=get_session_safe_filename,
        _auto_initialize_graphrag_background=_h_graphrag._auto_initialize_graphrag_background,
    )


@mcp.tool()
async def get_table_details(
    ctx: Context,
    table_name: str,
    schema_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Get detailed metadata for a single table.

    Args:
        table_name: Name of the table to analyze
        schema_name: Schema containing the table (optional)

    Returns:
        Table details including columns, constraints, and row count
    """
    return await _h_schema.get_table_details(
        ctx, table_name, schema_name, get_session_db_manager=get_session_db_manager
    )


@mcp.tool()
async def generate_ontology(
    ctx: Context,
    schema_info: Optional[str] = None,
    schema_name: Optional[str] = None,
    base_uri: str = "http://example.com/ontology/",
    auto_persist: bool = True,
    graph_uri: Optional[str] = None,
) -> str:
    """Generate an RDF ontology from database schema. AUTO-ANALYZES schema if needed!

    Args:
        schema_name: Name of the schema to analyze and generate ontology for
        schema_info: Optional pre-analyzed schema JSON (usually not needed)
        base_uri: Base URI for the ontology (default: http://example.com/ontology/)
        auto_persist: If True (default), automatically store in Oxigraph RDF database.
        graph_uri: Optional custom graph URI for RDF storage

    Returns:
        Ontology TTL or status message
    """
    return await _h_ontology.generate_ontology(
        ctx, schema_info, schema_name, base_uri, auto_persist, graph_uri,
        get_session_data=get_session_data,
        get_session_db_manager=get_session_db_manager,
        get_session_safe_filename=get_session_safe_filename,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
        _server_state=_server_state,
    )


@mcp.tool()
async def suggest_semantic_names(
    ctx: Context,
    ontology_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract and analyze names from a generated ontology to identify abbreviations and cryptic names.

    Args:
        ontology_file: The ontology filename from generate_ontology response

    Returns:
        Dictionary containing extracted names, analysis results, and instructions
    """
    return await _h_ontology.suggest_semantic_names(
        ctx, ontology_file,
        get_session_data=get_session_data,
        load_ontology_from_session=load_ontology_from_session,
    )


@mcp.tool()
async def apply_semantic_names(
    ctx: Context,
    suggestions: str,
    ontology_file: Optional[str] = None,
    save_to_file: bool = True,
) -> str:
    """Apply semantic name suggestions to an existing ontology.

    The suggestions parameter must be a JSON object with 'classes', 'properties',
    and/or 'relationships' arrays. Each entry needs 'original_name' and 'suggested_name'.

    Example suggestions JSON:
    {
      "classes": [
        {"original_name": "acctbal", "suggested_name": "AccountBalance", "description": "Account balance records"}
      ],
      "properties": [
        {"original_name": "bankid", "table_name": "acctbal", "suggested_name": "Bank Identifier"}
      ],
      "relationships": [
        {"original_name": "acctbal_to_banks", "suggested_name": "Account Bank Relationship"}
      ]
    }

    Args:
        suggestions: JSON string with classes/properties/relationships arrays (see example above)
        ontology_file: The ontology filename from generate_ontology response
        save_to_file: Whether to save the updated ontology to a file
    """
    return await _h_ontology.apply_semantic_names(
        ctx, suggestions, ontology_file, save_to_file,
        get_session_data=get_session_data,
        get_session_safe_filename=get_session_safe_filename,
        load_ontology_from_session=load_ontology_from_session,
        create_error_response=create_error_response,
        get_oxigraph_store=get_oxigraph_store,
    )


@mcp.tool()
async def load_my_ontology(
    ctx: Context,
    import_folder: str = "./import",
    auto_persist: bool = True,
    graph_uri: Optional[str] = None,
) -> Dict[str, Any]:
    """Load the newest .ttl ontology file from the import folder.

    Args:
        import_folder: Path to the folder containing .ttl files
        auto_persist: If True (default), store in Oxigraph RDF database
        graph_uri: Optional custom graph URI for RDF storage

    Returns:
        Dictionary with ontology information and status
    """
    return await _h_ontology.load_my_ontology(
        ctx, import_folder, auto_persist, graph_uri,
        get_session_data=get_session_data,
        get_oxigraph_store=get_oxigraph_store,
    )


@mcp.tool()
async def download_ontology(
    ctx: Context,
    schema_name: Optional[str] = None,
    source: str = "rdf",
) -> Dict[str, Any]:
    """Download ontology as TTL file from RDF store or tmp folder.

    Args:
        schema_name: Name of the schema
        source: Where to get the ontology from ("rdf" or "file")

    Returns:
        Dictionary with ontology content and file info
    """
    return await _h_ontology.download_ontology(
        ctx, schema_name, source,
        get_session_data=get_session_data,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def download_r2rml(
    ctx: Context,
    schema_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Download R2RML mapping file from tmp folder.

    Args:
        schema_name: Name of the schema

    Returns:
        Dictionary with R2RML content and file info
    """
    return await _h_ontology.download_r2rml(
        ctx, schema_name, get_session_data=get_session_data
    )


@mcp.tool()
async def sample_table_data(
    ctx: Context,
    table_name: str,
    schema_name: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Sample data from a specific table for analysis.

    Args:
        table_name: Name of the table to sample
        schema_name: Schema containing the table (optional)
        limit: Maximum number of rows to return (default: 10, max: 100)

    Returns:
        List of sample rows as dictionaries
    """
    return await _h_schema.sample_table_data(
        ctx, table_name, schema_name, limit, get_session_db_manager=get_session_db_manager
    )


@mcp.tool()
async def validate_sql_syntax(ctx: Context, sql_query: str) -> Dict[str, Any]:
    """Validate SQL syntax, security, and fan-trap risks before execution.

    Args:
        sql_query: SQL SELECT statement to validate

    Returns:
        Validation results with warnings and suggestions
    """
    return await _h_query.validate_sql_syntax(
        ctx, sql_query,
        get_session_db_manager=get_session_db_manager,
        get_session_obqc_validator=get_session_obqc_validator,
    )


@mcp.tool()
async def execute_sql_query(
    ctx: Context,
    sql_query: str,
    limit: int = 1000,
    checklist_completed: bool = False,
    query_intent: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute SQL query with validation and fan-trap protection.

    Args:
        sql_query: SQL SELECT statement (fully qualified identifiers required)
        limit: Maximum rows to return (default: 1000, max: 5,000)
        checklist_completed: Confirmation that pre-execution checklist is complete
        query_intent: Optional natural language description of query intent

    Returns:
        Query results with data, columns, row_count, execution_time_ms
    """
    return await _h_query.execute_sql_query(
        ctx, sql_query, limit, checklist_completed, query_intent,
        get_session_data=get_session_data,
        get_session_db_manager=get_session_db_manager,
        create_error_response=create_error_response,
    )


@mcp.tool(app=AppConfig(resource_uri="ui://orionbelt/chart-viewer"))
async def generate_chart(
    ctx: Context,
    data_source: Union[List[Dict[str, Any]], str],
    chart_type: str,
    x_column: str,
    y_column: Optional[Union[str, List[str]]] = None,
    color_column: Optional[str] = None,
    title: Optional[str] = None,
    chart_style: str = "grouped",
    width: int = 800,
    height: int = 600,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    output_format: str = "interactive",
) -> str:
    """Generate interactive or static charts from query results.

    Args:
        data_source: List of dicts from execute_sql_query()['data']
        chart_type: 'bar', 'line', 'scatter', or 'heatmap'
        x_column: Column name for X-axis
        y_column: Column name(s) for Y-axis
        color_column: Optional column for grouping/coloring
        title: Chart title
        chart_style: 'default', 'stacked', or 'grouped'
        width: Chart width in pixels
        height: Chart height in pixels
        sort_by: Column to sort by
        sort_order: 'ascending' or 'descending'
        output_format: "interactive" (default, renders via MCP Apps) or "image" (saves PNG file)

    Returns:
        Interactive chart JSON via MCP Apps or file path to saved PNG
    """
    return await _h_chart.generate_chart(
        ctx, data_source, chart_type, x_column, y_column,
        color_column, title, chart_style, width, height,
        sort_by, sort_order, output_format,
        get_session_data=get_session_data,
    )


@mcp.tool()
async def get_server_info(ctx: Context) -> Dict[str, Any]:
    """Get information about the MCP server and its capabilities.

    Returns:
        Dictionary containing server information
    """
    return await _h_info.get_server_info(ctx)


# --- GraphRAG Tools ---

@mcp.tool()
async def initialize_graphrag(
    ctx: Context,
    schema_name: Optional[str] = None,
    embedding_model: str = "tfidf",
) -> str:
    """Initialize GraphRAG for intelligent schema navigation and retrieval.

    Args:
        schema_name: Schema to initialize (uses last analyzed if not specified)
        embedding_model: Embedding type ("tfidf" or "sentence-transformers")

    Returns:
        Initialization status message
    """
    return await _h_graphrag.initialize_graphrag(
        ctx, schema_name, embedding_model,
        get_session_data=get_session_data,
        get_session_db_manager=get_session_db_manager,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def graphrag_search(
    ctx: Context,
    query: str,
    top_k: int = 5,
    element_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Search schema using natural language via GraphRAG semantic search.

    Args:
        query: Natural language search query
        top_k: Number of results to return
        element_type: Filter by type ("table", "column", "relationship", or None)

    Returns:
        Dictionary with search results and similarity scores
    """
    return await _h_graphrag.graphrag_search(
        ctx, query, top_k, element_type,
        get_session_data=get_session_data,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def graphrag_query_context(
    ctx: Context,
    query: str,
    max_tables: int = 5,
    max_columns: int = 20,
) -> Dict[str, Any]:
    """Get optimized context for SQL query generation using GraphRAG.

    Args:
        query: Natural language description of what you want to query
        max_tables: Maximum tables to include in context
        max_columns: Maximum columns to include in context

    Returns:
        Optimized context with relevant tables, columns, relationships
    """
    return await _h_graphrag.graphrag_query_context(
        ctx, query, max_tables, max_columns,
        get_session_data=get_session_data,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def graphrag_find_join_path(
    ctx: Context,
    from_table: str,
    to_table: str,
    max_hops: int = 3,
) -> Dict[str, Any]:
    """Find join path between two tables using GraphRAG graph traversal.

    Args:
        from_table: Source table name
        to_table: Target table name
        max_hops: Maximum number of joins allowed

    Returns:
        Dictionary with join path specifications
    """
    return await _h_graphrag.graphrag_find_join_path(
        ctx, from_table, to_table, max_hops,
        get_session_data=get_session_data,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def graphrag_overview(ctx: Context) -> Dict[str, Any]:
    """Get GraphRAG schema overview with statistics and communities.

    Returns:
        Dictionary with comprehensive schema statistics
    """
    return await _h_graphrag.graphrag_overview(
        ctx, get_session_data=get_session_data, create_error_response=create_error_response
    )


# --- Oxigraph RDF Store & SPARQL Tools ---

@mcp.tool()
async def store_ontology_in_rdf(
    ctx: Context,
    schema_name: Optional[str] = None,
    graph_uri: Optional[str] = None,
) -> str:
    """Store current session ontology in persistent RDF store with SPARQL access.

    Args:
        schema_name: Schema name (uses last analyzed if not specified)
        graph_uri: Named graph URI (auto-generated if not specified)

    Returns:
        Status message with triple count
    """
    return await _h_rdf.store_ontology_in_rdf(
        ctx, schema_name, graph_uri,
        get_session_data=get_session_data,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def query_sparql(
    ctx: Context,
    sparql_query: str,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Execute SPARQL SELECT query against stored ontologies.

    Args:
        sparql_query: SPARQL SELECT query string
        timeout_seconds: Query timeout

    Returns:
        Query results as list of bindings
    """
    return await _h_rdf.query_sparql(
        ctx, sparql_query, timeout_seconds,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def query_sparql_ask(ctx: Context, sparql_query: str) -> Dict[str, Any]:
    """Execute SPARQL ASK query (returns true/false).

    Args:
        sparql_query: SPARQL ASK query

    Returns:
        Boolean result
    """
    return await _h_rdf.query_sparql_ask(
        ctx, sparql_query,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def add_rdf_knowledge(
    ctx: Context,
    subject: str,
    predicate: str,
    object: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Add custom knowledge/metadata to the RDF store.

    Args:
        subject: Subject URI
        predicate: Predicate URI
        object: Object value (literal or URI)
        metadata: Optional metadata dict

    Returns:
        Confirmation message
    """
    return await _h_rdf.add_rdf_knowledge(
        ctx, subject, predicate, object, metadata,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def list_tables_sparql(
    ctx: Context,
    schema_graph: Optional[str] = None,
) -> Dict[str, Any]:
    """List all tables from stored ontology using SPARQL.

    Args:
        schema_graph: Optional graph URI to query

    Returns:
        List of table names
    """
    return await _h_rdf.list_tables_sparql(
        ctx, schema_graph,
        get_session_data=get_session_data,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def find_columns_by_type_sparql(
    ctx: Context,
    data_type: str,
    schema_graph: Optional[str] = None,
) -> Dict[str, Any]:
    """Find columns by data type using SPARQL.

    Args:
        data_type: SQL data type (e.g., "INTEGER", "VARCHAR", "DATE")
        schema_graph: Optional graph URI

    Returns:
        List of matching columns
    """
    return await _h_rdf.find_columns_by_type_sparql(
        ctx, data_type, schema_graph,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
    )


@mcp.tool()
async def get_rdf_store_stats(ctx: Context) -> Dict[str, Any]:
    """Get statistics about the persistent RDF store.

    Returns:
        Store statistics including triple counts and graphs
    """
    return await _h_rdf.get_rdf_store_stats(
        ctx,
        get_oxigraph_store=get_oxigraph_store,
        create_error_response=create_error_response,
    )


# --- Cleanup on shutdown ---

def cleanup_server():
    """Clean up server resources."""
    _server_state.cleanup()
