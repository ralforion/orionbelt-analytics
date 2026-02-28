"""Main MCP server application using FastMCP."""

import logging
import os
import base64
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Union

from dotenv import load_dotenv
from pydantic import BaseModel
from fastmcp import FastMCP, Context
from fastmcp.utilities.types import Image
from mcp_ui_server import create_ui_resource
from mcp_ui_server.core import UIResource

from .database_manager import DatabaseManager, TableInfo, ColumnInfo
from .ontology_generator import OntologyGenerator
from .r2rml_generator import R2RMLGenerator
from .obqc_validator import OBQCValidator
from .config import config_manager
from . import __version__, __name__ as SERVER_NAME
from .graphrag import GraphRAGManager
from .oxigraph_store import OxigraphStoreManager, OXIGRAPH_AVAILABLE

# Load environment variables from project root FIRST
# Try multiple possible paths for .env file
possible_env_paths = [
    os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'),  # relative to src
    os.path.join(os.getcwd(), '.env'),  # current working directory
    '/Users/ralfbecher/Documents/GitHub/mcp-servers/orionbelt-analytics/.env'  # absolute path
]

env_loaded = False
for env_path in possible_env_paths:
    if os.path.exists(env_path):
        load_dotenv(env_path)
        env_loaded = True
        break

# Configure logging
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Log environment loading info
logger.info(f"Environment loading: tried {len(possible_env_paths)} paths, loaded: {env_loaded}")
if env_loaded:
    logger.info(f"POSTGRES_HOST from environment: {os.getenv('POSTGRES_HOST')}")
else:
    logger.warning("No .env file found - environment variables may not be available")

# Output directory for generated files
from .constants import DEFAULT_OUTPUT_DIR
OUTPUT_DIR = Path(__file__).parent.parent / os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
OUTPUT_DIR.mkdir(exist_ok=True)

def get_output_dir() -> Path:
    """Get the output directory for generated files."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    return OUTPUT_DIR

def get_oxigraph_store_dir(connection_id: Optional[str] = None) -> Path:
    """
    Get the Oxigraph store directory.

    Now connection-scoped to prevent RDF data collisions between
    different databases with the same schema name.

    Args:
        connection_id: Database connection fingerprint.
                      If None, uses legacy global store (backward compat).

    Returns:
        Path to Oxigraph store directory for this connection
    """
    if connection_id:
        # NEW: Connection-scoped RDF store
        store_dir = OUTPUT_DIR / "oxigraph" / connection_id / "store"
    else:
        # LEGACY: Global RDF store (backward compatibility)
        store_dir = OUTPUT_DIR / "oxigraph_store"

    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


# --- MCP Apps: Load Chart Viewer HTML ---
# Load chart viewer HTML app at startup for MCP Apps interactive rendering
_CHART_VIEWER_HTML_PATH = Path(__file__).parent / "apps" / "chart_viewer.html"
try:
    CHART_VIEWER_HTML = _CHART_VIEWER_HTML_PATH.read_text(encoding="utf-8")
    logger.info(f"Loaded chart viewer HTML app from {_CHART_VIEWER_HTML_PATH}")
except FileNotFoundError:
    CHART_VIEWER_HTML = None
    logger.warning(f"Chart viewer HTML app not found at {_CHART_VIEWER_HTML_PATH}")


# --- MCP Server Setup ---

# Create server instance with comprehensive instructions
mcp = FastMCP(
    name=SERVER_NAME,
    instructions="""
# OrionBelt Analytics - AI-Powered Database Intelligence

Semantic database analysis with ontology-enhanced Text-to-SQL generation.

## Core Capabilities

- **Database Connectivity:** PostgreSQL, Snowflake, Dremio
- **Schema Intelligence:** Table/column analysis with relationship mapping
- **Ontology Generation:** RDF/OWL with db: namespace linking to SQL tables
- **Safe SQL Execution:** Fan-trap detection, injection prevention, query validation
- **Data Visualization:** Interactive charts (Matplotlib, Plotly)

## Recommended Workflow

1. `connect_database()` → Establish secure connection
2. `list_schemas()` → Discover available schemas
3. `analyze_schema()` → Get schema structure with relationships
4. `generate_ontology()` → Create semantic ontology with db: annotations
5. `execute_sql_query()` → Run validated SQL with fan-trap protection
6. `generate_chart()` → Visualize results

## Critical Guides (Claude Skills)

- **Fan-trap prevention:** `/fan-trap-prevention` - Prevent data multiplication in multi-table queries
- **SQL best practices:** `/sql-best-practices` - Identifier qualification and safe patterns
- **Chart examples:** `/chart-examples` - Visualization guide for all chart types

## Key Features

**Ontology-Enhanced SQL:**
- db: namespace annotations link ontology classes to SQL tables
- Automatic JOIN condition generation from relationships
- Business-friendly semantic layer over technical schemas

**Security:**
- SQL injection prevention
- Read-only enforcement
- Query timeout protection
- Result size limits (max 10,000 rows)

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
Supported Databases: PostgreSQL, Snowflake, Dremio
Primary Use Case: Semantic database analysis with ontology-enhanced Text-to-SQL
""".format(__version__=__version__)
)


# --- MCP Apps: Register Chart Viewer Resource ---
# This resource serves the interactive chart viewer HTML app for MCP Apps rendering
# MIME type text/html+mcp is the official MCP Apps MIME type
@mcp.resource("ui://orionbelt/chart-viewer", mime_type="text/html+mcp")
def chart_viewer_resource() -> str:
    """Serve the interactive chart viewer app for MCP Apps.

    This HTML app receives chart data via the MCP Apps postMessage protocol
    and renders interactive Plotly visualizations in Claude Desktop.
    """
    if CHART_VIEWER_HTML is None:
        return """<!DOCTYPE html>
<html><body>
<h1>Chart Viewer Not Available</h1>
<p>The chart viewer HTML app was not found at startup.</p>
</body></html>"""
    return CHART_VIEWER_HTML


# --- MCP Resources: Skills ---
# Serve skill documentation as MCP resources for HTTP transport compatibility

@mcp.resource("skill://fan-trap-prevention")
def fan_trap_prevention_skill() -> str:
    """Fan-trap prevention guide - comprehensive patterns and solutions."""
    skills_path = Path(__file__).parent.parent / ".claude" / "skills" / "fan-trap-prevention.md"
    if skills_path.exists():
        return skills_path.read_text()
    return "Fan-trap prevention skill not found. Please ensure .claude/skills/fan-trap-prevention.md exists."

@mcp.resource("skill://sql-best-practices")
def sql_best_practices_skill() -> str:
    """SQL best practices - identifier qualification and common patterns."""
    skills_path = Path(__file__).parent.parent / ".claude" / "skills" / "sql-best-practices.md"
    if skills_path.exists():
        return skills_path.read_text()
    return "SQL best practices skill not found. Please ensure .claude/skills/sql-best-practices.md exists."

@mcp.resource("skill://chart-examples")
def chart_examples_skill() -> str:
    """Chart generation examples - all chart types with complete examples."""
    skills_path = Path(__file__).parent.parent / ".claude" / "skills" / "chart-examples.md"
    if skills_path.exists():
        return skills_path.read_text()
    return "Chart examples skill not found. Please ensure .claude/skills/chart-examples.md exists."


# --- Dependency Management ---

class SessionData:
    """Per-session data storage."""

    def __init__(self):
        self.db_manager: Optional[DatabaseManager] = None
        self.schema_file: Optional[str] = None
        self.ontology_file: Optional[str] = None
        self.r2rml_file: Optional[str] = None
        self.obqc_validator: Optional[OBQCValidator] = None
        # Loaded ontology from external file (via load_my_ontology)
        self.loaded_ontology: Optional[str] = None  # TTL content
        self.loaded_ontology_path: Optional[str] = None  # File path
        # Cached schema analysis results (to avoid re-querying)
        self._cached_schema: Optional[Dict[str, List[TableInfo]]] = None  # schema_name -> tables
        self._last_analyzed_schema: Optional[str] = None  # Store last analyzed schema name
        # GraphRAG integration
        self.graphrag_manager: Optional[GraphRAGManager] = None
        self.graphrag_initialized: bool = False
        # Oxigraph RDF store for SPARQL
        self.oxigraph_store: Optional[OxigraphStoreManager] = None
        self.oxigraph_initialized: bool = False
        # Connection tracking (Phase 1: Auto session management)
        self.connection_id: Optional[str] = None
        self.connected_at: Optional[datetime] = None

    def cache_schema_analysis(self, schema_name: str, tables_info: List[TableInfo]) -> None:
        """Cache schema analysis results for reuse."""
        if self._cached_schema is None:
            self._cached_schema = {}
        cache_key = schema_name or "_default_"
        self._cached_schema[cache_key] = tables_info
        self._last_analyzed_schema = schema_name  # Remember the schema name
        logger.debug(f"Cached schema analysis for '{cache_key}': {len(tables_info)} tables")

    def get_cached_schema(self, schema_name: str) -> Optional[List[TableInfo]]:
        """Get cached schema analysis results if available."""
        if self._cached_schema is None:
            return None
        cache_key = schema_name or "_default_"
        cached = self._cached_schema.get(cache_key)
        if cached:
            logger.debug(f"Using cached schema for '{cache_key}': {len(cached)} tables")
        return cached

    def clear_schema_cache(self) -> None:
        """Clear cached schema analysis (e.g., on reconnect)."""
        self._cached_schema = None
        self._last_analyzed_schema = None
        logger.debug("Cleared schema cache")

    def get_last_analyzed_schema(self) -> Optional[str]:
        """Get the name of the last analyzed schema."""
        return self._last_analyzed_schema


def get_session_id(ctx: Context) -> str:
    """Get a unique session identifier from context.

    Args:
        ctx: The FastMCP context

    Returns:
        A unique session identifier string
    """
    # Try ctx.session_id first (may be None with HTTP transport)
    if hasattr(ctx, 'session_id') and ctx.session_id:
        return str(ctx.session_id)
    # Fall back to id(ctx.session) as unique identifier
    if hasattr(ctx, 'session') and ctx.session:
        return f"session_{id(ctx.session)}"
    # Last resort: use a default session (single-user mode)
    return "default_session"


def _get_connection_fingerprint(db_manager: DatabaseManager) -> str:
    """Generate unique fingerprint for current database connection.

    Used to detect when connection changes and trigger session cleanup.

    Args:
        db_manager: DatabaseManager instance with active connection

    Returns:
        Unique connection identifier string (hash)
    """
    import hashlib

    conn_info = db_manager.connection_info
    if not conn_info:
        return "no_connection"

    # Create fingerprint from connection parameters
    fingerprint_data = (
        f"{conn_info.get('database_type', '')}://"
        f"{conn_info.get('host', '')}:{conn_info.get('port', '')}/"
        f"{conn_info.get('database', '')}"
        f"@{conn_info.get('schema', '')}"
    )

    # Hash to create short identifier
    return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]


def _calculate_schema_hash(tables_info: List[Any]) -> str:
    """
    Calculate deterministic hash of schema structure.

    This hash captures the structural elements of the schema (tables, columns,
    relationships) and is used to detect when the schema has changed, triggering
    a new version creation.

    Only includes stable structural elements:
    - Table names, schemas
    - Column names, data types, nullability
    - Primary keys
    - Foreign key relationships

    Excludes volatile elements:
    - Row counts (changes with data)
    - Comments (documentation changes)
    - Default values (can change independently)
    - Indexes (performance tuning)

    Args:
        tables_info: List of TableInfo objects from schema analysis

    Returns:
        SHA256 hash (full 64 characters)
    """
    import hashlib
    import json

    schema_structure = {"tables": []}

    # Sort tables by name for deterministic hash
    sorted_tables = sorted(tables_info, key=lambda t: t.name)

    for table in sorted_tables:
        table_data = {
            "name": table.name,
            "schema": table.schema,
            "columns": [],
            "primary_keys": sorted(table.primary_keys) if table.primary_keys else [],
            "foreign_keys": []
        }

        # Sort columns by name
        sorted_columns = sorted(table.columns, key=lambda c: c.name)
        for col in sorted_columns:
            table_data["columns"].append({
                "name": col.name,
                "data_type": col.data_type,
                "nullable": col.nullable
                # Explicitly exclude: default, comment (volatile)
            })

        # Sort foreign keys
        if table.foreign_keys:
            sorted_fks = sorted(table.foreign_keys, key=lambda f: f.column)
            for fk in sorted_fks:
                table_data["foreign_keys"].append({
                    "column": fk.column,
                    "referenced_table": fk.referenced_table,
                    "referenced_column": fk.referenced_column
                })

        schema_structure["tables"].append(table_data)

    # Generate deterministic JSON string
    json_str = json.dumps(schema_structure, sort_keys=True)

    # Return full SHA256 hash
    return hashlib.sha256(json_str.encode()).hexdigest()


def _clear_session_state(session: SessionData, reason: str = "connection change") -> None:
    """Clear all session state caches and indexes.

    Called when database connection changes to ensure clean state.

    Args:
        session: SessionData instance to clear
        reason: Reason for clearing (for logging)
    """
    logger.info(f"🧹 Clearing session state ({reason})")

    # Clear caches
    session.clear_schema_cache()
    session.schema_file = None
    session.ontology_file = None
    session.r2rml_file = None
    session.loaded_ontology = None
    session.loaded_ontology_path = None

    # Clear GraphRAG
    session.graphrag_manager = None
    session.graphrag_initialized = False

    # Clear RDF store (keep the store object but note it may have stale data)
    # Don't clear oxigraph_store itself as it's a persistent database
    # Users should explicitly reset if needed

    logger.info("✅ Session state cleared")


class ServerState:
    """Manages server state with per-session isolation."""

    def __init__(self):
        self._sessions: Dict[str, SessionData] = {}

    def get_session(self, session_id: str) -> SessionData:
        """Get or create session data for a given session ID."""
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionData()
            logger.debug(f"Created new session: {session_id}")
        return self._sessions[session_id]

    def get_ontology_generator(self, base_uri: str = "http://example.com/ontology/") -> OntologyGenerator:
        """Create a new ontology generator instance.

        Always returns a NEW instance to avoid state pollution between calls.
        Each caller gets an isolated generator with its own RDF graph.
        """
        return OntologyGenerator(base_uri=base_uri)

    def cleanup_session(self, session_id: str):
        """Clean up a specific session's resources."""
        if session_id in self._sessions:
            session = self._sessions[session_id]
            if session.db_manager:
                session.db_manager.disconnect()
            del self._sessions[session_id]
            logger.debug(f"Cleaned up session: {session_id}")

    def cleanup(self):
        """Clean up all resources."""
        for session_id in list(self._sessions.keys()):
            self.cleanup_session(session_id)

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
    """Get or create OBQC validator for the current session.

    Returns None if no ontology is loaded. The validator is lazily initialized
    when first requested and caches the ontology schema for efficient validation.

    Ontology sources (in priority order):
    1. Session's generated ontology file (from generate_ontology)
    2. Session's loaded external ontology (from load_my_ontology)

    Args:
        ctx: The FastMCP context

    Returns:
        OBQCValidator instance if ontology is available, None otherwise
    """
    session = get_session_data(ctx)

    # Check if ontology is available in session (no global fallback for isolation)
    has_generated_ontology = session.ontology_file is not None
    has_loaded_ontology = session.loaded_ontology is not None

    if not has_generated_ontology and not has_loaded_ontology:
        return None

    # Lazy initialization of OBQC validator
    if session.obqc_validator is None:
        session.obqc_validator = OBQCValidator()

        # Load ontology into validator - use session-specific OntologyGenerator
        base_uri = os.getenv("ONTOLOGY_BASE_URI", "http://example.com/ontology/")
        ontology_generator = OntologyGenerator(base_uri)

        if has_generated_ontology:
            # Priority 1: Load from session's generated ontology file
            output_dir = get_output_dir()
            ontology_path = output_dir / session.ontology_file
            if ontology_path.exists():
                ontology_generator.load_from_file(str(ontology_path))
                logger.debug(f"OBQC loaded ontology from session file: {session.ontology_file}")
        elif has_loaded_ontology:
            # Priority 2: Load from session's external ontology (load_my_ontology)
            ontology_generator.load_from_string(session.loaded_ontology)
            logger.debug(f"OBQC loaded ontology from session's loaded ontology: {session.loaded_ontology_path}")

        session.obqc_validator.load_ontology(ontology_generator.graph, base_uri)
        logger.debug(f"Initialized OBQC validator for session: {get_session_id(ctx)}")

    return session.obqc_validator


def get_session_safe_filename(ctx: Context, prefix: str, suffix: str = "") -> str:
    """Generate a connection-safe filename to prevent cross-database file collisions.

    Uses connection ID and microsecond-precision timestamp to ensure uniqueness.
    Files from different database connections are isolated, preventing overwrites.

    Args:
        ctx: The FastMCP context
        prefix: Filename prefix (e.g., "schema", "ontology", "r2rml")
        suffix: Optional suffix before extension (e.g., schema name)

    Returns:
        Unique filename like "ontology_a7f3b2c1_public_20260227_143045123456.ttl"
    """
    session = get_session_data(ctx)
    # Use connection_id (database fingerprint) for file isolation
    connection_prefix = session.connection_id[:8] if session.connection_id and len(session.connection_id) >= 8 else "default"
    # Use microsecond precision to avoid collisions
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    if suffix:
        return f"{prefix}_{connection_prefix}_{suffix}_{timestamp}"
    return f"{prefix}_{connection_prefix}_{timestamp}"


def load_ontology_from_session(ctx: Context) -> tuple[OntologyGenerator, str]:
    """Load ontology from session state.

    This helper function retrieves the ontology filename from session state
    and loads it into an OntologyGenerator instance.

    Args:
        ctx: The FastMCP context

    Returns:
        Tuple of (OntologyGenerator with loaded graph, filename)

    Raises:
        ValueError: If no ontology file is found in session or file doesn't exist
    """
    session = get_session_data(ctx)
    filename = session.ontology_file
    if not filename:
        raise ValueError("No ontology file in session state. Run generate_ontology first.")

    output_dir = get_output_dir()
    ontology_path = output_dir / filename

    if not ontology_path.exists():
        raise ValueError(f"Ontology file not found: {filename}")

    generator = _server_state.get_ontology_generator()
    generator.load_from_file(str(ontology_path))

    return generator, filename


# --- Error Response Helper ---

class ErrorResponse(BaseModel):
    """Standardized error response format."""
    error: str
    error_type: str = "unknown"
    details: Optional[str] = None

def create_error_response(error_msg: str, error_type: str = "unknown", details: Optional[str] = None) -> str:
    """Create a standardized error response."""
    response = ErrorResponse(error=error_msg, error_type=error_type, details=details)
    return response.model_dump_json()

def safe_execute(func, *args, **kwargs):
    """Helper function to safely execute MCP tool functions with error handling."""
    try:
        return func(*args, **kwargs)
    except RuntimeError as e:
        logger.error(f"Runtime error in {func.__name__}: {e}")
        return create_error_response(str(e), "runtime_error")
    except Exception as e:
        logger.error(f"Unexpected error in {func.__name__}: {e}")
        return create_error_response(f"Internal server error: {str(e)}", "internal_error")

# --- MCP Tools ---

@mcp.tool()
async def connect_database(ctx: Context, db_type: str) -> str:
    """Connect to a database using credentials from environment variables.

    Args:
        db_type: Database type - either 'postgresql', 'snowflake', 'dremio', or 'clickhouse'

    Returns:
        Connection status message or error JSON
    """
    # Validate input parameters
    if not db_type or db_type not in ["postgresql", "snowflake", "dremio", "clickhouse"]:
        return create_error_response(
            f"Invalid database type '{db_type}'. Use 'postgresql', 'snowflake', 'dremio', or 'clickhouse'.",
            "validation_error"
        )
    
    db_manager = get_session_db_manager(ctx)
    
    if db_type == "postgresql":
        # Get parameters from environment
        host = os.getenv("POSTGRES_HOST")
        port = os.getenv("POSTGRES_PORT")
        database = os.getenv("POSTGRES_DATABASE")
        username = os.getenv("POSTGRES_USERNAME")
        password = os.getenv("POSTGRES_PASSWORD")
        
        # Validate required parameters
        required_params = {
            "POSTGRES_HOST": host,
            "POSTGRES_PORT": port,
            "POSTGRES_DATABASE": database,
            "POSTGRES_USERNAME": username,
            "POSTGRES_PASSWORD": password
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return create_error_response(
                f"Missing required environment variables for PostgreSQL: {', '.join(missing_params)}. Please check your .env file.",
                "validation_error"
            )
        
        success = db_manager.connect_postgresql(
            host=str(host),
            port=int(port),
            database=str(database),
            username=str(username),
            password=str(password)
        )
        db_name = database
        
    elif db_type == "snowflake":
        # Get parameters from environment
        account = os.getenv("SNOWFLAKE_ACCOUNT")
        username = os.getenv("SNOWFLAKE_USERNAME")
        password = os.getenv("SNOWFLAKE_PASSWORD")
        warehouse = os.getenv("SNOWFLAKE_WAREHOUSE")
        database = os.getenv("SNOWFLAKE_DATABASE")
        schema = os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC")
        
        # Validate required parameters
        required_params = {
            "SNOWFLAKE_ACCOUNT": account,
            "SNOWFLAKE_USERNAME": username,
            "SNOWFLAKE_PASSWORD": password,
            "SNOWFLAKE_WAREHOUSE": warehouse,
            "SNOWFLAKE_DATABASE": database
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return create_error_response(
                f"Missing required environment variables for Snowflake: {', '.join(missing_params)}. Please check your .env file.",
                "validation_error"
            )
        
        success = db_manager.connect_snowflake(
            account=str(account),
            username=str(username),
            password=str(password),
            warehouse=str(warehouse),
            database=str(database),
            schema=schema
        )
        db_name = database
        
    elif db_type == "dremio":
        # Get parameters from environment
        host = os.getenv("DREMIO_HOST")
        port = os.getenv("DREMIO_PORT")
        username = os.getenv("DREMIO_USERNAME")
        password = os.getenv("DREMIO_PASSWORD")
        
        # Validate required parameters
        required_params = {
            "DREMIO_HOST": host,
            "DREMIO_PORT": port,
            "DREMIO_USERNAME": username,
            "DREMIO_PASSWORD": password
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return create_error_response(
                f"Missing required environment variables for Dremio: {', '.join(missing_params)}. Please check your .env file.",
                "validation_error"
            )
        
        # Dremio uses PostgreSQL protocol
        success = db_manager.connect_postgresql(
            host=str(host),
            port=int(port),
            database="DREMIO",  # Dremio typically uses this as default
            username=str(username),
            password=str(password)
        )
        db_name = "DREMIO"

    elif db_type == "clickhouse":
        # Get parameters from environment
        host = os.getenv("CLICKHOUSE_HOST")
        port = os.getenv("CLICKHOUSE_PORT", "8123")
        database = os.getenv("CLICKHOUSE_DATABASE")
        username = os.getenv("CLICKHOUSE_USERNAME", "default")
        password = os.getenv("CLICKHOUSE_PASSWORD", "")
        protocol = os.getenv("CLICKHOUSE_PROTOCOL", "http")
        secure = os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true"

        # Validate required parameters
        required_params = {
            "CLICKHOUSE_HOST": host,
            "CLICKHOUSE_DATABASE": database
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return create_error_response(
                f"Missing required environment variables for ClickHouse: {', '.join(missing_params)}. Please check your .env file.",
                "validation_error"
            )

        success = db_manager.connect_clickhouse(
            host=str(host),
            port=int(port),
            database=str(database),
            username=str(username),
            password=str(password),
            protocol=protocol,
            secure=secure
        )
        db_name = database

    if success:
        # PHASE 1: Check if connection changed and clear state if needed
        session = get_session_data(ctx)

        # Get new connection fingerprint
        new_conn_id = _get_connection_fingerprint(db_manager)

        # Check if this is a different connection
        if session.connection_id and session.connection_id != new_conn_id:
            logger.info(f"🔄 Connection changed (old: {session.connection_id[:8]}..., new: {new_conn_id[:8]}...)")
            _clear_session_state(session, reason="connection change")
        elif not session.connection_id:
            logger.info(f"🔗 Initial connection established: {new_conn_id[:8]}...")

        # Update connection tracking
        session.connection_id = new_conn_id
        session.connected_at = datetime.now()

        # Clear schema cache (always, even for same connection)
        session.clear_schema_cache()

        await ctx.info(f"Connected to {db_type}: {db_name}")
        return f"Successfully connected to {db_type} database: {db_name}"
    else:
        await ctx.info(f"Database connection failed; check credentials and try again")
        return create_error_response(
            f"Failed to connect to {db_type} database: {db_name}",
            "connection_error"
        )


@mcp.tool()
async def list_schemas(ctx: Context) -> List[str]:
    """Get a list of available schemas from the connected database.

    Returns:
        List of schema names or error response
    """
    db_manager = get_session_db_manager(ctx)
    schemas = db_manager.get_schemas()
    if schemas:
        await ctx.info(f"Found {len(schemas)} schemas; next call should be analyze_schema")
    else:
        await ctx.info("No schemas found")
    return schemas if schemas else []


@mcp.tool()
async def reset_cache(
    ctx: Context,
    cache_type: Optional[str] = None
) -> Dict[str, Any]:
    """Reset cached schema and/or ontology data to force re-analysis.

    Use this tool when you need to:
    - Re-analyze schema after database changes
    - Regenerate ontology with different parameters
    - Start fresh with a new analysis workflow

    Args:
        cache_type: Type of cache to reset. Options:
            - "schema": Clear only schema cache (forces re-analysis)
            - "ontology": Clear only ontology cache (forces regeneration)
            - "all" or None: Clear all caches (default)

    Returns:
        Dictionary with status and cleared cache types
    """
    session = get_session_data(ctx)
    cleared = []

    cache_type_lower = (cache_type or "all").lower()

    if cache_type_lower in ("schema", "all"):
        session.clear_schema_cache()
        session.schema_file = None
        session.r2rml_file = None
        session._last_analyzed_schema = None
        cleared.append("schema")

    if cache_type_lower in ("ontology", "all"):
        session.ontology_file = None
        session.loaded_ontology = None
        session.obqc_validator = None
        cleared.append("ontology")

    await ctx.info(f"Cache cleared: {', '.join(cleared)}")

    return {
        "status": "success",
        "cleared_caches": cleared,
        "message": f"Cleared {', '.join(cleared)} cache(s). You can now re-run analyze_schema and/or generate_ontology.",
        "next_steps": {
            "schema": "Call analyze_schema() to re-analyze database schema",
            "ontology": "Call generate_ontology() to regenerate ontology"
        }
    }


@mcp.tool()
async def analyze_schema(
    ctx: Context,
    schema_name: Optional[str] = None,
    lightweight: bool = True
) -> Dict[str, Any]:
    """Analyze database schema and return table metadata with relationships.

    Args:
        schema_name: Schema to analyze (optional, uses default if not specified)
        lightweight: If True (default), return minimal data (table names, FK relationships, fan-trap warnings).
                     If False, return full schema with all column details.

    Returns (lightweight=True):
        Dict with:
            - schema: Schema name
            - table_count: Number of tables
            - table_names: List of table names only
            - relationships: FK relationships map (critical for joins!)
            - fan_trap_warnings: Warnings about potential fan-traps

    Returns (lightweight=False):
        Dict with full details:
            - schema: Schema name
            - table_count: Number of tables
            - tables: List of complete table metadata (columns, PKs, FKs, row counts)
            - schema_file: Saved JSON file path in tmp/
            - r2rml_file: Generated R2RML mapping file path
            - next_steps: Recommended workflow

    Lightweight Mode (Default):
        - Returns ONLY table names and FK relationships
        - Saves ~90% tokens compared to full schema
        - Use get_table_details(table_name) to get details on-demand
        - Ideal for initial schema discovery

    Full Mode (lightweight=False):
        - Returns complete column metadata for all tables
        - Results are cached for reuse by generate_ontology()
        - R2RML mappings auto-generated
        - Use when you need full schema upfront

    Important:
        - Foreign keys are CRITICAL for fan-trap prevention
        - Review relationships before complex joins
        - Use lightweight mode first, then get_table_details() as needed

    Recommended Next Step:
        Call generate_ontology() to create semantic ontology for SQL generation

    Example:
        ```python
        # Lightweight - get overview first
        schema = analyze_schema(schema_name="public", lightweight=True)
        # Then get details for specific tables
        details = get_table_details(table_name="orders")

        # Or full schema upfront
        schema = analyze_schema(schema_name="public", lightweight=False)
        ```
    """
    # Check if schema is already cached - return early with guidance
    session = get_session_data(ctx)
    effective_schema = schema_name or ""
    cached_tables = session.get_cached_schema(effective_schema)

    if cached_tables:
        # Schema already analyzed - return cache hit response
        # Check if ontology is also already generated
        ontology_also_cached = session.ontology_file is not None

        if ontology_also_cached:
            # Both schema AND ontology are cached - direct to enrichment
            await ctx.info(f"Schema AND ontology already cached - proceed directly to suggest_semantic_names()")
            return {
                "schema": effective_schema or "default",
                "table_count": len(cached_tables),
                "cache_hit": True,
                "ontology_cached": True,
                "message": "STOP! Both schema AND ontology are already CACHED. For enrichment, call suggest_semantic_names() directly!",
                "schema_file": session.schema_file,
                "ontology_file": session.ontology_file,
                "next_step": "suggest_semantic_names",
                "instruction": "Call suggest_semantic_names() NOW - do NOT call any other tools first!"
            }
        else:
            # Only schema is cached - direct to generate_ontology
            await ctx.info(f"Schema cached with {len(cached_tables)} tables - proceed to generate_ontology()")
            return {
                "schema": effective_schema or "default",
                "table_count": len(cached_tables),
                "cache_hit": True,
                "message": f"Schema already CACHED ({len(cached_tables)} tables). Call generate_ontology() next.",
                "schema_file": session.schema_file,
                "next_step": "generate_ontology",
                "instruction": "Call generate_ontology() NOW - do NOT call analyze_schema again!"
            }

    db_manager = get_session_db_manager(ctx)
    tables = db_manager.get_tables(schema_name)

    # Prefetch PKs and FKs at schema level (Snowflake optimization)
    if schema_name:
        db_manager.prefetch_schema_constraints(schema_name)

    # LIGHTWEIGHT MODE - Return minimal data
    if lightweight:
        logger.info(f"Analyzing schema in LIGHTWEIGHT mode - {len(tables)} tables")

        # Get full table analysis but only return minimal data
        # IMPORTANT: We must cache full TableInfo objects for generate_ontology() to use later
        table_info_objects = []
        relationships = {}
        fan_trap_warnings = []

        for table_name in tables:
            try:
                # Get full table analysis for caching
                table_info = db_manager.analyze_table(table_name, schema_name)
                if table_info:
                    table_info_objects.append(table_info)  # Cache for generate_ontology()

                    if table_info.foreign_keys:
                        relationships[table_name] = table_info.foreign_keys

                        # Check for fan-trap risk
                        if len(table_info.foreign_keys) > 1:
                            referenced_tables = [fk['referenced_table'] for fk in table_info.foreign_keys]
                            fan_trap_warnings.append({
                                "table": table_name,
                                "warning": f"Table {table_name} connects to multiple tables - potential fan-trap risk",
                                "referenced_tables": referenced_tables,
                                "recommendation": "Use separate CTEs or UNION approach for multi-fact aggregations"
                            })
            except Exception as e:
                logger.warning(f"Failed to analyze table {table_name}: {e}")

        # Cache the full TableInfo objects for generate_ontology() to use later
        session.cache_schema_analysis(schema_name or "", table_info_objects)
        logger.info(f"Cached {len(table_info_objects)} tables for generate_ontology() reuse")

        lightweight_result = {
            "schema": schema_name or "default",
            "table_count": len(tables),
            "table_names": tables,
            "relationships": relationships,
            "mode": "lightweight",
            "token_savings": f"~{len(tables) * 85}% tokens saved vs full schema",
            "note": "Use get_table_details(table_name) to get column details on-demand",
            "next_step": "generate_ontology",
            "cache_hint": "Schema is now CACHED. Call generate_ontology() next - it will use cached data automatically."
        }

        if fan_trap_warnings:
            lightweight_result["fan_trap_warnings"] = fan_trap_warnings

        # PHASE 1: Auto-initialize GraphRAG in background (if enabled)
        auto_graphrag = os.getenv("AUTO_GRAPHRAG", "true").lower()
        if auto_graphrag == "true" and table_info_objects:
            # Start background initialization (non-blocking)
            import asyncio
            asyncio.create_task(
                _auto_initialize_graphrag_background(
                    schema_name=schema_name or "default",
                    tables_info=table_info_objects,
                    session=session,
                    ctx=ctx
                )
            )
            logger.info(f"🔄 GraphRAG auto-initialization started in background")
            lightweight_result["graphrag_auto_init"] = "started in background"

        await ctx.info(f"Lightweight schema analysis: {len(tables)} tables cached, {len(relationships)} with FKs. Next: generate_ontology()")
        return lightweight_result

    all_table_info = []
    table_info_objects = []  # Keep original TableInfo objects for R2RML generation
    for table_name in tables:
        table_info = db_manager.analyze_table(table_name, schema_name)
        if table_info:
            table_info_objects.append(table_info)  # Store for R2RML generation
            # Convert dataclass to dict for JSON serialization
            table_dict = {
                "name": table_info.name,
                "schema": table_info.schema,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "is_nullable": col.is_nullable,
                        "is_primary_key": col.is_primary_key,
                        "is_foreign_key": col.is_foreign_key,
                        "foreign_key_table": col.foreign_key_table,
                        "foreign_key_column": col.foreign_key_column,
                        "comment": col.comment
                    } for col in table_info.columns
                ],
                "primary_keys": table_info.primary_keys,
                "foreign_keys": table_info.foreign_keys,
                "comment": table_info.comment,
                "row_count": table_info.row_count
            }
            all_table_info.append(table_dict)
    
    schema_result = {
        "schema": schema_name or "default",
        "table_count": len(all_table_info),
        "tables": all_table_info
    }

    # Cache the TableInfo objects for reuse by generate_ontology
    session = get_session_data(ctx)
    session.cache_schema_analysis(schema_name or "", table_info_objects)

    # Save schema analysis to output folder (without guidance data)
    schema_filename = None
    try:
        import json
        output_dir = get_output_dir()

        schema_safe = (schema_name or "default").replace(" ", "_").replace(".", "_")
        schema_filename = get_session_safe_filename(ctx, "schema", schema_safe) + ".json"
        schema_file_path = output_dir / schema_filename

        with open(schema_file_path, 'w', encoding='utf-8') as f:
            json.dump(schema_result, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved schema analysis to: {schema_file_path}")

        # Store filename in session state and result
        get_session_data(ctx).schema_file = schema_filename
        schema_result["schema_file"] = schema_filename

    except Exception as e:
        logger.warning(f"Failed to save schema analysis to file: {e}")
        # Continue even if file save failed

    # Generate R2RML mapping
    r2rml_filename = None
    if table_info_objects:
        try:
            output_dir = get_output_dir()

            # Determine base IRI from environment variable with schema appended
            from .constants import DEFAULT_R2RML_BASE_IRI
            effective_schema = schema_name or "default"
            r2rml_base = os.getenv("R2RML_BASE_IRI", DEFAULT_R2RML_BASE_IRI)
            if not r2rml_base.endswith('/'):
                r2rml_base += '/'
            base_iri = f"{r2rml_base}{effective_schema}/"

            # Get database name from connection info
            database_name = db_manager.connection_info.get("database", "database")

            # Generate R2RML mapping
            r2rml_generator = R2RMLGenerator(
                base_iri=base_iri,
                database_name=database_name
            )
            r2rml_content = r2rml_generator.generate_from_schema(
                table_info_objects,
                schema_name=effective_schema
            )

            # Save R2RML mapping to file
            schema_safe = effective_schema.replace(" ", "_").replace(".", "_")
            r2rml_filename = get_session_safe_filename(ctx, "r2rml", schema_safe) + ".ttl"
            r2rml_file_path = output_dir / r2rml_filename

            with open(r2rml_file_path, 'w', encoding='utf-8') as f:
                f.write(r2rml_content)

            logger.info(f"Generated R2RML mapping: {r2rml_file_path}")

            # Store filename in session state and result
            get_session_data(ctx).r2rml_file = r2rml_filename
            schema_result["r2rml_file"] = r2rml_filename
            schema_result["r2rml_base_iri"] = base_iri

            await ctx.info(f"R2RML mapping generated with {len(table_info_objects)} tables")

        except Exception as e:
            logger.warning(f"Failed to generate R2RML mapping: {e}")
            schema_result["r2rml_error"] = str(e)

    # Add analytical workflow guidance for LLM (not saved to file)
    if all_table_info:
        schema_result["next_steps"] = {
            "recommended": "generate_ontology",
            "reason": "Generate ontology with database schema linking for accurate SQL generation and fan-trap prevention",
            "workflow": [
                "1. analyze_schema (completed - schema is now CACHED)",
                "2. generate_ontology (recommended next - will use cached schema automatically)",
                "3. execute_sql_query (with ontology context)"
            ]
        }
        schema_result["schema_cached"] = True
        schema_result["cache_hint"] = (
            "IMPORTANT: Schema analysis is now CACHED for this session. "
            "Do NOT call analyze_schema again - just call generate_ontology() directly. "
            "It will automatically use the cached schema data."
        )
        schema_result["analytical_guidance"] = (
            "Recommended next step: Run generate_ontology() - NO parameters needed!\n\n"
            "The schema is CACHED - generate_ontology will use it automatically.\n"
            "Do NOT call analyze_schema again.\n\n"
            "This will create an ontology with:\n"
            "- Database schema linking (db: namespace)\n"
            "- SQL column references for queries\n"
            "- JOIN conditions for relationships\n"
            "- Metadata for fan-trap prevention\n\n"
            "The ontology provides context for accurate SQL generation."
        )
        schema_result["next_tool"] = "generate_ontology"
        await ctx.info(f"Schema CACHED with {len(all_table_info)} tables. Next: generate_ontology() - no need to pass schema data, it's cached!")
    else:
        await ctx.info("Schema analysis found no tables")

    return schema_result


@mcp.tool()
async def get_table_details(
    ctx: Context,
    table_name: str,
    schema_name: Optional[str] = None
) -> Dict[str, Any]:
    """Get detailed metadata for a single table.

    This tool provides on-demand table details after using analyze_schema(lightweight=True).
    Returns complete column information, constraints, and row count for ONE table only.

    Args:
        table_name: Name of the table to analyze
        schema_name: Schema containing the table (optional, uses default if not specified)

    Returns:
        Dict with:
            - name: Table name
            - schema: Schema name
            - columns: List of column details (name, data_type, nullable, PK/FK flags)
            - primary_keys: List of primary key columns
            - foreign_keys: List of foreign key constraints with references
            - row_count: Approximate number of rows
            - comment: Table comment/description if available

    Usage Pattern:
        1. Call analyze_schema(lightweight=True) to get table list and FK relationships
        2. Use get_table_details() for specific tables you need to query
        3. This hierarchical approach saves 85-90% tokens vs analyzing all tables upfront

    Example:
        ```python
        # Step 1: Get schema overview
        schema = analyze_schema(schema_name="public", lightweight=True)
        # Returns: table_names, relationships, fan_trap_warnings

        # Step 2: Get details for relevant tables only
        orders = get_table_details(table_name="orders", schema_name="public")
        customers = get_table_details(table_name="customers", schema_name="public")
        # Now you have full details for just the tables you need
        ```

    Note:
        - Much more efficient than analyze_schema(lightweight=False) for large schemas
        - Use when you only need details for a subset of tables
        - FK relationships already available from lightweight analysis
    """
    db_manager = get_session_db_manager(ctx)

    try:
        # Analyze single table
        table_info = db_manager.analyze_table(table_name, schema_name)

        if not table_info:
            await ctx.error(f"Table '{table_name}' not found in schema '{schema_name or 'default'}'")
            return {
                "success": False,
                "error": f"Table '{table_name}' not found",
                "table_name": table_name,
                "schema_name": schema_name or "default"
            }

        # Convert to dict format
        table_dict = {
            "success": True,
            "name": table_info.name,
            "schema": table_info.schema,
            "columns": [
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "is_nullable": col.is_nullable,
                    "is_primary_key": col.is_primary_key,
                    "is_foreign_key": col.is_foreign_key,
                    "foreign_key_table": col.foreign_key_table,
                    "foreign_key_column": col.foreign_key_column,
                    "comment": col.comment
                } for col in table_info.columns
            ],
            "primary_keys": table_info.primary_keys,
            "foreign_keys": table_info.foreign_keys,
            "comment": table_info.comment,
            "row_count": table_info.row_count
        }

        await ctx.info(f"Retrieved details for table '{table_name}': {len(table_info.columns)} columns")
        return table_dict

    except Exception as e:
        logger.error(f"Failed to get table details for {table_name}: {e}")
        await ctx.error(f"Failed to analyze table '{table_name}': {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "table_name": table_name,
            "schema_name": schema_name or "default"
        }


@mcp.tool()
async def generate_ontology(
    ctx: Context,
    schema_info: Optional[str] = None,
    schema_name: Optional[str] = None,
    base_uri: str = "http://example.com/ontology/",
    auto_persist: bool = True,
    graph_uri: Optional[str] = None
) -> str:
    """Generate an RDF ontology from database schema. AUTO-ANALYZES schema if needed!

    *** SIMPLIFIED WORKFLOW - Only 2 tools needed! ***

    After connect_database(), just call:
    1. generate_ontology(schema_name="YOUR_SCHEMA") → Auto-analyzes AND generates ontology!
    2. suggest_semantic_names(ontology_file="...") → For enrichment (optional)

    You do NOT need to call analyze_schema separately - this tool does it automatically!

    Args:
        schema_name: Name of the schema to analyze and generate ontology for
        schema_info: Optional pre-analyzed schema JSON (usually not needed)
        base_uri: Base URI for the ontology (default: http://example.com/ontology/)
        auto_persist: If True (default), automatically store in Oxigraph RDF database.
                     If False, return full ontology TTL (legacy behavior, uses more tokens).

                     ⚠️ BREAKING CHANGE (2026-02-27): Default changed from False to True.
                     If your code expects full TTL output, explicitly set auto_persist=False.

        graph_uri: Optional custom graph URI for RDF storage (only used if auto_persist=True)

    Returns:
        If auto_persist=True (default): Success message with stats (saves 23k-94k tokens!)
        If auto_persist=False: Full RDF ontology in Turtle format

    Notes:
        - The ontology file is saved to the configured OUTPUT_DIR (default: tmp/)
        - Use download_ontology() to retrieve the full TTL from RDF store
        - The RDF store persists in OUTPUT_DIR/oxigraph/{connection_id}/store/
    """
    # Check if ontology is already generated - return early with guidance
    session = get_session_data(ctx)
    if session.ontology_file:
        await ctx.info(f"Ontology CACHED - call suggest_semantic_names() for enrichment")
        return (
            f"# STOP! ONTOLOGY ALREADY CACHED!\n\n"
            f"Ontology file: {session.ontology_file}\n\n"
            f"Do NOT call generate_ontology or analyze_schema again!\n\n"
            f"## FOR ENRICHMENT:\n"
            f"Call suggest_semantic_names() NOW - it will use the cached ontology automatically.\n\n"
            f"That's the ONLY tool you need to call for enrichment!"
        )

    # Validate base_uri
    if not base_uri.endswith('/'):
        base_uri += '/'

    tables_info = []

    if schema_info:
        # Use provided schema information
        try:
            import json
            schema_data = json.loads(schema_info) if isinstance(schema_info, str) else schema_info

            # Convert schema data to TableInfo objects (already imported at module level)
            
            if "tables" in schema_data:
                for table_data in schema_data["tables"]:
                    # Convert column data
                    columns = []
                    for col_data in table_data.get("columns", []):
                        column = ColumnInfo(
                            name=col_data["name"],
                            data_type=col_data["data_type"],
                            is_nullable=col_data.get("is_nullable", True),
                            is_primary_key=col_data.get("is_primary_key", False),
                            is_foreign_key=col_data.get("is_foreign_key", False),
                            foreign_key_table=col_data.get("foreign_key_table"),
                            foreign_key_column=col_data.get("foreign_key_column"),
                            comment=col_data.get("comment")
                        )
                        columns.append(column)
                    
                    # Convert table data
                    table = TableInfo(
                        name=table_data["name"],
                        schema=table_data.get("schema", schema_name or "default"),
                        columns=columns,
                        primary_keys=table_data.get("primary_keys", []),
                        foreign_keys=table_data.get("foreign_keys", []),
                        comment=table_data.get("comment"),
                        row_count=table_data.get("row_count")
                    )
                    tables_info.append(table)
                    
            logger.info(f"Using provided schema info: {len(tables_info)} tables")
            
        except Exception as e:
            return create_error_response(
                f"Failed to parse schema_info parameter: {str(e)}",
                "parameter_error"
            )
    else:
        # Try to use cached schema analysis from previous analyze_schema call
        session = get_session_data(ctx)

        # If schema_name not provided, use the last analyzed schema
        effective_schema = schema_name
        if not effective_schema:
            effective_schema = session.get_last_analyzed_schema()
            if effective_schema:
                logger.info(f"Using last analyzed schema: {effective_schema}")

        cached_tables = session.get_cached_schema(effective_schema or "")

        if cached_tables:
            # Update schema_name to the effective one for later use
            schema_name = effective_schema
            tables_info = cached_tables
            logger.info(f"Using CACHED schema from analyze_schema: {len(tables_info)} tables (no re-query needed)")
            await ctx.info(f"Using cached schema: {len(tables_info)} tables - no database queries needed")
        else:
            # Fall back to fetching from database
            # Use effective_schema if schema_name was not provided
            schema_name = effective_schema or schema_name
            db_manager = get_session_db_manager(ctx)

            if not db_manager.has_engine():
                return create_error_response(
                    "No database connection established and no schema_info provided. Please use connect_database tool first or provide schema_info parameter.",
                    "connection_error"
                )

            try:
                tables = db_manager.get_tables(schema_name)
                logger.info(f"Found {len(tables)} tables in schema '{schema_name or 'default'}': {tables}")

                # Prefetch PKs and FKs at schema level (Snowflake optimization)
                if schema_name:
                    db_manager.prefetch_schema_constraints(schema_name)

                for table_name in tables:
                    try:
                        table_info = db_manager.analyze_table(table_name, schema_name)
                        if table_info:
                            tables_info.append(table_info)
                    except Exception as e:
                        logger.error(f"Failed to analyze table {table_name}: {e}")

                # Cache for future use
                session.cache_schema_analysis(schema_name or "", tables_info)

            except Exception as e:
                return create_error_response(
                    f"Failed to get tables from database: {str(e)}",
                    "database_error"
                )

    if not tables_info:
        return create_error_response(
            f"No tables found to generate ontology from",
            "data_error"
        )

    generator = _server_state.get_ontology_generator(base_uri=base_uri)
    ontology_ttl = generator.generate_from_schema(tables_info)

    # Save ontology to output folder
    ontology_filename = None
    try:
        output_dir = get_output_dir()

        schema_safe = (schema_name or "default").replace(" ", "_").replace(".", "_")
        ontology_filename = get_session_safe_filename(ctx, "ontology", schema_safe) + ".ttl"
        ontology_file_path = output_dir / ontology_filename

        with open(ontology_file_path, 'w', encoding='utf-8') as f:
            f.write(ontology_ttl)

        logger.info(f"Generated ontology for schema '{schema_name or 'default'}': {len(tables_info)} tables")
        logger.info(f"Saved ontology to: {ontology_file_path}")

        # Store filename in session state and invalidate OBQC cache
        session = get_session_data(ctx)
        session.ontology_file = ontology_filename
        session.obqc_validator = None  # Invalidate to reload with new ontology

        await ctx.info(f"Ontology generation complete; next call should be suggest_semantic_names to improve cryptic names")

        # Analyze the ontology for cryptic names
        generator = _server_state.get_ontology_generator(base_uri=base_uri)
        generator.graph.parse(data=ontology_ttl, format="turtle")
        # Re-bind namespaces after parsing
        generator.graph.bind("ns", generator.base_uri)
        generator.graph.bind("db", generator.db_ns)
        name_analysis = generator.extract_names_for_review()

        # Analyze the ontology for cryptic names
        cryptic_count = (name_analysis["summary"]["classes_needing_review"] +
                        name_analysis["summary"]["properties_needing_review"] +
                        name_analysis["summary"]["relationships_needing_review"])

        # Auto-persist to RDF store (default behavior - saves tokens!)
        if auto_persist and OXIGRAPH_AVAILABLE:
            try:
                store = get_oxigraph_store(ctx)
                if store:
                    # Generate graph URI if not provided
                    if not graph_uri:
                        schema_safe = (schema_name or "default").replace(" ", "_").replace(".", "_")
                        graph_uri = f"http://example.com/schema/{schema_safe}"

                    # Store in Oxigraph
                    triple_count = store.load_ontology(ontology_ttl, graph_uri, schema_name or "default")

                    logger.info(f"Auto-persisted ontology to Oxigraph: {triple_count} triples in graph <{graph_uri}>")

                    # Return concise success message (massive token savings!)
                    output_dir = get_output_dir()
                    result = f"""✅ Ontology generated and stored successfully!

Schema: {schema_name or "default"}
Tables: {len(tables_info)}
Ontology file: {ontology_filename}
Storage location: {output_dir}/
Graph URI: <{graph_uri}>
Triples stored: {triple_count:,}

💾 Ontology is now persistent in Oxigraph RDF database.
📊 Use query_sparql() to explore the schema graph.
📥 Use download_ontology(schema_name="{schema_name or "default"}") to get the TTL file.

Token savings: ~{len(ontology_ttl)//4} tokens saved by auto-persisting to RDF store!"""

                    # Add semantic name suggestions if needed
                    if cryptic_count > 0:
                        result += f"""

⚠️ SEMANTIC NAME RESOLUTION RECOMMENDED
Found {cryptic_count} names that may need review:
  • Classes needing review: {name_analysis['summary']['classes_needing_review']}
  • Properties needing review: {name_analysis['summary']['properties_needing_review']}
  • Relationships needing review: {name_analysis['summary']['relationships_needing_review']}

To improve ontology for business users:
1. Call suggest_semantic_names() (ontology is CACHED)
2. Review suggestions and provide alternatives
3. Call apply_semantic_names() with your suggestions"""

                    return result

            except Exception as e:
                logger.warning(f"Auto-persist to Oxigraph failed: {e}, falling back to full TTL return")
                # Fall through to legacy behavior

        # Legacy behavior: return full ontology TTL (uses more tokens)
        result = ontology_ttl
        result += f"\n\n# Ontology file: {ontology_filename}"

        # Add semantic name resolution guidance if cryptic names detected
        if cryptic_count > 0:
            result += f"\n\n# ⚠️ SEMANTIC NAME RESOLUTION RECOMMENDED"
            result += f"\n# Found {cryptic_count} names that may be abbreviations or cryptic identifiers."
            result += f"\n# To improve ontology readability for business users:"
            result += f"\n# 1. Call suggest_semantic_names() - NO parameters needed, ontology is CACHED"
            result += f"\n# 2. Review the suggestions and provide business-friendly alternatives"
            result += f"\n# 3. Call apply_semantic_names() with your suggestions"
            result += f"\n#"
            result += f"\n# IMPORTANT: Do NOT call analyze_schema or generate_ontology again!"
            result += f"\n# The ontology is CACHED in session. Just call suggest_semantic_names() directly."
            result += f"\n#"
            result += f"\n# Analysis summary:"
            result += f"\n#   - Classes needing review: {name_analysis['summary']['classes_needing_review']}"
            result += f"\n#   - Properties needing review: {name_analysis['summary']['properties_needing_review']}"
            result += f"\n#   - Relationships needing review: {name_analysis['summary']['relationships_needing_review']}"

        return result

    except Exception as e:
        logger.warning(f"Failed to save ontology to file: {e}")
        await ctx.info(f"Ontology file save failed but ontology generated; next call should be suggest_semantic_names to improve cryptic names")
        # Still return the ontology even if file save failed
        return ontology_ttl


@mcp.tool()
async def suggest_semantic_names(
    ctx: Context,
    ontology_file: Optional[str] = None
) -> Dict[str, Any]:
    """Extract and analyze names from a generated ontology to identify abbreviations and cryptic names.

    *** FOR ENRICHMENT: Just call this tool with ontology_file from generate_ontology response! ***

    Args:
        ontology_file: The ontology filename from generate_ontology response (e.g., "ontology_TPCDS.ttl").
                      If provided, loads from this file. If not provided, uses session cache.

    ## ENRICHMENT WORKFLOW

    For ontology enrichment, you only need TWO tools:
    ```
    1. suggest_semantic_names(ontology_file="filename.ttl") → Pass the file from generate_ontology
    2. apply_semantic_names(suggestions, ontology_file="filename.ttl") → Apply your improvements
    ```

    Do NOT call analyze_schema or generate_ontology again - just pass the ontology_file!

    ## NAME ANALYSIS

    The tool automatically detects:
    - **Abbreviations**: Short names like 'cust', 'ord', 'amt', 'qty'
    - **Cryptic suffixes**: '_id', '_dt', '_cd', '_no', '_nm', '_flg'
    - **Technical prefixes**: 'pk_', 'fk_', 'tbl_', 'vw_'
    - **All-caps acronyms**: 'SKU', 'UPC', 'EAN'
    - **Numeric suffixes**: Names ending in numbers

    ## OUTPUT FORMAT

    Returns a dictionary with:
    - classes: List of table/class names with analysis
    - properties: List of column/property names with analysis
    - relationships: List of foreign key relationships with analysis
    - analysis_hints: Summary of detected issues
    - llm_prompt: Instructions for generating name suggestions

    No database connection is required - the ontology is automatically
    loaded from the session context (set by generate_ontology).

    Returns:
        Dictionary containing extracted names, analysis results, and instructions
        for providing semantic name suggestions.

    Example Response Format for LLM to provide suggestions:
    ```json
    {
        "classes": [
            {"original_name": "cust_mstr", "suggested_name": "Customer", "description": "Master record for customer entities"}
        ],
        "properties": [
            {"original_name": "ord_dt", "table_name": "orders", "suggested_name": "Order Date", "description": "Date when order was placed"}
        ],
        "relationships": [
            {"original_name": "orders_has_customers", "suggested_name": "Placed By Customer", "description": "Links order to the customer who placed it"}
        ]
    }
    ```
    """
    try:
        # Load ontology - prefer provided file, fall back to session cache
        try:
            if ontology_file:
                # Use provided ontology file
                output_dir = get_output_dir()
                ontology_path = output_dir / ontology_file
                if not ontology_path.exists():
                    return {
                        "error": f"Ontology file not found: {ontology_file}",
                        "error_type": "file_not_found",
                        "hint": "Check the filename from generate_ontology response"
                    }
                generator = OntologyGenerator()
                generator.load_from_file(str(ontology_path))
                source_filename = ontology_file
                logger.info(f"Loaded ontology from provided file: {ontology_file}")
            else:
                # Fall back to session cache
                generator, source_filename = load_ontology_from_session(ctx)
        except ValueError as e:
            return {
                "error": str(e),
                "error_type": "session_error",
                "hint": "Pass ontology_file parameter from generate_ontology response"
            }

        # Extract names for review
        extraction_result = generator.extract_names_for_review()

        # Add LLM prompt instructions
        extraction_result["llm_instructions"] = {
            "task": "Review the extracted names and provide business-friendly alternatives",
            "focus_on": [
                "Names marked with 'needs_review.is_cryptic: true'",
                "Abbreviations that should be expanded",
                "Technical names that need business context"
            ],
            "response_format": {
                "classes": [
                    {"original_name": "string", "suggested_name": "string", "description": "string"}
                ],
                "properties": [
                    {"original_name": "string", "table_name": "string", "suggested_name": "string", "description": "string"}
                ],
                "relationships": [
                    {"original_name": "string", "suggested_name": "string", "description": "string"}
                ]
            },
            "guidelines": [
                "Use clear, business-oriented terminology",
                "Expand abbreviations to full words (e.g., 'cust' → 'Customer')",
                "Use Title Case for class names",
                "Use descriptive phrases for properties",
                "Provide meaningful descriptions that explain business context",
                "Keep the original db:tableName and db:columnName for SQL generation"
            ]
        }

        # Add next step guidance
        extraction_result["next_step"] = "Review the names above and call apply_semantic_names with your suggestions"
        extraction_result["next_tool"] = "apply_semantic_names"

        await ctx.info(f"Extracted {extraction_result['summary']['total_classes']} classes, {extraction_result['summary']['total_properties']} properties for review; next call should be apply_semantic_names with your suggestions")

        return extraction_result

    except Exception as e:
        logger.error(f"Error extracting names for review: {e}")
        return {
            "error": f"Failed to extract names: {str(e)}",
            "error_type": "internal_error"
        }


@mcp.tool()
async def apply_semantic_names(
    ctx: Context,
    suggestions: str,
    ontology_file: Optional[str] = None,
    save_to_file: bool = True
) -> str:
    """Apply LLM-suggested semantic names to an existing ontology.

    Args:
        suggestions: JSON string containing name suggestions (see format below)
        ontology_file: The ontology filename from generate_ontology response (e.g., "ontology_TPCDS.ttl").
                      If provided, loads from this file. If not provided, uses session cache.
        save_to_file: Whether to save the updated ontology to a file (default: True)

    ## ENRICHMENT WORKFLOW

    Pass the ontology_file from generate_ontology:
    ```
    1. suggest_semantic_names(ontology_file="filename.ttl") → Get names to review
    2. apply_semantic_names(suggestions, ontology_file="filename.ttl") → Apply improvements
    ```

    Do NOT call analyze_schema or generate_ontology again!

    ## WHAT GETS UPDATED

    For each suggestion:
    - Update rdfs:label to the business-friendly name
    - Add db:semanticName annotation
    - Add rdfs:comment with the description
    - Preserve original db:tableName/db:columnName for SQL generation

    Args:
        suggestions: JSON string containing name suggestions in the format:
            {
                "classes": [{"original_name": "...", "suggested_name": "...", "description": "..."}],
                "properties": [{"original_name": "...", "table_name": "...", "suggested_name": "...", "description": "..."}],
                "relationships": [{"original_name": "...", "suggested_name": "...", "description": "..."}]
            }
        save_to_file: Whether to save the updated ontology to a file (default: True)

    Returns:
        Updated ontology in Turtle format with semantic names applied.

    Example suggestions format:
    ```json
    {
        "classes": [
            {"original_name": "cust_mstr", "suggested_name": "Customer Master", "description": "Central repository of customer information"},
            {"original_name": "ord_hdr", "suggested_name": "Order Header", "description": "Main order record with summary information"}
        ],
        "properties": [
            {"original_name": "cust_id", "table_name": "cust_mstr", "suggested_name": "Customer ID", "description": "Unique identifier for customers"},
            {"original_name": "ord_dt", "table_name": "ord_hdr", "suggested_name": "Order Date", "description": "Date when the order was placed"}
        ],
        "relationships": [
            {"original_name": "ord_hdr_has_cust_mstr", "suggested_name": "Placed By", "description": "Links an order to the customer who placed it"}
        ]
    }
    ```
    """
    try:
        import json

        # Load ontology - prefer provided file, fall back to session cache
        try:
            if ontology_file:
                # Use provided ontology file
                output_dir = get_output_dir()
                ontology_path = output_dir / ontology_file
                if not ontology_path.exists():
                    return create_error_response(
                        f"Ontology file not found: {ontology_file}",
                        "file_not_found"
                    )
                generator = OntologyGenerator()
                generator.load_from_file(str(ontology_path))
                source_filename = ontology_file
                logger.info(f"Loaded ontology from provided file: {ontology_file}")
            else:
                # Fall back to session cache
                generator, source_filename = load_ontology_from_session(ctx)
        except ValueError as e:
            return create_error_response(
                f"{str(e)} - pass ontology_file parameter from generate_ontology response",
                "session_error"
            )

        # Parse suggestions
        try:
            if isinstance(suggestions, str):
                name_suggestions = json.loads(suggestions)
            else:
                name_suggestions = suggestions
        except json.JSONDecodeError as e:
            return create_error_response(
                f"Invalid JSON in suggestions parameter: {str(e)}",
                "parameter_error",
                "Ensure suggestions is valid JSON with classes, properties, and relationships arrays"
            )

        # Validate structure
        if not isinstance(name_suggestions, dict):
            return create_error_response(
                "Suggestions must be a JSON object with 'classes', 'properties', and/or 'relationships' arrays",
                "parameter_error"
            )

        # Apply the semantic names
        updated_ontology = generator.apply_semantic_names(name_suggestions)

        # Save to file if requested
        new_ontology_filename = None
        if save_to_file:
            try:
                output_dir = get_output_dir()

                new_ontology_filename = get_session_safe_filename(ctx, "ontology", "semantic") + ".ttl"
                ontology_file_path = output_dir / new_ontology_filename

                with open(ontology_file_path, 'w', encoding='utf-8') as f:
                    f.write(updated_ontology)

                logger.info(f"Saved semantic ontology to: {ontology_file_path}")

                # Update session state with new filename and invalidate OBQC cache
                session = get_session_data(ctx)
                session.ontology_file = new_ontology_filename
                session.obqc_validator = None  # Invalidate to reload with new ontology

            except Exception as e:
                logger.warning(f"Failed to save ontology to file: {e}")

        # Count changes made
        classes_updated = len(name_suggestions.get("classes", []))
        properties_updated = len(name_suggestions.get("properties", []))
        relationships_updated = len(name_suggestions.get("relationships", []))
        total_updated = classes_updated + properties_updated + relationships_updated

        await ctx.info(f"Applied {total_updated} semantic name changes to ontology")

        result = f"# Semantic Names Applied Successfully\n\n"
        result += f"- Classes updated: {classes_updated}\n"
        result += f"- Properties updated: {properties_updated}\n"
        result += f"- Relationships updated: {relationships_updated}\n"
        if new_ontology_filename:
            result += f"\n## ontology_file: {new_ontology_filename}\n"
            result += f"\nThe ontology file '{new_ontology_filename}' has been saved and is now the active ontology in session context.\n"

        result += f"\n{updated_ontology}"

        return result

    except Exception as e:
        logger.error(f"Error applying semantic names: {e}")
        return create_error_response(
            f"Failed to apply semantic names: {str(e)}",
            "internal_error"
        )


@mcp.tool()
async def load_my_ontology(
    ctx: Context,
    import_folder: str = "./import",
    auto_persist: bool = True,
    graph_uri: Optional[str] = None
) -> Dict[str, Any]:
    """Load the newest .ttl ontology file from the import folder.

    This tool allows you to load a custom ontology file instead of generating one
    from the database schema. The loaded ontology can be used for SQL generation
    and validation.

    ## PURPOSE

    Use this tool when you have a pre-existing ontology that should be used
    instead of auto-generating one from the database schema. This is useful for:
    - Using manually curated ontologies with business-friendly naming
    - Loading previously generated and refined ontologies
    - Working with ontologies that have been enhanced externally
    - Reusing ontologies across multiple sessions

    ## BEHAVIOR

    The tool will:
    1. Scan the specified import folder for .ttl (Turtle) files
    2. Select the newest file based on modification time
    3. Parse and validate the ontology
    4. If auto_persist=True (default): Store in Oxigraph RDF database
    5. If auto_persist=False: Store in session state for LLM context
    6. Return information about the loaded ontology

    ## FILE FORMAT

    The ontology file must be in Turtle (.ttl) format and should follow
    the RDF/OWL structure with:
    - owl:Class definitions for tables
    - owl:DatatypeProperty definitions for columns
    - owl:ObjectProperty definitions for relationships
    - db: namespace annotations for SQL mapping

    Args:
        import_folder: Path to the folder containing .ttl files (default: "./import")
                      Can be relative or absolute path.
        auto_persist: If True (default), store in Oxigraph RDF database (saves tokens).
                     If False, store in session state for LLM context (legacy behavior).
        graph_uri: Optional custom graph URI for RDF storage (only used if auto_persist=True)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if ontology was loaded
        - file_path: Path to the loaded file
        - file_name: Name of the loaded file
        - file_size: Size of the file in bytes
        - modified_time: Last modification time of the file
        - classes_count: Number of OWL classes found
        - properties_count: Number of properties found
        - relationships_count: Number of object properties found
        - stored_in_rdf: Boolean indicating if stored in Oxigraph
        - (optional) ontology_preview: First 2000 characters (only if auto_persist=False)
        - next_steps: Guidance for what to do next

    Example Usage:
        # Load ontology and store in RDF (recommended)
        load_my_ontology()

        # Load from custom folder
        load_my_ontology(import_folder="/path/to/my/ontologies")

        # Load without auto-persist (legacy)
        load_my_ontology(auto_persist=False)

    After Loading:
        If auto_persist=True: Ontology is queryable via SPARQL
        If auto_persist=False: Ontology is in session state for SQL generation
    """
    try:
        import glob
        from rdflib import Graph
        from rdflib.namespace import RDF, OWL

        # Resolve the import folder path
        if import_folder.startswith("./"):
            # Relative path - resolve from project root
            project_root = Path(__file__).parent.parent
            folder_path = project_root / import_folder[2:]
        elif not os.path.isabs(import_folder):
            # Relative path without ./
            project_root = Path(__file__).parent.parent
            folder_path = project_root / import_folder
        else:
            folder_path = Path(import_folder)

        # Check if folder exists
        if not folder_path.exists():
            return {
                "success": False,
                "error": f"Import folder not found: {folder_path}",
                "error_type": "folder_not_found",
                "suggestion": "Create the folder and add .ttl files, or specify a different path"
            }

        # Find all .ttl files in the folder
        ttl_files = list(folder_path.glob("*.ttl"))

        if not ttl_files:
            return {
                "success": False,
                "error": f"No .ttl files found in: {folder_path}",
                "error_type": "no_files_found",
                "suggestion": "Add .ttl ontology files to the import folder"
            }

        # Sort by modification time (newest first)
        ttl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        newest_file = ttl_files[0]

        # Read the file content
        with open(newest_file, 'r', encoding='utf-8') as f:
            ontology_content = f.read()

        # Parse and validate the ontology
        graph = Graph()
        try:
            graph.parse(data=ontology_content, format="turtle")
        except Exception as parse_error:
            return {
                "success": False,
                "error": f"Failed to parse ontology file: {str(parse_error)}",
                "error_type": "parse_error",
                "file_path": str(newest_file),
                "suggestion": "Ensure the file is valid Turtle format"
            }

        # Count ontology elements
        classes_count = len(list(graph.subjects(RDF.type, OWL.Class)))
        datatype_props = len(list(graph.subjects(RDF.type, OWL.DatatypeProperty)))
        object_props = len(list(graph.subjects(RDF.type, OWL.ObjectProperty)))

        # Get file stats
        file_stat = newest_file.stat()
        modified_time = datetime.fromtimestamp(file_stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

        # Store the loaded ontology in session state
        session = get_session_data(ctx)
        session.loaded_ontology = ontology_content
        session.loaded_ontology_path = str(newest_file)
        session.obqc_validator = None  # Invalidate to reload with new ontology

        logger.info(f"Loaded ontology from: {newest_file}")
        logger.info(f"Ontology contains: {classes_count} classes, {datatype_props} data properties, {object_props} object properties")

        # Auto-persist to RDF store (default behavior - saves tokens!)
        stored_in_rdf = False
        triple_count = 0
        used_graph_uri = None

        if auto_persist and OXIGRAPH_AVAILABLE:
            try:
                store = get_oxigraph_store(ctx)
                if store:
                    # Extract schema name from filename (e.g., "ontology_public.ttl" -> "public")
                    schema_name = newest_file.stem.replace("ontology_", "")
                    if not graph_uri:
                        graph_uri = f"http://example.com/schema/{schema_name}"
                    used_graph_uri = graph_uri

                    # Store in Oxigraph
                    triple_count = store.load_ontology(ontology_content, graph_uri, schema_name)
                    stored_in_rdf = True

                    logger.info(f"Auto-persisted ontology to Oxigraph: {triple_count} triples in graph <{graph_uri}>")
                    await ctx.info(f"Ontology loaded and stored in RDF database with {triple_count:,} triples; ready for SPARQL queries")
                else:
                    logger.warning("Oxigraph store not available for auto-persist")
                    await ctx.info(f"Ontology loaded with {classes_count} classes; ready for SQL generation")
            except Exception as e:
                logger.warning(f"Auto-persist to Oxigraph failed: {e}, ontology still available in session state")
                await ctx.info(f"Ontology loaded with {classes_count} classes; ready for SQL generation")
        else:
            await ctx.info(f"Ontology loaded with {classes_count} classes; ready for SQL generation")

        # Prepare response
        response = {
            "success": True,
            "file_path": str(newest_file),
            "file_name": newest_file.name,
            "file_size": file_stat.st_size,
            "modified_time": modified_time,
            "classes_count": classes_count,
            "properties_count": datatype_props,
            "relationships_count": object_props,
            "total_files_found": len(ttl_files),
            "other_files": [f.name for f in ttl_files[1:5]] if len(ttl_files) > 1 else [],
            "stored_in_rdf": stored_in_rdf,
        }

        if stored_in_rdf:
            response["graph_uri"] = used_graph_uri
            response["triples_stored"] = triple_count
            response["next_steps"] = {
                "recommended": "query_sparql",
                "reason": "The loaded ontology is now in Oxigraph RDF database",
                "workflow": [
                    "1. ✅ load_my_ontology (completed)",
                    "2. ➡️  query_sparql (explore schema with SPARQL)",
                    "3. ➡️  execute_sql_query (use ontology context for SQL generation)"
                ]
            }
            response["note"] = f"Ontology stored in RDF database with {triple_count:,} triples. Token savings: ~{len(ontology_content)//4} tokens!"
        else:
            # Legacy mode: include preview
            preview = ontology_content[:2000]
            if len(ontology_content) > 2000:
                preview += "\n\n... [truncated, full content available in file]"
            response["ontology_preview"] = preview
            response["next_steps"] = {
                "recommended": "execute_sql_query",
                "reason": "The loaded ontology provides semantic context for SQL generation",
                "workflow": [
                    "1. ✅ load_my_ontology (completed)",
                    "2. ➡️  connect_database (if not already connected)",
                    "3. ➡️  execute_sql_query (use ontology context for accurate SQL)"
                ]
            }
            response["note"] = "This ontology is now active and will be used instead of auto-generated ontologies"

        return response

    except Exception as e:
        logger.error(f"Error loading ontology: {e}")
        return {
            "success": False,
            "error": f"Failed to load ontology: {str(e)}",
            "error_type": "internal_error"
        }


@mcp.tool()
async def download_ontology(
    ctx: Context,
    schema_name: Optional[str] = None,
    source: str = "rdf"
) -> Dict[str, Any]:
    """Download ontology as TTL file from RDF store or tmp folder.

    This tool allows you to download the ontology in Turtle format, even though
    it's stored in the Oxigraph RDF database. Useful for:
    - Backing up ontologies
    - Sharing ontologies with other tools
    - Importing into external RDF systems
    - Version control
    - Offline analysis

    Args:
        schema_name: Name of the schema (e.g., "public", "TPCDS").
                    If not provided, uses the last analyzed/generated schema.
        source: Where to get the ontology from:
               - "rdf" (default): Export from Oxigraph RDF store
               - "file": Read from tmp folder (uses cached .ttl file)

    Returns:
        Dictionary containing:
        - success: Boolean indicating success
        - content: Full ontology in Turtle format
        - file_path: Path where it's saved in tmp folder
        - file_name: Name of the file
        - file_size: Size in bytes
        - triple_count: Number of triples (if from RDF)
        - source: Where it was retrieved from

    Example Usage:
        # Download from RDF store (recommended)
        download_ontology(schema_name="public")

        # Download from cached file
        download_ontology(schema_name="public", source="file")

        # Download last generated ontology
        download_ontology()

    After Downloading:
        You can:
        - Save the content to a file
        - Import into another RDF system
        - Share with colleagues
        - Version control with git
    """
    try:
        session = get_session_data(ctx)

        # Determine schema name
        if not schema_name:
            # Try to get from session
            schema_name = session.get_last_analyzed_schema()
            if not schema_name:
                return {
                    "success": False,
                    "error": "No schema_name provided and no schema in session",
                    "error_type": "parameter_error",
                    "hint": "Provide schema_name parameter or generate/load an ontology first"
                }

        schema_safe = schema_name.replace(" ", "_").replace(".", "_")
        output_dir = get_output_dir()

        if source == "rdf" and OXIGRAPH_AVAILABLE:
            # Export from Oxigraph RDF store
            store = get_oxigraph_store(ctx)
            if not store:
                return {
                    "success": False,
                    "error": "Oxigraph RDF store not initialized",
                    "error_type": "rdf_error",
                    "hint": "Call store_ontology_in_rdf first or use source='file'"
                }

            graph_uri = f"http://example.com/schema/{schema_safe}"

            try:
                # Export the named graph
                ontology_ttl = store.export_graph(graph_uri, format="turtle")

                if not ontology_ttl or len(ontology_ttl) < 100:
                    return {
                        "success": False,
                        "error": f"Graph <{graph_uri}> is empty or not found in RDF store",
                        "error_type": "rdf_error",
                        "hint": f"Call store_ontology_in_rdf(schema_name='{schema_name}') first"
                    }

                # Save to file
                file_name = f"ontology_{schema_safe}_export.ttl"
                file_path = output_dir / file_name

                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(ontology_ttl)

                # Count triples (approximate from lines)
                triple_count = len([line for line in ontology_ttl.split('\n') if line.strip() and not line.strip().startswith('#') and not line.strip().startswith('@')])

                logger.info(f"Exported ontology from RDF store <{graph_uri}> to {file_path}")

                return {
                    "success": True,
                    "content": ontology_ttl,
                    "file_path": str(file_path),
                    "file_name": file_name,
                    "file_size": len(ontology_ttl),
                    "triple_count": triple_count,
                    "graph_uri": graph_uri,
                    "source": "rdf",
                    "note": f"Ontology exported from Oxigraph RDF store. File saved to: {file_path}"
                }

            except Exception as e:
                logger.error(f"Failed to export from RDF store: {e}")
                return {
                    "success": False,
                    "error": f"Failed to export from RDF store: {str(e)}",
                    "error_type": "rdf_error",
                    "hint": "Try source='file' to read from tmp folder instead"
                }

        elif source == "file":
            # Read from tmp folder
            # Try to find the ontology file
            ontology_filename = session.ontology_file
            if not ontology_filename:
                # Look for files matching pattern
                pattern = f"ontology_{schema_safe}*.ttl"
                matching_files = list(output_dir.glob(pattern))
                if matching_files:
                    # Use the newest
                    matching_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                    ontology_file_path = matching_files[0]
                else:
                    return {
                        "success": False,
                        "error": f"No ontology file found for schema '{schema_name}' in tmp folder",
                        "error_type": "file_not_found",
                        "hint": "Generate ontology first with generate_ontology()"
                    }
            else:
                ontology_file_path = output_dir / ontology_filename

            if not ontology_file_path.exists():
                return {
                    "success": False,
                    "error": f"Ontology file not found: {ontology_file_path}",
                    "error_type": "file_not_found"
                }

            # Read the file
            with open(ontology_file_path, 'r', encoding='utf-8') as f:
                ontology_ttl = f.read()

            file_stat = ontology_file_path.stat()

            logger.info(f"Read ontology from file: {ontology_file_path}")

            return {
                "success": True,
                "content": ontology_ttl,
                "file_path": str(ontology_file_path),
                "file_name": ontology_file_path.name,
                "file_size": file_stat.st_size,
                "source": "file",
                "note": f"Ontology read from tmp folder: {ontology_file_path}"
            }

        else:
            return {
                "success": False,
                "error": f"Invalid source: {source}. Must be 'rdf' or 'file'",
                "error_type": "parameter_error"
            }

    except Exception as e:
        logger.error(f"Error downloading ontology: {e}")
        return {
            "success": False,
            "error": f"Failed to download ontology: {str(e)}",
            "error_type": "internal_error"
        }


@mcp.tool()
async def download_r2rml(
    ctx: Context,
    schema_name: Optional[str] = None
) -> Dict[str, Any]:
    """Download R2RML mapping file from tmp folder.

    R2RML (RDB to RDF Mapping Language) files are automatically generated by
    analyze_schema() and define how database tables/columns map to RDF ontology.
    This tool allows you to download the R2RML file for:
    - Backing up R2RML mappings
    - Sharing with other RDF tools (D2RQ, Ontop, etc.)
    - Importing into RDF triple stores
    - Version control
    - Documentation

    ## What is R2RML?

    R2RML is a W3C standard language for mapping relational databases to RDF.
    It defines how to:
    - Map database tables to RDF classes
    - Map database columns to RDF properties
    - Handle foreign key relationships
    - Generate RDF URIs from database values

    ## When is R2RML Generated?

    R2RML files are automatically created when you call:
    - analyze_schema() - Generates R2RML for the analyzed schema

    Args:
        schema_name: Name of the schema (e.g., "public", "TPCDS").
                    If not provided, uses the last analyzed schema.

    Returns:
        Dictionary containing:
        - success: Boolean indicating success
        - content: Full R2RML mapping in Turtle format
        - file_path: Path where it's saved in tmp folder
        - file_name: Name of the file
        - file_size: Size in bytes
        - base_iri: Base IRI used for RDF URI generation

    Example Usage:
        # Download R2RML for specific schema
        download_r2rml(schema_name="public")

        # Download last analyzed schema's R2RML
        download_r2rml()

    After Downloading:
        You can:
        - Use with D2RQ Server for virtual RDF views
        - Import into Ontop for SPARQL-to-SQL translation
        - Generate RDF dumps with R2RML processors
        - Share mappings with data integration teams
    """
    try:
        session = get_session_data(ctx)

        # Determine schema name
        if not schema_name:
            # Try to get from session
            schema_name = session.get_last_analyzed_schema()
            if not schema_name:
                return {
                    "success": False,
                    "error": "No schema_name provided and no schema in session",
                    "error_type": "parameter_error",
                    "hint": "Provide schema_name parameter or run analyze_schema() first"
                }

        schema_safe = schema_name.replace(" ", "_").replace(".", "_")
        output_dir = get_output_dir()

        # Try to find the R2RML file
        r2rml_filename = session.r2rml_file
        if not r2rml_filename:
            # Look for files matching pattern
            pattern = f"r2rml_{schema_safe}*.ttl"
            matching_files = list(output_dir.glob(pattern))
            if matching_files:
                # Use the newest
                matching_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                r2rml_file_path = matching_files[0]
            else:
                return {
                    "success": False,
                    "error": f"No R2RML file found for schema '{schema_name}' in tmp folder",
                    "error_type": "file_not_found",
                    "hint": "Run analyze_schema() first to generate R2RML mapping"
                }
        else:
            r2rml_file_path = output_dir / r2rml_filename

        if not r2rml_file_path.exists():
            return {
                "success": False,
                "error": f"R2RML file not found: {r2rml_file_path}",
                "error_type": "file_not_found",
                "hint": "Run analyze_schema() to generate R2RML mapping"
            }

        # Read the file
        with open(r2rml_file_path, 'r', encoding='utf-8') as f:
            r2rml_content = f.read()

        file_stat = r2rml_file_path.stat()

        # Extract base IRI from content (it's in the rr:baseIRI triple)
        base_iri = "http://example.com/r2rml/"  # default
        if "rr:baseIRI" in r2rml_content:
            import re
            match = re.search(r'rr:baseIRI\s+"([^"]+)"', r2rml_content)
            if match:
                base_iri = match.group(1)

        logger.info(f"Read R2RML mapping from file: {r2rml_file_path}")

        return {
            "success": True,
            "content": r2rml_content,
            "file_path": str(r2rml_file_path),
            "file_name": r2rml_file_path.name,
            "file_size": file_stat.st_size,
            "base_iri": base_iri,
            "schema_name": schema_name,
            "note": f"R2RML mapping read from tmp folder: {r2rml_file_path}",
            "usage_examples": [
                "Use with D2RQ Server: d2r-server r2rml_mapping.ttl",
                "Use with Ontop: ontop materialize -m r2rml_mapping.ttl",
                "Convert to RDF: r2rml r2rml_mapping.ttl > data.ttl"
            ]
        }

    except Exception as e:
        logger.error(f"Error downloading R2RML: {e}")
        return {
            "success": False,
            "error": f"Failed to download R2RML: {str(e)}",
            "error_type": "internal_error"
        }


@mcp.tool()
async def sample_table_data(
    ctx: Context,
    table_name: str,
    schema_name: Optional[str] = None,
    limit: int = 10
) -> List[Dict[str, Any]]:
    """Sample data from a specific table for analysis.
    
    Args:
        table_name: Name of the table to sample
        schema_name: Schema containing the table (optional)
        limit: Maximum number of rows to return (default: 10, max: 100)
    
    Returns:
        List of sample rows as dictionaries or error response
    """
    # Validate parameters
    if not table_name:
        return [{"error": "Table name is required"}]
    
    if limit <= 0 or limit > 100:
        limit = 10

    db_manager = get_session_db_manager(ctx)
    sample_data = db_manager.sample_table_data(table_name, schema_name, limit)

    if sample_data and len(sample_data) > 0:
        await ctx.info(f"Sample data retrieved with {len(sample_data)} rows; explore data or continue with other analysis")
    else:
        await ctx.info("No sample data found for table")

    return sample_data



@mcp.tool()
async def validate_sql_syntax(ctx: Context, sql_query: str) -> Dict[str, Any]:
    """Validate SQL syntax, security, and fan-trap risks before execution.

    Args:
        sql_query: SQL SELECT statement to validate

    Returns:
        Dict with:
            - is_valid (bool): Whether query passes validation
            - warnings (list): Potential issues (fan-traps, missing schema qualifiers)
            - suggestions (list): Optimization recommendations
            - security_analysis: Injection risk assessment

    Validation Checks:
        - SQL syntax correctness
        - Identifier qualification (schema.table.column)
        - Fan-trap detection in multi-table JOINs
        - Security pattern validation (injection prevention)
        - Performance analysis (LIMIT recommendations)
        - OBQC ontology-based query correctness (if ontology available)

    Example:
        ```python
        validation = validate_sql_syntax(
            "SELECT public.customers.name, SUM(public.orders.total) FROM public.customers JOIN public.orders ON public.customers.id = public.orders.customer_id GROUP BY public.customers.name"
        )
        if validation['is_valid']:
            result = execute_sql_query(...)
        ```

    Note: OBQC validation requires generate_ontology() to be called first.
    """
    try:
        db_manager = get_session_db_manager(ctx)
        
        if not db_manager.has_engine():
            return {
                "is_valid": False,
                "error": "No database connection established. Cannot perform full validation without schema information.",
                "error_type": "connection_error",
                "suggestions": [
                    "Use connect_database tool first to enable comprehensive validation",
                    "Basic syntax validation can still be performed, but schema validation requires a connection"
                ],
                "warnings": ["Schema-level validation disabled without database connection"],
                "database_dialect": "unknown"
            }
        
        # Validate SQL query is not empty
        if not sql_query or not sql_query.strip():
            return {
                "is_valid": False,
                "error": "SQL query cannot be empty.",
                "error_type": "parameter_error",
                "suggestions": ["Provide a valid SELECT statement or schema introspection query"],
                "database_dialect": "unknown"
            }
        
        # Perform validation through database manager
        validation_result = db_manager.validate_sql_syntax(sql_query.strip())

        # Ensure warnings and suggestions lists exist
        if "warnings" not in validation_result:
            validation_result["warnings"] = []
        if "suggestions" not in validation_result:
            validation_result["suggestions"] = []

        # Perform OBQC (Ontology-Based Query Check) validation if ontology is available
        obqc_validator = get_session_obqc_validator(ctx)
        if obqc_validator:
            # Determine dialect from connection info
            db_type = db_manager.connection_info.get("type", "postgresql")
            obqc_result = obqc_validator.validate(sql_query.strip(), dialect=db_type)

            # Merge OBQC results into validation_result
            validation_result.update(obqc_result.to_dict())

            # If OBQC found errors, mark overall validation as failed
            if not obqc_result.is_valid:
                validation_result["is_valid"] = False
                if not validation_result.get("error"):
                    validation_result["error"] = "OBQC validation failed - see obqc_issues for details"
                validation_result["error_type"] = validation_result.get("error_type") or "obqc_error"

            # Add OBQC warnings and suggestions to existing lists
            for issue in obqc_result.issues:
                if issue.severity.value == "warning":
                    msg = f"[OBQC] {issue.message}"
                    if issue.suggestion:
                        msg += f" - {issue.suggestion}"
                    validation_result["warnings"].append(msg)
                elif issue.severity.value == "error" and issue.suggestion:
                    validation_result["suggestions"].append(f"[OBQC] {issue.suggestion}")

            # Add fan-trap warning if detected
            if obqc_result.fan_trap_risk:
                validation_result["warnings"].append(
                    "[OBQC] FAN-TRAP RISK: Query aggregates across multiple 1:many relationships"
                )
                validation_result["suggestions"].append(
                    "Consider UNION ALL pattern: aggregate each fact table separately, then combine"
                )

            logger.debug(f"OBQC validation: valid={obqc_result.is_valid}, issues={len(obqc_result.issues)}")
        else:
            # No ontology loaded - add informational message
            validation_result["obqc_valid"] = None
            validation_result["obqc_issues"] = []
            validation_result["warnings"].append(
                "OBQC validation skipped - no ontology loaded. "
                "Use generate_ontology or load_my_ontology for semantic validation."
            )

        # Log validation results
        if validation_result.get('is_valid'):
            logger.info(f"SQL validation successful: {sql_query[:100]}{'...' if len(sql_query) > 100 else ''}")
            validation_result["next_tool"] = "execute_sql_query"
            await ctx.info("SQL validation passed; next call should be execute_sql_query")
        else:
            logger.info(f"SQL validation failed: {validation_result.get('error', 'Unknown validation error')}")
            await ctx.info("SQL validation failed; fix the query and try validate_sql_syntax again")

        return validation_result
        
    except Exception as e:
        logger.error(f"SQL validation error: {e}")
        return {
            "is_valid": False,
            "error": f"Validation system error: {str(e)}",
            "error_type": "internal_error",
            "suggestions": [
                "Check if the database connection is stable",
                "Verify the SQL query contains valid UTF-8 characters",
                "Try breaking down complex queries into smaller parts"
            ],
            "database_dialect": "unknown"
        }


def _extract_query_intent(sql: str) -> str:
    """Extract natural language intent from SQL query for context retrieval.

    Phase 2 helper function: Parses SQL to generate a query intent string
    that can be used with graphrag_query_context() for automatic context injection.

    Args:
        sql: SQL query string

    Returns:
        Natural language description of query intent

    Examples:
        "SELECT * FROM customers" → "query customers"
        "SELECT SUM(amount) FROM orders" → "aggregate SUM from orders"
        "SELECT c.name, SUM(o.amount) FROM customers c JOIN orders o" →
            "aggregate SUM from customers, orders"
    """
    import re

    # Normalize whitespace
    sql = ' '.join(sql.split())

    # Extract table names (FROM and JOIN clauses)
    tables = []
    # FROM clause
    from_matches = re.findall(r'FROM\s+(?:[\w.]+\.)?(\w+)', sql, re.IGNORECASE)
    tables.extend(from_matches)
    # JOIN clauses
    join_matches = re.findall(r'JOIN\s+(?:[\w.]+\.)?(\w+)', sql, re.IGNORECASE)
    tables.extend(join_matches)

    # Remove duplicates while preserving order
    seen = set()
    unique_tables = []
    for t in tables:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique_tables.append(t)

    # Extract aggregation functions
    aggs = re.findall(r'\b(SUM|AVG|COUNT|MAX|MIN)\s*\(', sql, re.IGNORECASE)
    aggs = list(set([a.upper() for a in aggs]))  # Deduplicate and uppercase

    # Extract WHERE conditions (simple keyword extraction)
    where_match = re.search(r'WHERE\s+(.+?)(?:GROUP BY|ORDER BY|LIMIT|$)', sql, re.IGNORECASE)
    conditions = []
    if where_match:
        where_clause = where_match.group(1)
        # Extract column names from WHERE
        cond_cols = re.findall(r'\b(\w+)\s*(?:=|>|<|LIKE|IN)', where_clause, re.IGNORECASE)
        conditions = list(set(cond_cols[:3]))  # Limit to 3 most important

    # Build intent string
    if aggs and unique_tables:
        intent = f"aggregate {', '.join(aggs)} from {', '.join(unique_tables)}"
    elif unique_tables:
        intent = f"query {', '.join(unique_tables)}"
    else:
        intent = "database query"

    # Add filter context if present
    if conditions:
        intent += f" filtered by {', '.join(conditions)}"

    return intent


@mcp.tool()
async def execute_sql_query(
    ctx: Context,
    sql_query: str,
    limit: int = 1000,
    checklist_completed: bool = False,
    query_intent: Optional[str] = None
) -> Dict[str, Any]:
    """Execute SQL query with validation and fan-trap protection.

    **CRITICAL:** Always fully qualify identifiers as `schema.table.column` to avoid ambiguity.

    Args:
        sql_query: SQL SELECT statement (fully qualified identifiers required)
        limit: Maximum rows to return (default: 1000, max: 10,000)
        checklist_completed: Confirmation that pre-execution checklist is complete
        query_intent: (Optional) Natural language description of what the query is trying to achieve.
                     Example: "Find total sales by customer" or "Show top 10 products by revenue"
                     If provided, this will be used to retrieve relevant schema context from GraphRAG.
                     If not provided, intent will be extracted from SQL (less accurate).

    Returns:
        Dict with: data (list of dicts), columns (list), row_count, execution_time_ms

    Security:
        - Read-only queries only (no DML/DDL)
        - SQL injection prevention via pattern validation
        - Query timeout protection
        - Automatic result size limiting

    Fan-Trap Prevention:
        For multi-fact queries (parent with multiple 1:many relationships):
        - Use /fan-trap-prevention skill for patterns and solutions
        - Recommended: UNION ALL approach or separate aggregation CTEs
        - Always review foreign_keys from analyze_schema() first

    Best Practices:
        - Use /sql-best-practices for identifier qualification examples
        - Call validate_sql_syntax() before execution
        - Start with small LIMIT values for testing
        - Verify results against source tables
        - **RECOMMENDED:** Provide query_intent for better context retrieval

    Phase 2 Enhancement - Smart Context Injection:
        If GraphRAG is initialized, this tool automatically retrieves relevant schema context
        to enhance validation. Provide query_intent for best results, or it will be extracted
        from SQL (less accurate).

    Examples:
        ```python
        # RECOMMENDED: With explicit query intent
        result = execute_sql_query(
            sql_query='''
                SELECT
                    public.customers.customer_id,
                    public.customers.name,
                    COUNT(public.orders.order_id) as order_count
                FROM public.customers
                LEFT JOIN public.orders
                    ON public.customers.customer_id = public.orders.customer_id
                GROUP BY public.customers.customer_id, public.customers.name
                LIMIT 100
            ''',
            limit=100,
            query_intent="Show total number of orders per customer"
        )

        # Basic: Without query intent (will auto-extract from SQL)
        result = execute_sql_query(
            sql_query="SELECT * FROM public.customers WHERE id = 1",
            limit=10
        )
        ```

    Note: Fan-traps cause silent data corruption! Always validate multi-table aggregations.
    """
    try:
        # Handle string "True"/"False" from LLMs that send strings instead of booleans
        if isinstance(checklist_completed, str):
            checklist_completed = checklist_completed.lower() in ('true', '1', 'yes')

        db_manager = get_session_db_manager(ctx)

        if not db_manager.has_engine():
            return create_error_response(
                "No database connection established. Please use connect_database tool first to establish a connection to PostgreSQL, Snowflake, or Dremio.",
                "connection_error",
                "Available connection methods: connect_database('postgresql'), connect_database('snowflake'), connect_database('dremio')"
            )

        # Validate limit parameter
        if limit <= 0 or limit > 10000:
            return create_error_response(
                f"Invalid limit value '{limit}'. Must be between 1 and 10000.",
                "parameter_error",
                "Use a reasonable limit to prevent memory exhaustion while allowing comprehensive analysis."
            )
        
        # Validate SQL query is not empty
        if not sql_query or not sql_query.strip():
            return create_error_response(
                "SQL query cannot be empty.",
                "parameter_error",
                "Provide a valid SELECT statement or schema introspection query."
            )

        # Validate checklist_completed parameter
        if not checklist_completed:
            return create_error_response(
                "ERROR: PRE-EXECUTION CHECKLIST NOT COMPLETED.\nSee tool description for required steps.",
                "validation_error",
                "You must complete the pre-execution checklist before executing SQL queries. Review the tool documentation for required steps."
            )

        # PHASE 2: Auto-inject GraphRAG context if available
        session = get_session_data(ctx)
        if session.graphrag_initialized and session.graphrag_manager:
            try:
                # Use provided query_intent if available, otherwise extract from SQL
                if query_intent:
                    logger.info(f"📊 Using provided query intent: '{query_intent}'")
                    intent_to_use = query_intent
                else:
                    # Fallback: Extract query intent from SQL (less accurate)
                    intent_to_use = _extract_query_intent(sql_query)
                    logger.info(f"📊 Auto-extracted intent from SQL: '{intent_to_use}'")

                # Retrieve relevant context using GraphRAG
                context = session.graphrag_manager.get_query_context(
                    query=intent_to_use,
                    max_tables=3,
                    max_columns=15
                )

                # Log context retrieval
                if context and 'relevant_tables' in context:
                    table_count = len(context['relevant_tables'])
                    logger.info(f"✅ Auto-retrieved context: {table_count} relevant tables")
                    # Context is now available for enhanced validation
                    # (In future, could enhance validation logic here)

            except Exception as e:
                # Don't fail query if context retrieval fails
                logger.debug(f"Context auto-retrieval failed (non-critical): {e}")

        # Execute the query through the database manager
        result = db_manager.execute_sql_query(sql_query.strip(), limit)

        # Log execution results
        if result.get('success'):
            logger.info(f"SQL query executed successfully: {result.get('row_count', 0)} rows returned in {result.get('execution_time_ms', 0)}ms")
            row_count = result.get('row_count', 0)
            if row_count > 0:
                result["next_tool"] = "generate_chart"
                await ctx.info(f"SQL query executed successfully with {row_count} rows; next call should be generate_chart for visualization")
            else:
                await ctx.info("SQL query executed successfully but returned no rows")
        else:
            logger.warning(f"SQL query execution failed: {result.get('error', 'Unknown error')}")
            await ctx.info("SQL query execution failed; review error and try again")

        return result
        
    except Exception as e:
        logger.error(f"Critical error in SQL execution: {e}")
        return create_error_response(
            f"Internal server error during SQL execution: {str(e)}",
            "internal_error",
            "This may indicate a system-level issue. Please check server logs and try again."
        )


@mcp.tool()
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
    output_format: str = "image"
) -> Union[List[UIResource], Image]:
    """Generate interactive or static charts from query results.

    Args:
        data_source: List of dicts from execute_sql_query()['data']
        chart_type: 'bar', 'line', 'scatter', or 'heatmap'
        x_column: Column name for X-axis
        y_column: Column name(s) for Y-axis (str or list of str for multi-line)
        color_column: Optional column for grouping/coloring
        chart_style: 'default', 'stacked', or 'grouped' (for bar charts)
        title: Chart title (recommended)

    Returns:
        Interactive chart via MCP Apps or PNG file path

    Chart Types:
        - bar: Compare categories (use color_column for stacked/grouped)
        - line: Show trends (supports multi-measure with list y_column)
        - scatter: Show relationships (use color_column for categories)
        - heatmap: Show correlations (auto-generates from numeric columns)

    Examples:
        See /chart-examples skill for comprehensive guide with 9 examples

    Quick Example:
        ```python
        result = execute_sql_query("SELECT public.orders.category, SUM(public.orders.sales) as total FROM public.orders GROUP BY public.orders.category")
        generate_chart(result['data'], 'bar', 'category', 'total', title='Sales by Category')
        ```

    Output:
        - Interactive mode: JSON data for MCP Apps rendering
        - Image mode: PNG saved to tmp/ directory

    Note: Data should be pre-aggregated in SQL for best performance.
    """
    # Import the implementation from tools module
    from .tools.chart import generate_chart as generate_chart_impl
    import json
    import numpy as np

    # Custom JSON encoder to handle numpy arrays from Plotly
    class NumpyEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, (np.integer, np.floating)):
                return obj.item()
            return super().default(obj)

    # FALLBACK: Parse data_source if it's incorrectly sent as a string
    # (documentation specifies it should be actual JSON, but some LLMs might send strings)
    if data_source and isinstance(data_source, str):
        logger.warning("data_source was sent as string instead of JSON array - attempting to parse")
        try:
            # First try JSON parsing (double quotes)
            parsed = json.loads(data_source)
            if isinstance(parsed, list):
                data_source = parsed
                logger.info("Successfully parsed data_source from JSON string format")
        except (json.JSONDecodeError, ValueError):
            # If JSON fails, try Python literal_eval (single quotes)
            try:
                import ast
                parsed = ast.literal_eval(data_source)
                if isinstance(parsed, list):
                    data_source = parsed
                    logger.info("Successfully parsed data_source from Python literal string format")
            except (ValueError, SyntaxError) as e:
                # Could not parse - return clear error
                await ctx.info("Chart generation failed - data_source format error")
                raise RuntimeError(
                    f"❌ data_source must be valid JSON (array of objects), not a string. "
                    f"Received string: {data_source[:100]}... "
                    f"Expected format: [{{'key': 'value'}}, ...] "
                    f"Parse error: {str(e)}"
                )

    # Parse y_column if it's a JSON string representation of a list
    # This handles cases where MCP passes ["col1", "col2"] as a string
    if y_column and isinstance(y_column, str):
        try:
            parsed = json.loads(y_column)
            if isinstance(parsed, list):
                y_column = parsed
        except (json.JSONDecodeError, ValueError):
            # Not a JSON string, keep as-is (it's a column name)
            pass

    # Call the implementation with appropriate output format
    logger.info(f"generate_chart called with output_format={output_format}, chart_type={chart_type}")
    result = generate_chart_impl(
        data_source, chart_type, x_column, y_column, color_column,
        title, chart_style, width, height, sort_by, sort_order,
        output_format=output_format
    )
    logger.info(f"generate_chart_impl returned: {type(result)}, has_error={isinstance(result, dict) and 'error' in result}")

    # Check if chart generation failed
    if isinstance(result, dict) and result.get("error"):
        await ctx.info("Chart generation failed")
        raise RuntimeError(result.get("error", "Chart generation failed"))

    # Handle interactive output format (returns UIResource with embedded chart)
    if output_format == "interactive":
        if isinstance(result, dict) and "traces" in result:
            data_points = result.get("metadata", {}).get("data_points", 0)
            chart_id = result.get("metadata", {}).get("chart_id", "chart")

            # Serialize chart data to JSON
            chart_json = json.dumps(result, cls=NumpyEncoder)

            # Create self-contained HTML with embedded Plotly chart
            html_content = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: system-ui, sans-serif; background: #fff; }}
        #chart {{ width: 100%; height: 100vh; min-height: 400px; }}
    </style>
</head>
<body>
    <div id="chart"></div>
    <script>
        const chartData = {chart_json};
        const {{ traces, layout, config }} = chartData;
        const finalConfig = {{
            displayModeBar: true,
            responsive: true,
            scrollZoom: true,
            displaylogo: false,
            modeBarButtonsToRemove: ['lasso2d', 'select2d'],
            ...config
        }};
        const finalLayout = {{
            autosize: true,
            margin: {{ l: 60, r: 60, t: 60, b: 60 }},
            ...layout
        }};
        Plotly.newPlot('chart', traces, finalLayout, finalConfig);
        window.addEventListener('resize', () => Plotly.Plots.resize(document.getElementById('chart')));
    </script>
</body>
</html>'''

            # Return UIResource with embedded chart HTML
            ui_resource = create_ui_resource({
                "uri": f"ui://orionbelt/chart/{chart_id}",
                "content": {
                    "type": "rawHtml",
                    "htmlString": html_content
                },
                "encoding": "text"
            })

            await ctx.info(f"Interactive chart generated: {chart_type} with {data_points} data points")
            return [ui_resource]
        else:
            await ctx.info("Chart generation failed")
            raise RuntimeError("Chart generation failed: unexpected result format for interactive mode")

    # Handle image output format (returns PNG bytes)
    if isinstance(result, tuple) and len(result) == 2:
        image_bytes, chart_id = result

        # Save the image to tmp directory
        from .chart_utils import save_image_to_tmp
        image_file_path = save_image_to_tmp(image_bytes, chart_id, 'png')

        if not image_file_path:
            await ctx.info("Chart generation failed to save file")
            raise RuntimeError("Failed to save chart image to file")

        await ctx.info(f"Chart image generated successfully: {image_file_path}")

        # Return Image object for Claude Desktop display
        return Image(path=str(image_file_path))
    else:
        await ctx.info("Chart generation failed")
        raise RuntimeError("Chart generation failed: unexpected result format")


@mcp.tool()
async def get_server_info(ctx: Context) -> Dict[str, Any]:
    """Get information about the MCP server and its capabilities.

    Returns:
        Dictionary containing server information
    """
    # Import centralized version info
    from . import __version__, __name__ as SERVER_NAME, __description__

    await ctx.info("Server info retrieved; next call should be connect_database to start working")

    return {
        "name": SERVER_NAME,
        "version": __version__,
        "description": __description__,
        "supported_databases": ["postgresql", "snowflake", "dremio"],
        "features": [
            "Database connection management",
            "Schema analysis",
            "Table relationship mapping",
            "RDF/OWL ontology generation",
            "Load custom ontologies from file",
            "Semantic name resolution (abbreviation expansion)",
            "Interactive data visualization (charts)"
        ],
        "tools": [
            "connect_database",
            "list_schemas",
            "analyze_schema",
            "generate_ontology",
            "suggest_semantic_names",
            "apply_semantic_names",
            "load_my_ontology",
            "sample_table_data",
            "validate_sql_syntax",
            "execute_sql_query",
            "generate_chart",
            "get_server_info"
        ],
        "next_tool": "connect_database"
    }


# --- GraphRAG Tools ---

async def _auto_generate_ontology_background(
    schema_name: str,
    tables_info: List[TableInfo],
    session: SessionData,
    ctx: Context
) -> None:
    """Background task: Auto-generate ontology after GraphRAG completes.

    This runs asynchronously after GraphRAG initialization, storing the ontology
    directly in the Oxigraph RDF store for immediate SPARQL access.
    Enabled by AUTO_ONTOLOGY=true environment variable (default: false in Phase 2).

    Args:
        schema_name: Schema name being analyzed
        tables_info: Analyzed table metadata
        session: SessionData instance
        ctx: FastMCP context

    Phase 2 Implementation Notes:
        - Runs after GraphRAG completes
        - Uses auto_persist=True to store in Oxigraph
        - Saves 23k-94k tokens by not returning full ontology
        - Logs progress and errors clearly
        - Gracefully degrades on failure
    """
    import time

    try:
        start_time = time.time()
        logger.info(f"🏗️ Auto-generating ontology for schema '{schema_name}'...")

        # Get config
        config = config_manager.get_server_config()
        base_uri = config.ontology_base_uri

        # Convert TableInfo to dict format for ontology generator
        schema_data = {
            "schema": schema_name,
            "tables": []
        }

        for table_info in tables_info:
            table_dict = {
                "name": table_info.name,
                "schema": table_info.schema,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "nullable": col.nullable,
                        "default": col.default,
                        "comment": col.comment
                    }
                    for col in table_info.columns
                ],
                "primary_keys": table_info.primary_keys,
                "foreign_keys": [
                    {
                        "column": fk.column,
                        "referenced_table": fk.referenced_table,
                        "referenced_column": fk.referenced_column
                    }
                    for fk in table_info.foreign_keys
                ],
                "comment": table_info.comment
            }
            schema_data["tables"].append(table_dict)

        # Generate ontology
        ontology_generator = OntologyGenerator(base_uri=base_uri)
        ontology_ttl = ontology_generator.generate_ontology(schema_data)

        # Save to connection-specific directory to prevent collisions
        output_dir = get_output_dir()
        connection_dir = output_dir / (session.connection_id or "default")
        connection_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ontology_file = connection_dir / f"ontology_{schema_name}_{timestamp}.ttl"
        ontology_file.write_text(ontology_ttl, encoding='utf-8')
        session.ontology_file = f"{session.connection_id}/{ontology_file.name}"

        # Store in Oxigraph RDF store if available
        if OXIGRAPH_AVAILABLE:
            try:
                store = get_oxigraph_store(ctx)
                if store:
                    graph_uri = f"{base_uri}{schema_name}"
                    triple_count = store.load_ontology(ontology_ttl, graph_uri, schema_name)
                    logger.info(f"📦 Stored {triple_count} triples in RDF store (graph: {graph_uri})")
            except Exception as e:
                logger.warning(f"Failed to store in RDF: {e}")

        elapsed = time.time() - start_time
        logger.info(f"✅ Ontology auto-generated successfully ({elapsed:.2f}s)")
        logger.info(f"💾 Saved to: {ontology_file.name}")

    except Exception as e:
        logger.error(f"❌ Ontology auto-generation failed: {type(e).__name__}: {e}")
        logger.debug("Ontology auto-gen traceback:", exc_info=True)
        # Don't fail the main operation - graceful degradation


async def _auto_initialize_graphrag_background(
    schema_name: str,
    tables_info: List[TableInfo],
    session: SessionData,
    ctx: Context
) -> None:
    """Background task: Auto-initialize GraphRAG after schema analysis.

    This runs asynchronously after analyze_schema returns, so it doesn't block the LLM.
    Enabled by AUTO_GRAPHRAG=true environment variable (default: true).

    Args:
        schema_name: Schema name being analyzed
        tables_info: Analyzed table metadata
        session: SessionData instance
        ctx: FastMCP context

    Phase 1 Implementation Notes:
        - Runs in background using asyncio.create_task()
        - Uses TF-IDF embeddings for fast initialization
        - Logs progress and errors clearly
        - Gracefully degrades on failure (doesn't block main operation)
        - Saves state to disk for persistence
    """
    import time

    try:
        start_time = time.time()
        logger.info(f"🧠 Auto-initializing GraphRAG for schema '{schema_name}'...")

        # Initialize GraphRAG manager if needed
        if session.graphrag_manager is None:
            session.graphrag_manager = GraphRAGManager(
                embedding_model="tfidf",  # Fast default for auto-init
                connection_id=session.connection_id,  # CRITICAL: Prevent naming collisions
                schema_name=schema_name  # For ChromaDB collection naming
            )

        # Convert TableInfo to dict format expected by GraphRAG
        tables_dict = []
        for table_info in tables_info:
            table_dict = {
                "name": table_info.name,
                "schema": table_info.schema,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "nullable": col.nullable,
                        "default": col.default,
                        "comment": col.comment
                    }
                    for col in table_info.columns
                ],
                "primary_keys": table_info.primary_keys,
                "foreign_keys": [
                    {
                        "column": fk.column,
                        "referenced_table": fk.referenced_table,
                        "referenced_column": fk.referenced_column
                    }
                    for fk in table_info.foreign_keys
                ],
                "comment": table_info.comment
            }
            tables_dict.append(table_dict)

        # Build vector index and graph
        session.graphrag_manager.initialize_from_schema(
            tables_info=tables_dict,
            schema_name=schema_name
        )

        # Save state to disk for persistence
        output_dir = get_output_dir()
        session.graphrag_manager.save_state(output_dir)

        elapsed = time.time() - start_time
        session.graphrag_initialized = True

        # Log success with statistics
        logger.info(f"✅ GraphRAG auto-initialized successfully ({elapsed:.2f}s)")
        logger.info(f"📊 Indexed {len(tables_dict)} tables with their metadata")

        # PHASE 2: Chain to ontology generation if enabled
        auto_ontology = os.getenv("AUTO_ONTOLOGY", "false").lower()
        if auto_ontology == "true":
            logger.info("🔗 Chaining to ontology auto-generation...")
            await _auto_generate_ontology_background(
                schema_name=schema_name,
                tables_info=tables_info,
                session=session,
                ctx=ctx
            )

    except Exception as e:
        logger.error(f"❌ GraphRAG auto-initialization failed: {type(e).__name__}: {e}")
        logger.debug("GraphRAG auto-init traceback:", exc_info=True)
        # Don't fail the main operation - graceful degradation
        session.graphrag_initialized = False
        # Tools will fall back to basic schema analysis


@mcp.tool()
async def initialize_graphrag(
    ctx: Context,
    schema_name: Optional[str] = None,
    embedding_model: str = "tfidf"
) -> str:
    """Initialize GraphRAG for intelligent schema navigation and retrieval.

    This tool sets up vector embeddings, graph relationships, and community detection
    for semantic search and context-aware query generation.

    Args:
        schema_name: Schema to initialize (uses last analyzed if not specified)
        embedding_model: Embedding type ("tfidf" or "sentence-transformers")

    Returns:
        Initialization status message

    Workflow:
        1. Call analyze_schema() first to get schema metadata
        2. Call initialize_graphrag() to build embeddings and graph
        3. Use graphrag_search() or graphrag_query_context() for retrieval

    Example:
        ```python
        # Step 1: Analyze schema
        schema = analyze_schema(schema_name="public")

        # Step 2: Initialize GraphRAG
        status = initialize_graphrag(schema_name="public")

        # Step 3: Use semantic search
        results = graphrag_search("find customer and order tables")
        ```
    """
    session = get_session_data(ctx)
    db_manager = get_session_db_manager(ctx)

    if not db_manager.has_engine():
        return create_error_response(
            "No database connection. Please use connect_database tool first.",
            "connection_error"
        )

    # Determine schema to use
    effective_schema = schema_name
    if not effective_schema:
        effective_schema = session.get_last_analyzed_schema()
        if effective_schema:
            logger.info(f"Using last analyzed schema: {effective_schema}")

    # Get cached schema or fetch from database
    tables_info = session.get_cached_schema(effective_schema or "")

    if not tables_info:
        # Need to fetch schema
        try:
            tables = db_manager.get_tables(effective_schema)
            logger.info(f"Found {len(tables)} tables in schema '{effective_schema or 'default'}'")

            if effective_schema:
                db_manager.prefetch_schema_constraints(effective_schema)

            tables_info = []
            for table_name in tables:
                try:
                    table_info = db_manager.analyze_table(table_name, effective_schema)
                    if table_info:
                        tables_info.append(table_info)
                except Exception as e:
                    logger.error(f"Failed to analyze table {table_name}: {e}")

            # Cache for future use
            session.cache_schema_analysis(effective_schema or "", tables_info)

        except Exception as e:
            return create_error_response(
                f"Failed to fetch schema: {str(e)}",
                "database_error"
            )

    if not tables_info:
        return create_error_response(
            f"No tables found in schema '{effective_schema or 'default'}'",
            "data_error"
        )

    # Convert TableInfo objects to dictionaries
    tables_dict = []
    for table_info in tables_info:
        table_dict = {
            "name": table_info.name,
            "schema": table_info.schema,
            "columns": [
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "is_nullable": col.is_nullable,
                    "is_primary_key": col.is_primary_key,
                    "is_foreign_key": col.is_foreign_key,
                    "foreign_key_table": col.foreign_key_table,
                    "foreign_key_column": col.foreign_key_column,
                    "comment": col.comment
                } for col in table_info.columns
            ],
            "primary_keys": table_info.primary_keys,
            "foreign_keys": table_info.foreign_keys,
            "comment": table_info.comment,
            "row_count": table_info.row_count
        }
        tables_dict.append(table_dict)

    # Initialize GraphRAG
    try:
        if session.graphrag_manager is None:
            session.graphrag_manager = GraphRAGManager(
                embedding_model=embedding_model,
                embedding_dimension=384,
                connection_id=session.connection_id,  # CRITICAL: Prevent naming collisions
                schema_name=effective_schema or "default"  # For ChromaDB collection naming
            )

        session.graphrag_manager.initialize_from_schema(
            tables_info=tables_dict,
            schema_name=effective_schema or "default"
        )

        session.graphrag_initialized = True

        # Save state to disk
        output_dir = get_output_dir()
        session.graphrag_manager.save_state(output_dir)

        await ctx.info(f"GraphRAG initialized for schema '{effective_schema or 'default'}' with {len(tables_dict)} tables")

        return (
            f"GraphRAG initialized successfully!\n\n"
            f"Schema: {effective_schema or 'default'}\n"
            f"Tables: {len(tables_dict)}\n"
            f"Embedding model: {embedding_model}\n\n"
            f"You can now use:\n"
            f"- graphrag_search() for semantic search\n"
            f"- graphrag_query_context() for optimized query context\n"
            f"- graphrag_find_join_path() for relationship discovery\n"
            f"- graphrag_overview() for schema statistics"
        )

    except Exception as e:
        logger.error(f"GraphRAG initialization failed: {e}", exc_info=True)
        return create_error_response(
            f"GraphRAG initialization failed: {str(e)}",
            "graphrag_error"
        )


@mcp.tool()
async def graphrag_search(
    ctx: Context,
    query: str,
    top_k: int = 5,
    element_type: Optional[str] = None
) -> Dict[str, Any]:
    """Search schema using natural language via GraphRAG semantic search.

    Uses vector embeddings to find tables, columns, or relationships that match
    your natural language description.

    Args:
        query: Natural language search query
        top_k: Number of results to return (default: 5)
        element_type: Filter by type ("table", "column", "relationship", or None for all)

    Returns:
        Dictionary with search results and similarity scores

    Examples:
        ```python
        # Find customer-related tables
        results = graphrag_search("customer information and profiles")

        # Find date columns
        results = graphrag_search("date and timestamp columns", element_type="column")

        # Find foreign key relationships
        results = graphrag_search("order to customer relationships", element_type="relationship")
        ```
    """
    session = get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        return create_error_response(
            "GraphRAG not initialized. Please call initialize_graphrag() first.",
            "graphrag_not_initialized"
        )

    try:
        results = session.graphrag_manager.search_schema(
            query=query,
            top_k=top_k,
            element_type=element_type
        )

        await ctx.info(f"Found {len(results)} results for query: {query}")

        return {
            "success": True,
            "query": query,
            "result_count": len(results),
            "results": results
        }

    except Exception as e:
        logger.error(f"GraphRAG search failed: {e}", exc_info=True)
        return create_error_response(
            f"GraphRAG search failed: {str(e)}",
            "graphrag_error"
        )


@mcp.tool()
async def graphrag_query_context(
    ctx: Context,
    query: str,
    max_tables: int = 5,
    max_columns: int = 20
) -> Dict[str, Any]:
    """Get optimized context for SQL query generation using GraphRAG.

    This is the main RAG retrieval function that returns minimal, relevant schema
    context for your query, dramatically reducing token usage compared to full schema dumps.

    Args:
        query: Natural language description of what you want to query
        max_tables: Maximum tables to include in context (default: 5)
        max_columns: Maximum columns to include in context (default: 20)

    Returns:
        Optimized context with relevant tables, columns, relationships, and warnings

    Token Savings:
        - Full schema: 36k-145k tokens (25-100 tables)
        - GraphRAG context: 1k-5k tokens (only relevant elements)
        - Savings: 85-95% reduction

    Example:
        ```python
        # Get context for customer orders query
        context = graphrag_query_context(
            query="Show me total sales by customer for last month",
            max_tables=5,
            max_columns=15
        )

        # Context includes:
        # - Relevant tables (customers, orders, order_items)
        # - Relevant columns (customer_id, order_date, total_amount)
        # - Join paths between tables
        # - Fan-trap warnings if applicable
        ```
    """
    session = get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        return create_error_response(
            "GraphRAG not initialized. Please call initialize_graphrag() first.",
            "graphrag_not_initialized"
        )

    try:
        context = session.graphrag_manager.get_query_context(
            query=query,
            max_tables=max_tables,
            max_columns=max_columns
        )

        await ctx.info(
            f"Generated context: {len(context['relevant_tables'])} tables, "
            f"{len(context['relevant_columns'])} columns, "
            f"~{context['token_estimate']} tokens"
        )

        return {
            "success": True,
            "query": query,
            "context": context,
            "usage_guidance": (
                "Use this context for SQL generation. "
                "It includes only relevant schema elements, reducing token usage by 85-95%."
            )
        }

    except Exception as e:
        logger.error(f"GraphRAG query context failed: {e}", exc_info=True)
        return create_error_response(
            f"GraphRAG query context failed: {str(e)}",
            "graphrag_error"
        )


@mcp.tool()
async def graphrag_find_join_path(
    ctx: Context,
    from_table: str,
    to_table: str,
    max_hops: int = 3
) -> Dict[str, Any]:
    """Find join path between two tables using GraphRAG graph traversal.

    Discovers the shortest path through foreign key relationships to connect
    two tables, with detailed join specifications.

    Args:
        from_table: Source table name
        to_table: Target table name
        max_hops: Maximum number of joins allowed (default: 3)

    Returns:
        Dictionary with join path specifications

    Example:
        ```python
        # Find how to join customers to order_items
        path = graphrag_find_join_path(
            from_table="customers",
            to_table="order_items",
            max_hops=3
        )

        # Returns:
        # {
        #   "from": "customers",
        #   "to": "order_items",
        #   "hops": 2,
        #   "path": ["customers", "orders", "order_items"],
        #   "joins": [
        #     {"from_table": "customers", "to_table": "orders", "from_column": "customer_id", ...},
        #     {"from_table": "orders", "to_table": "order_items", "from_column": "order_id", ...}
        #   ]
        # }
        ```
    """
    session = get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        return create_error_response(
            "GraphRAG not initialized. Please call initialize_graphrag() first.",
            "graphrag_not_initialized"
        )

    try:
        join_path = session.graphrag_manager.graph_retriever.find_join_path(
            from_table=from_table,
            to_table=to_table,
            max_hops=max_hops
        )

        if join_path is None:
            return {
                "success": False,
                "from": from_table,
                "to": to_table,
                "message": f"No path found between {from_table} and {to_table} within {max_hops} hops"
            }

        # Build path list
        path = [from_table]
        for join in join_path:
            if join["to_table"] not in path:
                path.append(join["to_table"])

        await ctx.info(f"Found {len(join_path)}-hop path from {from_table} to {to_table}")

        return {
            "success": True,
            "from": from_table,
            "to": to_table,
            "hops": len(join_path),
            "path": path,
            "joins": join_path
        }

    except Exception as e:
        logger.error(f"GraphRAG find join path failed: {e}", exc_info=True)
        return create_error_response(
            f"GraphRAG find join path failed: {str(e)}",
            "graphrag_error"
        )


@mcp.tool()
async def graphrag_overview(ctx: Context) -> Dict[str, Any]:
    """Get GraphRAG schema overview with statistics and communities.

    Provides high-level insights about the schema structure, including:
    - Vector store statistics
    - Graph topology (central tables, hubs, reference tables)
    - Detected communities (logical domain groupings)
    - Domain name suggestions

    Returns:
        Dictionary with comprehensive schema statistics

    Example:
        ```python
        overview = graphrag_overview()

        # Returns:
        # {
        #   "schema_name": "public",
        #   "vector_store_stats": {...},
        #   "graph_summary": {
        #     "total_tables": 50,
        #     "top_central_tables": [...],
        #     "top_hub_tables": [...],
        #     "top_reference_tables": [...]
        #   },
        #   "communities": [
        #     {
        #       "community_id": 0,
        #       "table_count": 15,
        #       "tables": ["customers", "orders", ...],
        #       "domain_name": "Sales Domain"
        #     },
        #     ...
        #   ]
        # }
        ```
    """
    session = get_session_data(ctx)

    if not session.graphrag_initialized or session.graphrag_manager is None:
        return create_error_response(
            "GraphRAG not initialized. Please call initialize_graphrag() first.",
            "graphrag_not_initialized"
        )

    try:
        overview = session.graphrag_manager.get_schema_overview()

        await ctx.info(f"Generated schema overview for: {overview['schema_name']}")

        return {
            "success": True,
            "overview": overview
        }

    except Exception as e:
        logger.error(f"GraphRAG overview failed: {e}", exc_info=True)
        return create_error_response(
            f"GraphRAG overview failed: {str(e)}",
            "graphrag_error"
        )


# --- Oxigraph RDF Store & SPARQL Tools ---

def get_oxigraph_store(ctx: Context) -> Optional[OxigraphStoreManager]:
    """
    Get or initialize connection-scoped Oxigraph store for the session.

    IMPORTANT: Now uses connection-scoped stores to prevent RDF data collisions
    between different databases with the same schema name.

    Each database connection gets its own Oxigraph instance at:
    tmp/oxigraph/{connection_id}/store/

    Returns:
        OxigraphStoreManager instance or None if unavailable
    """
    session = get_session_data(ctx)

    if not OXIGRAPH_AVAILABLE:
        logger.warning("pyoxigraph not available - SPARQL features disabled")
        return None

    if session.oxigraph_store is None:
        try:
            # Use connection-scoped store directory
            store_path = get_oxigraph_store_dir(connection_id=session.connection_id)
            session.oxigraph_store = OxigraphStoreManager(store_path=store_path)
            session.oxigraph_initialized = True

            if session.connection_id:
                logger.info(f"Initialized connection-scoped Oxigraph store at: {store_path}")
                logger.info(f"Connection ID: {session.connection_id}")
            else:
                logger.info(f"Initialized Oxigraph store at: {store_path} (legacy mode)")

        except Exception as e:
            logger.error(f"Failed to initialize Oxigraph store: {e}")
            return None

    return session.oxigraph_store


@mcp.tool()
async def store_ontology_in_rdf(
    ctx: Context,
    schema_name: Optional[str] = None,
    graph_uri: Optional[str] = None
) -> str:
    """Store current session ontology in persistent RDF store with SPARQL access.

    Loads the most recent ontology (from generate_ontology) into Oxigraph for
    persistent storage and SPARQL querying.

    Args:
        schema_name: Schema name (uses last analyzed if not specified)
        graph_uri: Named graph URI (auto-generated if not specified)

    Returns:
        Status message with triple count

    Workflow:
        1. analyze_schema() - Analyze database schema
        2. generate_ontology() - Generate RDF/OWL ontology
        3. store_ontology_in_rdf() - Store in Oxigraph (THIS TOOL)
        4. query_sparql() - Query using SPARQL

    Example:
        ```python
        # Generate and store ontology
        analyze_schema(schema_name="public")
        generate_ontology(schema_name="public")
        store_ontology_in_rdf(schema_name="public")

        # Now query with SPARQL
        query_sparql('SELECT ?table WHERE { ?table a db:Table }')
        ```
    """
    if not OXIGRAPH_AVAILABLE:
        return create_error_response(
            "pyoxigraph not installed. Install with: pip install pyoxigraph",
            "dependency_error"
        )

    store = get_oxigraph_store(ctx)
    if store is None:
        return create_error_response(
            "Failed to initialize Oxigraph store",
            "initialization_error"
        )

    session = get_session_data(ctx)

    # Determine schema
    effective_schema = schema_name or session.get_last_analyzed_schema() or "default"

    # Get ontology file
    if not session.ontology_file:
        return create_error_response(
            "No ontology generated. Please call generate_ontology() first.",
            "ontology_not_found"
        )

    # Read ontology TTL file
    try:
        output_dir = get_output_dir()
        ontology_path = output_dir / session.ontology_file

        if not ontology_path.exists():
            return create_error_response(
                f"Ontology file not found: {session.ontology_file}",
                "file_not_found"
            )

        ontology_ttl = ontology_path.read_text(encoding='utf-8')

        # Generate graph URI
        if not graph_uri:
            graph_uri = f"http://example.com/ontology/{effective_schema}"

        # Load into Oxigraph
        triple_count = store.load_ontology(
            ontology_ttl=ontology_ttl,
            graph_uri=graph_uri,
            schema_name=effective_schema
        )

        await ctx.info(f"Stored ontology for schema '{effective_schema}' in RDF store: {triple_count} triples")

        return (
            f"✅ Ontology stored successfully in RDF store!\n\n"
            f"Schema: {effective_schema}\n"
            f"Graph URI: <{graph_uri}>\n"
            f"Triples loaded: {triple_count}\n\n"
            f"You can now query using:\n"
            f"- query_sparql() - Execute SPARQL SELECT queries\n"
            f"- query_sparql_ask() - Execute SPARQL ASK queries\n"
            f"- list_tables_sparql() - List tables via SPARQL\n"
            f"- find_columns_by_type_sparql() - Find columns by data type"
        )

    except Exception as e:
        logger.error(f"Failed to store ontology in RDF: {e}", exc_info=True)
        return create_error_response(
            f"Failed to store ontology: {str(e)}",
            "storage_error"
        )


@mcp.tool()
async def query_sparql(
    ctx: Context,
    sparql_query: str,
    timeout_seconds: int = 30
) -> Dict[str, Any]:
    """Execute SPARQL SELECT query against stored ontologies.

    Queries the persistent RDF store containing your database ontologies.
    Supports full SPARQL 1.1 SELECT queries.

    Args:
        sparql_query: SPARQL SELECT query string
        timeout_seconds: Query timeout (default: 30)

    Returns:
        Query results as list of bindings

    SPARQL Prefixes Available:
        - rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        - rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        - owl: <http://www.w3.org/2002/07/owl#>
        - xsd: <http://www.w3.org/2001/XMLSchema#>
        - db: <http://example.com/db#>

    Example Queries:
        ```sparql
        # List all tables
        PREFIX db: <http://example.com/db#>
        SELECT ?tableName
        WHERE {
            ?table a db:Table .
            ?table db:tableName ?tableName .
        }

        # Find integer columns
        PREFIX db: <http://example.com/db#>
        SELECT ?tableName ?columnName
        WHERE {
            ?column a db:Column .
            ?column db:tableName ?tableName .
            ?column db:columnName ?columnName .
            ?column db:dataType "INTEGER" .
        }

        # Find foreign key relationships
        PREFIX db: <http://example.com/db#>
        SELECT ?fromTable ?toTable ?fromCol ?toCol
        WHERE {
            ?fk a db:ForeignKey .
            ?fk db:fromTable ?fromTable .
            ?fk db:toTable ?toTable .
            ?fk db:fromColumn ?fromCol .
            ?fk db:toColumn ?toCol .
        }
        ```
    """
    if not OXIGRAPH_AVAILABLE:
        return create_error_response(
            "pyoxigraph not installed",
            "dependency_error"
        )

    store = get_oxigraph_store(ctx)
    if store is None:
        return create_error_response(
            "Oxigraph store not initialized",
            "store_not_initialized"
        )

    try:
        results = store.query_sparql(sparql_query, timeout_seconds=timeout_seconds)

        await ctx.info(f"SPARQL query returned {len(results)} results")

        return {
            "success": True,
            "result_count": len(results),
            "results": results,
            "query": sparql_query
        }

    except Exception as e:
        logger.error(f"SPARQL query failed: {e}", exc_info=True)
        return create_error_response(
            f"SPARQL query failed: {str(e)}",
            "query_error"
        )


@mcp.tool()
async def query_sparql_ask(
    ctx: Context,
    sparql_query: str
) -> Dict[str, Any]:
    """Execute SPARQL ASK query (returns true/false).

    Args:
        sparql_query: SPARQL ASK query

    Returns:
        Boolean result

    Example:
        ```sparql
        # Check if any integer columns exist
        PREFIX db: <http://example.com/db#>
        ASK {
            ?column a db:Column .
            ?column db:dataType "INTEGER" .
        }
        ```
    """
    if not OXIGRAPH_AVAILABLE:
        return create_error_response(
            "pyoxigraph not installed",
            "dependency_error"
        )

    store = get_oxigraph_store(ctx)
    if store is None:
        return create_error_response(
            "Oxigraph store not initialized",
            "store_not_initialized"
        )

    try:
        result = store.query_sparql_ask(sparql_query)

        return {
            "success": True,
            "result": result,
            "query": sparql_query
        }

    except Exception as e:
        logger.error(f"SPARQL ASK query failed: {e}", exc_info=True)
        return create_error_response(
            f"SPARQL ASK query failed: {str(e)}",
            "query_error"
        )


@mcp.tool()
async def add_rdf_knowledge(
    ctx: Context,
    subject: str,
    predicate: str,
    object: str,
    metadata: Optional[Dict[str, Any]] = None
) -> str:
    """Add custom knowledge/metadata to the RDF store.

    Allows you to document learned patterns, business rules, or semantic mappings.

    Args:
        subject: Subject URI
        predicate: Predicate URI
        object: Object value (literal or URI)
        metadata: Optional metadata dict (added as additional triples)

    Returns:
        Confirmation message

    Example:
        ```python
        # Document a learned query pattern
        add_rdf_knowledge(
            subject="http://example.com/pattern/sales_by_customer",
            predicate="http://example.com/schema#hasSQL",
            object="SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id",
            metadata={
                "learned_from": "user_query",
                "timestamp": "2026-02-26T16:00:00Z",
                "confidence": 0.95
            }
        )

        # Document a business rule
        add_rdf_knowledge(
            subject="http://example.com/rule/order_validation",
            predicate="http://www.w3.org/2000/01/rdf-schema#comment",
            object="Orders must have a valid customer_id",
            metadata={"priority": "high"}
        )
        ```
    """
    if not OXIGRAPH_AVAILABLE:
        return create_error_response(
            "pyoxigraph not installed",
            "dependency_error"
        )

    store = get_oxigraph_store(ctx)
    if store is None:
        return create_error_response(
            "Oxigraph store not initialized",
            "store_not_initialized"
        )

    try:
        store.add_knowledge(
            subject=subject,
            predicate=predicate,
            object=object,
            metadata=metadata
        )

        await ctx.info(f"Added knowledge triple: <{subject}> <{predicate}> {object}")

        return f"✅ Knowledge added successfully!\n\nSubject: <{subject}>\nPredicate: <{predicate}>\nObject: {object}"

    except Exception as e:
        logger.error(f"Failed to add knowledge: {e}", exc_info=True)
        return create_error_response(
            f"Failed to add knowledge: {str(e)}",
            "add_error"
        )


@mcp.tool()
async def list_tables_sparql(
    ctx: Context,
    schema_graph: Optional[str] = None
) -> Dict[str, Any]:
    """List all tables from stored ontology using SPARQL.

    Args:
        schema_graph: Optional graph URI to query (auto-detected if not specified)

    Returns:
        List of table names

    Example:
        ```python
        # List all tables
        tables = list_tables_sparql()

        # List tables from specific graph
        tables = list_tables_sparql(
            schema_graph="http://example.com/ontology/public"
        )
        ```
    """
    if not OXIGRAPH_AVAILABLE:
        return create_error_response(
            "pyoxigraph not installed",
            "dependency_error"
        )

    store = get_oxigraph_store(ctx)
    if store is None:
        return create_error_response(
            "Oxigraph store not initialized",
            "store_not_initialized"
        )

    try:
        # Auto-detect graph if not specified
        if not schema_graph:
            session = get_session_data(ctx)
            schema_name = session.get_last_analyzed_schema() or "default"
            # Use same graph URI pattern as generate_ontology() and store_ontology_in_rdf()
            schema_graph = f"http://example.com/schema/{schema_name}"

        tables = store.list_tables_sparql(schema_graph)

        await ctx.info(f"Found {len(tables)} tables via SPARQL")

        return {
            "success": True,
            "table_count": len(tables),
            "tables": tables,
            "graph": schema_graph
        }

    except Exception as e:
        logger.error(f"SPARQL table listing failed: {e}", exc_info=True)
        return create_error_response(
            f"Failed to list tables: {str(e)}",
            "query_error"
        )


@mcp.tool()
async def find_columns_by_type_sparql(
    ctx: Context,
    data_type: str,
    schema_graph: Optional[str] = None
) -> Dict[str, Any]:
    """Find columns by data type using SPARQL.

    Args:
        data_type: SQL data type (e.g., "INTEGER", "VARCHAR", "DATE")
        schema_graph: Optional graph URI

    Returns:
        List of matching columns

    Example:
        ```python
        # Find all integer columns
        cols = find_columns_by_type_sparql("INTEGER")

        # Find date columns
        cols = find_columns_by_type_sparql("DATE")
        ```
    """
    if not OXIGRAPH_AVAILABLE:
        return create_error_response(
            "pyoxigraph not installed",
            "dependency_error"
        )

    store = get_oxigraph_store(ctx)
    if store is None:
        return create_error_response(
            "Oxigraph store not initialized",
            "store_not_initialized"
        )

    try:
        columns = store.find_columns_by_type(data_type, schema_graph)

        await ctx.info(f"Found {len(columns)} {data_type} columns via SPARQL")

        return {
            "success": True,
            "data_type": data_type,
            "column_count": len(columns),
            "columns": columns
        }

    except Exception as e:
        logger.error(f"SPARQL column search failed: {e}", exc_info=True)
        return create_error_response(
            f"Failed to find columns: {str(e)}",
            "query_error"
        )


@mcp.tool()
async def get_rdf_store_stats(ctx: Context) -> Dict[str, Any]:
    """Get statistics about the persistent RDF store.

    Returns:
        Store statistics including triple counts, graphs, and loaded ontologies

    Example:
        ```python
        stats = get_rdf_store_stats()
        # Returns:
        # {
        #   "total_triples": 15420,
        #   "named_graphs": 2,
        #   "graphs": ["http://example.com/ontology/public", ...],
        #   "loaded_ontologies": {"public": "http://example.com/ontology/public"}
        # }
        ```
    """
    if not OXIGRAPH_AVAILABLE:
        return create_error_response(
            "pyoxigraph not installed",
            "dependency_error"
        )

    store = get_oxigraph_store(ctx)
    if store is None:
        return create_error_response(
            "Oxigraph store not initialized",
            "store_not_initialized"
        )

    try:
        stats = store.get_ontology_stats()

        return {
            "success": True,
            "stats": stats
        }

    except Exception as e:
        logger.error(f"Failed to get store stats: {e}", exc_info=True)
        return create_error_response(
            f"Failed to get stats: {str(e)}",
            "stats_error"
        )


# --- Cleanup on shutdown ---

def cleanup_server():
    """Clean up server resources."""
    _server_state.cleanup()


# Main execution removed - server should only be started via server.py
# This prevents double startup when main.py is imported
