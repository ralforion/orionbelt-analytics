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
    _table("customers", ["id", "name", "code"]),
    _table(
        "orders",
        ["id", "customer_id", "customer_code", "referrer_id", "total"],
        {"customer_id": "customers"},
    ),
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


def _add_relationship(
    graph,
    column: str,
    to_column: str,
    status: str | None = None,
    inferred: bool = False,
) -> None:
    """A further orders -> customers relationship, on other columns."""
    prop = URIRef(VOCAB + f"orders_has_customers_by_{column}")
    triples = [
        (RDF.type, OWL.ObjectProperty),
        (RDFS.domain, URIRef(VOCAB + "orders")),
        (RDFS.range, URIRef(VOCAB + "customers")),
        (OBA.foreignKeyColumn, Literal(column)),
        (OBA.referencedTable, Literal("customers")),
        (OBA.referencedColumn, Literal(to_column)),
        (OBA.relationshipType, Literal("many_to_one")),
    ]
    if status:
        triples.append((OBA.validationStatus, Literal(status)))
    if inferred:
        triples.append((OBA.isInferredRelationship, Literal(True)))
    for p, o in triples:
        graph.add((prop, p, o))


BY_ID = (
    "SELECT customers.name, SUM(orders.total) FROM orders "
    "JOIN customers ON orders.customer_id = customers.id GROUP BY customers.name"
)


def test_verdict_applies_to_the_columns_the_join_uses() -> None:
    """A non-unique customers.code must not condemn a join on customers.id."""
    graph = _load(DECLARED)
    _add_relationship(graph, "customer_code", "code", status="target_not_unique")
    assert _validate(graph, BY_ID).is_valid
    by_code = BY_ID.replace(
        "orders.customer_id = customers.id", "orders.customer_code = customers.code"
    )
    assert _validate(graph, by_code).fan_trap_blocking


def test_filters_in_on_are_not_the_join() -> None:
    """Naming the code columns in IS NOT NULL filters joins nothing on them."""
    graph = _load(DECLARED)
    _add_relationship(graph, "customer_code", "code", status="target_not_unique")
    filtered = BY_ID.replace(
        "orders.customer_id = customers.id",
        "orders.customer_id = customers.id "
        "AND orders.customer_code IS NOT NULL AND customers.code IS NOT NULL",
    )
    assert _validate(graph, filtered).is_valid


def test_guessed_branch_does_not_hide_a_firm_one_on_the_same_tables() -> None:
    """Same two tables, different keys: the firm finding wins in any order."""
    graph = _load(DECLARED)
    _add_relationship(graph, "referrer_id", "id", inferred=True)
    guessed = (
        "SELECT SUM(customers.id) FROM customers "
        "JOIN orders ON orders.referrer_id = customers.id"
    )
    firm = (
        "SELECT SUM(customers.id) FROM customers "
        "JOIN orders ON orders.customer_id = customers.id"
    )
    assert not _validate(graph, guessed).fan_trap_blocking
    for sql in (f"{guessed} UNION ALL {firm}", f"{firm} UNION ALL {guessed}"):
        result = _validate(graph, sql)
        assert result.fan_trap_blocking, sql
        (finding,) = result.fan_trap_findings
        assert finding["relationship"] == "orders.customer_id -> customers.id"


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


# --- Composite keys: every column of the key counts --------------------------


def _composite_graph():
    """shipments (order_id, line_no) -> order_lines (order_id, line_no)."""
    order_lines = TableInfo(
        name="order_lines",
        schema="sales",
        columns=[_col("order_id"), _col("line_no"), _col("qty", "decimal")],
        primary_keys=["order_id", "line_no"],
        foreign_keys=[],
    )
    shipments = TableInfo(
        name="shipments",
        schema="sales",
        columns=[_col("id"), _col("order_id", fk=True), _col("line_no", fk=True)],
        primary_keys=["id"],
        foreign_keys=[
            {
                "column": c,
                "referenced_table": "order_lines",
                "referenced_column": c,
                "constraint_name": "shipment_line_fk",
            }
            for c in ("order_id", "line_no")
        ],
    )
    graph = _load([order_lines, shipments])
    # One constraint, as the generator records it: the columns form one key.
    for prop in set(graph.subjects(OBA.foreignKeyColumn, Literal("order_id"))):
        graph.add((prop, OBA.constraintName, Literal("shipment_line_fk")))
    return graph


FULL_KEY = (
    "SELECT SUM(order_lines.qty) FROM order_lines JOIN shipments "
    "ON shipments.order_id = order_lines.order_id "
    "AND shipments.line_no = order_lines.line_no"
)


def test_composite_key_is_one_relationship_with_all_its_columns() -> None:
    validator = OBQCValidator()
    validator.load_ontology(_composite_graph(), VOCAB)
    (rel,) = validator._schema_cache.relationships.values()
    assert rel.pairs == (("line_no", "line_no"), ("order_id", "order_id"))
    assert rel.join_condition == (
        "shipments.line_no = order_lines.line_no AND "
        "shipments.order_id = order_lines.order_id"
    )


def test_join_on_the_whole_composite_key_is_valid_and_fans_out() -> None:
    result = _validate(_composite_graph(), FULL_KEY)
    assert not any(i.issue_type.name == "INVALID_JOIN" for i in result.issues)
    (finding,) = result.fan_trap_findings
    assert finding["relationship"] == (
        "shipments.(line_no, order_id) -> order_lines.(line_no, order_id)"
    )
    assert result.fan_trap_blocking


def test_join_on_part_of_a_composite_key_is_flagged() -> None:
    """Joining on order_id alone matches every line of the order."""
    partial = FULL_KEY.replace(" AND shipments.line_no = order_lines.line_no", "")
    result = _validate(_composite_graph(), partial)
    (issue,) = [i for i in result.issues if i.issue_type.name == "INVALID_JOIN"]
    assert "shipments.order_id = order_lines.order_id" in issue.suggestion
    assert "shipments.line_no = order_lines.line_no" in issue.suggestion


COMPOSITE_LOOKUP = (
    "SELECT SUM(shipments.id) FROM shipments JOIN order_lines "
    "ON shipments.order_id = order_lines.order_id "
    "AND shipments.line_no = order_lines.line_no"
)


def test_single_column_verdict_is_not_composite_key_evidence() -> None:
    """order_id repeats in a unique (order_id, line_no) key: that says nothing."""
    graph = _composite_graph()
    (prop,) = set(graph.subjects(OBA.foreignKeyColumn, Literal("order_id")))
    graph.add((prop, OBA.validationStatus, Literal("target_not_unique")))
    result = _validate(graph, COMPOSITE_LOOKUP)
    assert result.is_valid and not result.fan_trap_risk


def test_columns_of_separate_keys_on_one_property_are_separate_keys() -> None:
    """Billing and shipping customer on one property are not one composite key."""
    graph = _composite_graph()
    graph.remove((None, OBA.constraintName, None))
    validator = OBQCValidator()
    validator.load_ontology(graph, VOCAB)
    keys = sorted(r.pairs for r in validator._schema_cache.relationships.values())
    assert keys == [(("line_no", "line_no"),), (("order_id", "order_id"),)]
    single = FULL_KEY.replace(" AND shipments.line_no = order_lines.line_no", "")
    assert not any(
        i.issue_type.name == "INVALID_JOIN" for i in _validate(graph, single).issues
    )


def test_or_does_not_pick_the_unique_key() -> None:
    """id = id OR code = code may match several customers per order."""
    graph = _load(DECLARED)
    _add_relationship(graph, "customer_code", "code", status="target_not_unique")
    either = BY_ID.replace(
        "orders.customer_id = customers.id",
        "orders.customer_id = customers.id OR orders.customer_code = customers.code",
    )
    assert _validate(graph, either).fan_trap_blocking


def test_guessed_unique_key_does_not_outweigh_a_checked_repeat() -> None:
    keyless = [
        _keyless("customers", ["id", "name", "code"]),
        _keyless("orders", ["id", "customer_id", "customer_code", "total"]),
    ]
    graph = _load(keyless)
    _add_relationship(graph, "customer_code", "code", status="target_not_unique")
    both = BY_ID.replace(
        "orders.customer_id = customers.id",
        "orders.customer_id = customers.id AND orders.customer_code = customers.code",
    )
    assert _validate(graph, both).fan_trap_blocking


def test_unpairable_composite_key_falls_back_to_one_column() -> None:
    """A hand-written ontology may give columns but no join conditions."""
    graph = _composite_graph()
    graph.remove((None, OBA.sqlJoinCondition, None))
    validator = OBQCValidator()
    validator.load_ontology(graph, VOCAB)
    (rel,) = validator._schema_cache.relationships.values()
    assert len(rel.pairs) == 1


def test_parenthesized_join_columns_are_still_compared() -> None:
    """o.customer_id = (c.id) is the same join as without parentheses."""
    graph = _load(DECLARED)
    _add_relationship(graph, "customer_code", "code", status="target_not_unique")
    wrapped = BY_ID.replace("= customers.id", "= (customers.id)")
    assert _validate(graph, wrapped).is_valid


def test_unique_key_equality_bounds_the_whole_conjunction() -> None:
    """With customer_id = id in the ON, the code equality multiplies nothing."""
    graph = _load(DECLARED)
    _add_relationship(graph, "customer_code", "code", status="target_not_unique")
    both = BY_ID.replace(
        "orders.customer_id = customers.id",
        "orders.customer_id = customers.id AND orders.customer_code = customers.code",
    )
    result = _validate(graph, both)
    assert result.is_valid and not result.fan_trap_risk


# --- Ontologies saved before keys were named apart ----------------------------


def _legacy_shared_property(status: str | None = None):
    """Billing and shipping customer on one property, as older generators did."""
    generator = OntologyGenerator(base_uri=VOCAB)
    generator.generate_from_schema(
        [
            _table("customers", ["id", "name", "credit"]),
            _table("orders", ["id", "billing_customer_id", "shipping_customer_id"]),
        ],
        include_inferred_relationships=False,
    )
    graph = generator.graph
    prop = URIRef(VOCAB + "orders_has_customers")
    graph.add((prop, RDF.type, OWL.ObjectProperty))
    graph.add((prop, RDFS.domain, URIRef(VOCAB + "orders")))
    graph.add((prop, RDFS.range, URIRef(VOCAB + "customers")))
    graph.add((prop, OBA.referencedTable, Literal("customers")))
    graph.add((prop, OBA.referencedColumn, Literal("id")))
    graph.add((prop, OBA.relationshipType, Literal("many_to_one")))
    for column in ("billing_customer_id", "shipping_customer_id"):
        graph.add((prop, OBA.foreignKeyColumn, Literal(column)))
        graph.add(
            (prop, OBA.sqlJoinCondition, Literal(f"orders.{column} = customers.id"))
        )
    if status:
        graph.add((prop, OBA.validationStatus, Literal(status)))
    return graph


def test_verdict_on_a_shared_property_does_not_remove_its_other_keys() -> None:
    """Refuting shipping must leave billing standing, fan-out and all."""
    graph = _legacy_shared_property(status="refuted")
    validator = OBQCValidator()
    validator.load_ontology(graph, VOCAB)
    assert len(validator._schema_cache.relationships) == 2
    result = _validate(
        graph,
        "SELECT SUM(customers.credit) FROM customers "
        "JOIN orders ON orders.billing_customer_id = customers.id",
    )
    assert result.fan_trap_blocking


def test_shared_property_with_different_targets_is_not_checkable() -> None:
    """customer_id -> id and customer_code -> code: no single pairing is known."""
    from src.relationship_validation import REASON_AMBIGUOUS_KEY, why_not_checkable

    graph = _legacy_shared_property()
    prop = URIRef(VOCAB + "orders_has_customers")
    graph.add((prop, OBA.referencedColumn, Literal("code")))
    assert why_not_checkable(graph, prop) == REASON_AMBIGUOUS_KEY
