"""Tests for Phase 4 optional in-process SHACL validation."""

import pytest

from src.shacl_validator import validate_ontology, shacl_available
from src.ontology_generator import OntologyGenerator
from src.database_manager import TableInfo, ColumnInfo


pytestmark = pytest.mark.skipif(
    not shacl_available(),
    reason="pyshacl not installed or oba-shacl.ttl shapes not found",
)


def _ecommerce_ttl() -> str:
    customers = TableInfo(
        name="customers", schema="public",
        columns=[
            ColumnInfo(name="id", data_type="INTEGER", is_nullable=False,
                       is_primary_key=True, is_foreign_key=False),
            ColumnInfo(name="name", data_type="VARCHAR(200)", is_nullable=False,
                       is_primary_key=False, is_foreign_key=False),
        ],
        primary_keys=["id"], foreign_keys=[], row_count=100,
    )
    orders = TableInfo(
        name="orders", schema="public",
        columns=[
            ColumnInfo(name="id", data_type="INTEGER", is_nullable=False,
                       is_primary_key=True, is_foreign_key=False),
            ColumnInfo(name="customer_id", data_type="INTEGER", is_nullable=False,
                       is_primary_key=False, is_foreign_key=True,
                       foreign_key_table="customers", foreign_key_column="id"),
        ],
        primary_keys=["id"],
        foreign_keys=[{"column": "customer_id", "referenced_table": "customers", "referenced_column": "id"}],
        row_count=1000,
    )
    gen = OntologyGenerator("http://test.com/ontology/")
    return gen.generate_from_schema([customers, orders], include_inferred_relationships=False)


def test_generated_ontology_conforms():
    """A freshly generated ontology should conform to the OBA SHACL shapes."""
    report = validate_ontology(_ecommerce_ttl())
    assert report["available"] is True
    assert report["conforms"] is True, report["report"]


def test_non_conformant_ontology_reports_violation():
    """A table class missing required oba: annotations is flagged."""
    bad_ttl = """
    @prefix oba: <https://ralforion.com/ns/oba#> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix ns: <http://test.com/ontology/> .
    ns:broken a owl:Class .
    """
    report = validate_ontology(bad_ttl)
    assert report["available"] is True
    # oba-shacl requires oba:tableName on table classes -> non-conformant
    assert report["conforms"] is False
    assert report["violations"] >= 1
