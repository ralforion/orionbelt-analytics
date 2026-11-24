"""Review status: what a person checked in the ontology.

Generated ontologies are drafts: tables, columns and relationships come from
the catalog and from inference. A person who checks an element marks it
``reviewed`` -- or ``rejected``, for a relationship that is no join -- with
``oba:reviewStatus``, ``oba:reviewedAt`` and, if given, ``oba:reviewNote``
and ``oba:reviewedBy``. Marking it ``draft`` again removes the review.

What it changes:

- OBQC treats a reviewed relationship as firm, like a declared key: a
  fan-trap resting on it blocks rather than warns. A rejected one is no
  relationship to join on, as a refuted one is.
- ``suggest_semantic_names`` leaves reviewed tables, columns and
  relationships alone, and ``apply_semantic_names`` does not rename them.
- Reviews are kept in the workspace and applied again to every regenerated
  ontology, each to the element it was given for: a relationship is its
  schema-qualified tables and each (key column, referenced column) pair, so
  the same column guessed onto another key, a key moved to another schema or
  a composite key paired differently is another, unreviewed join. A reviewed element keeps the name
  and description it had when reviewed -- one a person approved through
  ``apply_semantic_names`` comes back with it.

The idea follows NVIDIA Auto Ontology's certification, where experts mark
elements as reviewed; here it is three annotations and one tool.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from .constants import OBA_NAMESPACE
from .relationship_validation import column_pairs

OBA = Namespace(OBA_NAMESPACE)

REVIEWED = "reviewed"
REJECTED = "rejected"
DRAFT = "draft"
STATUSES = (REVIEWED, REJECTED, DRAFT)

REVIEW_PROPERTIES = (
    OBA.reviewStatus,
    OBA.reviewedAt,
    OBA.reviewNote,
    OBA.reviewedBy,
)


def _split(names: str | None) -> list[str]:
    """Comma-separated column names as a list."""
    return [n.strip() for n in (names or "").split(",") if n.strip()]


def _qualified(schema: str | None, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def _columns(table: str, names: str | None) -> str:
    columns = _split(names)
    return (
        f"{table}.{columns[0]}"
        if len(columns) == 1
        else (f"{table}.({','.join(columns)})")
    )


@dataclass(frozen=True)
class Review:
    """A person's verdict on one table, column or relationship."""

    table: str
    #: The column; for a relationship its key column(s), comma-separated.
    column: str | None
    #: The table a relationship references; None for a table or a column.
    to_table: str | None
    status: str
    note: str | None = None
    reviewer: str | None = None
    at: str = ""
    #: The column(s) a relationship references, comma-separated in the order
    #: of *column*: the pairs are its identity -- customer_id onto
    #: customers.legacy_id is another join than onto customers.id. Optional
    #: in a request, always set once recorded.
    to_column: str | None = None
    #: The table's schema, and the referenced table's: optional in a
    #: request, set once recorded where the ontology names them.
    schema: str | None = None
    to_schema: str | None = None
    #: The label, semantic name and description the element had when
    #: reviewed: a reviewed element keeps them, also when regenerated.
    name: str | None = None
    semantic_name: str | None = None
    description: str | None = None

    @property
    def kind(self) -> str:
        if self.to_table:
            return "relationship"
        return "column" if self.column else "table"

    def key(self) -> str:
        """Identity of the element, independent of name case and pair order."""
        schema = (self.schema or "").lower()
        if not self.to_table:
            return json.dumps([schema, self.table.lower(), (self.column or "").lower()])
        columns = [c.lower() for c in _split(self.column)]
        refs = [r.lower() for r in _split(self.to_column)]
        pairs = (
            sorted(f"{c}={r}" for c, r in zip(columns, refs))
            if len(columns) == len(refs)
            else [*sorted(columns), "->", *sorted(refs)]
        )
        return json.dumps(
            [
                schema,
                self.table.lower(),
                pairs,
                (self.to_schema or "").lower(),
                self.to_table.lower(),
            ]
        )

    def label(self) -> str:
        source = _qualified(self.schema, self.table)
        if self.to_table:
            target = _qualified(self.to_schema, self.to_table)
            if self.to_column:
                target = _columns(target, self.to_column)
            return f"{_columns(source, self.column)} -> {target}"
        return f"{source}.{self.column}" if self.column else source

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _literal(graph: Graph, subject: Any, predicate: URIRef) -> str | None:
    value = graph.value(subject, predicate)
    return str(value) if value is not None else None


def _same(given: str | None, found: str | None) -> bool:
    """Whether *found* is the name *given*, case aside; any when not given."""
    return given is None or (found or "").lower() == given.lower()


@dataclass(frozen=True)
class _Relationship:
    node: URIRef
    schema: str | None
    table: str
    to_schema: str | None
    to_table: str
    #: Key columns and the columns they reference, in the same order -- by
    #: key column when the ontology pairs them, else each sorted.
    columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    #: Whether the ontology says which key column references which.
    paired: bool
    inferred: bool

    def pairs(self) -> set[tuple[str, str]]:
        return {(c.lower(), r.lower()) for c, r in zip(self.columns, self.to_columns)}


@dataclass
class Index:
    """The ontology's tables, columns and relationships, by name: one pass.

    Marking or reapplying hundreds of reviews looks each up here instead of
    scanning the graph per review.
    """

    tables: dict[str, list[tuple[str | None, URIRef]]] = field(default_factory=dict)
    columns: dict[tuple[str, str], list[tuple[str | None, URIRef]]] = field(
        default_factory=dict
    )
    relationships: list[_Relationship] = field(default_factory=list)
    by_key_column: dict[tuple[str, str], list[_Relationship]] = field(
        default_factory=dict
    )


def index(graph: Graph) -> Index:
    """Index *graph*'s elements by table, column and key column."""
    tables: dict[str, list[tuple[str | None, URIRef]]] = defaultdict(list)
    for cls in set(graph.subjects(RDF.type, OWL.Class)):
        name = _literal(graph, cls, OBA.tableName)
        if name:
            tables[name.lower()].append(
                (_literal(graph, cls, OBA.schemaName), URIRef(str(cls)))
            )
    columns: dict[tuple[str, str], list[tuple[str | None, URIRef]]] = defaultdict(list)
    for prop in set(graph.subjects(RDF.type, OWL.DatatypeProperty)):
        table = _literal(graph, prop, OBA.tableName)
        column = _literal(graph, prop, OBA.columnName)
        if table and column:
            domain = graph.value(prop, RDFS.domain)
            schema = _literal(graph, prop, OBA.schemaName) or (
                _literal(graph, domain, OBA.schemaName) if domain else None
            )
            columns[(table.lower(), column.lower())].append((schema, URIRef(str(prop))))
    relationships: list[_Relationship] = []
    by_key_column: dict[tuple[str, str], list[_Relationship]] = defaultdict(list)
    for prop in set(graph.subjects(RDF.type, OWL.ObjectProperty)):
        to_table = _literal(graph, prop, OBA.referencedTable)
        key_columns = sorted(
            {str(c) for c in graph.objects(prop, OBA.foreignKeyColumn)}, key=str.lower
        )
        domain = graph.value(prop, RDFS.domain)
        table = _literal(graph, domain, OBA.tableName) if domain else None
        if not (to_table and key_columns and table):
            continue
        schema = _literal(graph, domain, OBA.schemaName)
        # Where the target lives, as find_relationships reads it: the
        # relationship's own statement first.
        range_class = graph.value(prop, RDFS.range)
        to_schema = (
            _literal(graph, prop, OBA.referencedSchema)
            or (_literal(graph, range_class, OBA.schemaName) if range_class else None)
            or schema
        )
        pairs = column_pairs(graph, prop, table)
        if pairs:
            fk_columns = tuple(c for c, _ in pairs)
            ref_columns = tuple(r for _, r in pairs)
        else:
            fk_columns = tuple(key_columns)
            ref_columns = tuple(
                sorted(
                    {str(c) for c in graph.objects(prop, OBA.referencedColumn)},
                    key=str.lower,
                )
            )
        rel = _Relationship(
            node=URIRef(str(prop)),
            schema=schema,
            table=table,
            to_schema=to_schema,
            to_table=to_table,
            columns=fk_columns,
            to_columns=ref_columns,
            paired=bool(pairs),
            inferred=(_literal(graph, prop, OBA.isInferredRelationship) or "").lower()
            == "true",
        )
        relationships.append(rel)
        for column in {c.lower() for c in key_columns}:
            by_key_column[(table.lower(), column)].append(rel)
    return Index(dict(tables), dict(columns), relationships, dict(by_key_column))


def _matches(review: Review, idx: Index) -> list[_Relationship]:
    """The relationships *review* names.

    All of its key columns (a composite key is named whole), the referenced
    table, and -- where given -- the schemas and the columns referenced, as
    pairs in the order of the key columns.
    """
    columns = _split(review.column)
    if not (columns and review.to_table):
        return []
    wanted = {c.lower() for c in columns}
    refs = _split(review.to_column)
    found = []
    for rel in idx.by_key_column.get((review.table.lower(), columns[0].lower()), []):
        if not (
            rel.to_table.lower() == review.to_table.lower()
            and {c.lower() for c in rel.columns} == wanted
            and _same(review.schema, rel.schema)
            and _same(review.to_schema, rel.to_schema)
        ):
            continue
        if review.to_column is not None:
            if rel.paired:
                if (
                    len(refs) != len(columns)
                    or {(c.lower(), r.lower()) for c, r in zip(columns, refs)}
                    != rel.pairs()
                ):
                    continue
            elif {r.lower() for r in refs} != {r.lower() for r in rel.to_columns}:
                continue
        found.append(rel)
    return found


def _resolved(review: Review, idx: Index) -> list[tuple[URIRef, Review]]:
    """The elements *review* names, each with *review* at its full identity."""
    if review.to_table:
        return [
            (
                rel.node,
                replace(
                    review,
                    schema=rel.schema,
                    table=rel.table,
                    column=",".join(rel.columns),
                    to_schema=rel.to_schema,
                    to_table=rel.to_table,
                    to_column=",".join(rel.to_columns),
                ),
            )
            for rel in _matches(review, idx)
        ]
    table = review.table.lower()
    found = (
        idx.columns.get((table, review.column.lower()), [])
        if review.column
        else idx.tables.get(table, [])
    )
    return [
        (node, replace(review, schema=schema))
        for schema, node in found
        if _same(review.schema, schema)
    ]


def elements(graph: Graph, review: Review, idx: Index | None = None) -> list[URIRef]:
    """The nodes *review* is about: a table's class, a column, or a relationship."""
    return [node for node, _ in _resolved(review, idx or index(graph))]


def _set(graph: Graph, node: URIRef, predicate: URIRef, value: str | None) -> None:
    """Make *value* the only *predicate* of *node*, unless it is already."""
    if value is None or _literal(graph, node, predicate) == value:
        return
    graph.remove((node, predicate, None))
    graph.add((node, predicate, Literal(value)))


def record(graph: Graph, review: Review, idx: Index | None = None) -> int:
    """Write *review* onto the elements it is about; ``draft`` removes one.

    A reviewed element gets back the name and description it was reviewed
    with, where they were recorded and differ -- a regenerated ontology
    starts from the database's names.

    Returns:
        How many nodes carry it now (or had it removed); 0 when the ontology
        has no such element.
    """
    nodes = elements(graph, review, idx)
    for node in nodes:
        for predicate in REVIEW_PROPERTIES:
            graph.remove((node, predicate, None))
        if review.status == DRAFT:
            continue
        if review.status == REVIEWED:
            _set(graph, node, RDFS.label, review.name)
            _set(graph, node, OBA.semanticName, review.semantic_name)
            _set(graph, node, RDFS.comment, review.description)
        graph.add((node, OBA.reviewStatus, Literal(review.status)))
        if review.at:
            graph.add((node, OBA.reviewedAt, Literal(review.at, datatype=XSD.dateTime)))
        if review.note:
            graph.add((node, OBA.reviewNote, Literal(review.note)))
        if review.reviewer:
            graph.add((node, OBA.reviewedBy, Literal(review.reviewer)))
    return len(nodes)


@dataclass
class Marked:
    """What a batch of review requests did."""

    #: One review per element marked, at its full identity.
    recorded: list[Review]
    #: Requests naming nothing in the ontology.
    missing: list[str]
    #: Requests naming several elements, with them: nothing was marked; name
    #: the schema, or the referenced columns, to choose.
    ambiguous: list[dict[str, Any]]


def mark(graph: Graph, requests: list[Review]) -> Marked:
    """Record each of *requests* in *graph*. CPU-bound; call it off the loop."""
    idx = index(graph)
    recorded: dict[str, Review] = {}
    missing: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    for request in requests:
        targets = _resolved(request, idx)
        if not targets:
            missing.append(request.label())
            continue
        by_identity: dict[str, Review] = {}
        for _, review in targets:
            by_identity.setdefault(review.key(), review)
        if len(by_identity) > 1:
            ambiguous.append(
                {
                    "item": request.label(),
                    "matches": sorted(r.label() for r in by_identity.values()),
                }
            )
            continue
        nodes = [node for node, _ in targets]
        (review,) = by_identity.values()
        if review.status == REVIEWED:
            node = nodes[0]
            review = replace(
                review,
                name=_literal(graph, node, RDFS.label),
                semantic_name=_literal(graph, node, OBA.semanticName),
                description=_literal(graph, node, RDFS.comment),
            )
        record(graph, review, idx)
        recorded[review.key()] = review
    return Marked(list(recorded.values()), missing, ambiguous)


def reapply(graph: Graph, recorded: dict[str, dict[str, Any]]) -> int:
    """Apply the reviews kept in the workspace to a regenerated ontology.

    Each goes to the element with the identity it was recorded for: a
    relationship review only to the same pairs of columns between the same
    schema-qualified tables.

    Returns:
        How many reviews found their element.
    """
    idx = index(graph)
    applied = 0
    for data in recorded.values():
        try:
            review = Review(**data)
        except TypeError:
            continue
        if review.status not in (REVIEWED, REJECTED):
            continue
        if review.to_table and not review.to_column:
            continue  # its key is unknown: it could land on another join
        if record(graph, review, idx):
            applied += 1
    return applied


def status_of(graph: Graph, node: Any) -> str | None:
    """The review status of *node*, or None for a draft."""
    status = _literal(graph, node, OBA.reviewStatus)
    return status if status in (REVIEWED, REJECTED) else None


def inventory(graph: Graph) -> dict[str, Any]:
    """What a reviewer has to look at: each relationship, and the counts.

    Relationships first, since their review changes what OBQC blocks: each
    with its identity -- the fields review_ontology takes back -- where it
    comes from (declared, or inferred and how), the latest check on the data,
    and its review status.
    """
    idx = index(graph)
    relationships = []
    for rel in idx.relationships:
        identity = Review(
            table=rel.table,
            column=",".join(rel.columns),
            to_table=rel.to_table,
            status=DRAFT,
            to_column=",".join(rel.to_columns),
            schema=rel.schema,
            to_schema=rel.to_schema,
        )
        relationships.append(
            {
                "relationship": identity.label(),
                "schema_name": rel.schema,
                "table": rel.table,
                "column": identity.column,
                "to_schema": rel.to_schema,
                "to_table": rel.to_table,
                "to_column": identity.to_column,
                "source": (
                    "inferred ("
                    f"{_literal(graph, rel.node, OBA.inferencePattern) or 'names'})"
                    if rel.inferred
                    else "declared"
                ),
                "checked": _literal(graph, rel.node, OBA.validationStatus),
                "review": status_of(graph, rel.node) or DRAFT,
            }
        )
    relationships.sort(key=lambda r: (r["review"] != DRAFT, r["relationship"]))
    counts: dict[str, dict[str, int]] = {}
    for kind, nodes in (
        ("tables", [n for ns in idx.tables.values() for _, n in ns]),
        ("columns", [n for ns in idx.columns.values() for _, n in ns]),
        ("relationships", [rel.node for rel in idx.relationships]),
    ):
        tally = {REVIEWED: 0, REJECTED: 0, DRAFT: 0}
        for node in set(nodes):
            tally[status_of(graph, node) or DRAFT] += 1
        counts[kind] = tally
    return {"counts": counts, "relationships": relationships}
