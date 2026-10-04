"""The OBA shapes select what they must check, and nothing they must not.

Two targeting mistakes in ontology/oba-shacl.ttl:

- oba:View and oba:ViewColumn were declared owl:Class, and TableShape selects
  every owl:Class: validating with the vocabulary merged in (pyshacl -e
  oba.ttl) reported them as tables missing oba:tableName and oba:schemaName.
- RelationshipShape selected subjects of oba:relationshipType, the very
  annotation it requires, so a relationship without it -- or without its
  domain, range and label -- was never checked and the ontology conformed.
"""

from pathlib import Path

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import RDFS

from src.database_manager import ColumnInfo, TableInfo, ViewInfo
from src.ontology_generator import OntologyGenerator

pyshacl = pytest.importorskip("pyshacl")

ROOT = Path(__file__).parent.parent / "ontology"
SHAPES = (ROOT / "oba-shacl.ttl").read_text(encoding="utf-8")
VOCABULARY = (ROOT / "oba.ttl").read_text(encoding="utf-8")
OBA = "https://ralforion.com/ns/oba#"


def _ontology() -> Graph:
    def col(name: str, pk: bool = False) -> ColumnInfo:
        return ColumnInfo(
            name=name,
            data_type="INTEGER",
            is_nullable=not pk,
            is_primary_key=pk,
            is_foreign_key=False,
        )

    tables = [
        TableInfo(
            name="customers",
            schema="shop",
            columns=[col("id", True)],
            primary_keys=["id"],
            foreign_keys=[],
        ),
        TableInfo(
            name="orders",
            schema="shop",
            columns=[col("id", True), col("customer_id")],
            primary_keys=["id"],
            foreign_keys=[
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                }
            ],
        ),
    ]
    views = [
        ViewInfo(
            name="order_counts",
            schema="shop",
            definition="SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY 1",
        )
    ]
    generator = OntologyGenerator()
    generator.generate_from_schema(
        tables, include_inferred_relationships=False, views_info=views
    )
    return generator.graph


def _violations(graph: Graph, *, with_vocabulary: bool) -> tuple[bool, int]:
    conforms, _, text = pyshacl.validate(
        graph.serialize(format="turtle"),
        shacl_graph=SHAPES,
        ont_graph=VOCABULARY if with_vocabulary else None,
        data_graph_format="turtle",
        shacl_graph_format="turtle",
        ont_graph_format="turtle",
        inference="none",
        advanced=True,
    )
    return conforms, text.count("Constraint Violation")


def test_a_generated_ontology_conforms_with_the_vocabulary_merged_in():
    graph = _ontology()

    assert _violations(graph, with_vocabulary=False) == (True, 0)
    assert _violations(graph, with_vocabulary=True) == (True, 0)


def test_vocabulary_classes_are_not_database_tables():
    vocabulary = Graph()
    vocabulary.parse(data=VOCABULARY, format="turtle")
    owl_class = URIRef("http://www.w3.org/2002/07/owl#Class")

    for term in ("View", "ViewColumn"):
        assert (URIRef(OBA + term), None, owl_class) not in vocabulary


@pytest.mark.parametrize(
    "stripped",
    [
        (URIRef(OBA + "relationshipType"),),
        (URIRef(OBA + "relationshipType"), RDFS.domain, RDFS.range, RDFS.label),
    ],
)
def test_a_relationship_missing_required_annotations_is_reported(stripped):
    graph = _ontology()
    forward = next(
        s
        for s in graph.subjects(URIRef(OBA + "foreignKeyColumn"), None)
        if "orders" in str(s)
    )
    for predicate in stripped:
        graph.remove((forward, predicate, None))

    conforms, violations = _violations(graph, with_vocabulary=False)

    assert not conforms
    assert violations == len(stripped)
