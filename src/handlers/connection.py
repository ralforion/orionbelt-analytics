"""Database connection and diagnostic handler implementations."""

import logging
import os
from datetime import datetime
from typing import Optional

from fastmcp import Context

from ..constants import SUPPORTED_DB_TYPES
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
    if not db_type or db_type not in SUPPORTED_DB_TYPES:
        return ValidationError(
            f"Invalid database type '{db_type}'. Use one of: {', '.join(SUPPORTED_DB_TYPES)}."
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

        success = db_manager.connect_dremio(
            host=str(host),
            port=int(port),
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

    elif db_type == "bigquery":
        project_id = os.getenv("BIGQUERY_PROJECT_ID")
        dataset = os.getenv("BIGQUERY_DATASET", "")
        credentials_path = os.getenv("BIGQUERY_CREDENTIALS_PATH")
        credentials_json = os.getenv("BIGQUERY_CREDENTIALS_JSON")

        required_params = {"BIGQUERY_PROJECT_ID": project_id}
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                f"Missing required environment variables for BigQuery: {', '.join(missing_params)}. Please check your .env file."
            ).to_response()

        success = db_manager.connect_bigquery(
            project_id=str(project_id),
            dataset=dataset or "",
            credentials_path=credentials_path,
            credentials_json=credentials_json,
        )
        db_name = f"{project_id}/{dataset}" if dataset else project_id

    elif db_type == "duckdb":
        database_path = os.getenv("DUCKDB_DATABASE_PATH", ":memory:")
        motherduck_token = os.getenv("MOTHERDUCK_TOKEN")
        read_only = os.getenv("DUCKDB_READ_ONLY", "false").lower() == "true"

        success = db_manager.connect_duckdb(
            database_path=database_path,
            motherduck_token=motherduck_token,
            read_only=read_only,
        )
        db_name = database_path

    elif db_type == "databricks":
        server_hostname = os.getenv("DATABRICKS_SERVER_HOSTNAME")
        http_path = os.getenv("DATABRICKS_HTTP_PATH")
        access_token = os.getenv("DATABRICKS_ACCESS_TOKEN")
        catalog = os.getenv("DATABRICKS_CATALOG", "hive_metastore")
        schema = os.getenv("DATABRICKS_SCHEMA", "default")

        required_params = {
            "DATABRICKS_SERVER_HOSTNAME": server_hostname,
            "DATABRICKS_HTTP_PATH": http_path,
            "DATABRICKS_ACCESS_TOKEN": access_token,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                f"Missing required environment variables for Databricks: {', '.join(missing_params)}. Please check your .env file."
            ).to_response()

        success = db_manager.connect_databricks(
            server_hostname=str(server_hostname),
            http_path=str(http_path),
            access_token=str(access_token),
            catalog=catalog,
            schema=schema,
        )
        db_name = f"{catalog}.{schema}"

    elif db_type == "mysql":
        host = os.getenv("MYSQL_HOST")
        port = os.getenv("MYSQL_PORT", "3306")
        database = os.getenv("MYSQL_DATABASE")
        username = os.getenv("MYSQL_USERNAME")
        password = os.getenv("MYSQL_PASSWORD")
        charset = os.getenv("MYSQL_CHARSET", "utf8mb4")

        required_params = {
            "MYSQL_HOST": host,
            "MYSQL_DATABASE": database,
            "MYSQL_USERNAME": username,
            "MYSQL_PASSWORD": password,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                f"Missing required environment variables for MySQL: {', '.join(missing_params)}. Please check your .env file."
            ).to_response()

        success = db_manager.connect_mysql(
            host=str(host),
            port=int(port),
            database=str(database),
            username=str(username),
            password=str(password),
            charset=charset,
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
