"""Infer how a column stores dates that its SQL type does not call dates.

A date kept as text or as a number -- ``'20150601'``, ``950324``,
``'06/01/2015'`` -- admits no common predicate with the way a query usually
writes a date. ``WHERE order_day >= '2015-06-01'`` on text in ``YYYYMMDD``
compares as text and silently returns the wrong rows; ``= '2015-06-01'``
returns none. The query runs and looks right, which is the failure OBA exists
to catch.

The format is inferred by validation, not by matching a shape: every candidate
format is tried against every sampled value, and a format is claimed only if
it reads all of them. An impossible reading rules itself out (``13/07/2011``
cannot be month-first), a mixed column yields no claim, and when two formats
read the values differently (day-first and month-first, while every day is at
most 12) none is claimed either. A wrong claim would be worse than none.

No MCP and no database access here; :meth:`DatabaseManager.infer_date_formats`
samples the values.
"""

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

#: Formats tried, most specific and most common first. The first one that
#: reads every sampled value -- and reads it the same way as every other
#: format that also does -- is the one claimed.
CANDIDATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y/%m/%d",
    "%Y-%m",
    "%Y%m%d",
    "%Y%m%d%H%M%S",
    "%y%m%d",
    "%Y%m",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m/%d/%y",
    "%d/%m/%y",
    "%d.%m.%Y",
    "%d.%m.%y",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d %b %Y",
    "%b %d, %Y",
)

#: Distinct values needed before a format is claimed: a handful of values can
#: fit a format by accident, a sample this size rarely does.
MIN_DISTINCT_VALUES = 5

#: Column names that suggest a date. Only such columns are sampled: it keeps
#: discovery cheap, and an integer column of IDs that happens to read as
#: YYYYMMDD is not mistaken for dates.
DATE_NAME = re.compile(
    r"(date|_dt$|^dt_|_dt_|day|time|_ts$|^ts_|month|year|period|ymd|yyyy|"
    r"_at$|_on$|created|updated|modified|changed|birth|dob|start|end|"
    r"valid|expir|due|posted|booked|shipped|delivered|ordered|issued|"
    r"opened|closed|effective)",
    re.IGNORECASE,
)

_TEXT_TYPES = re.compile(r"char|text|string|varchar|clob", re.IGNORECASE)
_INTEGER_TYPES = re.compile(r"int|number|numeric|decimal", re.IGNORECASE)
_NATIVE_TEMPORAL = re.compile(r"date|time", re.IGNORECASE)

# Formats written only with digits: their values must be the full width
# (strptime would read "2015061" as %Y%m%d), so they are checked by
# formatting the parsed value back and comparing.
_DIGITS_ONLY = re.compile(r"^(%[YymdHMS])+$")


def is_candidate(column_name: str, data_type: str) -> bool:
    """Whether a column may hold dates that its type does not say are dates.

    Text and integer columns with a date-like name. A native DATE or
    TIMESTAMP needs no format, and other names are not sampled.

    Args:
        column_name: The column's name.
        data_type: Its SQL type as the database reports it.

    Returns:
        True when the column should be sampled.
    """
    if _NATIVE_TEMPORAL.search(data_type or ""):
        return False
    if not (
        _TEXT_TYPES.search(data_type or "") or _INTEGER_TYPES.search(data_type or "")
    ):
        return False
    return bool(DATE_NAME.search(column_name or ""))


def _parse(value: str, fmt: str) -> datetime | None:
    try:
        # A stored value has no time zone unless its format writes one.
        parsed = datetime.strptime(value, fmt)  # noqa: DTZ007
    except ValueError:
        return None
    if _DIGITS_ONLY.match(fmt) and parsed.strftime(fmt) != value:
        return None
    return parsed


def _texts(values: Iterable[Any]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value is None or isinstance(value, (bool, date, datetime)):
            continue
        if isinstance(value, float):
            if not value.is_integer():
                continue
            value = int(value)
        text = str(value).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def infer_date_format(values: Iterable[Any]) -> str | None:
    """The one format every sampled value is written in, or None.

    Args:
        values: Sampled values of one column; None and empty ones are skipped.

    Returns:
        A ``strftime`` format such as ``"%Y%m%d"``, or None when fewer than
        :data:`MIN_DISTINCT_VALUES` distinct values were seen, no format reads
        all of them, or the formats that do read them disagree.
    """
    texts = _texts(values)
    if len(texts) < MIN_DISTINCT_VALUES:
        return None
    readings: dict[str, tuple[datetime, ...]] = {}
    for fmt in CANDIDATE_FORMATS:
        parsed = [_parse(text, fmt) for text in texts]
        if all(p is not None for p in parsed):
            readings[fmt] = tuple(p for p in parsed if p is not None)
    if not readings:
        return None
    if len(set(readings.values())) > 1:
        return None  # e.g. 01/02/2015: January 2nd or February 1st
    return next(iter(readings))


def example(fmt: str) -> str:
    """A date written in *fmt*, for messages: 2015-06-01 14:30:00."""
    return datetime(2015, 6, 1, 14, 30, 0, tzinfo=UTC).strftime(fmt)


def matches(fmt: str, value: Any) -> bool:
    """Whether *value*, as compared in a query, is written in *fmt*."""
    texts = _texts([value])
    return bool(texts) and _parse(texts[0], fmt) is not None


def rewrite(fmt: str, value: Any) -> str | None:
    """*value* rewritten in *fmt*, if it is a date in any candidate format.

    ``rewrite("%Y%m%d", "2015-06-01")`` is ``"20150601"``. None when the value
    reads as no date, or ambiguously (as two different dates).

    Args:
        fmt: The column's format.
        value: A literal the query compares the column with.

    Returns:
        The value in the column's format, or None.
    """
    texts = _texts([value])
    if not texts:
        return None
    readings = {
        parsed
        for candidate in CANDIDATE_FORMATS
        if (parsed := _parse(texts[0], candidate)) is not None
    }
    if len(readings) != 1:
        return None
    return readings.pop().strftime(fmt)
