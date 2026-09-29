"""A requested row limit actually bounds what comes back.

The limit used to be applied by looking for the word LIMIT in the query text,
so a string literal, a comment, or an explicit larger limit all suppressed it:
`limit=10` returned 6000 rows with `limit_applied=False`. Every row was then
fetched and serialized. Two independent bounds replace that -- a LIMIT written
from the parsed statement, and a bounded fetch -- because a statement that
cannot carry a LIMIT still must not materialize an unbounded result.
"""

import pytest

from src.constants import DB_SQLGLOT_DIALECTS
from src.database_manager import DatabaseManager
from src.result_limits import (
    DEFAULT_ROW_LIMIT,
    HARD_ROW_CAP,
    apply_row_limit,
    effective_row_limit,
    fetch_bounded,
)


@pytest.fixture
def duckdb() -> DatabaseManager:
    manager = DatabaseManager()
    assert manager.connect_duckdb(":memory:")
    yield manager
    manager.disconnect()


# --- the effective limit ---


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (10, 10),
        (5000, HARD_ROW_CAP),
        (99999, HARD_ROW_CAP),
        (0, DEFAULT_ROW_LIMIT),
        (-1, DEFAULT_ROW_LIMIT),
    ],
)
def test_effective_row_limit(requested, expected):
    assert effective_row_limit(requested) == expected


# --- writing the limit into the statement ---


@pytest.mark.parametrize(
    ("kind", "sql"),
    [
        ("a larger explicit limit", "SELECT i FROM range(6000) AS t(i) LIMIT 6000"),
        (
            "a string holding the word",
            "SELECT i, 'LIMIT' AS n FROM range(6000) AS t(i)",
        ),
        ("a comment holding the word", "SELECT i FROM range(6000) AS t(i) -- LIMIT"),
        ("a CTE", "WITH c AS (SELECT i FROM range(6000) AS t(i)) SELECT * FROM c"),
        ("a union", "SELECT 1 AS i UNION ALL SELECT 2"),
    ],
)
def test_the_limit_is_imposed_whatever_the_text_says(kind, sql):
    limited, applied = apply_row_limit(sql, 10, "duckdb")

    assert applied is True, kind
    assert limited.rstrip().endswith("LIMIT 10"), kind


def test_a_smaller_explicit_limit_is_left_alone():
    sql = "SELECT i FROM range(6000) AS t(i) LIMIT 3"

    assert apply_row_limit(sql, 10, "duckdb") == (sql, False)


def test_a_statement_that_takes_no_limit_is_left_alone():
    """SHOW and friends; the fetch bound is what protects these."""
    assert apply_row_limit("SHOW TABLES", 10, "duckdb") == ("SHOW TABLES", False)


def test_unparseable_sql_is_left_alone():
    nonsense = "NOT ACTUALLY ;;; SQL"

    assert apply_row_limit(nonsense, 10, "duckdb")[1] is False


# --- bounding the fetch, independently of the statement ---


class _Result:
    """Stands in for a driver result holding more rows than were asked for."""

    def __init__(self, count: int) -> None:
        self.rows = list(range(count))
        self.taken = 0

    def fetchmany(self, size: int) -> list[int]:
        out = self.rows[self.taken : self.taken + size]
        self.taken += len(out)
        return out


@pytest.mark.parametrize(
    ("available", "limit", "expected_rows", "expected_truncated"),
    [(6000, 10, 10, True), (11, 10, 10, True), (10, 10, 10, False), (3, 10, 3, False)],
)
def test_fetch_bounded(available, limit, expected_rows, expected_truncated):
    rows, truncated = fetch_bounded(_Result(available), limit)

    assert len(rows) == expected_rows
    assert truncated is expected_truncated


def test_no_more_rows_are_read_than_needed():
    """The one row past the limit is what makes truncation reportable; reading
    the rest is what the bound exists to prevent."""
    result = _Result(6000)

    fetch_bounded(result, 10)

    assert result.taken == 11


# --- end to end, against a real engine ---


@pytest.mark.parametrize(
    ("kind", "sql", "requested", "expected"),
    [
        (
            "a larger explicit limit",
            "SELECT i FROM range(6000) AS t(i) LIMIT 6000",
            10,
            10,
        ),
        (
            "a string holding the word",
            "SELECT i, 'LIMIT' AS n FROM range(6000) AS t(i)",
            10,
            10,
        ),
        (
            "a comment holding the word",
            "SELECT i FROM range(6000) AS t(i) -- LIMIT",
            10,
            10,
        ),
        (
            "a CTE",
            "WITH c AS (SELECT i FROM range(6000) AS t(i)) SELECT * FROM c",
            10,
            10,
        ),
        (
            "a smaller explicit limit",
            "SELECT i FROM range(6000) AS t(i) LIMIT 3",
            10,
            3,
        ),
        ("the hard cap", "SELECT i FROM range(9000) AS t(i)", 99999, HARD_ROW_CAP),
        ("fewer rows than asked for", "SELECT i FROM range(4) AS t(i)", 10, 4),
    ],
)
def test_the_request_bounds_the_result(duckdb, kind, sql, requested, expected):
    result = duckdb.execute_sql_query(sql, limit=requested)

    assert result["success"] is True, kind
    assert result["row_count"] == expected, kind
    assert len(result["data"]) == expected, kind


def test_ordering_survives_the_limit(duckdb):
    result = duckdb.execute_sql_query(
        "SELECT i FROM range(100) AS t(i) ORDER BY i DESC", limit=3
    )

    assert [row["i"] for row in result["data"]] == [99, 98, 97]


def test_a_bounded_result_says_there_may_be_more(duckdb):
    result = duckdb.execute_sql_query("SELECT i FROM range(6000) AS t(i)", limit=10)

    assert result["limit_applied"] is True
    assert any("may be more" in warning for warning in result["warnings"])


def test_a_complete_result_does_not_claim_there_may_be_more(duckdb):
    result = duckdb.execute_sql_query("SELECT i FROM range(4) AS t(i)", limit=10)

    assert not any("may be more" in warning for warning in result["warnings"])


# --- work the execution path must not do ---


async def test_executing_sql_does_no_vector_retrieval():
    """Running a statement used to embed an intent and search the vector store,
    then use the result for one log line and discard it. Nothing read it."""
    from unittest.mock import AsyncMock, Mock

    from src.handler_context import HandlerContext
    from src.handlers import query as query_handler

    graphrag = Mock()
    graphrag.get_query_context.side_effect = AssertionError(
        "execute_sql_query must not retrieve context"
    )
    session = Mock()
    session.graphrag_initialized = True
    session.graphrag_manager = graphrag
    session.connection_id = "conn-1"

    manager = DatabaseManager()
    assert manager.connect_duckdb(":memory:")
    ctx = Mock()
    ctx.info = AsyncMock()
    services = HandlerContext(
        get_session_data=lambda _ctx: session,
        get_session_db_manager=lambda _ctx: manager,
        get_session_obqc_validator=lambda _ctx: None,
        create_error_response=lambda message, kind: {"error": message},
    )
    try:
        result = await query_handler.execute_sql_query(
            ctx,
            "SELECT i FROM range(3) AS t(i)",
            10,
            True,
            "count the rows",
            services,
        )
    finally:
        manager.disconnect()

    assert result["success"] is True
    assert result["row_count"] == 3
    graphrag.get_query_context.assert_not_called()


# --- every dialect this server speaks ---


@pytest.mark.parametrize("db_type", sorted(DB_SQLGLOT_DIALECTS))
def test_the_limit_is_written_for_every_supported_database(db_type):
    limited, applied = apply_row_limit("SELECT a FROM t", 10, db_type)

    assert applied is True, db_type
    assert "10" in limited, db_type


def test_a_dialects_own_limit_form_is_understood():
    """Snowflake's TOP is a limit; a smaller one is kept, a larger one bounded."""
    small = "SELECT TOP 3 a FROM t"
    assert apply_row_limit(small, 10, "snowflake") == (small, False)

    bounded, applied = apply_row_limit("SELECT TOP 9000 a FROM t", 10, "snowflake")
    assert applied is True
    assert "10" in bounded


def test_an_offset_survives_being_bounded():
    """MySQL's `LIMIT offset, count`: the count is bounded, the offset kept."""
    bounded, applied = apply_row_limit("SELECT a FROM t LIMIT 5, 20", 10, "mysql")

    assert applied is True
    assert "OFFSET 5" in bounded.upper()
    assert "LIMIT 10" in bounded.upper()


def test_an_unknown_database_type_still_gets_a_limit():
    bounded, applied = apply_row_limit("SELECT a FROM t", 10, "something-new")

    assert applied is True
    assert "10" in bounded
