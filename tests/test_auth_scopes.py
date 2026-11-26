"""Scopes from a token's claims, the issuer's discovery document, and tool tiers.

Identity providers carry scopes and roles in different claims and name them
in their own terms; MCP_AUTH_SCOPE_CLAIMS says which claims to read and
MCP_AUTH_SCOPE_MAP which of their values grant OrionBelt's scopes. With
MCP_AUTH_TOOL_SCOPES=true each tool needs its tier: oba:read, oba:write or
oba:admin, each including the ones below. Without a key set or a key, the key
set comes from the issuer's discovery document.
"""

import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration
from fastmcp.utilities.authorization import AuthContext
from fastmcp.utilities.tests import asgi_client
from joserfc import jwt
from joserfc.jwk import OctKey

import src.main as main_module
import src.server_state as state_module
from src.auth import (
    ToolScopeCheck,
    build_auth_provider,
    tool_scope_middleware,
    with_discovery,
)
from src.config import AuthConfig, ConfigManager
from src.handlers import connection as connection_handler
from src.main import mcp
from src.scopes import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    TOOL_SCOPES,
    parse_scope_claims,
    parse_scope_map,
    scopes_from_claims,
)
from src.server_state import ServerState

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


def _token(subject: str = "alice", **extra) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300}
    claims.update({"sub": subject, **extra})
    return jwt.encode({"alg": "HS256"}, claims, OctKey.import_key(SECRET))


# --- scopes from claims --------------------------------------------------------


def test_each_scope_brings_the_ones_below_it():
    assert scopes_from_claims({"scope": "oba:admin"}) == sorted(
        [SCOPE_ADMIN, SCOPE_WRITE, SCOPE_READ]
    )
    assert scopes_from_claims({"scope": "oba:write"}) == [SCOPE_READ, SCOPE_WRITE]


def test_scopes_and_roles_are_read_from_every_claim_named():
    claims = {
        "scope": "openid oba:read",
        "scp": ["mcp:access"],
        "roles": ["oba:write"],
        "realm_access": {"roles": ["oba:admin"]},
    }
    # The defaults: scope, scp and roles, merged; values kept as they are.
    assert scopes_from_claims(claims) == sorted(
        ["openid", "oba:read", "mcp:access", "oba:write"]
    )
    # A dot reaches into an object, as Keycloak nests its roles.
    assert SCOPE_ADMIN in scopes_from_claims(claims, ("realm_access.roles",))
    # Anything else is no scope.
    assert scopes_from_claims({"scope": 42, "roles": [1, None]}) == []


def test_a_mapped_value_grants_what_it_maps_to():
    scope_map = parse_scope_map("analysts=oba:read, modelers=oba:write")
    granted = scopes_from_claims({"roles": ["modelers"]}, scope_map=scope_map)
    assert granted == ["modelers", SCOPE_READ, SCOPE_WRITE]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("analysts", "not of the form"),
        ("=oba:read", "not of the form"),
        ("analysts=", "not of the form"),
        ("analysts=read", "not an OrionBelt scope"),
    ],
)
def test_a_malformed_scope_map_is_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_scope_map(raw)


def test_scope_claims_must_name_one():
    assert parse_scope_claims(" permissions , realm_access.roles ") == (
        "permissions",
        "realm_access.roles",
    )
    with pytest.raises(ValueError, match="names no claim"):
        parse_scope_claims(" , ")


# --- settings -----------------------------------------------------------------


def _settings(monkeypatch, **env: str) -> AuthConfig:
    for name in (
        "MCP_AUTH",
        "MCP_AUTH_SCOPE_CLAIMS",
        "MCP_AUTH_SCOPE_MAP",
        "MCP_AUTH_TOOL_SCOPES",
        "MCP_AUTH_TOKEN_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return ConfigManager().get_auth_config()


def test_scope_settings_are_read_from_the_environment(monkeypatch):
    config = _settings(
        monkeypatch,
        MCP_AUTH="jwt",
        MCP_AUTH_SCOPE_CLAIMS="permissions",
        MCP_AUTH_SCOPE_MAP="analysts=oba:read",
        MCP_AUTH_TOOL_SCOPES="true",
        MCP_AUTH_TOKEN_ENDPOINT="https://issuer.example/token",
    )
    assert config.scope_claims == ("permissions",)
    assert config.scope_map == {"analysts": frozenset({SCOPE_READ})}
    assert config.tool_scopes is True
    assert config.token_endpoint == "https://issuer.example/token"


def test_a_malformed_scope_map_stops_only_a_server_that_reads_tokens(monkeypatch):
    assert _settings(monkeypatch, MCP_AUTH_SCOPE_MAP="nonsense").scope_map == {}
    with pytest.raises(ValueError, match="MCP_AUTH_SCOPE_MAP"):
        _settings(monkeypatch, MCP_AUTH="jwt", MCP_AUTH_SCOPE_MAP="nonsense")


def test_tool_scopes_without_tokens_is_refused():
    with pytest.raises(ValueError, match="MCP_AUTH_TOOL_SCOPES needs MCP_AUTH=jwt"):
        AuthConfig(tool_scopes=True).validate()


def test_tool_scopes_are_off_by_default(monkeypatch):
    """Tokens issued before the tiers existed carry none of their scopes."""
    assert _settings(monkeypatch, MCP_AUTH="jwt").tool_scopes is False
    assert tool_scope_middleware(_jwt_config()) == []
    (middleware,) = tool_scope_middleware(_jwt_config(tool_scopes=True))
    assert isinstance(middleware.auth, ToolScopeCheck)


def test_a_key_set_and_a_key_together_are_refused():
    with pytest.raises(ValueError, match="not both"):
        _jwt_config(
            jwks_uri="https://issuer.example/jwks", algorithm="RS256"
        ).validate()


def test_a_shared_secret_cannot_come_from_discovery():
    with pytest.raises(ValueError, match="shared-secret"):
        _jwt_config(public_key=None).validate()


# --- discovery ----------------------------------------------------------------


def _discovered(monkeypatch, document=None, error=None) -> list[str]:
    asked: list[str] = []

    def fetch(url, *, strict, timeout_seconds):
        asked.append(str(url))
        if error is not None:
            raise error
        return OIDCConfiguration(strict=False, **document)

    monkeypatch.setattr(
        OIDCConfiguration, "get_oidc_configuration", staticmethod(fetch)
    )
    return asked


def _rs256(**overrides) -> AuthConfig:
    return _jwt_config(public_key=None, algorithm="RS256", **overrides)


def test_without_a_key_set_or_key_the_issuer_says_where_its_keys_are(monkeypatch):
    asked = _discovered(
        monkeypatch,
        {
            "issuer": ISSUER,
            "jwks_uri": "https://issuer.example/keys",
            "token_endpoint": "https://issuer.example/token",
        },
    )
    config = with_discovery(_rs256())
    assert asked == [f"{ISSUER}/.well-known/openid-configuration"]
    assert config.jwks_uri == "https://issuer.example/keys"
    assert config.token_endpoint == "https://issuer.example/token"
    # A configured token endpoint wins over the document's.
    kept = with_discovery(_rs256(token_endpoint="https://elsewhere.example/t"))
    assert kept.token_endpoint == "https://elsewhere.example/t"
    # And the provider built from it verifies against that key set.
    provider = build_auth_provider(_rs256())
    assert provider is not None and provider.jwks_uri == config.jwks_uri


def test_a_configured_key_set_or_key_is_not_rediscovered(monkeypatch):
    asked = _discovered(monkeypatch, error=AssertionError("fetched"))
    with_discovery(_jwt_config())
    with_discovery(_rs256(jwks_uri="https://issuer.example/keys"))
    assert asked == []


@pytest.mark.parametrize(
    ("document", "error", "message"),
    [
        (None, OSError("connection refused"), "which failed: OSError"),
        (
            {"issuer": "https://impostor.example", "jwks_uri": "https://x/keys"},
            None,
            "names the issuer",
        ),
        ({"issuer": ISSUER}, None, "names no key set"),
        ({"issuer": ISSUER, "jwks_uri": "file:///etc/keys"}, None, "no key set"),
    ],
)
def test_discovery_that_cannot_be_trusted_stops_the_server(
    monkeypatch, document, error, message
):
    _discovered(monkeypatch, document, error)
    with pytest.raises(ValueError, match=message):
        build_auth_provider(_rs256())


# --- the verifier's scopes ----------------------------------------------------


async def test_the_verifier_reads_and_maps_scopes():
    provider = build_auth_provider(
        _jwt_config(scope_map=parse_scope_map("analysts=oba:read"))
    )
    access = await provider.load_access_token(_token(roles=["analysts"]))
    assert access is not None
    assert access.scopes == ["analysts", SCOPE_READ]


@pytest.mark.parametrize(
    ("required", "claims", "accepted"),
    [
        # An identity provider's own scope still satisfies the requirement.
        (["mcp:access"], {"scope": "mcp:access"}, True),
        # A mapped role satisfies an OrionBelt one.
        ([SCOPE_READ], {"roles": ["analysts"]}, True),
        ([SCOPE_READ], {"roles": ["visitors"]}, False),
    ],
)
async def test_required_scopes_see_the_mapped_scopes(required, claims, accepted):
    provider = build_auth_provider(
        _jwt_config(
            required_scopes=required, scope_map=parse_scope_map("analysts=oba:read")
        )
    )
    access = await provider.load_access_token(_token(**claims))
    assert (access is not None) is accepted


# --- tool tiers ---------------------------------------------------------------


async def test_every_tool_has_a_tier():
    """A tool added without one would be refused to everyone: add it here."""
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == set(TOOL_SCOPES)
    assert set(TOOL_SCOPES.values()) == {SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN}


@pytest.fixture
def tiered(monkeypatch, tmp_path):
    """The real server, its tools tiered, the tokens' roles mapped."""
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    config = _jwt_config(
        tool_scopes=True,
        scope_map=parse_scope_map(
            "analysts=oba:read,modelers=oba:write,operators=oba:admin"
        ),
    )
    monkeypatch.setattr(mcp, "auth", build_auth_provider(config))
    monkeypatch.setattr(
        mcp, "middleware", [*mcp.middleware, *tool_scope_middleware(config)]
    )
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.delenv("DUCKDB_DATABASE_PATH", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("AUTO_GRAPHRAG", "false")
    yield fresh
    fresh.cleanup()


@pytest.mark.parametrize(
    ("role", "tiers"),
    [
        ("visitors", set()),
        ("analysts", {SCOPE_READ}),
        ("modelers", {SCOPE_READ, SCOPE_WRITE}),
        ("operators", {SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN}),
    ],
)
async def test_a_caller_sees_the_tools_of_their_tiers(tiered, role, tiers):
    async with asgi_client(mcp, auth=_token(roles=[role])) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {name for name, tier in TOOL_SCOPES.items() if tier in tiers}


async def test_a_call_beyond_the_callers_tier_is_refused(tiered):
    async with asgi_client(mcp, auth=_token(roles=["analysts"])) as client:
        connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
        assert "Connection handle" in connected.data
        with pytest.raises(ToolError, match=r"insufficient scope.*oba:admin"):
            await client.call_tool("reset_cache", {})


async def test_a_shortfall_names_the_scope_to_ask_for():
    """Scope-aware: FastMCP answers it as insufficient_scope, not a bare no."""

    class _Token:
        scopes = [SCOPE_READ]

    tools = await mcp.list_tools(run_middleware=False)
    reset = next(tool for tool in tools if tool.name == "reset_cache")
    connect = next(tool for tool in tools if tool.name == "connect_database")
    check = ToolScopeCheck()
    denied = AuthContext(token=_Token(), component=reset)  # type: ignore[arg-type]
    assert check(denied) is False
    assert check.missing_scopes(denied) == {SCOPE_ADMIN}
    allowed = AuthContext(token=_Token(), component=connect)  # type: ignore[arg-type]
    assert check(allowed) is True and check.missing_scopes(allowed) == set()
    # No token is a matter of signing in, not of a scope.
    anonymous = AuthContext(token=None, component=reset)
    assert check(anonymous) is False and check.missing_scopes(anonymous) == set()


# --- review: literal claim names, exact issuers -------------------------------


def test_a_claim_named_like_a_url_is_read_as_named():
    """Auth0 namespaces custom claims with a URL, dots and all."""
    claim = "https://example.com/roles"
    scope_map = parse_scope_map("analysts=oba:read")
    granted = scopes_from_claims({claim: ["analysts"]}, (claim,), scope_map)
    assert SCOPE_READ in granted
    # A dot path still reaches into objects when no claim has that exact name.
    nested = {"realm_access": {"roles": ["analysts"]}}
    assert SCOPE_READ in scopes_from_claims(nested, ("realm_access.roles",), scope_map)


async def test_a_signed_token_with_a_url_claim_is_accepted():
    claim = "https://example.com/roles"
    provider = build_auth_provider(
        _jwt_config(
            scope_claims=(claim,),
            scope_map=parse_scope_map("analysts=oba:read"),
            required_scopes=[SCOPE_READ],
        )
    )
    access = await provider.load_access_token(_token(**{claim: ["analysts"]}))
    assert access is not None and SCOPE_READ in access.scopes


@pytest.mark.parametrize(
    ("configured", "discovered"),
    [(ISSUER, ISSUER + "/"), (ISSUER + "/", ISSUER)],
)
def test_an_issuer_that_differs_by_a_slash_stops_the_server(
    monkeypatch, configured, discovered
):
    """Tokens are checked against MCP_AUTH_ISSUER exactly; so is the document."""
    _discovered(
        monkeypatch, {"issuer": discovered, "jwks_uri": "https://issuer.example/k"}
    )
    with pytest.raises(ValueError, match="exactly the issuer"):
        build_auth_provider(_rs256(issuer=configured))
