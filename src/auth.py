"""Bearer-token authentication for the HTTP transport.

With ``MCP_AUTH=jwt`` every HTTP request must carry a JSON Web Token that the
configured issuer signed for this server's audience. FastMCP verifies it
(``JWTVerifier``: signature against a JWKS endpoint or a key, expiry, issuer,
audience, scopes) and rejects the request with 401 before any tool runs.

The verified token also names the caller. ``current_user`` turns it into a
stable key -- issuer and subject -- that the session layer uses to keep one
user out of another's sessions. stdio has no tokens; there, as with
``MCP_AUTH=none``, there is no user and nothing changes.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth import AccessToken, AuthProvider, RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

from .config import AuthConfig

logger = logging.getLogger(__name__)


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
    config.validate()
    if config.mode == "none":
        return None

    verifier = JWTVerifier(
        jwks_uri=config.jwks_uri,
        public_key=config.public_key,
        issuer=config.issuer,
        audience=config.audience,
        algorithm=config.algorithm,
        required_scopes=config.required_scopes or None,
    )
    logger.info(
        "Bearer-token authentication on: issuer %s, audience %s",
        config.issuer,
        config.audience,
    )
    if not config.resource_url:
        return verifier
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[config.issuer],  # type: ignore[list-item]
        base_url=config.resource_url,
    )


def user_key(token: AccessToken | None) -> str | None:
    """The stable identity in a verified token: issuer and subject.

    The subject alone is only unique per issuer, so both are part of it.
    """
    if token is None:
        return None
    subject = token.claims.get("sub")
    if not subject:
        return None
    return f"{token.claims.get('iss', '')}|{subject}"


def current_user() -> str | None:
    """The authenticated caller of the current request, or None if anonymous.

    Outside a request (startup, background work) there is no token and the
    answer is None. Any other failure propagates: guessing "anonymous" would
    put the caller in sessions that belong to no one.
    """
    return user_key(get_access_token())
