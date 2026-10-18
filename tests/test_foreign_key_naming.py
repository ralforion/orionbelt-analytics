"""Foreign keys as relationships: grouped and named alike in OWL and R2RML."""

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from src.constants import OBA_NAMESPACE
from src.database_manager import ColumnInfo, TableInfo
from src.obqc_validator import OBQCValidator
from src.ontology_generator import OntologyGenerator
from src.ontology_naming import foreign_keys

VOCAB = "http://example.com/ontology/"
ONT = Namespace(VOCAB)
OBA = Namespace(OBA_NAMESPACE)


def _fk(
    column: str,
    table: str,
    ref_column: str | None = "id",
    constraint: str | None = None,
) -> dict:
    """An FK entry as drivers report it, one per constraint unless shared."""
    return {
        "column": column,
        "referenced_table": table,
        "referenced_column": ref_column,
        "constraint_name": constraint or f"{column}_fk",
    }


def _table(name: str, columns: list[str], pks: list[str], fks: list[dict]) -> TableInfo:
    fk_columns = {fk["column"] for fk in fks}
    return TableInfo(
        name=name,
        schema="sales",
        columns=[
            ColumnInfo(
                name=c,
                data_type="integer",
                is_nullable=False,
                is_primary_key=c in pks,
                is_foreign_key=c in fk_columns,
            )
            for c in columns
        ],
        primary_keys=pks,
        foreign_keys=fks,
    )


def _schema() -> list[TableInfo]:
    return [
        _table("countries", ["id"], ["id"], []),
        _table(
            "addresses", ["id", "country_id"], ["id"], [_fk("country_id", "countries")]
        ),
        _table("customers", ["id"], ["id"], []),
        _table(
            "orders",
            ["id", "customer_id", "billing_address_id", "shipping_address_id"],
            ["id"],
            [
                _fk("customer_id", "customers"),
                _fk("billing_address_id", "addresses"),
                _fk("shipping_address_id", "addresses"),
            ],
        ),
    ]


def _ontology(generator: OntologyGenerator | None = None) -> Graph:
    generator = generator or OntologyGenerator(base_uri=VOCAB)
    generator.generate_from_schema(_schema(), include_inferred_relationships=False)
    return generator.graph


class TestForeignKeys:
    def test_single_key_keeps_the_plain_name(self) -> None:
        (key,) = foreign_keys("orders", [_fk("customer_id", "customers")])
        assert key.local_name == "orders_has_customers"
        assert key.inverse_local_name == "customers_referenced_by_orders"
        assert key.via_column is None

    def test_two_keys_to_one_table_are_named_by_column(self) -> None:
        keys = foreign_keys(
            "orders",
            [
                _fk("billing_address_id", "addresses"),
                _fk("shipping_address_id", "addresses"),
            ],
        )
        assert [k.local_name for k in keys] == [
            "orders_has_addresses_via_billing_address_id",
            "orders_has_addresses_via_shipping_address_id",
        ]

    def test_composite_key_is_one_key(self) -> None:
        (key,) = foreign_keys(
            "shipments",
            [
                _fk("order_id", "order_lines", "order_id", "line_fk"),
                _fk("line_no", "order_lines", "line_no", "line_fk"),
            ],
        )
        assert key.local_name == "shipments_has_order_lines"
        assert key.column_pairs == [("order_id", "order_id"), ("line_no", "line_no")]

    def test_keys_to_different_columns_of_one_table_stay_separate(self) -> None:
        """Two keys onto users.id and users.email are not one composite key."""
        keys = foreign_keys(
            "audits",
            [_fk("actor_id", "users", "id"), _fk("approver_email", "users", "email")],
        )
        assert [k.column_pairs for k in keys] == [
            [("actor_id", "id")],
            [("approver_email", "email")],
        ]

    def test_entries_without_constraint_are_separate_keys(self) -> None:
        """A schema saved before constraints were recorded: never guess."""
        entries = [
            _fk("order_id", "order_lines", "order_id"),
            _fk("line_no", "order_lines", "line_no"),
        ]
        for entry in entries:
            del entry["constraint_name"]
        assert len(foreign_keys("shipments", entries)) == 2

    def test_same_constraint_name_on_other_tables_is_not_merged(self) -> None:
        keys = foreign_keys(
            "orders",
            [
                _fk("a_id", "addresses", "id", "fk1"),
                _fk("c_id", "customers", "id", "fk1"),
            ],
        )
        assert len(keys) == 2

    def test_entries_without_target_are_skipped(self) -> None:
        assert foreign_keys("orders", [{"column": "x", "referenced_table": ""}]) == []


class TestOntology:
    def test_each_key_is_its_own_property_with_one_column(self) -> None:
        graph = _ontology()
        for column in ("billing_address_id", "shipping_address_id"):
            prop = ONT[f"orders_has_addresses_via_{column}"]
            assert (prop, RDF.type, OWL.ObjectProperty) in graph
            assert [str(c) for c in graph.objects(prop, OBA.foreignKeyColumn)] == [
                column
            ]
            assert str(graph.value(prop, RDFS.label)) == (
                f"orders has addresses via {column}"
            )
            inverse = ONT[f"addresses_referenced_by_orders_via_{column}"]
            assert (prop, OWL.inverseOf, inverse) in graph
        assert (ONT.orders_has_addresses, RDF.type, OWL.ObjectProperty) not in graph

    def test_obqc_sees_both_keys(self) -> None:
        validator = OBQCValidator()
        validator.load_ontology(_ontology(), VOCAB)
        joins = {
            (r.from_column, r.to_table)
            for r in validator._schema_cache.relationships.values()
        }
        assert ("billing_address_id", "addresses") in joins
        assert ("shipping_address_id", "addresses") in joins

    def test_description_lands_on_the_named_key(self) -> None:
        generator = OntologyGenerator(base_uri=VOCAB)
        graph = _ontology(generator)
        generator.apply_semantic_descriptions(
            {
                "relationships": {
                    "orders.shipping_address_id -> addresses.id": {
                        "description": "Where the order is shipped"
                    }
                }
            }
        )
        described = set(graph.subjects(OBA.relationshipDescription, None))
        assert described == {ONT.orders_has_addresses_via_shipping_address_id}

    def test_ambiguous_enrichment_is_not_applied(self) -> None:
        generator = OntologyGenerator(base_uri=VOCAB)
        graph = _ontology(generator)
        generator.apply_enrichment(
            {
                "relationships": [
                    {
                        "from_table": "orders",
                        "to_table": "addresses",
                        "suggested_name": "has address",
                    },
                    {
                        "from_table": "orders",
                        "to_table": "customers",
                        "suggested_name": "placed by",
                    },
                ]
            }
        )
        labels = {str(o) for o in graph.objects(None, RDFS.label)}
        assert "placed by" in labels
        assert "has address" not in labels

    def test_no_property_chain_through_an_ambiguous_hop(self) -> None:
        graph = _ontology()
        chains = {
            str(s).removeprefix(VOCAB)
            for s in graph.subjects(OWL.propertyChainAxiom, None)
        }
        assert "orders_via_addresses_has_countries" not in chains


def test_r2rml_and_ontology_agree_on_keys() -> None:
    from src.r2rml_generator import R2RMLGenerator

    rr = Namespace("http://www.w3.org/ns/r2rml#")
    mapping = Graph()
    mapping.parse(
        data=R2RMLGenerator(vocabulary_iri=VOCAB).generate_from_schema(_schema()),
        format="turtle",
        publicID="http://mapping/",
    )
    ontology = _ontology()
    predicates = set(mapping.objects(None, rr.predicate))
    object_properties = {
        p
        for p in predicates
        if isinstance(p, URIRef) and (p, RDF.type, OWL.ObjectProperty) in ontology
    }
    assert {str(p).removeprefix(VOCAB) for p in object_properties} == {
        "orders_has_customers",
        "orders_has_addresses_via_billing_address_id",
        "orders_has_addresses_via_shipping_address_id",
        "addresses_has_countries",
    }


def test_reflection_records_the_constraint_of_each_key_column() -> None:
    from src.drivers.reflection import build_table_info

    columns = [
        {"name": c, "type": "INTEGER", "nullable": False}
        for c in ("id", "order_id", "line_no", "actor_id")
    ]
    fks = [
        {
            "name": "line_fk",
            "constrained_columns": ["order_id", "line_no"],
            "referred_table": "order_lines",
            "referred_columns": ["order_id", "line_no"],
        },
        {
            "name": None,
            "constrained_columns": ["actor_id"],
            "referred_table": "users",
            "referred_columns": ["id"],
        },
    ]
    table = build_table_info(
        "shipments", "public", columns, {"constrained_columns": ["id"]}, fks
    )
    keys = foreign_keys(table.name, table.foreign_keys)
    assert [k.column_pairs for k in keys] == [
        [("order_id", "order_id"), ("line_no", "line_no")],
        [("actor_id", "id")],
    ]
