"""Bearer-token authentication, and the session ownership it makes possible.

With MCP_AUTH=jwt every HTTP request needs a token from the configured issuer
for this server's audience. The token's issuer and subject name the user, and
a session belongs to the user who opened it: nobody else resolves to it, by
connection handle, transport session or the sole-session fallback.
"""

import re
import time

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.utilities.tests import asgi_client, asgi_server
from joserfc import jwt
from joserfc.jwk import OctKey

import src.main as main_module
import src.server_state as state_module
from src.auth import build_auth_provider, user_key
from src.config import AuthConfig, ConfigManager
from src.exceptions import UnknownConnectionError
from src.handlers import connection as connection_handler
from src.main import mcp
from src.server_state import ServerState, get_session_data

SECRET = "an-hs256-test-secret-of-at-least-32-bytes!"
ISSUER = "https://issuer.example"
AUDIENCE = "orionbelt-analytics"


def _jwt_config(**overrides) -> AuthConfig:
    settings = {
        "mode": "jwt",
        "public_key": SECRET,
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "algorithm": "HS256",
    }
    settings.update(overrides)
    return AuthConfig(**settings)


def _token(
    subject: str | None, audience: str = AUDIENCE, issuer: str = ISSUER, **extra
) -> str:
    now = int(time.time())
    claims = {"iss": issuer, "aud": audience, "iat": now, **extra}
    if subject is not None:
        claims["sub"] = subject
    claims["exp"] = now + 300
    return jwt.encode({"alg": "HS256"}, claims, OctKey.import_key(SECRET))


# --- configuration -----------------------------------------------------------


def test_auth_is_off_by_default():
    assert build_auth_provider(AuthConfig()) is None


def test_an_unknown_mode_fails_closed(monkeypatch):
    """A typo must stop the server, not run it unauthenticated."""
    monkeypatch.setenv("MCP_AUTH", "jwy")
    with pytest.raises(ValueError, match="MCP_AUTH"):
        ConfigManager().validate_config()
    with pytest.raises(ValueError, match="MCP_AUTH"):
        build_auth_provider(AuthConfig(mode="jwy"))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"public_key": None}, "exactly one of"),
        ({"jwks_uri": "https://issuer.example/jwks"}, "exactly one of"),
        ({"issuer": None}, "MCP_AUTH_ISSUER"),
        ({"audience": None}, "MCP_AUTH_AUDIENCE"),
    ],
    ids=["no-key", "two-keys", "no-issuer", "no-audience"],
)
def test_jwt_mode_refuses_incomplete_settings(overrides, message):
    with pytest.raises(ValueError, match=message):
        _jwt_config(**overrides).validate()


def test_a_shared_secret_algorithm_cannot_use_a_key_set():
    config = _jwt_config(public_key=None, jwks_uri="https://issuer.example/jwks")
    with pytest.raises(ValueError, match="shared-secret"):
        config.validate()


def test_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MCP_AUTH", "JWT")
    monkeypatch.setenv("MCP_AUTH_JWKS_URI", "https://issuer.example/jwks")
    monkeypatch.setenv("MCP_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("MCP_AUTH_REQUIRED_SCOPES", "read:schema, run:sql")
    config = ConfigManager().get_auth_config()
    assert config.mode == "jwt"
    assert config.required_scopes == ["read:schema", "run:sql"]
    assert isinstance(build_auth_provider(config), JWTVerifier)


@pytest.mark.parametrize(
    ("given", "canonical"),
    [
        ("EdDSA", "EdDSA"),
        ("Ed25519", "Ed25519"),
        ("ed448", "Ed448"),
        ("EDDSA", "EdDSA"),
        ("es256", "ES256"),
        (" RS256 ", "RS256"),
    ],
)
def test_the_algorithm_keeps_its_canonical_spelling(monkeypatch, given, canonical):
    """Mixed-case names must reach JWTVerifier as it spells them."""
    monkeypatch.setenv("MCP_AUTH", "jwt")
    monkeypatch.setenv("MCP_AUTH_PUBLIC_KEY", _ed25519_public_pem())
    monkeypatch.setenv("MCP_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("MCP_AUTH_ALGORITHM", given)
    config = ConfigManager().get_auth_config()
    assert config.algorithm == canonical
    assert build_auth_provider(config).algorithm == canonical


def test_an_unknown_algorithm_fails_validation():
    with pytest.raises(ValueError, match="MCP_AUTH_ALGORITHM"):
        _jwt_config(algorithm="none").validate()


def _ed25519_public_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    public = Ed25519PrivateKey.generate().public_key()
    return public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def test_a_resource_url_advertises_the_issuer_for_login():
    provider = build_auth_provider(_jwt_config(resource_url="https://oba.example"))
    assert isinstance(provider, RemoteAuthProvider)


def test_the_user_is_issuer_and_subject():
    class Token:
        def __init__(self, claims):
            self.claims = claims

    assert user_key(Token({"iss": ISSUER, "sub": "alice"})) == f"{ISSUER}|alice"
    assert user_key(Token({"iss": ISSUER})) is None
    assert user_key(Token({"iss": ISSUER, "sub": "  "})) is None
    assert user_key(Token({"iss": ISSUER, "sub": 42})) is None
    assert user_key(None) is None


# --- session ownership -------------------------------------------------------


@pytest.fixture
def state(monkeypatch) -> ServerState:
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    yield fresh
    fresh.cleanup()


def _as(monkeypatch, user: str | None) -> None:
    monkeypatch.setattr(state_module, "current_user", lambda: user)


class _Ctx:
    session_id = None


def test_a_handle_only_resolves_for_its_owner(state, monkeypatch):
    _as(monkeypatch, "alice")
    session = state.open_handle_session()
    token = state_module.begin_connection_scope(_Ctx(), session.handle, mint=False)
    try:
        assert get_session_data(_Ctx()) is session
        _as(monkeypatch, "bob")
        with pytest.raises(UnknownConnectionError, match="Unknown or expired"):
            get_session_data(_Ctx())
    finally:
        state_module.end_connection_scope(token)


def test_a_transport_session_only_resolves_for_its_owner(state, monkeypatch):
    monkeypatch.setattr(state_module, "_transport_session_id", lambda _ctx: "s-1")
    _as(monkeypatch, "alice")
    session = get_session_data(_Ctx())
    assert session.owner == "alice"
    _as(monkeypatch, "bob")
    with pytest.raises(UnknownConnectionError, match="another user"):
        get_session_data(_Ctx())


def test_another_users_session_is_never_the_sole_one(state, monkeypatch):
    monkeypatch.setattr(state_module, "_sessionless_fallback_enabled", lambda: True)
    monkeypatch.setattr(state_module, "_transport_session_id", lambda _ctx: None)
    _as(monkeypatch, "alice")
    alices = state.open_handle_session()
    assert get_session_data(_Ctx()) is alices
    _as(monkeypatch, "bob")
    with pytest.raises(Exception, match="connection"):
        get_session_data(_Ctx())


def test_without_auth_nothing_changes(state):
    session = state.open_handle_session()
    assert session.owner is None


# --- end to end over HTTP ----------------------------------------------------


@pytest.fixture
def secured(state, monkeypatch, tmp_path):
    """The real server behind token verification, with an in-memory DuckDB."""
    monkeypatch.setattr(mcp, "auth", build_auth_provider(_jwt_config()))
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.delenv("DUCKDB_DATABASE_PATH", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("AUTO_GRAPHRAG", "false")
    return state


def _handle_in(text: str) -> str:
    match = re.search(r"Connection handle: (ob_[a-z0-9]{6})", text)
    assert match, text
    return match.group(1)


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.mark.parametrize(
    "token",
    [
        None,
        "not-a-jwt",
        "wrong-audience",
        "wrong-issuer",
        "wrong-secret",
        "no-subject",
        "empty-subject",
    ],
)
async def test_requests_without_a_valid_token_get_401(secured, token):
    bad = {
        "wrong-audience": lambda: _token("alice", audience="another-service"),
        "wrong-issuer": lambda: _token("alice", issuer="https://elsewhere.example"),
        # Signed and otherwise valid, but naming nobody: all such tokens
        # would share one identity and so each other's sessions.
        "no-subject": lambda: _token(None, client_id="client-a"),
        "empty-subject": lambda: _token("", client_id="client-a"),
        "wrong-secret": lambda: jwt.encode(
            {"alg": "HS256"},
            {"sub": "alice", "iss": ISSUER, "aud": AUDIENCE, "exp": 2**31},
            OctKey.import_key("x" * 48),
        ),
    }
    value = bad[token]() if token in bad else token
    headers = {"Accept": "application/json, text/event-stream"}
    if value:
        headers["Authorization"] = f"Bearer {value}"
    async with asgi_server(mcp) as server, server.http_client() as http:
        response = await http.post(server.url, json=_INITIALIZE, headers=headers)
    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")


async def test_a_valid_token_gets_through(secured):
    async with asgi_client(mcp, auth=_token("alice")) as client:
        assert any(t.name == "connect_database" for t in await client.list_tools())


async def test_one_user_cannot_use_another_users_handle(secured):
    async with asgi_client(mcp, auth=_token("alice")) as alice:
        connected = await alice.call_tool("connect_database", {"db_type": "duckdb"})
        handle = _handle_in(connected.data)
        schemas = await alice.call_tool("list_schemas", {"connection": handle})
        assert "main" in schemas.data

    async with asgi_client(mcp, auth=_token("bob")) as bob:
        with pytest.raises(ToolError, match="Unknown or expired"):
            await bob.call_tool("list_schemas", {"connection": handle})

    assert secured.session_for_handle(handle).owner == f"{ISSUER}|alice"
