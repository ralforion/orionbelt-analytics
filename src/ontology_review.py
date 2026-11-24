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
  ontology, each to the element it was given for: a relationship is its key
  column *and* the key it references, so the same column guessed to reference
  another key is another, unreviewed join. A reviewed element keeps the name
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


@dataclass(frozen=True)
class Review:
    """A person's verdict on one table, column or relationship."""

    table: str
    #: The column; with *to_table*, the key column of a relationship.
    column: str | None
    #: The table a relationship references; None for a table or a column.
    to_table: str | None
    status: str
    note: str | None = None
    reviewer: str | None = None
    at: str = ""
    #: The column(s) a relationship references, comma-separated: part of its
    #: identity -- customer_id onto customers.legacy_id is another join than
    #: onto customers.id. Optional in a request, always set once recorded.
    to_column: str | None = None
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
        """Identity of the element, independent of name case."""
        return json.dumps(
            [
                self.table.lower(),
                (self.column or "").lower(),
                (self.to_table or "").lower(),
                (self.to_column or "").lower(),
            ]
        )

    def label(self) -> str:
        if self.to_table:
            target = (
                f"{self.to_table}.{self.to_column}"
                if self.to_column
                else (self.to_table)
            )
            return f"{self.table}.{self.column} -> {target}"
        return f"{self.table}.{self.column}" if self.column else self.table

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _literal(graph: Graph, subject: Any, predicate: URIRef) -> str | None:
    value = graph.value(subject, predicate)
    return str(value) if value is not None else None


@dataclass(frozen=True)
class _Relationship:
    node: URIRef
    table: str
    columns: tuple[str, ...]
    to_table: str
    #: The referenced column(s), comma-separated in a stable order.
    to_column: str
    inferred: bool


@dataclass
class Index:
    """The ontology's tables, columns and relationships, by name: one pass.

    Marking or reapplying hundreds of reviews looks each up here instead of
    scanning the graph per review.
    """

    tables: dict[str, list[URIRef]] = field(default_factory=dict)
    columns: dict[tuple[str, str], list[URIRef]] = field(default_factory=dict)
    relationships: list[_Relationship] = field(default_factory=list)
    by_key_column: dict[tuple[str, str], list[_Relationship]] = field(
        default_factory=dict
    )


def index(graph: Graph) -> Index:
    """Index *graph*'s elements by table, column and key column."""
    tables: dict[str, list[URIRef]] = defaultdict(list)
    for cls in set(graph.subjects(RDF.type, OWL.Class)):
        name = _literal(graph, cls, OBA.tableName)
        if name:
            tables[name.lower()].append(URIRef(str(cls)))
    columns: dict[tuple[str, str], list[URIRef]] = defaultdict(list)
    for prop in set(graph.subjects(RDF.type, OWL.DatatypeProperty)):
        table = _literal(graph, prop, OBA.tableName)
        column = _literal(graph, prop, OBA.columnName)
        if table and column:
            columns[(table.lower(), column.lower())].append(URIRef(str(prop)))
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
        referenced = sorted(
            {str(c) for c in graph.objects(prop, OBA.referencedColumn)}, key=str.lower
        )
        rel = _Relationship(
            node=URIRef(str(prop)),
            table=table,
            columns=tuple(key_columns),
            to_table=to_table,
            to_column=",".join(referenced) or "id",
            inferred=(_literal(graph, prop, OBA.isInferredRelationship) or "").lower()
            == "true",
        )
        relationships.append(rel)
        for column in key_columns:
            by_key_column[(table.lower(), column.lower())].append(rel)
    return Index(dict(tables), dict(columns), relationships, dict(by_key_column))


def _matches(review: Review, idx: Index) -> list[_Relationship]:
    """The relationships *review* names: key column, table and, if given, key."""
    if not (review.column and review.to_table):
        return []
    return [
        rel
        for rel in idx.by_key_column.get(
            (review.table.lower(), review.column.lower()), []
        )
        if rel.to_table.lower() == review.to_table.lower()
        and (
            review.to_column is None
            or rel.to_column.lower() == review.to_column.lower()
        )
    ]


def elements(graph: Graph, review: Review, idx: Index | None = None) -> list[URIRef]:
    """The nodes *review* is about: a table's class, a column, or a relationship."""
    idx = idx or index(graph)
    if review.to_table:
        return [rel.node for rel in _matches(review, idx)]
    table = review.table.lower()
    if review.column:
        return list(idx.columns.get((table, review.column.lower()), []))
    return list(idx.tables.get(table, []))


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


def _resolved(graph: Graph, request: Review, idx: Index) -> list[Review]:
    """*request* as one review per element, each with its full identity.

    A relationship's referenced key is filled in from the ontology, and a
    reviewed element's current name and description are kept with it.
    """
    if request.to_table:
        targets = [
            (rel.node, replace(request, to_column=rel.to_column))
            for rel in _matches(request, idx)
        ]
    else:
        targets = [(node, request) for node in elements(graph, request, idx)]
    if request.status != REVIEWED:
        return [review for _, review in targets]
    return [
        replace(
            review,
            name=_literal(graph, node, RDFS.label),
            semantic_name=_literal(graph, node, OBA.semanticName),
            description=_literal(graph, node, RDFS.comment),
        )
        for node, review in targets
    ]


def mark(graph: Graph, requests: list[Review]) -> tuple[list[Review], list[str]]:
    """Record each of *requests* in *graph*. CPU-bound; call it off the loop.

    Returns:
        The reviews recorded, one per element with its full identity, and
        the requests naming nothing in the ontology.
    """
    idx = index(graph)
    recorded: dict[str, Review] = {}
    missing: list[str] = []
    for request in requests:
        reviews = _resolved(graph, request, idx)
        if not reviews:
            missing.append(request.label())
            continue
        for review in reviews:
            record(graph, review, idx)
            recorded[review.key()] = review
    return list(recorded.values()), missing


def reapply(graph: Graph, recorded: dict[str, dict[str, Any]]) -> int:
    """Apply the reviews kept in the workspace to a regenerated ontology.

    Each goes to the element with the identity it was recorded for: a
    relationship review only to the same key column onto the same key.

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
    with where it comes from -- declared, or inferred and how -- the latest
    check on the data, and its review status.
    """
    idx = index(graph)
    relationships = []
    for rel in idx.relationships:
        column = ",".join(rel.columns)
        relationships.append(
            {
                "relationship": f"{rel.table}.{column} -> {rel.to_table}.{rel.to_column}",
                "table": rel.table,
                "column": column,
                "to_table": rel.to_table,
                "to_column": rel.to_column,
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
        ("tables", [n for ns in idx.tables.values() for n in ns]),
        ("columns", [n for ns in idx.columns.values() for n in ns]),
        ("relationships", [rel.node for rel in idx.relationships]),
    ):
        tally = {REVIEWED: 0, REJECTED: 0, DRAFT: 0}
        for node in set(nodes):
            tally[status_of(graph, node) or DRAFT] += 1
        counts[kind] = tally
    return {"counts": counts, "relationships": relationships}
