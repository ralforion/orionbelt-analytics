"""BigQuery logging in as the signed-in user (workforce identity federation).

Google's security token service exchanges the user's token for a Google
access token; each connection gets a BigQuery client made from the current
one, with no key file or inline key.
"""

import json
from typing import Any

import pytest
from fastmcp import Client

import src.drivers.bigquery as bigquery_driver
import src.main as main_module
import src.server_state as state_module
import src.warehouse_login as login_module
from src.database_manager import DatabaseManager
from src.database_registry import configured_databases
from src.handlers import connection as connection_handler
from src.main import mcp
from src.server_state import ServerState, _get_connection_fingerprint
from src.warehouse_login import LoginSource, UserLogin, check_sources

ALICE = "https://login.example.com/|alice"
SUBJECT = "eyJ.alices-id-token.sig"
PROVIDER = "//iam.googleapis.com/locations/global/workforcePools/p/providers/q"

BQ = {
    "OBA_DATABASES": "bq",
    "DB_BQ_TYPE": "bigquery",
    "DB_BQ_AUTH": "user",
    "DB_BQ_WORKFORCE_PROVIDER": PROVIDER,
    "DB_BQ_BIGQUERY_PROJECT_ID": "analytics-prod",
    "DB_BQ_BIGQUERY_DATASET": "sales",
}


class _TokenService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, form: Any, auth: Any) -> tuple[int, Any]:
        self.calls.append({"url": url, "form": dict(form), "auth": auth})
        return 200, {"access_token": "ya29.google-1", "expires_in": 3599}


# --- what startup refuses -------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"DB_BQ_WORKFORCE_PROVIDER": ""}, "needs DB_BQ_WORKFORCE_PROVIDER"),
        ({"DB_BQ_BIGQUERY_PROJECT_ID": ""}, "needs BIGQUERY_PROJECT_ID"),
    ],
)
def test_startup_refuses_a_bigquery_login_it_cannot_make(env, message):
    with pytest.raises(ValueError, match=message):
        check_sources(configured_databases({**BQ, **env}), "jwt")


def test_a_configured_key_is_ignored_and_said_so():
    env = {**BQ, "BIGQUERY_CREDENTIALS_PATH": "/keys/sa.json"}
    (warning,) = check_sources(configured_databases(env), "jwt")
    assert "BIGQUERY_CREDENTIALS_PATH is ignored" in warning


def test_the_exchange_is_billed_to_the_connections_project_by_default():
    (entry,) = configured_databases(BQ)
    assert LoginSource.from_entry(entry).workforce_user_project == "analytics-prod"
    (billed,) = configured_databases({**BQ, "DB_BQ_WORKFORCE_USER_PROJECT": "billing"})
    assert LoginSource.from_entry(billed).workforce_user_project == "billing"


# --- the driver -----------------------------------------------------------------


def test_each_connection_gets_a_client_with_the_current_token(monkeypatch):
    from sqlalchemy import create_engine as real_create_engine
    from sqlalchemy import event as real_event
    from sqlalchemy.pool import NullPool

    made: list[dict[str, Any]] = []
    clients: list[Any] = []

    def sqlite_engine(url: str, **kwargs: Any) -> Any:
        made.append({"url": url, **kwargs})
        return real_create_engine("sqlite://", poolclass=NullPool)

    class _Event:
        @staticmethod
        def listen(target: Any, name: str, fn: Any) -> None:
            def then_record(dialect, record, args, cparams):
                fn(dialect, record, args, cparams)
                if "client" in cparams:
                    clients.append(cparams.pop("client"))  # sqlite takes none

            real_event.listen(target, name, then_record)

    monkeypatch.setattr(bigquery_driver, "create_engine", sqlite_engine)
    monkeypatch.setattr(bigquery_driver, "event", _Event)
    tokens = iter(["ya29.one", "ya29.two"])
    driver = bigquery_driver.BigQueryDriver()
    assert driver.connect(
        project_id="analytics-prod",
        dataset="sales",
        credentials_path="/ignored.json",
        token_provider=lambda: next(tokens),
    )
    with driver.engine.connect():
        pass
    assert [c.project for c in clients] == ["analytics-prod", "analytics-prod"]
    assert [c._credentials.token for c in clients] == ["ya29.one", "ya29.two"]
    (engine,) = made
    assert engine["url"].endswith("?user_supplied_client=true")
    assert "credentials_path" not in engine["url"] and "credentials_info" not in engine


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


async def test_a_bigquery_login_is_the_users_own_connection(monkeypatch):
    monkeypatch.setattr(bigquery_driver, "BigQueryDriver", _FakeDriver)
    _FakeDriver.given = []
    (entry,) = configured_databases(BQ)
    managers = []
    for user in (ALICE, "https://login.example.com/|bob"):
        login = UserLogin(LoginSource.from_entry(entry), user, post=_TokenService())
        await login.renew(user, SUBJECT)
        manager = DatabaseManager()
        monkeypatch.setattr(manager, "_sync_engine_from_driver", lambda: None)
        assert manager.connect_bigquery(
            "analytics-prod", "sales", "/keys/sa.json", None, user_login=login
        )
        managers.append(manager)
    alice, bob = managers
    assert alice.auth_identity == ALICE
    assert _get_connection_fingerprint(alice) != _get_connection_fingerprint(bob)
    first = _FakeDriver.given[0]
    assert first["credentials_path"] is None
    assert first["token_provider"]() == "ya29.google-1"


# --- connect_database, end to end -----------------------------------------------


@pytest.fixture
def bq(monkeypatch, tmp_path):
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    for name, value in BQ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setattr(bigquery_driver, "BigQueryDriver", _FakeDriver)
    monkeypatch.setattr(DatabaseManager, "_sync_engine_from_driver", lambda _s: None)
    for module in (connection_handler, main_module):
        monkeypatch.setattr(module, "current_user", lambda: ALICE)
        monkeypatch.setattr(module, "current_bearer", lambda: SUBJECT)
    service = _TokenService()
    monkeypatch.setattr(login_module, "post_form", service)
    yield service
    fresh.cleanup()


async def test_connect_database_exchanges_at_google(bq):
    _FakeDriver.given = []
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"database": "bq"})
    assert "Successfully connected to bq (bigquery)" in connected.data
    (call,) = bq.calls
    assert call["url"] == "https://sts.googleapis.com/v1/token"
    assert call["form"]["audience"] == PROVIDER
    assert json.loads(call["form"]["options"]) == {"userProject": "analytics-prod"}
    (params,) = _FakeDriver.given
    assert params["token_provider"]() == "ya29.google-1"
