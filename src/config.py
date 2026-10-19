"""Configuration management for OrionBelt Analytics."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from .constants import (
    DEFAULT_BASE_URI,
    DEFAULT_CLICKHOUSE_PORT,
    DEFAULT_DREMIO_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS,
    DEFAULT_SESSION_SCAN_INTERVAL_SECONDS,
    DEFAULT_SNOWFLAKE_SCHEMA,
)

logger = logging.getLogger(__name__)

# Accepted until v2.1.x with a deprecation warning, now refused at startup.
_REMOVED_TRANSPORT = "sse"


SEMANTIC_NAMING_MODES = ("auto", "input_required", "review")


def _resolve_semantic_naming_mode() -> str:
    """Read SEMANTIC_NAMING_MODE, honouring the deprecated ENABLE_SAMPLING.

    ``SEMANTIC_NAMING_MODE`` wins when set. Otherwise ``ENABLE_SAMPLING=false``
    still means what it always did -- never ask the client's model -- which is
    the ``review`` mode.

    Returns:
        One of :data:`SEMANTIC_NAMING_MODES`.
    """
    raw = os.getenv("SEMANTIC_NAMING_MODE")
    if raw is not None and raw.strip():
        mode = raw.strip().lower()
        if mode in SEMANTIC_NAMING_MODES:
            return mode
        logger.warning(
            f"Invalid SEMANTIC_NAMING_MODE='{raw}'. Must be one of: "
            f"{', '.join(SEMANTIC_NAMING_MODES)}. Defaulting to 'auto'."
        )
        return "auto"

    legacy = os.getenv("ENABLE_SAMPLING")
    if legacy is None:
        return "auto"
    disabled = legacy.strip().lower() != "true"
    # Every .env copied from the old template carries ENABLE_SAMPLING=true, so
    # only the value that actually changes behaviour is worth a warning.
    logger.log(
        logging.WARNING if disabled else logging.INFO,
        "ENABLE_SAMPLING is deprecated; use SEMANTIC_NAMING_MODE "
        "(auto | input_required | review). ENABLE_SAMPLING=false maps to "
        "'review'.",
    )
    return "review" if disabled else "auto"


DEFAULT_MCP_CACHE_TTL_SECONDS = 300


def resolve_mcp_cache_ttl() -> int:
    """Read MCP_CACHE_TTL_SECONDS; 0 disables the hint, junk keeps the default."""
    raw = os.getenv("MCP_CACHE_TTL_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_MCP_CACHE_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        ttl = -1
    if ttl < 0:
        logger.warning(
            f"Invalid MCP_CACHE_TTL_SECONDS='{raw}'. Must be a whole number of "
            f"seconds, 0 to disable. Defaulting to {DEFAULT_MCP_CACHE_TTL_SECONDS}."
        )
        return DEFAULT_MCP_CACHE_TTL_SECONDS
    return ttl


DEFAULT_METADATA_CACHE_TTL_SECONDS = 300


def resolve_metadata_cache_ttl() -> int:
    """How long the database's own metadata may be reused, in seconds.

    Its own setting, not the session idle timeout: how fast a *database's*
    tables change has nothing to do with how long a *client* sits idle. A
    development database that gains columns every few minutes wants a short
    one; a warehouse whose schema changes quarterly can hold it for hours and
    save the reflection.

    A manual ``reset_cache`` is immediate whatever this says.

    Returns:
        Seconds to keep a metadata answer; 0 disables reuse entirely, and
        anything unreadable keeps the default.
    """
    raw = os.getenv("METADATA_CACHE_TTL_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_METADATA_CACHE_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        ttl = -1
    if ttl < 0:
        logger.warning(
            f"Invalid METADATA_CACHE_TTL_SECONDS='{raw}'. Must be a whole "
            f"number of seconds, 0 to disable reuse. Defaulting to "
            f"{DEFAULT_METADATA_CACHE_TTL_SECONDS}."
        )
        return DEFAULT_METADATA_CACHE_TTL_SECONDS
    return ttl


DEFAULT_DB_MAX_QUEUED_CALLS = 32


def resolve_db_max_queued_calls() -> int:
    """How many calls may wait for one database connection at a time.

    Calls on a connection run one after another -- an in-memory DuckDB engine
    shares a single connection, and the order a blocked event loop used to give
    is kept everywhere. This bounds the line behind a slow call.

    Returns:
        The most calls allowed to wait; 0 lets the line grow without bound,
        and anything unreadable keeps the default.
    """
    raw = os.getenv("DB_MAX_QUEUED_CALLS")
    if raw is None or not raw.strip():
        return DEFAULT_DB_MAX_QUEUED_CALLS
    try:
        bound = int(raw)
    except ValueError:
        bound = -1
    if bound < 0:
        logger.warning(
            f"Invalid DB_MAX_QUEUED_CALLS='{raw}'. Must be a whole number, 0 for "
            f"no bound. Defaulting to {DEFAULT_DB_MAX_QUEUED_CALLS}."
        )
        return DEFAULT_DB_MAX_QUEUED_CALLS
    return bound


SESSIONLESS_FALLBACKS = ("sole_session", "none")


def _resolve_sessionless_fallback() -> str:
    """Read SESSIONLESS_FALLBACK, defaulting to ``sole_session``."""
    raw = os.getenv("SESSIONLESS_FALLBACK", "sole_session").strip().lower()
    if raw in SESSIONLESS_FALLBACKS:
        return raw
    logger.warning(
        f"Invalid SESSIONLESS_FALLBACK='{raw}'. Must be one of: "
        f"{', '.join(SESSIONLESS_FALLBACKS)}. Defaulting to 'sole_session'."
    )
    return "sole_session"


_AUTH_MODES = ("none", "jwt")
_HMAC_ALGORITHMS = ("HS256", "HS384", "HS512")
# What JWTVerifier accepts, in its spelling. Mixed case matters: "EdDSA",
# "Ed25519" and "Ed448" are not recognised in upper case.
_JWT_ALGORITHMS = (
    *_HMAC_ALGORITHMS,
    *(f"{family}{bits}" for family in ("RS", "ES", "PS") for bits in (256, 384, 512)),
    "EdDSA",
    "Ed25519",
    "Ed448",
)


def _canonical_algorithm(raw: str) -> str:
    """The algorithm in its canonical spelling, matched regardless of case.

    An unknown name is returned as given, for validation to reject.
    """
    name = raw.strip()
    for known in _JWT_ALGORITHMS:
        if known.lower() == name.lower():
            return known
    return name


@dataclass
class AuthConfig:
    """Bearer-token authentication of the HTTP transport (``MCP_AUTH_*``)."""

    # "none" (default) or "jwt": verify a JSON Web Token on every request.
    mode: str = "none"
    # Where the verification key comes from: the issuer's JWKS endpoint, or
    # a PEM public key / shared HMAC secret. Exactly one of them.
    jwks_uri: str | None = None
    public_key: str | None = None
    issuer: str | None = None
    # This server's identifier in the token's "aud" claim. Required: without
    # it, a token the issuer minted for any other service would be accepted.
    audience: str | None = None
    algorithm: str = "RS256"
    required_scopes: list[str] = field(default_factory=list)
    # Public URL of this server. When set, it advertises the issuer as its
    # authorization server (RFC 9728), so MCP clients can find where to log in.
    resource_url: str | None = None

    def validate(self) -> None:
        """Refuse settings that would leave the server open or never accept.

        Raises:
            ValueError: Unknown mode, or a jwt mode missing what it needs.
        """
        if self.mode not in _AUTH_MODES:
            # Fail closed: a typo must not turn authentication off.
            raise ValueError(
                f"Invalid MCP_AUTH='{self.mode}'. Must be one of: "
                f"{', '.join(_AUTH_MODES)}"
            )
        if self.mode == "none":
            return
        if bool(self.jwks_uri) == bool(self.public_key):
            raise ValueError(
                "MCP_AUTH=jwt needs exactly one of MCP_AUTH_JWKS_URI (the "
                "issuer's key set) or MCP_AUTH_PUBLIC_KEY (a PEM public key, or "
                "the shared secret for HS256/384/512)."
            )
        missing = [
            name
            for name, value in (
                ("MCP_AUTH_ISSUER", self.issuer),
                ("MCP_AUTH_AUDIENCE", self.audience),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"MCP_AUTH=jwt needs {' and '.join(missing)}: tokens are only "
                "accepted from a known issuer and for this server's audience."
            )
        if self.algorithm not in _JWT_ALGORITHMS:
            raise ValueError(
                f"Unsupported MCP_AUTH_ALGORITHM='{self.algorithm}'. Must be one "
                f"of: {', '.join(_JWT_ALGORITHMS)}"
            )
        if self.algorithm in _HMAC_ALGORITHMS and self.jwks_uri:
            raise ValueError(
                f"MCP_AUTH_ALGORITHM={self.algorithm} is a shared-secret "
                "algorithm; set the secret in MCP_AUTH_PUBLIC_KEY, not a JWKS URI."
            )


def _load_auth_config() -> AuthConfig:
    scopes = os.getenv("MCP_AUTH_REQUIRED_SCOPES", "")
    return AuthConfig(
        mode=os.getenv("MCP_AUTH", "none").strip().lower(),
        jwks_uri=os.getenv("MCP_AUTH_JWKS_URI") or None,
        public_key=os.getenv("MCP_AUTH_PUBLIC_KEY") or None,
        issuer=os.getenv("MCP_AUTH_ISSUER") or None,
        audience=os.getenv("MCP_AUTH_AUDIENCE") or None,
        algorithm=_canonical_algorithm(os.getenv("MCP_AUTH_ALGORITHM", "RS256")),
        required_scopes=[s for s in scopes.replace(",", " ").split() if s],
        resource_url=os.getenv("MCP_AUTH_RESOURCE_URL") or None,
    )


@dataclass
class ServerConfig:
    """Server configuration settings."""

    log_level: str
    ontology_base_uri: str
    mcp_transport: str = "http"  # Default to http transport
    mcp_server_host: str = "localhost"
    mcp_server_port: int = 9000
    session_idle_timeout: int = DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS
    session_scan_interval: int = DEFAULT_SESSION_SCAN_INTERVAL_SECONDS
    chart_return_binary: bool = False
    # How suggest_semantic_names obtains rename suggestions: "auto" (best path
    # the client supports), "input_required" (multi round-trip request; needs
    # FastMCP 4) or "review" (the client model proposes names itself).
    semantic_naming_mode: str = "auto"
    # What a request with neither a transport session nor a connection handle
    # resolves to: "sole_session" (the only live session, if there is exactly
    # one) or "none" (always an error). Use "none" when several people share
    # the server and must never land in each other's session by omission.
    sessionless_fallback: str = "sole_session"
    # Seconds a client may cache the tool list, the resource list and resource
    # reads (MCP 2026-07-28, SEP-2549). 0 sends no hint.
    mcp_cache_ttl: int = 300

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.ontology_base_uri.endswith("/"):
            self.ontology_base_uri += "/"

        # Validate transport type. "sse" is kept as given, so validate_config
        # can say it was removed rather than start an HTTP server at another
        # URL that its clients never reach.
        if self.mcp_transport not in ["http", _REMOVED_TRANSPORT]:
            logger.warning(
                f"Invalid MCP_TRANSPORT value '{self.mcp_transport}'. Defaulting to 'http'."
            )
            self.mcp_transport = "http"


@dataclass
class DatabaseConfig:
    """Database connection configuration."""

    # PostgreSQL settings
    postgres_host: str | None = None
    postgres_port: int = DEFAULT_POSTGRES_PORT
    postgres_database: str | None = None
    postgres_username: str | None = None
    postgres_password: str | None = None

    # Snowflake settings
    snowflake_account: str | None = None
    snowflake_username: str | None = None
    snowflake_password: str | None = None
    snowflake_warehouse: str | None = None
    snowflake_database: str | None = None
    snowflake_schema: str = DEFAULT_SNOWFLAKE_SCHEMA
    snowflake_role: str = "PUBLIC"

    # Dremio settings (following official dremio-mcp approach)
    dremio_uri: str | None = (
        None  # Full API endpoint like https://api.dremio.cloud or https://host:port
    )
    dremio_pat: str | None = None  # Personal Access Token
    # Legacy settings for backward compatibility
    dremio_host: str | None = None
    dremio_port: int = DEFAULT_DREMIO_PORT
    dremio_username: str | None = None
    dremio_password: str | None = None
    dremio_ssl: bool = False

    # ClickHouse settings
    clickhouse_host: str | None = None
    clickhouse_port: int = DEFAULT_CLICKHOUSE_PORT
    clickhouse_database: str | None = None
    clickhouse_username: str = "default"
    clickhouse_password: str = ""
    clickhouse_protocol: str = "http"
    clickhouse_secure: bool = False


class ConfigManager:
    """Manages application configuration."""

    def __init__(self) -> None:
        """Initialize configuration manager."""
        # Load .env from project root (one level up from src)
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        load_dotenv(env_path)
        self._server_config: ServerConfig | None = None
        self._db_config: DatabaseConfig | None = None
        self._auth_config: AuthConfig | None = None

    def get_server_config(self) -> ServerConfig:
        """Get server configuration."""
        if self._server_config is None:
            self._server_config = ServerConfig(
                log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
                ontology_base_uri=os.getenv("ONTOLOGY_BASE_URI", DEFAULT_BASE_URI),
                mcp_transport=os.getenv("MCP_TRANSPORT", "http").lower(),
                mcp_server_host=os.getenv("MCP_SERVER_HOST", "localhost"),
                mcp_server_port=int(os.getenv("MCP_SERVER_PORT", "9000")),
                session_idle_timeout=int(
                    os.getenv(
                        "SESSION_IDLE_TIMEOUT_SECONDS",
                        str(DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS),
                    )
                ),
                session_scan_interval=int(
                    os.getenv(
                        "SESSION_SCAN_INTERVAL_SECONDS",
                        str(DEFAULT_SESSION_SCAN_INTERVAL_SECONDS),
                    )
                ),
                chart_return_binary=os.getenv("CHART_RETURN_BINARY", "false").lower()
                == "true",
                semantic_naming_mode=_resolve_semantic_naming_mode(),
                sessionless_fallback=_resolve_sessionless_fallback(),
                mcp_cache_ttl=resolve_mcp_cache_ttl(),
            )
            logger.info("Server configuration loaded")
        return self._server_config

    def get_auth_config(self) -> AuthConfig:
        """Get bearer-token authentication settings."""
        if self._auth_config is None:
            self._auth_config = _load_auth_config()
        return self._auth_config

    def get_database_config(self) -> DatabaseConfig:
        """Get database configuration."""
        if self._db_config is None:
            self._db_config = DatabaseConfig(
                postgres_host=os.getenv("POSTGRES_HOST"),
                postgres_port=int(os.getenv("POSTGRES_PORT", DEFAULT_POSTGRES_PORT)),
                postgres_database=os.getenv("POSTGRES_DATABASE"),
                postgres_username=os.getenv("POSTGRES_USERNAME"),
                postgres_password=os.getenv("POSTGRES_PASSWORD"),
                snowflake_account=os.getenv("SNOWFLAKE_ACCOUNT"),
                snowflake_username=os.getenv("SNOWFLAKE_USERNAME"),
                snowflake_password=os.getenv("SNOWFLAKE_PASSWORD"),
                snowflake_warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
                snowflake_database=os.getenv("SNOWFLAKE_DATABASE"),
                snowflake_schema=os.getenv(
                    "SNOWFLAKE_SCHEMA", DEFAULT_SNOWFLAKE_SCHEMA
                ),
                snowflake_role=os.getenv("SNOWFLAKE_ROLE", "PUBLIC"),
                # New PAT-based settings (preferred)
                dremio_uri=os.getenv("DREMIO_URI"),
                dremio_pat=os.getenv("DREMIO_PAT"),
                # Legacy settings for backward compatibility
                dremio_host=os.getenv("DREMIO_HOST"),
                dremio_port=int(os.getenv("DREMIO_PORT", DEFAULT_DREMIO_PORT)),
                dremio_username=os.getenv("DREMIO_USERNAME"),
                dremio_password=os.getenv("DREMIO_PASSWORD"),
                dremio_ssl=os.getenv("DREMIO_SSL", "false").lower() == "true",
                # ClickHouse settings
                clickhouse_host=os.getenv("CLICKHOUSE_HOST"),
                clickhouse_port=int(
                    os.getenv("CLICKHOUSE_PORT", DEFAULT_CLICKHOUSE_PORT)
                ),
                clickhouse_database=os.getenv("CLICKHOUSE_DATABASE"),
                clickhouse_username=os.getenv("CLICKHOUSE_USERNAME", "default"),
                clickhouse_password=os.getenv("CLICKHOUSE_PASSWORD", ""),
                clickhouse_protocol=os.getenv("CLICKHOUSE_PROTOCOL", "http"),
                clickhouse_secure=os.getenv("CLICKHOUSE_SECURE", "false").lower()
                == "true",
            )
            logger.info("Database configuration loaded")
        return self._db_config

    def validate_config(self) -> None:
        """Validate core server configuration at startup.

        Checks that MCP_TRANSPORT is a recognized value and MCP_SERVER_PORT
        is within the valid TCP port range (1-65535).

        Raises:
            ValueError: If any configuration value is invalid.
        """
        config = self.get_server_config()

        # Validate transport type
        if config.mcp_transport == _REMOVED_TRANSPORT:
            # Superseded since MCP 2025-03-26, Deprecated in 2026-07-28, and
            # removed here after a release of warnings (v2.1.0).
            raise ValueError(
                "MCP_TRANSPORT=sse is no longer supported: the MCP specification "
                "deprecated the HTTP+SSE transport. Set MCP_TRANSPORT=http and "
                "point clients at the streamable HTTP endpoint "
                f"(http://<host>:{config.mcp_server_port}/mcp)."
            )
        valid_transports = {"http"}
        if config.mcp_transport not in valid_transports:
            raise ValueError(
                f"Invalid MCP_TRANSPORT='{config.mcp_transport}'. "
                f"Must be one of: {', '.join(sorted(valid_transports))}"
            )

        self.get_auth_config().validate()

        # Validate port range
        if not (1 <= config.mcp_server_port <= 65535):
            raise ValueError(
                f"Invalid MCP_SERVER_PORT={config.mcp_server_port}. "
                f"Must be between 1 and 65535."
            )

        logger.info("Server configuration validated successfully")

    def validate_db_config(self, db_type: str) -> dict[str, Any]:
        """Validate database configuration for a specific type."""
        config = self.get_database_config()
        missing_params = []

        if db_type == "postgresql":
            required_fields = {
                "host": config.postgres_host,
                "port": config.postgres_port,
                "database": config.postgres_database,
                "username": config.postgres_username,
                "password": config.postgres_password,
            }
        elif db_type == "snowflake":
            required_fields = {
                "account": config.snowflake_account,
                "username": config.snowflake_username,
                "password": config.snowflake_password,
                "warehouse": config.snowflake_warehouse,
                "database": config.snowflake_database,
            }
        elif db_type == "dremio":
            # Prefer new PAT-based authentication
            if config.dremio_uri and config.dremio_pat:
                required_fields = {"uri": config.dremio_uri, "pat": config.dremio_pat}
            else:
                # Fall back to legacy username/password authentication
                required_fields = {
                    "host": config.dremio_host,
                    "port": config.dremio_port,
                    "username": config.dremio_username,
                    "password": config.dremio_password,
                    "ssl": config.dremio_ssl,
                }
        elif db_type == "clickhouse":
            required_fields = {
                "host": config.clickhouse_host,
                "database": config.clickhouse_database,
            }
        else:
            raise ValueError(f"Unsupported database type: {db_type}")

        missing_params = [k for k, v in required_fields.items() if not v]

        return {
            "valid": len(missing_params) == 0,
            "missing_params": missing_params,
            "config": required_fields,
        }


# Global configuration manager instance
config_manager = ConfigManager()
