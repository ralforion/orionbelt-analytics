"""Bounding how many rows a query returns, and how many are ever materialized.

Two independent limits, because neither alone is enough:

- A ``LIMIT`` added to the statement, so the database stops early. Derived from
  the parsed query, since the text of a query says nothing reliable about it: a
  string literal, a comment or a column name can all contain the word LIMIT.
- A bound on the fetch itself, so a statement that could not be limited -- one
  that failed to parse, or a ``SHOW`` that takes no LIMIT -- still cannot
  materialize an unbounded result into memory.

Neither bounds the work the engine does to produce the rows; cancellation and
statement timeouts are separate concerns.
"""

import logging
from typing import Any

import sqlglot
from sqlglot import exp

from .constants import DB_SQLGLOT_DIALECTS

logger = logging.getLogger(__name__)

# The most rows this server will return from one query, whatever was asked for.
HARD_ROW_CAP = 5000
# What an unusable request (zero, negative) falls back to.
DEFAULT_ROW_LIMIT = 100


def effective_row_limit(requested: int) -> int:
    """The row limit actually enforced for a request.

    Args:
        requested: Rows the caller asked for.

    Returns:
        A positive limit, never above :data:`HARD_ROW_CAP`.
    """
    if requested <= 0:
        return DEFAULT_ROW_LIMIT
    return min(requested, HARD_ROW_CAP)


def _existing_limit(statement: exp.Query) -> tuple[bool, int | None]:
    """What the statement already says about how many rows it wants.

    Three answers, and the difference matters:

    * no limit of its own -- one can be imposed;
    * a limit of *n* -- narrowing to a smaller cap is fine, widening never is;
    * a limit that exists but cannot be read here -- ``LIMIT (SELECT 3)``, a
      parameter, an expression. Rewriting that would *replace* the caller's
      limit with the tool's, which can only make the result bigger. The fetch
      bound is what protects those.

    ``FETCH FIRST n ROWS ONLY`` is the same statement in another spelling, and
    sqlglot parks it under the same ``limit`` argument as an ``exp.Fetch`` with
    a ``count``; reading only ``expression`` missed it, so ``FETCH FIRST 3``
    became ``LIMIT 10``.

    Args:
        statement: The parsed statement.

    Returns:
        Whether it carries a limit at all, and that limit's row count when it
        can be read.
    """
    limit = statement.args.get("limit")
    if limit is None:
        return False, None

    count = (
        limit.args.get("count")
        if isinstance(limit, exp.Fetch)
        else getattr(limit, "expression", None)
    )
    if isinstance(count, exp.Literal) and count.is_int:
        return True, int(count.this)
    return True, None


def apply_row_limit(
    sql_query: str, limit: int, db_type: str | None = None
) -> tuple[str, bool]:
    """Bound a statement to ``limit`` rows, if the statement can carry a limit.

    The query's own limit wins when it is smaller: asking for at most 10 rows
    never widens a ``LIMIT 3``.

    Args:
        sql_query: The statement, already validated as safe to run.
        limit: The effective row limit, from :func:`effective_row_limit`.
        db_type: Database type, mapped to a SQLGlot dialect for parsing and
            for writing the limit back in that dialect's own form.

    Returns:
        The statement to execute, and whether this function imposed the limit.
        A statement that cannot be parsed, or cannot take a LIMIT, is returned
        unchanged -- the fetch bound is what protects those.
    """
    dialect = DB_SQLGLOT_DIALECTS.get((db_type or "").lower(), "postgres")
    try:
        statement = sqlglot.parse_one(sql_query, dialect=dialect)
    except Exception as e:
        logger.debug(f"Could not parse for row limiting, relying on fetch bound: {e}")
        return sql_query, False

    if statement is None or not isinstance(statement, exp.Query):
        # SHOW, DESCRIBE, PRAGMA and friends take no LIMIT.
        return sql_query, False

    has_limit, existing = _existing_limit(statement)
    if has_limit and (existing is None or existing <= limit):
        # Its own limit already governs, or cannot be evaluated without running
        # the query. Either way, imposing this one could only widen the result.
        return sql_query, False

    try:
        limited = statement.limit(limit)
        return limited.sql(dialect=dialect), True
    except Exception as e:
        logger.debug(f"Could not write a row limit for {dialect}: {e}")
        return sql_query, False


def fetch_bounded(result: Any, limit: int) -> tuple[list[Any], bool]:
    """Read at most ``limit`` rows, and report whether more were waiting.

    One row beyond the limit is read and discarded, which is what makes
    truncation reportable without materializing the rest.

    Args:
        result: A DBAPI or SQLAlchemy result supporting ``fetchmany``.
        limit: The effective row limit.

    Returns:
        The rows, and True if the result held more than ``limit``.
    """
    try:
        rows = list(result.fetchmany(limit + 1))
    except AttributeError:
        # A driver whose result cannot fetch in batches; nothing to bound with.
        rows = list(result.fetchall())
    truncated = len(rows) > limit
    return rows[:limit], truncated
