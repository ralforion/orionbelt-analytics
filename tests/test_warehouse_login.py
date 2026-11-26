"""Logging in to a database as the signed-in user (DB_<NAME>_AUTH=user).

The caller's token is exchanged for one issued for the warehouse, renewed
before it expires, and the connection is the user's own: its identity is
their issuer and subject, so no other user shares its runtime, schema cache
or workspace.
"""

import asyncio
import json
import logging
from typing import Any

import pytest
from aiohttp import web
from fastmcp import Client

import src.drivers.databricks as databricks_driver
import src.main as main_module
import src.server_state as state_module
import src.warehouse_login as login_module
from src.config import ConfigManager
from src.database_manager import DatabaseManager
from src.database_registry import (
    DatabaseEntry,
    LoginConfigError,
    configured_databases,
)
from src.handlers import connection as connection_handler
from src.main import mcp
from src.server_state import ServerState, _get_connection_fingerprint
from src.warehouse_login import (
    ENTRA_OBO,
    RENEW_MARGIN_SECONDS,
    STANDARD_EXCHANGE,
    IdpClient,
    LoginSource,
    SignInInteractionRequiredError,
    SignInRefusedError,
    SignInRequiredError,
    SignInUnavailableError,
    UserLogin,
    check_sources,
    exchange,
    post_form,
)

ALICE = "https://issuer.example|alice"
BOB = "https://issuer.example|bob"
SUBJECT = "eyJ.the-users-own-token.sig"

FIN = {
    "OBA_DATABASES": "fin",
    "DB_FIN_TYPE": "databricks",
    "DB_FIN_AUTH": "user",
    "DB_FIN_DATABRICKS_SERVER_HOSTNAME": "https://adb-1.azuredatabricks.net/",
    "DB_FIN_DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc",
    "DB_FIN_DATABRICKS_CATALOG": "finance",
    "DB_FIN_DATABRICKS_SCHEMA": "gold",
}


def _entry(env: dict[str, str] | None = None) -> DatabaseEntry:
    (entry,) = [e for e in configured_databases(env or FIN) if e.name == "fin"]
    return entry


class _TokenService:
    """A token endpoint: records each request, answers what it is told."""

    def __init__(self, *answers: tuple[int, Any]) -> None:
        self.answers = list(answers) or [(200, {"access_token": "dbx-1"})]
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, form: Any, auth: Any) -> tuple[int, Any]:
        self.calls.append({"url": url, "form": dict(form), "auth": auth})
        await asyncio.sleep(0)
        return self.answers[min(len(self.calls), len(self.answers)) - 1]


# --- settings -----------------------------------------------------------------


def test_sign_in_settings_are_the_connections_own():
    """A bare AUTH or OAUTH_SCOPE must not switch every database to the user."""
    env = {**FIN, "DB_FIN_AUTH": "", "AUTH": "user", "OAUTH_SCOPE": "x"}
    entry = _entry(env)
    assert not entry.logs_in_as_user
    assert entry.login_setting("OAUTH_SCOPE") is None
    # A type configured without a name never logs in as the user.
    unnamed = DatabaseEntry(name="duckdb", db_type="duckdb", environ={"AUTH": "user"})
    assert not unnamed.logs_in_as_user


def test_an_unknown_login_mode_is_refused():
    with pytest.raises(LoginConfigError, match="DB_FIN_AUTH='users'"):
        configured_databases({**FIN, "DB_FIN_AUTH": "users"})


def test_the_listing_says_which_database_logs_in_as_the_user():
    assert _entry().describe()["login"] == "as the signed-in user"
    service = _entry({**FIN, "DB_FIN_AUTH": "service"}).describe()
    assert "login" not in service


@pytest.mark.parametrize(
    ("env", "mode", "message"),
    [
        ({}, "none", "needs MCP_AUTH=jwt"),
        ({"DB_FIN_TYPE": "postgresql"}, "jwt", "cannot log in as the user"),
        ({"DB_FIN_DATABRICKS_HTTP_PATH": ""}, "jwt", "DATABRICKS_HTTP_PATH"),
    ],
)
def test_startup_refuses_what_cannot_log_in_as_the_user(env, mode, message):
    with pytest.raises(ValueError, match=message):
        check_sources(configured_databases({**FIN, **env}), mode)


def test_a_configured_token_is_ignored_and_said_so():
    entries = configured_databases({**FIN, "DATABRICKS_ACCESS_TOKEN": "dapi-x"})
    (warning,) = check_sources(entries, "jwt")
    assert "DATABRICKS_ACCESS_TOKEN is ignored" in warning
    assert "dapi-x" not in warning


def test_validate_config_runs_the_checks(monkeypatch):
    for name, value in FIN.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MCP_AUTH", "none")
    with pytest.raises(ValueError, match="needs MCP_AUTH=jwt"):
        ConfigManager().validate_config()
    monkeypatch.setenv("DB_FIN_AUTH", "sometimes")
    with pytest.raises(ValueError, match="unknown DB_FIN_AUTH"):
        ConfigManager().validate_config()


# --- the exchanges --------------------------------------------------------------


async def test_databricks_exchanges_at_the_workspace():
    service = _TokenService((200, {"access_token": "dbx-1", "expires_in": 3599}))
    token, seconds = await exchange(
        LoginSource.from_entry(_entry()), SUBJECT, post=service
    )
    assert (token, seconds) == ("dbx-1", 3599.0)
    (call,) = service.calls
    assert call["url"] == "https://adb-1.azuredatabricks.net/oidc/v1/token"
    assert call["form"] == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": SUBJECT,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "scope": "all-apis",
    }
    assert call["auth"] is None


async def test_a_service_principal_policy_sends_its_client_id():
    service = _TokenService()
    entry = _entry({**FIN, "DB_FIN_OAUTH_CLIENT_ID": "sp-123"})
    await exchange(LoginSource.from_entry(entry), SUBJECT, post=service)
    assert service.calls[0]["form"]["client_id"] == "sp-123"


async def test_google_sts_exchanges_without_a_secret():
    service = _TokenService()
    source = LoginSource(
        name="bq",
        dialect="bigquery",
        workforce_provider="//iam.googleapis.com/locations/global/workforcePools/p/providers/q",
        workforce_user_project="billing-1",
    )
    await exchange(source, SUBJECT, post=service)
    (call,) = service.calls
    assert call["url"] == "https://sts.googleapis.com/v1/token"
    assert call["form"]["subject_token_type"].endswith(":id_token")
    assert json.loads(call["form"]["options"]) == {"userProject": "billing-1"}
    assert call["auth"] is None


async def test_entra_obo_asks_as_our_client():
    service = _TokenService()
    source = LoginSource(
        name="sf", dialect="snowflake", exchange=ENTRA_OBO, scope="api://sf/x"
    )
    idp = IdpClient("our-client", "s3cret")
    await exchange(source, SUBJECT, idp, "https://login.example/token", service)
    form = service.calls[0]["form"]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert form["requested_token_use"] == "on_behalf_of"
    assert (form["client_id"], form["assertion"]) == ("our-client", SUBJECT)
    assert "s3cret" not in repr(idp)


async def test_rfc_8693_at_the_idp_authenticates_with_basic():
    service = _TokenService()
    source = LoginSource(
        name="sf",
        dialect="snowflake",
        exchange=STANDARD_EXCHANGE,
        scope="session:role:ANALYST",
        audience="snowflake",
    )
    idp = IdpClient("our client", "s:cr t")
    await exchange(source, SUBJECT, idp, "https://login.example/token", service)
    (call,) = service.calls
    # Form-encoded before HTTP Basic (RFC 6749, 2.3.1).
    assert call["auth"] == ("our%20client", "s%3Acr%20t")
    assert call["form"]["audience"] == "snowflake"
    assert "client_secret" not in call["form"]


async def test_a_snowflake_exchange_without_our_client_is_unavailable():
    source = LoginSource(name="sf", dialect="snowflake", exchange=ENTRA_OBO)
    with pytest.raises(SignInUnavailableError, match="MCP_AUTH_CLIENT_ID"):
        await exchange(source, SUBJECT, post=_TokenService())


@pytest.mark.parametrize(
    ("answer", "error", "detail"),
    [
        ((400, {"error": "invalid_grant"}), SignInRefusedError, "invalid_grant"),
        (
            (400, {"error": "interaction_required", "claims": '{"mfa":1}'}),
            SignInInteractionRequiredError,
            "interaction_required",
        ),
        # Entra: an invalid_grant the user resolves by signing in again...
        (
            (400, {"error": "invalid_grant", "suberror": "basic_action"}),
            SignInInteractionRequiredError,
            "invalid_grant",
        ),
        # ...and one they cannot.
        (
            (400, {"error": "invalid_grant", "suberror": "client_mismatch"}),
            SignInRefusedError,
            "invalid_grant",
        ),
        ((503, {}), SignInUnavailableError, "HTTP 503"),
        ((200, {"token_type": "bearer"}), SignInUnavailableError, "no access token"),
        ((200, "not json"), SignInUnavailableError, "no access token"),
    ],
)
async def test_a_failed_exchange_says_what_to_do(answer, error, detail, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(error, match=detail) as raised:
        await exchange(
            LoginSource.from_entry(_entry()), SUBJECT, post=_TokenService(answer)
        )
    if isinstance(raised.value, SignInInteractionRequiredError) and "claims" in (
        answer[1] if isinstance(answer[1], dict) else {}
    ):
        assert raised.value.claims == '{"mfa":1}'
    # No token in the message or the log.
    assert SUBJECT not in str(raised.value)
    assert SUBJECT not in caplog.text


async def test_an_unreadable_lifetime_is_an_hour():
    service = _TokenService((200, {"access_token": "t", "expires_in": "soon"}))
    _, seconds = await exchange(LoginSource.from_entry(_entry()), SUBJECT, post=service)
    assert seconds == 3600.0


async def test_the_token_never_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    service = _TokenService((200, {"access_token": "dbx-secret-1"}))
    await exchange(LoginSource.from_entry(_entry()), SUBJECT, post=service)
    assert "dbx-secret-1" not in caplog.text and SUBJECT not in caplog.text
    assert "Signed in to database 'fin' as the user" in caplog.text


# --- over HTTP ------------------------------------------------------------------


async def test_post_form_reads_the_answer_and_reports_an_unreachable_service():
    seen: dict[str, Any] = {}

    async def token(request: web.Request) -> web.Response:
        seen["form"] = dict(await request.post())
        seen["auth"] = request.headers.get("Authorization")
        if seen["form"].get("broken"):
            return web.Response(text="<html>oops</html>", status=502)
        return web.json_response({"access_token": "t", "expires_in": 60})

    app = web.Application()
    app.router.add_post("/token", token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    url = f"http://127.0.0.1:{port}/token"
    try:
        status, body = await post_form(url, {"a": "1"}, ("id", "secret"))
        assert (status, body) == (200, {"access_token": "t", "expires_in": 60})
        assert seen["form"] == {"a": "1"} and seen["auth"].startswith("Basic ")
        status, body = await post_form(url, {"broken": "1"})
        assert (status, body) == (502, {})
    finally:
        await runner.cleanup()
    with pytest.raises(SignInUnavailableError, match="Could not reach"):
        await post_form(url, {"a": "1"})


# --- the token over time --------------------------------------------------------


async def test_concurrent_calls_exchange_once():
    service = _TokenService()
    login = UserLogin(LoginSource.from_entry(_entry()), ALICE, post=service)
    await asyncio.gather(*(login.renew(ALICE, SUBJECT) for _ in range(5)))
    assert len(service.calls) == 1
    assert login.token() == "dbx-1"
    # Fresh: no exchange at all.
    await login.renew(ALICE, SUBJECT)
    assert len(service.calls) == 1


async def test_a_token_near_its_end_is_exchanged_again():
    lifetime = RENEW_MARGIN_SECONDS - 1
    service = _TokenService(
        (200, {"access_token": "dbx-1", "expires_in": lifetime}),
        (200, {"access_token": "dbx-2", "expires_in": 3600}),
    )
    login = UserLogin(LoginSource.from_entry(_entry()), ALICE, post=service)
    await login.renew(ALICE, SUBJECT)
    await login.renew(ALICE, SUBJECT)
    assert len(service.calls) == 2 and login.token() == "dbx-2"


async def test_the_driver_side_never_exchanges():
    service = _TokenService((200, {"access_token": "dbx-1", "expires_in": 0}))
    login = UserLogin(LoginSource.from_entry(_entry()), ALICE, post=service)
    with pytest.raises(SignInRequiredError):
        login.token()
    await login.renew(ALICE, SUBJECT)
    # Expired at once: the driver is told, it does not exchange.
    with pytest.raises(SignInRequiredError, match="expired"):
        login.token()
    assert len(service.calls) == 1


async def test_a_login_is_renewed_only_by_its_own_user_with_a_token():
    service = _TokenService()
    login = UserLogin(LoginSource.from_entry(_entry()), ALICE, post=service)
    with pytest.raises(SignInRequiredError, match="another user"):
        await login.renew(BOB, SUBJECT)
    with pytest.raises(SignInRequiredError, match="access token"):
        await login.renew(ALICE, None)
    assert service.calls == []
    await login.renew(ALICE, SUBJECT)
    assert "dbx-1" not in repr(login) and "alice" in repr(login)


# --- who the connection is ------------------------------------------------------


class _FakeDriver:
    """Accepts any connect; records what it was given."""

    given: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.engine = None

    def connect(self, **params: Any) -> bool:
        _FakeDriver.given.append(params)
        return True

    def disconnect(self) -> None:
        pass


async def _connected_as(monkeypatch, user: str, token: str = "dbx-1") -> Any:
    monkeypatch.setattr(databricks_driver, "DatabricksDriver", _FakeDriver)
    service = _TokenService((200, {"access_token": token}))
    login = UserLogin(LoginSource.from_entry(_entry()), user, post=service)
    await login.renew(user, SUBJECT)
    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_sync_engine_from_driver", lambda: None)
    assert manager.connect_databricks(
        "adb-1.azuredatabricks.net",
        "/sql/1.0/warehouses/abc",
        None,
        "finance",
        "gold",
        user_login=login,
    )
    return manager


async def test_each_user_has_a_connection_of_their_own(monkeypatch):
    alice = await _connected_as(monkeypatch, ALICE)
    bob = await _connected_as(monkeypatch, BOB)
    alice_again = await _connected_as(monkeypatch, ALICE, token="dbx-rotated")
    assert alice.auth_identity == ALICE and bob.auth_identity == BOB
    assert _get_connection_fingerprint(alice) != _get_connection_fingerprint(bob)
    # A new warehouse token is the same user, the same workspace.
    assert _get_connection_fingerprint(alice) == _get_connection_fingerprint(
        alice_again
    )


async def test_the_driver_gets_the_logins_token_not_a_configured_one(monkeypatch):
    _FakeDriver.given = []
    manager = await _connected_as(monkeypatch, ALICE)
    (params,) = _FakeDriver.given
    assert params["access_token"] is None
    assert params["token_provider"]() == "dbx-1"
    # Reconnecting keeps the login, never a token.
    assert manager._last_connection_params["access_token"] is None
    assert manager._last_connection_params["user_login"] is manager.user_login


async def test_a_user_login_never_adopts_a_shared_workspace(monkeypatch):
    manager = await _connected_as(monkeypatch, ALICE)
    monkeypatch.setattr(
        state_module,
        "_legacy_connection_fingerprint",
        lambda _m: (_ for _ in ()).throw(AssertionError("looked for one")),
    )
    assert state_module.adopt_legacy_workspace(manager, "x", "databricks", "f") == []


def test_the_engine_reads_the_token_as_each_connection_opens(monkeypatch):
    """Through do_connect, from the provider; never from the URL."""
    from sqlalchemy import create_engine as real_create_engine
    from sqlalchemy import event as real_event

    urls: list[str] = []
    seen: list[str] = []

    def sqlite_engine(url: str, **kwargs: Any) -> Any:
        urls.append(url)
        # The driver's NullPool: a connection per use, so a token per use.
        return real_create_engine("sqlite://", poolclass=kwargs["poolclass"])

    class _Event:
        @staticmethod
        def listens_for(target: Any, name: str) -> Any:
            def register(fn: Any) -> Any:
                def then_record(dialect, record, args, kwargs):
                    fn(dialect, record, args, kwargs)
                    seen.append(kwargs.pop("access_token"))  # sqlite takes none

                real_event.listen(target, name, then_record)
                return fn

            return register

    monkeypatch.setattr(databricks_driver, "create_engine", sqlite_engine)
    monkeypatch.setattr(databricks_driver, "event", _Event)
    tokens = iter(["dbx-1", "dbx-2"])
    driver = databricks_driver.DatabricksDriver()
    assert driver.connect(
        server_hostname="adb-1.azuredatabricks.net",
        http_path="/sql/1.0/warehouses/abc",
        token_provider=lambda: next(tokens),
        catalog="finance",
    )
    with driver.engine.connect():
        pass
    assert seen == ["dbx-1", "dbx-2"]
    assert "dbx" not in urls[0] and urls[0].startswith("databricks://token@")
    # A configured token goes the same way.
    static = databricks_driver.DatabricksDriver()
    assert static.connect(
        server_hostname="h", http_path="/p", access_token="dapi-1", catalog="c"
    )
    assert seen[-1] == "dapi-1" and "dapi-1" not in urls[-1]


# --- connect_database, end to end -----------------------------------------------


@pytest.fixture
def fin(monkeypatch, tmp_path):
    """The real server, a database that logs in as the user, a fake warehouse."""
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    for name, value in FIN.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setattr(databricks_driver, "DatabricksDriver", _FakeDriver)
    monkeypatch.setattr(DatabaseManager, "_sync_engine_from_driver", lambda _s: None)
    service = _TokenService((200, {"access_token": "dbx-1", "expires_in": 60}))
    monkeypatch.setattr(login_module, "post_form", service)
    yield service
    fresh.cleanup()


def _signed_in_as(monkeypatch, user: str | None, bearer: str | None) -> None:
    for module in (connection_handler, main_module):
        monkeypatch.setattr(module, "current_user", lambda: user)
        monkeypatch.setattr(module, "current_bearer", lambda: bearer)


async def test_a_caller_without_a_token_is_refused_before_connecting(fin, monkeypatch):
    _signed_in_as(monkeypatch, None, None)
    _FakeDriver.given = []
    async with Client(mcp) as client:
        result = await client.call_tool("connect_database", {"database": "fin"})
    text = result.data if isinstance(result.data, str) else json.dumps(result.data)
    assert "SIGN_IN_REQUIRED" in text
    assert _FakeDriver.given == [] and fin.calls == []


async def test_a_signed_in_caller_connects_as_themself(fin, monkeypatch):
    _signed_in_as(monkeypatch, ALICE, SUBJECT)
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"database": "fin"})
        assert "Successfully connected to fin (databricks)" in connected.data
        (session,) = list(state_module._server_state._sessions.values())
        assert session.db_manager.auth_identity == ALICE
        # Each call renews ahead: this token lives 60 s, inside the margin.
        await client.call_tool("list_databases", {})
    assert len(fin.calls) == 2
    assert all(c["form"]["subject_token"] == SUBJECT for c in fin.calls)


async def test_a_refused_sign_in_is_an_answer_not_a_crash(fin, monkeypatch):
    _signed_in_as(monkeypatch, ALICE, SUBJECT)
    fin.answers = [(400, {"error": "invalid_grant", "error_description": "no"})]
    async with Client(mcp) as client:
        result = await client.call_tool("connect_database", {"database": "fin"})
    text = result.data if isinstance(result.data, str) else json.dumps(result.data)
    assert "DATA_SOURCE_SIGN_IN_REFUSED" in text and SUBJECT not in text


async def test_the_listing_marks_the_database(fin, monkeypatch):
    _signed_in_as(monkeypatch, None, None)
    async with Client(mcp) as client:
        listed = await client.call_tool("list_databases", {})
    text = listed.data if isinstance(listed.data, str) else json.dumps(listed.data)
    assert "as the signed-in user" in text
