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
