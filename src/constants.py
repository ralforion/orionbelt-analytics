"""Constants for OrionBelt Analytics."""

# Database connection constants
DEFAULT_POSTGRES_PORT = 5432
DEFAULT_SNOWFLAKE_SCHEMA = "PUBLIC"
DEFAULT_DREMIO_PORT = 9047  # Dremio REST API port
DEFAULT_CLICKHOUSE_PORT = 8123  # ClickHouse HTTP protocol port
# Data sampling limits
MIN_SAMPLE_LIMIT = 1
MAX_SAMPLE_LIMIT = 1000
DEFAULT_SAMPLE_LIMIT = 10

# Connection and timeout settings
CONNECTION_TIMEOUT = 30
QUERY_TIMEOUT = 60

# Session idle timeout defaults
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 1800  # 30 minutes
DEFAULT_SESSION_SCAN_INTERVAL_SECONDS = 60

# Ontology generation constants
DEFAULT_BASE_URI = "http://example.com/ontology/"
OBA_NAMESPACE = "https://ralforion.com/ns/oba#"
ONTOLOGY_TITLE = "Database Schema Ontology"
ONTOLOGY_DESCRIPTION = "Ontology generated from database schema"

# R2RML mapping constants
DEFAULT_R2RML_BASE_IRI = "http://mycompany.com/"

# Output directory for generated files (schema, ontology, r2rml, etc.)
DEFAULT_OUTPUT_DIR = "tmp"

# Identifier validation pattern
IDENTIFIER_PATTERN = r'^[a-zA-Z_][a-zA-Z0-9_-]*$'

# Canonical database metadata: maps each supported db_type to its sqlglot
# dialect. This is the single source of truth — add a database here once and
# SUPPORTED_DB_TYPES and OBQC's dialect-aware parsing both pick it up, so the
# "supported list" and the "dialect list" can never drift apart.
DB_SQLGLOT_DIALECTS = {
    "postgresql": "postgres",
    "snowflake": "snowflake",
    "dremio": "trino",  # Dremio uses Trino-compatible syntax
    "clickhouse": "clickhouse",
    "bigquery": "bigquery",
    "duckdb": "duckdb",
    "databricks": "databricks",
    "mysql": "mysql",
}

# Supported database types (order-preserving; derived from the canonical map)
SUPPORTED_DB_TYPES = list(DB_SQLGLOT_DIALECTS)

# System schemas to exclude
POSTGRES_SYSTEM_SCHEMAS = ["information_schema", "pg_catalog", "pg_toast"]
SNOWFLAKE_SYSTEM_SCHEMAS = ["INFORMATION_SCHEMA", "SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA"]
DREMIO_SYSTEM_SCHEMAS = ["INFORMATION_SCHEMA", "sys"]
CLICKHOUSE_SYSTEM_SCHEMAS = ["system", "INFORMATION_SCHEMA", "information_schema"]
BIGQUERY_SYSTEM_SCHEMAS = ["INFORMATION_SCHEMA", "information_schema"]
DUCKDB_SYSTEM_SCHEMAS = ["information_schema", "pg_catalog"]
DATABRICKS_SYSTEM_SCHEMAS = ["information_schema", "default"]
MYSQL_SYSTEM_SCHEMAS = ["information_schema", "mysql", "performance_schema", "sys"]