"""DuckDB/MotherDuck database driver."""

import logging
from typing import Any

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError, SQLAlchemyError
from sqlalchemy.pool import NullPool, Pool, StaticPool

from ..constants import (
    DEFAULT_SAMPLE_LIMIT,
    DUCKDB_SYSTEM_SCHEMAS,
    MAX_SAMPLE_LIMIT,
    MIN_SAMPLE_LIMIT,
)
from ..database_manager import ColumnInfo, TableInfo
from ..result_limits import fetch_bounded
from ..security import (
    SecureCredentialManager,
    SecurityLevel,
    ascii_lower,
    audit_log_security_event,
    identifier_validator,
    table_references,
)
from ..serialization import serialize_rows
from ..sql_build import names_except, plain, sample_rows, table_path
from .base import DatabaseDriver
from .reflection import column_fk_entries, reflect_tables

logger = logging.getLogger(__name__)


def _resolves_in_catalog(
    parts: tuple[str, ...],
    known: set[tuple[str, ...]],
    schemas: set[tuple[str, ...]],
    databases: set[str],
    default_catalog: str,
    default_schema: str,
) -> bool:
    """Whether DuckDB's catalog lookup finds a table or view for *parts*.

    DuckDB's rules with the default search path (temp, the current database,
    then the system catalog's main and pg_catalog):

    - ``name``: the first search-path schema holding it.
    - ``a.name``: if a database ``a`` is attached, catalog ``a`` in its
      search-path schemas, else ``main`` -- and an error if the current
      database also has a schema ``a``. Otherwise schema ``a`` in the
      search-path catalogs listing it, else the current database or ``system``.
    - ``catalog.schema.name``: exactly that.

    Checked against DuckDB itself in tests/test_read_only_sql_gate.py.

    Every name is folded with :func:`ascii_lower`, as DuckDB compares them.

    Args:
        parts: The reference's name parts.
        known: Every (catalog, schema, name) of a table or view.
        schemas: Every (catalog, schema).
        databases: Every attached catalog.
        default_catalog: ``current_database()``.
        default_schema: ``current_schema()``.

    Returns:
        True when the reference resolves to a catalog entry.
    """
    path = [
        ("temp", "main"),
        (default_catalog, default_schema),
        ("system", "main"),
        ("system", "pg_catalog"),
    ]
    *qualifiers, name = parts
    if not qualifiers:
        return any((c, s, name) in known for c, s in path)
    if len(qualifiers) == 1:
        (first,) = qualifiers
        if first in databases:
            holders = [c for c, s in path if s == first] or [default_catalog]
            if any((c, first) in schemas for c in holders):
                return False  # DuckDB refuses it as ambiguous
            in_path = [s for c, s in path if c == first] or ["main"]
            return any((first, s, name) in known for s in in_path)
        catalogs = [c for c, s in path if s == first] or [default_catalog, "system"]
        return any((c, first, name) in known for c in catalogs)
    catalog, schema = qualifiers[-2:]
    return (catalog, schema, name) in known


class DuckDBDriver(DatabaseDriver):
    """DuckDB/MotherDuck database driver.

    Supports both local DuckDB files and MotherDuck cloud databases.

    Connection types:
    - Local file: database_path="/path/to/file.db"
    - In-memory: database_path=":memory:"
    - MotherDuck: database_path="md:database_name", motherduck_token="token"
    """

    db_type = "duckdb"

    def __init__(self, pool_size: int = 5, max_overflow: int = 10):
        self.engine: Engine | None = None
        self.metadata: MetaData | None = None
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._credential_manager = SecureCredentialManager()
        self._database_path: str | None = None
        self._is_motherduck: bool = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, **params: Any) -> bool:
        """Connect to DuckDB or MotherDuck.

        Expected params:
        - database_path: Path to DuckDB file, ":memory:", or "md:database_name"
        - motherduck_token: API token for MotherDuck (optional, for MotherDuck connections)
        - read_only: Whether to open in read-only mode (default: False)
        """
        database_path = params.get("database_path", ":memory:")
        motherduck_token = params.get("motherduck_token")
        read_only = params.get("read_only", False)

        try:
            self._database_path = database_path
            self._is_motherduck = database_path.startswith("md:")

            # Build connection string
            if self._is_motherduck:
                # MotherDuck connection
                if motherduck_token:
                    connection_string = (
                        f"duckdb:///md:?motherduck_token={motherduck_token}"
                    )
                else:
                    # Try to use default MotherDuck token from environment
                    connection_string = "duckdb:///md:"

                database_name = database_path[3:]  # Remove "md:" prefix
                if database_name and not identifier_validator.validate_identifier(
                    database_name
                ):
                    audit_log_security_event(
                        "invalid_identifier_attempt",
                        {"identifier": database_name[:50]},
                        SecurityLevel.MEDIUM,
                    )
                    logger.error(f"Invalid MotherDuck database name: {database_name}")
                    return False
            else:
                # Local DuckDB connection
                if database_path == ":memory:":
                    connection_string = "duckdb:///:memory:"
                else:
                    connection_string = f"duckdb:///{database_path}"
                    if read_only:
                        connection_string += "?read_only=true"

            # DuckDB uses StaticPool for in-memory, NullPool for file-based
            poolclass: type[Pool]
            poolclass = StaticPool if database_path == ":memory:" else NullPool

            # No connect_args: duckdb.connect() accepts only (database,
            # read_only, config), so anything else here is a TypeError at the
            # first connection. A "timeout" was passed until it was found to
            # make every DuckDB connection fail. There is nothing to time out
            # anyway -- DuckDB is in-process, so a connection is a file open,
            # not a network round trip.
            self.engine = create_engine(
                connection_string,
                poolclass=poolclass,
                echo=False,
            )
            self.metadata = MetaData()

            # Initialize encryption
            try:
                if not self._credential_manager._cipher:
                    key_material = f"duckdb:{database_path}"
                    self._credential_manager._initialize_encryption(key_material)
                logger.info(
                    f"DuckDB connection encryption initialized for {database_path}"
                )
            except Exception as e:
                logger.warning(f"Could not initialize credential encryption: {e}")

            # Test connection
            with self.engine.connect() as conn:
                result = conn.execute(text("SELECT 1"))
                result.fetchone()

            db_type_str = "MotherDuck" if self._is_motherduck else "DuckDB"
            logger.info(f"Connected to {db_type_str}: {database_path}")
            return True

        except (SQLAlchemyError, OperationalError, DatabaseError) as e:
            logger.error(
                f"Failed to connect to DuckDB {database_path}: "
                f"{type(e).__name__}: {e}"
            )
            self.engine = None
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error connecting to DuckDB: {type(e).__name__}: {e}"
            )
            self.engine = None
            return False

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def get_schemas(self) -> list[str]:
        """Get schemas in DuckDB database.

        DuckDB uses 'main' as the default schema. Additional schemas can be created
        or attached from other databases.
        """
        try:
            query = text(
                names_except(
                    "duckdb",
                    "schema_name",
                    table_path(plain("information_schema"), plain("schemata")),
                    DUCKDB_SYSTEM_SCHEMAS,
                )
            )
            assert self.engine is not None
            with self.engine.connect() as conn:
                result = conn.execute(query)
                schemas = [row[0] for row in result.fetchall()]
                # Ensure 'main' is included if it exists
                if not schemas:
                    schemas = ["main"]
                return schemas
        except SQLAlchemyError as e:
            logger.error(f"Failed to get DuckDB schemas: {e}")
            return ["main"]  # Return default schema on error

    def get_tables(self, schema_name: str | None = None) -> list[str]:
        """Get tables in a schema."""
        try:
            schema = schema_name or "main"

            assert self.engine is not None
            with self.engine.connect() as conn:
                query = text("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = :schema_name
                    AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                """)
                result = conn.execute(query, {"schema_name": schema})
                return [row[0] for row in result.fetchall()]
        except SQLAlchemyError as e:
            logger.error(f"Failed to get DuckDB tables: {e}")
            return []

    def get_views(self, schema_name: str | None = None) -> dict[str, str | None]:
        """Get views in a schema, with their SQL definitions."""
        # duckdb_views() rather than information_schema.views: it exposes the
        # full CREATE statement in `sql`, where information_schema carries only
        # the SELECT body.
        try:
            schema = schema_name or "main"

            assert self.engine is not None
            with self.engine.connect() as conn:
                query = text("""
                    SELECT view_name, sql
                    FROM duckdb_views()
                    WHERE schema_name = :schema_name
                      AND NOT internal
                    ORDER BY view_name
                """)
                result = conn.execute(query, {"schema_name": schema})
                return {row[0]: row[1] for row in result.fetchall()}
        except SQLAlchemyError as e:
            logger.error(f"Failed to get DuckDB views: {e}")
            return {}

    def analyze_tables(
        self, table_names: list[str], schema_name: str | None = None
    ) -> dict[str, TableInfo]:
        """Reflect the whole batch in one pass, falling back to one at a time.

        Args:
            table_names: Tables to analyze.
            schema_name: Schema they live in, or None for the default.

        Returns:
            The metadata by table name, without the tables that could not be
            read.
        """
        if not table_names or self.engine is None:
            return super().analyze_tables(table_names, schema_name)
        try:
            return reflect_tables(
                self.engine, table_names, schema_name, schema_name or "main"
            )
        except Exception as e:
            logger.warning(
                f"Schema-wide reflection failed ({e}); reflecting one table at a time"
            )
            return super().analyze_tables(table_names, schema_name)

    def analyze_table(
        self, table_name: str, schema_name: str | None = None
    ) -> TableInfo | None:
        """Analyze a DuckDB table and return its metadata."""
        try:
            schema = schema_name or "main"

            assert self.engine is not None
            with self.engine.connect():
                inspector = inspect(self.engine)

                # Check if table exists
                if not inspector.has_table(table_name, schema=schema):
                    logger.error(f"Table {schema}.{table_name} not found")
                    return None

                table_columns = inspector.get_columns(table_name, schema=schema)
                table_pk = inspector.get_pk_constraint(table_name, schema=schema)
                table_fks = inspector.get_foreign_keys(table_name, schema=schema)

                primary_keys = (
                    table_pk.get("constrained_columns", []) if table_pk else []
                )
                primary_keys_upper = [pk.upper() for pk in primary_keys]

                logger.info(
                    f"DuckDB table {schema}.{table_name}: PKs={primary_keys}, "
                    f"FKs={len(table_fks)} constraints"
                )

                columns = []
                foreign_keys: list[dict[str, Any]] = []
                for col_info in table_columns:
                    column_name = col_info["name"]
                    is_pk = column_name.upper() in primary_keys_upper

                    fk_entries = column_fk_entries(column_name, table_fks)
                    foreign_keys.extend(fk_entries)
                    # A column in several keys names the first on the column itself.
                    is_fk = bool(fk_entries)
                    fk_table = fk_entries[0]["referenced_table"] if fk_entries else None
                    fk_column = (
                        fk_entries[0]["referenced_column"] if fk_entries else None
                    )

                    columns.append(
                        ColumnInfo(
                            name=column_name,
                            data_type=str(col_info["type"]),
                            is_nullable=col_info.get("nullable", True),
                            is_primary_key=is_pk,
                            is_foreign_key=is_fk,
                            foreign_key_table=fk_table,
                            foreign_key_column=fk_column,
                            comment=col_info.get("comment"),
                        )
                    )

                return TableInfo(
                    name=table_name,
                    schema=schema,
                    columns=columns,
                    primary_keys=primary_keys,
                    foreign_keys=foreign_keys,
                    comment=None,
                    row_count=None,
                    sample_data=None,
                )

        except SQLAlchemyError as e:
            logger.error(f"Failed to analyze DuckDB table {table_name}: {e}")
            return None

    # ------------------------------------------------------------------
    # Query validation & execution
    # ------------------------------------------------------------------

    def _names_outside_catalog(self, conn: Any, sql_query: str) -> list[str]:
        """The table references in *sql_query* that DuckDB's catalog cannot resolve.

        Resolution follows DuckDB's own lookup (see :func:`_resolves_in_catalog`),
        since a reference that misses the catalog is read as a file. A query
        that does not parse resolves nothing, so it is reported whole.
        """
        references = table_references(sql_query, "duckdb")
        if references is None:
            return [sql_query[:80]]
        if not references:
            return []

        def lowered(rows: Any) -> set[tuple[str, ...]]:
            return {tuple(ascii_lower(str(value)) for value in row) for row in rows}

        known = lowered(
            conn.execute(
                text(
                    "SELECT database_name, schema_name, table_name "
                    "FROM duckdb_tables() UNION ALL "
                    "SELECT database_name, schema_name, view_name FROM duckdb_views()"
                )
            ).fetchall()
        )
        schemas = lowered(
            conn.execute(
                text("SELECT database_name, schema_name FROM duckdb_schemas()")
            ).fetchall()
        )
        databases = {
            name
            for (name,) in lowered(
                conn.execute(
                    text("SELECT database_name FROM duckdb_databases()")
                ).fetchall()
            )
        }
        default_catalog, default_schema = (
            ascii_lower(str(value))
            for value in conn.execute(
                text("SELECT current_database(), current_schema()")
            ).one()
        )
        return [
            ".".join(parts)
            for parts in references
            if not _resolves_in_catalog(
                parts, known, schemas, databases, default_catalog, default_schema
            )
        ]

    def validate_sql_syntax(
        self, sql_query: str, validation_result: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate DuckDB SQL syntax using EXPLAIN.

        Every table the query names must first be a table or view in the
        catalog. DuckDB reads a name it cannot resolve as a file instead
        (``FROM data.csv`` reads ./data.csv), and binding the query for
        EXPLAIN would already open it, so this check runs before EXPLAIN.
        """
        try:
            assert self.engine is not None
            with self.engine.connect() as conn:
                unknown = self._names_outside_catalog(conn, sql_query)
                if unknown:
                    validation_result["error"] = (
                        "Not a table or view in this database: " f"{', '.join(unknown)}"
                    )
                    validation_result["error_type"] = "unknown_table"
                    validation_result["suggestions"].append(
                        "Check table names against list_schemas and discover_schema"
                    )
                    return validation_result
                try:
                    # Use EXPLAIN to validate syntax without executing
                    explain_query = f"EXPLAIN {sql_query}"
                    conn.execute(text(explain_query))
                    validation_result["is_valid"] = True
                except Exception as syntax_error:
                    error_msg = str(syntax_error)
                    validation_result["database_error"] = error_msg
                    validation_result["error"] = f"DuckDB syntax error: {error_msg}"
                    validation_result["error_type"] = "syntax_error"

                    if "does not exist" in error_msg.lower():
                        validation_result["suggestions"].append(
                            "Check table/column names - they may not exist in the schema"
                        )
                    elif "syntax error" in error_msg.lower():
                        validation_result["suggestions"].append(
                            "Review DuckDB SQL syntax - check for missing commas, parentheses, or keywords"
                        )
        except Exception as conn_error:
            validation_result["error"] = (
                f"Database connection error during validation: {conn_error}"
            )
            validation_result["error_type"] = "connection_error"

        return validation_result

    def execute_sql_query(self, sql_query: str, limit: int = 1000) -> dict[str, Any]:
        """Execute DuckDB SQL query."""
        import time as time_mod

        result_data: dict[str, Any] = {
            "success": False,
            "data": [],
            "columns": [],
            "row_count": 0,
            "execution_time_ms": None,
            "error": None,
            "error_type": None,
            "warnings": [],
            "query_plan": None,
            "limit_applied": False,
        }

        try:
            start_time = time_mod.time()
            db_type_str = "MOTHERDUCK" if self._is_motherduck else "DUCKDB"

            assert self.engine is not None
            with self.engine.connect() as conn:
                logger.info(f"🔍 {db_type_str} SQL QUERY: {sql_query}")
                result = conn.execute(text(sql_query))

                try:
                    if result.returns_rows:
                        result_data["columns"] = list(result.keys())
                        try:
                            raw_rows, truncated = fetch_bounded(result, limit)
                            result_data["truncated"] = truncated
                        except Exception as fetch_error:
                            logger.error(f"Error fetching results: {fetch_error}")
                            try:
                                result.close()
                            except Exception as close_error:
                                # Swallowed on purpose: a cursor-close failure must never
                                # mask the real error being raised. Logged so it stays
                                # diagnosable instead of vanishing.
                                logger.debug(
                                    f"Ignoring cursor close failure: {close_error}"
                                )
                            raise

                        result_data["data"] = serialize_rows(
                            raw_rows, result_data["columns"]
                        )
                        result_data["row_count"] = len(result_data["data"])
                    else:
                        result_data["row_count"] = getattr(result, "rowcount", 0)
                finally:
                    try:
                        result.close()
                    except Exception as close_error:
                        # Swallowed on purpose: a cursor-close failure must never
                        # mask the real error being raised. Logged so it stays
                        # diagnosable instead of vanishing.
                        logger.debug(f"Ignoring cursor close failure: {close_error}")

                end_time = time_mod.time()
                result_data["execution_time_ms"] = round(
                    (end_time - start_time) * 1000, 2
                )
                result_data["success"] = True
                logger.info(
                    f"DuckDB query executed: {result_data['row_count']} rows "
                    f"in {result_data['execution_time_ms']}ms"
                )

        except SQLAlchemyError as e:
            result_data["error"] = str(e)
            result_data["error_type"] = "execution_error"
            logger.error(f"DuckDB SQL execution failed: {e}")
        except Exception as e:
            result_data["error"] = f"Unexpected execution error: {e!s}"
            result_data["error_type"] = "internal_error"
            logger.error(f"Unexpected DuckDB SQL execution error: {e}")

        return result_data

    def sample_table_data(
        self,
        table_name: str,
        schema_name: str | None = None,
        limit: int = DEFAULT_SAMPLE_LIMIT,
    ) -> list[dict[str, Any]]:
        """Sample data from a DuckDB table."""
        if not isinstance(limit, int) or limit < MIN_SAMPLE_LIMIT:
            limit = DEFAULT_SAMPLE_LIMIT
        elif limit > MAX_SAMPLE_LIMIT:
            limit = MAX_SAMPLE_LIMIT
            logger.warning(f"Sample limit capped at {MAX_SAMPLE_LIMIT}")

        try:
            schema = schema_name or "main"

            assert self.engine is not None
            with self.engine.connect() as conn:
                query_str = sample_rows("duckdb", limit, schema, table_name)
                db_type_str = "MOTHERDUCK" if self._is_motherduck else "DUCKDB"
                logger.info(f"🔍 {db_type_str} SQL QUERY: {query_str} ")
                result = conn.execute(text(query_str))
                columns = list(result.keys())
                return serialize_rows(result.fetchall(), columns)

        except (SQLAlchemyError, ValueError) as e:
            logger.error(
                f"Failed to sample data from DuckDB table {table_name}: {type(e).__name__}: {e}"
            )
            return []
        except Exception as e:
            logger.error(
                f"Unexpected error sampling DuckDB table {table_name}: {type(e).__name__}: {e}"
            )
            return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        """Test DuckDB connection health."""
        if not self.engine:
            return False
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                return True
        except Exception as e:
            logger.warning(f"DuckDB connection health check failed: {e}")
            return False

    def disconnect(self) -> None:
        """Close DuckDB connection."""
        if self.engine:
            self.engine.dispose()
            self.engine = None
            self.metadata = None
            db_type_str = "MotherDuck" if self._is_motherduck else "DuckDB"
            logger.info(f"{db_type_str} connection closed")
