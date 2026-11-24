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
  ontology.

The idea follows NVIDIA Auto Ontology's certification, where experts mark
elements as reviewed; here it is three annotations and one tool.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from .constants import OBA_NAMESPACE
from .relationship_validation import find_relationships

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
            ]
        )

    def label(self) -> str:
        if self.to_table:
            return f"{self.table}.{self.column} -> {self.to_table}"
        return f"{self.table}.{self.column}" if self.column else self.table

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _literal(graph: Graph, subject: Any, predicate: URIRef) -> str | None:
    value = graph.value(subject, predicate)
    return str(value) if value is not None else None


def elements(graph: Graph, review: Review) -> list[URIRef]:
    """The nodes *review* is about: a table's class, a column, or a relationship."""
    if review.to_table:
        if not review.column:
            return []
        return [
            URIRef(ref.property_uri)
            for ref in find_relationships(
                graph, review.table, review.column, review.to_table
            )
        ]
    table = review.table.lower()
    if review.column:
        column = review.column.lower()
        return [
            URIRef(str(prop))
            for prop in graph.subjects(RDF.type, OWL.DatatypeProperty)
            if (_literal(graph, prop, OBA.tableName) or "").lower() == table
            and (_literal(graph, prop, OBA.columnName) or "").lower() == column
        ]
    return [
        URIRef(str(cls))
        for cls in graph.subjects(RDF.type, OWL.Class)
        if (_literal(graph, cls, OBA.tableName) or "").lower() == table
    ]


def record(graph: Graph, review: Review) -> int:
    """Write *review* onto the elements it is about; ``draft`` removes one.

    Returns:
        How many nodes carry it now (or had it removed); 0 when the ontology
        has no such element.
    """
    nodes = elements(graph, review)
    for node in nodes:
        for predicate in REVIEW_PROPERTIES:
            graph.remove((node, predicate, None))
        if review.status == DRAFT:
            continue
        graph.add((node, OBA.reviewStatus, Literal(review.status)))
        if review.at:
            graph.add((node, OBA.reviewedAt, Literal(review.at, datatype=XSD.dateTime)))
        if review.note:
            graph.add((node, OBA.reviewNote, Literal(review.note)))
        if review.reviewer:
            graph.add((node, OBA.reviewedBy, Literal(review.reviewer)))
    return len(nodes)


def reapply(graph: Graph, recorded: dict[str, dict[str, Any]]) -> int:
    """Apply the reviews kept in the workspace to a regenerated ontology.

    Returns:
        How many reviews found their element.
    """
    applied = 0
    for data in recorded.values():
        try:
            review = Review(**data)
        except TypeError:
            continue
        if review.status in STATUSES and record(graph, review):
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
    relationships = []
    for prop in set(graph.subjects(OBA.foreignKeyColumn, None)):
        domain = graph.value(prop, RDFS.domain)
        from_table = _literal(graph, domain, OBA.tableName) if domain else None
        inferred = (
            _literal(graph, prop, OBA.isInferredRelationship) or ""
        ).lower() == "true"
        relationships.append(
            {
                "relationship": (
                    f"{from_table}.{_literal(graph, prop, OBA.foreignKeyColumn)} -> "
                    f"{_literal(graph, prop, OBA.referencedTable)}"
                ),
                "source": (
                    f"inferred ({_literal(graph, prop, OBA.inferencePattern) or 'names'})"
                    if inferred
                    else "declared"
                ),
                "checked": _literal(graph, prop, OBA.validationStatus),
                "review": status_of(graph, prop) or DRAFT,
            }
        )
    relationships.sort(key=lambda r: (r["review"] != DRAFT, r["relationship"]))
    counts: dict[str, dict[str, int]] = {}
    for kind, nodes in (
        ("tables", graph.subjects(RDF.type, OWL.Class)),
        ("columns", graph.subjects(RDF.type, OWL.DatatypeProperty)),
        ("relationships", graph.subjects(OBA.foreignKeyColumn, None)),
    ):
        tally = {REVIEWED: 0, REJECTED: 0, DRAFT: 0}
        for node in set(nodes):
            if kind != "relationships" and _literal(graph, node, OBA.tableName) is None:
                continue
            tally[status_of(graph, node) or DRAFT] += 1
        counts[kind] = tally
    return {"counts": counts, "relationships": relationships}
