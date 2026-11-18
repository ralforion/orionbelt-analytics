"""Infer how a column stores dates that its SQL type does not call dates.

A date kept as text or as a number -- ``'20150601'``, ``950324``,
``'06/01/2015'`` -- admits no common predicate with the way a query usually
writes a date. ``WHERE order_day >= '2015-06-01'`` on text in ``YYYYMMDD``
compares as text and silently returns the wrong rows; ``= '2015-06-01'``
returns none. The query runs and looks right, which is the failure OBA exists
to catch.

The format is inferred by validation, not by matching a shape: every candidate
format is tried against every sampled value, and a format is claimed only if
it reads all of them *and writes each of them back exactly as stored*. So a
claimed format reproduces the stored form -- ``2015-6-1`` is not
``%Y-%m-%d`` -- and a value suggested in it matches stored values. An
impossible reading rules itself out (``13/07/2011`` cannot be month-first), a
mixed column yields no claim, a numeric value with a fraction rules the column
out, and when two formats read the values differently (day-first and
month-first, while every day is at most 12) none is claimed either. A wrong
claim would be worse than none.

No MCP and no database access here; :meth:`DatabaseManager.infer_date_formats`
samples the values.
"""

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from decimal import Decimal
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

# The order in which date fields sort: a format whose fields appear in this
# order, from the year down, writes dates that sort as dates.
_SORT_ORDER = ("%Y", "%m", "%d", "%H", "%M", "%S", "%f")


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
    """*value* read in *fmt*, only if *fmt* writes it back exactly as stored.

    strptime is lenient -- it reads ``2015-6-1`` with ``%Y-%m-%d`` and
    ``2015061`` with ``%Y%m%d`` -- so the reading is formatted back and must
    equal the value. That keeps a claimed format true to the stored form.
    """
    try:
        # A stored value has no time zone unless its format writes one.
        parsed = datetime.strptime(value, fmt)  # noqa: DTZ007
    except ValueError:
        return None
    return parsed if parsed.strftime(fmt) == value else None


def _texts(values: Iterable[Any]) -> list[str] | None:
    """The distinct non-empty values as text; None if any cannot be a date.

    A number with a fraction is no date in any format, and a DECIMAL column
    holding one is not a column of dates, so it rules the column out rather
    than being skipped.
    """
    seen: dict[str, None] = {}
    for value in values:
        if value is None or isinstance(value, (bool, date, datetime)):
            continue
        if isinstance(value, (float, Decimal)):
            if value != int(value):
                return None
            value = int(value)
        # As stored: " 20150601 " is not written in %Y%m%d, and a value
        # suggested in that format would not match it. Blank is missing.
        text = str(value)
        if text.strip():
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
    if texts is None or len(texts) < MIN_DISTINCT_VALUES:
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
    return texts is not None and bool(texts) and _parse(texts[0], fmt) is not None


def as_datetime(value: Any) -> datetime | None:
    """The moment *value* stands for, if it reads as one in a candidate format.

    The time of day is kept: ``'2015-06-01 12:00:00'`` is noon, not the day.
    """
    texts = _texts([value])
    if not texts:
        return None
    readings = {
        parsed
        for candidate in CANDIDATE_FORMATS
        if (parsed := _parse(texts[0], candidate)) is not None
    }
    return readings.pop() if len(readings) == 1 else None


def sql_moment(moment: datetime) -> str:
    """*moment* as a SQL literal: a DATE at midnight, a TIMESTAMP otherwise."""
    if moment.time() == time(0):
        return f"DATE '{moment.date().isoformat()}'"
    return f"TIMESTAMP '{moment.isoformat(sep=' ')}'"


def sorts_chronologically(fmt: str) -> bool:
    """Whether values written in *fmt* sort in date order as text or numbers.

    True when the format's fields run from the four-digit year down without
    gaps -- ``%Y%m%d``, ``%Y-%m-%d %H:%M:%S`` -- since a claimed format
    writes every field at full width. ``%d.%m.%Y`` puts the day first and
    ``%y`` loses the century, so neither does.
    """
    fields = re.findall(r"%[A-Za-z]", fmt)
    return bool(fields) and tuple(fields) == _SORT_ORDER[: len(fields)]


def rewrite(fmt: str, value: Any) -> str | None:
    """*value* rewritten in *fmt*, if that keeps all of it.

    ``rewrite("%Y%m%d", "2015-06-01")`` is ``"20150601"``. None when the value
    reads as no date or ambiguously (as two different dates), or when *fmt*
    cannot hold all of it: ``"2015-06-01 12:00:00"`` in ``%d.%m.%Y`` would
    lose the time of day, and 2075 in ``%y%m%d`` would read back as 1975.

    Args:
        fmt: The column's format.
        value: A literal the query compares the column with.

    Returns:
        The value in the column's format, or None.
    """
    moment = as_datetime(value)
    if moment is None:
        return None
    written = moment.strftime(fmt)
    try:
        # A stored value has no time zone unless its format writes one.
        back = datetime.strptime(written, fmt)  # noqa: DTZ007
    except ValueError:
        return None
    return written if back == moment.replace(tzinfo=None) else None


def _has_time(fmt: str) -> bool:
    return any(field in fmt for field in ("%H", "%M", "%S", "%f"))


def _dremio_format(fmt: str) -> str | None:
    """*fmt* in Dremio's format tokens, or None when it has no equivalent."""
    tokens = {
        "%Y": "YYYY",
        "%m": "MM",
        "%d": "DD",
        "%H": "HH24",
        "%M": "MI",
        "%S": "SS",
        "%b": "MON",
    }
    if any(field not in tokens for field in re.findall(r"%[A-Za-z]", fmt)):
        return None
    if re.search(r"[A-Za-z]", re.sub(r"%[A-Za-z]", "", fmt)):
        return None  # literal letters would need quoting
    return re.sub(r"%[A-Za-z]", lambda m: tokens[m.group(0)], fmt)


def to_date_sql(
    column: Any,
    fmt: str,
    dialect: str,
    numeric: bool = False,
    database: str | None = None,
) -> str | None:
    """*column* converted to a date (or a timestamp) in the database's own syntax.

    sqlglot writes the function and translates the format --
    ``STRPTIME`` for DuckDB, ``TO_DATE(c, 'DD.MM.YYYY')`` for PostgreSQL,
    ``STR_TO_DATE`` for MySQL -- and Dremio, which sqlglot does not cover
    here, gets its own ``TO_DATE``/``TO_TIMESTAMP`` with Dremio's tokens. The
    reading matches the one inference made:

    - A numeric column is read as an integer, then as text padded to the
      format's width, so ``950324.00`` and the ``000229`` stored as 229
      read as inference read them.
    - A two-digit year gets its century written out, the way inference
      read it (69-99 as 19xx, 00-68 as 20xx), since databases differ:
      Databricks reads ``yy`` as 20xx, PostgreSQL as the year nearest 2020.

    Args:
        column: The column as a sqlglot expression.
        fmt: The column's strftime format.
        dialect: sqlglot dialect.
        numeric: Whether the column is numeric.
        database: The database type (``dremio``, ``postgresql`` ...).

    Returns:
        The expression as SQL, or None when the format has no equivalent.
    """
    from sqlglot import exp

    text = column.copy()
    if numeric:
        text = exp.Pad(
            this=exp.cast(exp.cast(text, "BIGINT"), "VARCHAR"),
            expression=exp.Literal.number(len(example(fmt))),
            fill_pattern=exp.Literal.string("0"),
            is_left=True,
        )
    if "%y" in fmt:
        # Where the two year digits stand: 77 occurs in no day or month.
        offset = datetime(2077, 1, 2, tzinfo=UTC).strftime(fmt).index("77")
        two_digits = exp.Substring(
            this=text.copy(),
            start=exp.Literal.number(offset + 1),
            length=exp.Literal.number(2),
        )
        century = exp.Case(
            ifs=[
                exp.If(
                    this=exp.LT(
                        this=exp.cast(two_digits, "INT"),
                        expression=exp.Literal.number(69),
                    ),
                    true=exp.Literal.string("20"),
                )
            ],
            default=exp.Literal.string("19"),
        )
        pieces: list[exp.Expression] = []
        if offset:
            pieces.append(
                exp.Substring(
                    this=text.copy(),
                    start=exp.Literal.number(1),
                    length=exp.Literal.number(offset),
                )
            )
        pieces += [
            century,
            exp.Substring(this=text.copy(), start=exp.Literal.number(offset + 1)),
        ]
        text = exp.Concat(expressions=pieces)
        fmt = fmt.replace("%y", "%Y")
    if database == "dremio":
        tokens = _dremio_format(fmt)
        if tokens is None:
            return None
        function = "TO_TIMESTAMP" if _has_time(fmt) else "TO_DATE"
        return exp.Anonymous(
            this=function, expressions=[text, exp.Literal.string(tokens)]
        ).sql(dialect="dremio")
    convert = exp.StrToTime if _has_time(fmt) else exp.StrToDate
    return convert(this=text, format=exp.Literal.string(fmt)).sql(dialect=dialect)
