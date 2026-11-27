"""BigQueryDriver.connect() against a BigQuery that answers in-process.

The driver runs through sqlalchemy-bigquery and google-cloud-bigquery as in
production; only the HTTP session under the client is replaced, by one that
answers the connection's ``SELECT 1`` and records each request it is sent.
"""

import json
from typing import Any
from unittest.mock import patch

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

from src.client_name import CLIENT_NAME
from src.drivers.bigquery import BigQueryDriver

# Without google-cloud-bigquery-storage, which is not a dependency, the DB-API
# says it reads results over REST.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Cannot create BigQuery Storage client:UserWarning"
)

# What jobs.query returns for a finished ``SELECT 1``.
_SELECT_1 = {
    "kind": "bigquery#queryResponse",
    "jobComplete": True,
    "jobReference": {"projectId": "p", "jobId": "j", "location": "US"},
    "schema": {"fields": [{"name": "f0_", "type": "INTEGER", "mode": "NULLABLE"}]},
    "rows": [{"f": [{"v": "1"}]}],
    "totalRows": "1",
}


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, body: dict[str, Any]) -> None:
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self) -> dict[str, Any]:
        return json.loads(self.content)


class FakeBigQueryHTTP:
    """Stands in for the client's authorised HTTP session."""

    is_mtls = False

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.requests.append({"method": method, "url": url, **kwargs})
        if method == "POST" and url.split("?")[0].endswith("/queries"):
            return _Response(_SELECT_1)
        raise AssertionError(f"unexpected BigQuery request: {method} {url}")


@pytest.fixture
def bigquery_http():
    """The HTTP session of every client the dialect creates."""
    http = FakeBigQueryHTTP()

    def client(**kwargs: Any) -> bigquery.Client:
        return bigquery.Client(
            project="p", credentials=AnonymousCredentials(), _http=http
        )

    with patch("sqlalchemy_bigquery._helpers.create_bigquery_client", client):
        yield http


def test_connect_runs_its_test_query(bigquery_http):
    """The DB-API takes no ``timeout``, which used to fail every connection."""
    driver = BigQueryDriver()

    assert driver.connect(project_id="p", dataset="d")

    assert [r["method"] for r in bigquery_http.requests] == ["POST"]
    assert json.loads(bigquery_http.requests[0]["data"])["query"] == "SELECT 1"


def test_its_requests_carry_the_client_name_first(bigquery_http):
    """BigQuery shows it in Cloud Audit Logs as ``callerSuppliedUserAgent``."""
    assert BigQueryDriver().connect(project_id="p", dataset="d")

    agent = bigquery_http.requests[0]["headers"]["User-Agent"]
    assert agent.startswith(f"{CLIENT_NAME} gl-python/")


def test_a_project_id_with_hyphens_connects(bigquery_http):
    """Google's project IDs are like my-project-123; nearly all have hyphens."""
    assert BigQueryDriver().connect(project_id="my-project-123", dataset="d")
    assert [r["method"] for r in bigquery_http.requests] == ["POST"]


@pytest.mark.parametrize(
    "project_id",
    ["-leading", "trailing-", "semi;colon", "example.com:legacy", "with space"],
)
def test_a_project_id_that_is_none_is_refused(bigquery_http, project_id):
    assert not BigQueryDriver().connect(project_id=project_id, dataset="d")
    assert bigquery_http.requests == []
