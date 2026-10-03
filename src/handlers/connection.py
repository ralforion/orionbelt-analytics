"""Database connection and diagnostic handler implementations."""

import logging
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

from fastmcp import Context

from ..async_utils import run_db
from ..constants import SUPPORTED_DB_TYPES
from ..database_manager import DatabaseManager
from ..database_registry import (
    DatabaseConfigError,
    configured_databases,
    find_database,
    unnamed_entry,
)
from ..exceptions import ConnectionError, ValidationError
from ..handler_context import HandlerContext
from ..lifecycle.metadata import mutate_workspace_metadata
from ..paths import OUTPUT_DIR
from ..session import ConnectionRuntime, SessionData
from ..utils import notify_client, utc_now
from ..workspace import detect_workspace, format_workspace_summary
from .workspace import _format_restore_summary, _restore_workspace_core

logger = logging.getLogger(__name__)


def _restore_lock(
    services: "HandlerContext", session: Any
) -> AbstractAsyncContextManager[Any]:
    """The connection's writer lock, or a no-op without a registry."""
    if services.server_state is None:
        return nullcontext()
    lock: AbstractAsyncContextManager[Any] = services.server_state.writer_lock(session)
    return lock


async def connect_database(
    ctx: Context,
    db_type: str | None,
    services: "HandlerContext",
    database: str | None = None,
) -> str | dict[str, Any]:
    """Connect to a configured database, by name or by type.

    If a previous workspace exists for this connection, it is automatically
    restored (schema cache, ontology, GraphRAG, RDF store).

    Args:
        ctx: FastMCP context
        db_type: Database type, for a server configured by type; or None.
        services: Request-scoped services.
        database: A named connection from ``OBA_DATABASES`` (or a type's name).
            Without it or a type, the only configured database is used.

    Returns:
        Connection status message or error JSON
    """
    try:
        configured = configured_databases()
    except DatabaseConfigError as e:
        return ValidationError(str(e)).to_response()

    if database:
        found = find_database(database, configured)
        if found is None:
            names = ", ".join(e.name for e in configured) or "none"
            return ValidationError(
                f"No database named '{database}' is configured. "
                f"Configured: {names}. Call list_databases to see what each holds."
            ).to_response()
        entry = found
    elif db_type:
        if db_type not in SUPPORTED_DB_TYPES:
            return ValidationError(
                f"Invalid database type '{db_type}'. "
                f"Use one of: {', '.join(SUPPORTED_DB_TYPES)}."
            ).to_response()
        entry = unnamed_entry(db_type)
    elif len(configured) == 1:
        entry = configured[0]
    else:
        names = ", ".join(e.name for e in configured) or "none"
        return ValidationError(
            "Say which database to connect to: pass database=<name>. "
            f"Configured: {names}. Call list_databases to see what each holds."
        ).to_response()

    db_type = entry.db_type
    env = entry.getenv
    label = f"{entry.name} ({db_type})" if entry.named else db_type
    # Named connections read prefixed variables; say so when one is missing.
    where = (
        f" (for '{entry.name}': {entry.prefix}<VARIABLE>, or the unprefixed one)"
        if entry.named
        else ""
    )

    # A session sharing a connection runtime connects with a fresh manager:
    # reconnecting the shared one in place would swap the engine out from under
    # every other session using it. ServerState.bind_session then decides
    # whether the fresh manager is redundant or replaces a dead one.
    shares_runtime = services.provides("get_session_data") and isinstance(
        getattr(services.get_session_data(ctx), "runtime", None), ConnectionRuntime
    )
    db_manager = (
        DatabaseManager() if shares_runtime else services.get_session_db_manager(ctx)
    )
    success = False
    db_name = ""
    # The schema the connection is configured for, if any; otherwise the
    # database is asked once connected.
    configured_schema: str | None = None

    if db_type == "postgresql":
        host = env("POSTGRES_HOST")
        port = env("POSTGRES_PORT")
        database = env("POSTGRES_DATABASE")
        username = env("POSTGRES_USERNAME")
        password = env("POSTGRES_PASSWORD")
        configured_schema = env("POSTGRES_SCHEMA")

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
                "Missing required environment variables for PostgreSQL: "
                f"{', '.join(missing_params)}. Please check your .env file{where}."
            ).to_response()

        success = await run_db(
            db_manager.connect_postgresql,
            host=str(host),
            port=int(str(port)),
            database=str(database),
            username=str(username),
            password=str(password),
        )
        db_name = str(database)

    elif db_type == "snowflake":
        account = env("SNOWFLAKE_ACCOUNT")
        username = env("SNOWFLAKE_USERNAME")
        password = env("SNOWFLAKE_PASSWORD")
        warehouse = env("SNOWFLAKE_WAREHOUSE")
        database = env("SNOWFLAKE_DATABASE")
        schema = env("SNOWFLAKE_SCHEMA", "PUBLIC")
        configured_schema = schema

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
                "Missing required environment variables for Snowflake: "
                f"{', '.join(missing_params)}. Please check your .env file{where}."
            ).to_response()

        success = await run_db(
            db_manager.connect_snowflake,
            account=str(account),
            username=str(username),
            password=str(password),
            warehouse=str(warehouse),
            database=str(database),
            schema=schema,
        )
        db_name = str(database)

    elif db_type == "dremio":
        # Prefer PAT-based authentication (DREMIO_URI + DREMIO_PAT)
        dremio_uri = env("DREMIO_URI")
        dremio_pat = env("DREMIO_PAT")

        if dremio_uri and dremio_pat:
            success = await run_db(
                db_manager.connect_dremio, uri=dremio_uri, pat=dremio_pat
            )
            db_name = "DREMIO"
        else:
            # Fall back to legacy username/password authentication
            host = env("DREMIO_HOST")
            port = env("DREMIO_PORT")
            username = env("DREMIO_USERNAME")
            password = env("DREMIO_PASSWORD")

            required_params = {
                "DREMIO_HOST": host,
                "DREMIO_PORT": port,
                "DREMIO_USERNAME": username,
                "DREMIO_PASSWORD": password,
            }
            missing_params = [k for k, v in required_params.items() if not v]
            if missing_params:
                return ValidationError(
                    "Missing required environment variables for Dremio: "
                    f"{', '.join(missing_params)}. "
                    f"Please check your .env file{where}. "
                    "For PAT-based auth, set DREMIO_URI and DREMIO_PAT instead."
                ).to_response()

            success = await run_db(
                db_manager.connect_dremio,
                host=str(host),
                port=int(str(port)),
                username=str(username),
                password=str(password),
            )
            db_name = "DREMIO"

    elif db_type == "clickhouse":
        host = env("CLICKHOUSE_HOST")
        port = env("CLICKHOUSE_PORT", "8123")
        database = env("CLICKHOUSE_DATABASE")
        configured_schema = database
        username = env("CLICKHOUSE_USERNAME", "default")
        password = env("CLICKHOUSE_PASSWORD", "")
        protocol = env("CLICKHOUSE_PROTOCOL", "http")
        secure = env("CLICKHOUSE_SECURE", "false").lower() == "true"

        required_params = {
            "CLICKHOUSE_HOST": host,
            "CLICKHOUSE_DATABASE": database,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                "Missing required environment variables for ClickHouse: "
                f"{', '.join(missing_params)}. Please check your .env file{where}."
            ).to_response()

        success = await run_db(
            db_manager.connect_clickhouse,
            host=str(host),
            port=int(port),
            database=str(database),
            username=str(username),
            password=str(password),
            protocol=protocol,
            secure=secure,
        )
        db_name = str(database)

    elif db_type == "bigquery":
        project_id = env("BIGQUERY_PROJECT_ID")
        dataset = env("BIGQUERY_DATASET", "")
        configured_schema = dataset or None
        credentials_path = env("BIGQUERY_CREDENTIALS_PATH")
        credentials_json = env("BIGQUERY_CREDENTIALS_JSON")

        required_params = {"BIGQUERY_PROJECT_ID": project_id}
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                "Missing required environment variables for BigQuery: "
                f"{', '.join(missing_params)}. Please check your .env file{where}."
            ).to_response()

        success = await run_db(
            db_manager.connect_bigquery,
            project_id=str(project_id),
            dataset=dataset or "",
            credentials_path=credentials_path,
            credentials_json=credentials_json,
        )
        db_name = f"{project_id}/{dataset}" if dataset else str(project_id)

    elif db_type == "duckdb":
        database_path = env("DUCKDB_DATABASE_PATH", ":memory:")
        motherduck_token = env("MOTHERDUCK_TOKEN")
        read_only = env("DUCKDB_READ_ONLY", "false").lower() == "true"

        success = await run_db(
            db_manager.connect_duckdb,
            database_path=database_path,
            motherduck_token=motherduck_token,
            read_only=read_only,
        )
        db_name = database_path

    elif db_type == "databricks":
        server_hostname = env("DATABRICKS_SERVER_HOSTNAME")
        http_path = env("DATABRICKS_HTTP_PATH")
        access_token = env("DATABRICKS_ACCESS_TOKEN")
        catalog = env("DATABRICKS_CATALOG", "hive_metastore")
        schema = env("DATABRICKS_SCHEMA", "default")
        configured_schema = schema

        required_params = {
            "DATABRICKS_SERVER_HOSTNAME": server_hostname,
            "DATABRICKS_HTTP_PATH": http_path,
            "DATABRICKS_ACCESS_TOKEN": access_token,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                "Missing required environment variables for Databricks: "
                f"{', '.join(missing_params)}. Please check your .env file{where}."
            ).to_response()

        success = await run_db(
            db_manager.connect_databricks,
            server_hostname=str(server_hostname),
            http_path=str(http_path),
            access_token=str(access_token),
            catalog=catalog,
            schema=schema,
        )
        db_name = f"{catalog}.{schema}"

    elif db_type == "mysql":
        host = env("MYSQL_HOST")
        port = env("MYSQL_PORT", "3306")
        database = env("MYSQL_DATABASE")
        configured_schema = database
        username = env("MYSQL_USERNAME")
        password = env("MYSQL_PASSWORD")
        charset = env("MYSQL_CHARSET", "utf8mb4")

        required_params = {
            "MYSQL_HOST": host,
            "MYSQL_DATABASE": database,
            "MYSQL_USERNAME": username,
            "MYSQL_PASSWORD": password,
        }
        missing_params = [k for k, v in required_params.items() if not v]
        if missing_params:
            return ValidationError(
                "Missing required environment variables for MySQL: "
                f"{', '.join(missing_params)}. Please check your .env file{where}."
            ).to_response()

        success = await run_db(
            db_manager.connect_mysql,
            host=str(host),
            port=int(port),
            database=str(database),
            username=str(username),
            password=str(password),
            charset=charset,
        )
        db_name = str(database)

    if success:
        session = services.get_session_data(ctx)
        new_conn_id = services.get_connection_fingerprint(db_manager)

        if session.connection_id and session.connection_id != new_conn_id:
            logger.info(
                f"Connection changed (old: {session.connection_id[:8]}..., new: {new_conn_id[:8]}...)"
            )
            # Awaited where it can be: background init started on the old
            # database is cancelled if nobody else needs it, and must have
            # stopped before the old database's RDF store is released.
            if services.provides("aclear_session_state"):
                await services.aclear_session_state(session, reason="connection change")
            else:
                services.clear_session_state(session, reason="connection change")
        elif not session.connection_id:
            logger.info(f"Initial connection established: {new_conn_id[:8]}...")

        # Before anything reads or writes the workspace: the fingerprint that
        # names it changed, so a directory from a previous release is still
        # under the old name. Renaming it is what keeps an upgrade from looking
        # like a first run.
        if services.provides("adopt_legacy_workspace"):
            try:
                adopted = services.adopt_legacy_workspace(
                    db_manager, new_conn_id, db_type, db_name
                )
                if adopted:
                    logger.info(
                        f"Adopted {len(adopted)} workspace director(ies) from a "
                        f"previous connection id: {', '.join(adopted)}"
                    )
            except Exception as e:
                logger.warning(f"Could not adopt a legacy workspace: {e}")

        session.connection_id = new_conn_id
        session.connected_at = utc_now()

        # Join the runtime every session on this database shares. Its schema
        # cache describes the database, not this client, so it is only reset
        # when nobody else is relying on it.
        shared_with_others = False
        if services.server_state is not None and isinstance(session, SessionData):
            runtime = services.server_state.bind_session(
                session, new_conn_id, db_manager
            )
            shared_with_others = runtime.holders > 1
        if not shared_with_others:
            session.clear_schema_cache()

        # The schema tables are qualified with. Told to the caller and made
        # the session's working schema: without a real name a client cannot
        # write schema.table and has to go looking for one.
        # Asked of the manager the session is bound to: binding may have kept
        # another session's open manager and discarded the fresh one.
        bound = session.db_manager or db_manager
        resolve = getattr(bound, "resolve_working_schema", None)
        working_schema = configured_schema or (
            await run_db(resolve) if resolve is not None else None
        )
        if not isinstance(working_schema, str) or not working_schema:
            working_schema = None
        session.working_schema = working_schema
        if working_schema:
            # The session's own pointers, on every successful connect: the
            # current schema and the default target a parameterless
            # generate_ontology() uses. Not the shared schema cache, which
            # other sessions on this database may be relying on.
            session.set_current_schema(working_schema)
            session.mark_schema_analyzed(working_schema)

        await notify_client(ctx, f"Connected to {label}: {db_name}")

        # Write workspace connection info
        try:
            await mutate_workspace_metadata(
                new_conn_id,
                OUTPUT_DIR,
                lambda mgr: mgr.update_workspace_connection(
                    db_type=db_type, db_name=db_name
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to write workspace connection info: {e}")

        # Detect and auto-restore existing workspace
        response = f"Successfully connected to {label} database: {db_name}"
        if working_schema:
            response += (
                f"\nWorking schema: {working_schema} -- qualify tables as "
                f"{working_schema}.<table>; discover_schema() without a schema "
                "analyzes it."
            )
        workspace = detect_workspace(new_conn_id)
        if workspace and services.provides("get_oxigraph_store"):
            try:
                # Restoring writes into state other sessions may be using.
                async with _restore_lock(services, session):
                    restore_result = await _restore_workspace_core(
                        ctx, session, new_conn_id, None, services
                    )
                # The restore selects a schema of its own, and points the
                # session's default target at the last one it restored; the
                # one announced above is the one the session works in, for
                # discovery and for a parameterless generate_ontology() alike.
                if working_schema:
                    session.set_current_schema(working_schema)
                    session.mark_schema_analyzed(working_schema)
                if restore_result:
                    response += "\n\n" + _format_restore_summary(restore_result)
                    restored = restore_result.get(
                        "restored_schemas", [restore_result.get("schema_name")]
                    )
                    if working_schema and working_schema not in restored:
                        response += (
                            f"\n\nNote: the working schema '{working_schema}' "
                            "has nothing restored -- the list above is for "
                            f"{', '.join(str(r) for r in restored)}. Call "
                            "discover_schema() to analyze it."
                        )
                else:
                    # Workspace detected but restore returned nothing
                    response += "\n\n" + format_workspace_summary(workspace)
            except Exception as e:
                logger.warning(f"Auto-restore failed: {e}")
                response += "\n\n" + format_workspace_summary(workspace)
        elif workspace:
            # No get_oxigraph_store available (e.g., tests) — show summary only
            response += "\n\n" + format_workspace_summary(workspace)

        return response
    else:
        if db_manager is not services.get_session_data(ctx).db_manager:
            # A fresh manager that never connected; the shared one is untouched.
            try:
                db_manager.disconnect()
            except Exception as e:
                logger.debug(f"Discarding failed connect manager: {e}")
        await notify_client(
            ctx, "Database connection failed; check credentials and try again"
        )
        return ConnectionError(
            f"Failed to connect to {label} database: {db_name}"
        ).to_response()


async def list_databases(services: "HandlerContext") -> dict[str, Any]:
    """The databases this server is configured for, without credentials.

    Args:
        services: Request-scoped services.

    Returns:
        Each database's name, type, description and target (catalog, schema
        or database name where the type has one), or the configuration error.
    """
    try:
        configured = configured_databases()
    except DatabaseConfigError as e:
        return ValidationError(str(e)).to_response()
    databases = [entry.describe() for entry in configured]
    result: dict[str, Any] = {"success": True, "databases": databases}
    if not databases:
        result["message"] = (
            "No database is configured. Set OBA_DATABASES and DB_<NAME>_* "
            "variables, or a type's own variables, in the server's .env."
        )
    elif len(databases) == 1:
        result["message"] = (
            f"One database is configured: connect_database() connects to "
            f"'{databases[0]['name']}' without arguments."
        )
    else:
        result["message"] = (
            "Pick the one the user means by name or description and call "
            "connect_database(database=<name>). If it is unclear, ask."
        )
    return result


async def list_schemas(ctx: Context, services: "HandlerContext") -> list[str]:
    """Get a list of available schemas from the connected database.

    Args:
        ctx: FastMCP context
        get_session_db_manager: Function to get session db manager

    Returns:
        List of schema names
    """
    db_manager = services.get_session_db_manager(ctx)
    schemas = await run_db(db_manager.get_schemas)
    if schemas:
        await notify_client(
            ctx, f"Found {len(schemas)} schemas; next call should be discover_schema"
        )
    else:
        await notify_client(ctx, "No schemas found")
    return schemas if schemas else []
