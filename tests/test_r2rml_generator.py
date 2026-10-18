"""R2RML mapping generation: valid Turtle, in the ontology's own vocabulary."""

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF

from src.database_manager import ColumnInfo, TableInfo
from src.ontology_generator import OntologyGenerator
from src.r2rml_generator import R2RMLGenerator

RR = Namespace("http://www.w3.org/ns/r2rml#")
VOCAB = "http://example.com/ontology/"
BASE = "http://data.example.com/sales/"


def _col(name: str, data_type: str = "integer", **kw: object) -> ColumnInfo:
    return ColumnInfo(
        name=name,
        data_type=data_type,
        is_nullable=bool(kw.get("nullable", False)),
        is_primary_key=bool(kw.get("pk", False)),
        is_foreign_key=bool(kw.get("fk", False)),
    )


def _schema() -> list[TableInfo]:
    customers = TableInfo(
        name="customers",
        schema="sales",
        columns=[_col("id", pk=True), _col("name", "varchar(100)", nullable=True)],
        primary_keys=["id"],
        foreign_keys=[],
    )
    addresses = TableInfo(
        name="addresses",
        schema="sales",
        columns=[_col("id", pk=True), _col("city", "text")],
        primary_keys=["id"],
        foreign_keys=[],
    )
    orders = TableInfo(
        name="orders",
        schema="sales",
        columns=[
            _col("id", pk=True),
            _col("customer_id", fk=True),
            _col("billing_address_id", fk=True),
            _col("shipping_address_id", fk=True),
        ],
        primary_keys=["id"],
        foreign_keys=[
            {
                "column": "customer_id",
                "referenced_table": "customers",
                "referenced_column": "id",
            },
            {
                "column": "billing_address_id",
                "referenced_table": "addresses",
                "referenced_column": "id",
            },
            {
                "column": "shipping_address_id",
                "referenced_table": "addresses",
                "referenced_column": "id",
            },
        ],
    )
    order_lines = TableInfo(
        name="order_lines",
        schema="sales",
        columns=[_col("order_id", pk=True), _col("line_no", pk=True)],
        primary_keys=["order_id", "line_no"],
        foreign_keys=[],
    )
    shipments = TableInfo(
        name="shipments",
        schema="sales",
        columns=[
            _col("id", pk=True),
            _col("order_id", fk=True),
            _col("line_no", fk=True),
        ],
        primary_keys=["id"],
        foreign_keys=[
            {
                "column": "order_id",
                "referenced_table": "order_lines",
                "referenced_column": "order_id",
            },
            {
                "column": "line_no",
                "referenced_table": "order_lines",
                "referenced_column": "line_no",
            },
        ],
    )
    audit_log = TableInfo(
        name="audit_log",
        schema="sales",
        columns=[_col("event", "text"), _col("note", "text", nullable=True)],
        primary_keys=[],
        foreign_keys=[],
    )
    return [customers, addresses, orders, order_lines, shipments, audit_log]


def _mapping(tables: list[TableInfo] | None = None) -> Graph:
    ttl = R2RMLGenerator(base_iri=BASE, vocabulary_iri=VOCAB).generate_from_schema(
        tables or _schema(), schema_name="sales"
    )
    graph = Graph()
    graph.parse(data=ttl, format="turtle", publicID="http://mapping/")
    return graph


def _triples_map(graph: Graph, table: str) -> URIRef:
    for tm in graph.subjects(RDF.type, RR.TriplesMap):
        logical = graph.value(tm, RR.logicalTable)
        if str(graph.value(logical, RR.tableName)) == f'"sales"."{table}"':
            return tm  # type: ignore[return-value]
    raise AssertionError(f"no TriplesMap for {table}")


def _object_maps(graph: Graph, tm: URIRef, predicate: str) -> list:
    return [
        graph.value(pom, RR.objectMap)
        for pom in graph.objects(tm, RR.predicateObjectMap)
        if graph.value(pom, RR.predicate) == URIRef(VOCAB + predicate)
    ]


def test_mapping_uses_ontology_vocabulary() -> None:
    """Every class and predicate in the mapping is declared by the ontology."""
    tables = _schema()
    ontology = Graph()
    generator = OntologyGenerator(base_uri=VOCAB)
    ontology.parse(
        data=generator.generate_from_schema(
            tables, include_inferred_relationships=False
        ),
        format="turtle",
    )
    mapping = _mapping(tables)

    classes = set(mapping.objects(None, RR["class"]))
    predicates = set(mapping.objects(None, RR.predicate))
    assert classes and predicates
    for cls in classes:
        assert (cls, RDF.type, OWL.Class) in ontology, cls
    for pred in predicates:
        assert (pred, RDF.type, OWL.DatatypeProperty) in ontology or (
            pred,
            RDF.type,
            OWL.ObjectProperty,
        ) in ontology, pred


def test_subjects_are_rows_under_base_iri() -> None:
    graph = _mapping()
    subject = graph.value(_triples_map(graph, "customers"), RR.subjectMap)
    assert str(graph.value(subject, RR.template)) == BASE + 'customers/{"id"}'
    assert graph.value(subject, RR["class"]) == URIRef(VOCAB + "customers")


def test_composite_primary_key_separated_by_slash() -> None:
    graph = _mapping()
    subject = graph.value(_triples_map(graph, "order_lines"), RR.subjectMap)
    assert (
        str(graph.value(subject, RR.template))
        == BASE + 'order_lines/{"order_id"}/{"line_no"}'
    )


def test_table_without_primary_key_maps_to_blank_nodes() -> None:
    """Keyed on its non-null columns, never on a guessed identifier."""
    graph = _mapping()
    subject = graph.value(_triples_map(graph, "audit_log"), RR.subjectMap)
    assert graph.value(subject, RR.termType) == RR.BlankNode
    assert str(graph.value(subject, RR.template)) == '{"event"}'


def test_foreign_key_column_keeps_its_value() -> None:
    graph = _mapping()
    (om,) = _object_maps(graph, _triples_map(graph, "orders"), "orders_customer_id")
    assert str(graph.value(om, RR.column)) == '"customer_id"'


def test_foreign_key_joins_to_parent_triples_map() -> None:
    graph = _mapping()
    (om,) = _object_maps(graph, _triples_map(graph, "orders"), "orders_has_customers")
    assert graph.value(om, RR.parentTriplesMap) == _triples_map(graph, "customers")
    (join,) = graph.objects(om, RR.joinCondition)
    assert str(graph.value(join, RR.child)) == '"customer_id"'
    assert str(graph.value(join, RR.parent)) == '"id"'


def test_composite_foreign_key_is_one_join_with_all_columns() -> None:
    graph = _mapping()
    (om,) = _object_maps(
        graph, _triples_map(graph, "shipments"), "shipments_has_order_lines"
    )
    pairs = {
        (str(graph.value(j, RR.child)), str(graph.value(j, RR.parent)))
        for j in graph.objects(om, RR.joinCondition)
    }
    assert pairs == {('"order_id"', '"order_id"'), ('"line_no"', '"line_no"')}


def test_two_foreign_keys_to_one_table_stay_separate() -> None:
    """Billing and shipping address must not be required to be the same row."""
    graph = _mapping()
    maps = _object_maps(graph, _triples_map(graph, "orders"), "orders_has_addresses")
    children = sorted(
        str(graph.value(j, RR.child))
        for om in maps
        for j in graph.objects(om, RR.joinCondition)
    )
    assert len(maps) == 2
    assert children == ['"billing_address_id"', '"shipping_address_id"']


def test_foreign_key_to_unmapped_table_keeps_only_the_value() -> None:
    orders = next(t for t in _schema() if t.name == "orders")
    graph = _mapping([orders])
    tm = _triples_map(graph, "orders")
    assert not _object_maps(graph, tm, "orders_has_customers")
    assert _object_maps(graph, tm, "orders_customer_id")


def test_identifiers_keep_case_and_escape_quotes() -> None:
    table = TableInfo(
        name="Order Items",
        schema="SALES",
        columns=[_col('Weird"Id', pk=True)],
        primary_keys=['Weird"Id'],
        foreign_keys=[],
    )
    ttl = R2RMLGenerator(base_iri=BASE, vocabulary_iri=VOCAB).generate_from_schema(
        [table]
    )
    graph = Graph()
    graph.parse(data=ttl, format="turtle", publicID="http://mapping/")
    (tm,) = graph.subjects(RDF.type, RR.TriplesMap)
    assert (
        str(graph.value(graph.value(tm, RR.logicalTable), RR.tableName))
        == '"SALES"."Order Items"'
    )
    subject = graph.value(tm, RR.subjectMap)
    assert str(graph.value(subject, RR.template)) == BASE + 'Order_Items/{"Weird""Id"}'
    assert graph.value(subject, RR["class"]) == URIRef(VOCAB + "Order_Items")


def test_empty_schema_gives_empty_mapping() -> None:
    assert R2RMLGenerator().generate_from_schema([]) == ""
