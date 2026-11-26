"""Bearer-token authentication for the HTTP transport.

With ``MCP_AUTH=jwt`` every HTTP request must carry a JSON Web Token that the
configured issuer signed for this server's audience. FastMCP verifies it
(``JWTVerifier``: signature against a JWKS endpoint or a key, expiry, issuer,
audience, scopes) and rejects the request with 401 before any tool runs.
Tokens without a subject are rejected too, since the subject names the user.

The verified token also names the caller. ``current_user`` turns it into a
stable key -- issuer and subject -- that the session layer uses to keep one
user out of another's sessions. stdio has no tokens; there, as with
``MCP_AUTH=none``, there is no user and nothing changes.

Its scopes are read from the claims ``MCP_AUTH_SCOPE_CLAIMS`` names, mapped by
``MCP_AUTH_SCOPE_MAP`` (:mod:`src.scopes`). With ``MCP_AUTH_TOOL_SCOPES=true``,
each tool needs its tier's scope: a call without it is refused with
``insufficient_scope``, and the tool is left out of the caller's tool list.

Without ``MCP_AUTH_JWKS_URI`` or ``MCP_AUTH_PUBLIC_KEY``, the key set comes
from the issuer's OpenID discovery document, read at startup.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from fastmcp.server.auth import AccessToken, AuthProvider, RemoteAuthProvider
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.tools.base import Tool
from fastmcp.utilities.authorization import AuthContext, _ScopeAwareCheck

from .config import AuthConfig
from .scopes import DEFAULT_SCOPE_CLAIMS, TOOL_SCOPES, scopes_from_claims
from .warehouse_login import IdpClient

#: Seconds to wait for the issuer's discovery document at startup.
DISCOVERY_TIMEOUT_SECONDS = 10

logger = logging.getLogger(__name__)


class _SubjectJWTVerifier(JWTVerifier):
    """A JWTVerifier that also rejects tokens without a subject.

    The subject is what keeps users apart: every token without one would
    otherwise be the same nobody, free to use each other's sessions.

    Its scopes are those :func:`~src.scopes.scopes_from_claims` reads, so the
    required scopes and the tools' tiers are checked against the same set.
    """

    def __init__(
        self,
        *args: Any,
        scope_claims: tuple[str, ...] = DEFAULT_SCOPE_CLAIMS,
        scope_map: dict[str, frozenset[str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._scope_claims = scope_claims
        self._scope_map = scope_map or {}

    def _extract_scopes(self, claims: dict[str, Any]) -> list[str]:
        return scopes_from_claims(claims, self._scope_claims, self._scope_map)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = await super().load_access_token(token)
        if access is not None and user_key(access) is None:
            self.logger.warning(
                "Bearer token rejected for client %s: no subject (sub) claim",
                access.client_id,
            )
            return None
        return access


def build_auth_provider(config: AuthConfig) -> AuthProvider | None:
    """The auth provider for the configured mode, or None when auth is off.

    Args:
        config: Authentication settings.

    Returns:
        A token verifier; wrapped so that it advertises the issuer as the
        authorization server (RFC 9728) when ``MCP_AUTH_RESOURCE_URL`` is
        set, which lets MCP clients discover where to log in.

    Raises:
        ValueError: If the settings are incomplete or unknown. Raised here as
            well as at startup validation, so that a server with broken auth
            settings never starts unauthenticated.
    """
    global _resolved
    config.validate()
    if config.mode == "none":
        _resolved = config
        return None
    config = with_discovery(config, need_token_endpoint=_idp_logins_configured())
    _resolved = config

    verifier = _SubjectJWTVerifier(
        jwks_uri=config.jwks_uri,
        public_key=config.public_key,
        issuer=config.issuer,
        audience=config.audience,
        algorithm=config.algorithm,
        required_scopes=config.required_scopes or None,
        scope_claims=config.scope_claims,
        scope_map=config.scope_map,
    )
    logger.info(
        "Bearer-token authentication on: issuer %s, audience %s, tool scopes %s",
        config.issuer,
        config.audience,
        "enforced" if config.tool_scopes else "not enforced",
    )
    if not config.resource_url:
        return verifier
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[config.issuer],  # type: ignore[list-item]
        base_url=config.resource_url,
    )


def with_discovery(config: AuthConfig, need_token_endpoint: bool = False) -> AuthConfig:
    """*config* with what it leaves out read from the issuer's discovery document.

    The key set (``jwks_uri``) when neither a key set nor a key is configured,
    and the token endpoint unless ``MCP_AUTH_TOKEN_ENDPOINT`` is set. The
    document is read only when one of them is missing and needed: the token
    endpoint is needed when *need_token_endpoint* (a database logs in as the
    user through the identity provider). Air-gapped installs keep the
    explicit settings.

    Raises:
        ValueError: When the document cannot be read, names another issuer, or
            lacks what is needed. The server does not start then, as it does
            not with incomplete settings.
    """
    need_keys = not (config.jwks_uri or config.public_key)
    need_endpoint = need_token_endpoint and not config.token_endpoint
    if not (need_keys or need_endpoint):
        return config
    url = config.discovery_url
    wanted = "the key set" if need_keys else "the token endpoint"
    try:
        document = OIDCConfiguration.get_oidc_configuration(
            url,  # type: ignore[arg-type]
            strict=False,
            timeout_seconds=DISCOVERY_TIMEOUT_SECONDS,
        )
    except Exception as error:
        raise ValueError(
            f"MCP_AUTH=jwt reads {wanted} from {url}, which failed: "
            f"{type(error).__name__}. Set "
            + (
                "MCP_AUTH_JWKS_URI or MCP_AUTH_PUBLIC_KEY"
                if need_keys
                else "MCP_AUTH_TOKEN_ENDPOINT"
            )
            + ", or check MCP_AUTH_ISSUER."
        ) from error
    issuer = str(document.issuer or "")
    if issuer != config.issuer:
        # OpenID Connect Discovery 1.0, 4.3: the document's issuer must be
        # identical to the one it was fetched for -- and tokens are checked
        # against MCP_AUTH_ISSUER character for character, so a trailing slash
        # either side would start a server that refuses every token.
        raise ValueError(
            f"The discovery document at {url} names the issuer {issuer!r}, not "
            f"MCP_AUTH_ISSUER {config.issuer!r}. Set MCP_AUTH_ISSUER to exactly "
            "the issuer the identity provider puts in its tokens."
        )
    jwks_uri = config.jwks_uri
    if need_keys:
        jwks_uri = str(document.jwks_uri or "")
        if not jwks_uri.startswith(("https://", "http://")):
            raise ValueError(
                f"The discovery document at {url} names no key set (jwks_uri)."
            )
        logger.info("Key set from the issuer's discovery document: %s", jwks_uri)
    token_endpoint = config.token_endpoint or (
        str(document.token_endpoint) if document.token_endpoint else None
    )
    if need_token_endpoint and not token_endpoint:
        raise ValueError(
            f"The discovery document at {url} names no token endpoint, which a "
            "database logging in as the user needs: set MCP_AUTH_TOKEN_ENDPOINT."
        )
    return dataclasses.replace(config, jwks_uri=jwks_uri, token_endpoint=token_endpoint)


#: The settings the server runs with, discovery applied; set at startup.
_resolved: AuthConfig | None = None


def sign_in_client() -> tuple[IdpClient | None, str | None]:
    """Our own client at the identity provider, and its token endpoint.

    What a database that logs in as the user through the identity provider
    (Snowflake) asks with. As resolved at startup, discovery included; None
    where they are not configured, which startup refuses for such a
    database.
    """
    config = _resolved
    if config is None or config.mode != "jwt":
        return None, None
    client = (
        IdpClient(str(config.client_id), str(config.client_secret))
        if config.has_client
        else None
    )
    return client, config.token_endpoint


def _idp_logins_configured() -> bool:
    """Whether a configured database logs in as the user through the IdP."""
    from .database_registry import DatabaseConfigError, configured_databases
    from .warehouse_login import uses_idp

    try:
        entries = configured_databases()
    except DatabaseConfigError:
        return False
    return any(uses_idp(entry) for entry in entries)


class ToolScopeCheck(_ScopeAwareCheck):
    """Each tool needs its tier's scope (:data:`~src.scopes.TOOL_SCOPES`).

    Scope-aware, so a shortfall is answered as ``insufficient_scope`` naming
    the scope to ask for. A tool without a tier is refused, so one added
    without a tier fails closed (a test keeps the table complete). Resources
    and prompts are not tools and need no tier.
    """

    def __call__(self, ctx: AuthContext) -> bool:
        if not isinstance(ctx.component, Tool):
            return True
        if ctx.token is None:
            return False
        needed = TOOL_SCOPES.get(ctx.component.name)
        return needed is not None and needed in ctx.token.scopes

    def missing_scopes(self, ctx: AuthContext) -> set[str]:
        if not isinstance(ctx.component, Tool) or ctx.token is None:
            return set()
        needed = TOOL_SCOPES.get(ctx.component.name)
        if needed is None or needed in ctx.token.scopes:
            return set()
        return {needed}


def tool_scope_middleware(config: AuthConfig) -> list[Middleware]:
    """The middleware that checks the tools' tiers, when they are enforced.

    Empty unless ``MCP_AUTH=jwt`` and ``MCP_AUTH_TOOL_SCOPES=true``. stdio
    carries no tokens, and FastMCP's AuthMiddleware lets it through.
    """
    if config.mode != "jwt" or not config.tool_scopes:
        return []
    return [AuthMiddleware(auth=ToolScopeCheck())]


def user_key(token: AccessToken | None) -> str | None:
    """The stable identity in a verified token: issuer and subject.

    The subject alone is only unique per issuer, so both are part of it.
    None when there is no token, or its subject is missing or not a
    nonempty string; the verifier rejects such tokens.
    """
    if token is None:
        return None
    subject = token.claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        return None
    return f"{token.claims.get('iss', '')}|{subject}"


def current_bearer() -> str | None:
    """The current request's verified bearer token, as the client sent it.

    What a connection that logs in as the user exchanges for a warehouse
    token. None outside a request, or without authentication. Never log it.
    """
    token = get_access_token()
    return token.token if token is not None and token.token else None


def current_user() -> str | None:
    """The authenticated caller of the current request, or None if anonymous.

    Outside a request (startup, background work) there is no token and the
    answer is None. Any other failure propagates: guessing "anonymous" would
    put the caller in sessions that belong to no one.
    """
    return user_key(get_access_token())
