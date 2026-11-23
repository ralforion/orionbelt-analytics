"""Joins found in the data where column names do not say them.

Relationships are inferred from names: ``customer_id`` names ``customers``.
``orders.cust``, ``tickets.owner_ref`` or ``payments.acct_no`` name no table,
and nothing linked them. Here a column that looks like a reference -- named
with a key word (``_id``, ``_ref``, ``_no``, ``_code``, ...) or like a table
(``cust`` for ``customers``) -- is checked against the keys of other tables:
its sampled distinct values must be held by the key, nine in ten at least.

Values alone are weak evidence. Small numbers and dense surrogate ids
coincide with every key, so a column whose values are all shorter than three
characters is not checked, and a match must be unique: when several keys hold
the values, a key named like the column (``supplier_ref`` and ``suppliers``)
decides, and otherwise nothing is inferred. A table's own key is never a
match: it holds nearly any integer column's values. What is found is a guess
like any inferred relationship, marked ``data_containment``;
``AUTO_VALIDATE_RELATIONSHIPS`` checks it on all rows. The idea follows NVIDIA
Auto Ontology, which asks a model to pick among the keys that hold a column's
values; here only a unique match, or a name, does.

Opt-in (``OBA_DISCOVER_JOINS_BY_DATA``): it reads a sample of every table with
a candidate column (on BigQuery, billed for the columns scanned) and runs one
small lookup per candidate key.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlglot import exp

from .bookkeeping import is_bookkeeping
from .constants import DB_SQLGLOT_DIALECTS
from .sql_build import quoted, quoted_path, select_columns

logger = logging.getLogger(__name__)

#: Set to true to look for joins in the data during generate_ontology.
DISCOVER_VARIABLE = "OBA_DISCOVER_JOINS_BY_DATA"

#: Rows sampled from a table with candidate columns.
SAMPLE_ROWS = 1000
#: Distinct values of a column checked against a key.
MAX_VALUES = 100
#: Distinct values a column needs before its values say anything.
MIN_VALUES = 3
#: Characters every value must reach at least once: 1, 2, Y prove nothing.
MIN_VALUE_LENGTH = 3
#: Share of the sampled values a key must hold.
MIN_CONTAINMENT = 0.9
#: Key lookups per run at most; the rest are skipped and counted.
MAX_CHECKS = 300

PATTERN = "data_containment"

# Words a reference column ends with.
_KEY_WORDS = frozenset(
    {"id", "key", "ref", "code", "no", "num", "nr", "fk", "sk", "uuid", "guid"}
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")
_INTEGER_DECIMAL = re.compile(
    r"^(?:number|numeric|decimal)\s*\(\s*\d+\s*(?:,\s*0\s*)?\)$"
)


def enabled() -> bool:
    """Whether generate_ontology looks for joins in the data."""
    return os.getenv(DISCOVER_VARIABLE, "false").strip().lower() == "true"


@dataclass(frozen=True)
class FoundJoin:
    """A column whose values a unique key of another table holds."""

    table: str
    column: str
    target_table: str
    target_column: str
    #: Share of the sampled distinct values the key holds.
    containment: float
    #: Whether the column is also named like the target table.
    named: bool


@dataclass
class Discovery:
    """What a run found, and what it left unchecked."""

    found: list[FoundJoin]
    #: Columns several keys held, with no name to choose between them.
    ambiguous: list[str]
    #: Key lookups skipped for MAX_CHECKS; a column with any skipped is
    #: linked to nothing.
    skipped_checks: int = 0


def _words(name: str) -> list[str]:
    return [
        word.lower()
        for part in _SEPARATORS.split(name)
        for word in _CAMEL.split(part)
        if word
    ]


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def kind(data_type: str) -> str | None:
    """``integer`` or ``text`` for a type a key may have; None otherwise.

    Dates, decimals with a fraction, floats and booleans are never keys here.
    """
    t = data_type.strip().lower()
    if "int" in t and "interval" not in t and "point" not in t:
        return "integer"
    if _INTEGER_DECIMAL.match(t) or t == "number":
        return "integer"
    if any(word in t for word in ("char", "text", "string", "uuid", "clob")):
        return "text"
    return None


def _names(column: str, table: str) -> bool:
    """Whether *column* is named like *table*: ``cust`` and ``customers``."""
    words = [w for w in _words(column) if w not in _KEY_WORDS]
    targets = [_singular(w) for w in _words(table)]
    return any(
        len(word) >= 3
        and any(t.startswith(word) or word.startswith(t) for t in targets)
        for word in words
    )


def _looks_like_a_reference(column: str, tables: Iterable[str]) -> bool:
    words = _words(column)
    if words and words[-1] in _KEY_WORDS and len(words) > 1:
        return True
    return any(_names(column, table) for table in tables)


@dataclass(frozen=True)
class _Key:
    table: Any
    column: str
    kind: str


def _keys(tables: list[Any]) -> list[_Key]:
    """Each table's one-column key: its primary key, or an ``id`` without one."""
    keys = []
    for table in tables:
        primary = list(table.primary_keys or [])
        if len(primary) > 1:
            continue
        name = primary[0] if primary else None
        if name is None:
            name = next((c.name for c in table.columns if c.name.lower() == "id"), None)
        column = next((c for c in table.columns if c.name == name), None)
        if column is None:
            continue
        key_kind = kind(column.data_type)
        if key_kind:
            keys.append(_Key(table, column.name, key_kind))
    return keys


def candidates(
    tables: list[Any], linked: Iterable[tuple[str, str]]
) -> dict[str, list[tuple[str, str]]]:
    """Per table, the columns worth checking: (column, kind).

    A column that looks like a reference and is no key the table declares or
    one inferred from names, nor the table's own one-column key, nor a
    bookkeeping column, and is of a type a key may have.

    Args:
        tables: Analyzed tables (``TableInfo``).
        linked: (table, column) already in a relationship, lower-cased.
    """
    known = {(t.lower(), c.lower()) for t, c in linked}
    names = [t.name for t in tables]
    found: dict[str, list[tuple[str, str]]] = {}
    for table in tables:
        own = [k.lower() for k in table.primary_keys or []]
        declared = {fk["column"].lower() for fk in table.foreign_keys or []}
        for column in table.columns:
            lowered = column.name.lower()
            column_kind = kind(column.data_type)
            if (
                column_kind is None
                or lowered in declared
                or (table.name.lower(), lowered) in known
                or (len(own) == 1 and lowered == own[0])
                or is_bookkeeping(lowered)
                or not _looks_like_a_reference(
                    column.name, (n for n in names if n != table.name)
                )
            ):
                continue
            found.setdefault(table.name, []).append((column.name, column_kind))
    return found


def _path(table: Any, schema_name: str | None, db_type: str) -> list[str]:
    schema = table.schema if table.schema and table.schema != "default" else schema_name
    path = [schema, table.name]
    if db_type == "dremio":
        path = [p for part in path for p in str(part or "").split(".")]
    return [str(p) for p in path if p]


def _rows(result: dict[str, Any]) -> list[list[Any]]:
    return [
        list(row.values()) if isinstance(row, dict) else list(row)
        for row in result.get("data") or []
    ]


def _value(value: Any, value_kind: str) -> Any:
    """*value* as a key of *value_kind* would hold it, or None if it cannot."""
    if value is None or isinstance(value, bool):
        return None
    if value_kind == "integer":
        if isinstance(value, int):
            return value
        if isinstance(value, Decimal) and value == value.to_integral_value():
            return int(value)
        return None
    if isinstance(value, (bytes, bytearray)):
        return None
    return str(value)


def _literal(value: Any) -> exp.Expression:
    if isinstance(value, int):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def held_query(dialect: str, path: list[str], key: str, values: list[Any]) -> str:
    """``SELECT COUNT(DISTINCT key) FROM path WHERE key IN (values)``."""
    column = exp.column(quoted(key))
    return (
        exp.select(exp.Count(this=exp.Distinct(expressions=[column.copy()])))
        .from_(quoted_path(*path))
        .where(exp.In(this=column, expressions=[_literal(v) for v in values]))
        .sql(dialect=dialect)
    )


def discover(
    db_manager: Any,
    tables: list[Any],
    linked: Iterable[tuple[str, str]],
    schema_name: str | None = None,
) -> Discovery:
    """Look for the keys whose values the candidate columns hold.

    Args:
        db_manager: The connection's database manager.
        tables: Analyzed tables of one schema.
        linked: (table, column) already in a relationship: declared, or
            inferred from names.
        schema_name: The tables' schema.

    Returns:
        The joins found, the columns left ambiguous, and how many lookups the
        cap skipped. A table or key that cannot be read is passed over.
    """
    db_type = (getattr(db_manager, "connection_info", None) or {}).get("type", "")
    dialect = DB_SQLGLOT_DIALECTS.get(db_type, "postgres")
    by_name = {t.name: t for t in tables}
    keys = _keys(tables)
    discovery = Discovery(found=[], ambiguous=[])
    checks = 0
    for table_name, columns in candidates(tables, linked).items():
        table = by_name[table_name]
        sql = select_columns(
            dialect,
            SAMPLE_ROWS,
            [c for c, _ in columns],
            *_path(table, schema_name, db_type),
        )
        try:
            result = db_manager.execute_sql_query(sql, SAMPLE_ROWS)
        except Exception as e:
            logger.debug(f"Join discovery could not sample {table_name}: {e}")
            continue
        if not result.get("success"):
            continue
        rows = _rows(result)
        for position, (column, column_kind) in enumerate(columns):
            values = sorted(
                {
                    v
                    for row in rows
                    if position < len(row)
                    and (v := _value(row[position], column_kind)) is not None
                },
                key=str,
            )[:MAX_VALUES]
            if len(values) < MIN_VALUES or all(
                len(str(v)) < MIN_VALUE_LENGTH for v in values
            ):
                continue
            held: list[tuple[_Key, float]] = []
            cut_short = False
            for key in keys:
                if key.table is table or key.kind != column_kind:
                    continue
                if checks >= MAX_CHECKS:
                    discovery.skipped_checks += 1
                    cut_short = True
                    continue
                checks += 1
                sql = held_query(
                    dialect,
                    _path(key.table, schema_name, db_type),
                    key.column,
                    values,
                )
                try:
                    answer = db_manager.execute_sql_query(sql, 1)
                except Exception as e:
                    logger.debug(f"Join discovery lookup failed: {e}")
                    continue
                count = (_rows(answer) or [[0]])[0][0] if answer.get("success") else 0
                share = int(count or 0) / len(values)
                if share >= MIN_CONTAINMENT:
                    held.append((key, share))
            if cut_short:
                # A key left unchecked may hold the values too: no match is
                # known to be the only one.
                continue
            chosen = (
                held
                if len(held) == 1
                else [
                    (key, share)
                    for key, share in held
                    if _names(column, key.table.name)
                ]
            )
            if len(chosen) == 1:
                key, share = chosen[0]
                discovery.found.append(
                    FoundJoin(
                        table=table_name,
                        column=column,
                        target_table=key.table.name,
                        target_column=key.column,
                        containment=round(share, 3),
                        named=_names(column, key.table.name),
                    )
                )
            elif held:
                discovery.ambiguous.append(f"{table_name}.{column}")
    return discovery


def linked_by_names(tables: list[Any]) -> set[tuple[str, str]]:
    """(table, column) of the relationships names suggest, lower-cased."""
    from .ontology_generator import OntologyGenerator

    return {
        (rel.source_table.lower(), rel.column.lower())
        for rel in OntologyGenerator().infer_relationships(tables)
    }


def run(db_manager: Any, tables: list[Any], schema_name: str | None) -> Discovery:
    """:func:`discover`, past the relationships names already suggest."""
    return discover(db_manager, tables, linked_by_names(tables), schema_name)


def as_inferred(discovery: Discovery | None) -> list[Any]:
    """The joins found, as relationships the ontology generator infers.

    Named like their table as well, they are as firm as name inference gets
    (``high``); by their values alone, ``medium``.
    """
    from .ontology_generator import InferredRelationship

    if discovery is None:
        return []
    return [
        InferredRelationship(
            source_table=join.table,
            column=join.column,
            target_table=join.target_table,
            target_column=join.target_column,
            confidence="high" if join.named else "medium",
            pattern_matched=PATTERN,
        )
        for join in discovery.found
    ]


def note(discovery: Discovery | None) -> str:
    """What discovery found, for the generate_ontology result."""
    if discovery is None or not (
        discovery.found or discovery.ambiguous or discovery.skipped_checks
    ):
        return ""
    lines = [f"\n\nJOINS FOUND IN THE DATA: {len(discovery.found)}"]
    lines += [
        f"  {j.table}.{j.column} -> {j.target_table}.{j.target_column}"
        f" ({j.containment:.0%} of sampled values)"
        for j in discovery.found[:20]
    ]
    if len(discovery.found) > 20:
        lines.append(f"  ... and {len(discovery.found) - 20} more")
    lines.append(
        "Inferred, not declared: AUTO_VALIDATE_RELATIONSHIPS or "
        "validate_relationship() checks each on all rows."
    )
    if discovery.ambiguous:
        lines.append(
            "Held by several keys, so left unlinked: "
            + ", ".join(discovery.ambiguous[:10])
            + (" ..." if len(discovery.ambiguous) > 10 else "")
        )
    if discovery.skipped_checks:
        lines.append(f"Key lookups skipped past the limit: {discovery.skipped_checks}")
    return "\n".join(lines)
