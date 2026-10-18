"""OBQC weighs relationships by their evidence: declared, checked or guessed.

Warehouses rarely declare keys, so their relationships are inferred from
column names. A finding resting on such a guess is reported, not blocking,
until validate_relationship has checked it; its verdicts change the rules.
"""

from rdflib import Literal, Namespace
from rdflib.namespace import OWL, RDF

from src.constants import OBA_NAMESPACE
from src.database_manager import ColumnInfo, TableInfo
from src.obqc_validator import OBQCSeverity, OBQCValidator
from src.ontology_generator import OntologyGenerator

VOCAB = "http://example.com/ontology/"
OBA = Namespace(OBA_NAMESPACE)

# Total of customers (parent) across its orders (children): inflated.
PARENT_MEASURE = (
    "SELECT SUM(customers.credit_limit) FROM customers "
    "JOIN orders ON orders.customer_id = customers.id"
)
# Total of orders across the customer lookup: fine if customers.id is unique.
CHILD_MEASURE = (
    "SELECT customers.name, SUM(orders.total) FROM orders "
    "JOIN customers ON orders.customer_id = customers.id GROUP BY customers.name"
)


def _col(name: str, data_type: str = "integer", fk: bool = False) -> ColumnInfo:
    return ColumnInfo(
        name=name,
        data_type=data_type,
        is_nullable=True,
        is_primary_key=False,
        is_foreign_key=fk,
    )


def _validator(
    declared: bool = False, status: str | None = None
) -> tuple[OBQCValidator, object]:
    """Customers and orders, related by a declared or an inferred key."""
    customers = TableInfo(
        name="customers",
        schema="sales",
        columns=[
            _col("id"),
            _col("name", "varchar(100)"),
            _col("credit_limit", "decimal"),
        ],
        primary_keys=["id"] if declared else [],
        foreign_keys=[],
    )
    orders = TableInfo(
        name="orders",
        schema="sales",
        columns=[
            _col("id"),
            _col("customer_id", fk=declared),
            _col("total", "decimal"),
        ],
        primary_keys=["id"] if declared else [],
        foreign_keys=(
            [
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                }
            ]
            if declared
            else []
        ),
    )
    generator = OntologyGenerator(base_uri=VOCAB)
    generator.generate_from_schema([customers, orders])
    graph = generator.graph
    (prop,) = [
        p
        for p in graph.subjects(OBA.foreignKeyColumn, Literal("customer_id"))
        if (p, RDF.type, OWL.ObjectProperty) in graph
    ]
    if status:
        graph.add((prop, OBA.validationStatus, Literal(status)))
    validator = OBQCValidator()
    validator.load_ontology(graph, VOCAB)
    return validator, prop


def _fan_traps(result):
    return [i for i in result.issues if i.issue_type.name == "FAN_TRAP_DETECTED"]


def test_relationship_is_inferred_in_the_keyless_schema() -> None:
    validator, _ = _validator()
    (rel,) = validator._schema_cache.relationships.values()
    assert rel.inferred and rel.assumed


def test_fan_out_on_a_guessed_relationship_warns_without_blocking() -> None:
    validator, _ = _validator()
    result = validator.validate(PARENT_MEASURE)
    assert result.is_valid
    assert result.fan_trap_risk and not result.fan_trap_blocking
    assert not result.fan_trap_overridden
    (issue,) = [i for i in _fan_traps(result) if i.severity == OBQCSeverity.WARNING]
    assert "inferred from column names" in issue.message
    (finding,) = result.fan_trap_findings
    assert finding["assumed"] is True
    assert finding["relationship"] == "orders.customer_id -> customers.id"


def test_fan_out_on_a_declared_relationship_still_blocks() -> None:
    validator, _ = _validator(declared=True)
    result = validator.validate(PARENT_MEASURE)
    assert not result.is_valid and result.fan_trap_blocking
    assert result.fan_trap_findings[0]["assumed"] is False


def test_confirmed_relationship_blocks_like_a_declared_one() -> None:
    validator, _ = _validator(status="confirmed")
    result = validator.validate(PARENT_MEASURE)
    assert not result.is_valid and result.fan_trap_blocking


def test_non_unique_target_fans_out_from_the_child_too() -> None:
    """customers repeats ids (history rows): the lookup multiplies orders."""
    validator, _ = _validator(status="target_not_unique")
    result = validator.validate(CHILD_MEASURE)
    assert not result.is_valid and result.fan_trap_blocking
    (finding,) = result.fan_trap_findings
    assert finding["measure_table"] == "orders"
    assert finding["fan_out_table"] == "customers"


def test_unique_target_lookup_is_fine() -> None:
    validator, _ = _validator(declared=True)
    result = validator.validate(CHILD_MEASURE)
    assert result.is_valid and not result.fan_trap_risk
    assert not [i for i in result.issues if i.severity == OBQCSeverity.INFO]


def test_lookup_through_a_guessed_key_names_the_assumption() -> None:
    validator, _ = _validator()
    result = validator.validate(CHILD_MEASURE)
    assert result.is_valid and not result.fan_trap_risk
    (note,) = [i for i in result.issues if i.severity == OBQCSeverity.INFO]
    assert note.message.startswith("Assumes customers.id is unique")
    assert "validate_relationship('orders', 'customer_id')" in note.suggestion


def test_checked_key_is_no_longer_named_as_an_assumption() -> None:
    validator, _ = _validator(status="partial")
    result = validator.validate(CHILD_MEASURE)
    assert not [i for i in result.issues if i.severity == OBQCSeverity.INFO]


def test_refuted_relationship_is_not_a_join_to_lean_on() -> None:
    validator, _ = _validator(status="refuted")
    assert not validator._schema_cache.relationships
    result = validator.validate(PARENT_MEASURE)
    assert not result.fan_trap_risk
    assert any(i.issue_type.name == "INVALID_JOIN" for i in result.issues)
