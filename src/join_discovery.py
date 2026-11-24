"""Joins found in the data where column names do not say them.

Relationships are inferred from names: ``customer_id`` names ``customers``.
``orders.cust``, ``tickets.owner_ref`` or ``purchases.vendor`` name no table,
and nothing linked them. Here a column that looks like a reference is checked
against the one-column keys of the other tables: which of them holds its
sampled distinct values, nine in ten at least.

**Names choose what to check, and rank; they never link.** A column is
checked when it ends in a key word (``_id``, ``_ref``, ``_no``, ...) or a word
of it names another table, in three steps:

1. Abbreviations of the schema's own table words: ``cust`` is the start of
   ``customer``, ``cstmr`` and ``acct`` keep the letters of ``customer`` and
   ``account`` in order.
2. Otherwise, the English words it may stand for, from wordfreq's list
   (shipped already): the word itself when it is one (``vendor``), then up to
   five words it abbreviates, most frequent first (``vndr``: vendor; ``mgr``:
   manager; ``pmt``: parliament, payment, ...). One that is a table's own word
   names that table.
3. Otherwise, what those words mean, with the embedding model GraphRAG
   already runs when it is loaded: ``vndr`` read as vendor means
   ``suppliers``, ``cust`` read as customer means ``clients`` -- only with a
   clear lead over every other table. Asked about the abbreviation itself the
   model guesses (``cust`` lands nearer ``products`` than ``customers``):
   it knows meaning, not spelling, so it is asked about the words.

**The data links.** A key that alone holds the values is checked on all rows,
as ``validate_relationship`` does, and linked only when that confirms it:
then the relationship carries the verdict, and OBQC treats it like a checked
key. Values alone are weak evidence -- numbers from dense surrogate ids
coincide with every such key, ``parent_ref`` 500..899 sits in any ``id``
running to 3000 -- so a column whose values are all shorter than three
characters is not checked, a table's own key is never a match, and numbers
(digits stored as text too) link by themselves only when a word of the column
names the table as well. Distinctive values -- ``C-0001``, a UUID -- do. What
the data cannot settle -- several keys holding the values, or numbers only one
unnamed key holds -- is listed with the keys, likeliest by name first, for the
client to confirm one (``confirm_join``). The idea follows NVIDIA Auto Ontology, which asks a model
inside its server to pick; here the client's does, or a person.

Opt-in (``OBA_DISCOVER_JOINS_BY_DATA``): it reads a sample of every table with
a candidate column (on BigQuery, billed for the columns scanned), runs one
small lookup per candidate key, and checks each unique match on all rows.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any

import numpy as np
from sqlglot import exp

from .bookkeeping import is_bookkeeping
from .constants import DB_SQLGLOT_DIALECTS
from .relationship_validation import (
    STATUS_CONFIRMED,
    RelationshipRef,
    ValidationRecord,
    build_check_queries,
    classify,
)
from .sql_build import quoted, quoted_path
from .utils import utc_now

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
#: A synonym: the model's similarity to the best table at least this...
MIN_SYNONYM = 0.5
#: ...and this far ahead of the next table.
SYNONYM_LEAD = 0.15

PATTERN = "data_containment"
CONFIRMED_PATTERN = "confirmed_join"

#: Texts in, one vector per text out: the GraphRAG embedder's _embed_texts.
Embed = Callable[[list[str]], Sequence[Any]]

# Words a reference column ends with.
_KEY_WORDS = frozenset(
    {"id", "key", "ref", "code", "no", "num", "nr", "fk", "sk", "uuid", "guid"}
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")
_INTEGER_DECIMAL = re.compile(
    r"^(?:number|numeric|decimal)\s*\(\s*\d+\s*(?:,\s*0\s*)?\)$"
)

# How a column word names a table, strongest first.
WORD, ABBREVIATION, SYNONYM = "word", "abbreviation", "synonym"
_STRENGTH = {WORD: 3, ABBREVIATION: 2, SYNONYM: 1}


def enabled() -> bool:
    """Whether generate_ontology looks for joins in the data."""
    return os.getenv(DISCOVER_VARIABLE, "false").strip().lower() == "true"


# --- Names ---------------------------------------------------------------------


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


def abbreviates(short: str, word: str, in_order_from: int = 4) -> bool:
    """Whether *short* abbreviates *word*.

    Its start (``cust``, ``emp``, ``ord``), from three letters; or its letters
    in order from the same first letter (``cstmr``, ``acct``), from
    *in_order_from* letters -- four against table words, where ``cat`` would
    otherwise abbreviate ``contract``; three against the dictionary, whose
    expansions the model then weighs (``mgr``, ``pmt``).
    """
    if len(short) < 3 or len(short) >= len(word) or short[0] != word[0]:
        return False
    if word.startswith(short):
        return True
    if len(short) < in_order_from:
        return False
    letters = iter(word)
    return all(letter in letters for letter in short)


#: English words a column word may abbreviate, most frequent first.
DICTIONARY_WORDS = 40_000
#: Expansions of a word weighed, most frequent first.
MAX_EXPANSIONS = 5
#: How much less each later expansion counts: the likelier reading first.
EXPANSION_DISCOUNT = 0.05


@lru_cache(maxsize=1)
def _dictionary() -> tuple[tuple[str, ...], frozenset[str]]:
    """English words, most frequent first, from wordfreq (shipped already)."""
    try:
        from wordfreq import top_n_list

        words = tuple(
            w for w in top_n_list("en", DICTIONARY_WORDS) if w.isalpha() and len(w) >= 3
        )
    except Exception as e:  # never stop discovery for want of a word list
        logger.debug(f"No dictionary for abbreviations: {e}")
        words = ()
    return words, frozenset(words)


@lru_cache(maxsize=4096)
def expansions(word: str) -> tuple[str, ...]:
    """The English words *word* most likely stands for, most frequent first.

    The word itself when it is one (``vendor``), then the words it
    abbreviates (``vndr``: vendor; ``pmt``: parliament, payment, ...), up to
    MAX_EXPANSIONS. Which of them a column means is for the model to weigh.
    """
    words, known = _dictionary()
    found = [word] if word in known else []
    for candidate in words:
        if len(found) >= MAX_EXPANSIONS:
            break
        if candidate != word and abbreviates(word, candidate, in_order_from=3):
            found.append(candidate)
    return tuple(found)


@dataclass(frozen=True)
class NameMatch:
    """How a column names a table."""

    how: str  # WORD, ABBREVIATION or SYNONYM
    #: 1.0 for a word or an abbreviation; the model's similarity for a synonym.
    score: float
    #: The English word it was read as, when that is not the column's own.
    via: str = ""

    def rank(self) -> tuple[int, float]:
        return _STRENGTH[self.how], self.score


class Names:
    """Which tables a column's name names, by word, abbreviation or synonym."""

    def __init__(self, tables: Iterable[str], embed: Embed | None = None) -> None:
        self._tables = list(dict.fromkeys(tables))
        self._words = {t: [_singular(w) for w in _words(t)] for t in self._tables}
        self._embed = embed
        self._vectors: dict[str, np.ndarray] | None = None

    def __call__(self, column: str, exclude: str = "") -> dict[str, NameMatch]:
        """Table -> how *column* names it, for the tables other than *exclude*."""
        tables = [t for t in self._tables if t != exclude]
        found: dict[str, NameMatch] = {}
        unresolved = []
        for word in (w for w in _words(column) if w not in _KEY_WORDS):
            hits = False
            for table in tables:
                words = self._words[table]
                if word in words or _singular(word) in words:
                    found[table] = _stronger(found.get(table), NameMatch(WORD, 1.0))
                    hits = True
                elif any(abbreviates(word, w) for w in words):
                    match = NameMatch(ABBREVIATION, 1.0)
                    found[table] = _stronger(found.get(table), match)
                    hits = True
            if not hits and len(word) >= 3:
                unresolved.append(word)
        for word in unresolved:
            # Step 2: the English words it may stand for. One that is a table's
            # own word names that table, model or not (mgr: manager).
            readings = expansions(word)
            named = False
            for reading in readings:
                for table in tables:
                    if reading in self._words[table] or (
                        _singular(reading) in self._words[table]
                    ):
                        match = NameMatch(ABBREVIATION, 1.0, via=reading)
                        found[table] = _stronger(found.get(table), match)
                        named = True
            if named:
                continue
            # Step 3: what those words mean, with the model.
            synonym = self._synonym(readings or (word,), tables)
            if synonym is not None:
                table, score, via = synonym
                match = NameMatch(SYNONYM, score, via=via)
                found[table] = _stronger(found.get(table), match)
        return found

    def _synonym(
        self, readings: Sequence[str], tables: list[str]
    ) -> tuple[str, float, str] | None:
        """The one table the readings mean by the model, with a clear lead.

        Each table's score is its best over the readings, a later (rarer)
        reading discounted; the best table must reach MIN_SYNONYM and lead the
        next by SYNONYM_LEAD.
        """
        if self._embed is None or not tables:
            return None
        try:
            vectors = self._table_vectors()
            mine = [np.asarray(v, dtype=float) for v in self._embed(list(readings))]
        except Exception as e:
            logger.debug(f"Synonym lookup unavailable: {e}")
            self._embed = None
            return None
        best: dict[str, tuple[float, str]] = {}
        for rank, (reading, vector) in enumerate(zip(readings, mine, strict=True)):
            weight = 1.0 - EXPANSION_DISCOUNT * rank
            for table in tables:
                if table not in vectors:
                    continue
                score = _cosine(vector, vectors[table]) * weight
                if score > best.get(table, (float("-inf"), ""))[0]:
                    best[table] = (score, reading)
        scores = sorted(((s, t, r) for t, (s, r) in best.items()), reverse=True)
        if not scores or scores[0][0] < MIN_SYNONYM:
            return None
        if len(scores) > 1 and scores[0][0] - scores[1][0] < SYNONYM_LEAD:
            return None
        score, table, reading = scores[0]
        return table, round(score, 3), reading

    def _table_vectors(self) -> dict[str, np.ndarray]:
        if self._vectors is None:
            assert self._embed is not None
            texts = [" ".join(_words(t)) for t in self._tables]
            self._vectors = {
                t: np.asarray(v, dtype=float)
                for t, v in zip(self._tables, self._embed(texts), strict=True)
            }
        return self._vectors


def _stronger(old: NameMatch | None, new: NameMatch) -> NameMatch:
    return new if old is None or new.rank() > old.rank() else old


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / norm) if norm else 0.0


# --- Columns and keys ----------------------------------------------------------


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
    tables: list[Any], linked: Iterable[tuple[str, str]], names: Names | None = None
) -> dict[str, list[tuple[str, str]]]:
    """Per table, the columns worth checking: (column, kind).

    A column that looks like a reference -- a key word last, or a word naming
    another table -- and is no key the table declares or one inferred from
    names, nor the table's own one-column key, nor a bookkeeping column, and
    is of a type a key may have.

    Args:
        tables: Analyzed tables (``TableInfo``).
        linked: (table, column) already in a relationship, lower-cased.
        names: How columns name tables; by abbreviation alone without one.
    """
    names = names or Names(t.name for t in tables)
    known = {(t.lower(), c.lower()) for t, c in linked}
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
            ):
                continue
            words = _words(column.name)
            keyed = len(words) > 1 and words[-1] in _KEY_WORDS
            if keyed or names(column.name, exclude=table.name):
                found.setdefault(table.name, []).append((column.name, column_kind))
    return found


# --- In the data -----------------------------------------------------------------


@dataclass(frozen=True)
class FoundJoin:
    """A column whose values one key alone holds, confirmed on all rows."""

    table: str
    column: str
    target_table: str
    target_column: str
    #: Share of the sampled distinct values the key holds.
    containment: float
    #: The check on all rows, confirmed.
    record: ValidationRecord


@dataclass(frozen=True)
class HeldBy:
    """A key that holds a column's sampled values, and how the column names it."""

    table: str
    column: str
    containment: float
    name: NameMatch | None


@dataclass(frozen=True)
class Choice:
    """A column the data cannot link alone: the client may confirm a key.

    Several keys hold its values, or only one does but the values are numbers
    and no word of the column names its table.
    """

    table: str
    column: str
    #: Likeliest first: named by a word, an abbreviation, a synonym, none.
    keys: tuple[HeldBy, ...]


@dataclass
class Discovery:
    """What a run found, what it left to the client, and what it skipped."""

    found: list[FoundJoin] = field(default_factory=list)
    to_confirm: list[Choice] = field(default_factory=list)
    #: One key held the sample, the check on all rows did not confirm it:
    #: (column, key, status).
    unconfirmed: list[tuple[str, str, str]] = field(default_factory=list)
    #: Key lookups skipped for MAX_CHECKS; a column with any skipped is
    #: linked to nothing.
    skipped_checks: int = 0


def _path(table: Any, schema_name: str | None, db_type: str) -> list[str]:
    schema = _schema(table, schema_name)
    path = [schema, table.name]
    if db_type == "dremio":
        path = [p for part in path for p in str(part or "").split(".")]
    return [str(p) for p in path if p]


def _schema(table: Any, schema_name: str | None) -> str | None:
    return table.schema if table.schema and table.schema != "default" else schema_name


def _rows(result: dict[str, Any]) -> list[list[Any]]:
    return [
        list(row.values()) if isinstance(row, dict) else list(row)
        for row in result.get("data") or []
    ]


def _number(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")


def _value(value: Any, value_kind: str) -> Any:
    """*value* as a key of *value_kind* would hold it, or None if it cannot.

    Integer columns are sampled as text (:func:`sample_query`), so a
    ``DECIMAL(20,0)`` key arrives exactly, not as the float a driver turns a
    Decimal into, which loses digits past 2**53.
    """
    if value is None or isinstance(value, bool):
        return None
    if value_kind == "integer":
        if isinstance(value, int):
            return value
        if isinstance(value, Decimal) and value == value.to_integral_value():
            return int(value)
        if isinstance(value, str) and _INTEGER_TEXT.match(value.strip()):
            return int(value.strip())
        return None
    if isinstance(value, (bytes, bytearray)):
        return None
    return str(value)


def _literal(value: Any) -> exp.Expression:
    if isinstance(value, int):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def sample_query(
    dialect: str, path: list[str], columns: list[tuple[str, str]], limit: int
) -> str:
    """``SELECT <columns> FROM path LIMIT n``, integer columns cast to text.

    Cast in SQL so the database writes each value whole: drivers hand back a
    DECIMAL as a float, and ``DECIMAL(20,0)`` keys past 2**53 lose digits.
    """
    text = exp.DataType.build("text")
    selected = [
        (
            exp.cast(exp.column(quoted(name)), text.copy())
            if column_kind == "integer"
            else exp.column(quoted(name))
        )
        for name, column_kind in columns
    ]
    return (
        exp.select(*selected)
        .from_(quoted_path(*path))
        .limit(int(limit))
        .sql(dialect=dialect)
    )


def held_query(dialect: str, path: list[str], key: str, values: list[Any]) -> str:
    """``SELECT COUNT(DISTINCT key) FROM path WHERE key IN (values)``."""
    column = exp.column(quoted(key))
    return (
        exp.select(exp.Count(this=exp.Distinct(expressions=[column.copy()])))
        .from_(quoted_path(*path))
        .where(exp.In(this=column, expressions=[_literal(v) for v in values]))
        .sql(dialect=dialect)
    )


def verify(db_manager: Any, ref: RelationshipRef) -> ValidationRecord:
    """The check validate_relationship runs, on all rows.

    Raises:
        RuntimeError: A check query failed.
    """
    db_type = (getattr(db_manager, "connection_info", None) or {}).get("type", "")
    coverage_sql, uniqueness_sql = build_check_queries(ref, db_type)
    answers = []
    for sql in (coverage_sql, uniqueness_sql):
        result = db_manager.execute_sql_query(sql, 5)
        if not result.get("success"):
            raise RuntimeError(result.get("error") or "check query failed")
        answers.append((_rows(result) or [[0, 0]])[0])
    coverage, uniqueness = answers
    checked = _number(coverage[0] if coverage else 0)
    matched = _number(coverage[1] if len(coverage) > 1 else 0)
    target_rows = _number(uniqueness[0] if uniqueness else 0)
    target_distinct = _number(uniqueness[1] if len(uniqueness) > 1 else 0)
    status, ratio = classify(checked, matched, target_rows, target_distinct)
    return ValidationRecord(
        from_schema=ref.from_schema,
        from_table=ref.from_table,
        column=ref.column,
        to_schema=ref.to_schema,
        to_table=ref.to_table,
        to_column=ref.to_column,
        status=status,
        match_ratio=ratio,
        checked_rows=checked,
        matched_rows=matched,
        target_rows=target_rows,
        target_distinct=target_distinct,
        checked_at=utc_now().isoformat(),
    )


def discover(
    db_manager: Any,
    tables: list[Any],
    linked: Iterable[tuple[str, str]],
    schema_name: str | None = None,
    embed: Embed | None = None,
) -> Discovery:
    """Look for the keys whose values the candidate columns hold.

    Args:
        db_manager: The connection's database manager.
        tables: Analyzed tables of one schema.
        linked: (table, column) already in a relationship: declared, or
            inferred from names.
        schema_name: The tables' schema.
        embed: The loaded embedding model, for synonyms; None for none.

    Returns:
        The joins found and confirmed, the ties left to the client, the
        matches all rows did not confirm, and how many lookups the cap
        skipped. A table or key that cannot be read is passed over.
    """
    db_type = (getattr(db_manager, "connection_info", None) or {}).get("type", "")
    dialect = DB_SQLGLOT_DIALECTS.get(db_type, "postgres")
    by_name = {t.name: t for t in tables}
    names = Names((t.name for t in tables), embed)
    keys = _keys(tables)
    discovery = Discovery()
    checks = 0
    for table_name, columns in candidates(tables, linked, names).items():
        table = by_name[table_name]
        sql = sample_query(
            dialect, _path(table, schema_name, db_type), columns, SAMPLE_ROWS
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
            named = names(column, exclude=table_name)
            held: list[HeldBy] = []
            cut_short = failed = False
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
                    failed = True
                    continue
                if not answer.get("success"):
                    logger.debug(f"Join discovery lookup failed: {answer.get('error')}")
                    failed = True
                    continue
                count = (_rows(answer) or [[0]])[0][0]
                share = _number(count) / len(values)
                if share >= MIN_CONTAINMENT:
                    held.append(
                        HeldBy(
                            key.table.name,
                            key.column,
                            round(share, 3),
                            named.get(key.table.name),
                        )
                    )
            if cut_short or not held:
                # Cut short, a key left unchecked may hold the values too: no
                # match is known to be the only one.
                continue
            distinctive = column_kind == "text" and not all(
                str(v).isdigit() for v in values
            )
            # A failed lookup is a key that may hold the values too: whatever
            # held them is no known unique match, only one to confirm.
            if failed or len(held) > 1 or not (distinctive or held[0].name):
                ranked = sorted(
                    held,
                    key=lambda h: (
                        tuple(-x for x in (h.name.rank() if h.name else (0, 0.0))),
                        -h.containment,
                        h.table,
                    ),
                )
                discovery.to_confirm.append(Choice(table_name, column, tuple(ranked)))
                continue
            (only,) = held
            target = by_name[only.table]
            ref = RelationshipRef(
                from_table=table_name,
                column=column,
                to_table=only.table,
                to_column=only.column,
                from_schema=_schema(table, schema_name),
                to_schema=_schema(target, schema_name),
                property_uri="",
            )
            try:
                record = verify(db_manager, ref)
            except Exception as e:
                logger.debug(f"Join discovery check failed: {e}")
                continue
            if record.status == STATUS_CONFIRMED:
                discovery.found.append(
                    FoundJoin(
                        table_name,
                        column,
                        only.table,
                        only.column,
                        only.containment,
                        record,
                    )
                )
            else:
                discovery.unconfirmed.append(
                    (
                        f"{table_name}.{column}",
                        f"{only.table}.{only.column}",
                        record.status,
                    )
                )
    return discovery


# --- Into the ontology ---------------------------------------------------------


def linked_by_names(tables: list[Any]) -> set[tuple[str, str]]:
    """(table, column) of the relationships names suggest, lower-cased."""
    from .ontology_generator import OntologyGenerator

    return {
        (rel.source_table.lower(), rel.column.lower())
        for rel in OntologyGenerator().infer_relationships(tables)
    }


def run(
    db_manager: Any,
    tables: list[Any],
    schema_name: str | None,
    embed: Embed | None = None,
) -> Discovery:
    """:func:`discover`, past the relationships names already suggest."""
    return discover(db_manager, tables, linked_by_names(tables), schema_name, embed)


def as_inferred(discovery: Discovery | None) -> list[Any]:
    """The joins found, as relationships the ontology generator infers."""
    from .ontology_generator import InferredRelationship

    if discovery is None:
        return []
    return [
        InferredRelationship(
            source_table=join.table,
            column=join.column,
            target_table=join.target_table,
            target_column=join.target_column,
            confidence="high",
            pattern_matched=PATTERN,
        )
        for join in discovery.found
    ]


def records(discovery: Discovery | None) -> list[ValidationRecord]:
    """The checks that confirmed the joins found, to record on them."""
    return [join.record for join in discovery.found] if discovery else []


def _held(key: HeldBy) -> str:
    how = ""
    if key.name is not None:
        how = f", named by {key.name.how}"
        if key.name.via:
            how += f" via {key.name.via}"
        if key.name.how == SYNONYM:
            how += f" {key.name.score:.2f}"
    return f"{key.table}.{key.column} ({key.containment:.0%}{how})"


def note(discovery: Discovery | None) -> str:
    """What discovery found and left open, for the generate_ontology result."""
    if discovery is None or not (
        discovery.found
        or discovery.to_confirm
        or discovery.unconfirmed
        or discovery.skipped_checks
    ):
        return ""
    lines = [f"\n\nJOINS FOUND IN THE DATA: {len(discovery.found)}"]
    lines += [
        f"  {j.table}.{j.column} -> {j.target_table}.{j.target_column}"
        f" (confirmed on {j.record.checked_rows} rows)"
        for j in discovery.found[:20]
    ]
    if len(discovery.found) > 20:
        lines.append(f"  ... and {len(discovery.found) - 20} more")
    if discovery.to_confirm:
        lines.append(
            f"\nJOINS TO CONFIRM: {len(discovery.to_confirm)} -- several keys "
            "hold the values, or numbers only one key holds that the column "
            "does not name: the data cannot choose. Keys likeliest first; link "
            "one with confirm_join(table, column, target_table), which checks "
            "it on all rows first:"
        )
        lines += [
            f"  {c.table}.{c.column}: " + "; ".join(_held(k) for k in c.keys)
            for c in discovery.to_confirm[:20]
        ]
        if len(discovery.to_confirm) > 20:
            lines.append(f"  ... and {len(discovery.to_confirm) - 20} more")
    if discovery.unconfirmed:
        lines.append(
            "\nHeld by the sample, not by all rows, so not linked: "
            + "; ".join(f"{c} -> {k} ({s})" for c, k, s in discovery.unconfirmed[:10])
            + (" ..." if len(discovery.unconfirmed) > 10 else "")
        )
    if discovery.skipped_checks:
        lines.append(
            f"Key lookups skipped past the limit of {MAX_CHECKS}: "
            f"{discovery.skipped_checks}; their columns are not linked."
        )
    return "\n".join(lines)
