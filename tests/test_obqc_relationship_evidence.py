"""OBQC weighs relationships by their evidence: declared, checked or guessed.

Warehouses rarely declare keys, so their relationships are inferred from
column names. A finding resting on such a guess is reported, not blocking,
until validate_relationship has checked it; its verdicts change the rules.
"""

from rdflib import Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

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


# --- Review round: evidence must not let one finding mask another ----------


def _table(name: str, columns: list[str], fks: dict[str, str] | None = None):
    """A table; ``fks`` maps declared key columns to the table they reference."""
    fks = fks or {}
    return TableInfo(
        name=name,
        schema="sales",
        columns=[
            _col(c, "decimal" if c in ("total", "credit") else "integer", c in fks)
            for c in columns
        ],
        primary_keys=["id"] if "id" in columns else [],
        foreign_keys=[
            {"column": c, "referenced_table": t, "referenced_column": "id"}
            for c, t in fks.items()
        ],
    )


def _load(tables: list[TableInfo]):
    generator = OntologyGenerator(base_uri=VOCAB)
    generator.generate_from_schema(tables)
    return generator.graph


def _keyless(name: str, columns: list[str]) -> TableInfo:
    table = _table(name, columns)
    table.primary_keys = []
    return table


def _validate(graph, sql: str):
    validator = OBQCValidator()
    validator.load_ontology(graph, VOCAB)
    return validator.validate(sql)


# Declared: customers <- orders <- order_items, and customers <- returns.
DECLARED = [
    _table("customers", ["id", "name"]),
    _table("orders", ["id", "customer_id", "total"], {"customer_id": "customers"}),
    _table("returns", ["id", "customer_id"], {"customer_id": "customers"}),
    _table("order_items", ["id", "order_id"], {"order_id": "orders"}),
]
# Keyless, related only by names: leads <- visits <- clicks <- events.
GUESSED = [
    _keyless("leads", ["id", "credit"]),
    _keyless("visits", ["id", "lead_id"]),
    _keyless("clicks", ["id", "visit_id"]),
    _keyless("events", ["id", "click_id"]),
]


def test_guessed_measure_fan_out_does_not_end_the_check() -> None:
    """A warning-only branch must not let a blocked branch through."""
    sql = (
        "SELECT COUNT(*) FROM customers "
        "JOIN orders ON orders.customer_id = customers.id "
        "JOIN returns ON returns.customer_id = customers.id "
        "UNION ALL "
        "SELECT SUM(leads.credit) FROM leads JOIN visits ON visits.lead_id = leads.id"
    )
    result = _validate(_load(DECLARED + GUESSED), sql)
    kinds = {f["kind"]: f["assumed"] for f in result.fan_trap_findings}
    assert kinds["measure_across_fan_out"] is True
    assert kinds["disjoint_facts"] is False
    assert not result.is_valid and result.fan_trap_blocking


def test_larger_guessed_scope_does_not_hide_a_firm_one() -> None:
    sql = (
        "SELECT COUNT(*) FROM customers "
        "JOIN orders ON orders.customer_id = customers.id "
        "JOIN order_items ON order_items.order_id = orders.id "
        "UNION ALL "
        "SELECT COUNT(*) FROM leads "
        "JOIN visits ON visits.lead_id = leads.id "
        "JOIN clicks ON clicks.visit_id = visits.id "
        "JOIN events ON events.click_id = clicks.id"
    )
    result = _validate(_load(DECLARED + GUESSED), sql)
    (finding,) = [
        f for f in result.fan_trap_findings if f["kind"] == "multiple_fan_out_joins"
    ]
    assert finding["assumed"] is False
    assert finding["tables"] == ["order_items", "orders"]
    assert not result.is_valid and result.fan_trap_blocking


def test_verdict_applies_to_the_columns_the_join_uses() -> None:
    """A non-unique customers.code must not condemn a join on customers.id."""
    graph = _load(DECLARED)
    by_code = URIRef(VOCAB + "orders_has_customers_by_code")
    for p, o in (
        (RDF.type, OWL.ObjectProperty),
        (RDFS.domain, URIRef(VOCAB + "orders")),
        (RDFS.range, URIRef(VOCAB + "customers")),
        (OBA.foreignKeyColumn, Literal("customer_code")),
        (OBA.referencedTable, Literal("customers")),
        (OBA.referencedColumn, Literal("code")),
        (OBA.relationshipType, Literal("many_to_one")),
        (OBA.validationStatus, Literal("target_not_unique")),
    ):
        graph.add((by_code, p, o))
    by_id = (
        "SELECT customers.name, SUM(orders.total) FROM orders "
        "JOIN customers ON orders.customer_id = customers.id GROUP BY customers.name"
    )
    assert _validate(graph, by_id).is_valid
    by_code_sql = by_id.replace(
        "orders.customer_id = customers.id", "orders.customer_code = customers.code"
    )
    assert _validate(graph, by_code_sql).fan_trap_blocking


def test_disjointness_generated_from_guesses_only_warns() -> None:
    keyless = [
        _keyless("customers", ["id", "name"]),
        _keyless("orders", ["id", "customer_id"]),
        _keyless("returns", ["id", "customer_id"]),
    ]
    graph = _load(keyless)
    assert (
        URIRef(VOCAB + "orders"),
        OWL.disjointWith,
        URIRef(VOCAB + "returns"),
    ) in graph or (
        URIRef(VOCAB + "returns"),
        OWL.disjointWith,
        URIRef(VOCAB + "orders"),
    ) in graph
    sql = (
        "SELECT COUNT(*) FROM customers "
        "JOIN orders ON orders.customer_id = customers.id "
        "JOIN returns ON returns.customer_id = customers.id"
    )
    result = _validate(graph, sql)
    assert result.is_valid and not result.fan_trap_blocking
    (finding,) = [f for f in result.fan_trap_findings if f["kind"] == "disjoint_facts"]
    assert finding["assumed"] is True

    # Once both relationships are confirmed, the axiom stands on the data.
    for prop in graph.subjects(OBA.isInferredRelationship, Literal(True)):
        if (prop, OBA.foreignKeyColumn, None) in graph:
            graph.add((prop, OBA.validationStatus, Literal("confirmed")))
    assert _validate(graph, sql).fan_trap_blocking


def test_hand_written_disjointness_still_blocks() -> None:
    """An axiom no shared parent explains was authored, and stands."""
    graph = _load([_table("orders", ["id", "total"]), _table("returns", ["id"])])
    graph.add((URIRef(VOCAB + "orders"), OWL.disjointWith, URIRef(VOCAB + "returns")))
    result = _validate(
        graph, "SELECT COUNT(*) FROM orders JOIN returns ON returns.id = orders.id"
    )
    assert result.fan_trap_blocking
