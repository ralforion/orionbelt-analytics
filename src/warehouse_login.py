"""Signing in to a database as the user who called, not as the server.

A named connection with ``DB_<NAME>_AUTH=user`` runs its queries as the
signed-in caller: the caller's token (``MCP_AUTH=jwt``) is exchanged for one
issued *for the warehouse*, and the connection is opened with it. The warehouse
then applies that user's grants, row-level security and masking, and its query
history names them. Ported from OrionBelt Semantic Layer, with its setting
names and error codes, so one guide serves both:

- Databricks: OAuth token federation, an RFC 8693 exchange at the workspace's
  own ``/oidc/v1/token``;
- BigQuery: workforce identity federation at Google's security token service;
- Snowflake: External OAuth, the identity provider issuing the Snowflake
  token to our own client -- Entra ID's on-behalf-of flow, or the standard
  token exchange (Okta, Keycloak).

A warehouse token lasts about an hour. :class:`UserLogin` keeps the current
one in memory, one per user and connection, and exchanges again shortly
before it expires -- in async code, ahead of the query, since the driver reads
the token in a worker thread that has no request to exchange from. A token is
never logged, written to disk, or put in an error message.

No MCP dependency: the caller passes the user's token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

import aiohttp

from .database_registry import DatabaseEntry

logger = logging.getLogger(__name__)

#: Exchange again when the warehouse token expires within this many seconds.
RENEW_MARGIN_SECONDS = 120.0
#: How long a token service may take to answer.
EXCHANGE_TIMEOUT_SECONDS = 10.0
#: The databases that can log in as the user, as this release supports them.
USER_LOGIN_DIALECTS = ("databricks",)

_GRANT_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"
_TYPE_ID = "urn:ietf:params:oauth:token-type:id_token"
_ON_BEHALF_OF = "on_behalf_of"
_GOOGLE_STS = "https://sts.googleapis.com/v1/token"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
#: ``DB_<NAME>_OAUTH_EXCHANGE`` for Entra ID's on-behalf-of flow.
ENTRA_OBO = "entra_obo"
#: ``DB_<NAME>_OAUTH_EXCHANGE`` for the standard token exchange (RFC 8693).
STANDARD_EXCHANGE = "token_exchange"
#: Identity provider errors the user resolves by signing in again: MFA or
#: conditional access (Entra adds a claims challenge), or consent.
_INTERACTION_ERRORS = frozenset(
    {"interaction_required", "login_required", "consent_required"}
)
#: Entra marks an ``invalid_grant`` the user can resolve by signing in again
#: with a ``suberror``; these two they cannot. Without one, it is a refusal.
_REFUSAL_SUBERRORS = frozenset({"client_mismatch", "protection_policy_required"})


# --- errors -----------------------------------------------------------------


class DataSourceSignInError(Exception):
    """Signing in to a database as the user failed. Carries a code."""

    code = "DATA_SOURCE_SIGN_IN_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(f"[{self.code}] {message}")
        self.message = message


class SignInRequiredError(DataSourceSignInError):
    """The database logs in as the user, and the caller is no signed-in user."""

    code = "SIGN_IN_REQUIRED"


class SignInRefusedError(DataSourceSignInError):
    """The warehouse's token service refused this user; ``error`` its OAuth code."""

    code = "DATA_SOURCE_SIGN_IN_REFUSED"

    def __init__(self, message: str, error: str | None = None) -> None:
        super().__init__(message)
        self.error = error


class SignInInteractionRequiredError(DataSourceSignInError):
    """The identity provider wants the user at its sign-in page first.

    Multi-factor authentication, a conditional access policy or a missing
    consent. ``claims`` is the challenge Entra adds, which the client presents
    when it signs the user in again, or None.
    """

    code = "DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED"

    def __init__(self, message: str, claims: str | None = None) -> None:
        super().__init__(message)
        self.claims = claims


class SignInUnavailableError(DataSourceSignInError):
    """The token service could not be reached, or answered nonsense."""

    code = "DATA_SOURCE_SIGN_IN_UNAVAILABLE"


# --- what a sign-in takes ---------------------------------------------------


@dataclass(frozen=True)
class IdpClient:
    """Our own confidential client at the identity provider.

    Asks it for a warehouse token on a user's behalf, where the identity
    provider issues that token (Snowflake). The secret is left out of
    ``repr``, so it reaches no log line through this object.
    """

    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class LoginSource:
    """What signing in to one named connection as the user takes."""

    name: str
    dialect: str
    #: Databricks: the workspace whose ``/oidc/v1/token`` exchanges.
    host: str | None = None
    #: Databricks: only for a service-principal federation policy.
    client_id: str | None = None
    #: Snowflake: ``entra_obo`` or ``token_exchange``, the scope to ask for
    #: and, for RFC 8693, the target audience.
    exchange: str | None = None
    scope: str | None = None
    audience: str | None = None
    #: BigQuery: the workforce pool provider and the project billed.
    workforce_provider: str | None = None
    workforce_user_project: str | None = None

    @classmethod
    def from_entry(cls, entry: DatabaseEntry) -> LoginSource:
        """The sign-in settings of a named connection (``DB_<NAME>_...``)."""
        return cls(
            name=entry.name,
            dialect=entry.db_type,
            host=entry.getenv("DATABRICKS_SERVER_HOSTNAME"),
            client_id=entry.login_setting("OAUTH_CLIENT_ID"),
            exchange=entry.login_setting("OAUTH_EXCHANGE"),
            scope=entry.login_setting("OAUTH_SCOPE"),
            audience=entry.login_setting("OAUTH_AUDIENCE"),
            workforce_provider=entry.login_setting("WORKFORCE_PROVIDER"),
            workforce_user_project=entry.login_setting("WORKFORCE_USER_PROJECT")
            or entry.getenv("BIGQUERY_PROJECT_ID"),
        )


#: POSTs a form; answers (HTTP status, parsed JSON body or {}). Replaced in
#: tests.
Poster = Callable[
    [str, Mapping[str, str], tuple[str, str] | None],
    Awaitable[tuple[int, Any]],
]


async def post_form(
    url: str, form: Mapping[str, str], auth: tuple[str, str] | None = None
) -> tuple[int, Any]:
    """POST a token request. Never logs the form, which holds a token.

    Raises:
        SignInUnavailableError: When the service cannot be reached.
    """
    timeout = aiohttp.ClientTimeout(total=EXCHANGE_TIMEOUT_SECONDS)
    basic = aiohttp.BasicAuth(auth[0], auth[1]) if auth else None
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(url, data=dict(form), auth=basic) as response,
        ):
            try:
                body = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                body = {}
            return response.status, body
    except (aiohttp.ClientError, TimeoutError) as error:
        raise SignInUnavailableError(
            f"Could not reach the sign-in service at {url}: {type(error).__name__}."
        ) from None


# --- exchanges --------------------------------------------------------------


async def exchange(
    source: LoginSource,
    subject_token: str,
    idp: IdpClient | None = None,
    token_endpoint: str | None = None,
    post: Poster = post_form,
) -> tuple[str, float]:
    """A warehouse token for the user whose token *subject_token* is.

    Returns:
        The token and the seconds it is valid for.

    Raises:
        DataSourceSignInError: One of its kinds, saying what the user or the
            operator must do.
    """
    if source.dialect == "databricks":
        return await _exchange_databricks(source, subject_token, post)
    if source.dialect == "bigquery":
        return await _exchange_google_sts(source, subject_token, post)
    if source.dialect == "snowflake" and source.exchange == ENTRA_OBO:
        return await _exchange_entra_obo(
            source, subject_token, idp, token_endpoint, post
        )
    if source.dialect == "snowflake" and source.exchange == STANDARD_EXCHANGE:
        return await _exchange_at_idp(source, subject_token, idp, token_endpoint, post)
    raise SignInRefusedError(f"Database '{source.name}' cannot log in as the user.")


async def _exchange_databricks(
    source: LoginSource, subject_token: str, post: Poster
) -> tuple[str, float]:
    """RFC 8693 at the workspace: the user's token for a Databricks token.

    The account's federation policy decides which issuer and audience it
    accepts and which claim names the Databricks user. A service-principal
    policy also needs ``DB_<NAME>_OAUTH_CLIENT_ID``.
    """
    host = (source.host or "").strip().removeprefix("https://").rstrip("/")
    if not host:
        raise SignInUnavailableError(
            f"Database '{source.name}' has no DATABRICKS_SERVER_HOSTNAME to sign "
            "in at."
        )
    form = {
        "grant_type": _GRANT_EXCHANGE,
        "subject_token": subject_token,
        "subject_token_type": _TYPE_JWT,
        "scope": "all-apis",
    }
    if source.client_id:
        form["client_id"] = source.client_id
    return await _token_request(source, f"https://{host}/oidc/v1/token", form, post)


async def _exchange_google_sts(
    source: LoginSource, subject_token: str, post: Poster
) -> tuple[str, float]:
    """Workforce identity federation: the user's token for a Google token.

    Google's security token service checks the token against the workforce
    pool provider (``DB_<NAME>_WORKFORCE_PROVIDER``), and bills the exchange
    to ``DB_<NAME>_WORKFORCE_USER_PROJECT``, else the connection's project.
    No client authenticates: no secret of ours is involved.
    """
    form = {
        "grant_type": _GRANT_EXCHANGE,
        "audience": source.workforce_provider or "",
        "scope": _CLOUD_PLATFORM_SCOPE,
        "requested_token_type": _TYPE_ACCESS,
        "subject_token": subject_token,
        "subject_token_type": _TYPE_ID,
        "options": json.dumps({"userProject": source.workforce_user_project or ""}),
    }
    return await _token_request(source, _GOOGLE_STS, form, post)


async def _exchange_entra_obo(
    source: LoginSource,
    subject_token: str,
    idp: IdpClient | None,
    token_endpoint: str | None,
    post: Poster,
) -> tuple[str, float]:
    """Entra ID's on-behalf-of flow: the user's token for the warehouse's.

    Asked as our own client, the one the user's token was issued for, for
    ``DB_<NAME>_OAUTH_SCOPE`` -- for Snowflake its app ID URI and role scope,
    ``api://<Snowflake app ID>/session:role-any``.
    """
    client, endpoint = _client_at_idp(source, idp, token_endpoint)
    form = {
        "grant_type": _JWT_BEARER,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "assertion": subject_token,
        "scope": source.scope or "",
        "requested_token_use": _ON_BEHALF_OF,
    }
    return await _token_request(source, endpoint, form, post)


async def _exchange_at_idp(
    source: LoginSource,
    subject_token: str,
    idp: IdpClient | None,
    token_endpoint: str | None,
    post: Poster,
) -> tuple[str, float]:
    """The standard token exchange (RFC 8693) at the identity provider.

    Okta, Keycloak. Asked as our own client, for ``DB_<NAME>_OAUTH_SCOPE`` and,
    when set, ``DB_<NAME>_OAUTH_AUDIENCE``. The client authenticates with HTTP
    Basic, its id and secret form-encoded first (RFC 6749, 2.3.1).
    """
    client, endpoint = _client_at_idp(source, idp, token_endpoint)
    form = {
        "grant_type": _GRANT_EXCHANGE,
        "subject_token": subject_token,
        "subject_token_type": _TYPE_ACCESS,
        "scope": source.scope or "",
    }
    if source.audience:
        form["audience"] = source.audience
    basic = (quote(client.client_id, safe=""), quote(client.client_secret, safe=""))
    return await _token_request(source, endpoint, form, post, auth=basic)


def _client_at_idp(
    source: LoginSource, idp: IdpClient | None, token_endpoint: str | None
) -> tuple[IdpClient, str]:
    if idp is None or not token_endpoint:  # startup refuses this; defence in depth
        raise SignInUnavailableError(
            f"Database '{source.name}' logs in as the user through the identity "
            "provider, and this server has no client there (MCP_AUTH_CLIENT_ID) "
            "or no token endpoint."
        )
    return idp, token_endpoint


def _needs_sign_in(body: Mapping[str, Any]) -> bool:
    """Whether an error answer is one the user resolves by signing in again."""
    if body.get("error") in _INTERACTION_ERRORS:
        return True
    suberror = body.get("suberror")
    return (
        body.get("error") == "invalid_grant"
        and isinstance(suberror, str)
        and bool(suberror)
        and suberror not in _REFUSAL_SUBERRORS
    )


#: Form fields whose values are credentials: never repeated back.
_SECRET_FIELDS = ("subject_token", "assertion", "client_secret")


def _redact(text: str, secrets: list[str]) -> str:
    """*text* with every credential this server sent blanked out.

    An identity provider's error description is passed on to the user, and a
    service that echoes its input would otherwise hand the token back.
    """
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


async def _token_request(
    source: LoginSource,
    url: str,
    form: Mapping[str, str],
    post: Poster,
    auth: tuple[str, str] | None = None,
) -> tuple[str, float]:
    """POST a token request; the token it answers with, and for how long."""
    status, body = await post(url, form, auth)
    body = body if isinstance(body, dict) else {}
    if status >= 400:
        sent = [form.get(name, "") for name in _SECRET_FIELDS]
        if auth is not None:
            # Both as sent (form-encoded) and as they are.
            sent += [auth[1], unquote(auth[1])]
        error = _redact(str(body.get("error", f"HTTP {status}")), sent)
        description = _redact(str(body.get("error_description", "")), sent)
        detail = f"{error}: {description}" if description else error
        if status >= 500:
            raise SignInUnavailableError(
                f"The sign-in service of database '{source.name}' failed ({detail})."
            )
        if _needs_sign_in(body):
            claims = body.get("claims")
            raise SignInInteractionRequiredError(
                f"Database '{source.name}' needs the user to sign in again first "
                f"({detail}).",
                claims=(
                    _redact(claims, sent)
                    if isinstance(claims, str) and claims
                    else None
                ),
            )
        raise SignInRefusedError(
            f"Database '{source.name}' refused to log in this user ({detail}).",
            error=error if isinstance(body.get("error"), str) else None,
        )
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise SignInUnavailableError(
            f"The sign-in service of database '{source.name}' returned no access "
            "token."
        )
    try:
        seconds = float(body.get("expires_in", 3600))
    except (TypeError, ValueError):
        seconds = 3600.0
    logger.info(
        "Signed in to database '%s' as the user (%ds)", source.name, int(seconds)
    )
    return token, seconds


# --- the token over time ----------------------------------------------------


class UserLogin:
    """One user's warehouse token for one connection, renewed ahead of use.

    :meth:`renew` runs in async code before every call that may reach the
    database, with the request's token: it exchanges again when the warehouse
    token expires within :data:`RENEW_MARGIN_SECONDS`, one exchange at a time.
    :meth:`token` is what the driver reads at connect time, in a worker
    thread: it never exchanges, so an expired token there is a renewal that
    did not happen, not a request that can make one.

    Memory only. ``repr`` names the connection and the user, never a token.
    """

    def __init__(
        self,
        source: LoginSource,
        user: str,
        idp: IdpClient | None = None,
        token_endpoint: str | None = None,
        post: Poster | None = None,
    ) -> None:
        self.source = source
        #: Issuer and subject of the user, as the connection's identity.
        self.user = user
        self._idp = idp
        self._token_endpoint = token_endpoint
        self._post = post
        self._token: str | None = None
        self._expires_at = 0.0  # monotonic
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"UserLogin(source={self.source.name!r}, user={self.user!r})"

    @property
    def identity(self) -> str:
        """Who the connection is: the user, and how the connection signs in.

        The user alone is not enough: two connections to one warehouse that
        sign in differently -- another service principal, scope, exchange or
        workforce provider -- see different things, and must not share a
        runtime, a manager or its token. Only settings, never a token, so a
        renewed token keeps the workspace.
        """
        source = self.source
        how = "|".join(
            f"{name}={value}"
            for name, value in (
                ("dialect", source.dialect),
                ("client_id", source.client_id),
                ("exchange", source.exchange),
                ("scope", source.scope),
                ("audience", source.audience),
                ("workforce_provider", source.workforce_provider),
                ("workforce_user_project", source.workforce_user_project),
            )
            if value
        )
        return f"{self.user}#{how}"

    def _fresh(self) -> bool:
        return (
            self._token is not None
            and self._expires_at - RENEW_MARGIN_SECONDS > time.monotonic()
        )

    async def renew(self, caller: str | None, subject_token: str | None) -> None:
        """Make sure the warehouse token outlasts the coming call.

        Args:
            caller: The current request's user (issuer and subject).
            subject_token: The current request's verified bearer token.

        Raises:
            SignInRequiredError: Without a token, or for another user: a login
                is renewed only with its own user's token.
            DataSourceSignInError: When the exchange fails.
        """
        if caller != self.user:
            # The session layer keeps users apart; this keeps a slip there
            # from giving one user's connection another user's token.
            raise SignInRequiredError(
                f"This connection to database '{self.source.name}' belongs to "
                "another user."
            )
        if self._fresh():
            return
        if not subject_token:
            raise SignInRequiredError(
                f"Database '{self.source.name}' logs in as the signed-in user; "
                "call with the identity provider's access token."
            )
        async with self._lock:
            if self._fresh():
                return
            token, seconds = await exchange(
                self.source,
                subject_token,
                self._idp,
                self._token_endpoint,
                # Looked up now, not when the login was made: one place to
                # replace it (tests).
                self._post or post_form,
            )
            self._token = token
            self._expires_at = time.monotonic() + seconds

    def token(self) -> str:
        """The current warehouse token, for the driver. Never exchanges.

        Raises:
            SignInRequiredError: When there is none, or it has expired.
        """
        if self._token is None or self._expires_at <= time.monotonic():
            raise SignInRequiredError(
                f"The sign-in to database '{self.source.name}' has expired; call "
                "again with a current access token."
            )
        return self._token


# --- what startup refuses ---------------------------------------------------


def check_sources(entries: list[DatabaseEntry], auth_mode: str) -> list[str]:
    """Refuse a connection that cannot log in as the user, as configured.

    Args:
        entries: The configured databases.
        auth_mode: ``MCP_AUTH``.

    Returns:
        Warnings: settings that a user login ignores.

    Raises:
        ValueError: ``DB_<NAME>_AUTH=user`` without ``MCP_AUTH=jwt``, on a
            database that cannot log in as the user, or without what its
            exchange needs.
    """
    warnings: list[str] = []
    for entry in entries:
        if not entry.logs_in_as_user:
            continue
        where = f"Database '{entry.name}' ({entry.prefix}AUTH=user)"
        if auth_mode != "jwt":
            raise ValueError(
                f"{where} logs in as the signed-in user, which needs "
                "MCP_AUTH=jwt: without tokens there is no user to log in as."
            )
        if entry.db_type not in USER_LOGIN_DIALECTS:
            raise ValueError(
                f"{where} is {entry.db_type}, which cannot log in as the user in "
                f"this release (supported: {', '.join(USER_LOGIN_DIALECTS)})."
            )
        if entry.db_type == "databricks":
            missing = [
                v
                for v in ("DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH")
                if not entry.getenv(v)
            ]
            if missing:
                raise ValueError(f"{where} needs {' and '.join(missing)}.")
            if entry.getenv("DATABRICKS_ACCESS_TOKEN"):
                warnings.append(
                    f"{where}: DATABRICKS_ACCESS_TOKEN is ignored; each user's "
                    "own token is used."
                )
    return warnings
