"""Every driver's SQLAlchemy dialect is installed.

databricks-sql-connector 4.0 moved its SQLAlchemy dialect into a package of
its own, databricks-sqlalchemy. Without it ``create_engine("databricks://")``
raises NoSuchModuleError, so no Databricks connection could be made -- and no
test noticed, since every Databricks test replaces the driver. Building an
engine connects to nothing, so each dialect is checked here offline.
"""

import pytest
from sqlalchemy import create_engine

# The URL each driver builds its engine from, credentials made up.
URLS = {
    "databricks": "databricks://token:x@host.example?http_path=/sql/1.0/w&catalog=c",
    "snowflake": "snowflake://user:secret@account/db/schema",
    # user_supplied_client: the dialect leaves the client to us instead of
    # looking for Google credentials as the engine is built, which CI has none
    # of. The dialect itself is still the one that loads.
    "bigquery": "bigquery://project?user_supplied_client=true",
    "clickhouse": "clickhouse+http://user:secret@host.example:8123/db",
    "duckdb": "duckdb:///:memory:",
    "mysql": "mysql+pymysql://user:secret@host.example/db",
    "postgresql": "postgresql://user:secret@host.example/db",
}


@pytest.mark.parametrize("url", URLS.values(), ids=URLS.keys())
def test_the_dialect_is_installed(url: str) -> None:
    engine = create_engine(url)
    engine.dispose()


def test_the_databricks_dialect_takes_the_token_as_a_connect_argument() -> None:
    """What lets a token be set per connection instead of in the URL."""
    engine = create_engine(URLS["databricks"])
    _, kwargs = engine.dialect.create_connect_args(engine.url)
    assert kwargs["access_token"] == "x"
    assert kwargs["server_hostname"] == "host.example"
