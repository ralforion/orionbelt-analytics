"""Database connection and diagnostic handler implementations."""

import logging
import os
from datetime import datetime
from typing import Optional

from fastmcp import Context

from ..exceptions import ConnectionError, ValidationError, ParameterError
from ..session import SessionData

logger = logging.getLogger(__name__)


async def connect_database(
    ctx: Context,
    db_type: str,
    get_session_db_manager,
    get_session_data,
    create_error_response,
    _get_connection_fingerprint,
    _clear_session_state,
) -> str:
    """Connect to a database using credentials from environment variables.

    Args:
        ctx: FastMCP context
        db_type: Database type - 'postgresql', 'snowflake', 'dremio', or 'clickhouse'
        get_session_db_manager: Function to get session db manager
        get_session_data: Function to get session data
        create_error_response: Error response helper
        _get_connection_fingerprint: Connection fingerprint function
        _clear_session_state: State clearing function

    Returns:
        Connection status message or error JSON
    """
    # Validate input parameters
    if not db_type or db_type not in ["postgresql", "snowflake", "dremio", "clickhouse"]:
        return ValidationError(
            f"Invalid database type '{db_type}'. Use 'postgresql', 'snowflake', 'dremio', or 'clickhouse'."
        ).to_response()

    db_manager = get_session_db_manager(ctx)
    success = False
    db_name = ""

    if db_type == "postgresql":
        host = os.getenv("POSTGRES_HOST")
        port = os.getenv("POSTGRES_PORT")
        database = os.getenv("POSTGRES_DATABASE")
        username = os.getenv("POSTGRES_USERNAME")
        password = os.getenv("POSTGRES_PASSWORD")

        required_params = {
            "POSTGRES_HOST": host,
            "POSTGRES_PORT": port,
            "POSTGRES_DATABASE": database,
            "POSTGRES_USERNAME": username,
            "POSTGRES_PASSWORD": password,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                f"Missing required environment variables for PostgreSQL: {', '.join(missing_params)}. Please check your .env file."
            ).to_response()

        success = db_manager.connect_postgresql(
            host=str(host),
            port=int(port),
            database=str(database),
            username=str(username),
            password=str(password),
        )
        db_name = database

    elif db_type == "snowflake":
        account = os.getenv("SNOWFLAKE_ACCOUNT")
        username = os.getenv("SNOWFLAKE_USERNAME")
        password = os.getenv("SNOWFLAKE_PASSWORD")
        warehouse = os.getenv("SNOWFLAKE_WAREHOUSE")
        database = os.getenv("SNOWFLAKE_DATABASE")
        schema = os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC")

        required_params = {
            "SNOWFLAKE_ACCOUNT": account,
            "SNOWFLAKE_USERNAME": username,
            "SNOWFLAKE_PASSWORD": password,
            "SNOWFLAKE_WAREHOUSE": warehouse,
            "SNOWFLAKE_DATABASE": database,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                f"Missing required environment variables for Snowflake: {', '.join(missing_params)}. Please check your .env file."
            ).to_response()

        success = db_manager.connect_snowflake(
            account=str(account),
            username=str(username),
            password=str(password),
            warehouse=str(warehouse),
            database=str(database),
            schema=schema,
        )
        db_name = database

    elif db_type == "dremio":
        host = os.getenv("DREMIO_HOST")
        port = os.getenv("DREMIO_PORT")
        username = os.getenv("DREMIO_USERNAME")
        password = os.getenv("DREMIO_PASSWORD")

        required_params = {
            "DREMIO_HOST": host,
            "DREMIO_PORT": port,
            "DREMIO_USERNAME": username,
            "DREMIO_PASSWORD": password,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                f"Missing required environment variables for Dremio: {', '.join(missing_params)}. Please check your .env file."
            ).to_response()

        success = db_manager.connect_postgresql(
            host=str(host),
            port=int(port),
            database="DREMIO",
            username=str(username),
            password=str(password),
        )
        db_name = "DREMIO"

    elif db_type == "clickhouse":
        host = os.getenv("CLICKHOUSE_HOST")
        port = os.getenv("CLICKHOUSE_PORT", "8123")
        database = os.getenv("CLICKHOUSE_DATABASE")
        username = os.getenv("CLICKHOUSE_USERNAME", "default")
        password = os.getenv("CLICKHOUSE_PASSWORD", "")
        protocol = os.getenv("CLICKHOUSE_PROTOCOL", "http")
        secure = os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true"

        required_params = {
            "CLICKHOUSE_HOST": host,
            "CLICKHOUSE_DATABASE": database,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                f"Missing required environment variables for ClickHouse: {', '.join(missing_params)}. Please check your .env file."
            ).to_response()

        success = db_manager.connect_clickhouse(
            host=str(host),
            port=int(port),
            database=str(database),
            username=str(username),
            password=str(password),
            protocol=protocol,
            secure=secure,
        )
        db_name = database

    if success:
        session = get_session_data(ctx)
        new_conn_id = _get_connection_fingerprint(db_manager)

        if session.connection_id and session.connection_id != new_conn_id:
            logger.info(
                f"Connection changed (old: {session.connection_id[:8]}..., new: {new_conn_id[:8]}...)"
            )
            _clear_session_state(session, reason="connection change")
        elif not session.connection_id:
            logger.info(f"Initial connection established: {new_conn_id[:8]}...")

        session.connection_id = new_conn_id
        session.connected_at = datetime.now()
        session.clear_schema_cache()

        await ctx.info(f"Connected to {db_type}: {db_name}")
        return f"Successfully connected to {db_type} database: {db_name}"
    else:
        await ctx.info("Database connection failed; check credentials and try again")
        return ConnectionError(
            f"Failed to connect to {db_type} database: {db_name}"
        ).to_response()


async def list_schemas(ctx: Context, get_session_db_manager):
    """Get a list of available schemas from the connected database.

    Args:
        ctx: FastMCP context
        get_session_db_manager: Function to get session db manager

    Returns:
        List of schema names
    """
    db_manager = get_session_db_manager(ctx)
    schemas = db_manager.get_schemas()
    if schemas:
        await ctx.info(f"Found {len(schemas)} schemas; next call should be analyze_schema")
    else:
        await ctx.info("No schemas found")
    return schemas if schemas else []


async def diagnose_connection_issue(
    ctx: Context,
    db_type: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    username: Optional[str] = None,
    ssl: Optional[bool] = None,
):
    """Diagnose connection issues. Imported from tools/connection.py for now."""
    from ..tools.connection import diagnose_connection_issue as _diagnose
    return _diagnose(db_type, host, port, username, ssl)
