"""Snowflake logging in as the signed-in user (External OAuth).

The identity provider issues the Snowflake token, asked by this server's own
client there: Entra ID's on-behalf-of flow, or the standard token exchange of
Okta and Keycloak. The token says who connects -- no user, no password -- and
each connection the pool opens reads the current one.
"""

import json
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration

import src.auth as auth_module
import src.drivers.snowflake as snowflake_driver
import src.main as main_module
import src.server_state as state_module
import src.warehouse_login as login_module
from src.auth import build_auth_provider, sign_in_client, with_discovery
from src.config import AuthConfig, ConfigManager
from src.database_manager import DatabaseManager
from src.database_registry import configured_databases
from src.handlers import connection as connection_handler
from src.main import mcp
from src.server_state import ServerState, _get_connection_fingerprint
from src.warehouse_login import (
    IdpClient,
    LoginSource,
    UserLogin,
    check_sources,
)

ALICE = "https://login.example.com/|alice"
SUBJECT = "eyJ.alices-token.sig"
SECRET = "an-hs256-test-secret-of-at-least-32-bytes!"

SF = {
    "OBA_DATABASES": "sf",
    "DB_SF_TYPE": "snowflake",
    "DB_SF_AUTH": "user",
    "DB_SF_OAUTH_EXCHANGE": "entra_obo",
    "DB_SF_OAUTH_SCOPE": "api://sf-app/session:role-any",
    "DB_SF_SNOWFLAKE_ACCOUNT": "myorg-acct",
    "DB_SF_SNOWFLAKE_WAREHOUSE": "WH",
    "DB_SF_SNOWFLAKE_DATABASE": "SALES",
    "DB_SF_SNOWFLAKE_SCHEMA": "PUBLIC",
}


class _TokenService:
    def __init__(self, answer: tuple[int, Any] | None = None) -> None:
        self.answer = answer or (200, {"access_token": "sf-1", "expires_in": 3600})
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, form: Any, auth: Any) -> tuple[int, Any]:
        self.calls.append({"url": url, "form": dict(form), "auth": auth})
        return self.answer


# --- our client's secret --------------------------------------------------------


def _auth_settings(monkeypatch, **env: str) -> AuthConfig:
    for name in (
        "MCP_AUTH_CLIENT_ID",
        "MCP_AUTH_CLIENT_SECRET",
        "MCP_AUTH_CLIENT_SECRET_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCP_AUTH", "jwt")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return ConfigManager().get_auth_config()


def test_the_secret_comes_from_a_file_and_stays_out_of_repr(monkeypatch, tmp_path):
    path = tmp_path / "secret"
    path.write_text("  s3cret-from-file\n")
    config = _auth_settings(
        monkeypatch,
        MCP_AUTH_CLIENT_ID="our-client",
        MCP_AUTH_CLIENT_SECRET_FILE=str(path),
    )
    assert config.client_secret == "s3cret-from-file" and config.has_client
    assert "s3cret" not in repr(config)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            {"MCP_AUTH_CLIENT_SECRET": "a", "MCP_AUTH_CLIENT_SECRET_FILE": "b"},
            "not both",
        ),
        ({"MCP_AUTH_CLIENT_SECRET_FILE": "/no/such/file"}, "Could not read"),
    ],
)
def test_a_secret_that_cannot_be_read_stops_the_server(monkeypatch, env, message):
    with pytest.raises(ValueError, match=message):
        _auth_settings(monkeypatch, **env)


# --- what startup refuses -------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "has_client", "message"),
    [
        ({"DB_SF_OAUTH_EXCHANGE": ""}, True, "needs DB_SF_OAUTH_EXCHANGE"),
        ({"DB_SF_OAUTH_EXCHANGE": "magic"}, True, "needs DB_SF_OAUTH_EXCHANGE"),
        ({}, False, "MCP_AUTH_CLIENT_ID"),
        ({"DB_SF_OAUTH_SCOPE": ""}, True, "needs DB_SF_OAUTH_SCOPE"),
        ({"DB_SF_SNOWFLAKE_WAREHOUSE": ""}, True, "SNOWFLAKE_WAREHOUSE"),
    ],
)
def test_startup_refuses_a_snowflake_login_it_cannot_make(env, has_client, message):
    with pytest.raises(ValueError, match=message):
        check_sources(configured_databases({**SF, **env}), "jwt", has_client)


def test_a_token_exchange_at_okta_or_keycloak_needs_no_scope_of_its_own():
    env = {**SF, "DB_SF_OAUTH_EXCHANGE": "token_exchange", "DB_SF_OAUTH_SCOPE": ""}
    assert check_sources(configured_databases(env), "jwt", True) == []


def test_a_configured_user_and_password_are_ignored_and_said_so():
    env = {**SF, "SNOWFLAKE_USERNAME": "svc", "SNOWFLAKE_PASSWORD": "pw-123"}
    warnings = check_sources(configured_databases(env), "jwt", True)
    assert len(warnings) == 2 and all("ignored" in w for w in warnings)
    assert not any("pw-123" in w for w in warnings)


# --- the token endpoint ---------------------------------------------------------


def _discovered(monkeypatch, document: dict[str, Any]) -> list[str]:
    asked: list[str] = []

    def fetch(url, *, strict, timeout_seconds):
        asked.append(str(url))
        return OIDCConfiguration(strict=False, **document)

    monkeypatch.setattr(
        OIDCConfiguration, "get_oidc_configuration", staticmethod(fetch)
    )
    return asked


def _jwt(**overrides) -> AuthConfig:
    settings = {
        "mode": "jwt",
        "public_key": SECRET,
        "issuer": "https://login.example.com/",
        "audience": "orionbelt-analytics",
        "algorithm": "HS256",
        "client_id": "our-client",
        "client_secret": "s3cret",
    }
    settings.update(overrides)
    return AuthConfig(**settings)


def test_the_token_endpoint_is_discovered_only_when_a_login_needs_it(monkeypatch):
    asked = _discovered(
        monkeypatch,
        {
            "issuer": "https://login.example.com/",
            "token_endpoint": "https://login.example.com/token",
        },
    )
    # Keys configured, endpoint not needed: nothing is read.
    assert with_discovery(_jwt()).token_endpoint is None and asked == []
    # Needed: read, the configured key untouched.
    config = with_discovery(_jwt(), need_token_endpoint=True)
    assert config.token_endpoint == "https://login.example.com/token"
    assert config.public_key == SECRET and config.jwks_uri is None
    # Set explicitly: not read again.
    explicit = _jwt(token_endpoint="https://elsewhere.example/t")
    assert with_discovery(explicit, need_token_endpoint=True) is explicit


def test_a_document_without_a_token_endpoint_stops_the_server(monkeypatch):
    _discovered(monkeypatch, {"issuer": "https://login.example.com/"})
    with pytest.raises(ValueError, match="names no token endpoint"):
        with_discovery(_jwt(), need_token_endpoint=True)


def test_the_sign_in_client_is_what_startup_resolved(monkeypatch):
    for name, value in SF.items():
        monkeypatch.setenv(name, value)
    asked = _discovered(
        monkeypatch,
        {
            "issuer": "https://login.example.com/",
            "token_endpoint": "https://login.example.com/token",
        },
    )
    monkeypatch.setattr(auth_module, "_resolved", None)
    build_auth_provider(_jwt())
    client, endpoint = sign_in_client()
    assert asked and endpoint == "https://login.example.com/token"
    assert client == IdpClient("our-client", "s3cret")


# --- the driver -----------------------------------------------------------------


def test_the_engine_connects_by_oauth_with_the_current_token(monkeypatch):
    """No user or password in the URL; the token read as each connection opens."""
    from sqlalchemy import create_engine as real_create_engine
    from sqlalchemy import event as real_event
    from sqlalchemy.pool import NullPool

    made: list[dict[str, Any]] = []
    seen: list[str] = []

    def sqlite_engine(url: Any, **kwargs: Any) -> Any:
        made.append({"url": url, "connect_args": kwargs["connect_args"]})
        return real_create_engine("sqlite://", poolclass=NullPool)

    class _Event:
        @staticmethod
        def listens_for(target: Any, name: str) -> Any:
            def register(fn: Any) -> Any:
                def then_record(dialect, record, args, kwargs):
                    fn(dialect, record, args, kwargs)
                    seen.append(kwargs.pop("token"))  # sqlite takes none

                real_event.listen(target, name, then_record)
                return fn

            return register

    monkeypatch.setattr(snowflake_driver, "create_engine", sqlite_engine)
    monkeypatch.setattr(snowflake_driver, "event", _Event)
    tokens = iter(["sf-1", "sf-2"])
    driver = snowflake_driver.SnowflakeDriver()
    assert driver.connect(
        account="myorg-acct",
        username="ignored-svc",
        password="ignored-pw",
        token_provider=lambda: next(tokens),
        warehouse="WH",
        database="SALES",
    )
    with driver.engine.connect():
        pass
    assert seen == ["sf-1", "sf-2"]
    (engine,) = made
    assert engine["url"].username is None and engine["url"].password is None
    assert engine["connect_args"]["authenticator"] == "oauth"
    assert "token" not in engine["connect_args"]


# --- who the connection is ------------------------------------------------------


class _FakeDriver:
    given: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.engine = None

    def connect(self, **params: Any) -> bool:
        _FakeDriver.given.append(params)
        return True

    def disconnect(self) -> None:
        pass


async def test_a_snowflake_login_is_the_users_own_connection(monkeypatch):
    monkeypatch.setattr(snowflake_driver, "SnowflakeDriver", _FakeDriver)
    _FakeDriver.given = []
    (entry,) = configured_databases(SF)
    managers = []
    for user in (ALICE, "https://login.example.com/|bob"):
        login = UserLogin(
            LoginSource.from_entry(entry),
            user,
            idp=IdpClient("our-client", "s3cret"),
            token_endpoint="https://login.example.com/token",
            post=_TokenService(),
        )
        await login.renew(user, SUBJECT)
        manager = DatabaseManager()
        monkeypatch.setattr(manager, "_sync_engine_from_driver", lambda: None)
        assert manager.connect_snowflake(
            "myorg-acct", "svc", "pw", "WH", "SALES", user_login=login
        )
        managers.append(manager)
    alice, bob = managers
    assert alice.auth_identity == ALICE
    assert _get_connection_fingerprint(alice) != _get_connection_fingerprint(bob)
    first = _FakeDriver.given[0]
    assert first["username"] is None and first["password"] is None
    assert first["token_provider"]() == "sf-1"
    assert alice._last_connection_params["password"] is None


# --- connect_database, end to end -----------------------------------------------


@pytest.fixture
def sf(monkeypatch, tmp_path):
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    for name, value in SF.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setattr(snowflake_driver, "SnowflakeDriver", _FakeDriver)
    monkeypatch.setattr(DatabaseManager, "_sync_engine_from_driver", lambda _s: None)
    monkeypatch.setattr(
        connection_handler,
        "sign_in_client",
        lambda: (IdpClient("our-client", "s3cret"), "https://login.example.com/t"),
    )
    for module in (connection_handler, main_module):
        monkeypatch.setattr(module, "current_user", lambda: ALICE)
        monkeypatch.setattr(module, "current_bearer", lambda: SUBJECT)
    service = _TokenService()
    monkeypatch.setattr(login_module, "post_form", service)
    yield service
    fresh.cleanup()


async def test_connect_database_signs_in_on_behalf_of_the_user(sf):
    _FakeDriver.given = []
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"database": "sf"})
    assert "Successfully connected to sf (snowflake)" in connected.data
    (call,) = sf.calls
    assert call["url"] == "https://login.example.com/t"
    assert call["form"]["requested_token_use"] == "on_behalf_of"
    assert call["form"]["assertion"] == SUBJECT
    assert call["form"]["scope"] == "api://sf-app/session:role-any"
    (params,) = _FakeDriver.given
    assert params["token_provider"]() == "sf-1" and params["username"] is None


async def test_a_refused_snowflake_sign_in_is_an_answer(sf):
    sf.answer = (400, {"error": "invalid_grant", "suberror": "basic_action"})
    async with Client(mcp) as client:
        result = await client.call_tool("connect_database", {"database": "sf"})
    text = result.data if isinstance(result.data, str) else json.dumps(result.data)
    assert "DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED" in text
    assert SUBJECT not in text and "s3cret" not in text
