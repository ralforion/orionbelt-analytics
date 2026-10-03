"""Checking a relationship against the data, and recording the verdict.

An inferred relationship -- or one an uploaded ontology states -- is a claim
about the data that nothing had checked. A model asked to confirm one ran its
own queries and wrote free-form triples into a side graph nothing reads, so
the work changed nothing: the relationship stayed "inferred, medium", the
result was not in the ontology file, and a regeneration forgot it.

This module measures a relationship the same way every time and records the
result in the ontology itself, with terms the OBA vocabulary defines.

No MCP dependencies: importable and testable on its own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from sqlglot import exp

from .constants import DB_SQLGLOT_DIALECTS, OBA_NAMESPACE

OBA = Namespace(OBA_NAMESPACE)

# At or above: every key, give or take a few rows of drift, finds its target.
CONFIRMED_RATIO = 0.99
# At or above: the relationship holds for most rows; joins drop the rest.
PARTIAL_RATIO = 0.90

STATUS_CONFIRMED = "confirmed"
STATUS_PARTIAL = "partial"
STATUS_REFUTED = "refuted"
# The referenced column repeats values: each key matches several rows, so the
# join multiplies rows instead of looking one up.
STATUS_TARGET_NOT_UNIQUE = "target_not_unique"
# No key values to check.
STATUS_NO_DATA = "no_data"

# Properties the verdict is recorded with (ontology/oba.ttl, section 4b).
VALIDATION_PROPERTIES = (
    OBA.validationStatus,
    OBA.validationMatchRatio,
    OBA.validationCheckedRows,
    OBA.validationCheckedAt,
)


@dataclass(frozen=True)
class RelationshipRef:
    """One relationship as the ontology states it."""

    from_table: str
    column: str
    to_table: str
    to_column: str
    from_schema: str | None
    to_schema: str | None
    property_uri: str


@dataclass
class ValidationRecord:
    """What a check found, as recorded in the ontology and the workspace."""

    from_schema: str | None
    from_table: str
    column: str
    to_schema: str | None
    to_table: str
    to_column: str
    status: str
    match_ratio: float | None
    checked_rows: int
    matched_rows: int
    target_rows: int
    target_distinct: int
    checked_at: str

    def key(self) -> str:
        """Identity of the relationship, independent of name case."""
        return relationship_key(
            self.from_schema,
            self.from_table,
            self.column,
            self.to_schema,
            self.to_table,
            self.to_column,
        )

    def as_dict(self) -> dict[str, Any]:
        """The record as plain data, for metadata and tool results."""
        return asdict(self)


def relationship_key(
    from_schema: str | None,
    from_table: str,
    column: str,
    to_schema: str | None,
    to_table: str,
    to_column: str,
) -> str:
    """The key a relationship's validation is filed under.

    Both ends in full -- schema, table and column -- so a verdict never moves
    to a same-named table in another schema, nor survives its target column
    changing (``id`` to ``legacy_id``).
    """
    parts = (
        from_schema or "",
        from_table,
        column,
        to_schema or "",
        to_table,
        to_column,
    )
    return "|".join(part.lower() for part in parts)


def _table(schema: str | None, table: str, alias: str) -> exp.Table:
    return exp.Table(
        this=exp.to_identifier(table, quoted=True),
        db=exp.to_identifier(schema, quoted=True) if schema else None,
        alias=exp.TableAlias(this=exp.to_identifier(alias)),
    )


def _col(alias: str, name: str) -> exp.Column:
    return exp.Column(
        this=exp.to_identifier(name, quoted=True), table=exp.to_identifier(alias)
    )


def build_check_queries(ref: RelationshipRef, db_type: str) -> tuple[str, str]:
    """The two read-only queries that measure a relationship.

    The first counts the non-null key values and how many of them exist in the
    referenced column; the second whether that column is unique. The match is
    an uncorrelated ``IN (SELECT ...)`` rather than a join, because a join to a
    non-unique column would count a key once per matching row, and every
    supported engine runs this form (ClickHouse's joins fill a missing match
    with a default value, not NULL).

    Built as syntax trees, not text: the names come from the ontology, which a
    user may have uploaded, so they are always emitted as quoted identifiers
    in the engine's own syntax -- keeping their case and escaping any quote
    they contain.

    Args:
        ref: The relationship.
        db_type: The connected database type.

    Returns:
        ``(coverage query, uniqueness query)``.
    """
    dialect = DB_SQLGLOT_DIALECTS.get(db_type, "postgres")
    key = _col("s", ref.column)
    referenced = _col("t", ref.to_column)
    lookup = exp.select(referenced.copy()).from_(
        _table(ref.to_schema, ref.to_table, "t")
    )
    matched = exp.Sum(
        this=exp.If(
            this=exp.In(this=key.copy(), query=exp.Subquery(this=lookup)),
            true=exp.Literal.number(1),
            false=exp.Literal.number(0),
        )
    )
    coverage = (
        exp.select(
            exp.alias_(exp.Count(this=exp.Star()), "checked_rows"),
            exp.alias_(matched, "matched_rows"),
        )
        .from_(_table(ref.from_schema, ref.from_table, "s"))
        .where(exp.Not(this=exp.Is(this=key.copy(), expression=exp.Null())))
    )
    uniqueness = exp.select(
        exp.alias_(exp.Count(this=referenced.copy()), "target_rows"),
        exp.alias_(
            exp.Count(this=exp.Distinct(expressions=[referenced.copy()])),
            "target_distinct",
        ),
    ).from_(_table(ref.to_schema, ref.to_table, "t"))
    return coverage.sql(dialect=dialect), uniqueness.sql(dialect=dialect)


def classify(
    checked_rows: int, matched_rows: int, target_rows: int, target_distinct: int
) -> tuple[str, float | None]:
    """The verdict for measured counts.

    Args:
        checked_rows: Non-null key values.
        matched_rows: Of those, how many exist in the referenced column.
        target_rows: Non-null values in the referenced column.
        target_distinct: Distinct values among them.

    Returns:
        ``(status, match ratio or None when there was nothing to check)``.
    """
    if checked_rows <= 0:
        return STATUS_NO_DATA, None
    ratio = matched_rows / checked_rows
    if target_distinct < target_rows:
        return STATUS_TARGET_NOT_UNIQUE, ratio
    if ratio >= CONFIRMED_RATIO:
        return STATUS_CONFIRMED, ratio
    if ratio >= PARTIAL_RATIO:
        return STATUS_PARTIAL, ratio
    return STATUS_REFUTED, ratio


def _literal(graph: Graph, subject: Any, predicate: URIRef) -> str | None:
    value = graph.value(subject, predicate)
    return str(value) if value is not None else None


def find_relationships(
    graph: Graph,
    from_table: str,
    column: str,
    to_table: str | None = None,
    from_schema: str | None = None,
) -> list[RelationshipRef]:
    """The ontology's relationships from a key column, matched ignoring case.

    Args:
        graph: The ontology.
        from_table: Table holding the key.
        column: The key column.
        to_table: The referenced table, to choose between several.
        from_schema: The key table's schema, to choose between several.

    Returns:
        Every matching relationship that names its columns and tables.
    """
    found: list[RelationshipRef] = []
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        fk_column = _literal(graph, prop, OBA.foreignKeyColumn)
        ref_table = _literal(graph, prop, OBA.referencedTable)
        if not fk_column or not ref_table:
            continue
        domain = graph.value(prop, RDFS.domain)
        domain_table = _literal(graph, domain, OBA.tableName) if domain else None
        if not domain_table:
            continue
        if domain_table.lower() != from_table.lower():
            continue
        if fk_column.lower() != column.lower():
            continue
        if to_table and ref_table.lower() != to_table.lower():
            continue
        domain_schema = _literal(graph, domain, OBA.schemaName)
        if from_schema and (domain_schema or "").lower() != from_schema.lower():
            continue
        # The relationship's own statement of where its target lives comes
        # first: its range class may be a same-named table in another schema.
        range_class = graph.value(prop, RDFS.range)
        to_schema = (
            _literal(graph, prop, OBA.referencedSchema)
            or (_literal(graph, range_class, OBA.schemaName) if range_class else None)
            or domain_schema
        )
        found.append(
            RelationshipRef(
                from_table=domain_table,
                column=fk_column,
                to_table=ref_table,
                to_column=_literal(graph, prop, OBA.referencedColumn) or "id",
                from_schema=domain_schema,
                to_schema=to_schema,
                property_uri=str(prop),
            )
        )
    return found


def record_in_graph(graph: Graph, record: ValidationRecord) -> int:
    """Write a verdict onto every property stating the relationship.

    Replaces an earlier verdict rather than adding a second one, so the
    ontology always says what the latest check found.

    Args:
        graph: The ontology, changed in place.
        record: The verdict.

    Returns:
        How many properties carry it now; 0 if the ontology no longer states
        the relationship.
    """
    refs = [
        ref
        for ref in find_relationships(
            graph, record.from_table, record.column, record.to_table
        )
        # The same relationship end to end: a changed target column or schema
        # is a different claim, which this verdict says nothing about.
        if relationship_key(
            ref.from_schema,
            ref.from_table,
            ref.column,
            ref.to_schema,
            ref.to_table,
            ref.to_column,
        )
        == record.key()
    ]
    for ref in refs:
        prop = URIRef(ref.property_uri)
        for predicate in VALIDATION_PROPERTIES:
            graph.remove((prop, predicate, None))
        graph.add((prop, OBA.validationStatus, Literal(record.status)))
        if record.match_ratio is not None:
            graph.add(
                (
                    prop,
                    OBA.validationMatchRatio,
                    Literal(round(record.match_ratio, 4), datatype=XSD.decimal),
                )
            )
        graph.add((prop, OBA.validationCheckedRows, Literal(record.checked_rows)))
        graph.add(
            (
                prop,
                OBA.validationCheckedAt,
                Literal(record.checked_at, datatype=XSD.dateTime),
            )
        )
    return len(refs)


def apply_recorded(graph: Graph, records: dict[str, dict[str, Any]]) -> int:
    """Re-apply verdicts recorded for a schema to a freshly generated ontology.

    Args:
        graph: The ontology, changed in place.
        records: Recorded verdicts, keyed by :func:`relationship_key`.

    Returns:
        How many verdicts found their relationship again.
    """
    applied = 0
    for data in records.values():
        try:
            record = ValidationRecord(**data)
        except TypeError:
            continue
        if record_in_graph(graph, record):
            applied += 1
    return applied
