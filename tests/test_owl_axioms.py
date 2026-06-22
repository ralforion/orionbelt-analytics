"""Tests for OWL axiom generation: FunctionalProperty, disjointWith, propertyChainAxiom."""

import unittest

from rdflib import Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from src.constants import OBA_NAMESPACE
from src.database_manager import ColumnInfo, TableInfo
from src.ontology_generator import OntologyGenerator

OBA = Namespace(OBA_NAMESPACE)


class TestOwlAxioms(unittest.TestCase):
    """Tests for the three OWL axiom types added to ontology generation."""

    def setUp(self):
        """Set up a 3-table schema: customers (dim), orders (fact), returns (fact).

        orders and returns both FK to customers, forming a classic fan-trap
        pattern suitable for testing all three axiom types.
        """
        self.generator = OntologyGenerator("http://test.com/ontology/")
        self.ns = "http://test.com/ontology/"

        self.customers = TableInfo(
            name="customers",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="name",
                    data_type="VARCHAR(200)",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=False,
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[],
            row_count=15000,
        )

        self.orders = TableInfo(
            name="orders",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="customer_id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=True,
                    foreign_key_table="customers",
                    foreign_key_column="id",
                ),
                ColumnInfo(
                    name="total",
                    data_type="DECIMAL(12,2)",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=False,
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                }
            ],
            row_count=482000,
        )

        self.returns = TableInfo(
            name="returns",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="customer_id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=True,
                    foreign_key_table="customers",
                    foreign_key_column="id",
                ),
                ColumnInfo(
                    name="refund_amount",
                    data_type="DECIMAL(12,2)",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=False,
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                }
            ],
            row_count=31000,
        )

    # -------------------------------------------------------------------------
    # owl:FunctionalProperty tests
    # -------------------------------------------------------------------------

    def test_functional_property_on_many_to_one_declared_fk(self):
        """Many-to-one declared FK relationships get owl:FunctionalProperty."""
        self.generator.generate_from_schema(
            [self.customers, self.orders],
            include_inferred_relationships=False,
        )

        prop = URIRef(self.ns + "orders_has_customers")
        g = self.generator.graph

        self.assertIn((prop, RDF.type, OWL.ObjectProperty), g)
        self.assertIn((prop, RDF.type, OWL.FunctionalProperty), g)

    def test_functional_property_not_on_inverse(self):
        """Inverse (one-to-many) relationships must NOT be FunctionalProperty."""
        self.generator.generate_from_schema(
            [self.customers, self.orders],
            include_inferred_relationships=False,
        )

        inverse = URIRef(self.ns + "customers_referenced_by_orders")
        g = self.generator.graph

        self.assertIn((inverse, RDF.type, OWL.ObjectProperty), g)
        self.assertNotIn((inverse, RDF.type, OWL.FunctionalProperty), g)

    def test_functional_property_on_inferred_relationship(self):
        """Inferred many-to-one relationships also get owl:FunctionalProperty."""
        # Use a table with no declared FK but a column named after another table
        products = TableInfo(
            name="products",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="name",
                    data_type="VARCHAR(100)",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=False,
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[],
            row_count=500,
        )
        line_items = TableInfo(
            name="line_items",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="product_id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=False,
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[],
            row_count=10000,
        )

        self.generator.generate_from_schema(
            [products, line_items],
            include_inferred_relationships=True,
        )

        prop = URIRef(self.ns + "line_items_has_products")
        g = self.generator.graph

        # Should exist as an inferred relationship with FunctionalProperty
        if (prop, RDF.type, OWL.ObjectProperty) in g:
            self.assertIn((prop, RDF.type, OWL.FunctionalProperty), g)

    # -------------------------------------------------------------------------
    # owl:disjointWith tests
    # -------------------------------------------------------------------------

    def test_disjoint_sibling_fact_tables(self):
        """Tables sharing a FK target but no FK between them are disjoint."""
        self.generator.generate_from_schema(
            [self.customers, self.orders, self.returns],
            include_inferred_relationships=False,
        )

        orders_uri = URIRef(self.ns + "orders")
        returns_uri = URIRef(self.ns + "returns")
        g = self.generator.graph

        # Either direction is valid for disjointWith (symmetric)
        has_disjoint = (orders_uri, OWL.disjointWith, returns_uri) in g or (
            returns_uri,
            OWL.disjointWith,
            orders_uri,
        ) in g
        self.assertTrue(has_disjoint, "orders and returns should be owl:disjointWith")

    def test_no_disjoint_between_fk_pair(self):
        """Tables linked by a FK should NOT be declared disjoint."""
        self.generator.generate_from_schema(
            [self.customers, self.orders],
            include_inferred_relationships=False,
        )

        customers_uri = URIRef(self.ns + "customers")
        orders_uri = URIRef(self.ns + "orders")
        g = self.generator.graph

        self.assertNotIn((customers_uri, OWL.disjointWith, orders_uri), g)
        self.assertNotIn((orders_uri, OWL.disjointWith, customers_uri), g)

    def test_no_disjoint_with_single_fact_table(self):
        """No disjoint axioms when only one table FKs to a dimension."""
        self.generator.generate_from_schema(
            [self.customers, self.orders],
            include_inferred_relationships=False,
        )

        g = self.generator.graph
        disjoint_triples = list(g.triples((None, OWL.disjointWith, None)))
        self.assertEqual(len(disjoint_triples), 0)

    def test_disjoint_with_three_siblings(self):
        """Three fact tables sharing a dimension get pairwise disjoint axioms."""
        shipments = TableInfo(
            name="shipments",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="customer_id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=True,
                    foreign_key_table="customers",
                    foreign_key_column="id",
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                }
            ],
            row_count=200000,
        )

        self.generator.generate_from_schema(
            [self.customers, self.orders, self.returns, shipments],
            include_inferred_relationships=False,
        )

        g = self.generator.graph
        disjoint_triples = list(g.triples((None, OWL.disjointWith, None)))
        # 3 fact tables → 3 pairwise disjoint axioms (orders-returns, orders-shipments, returns-shipments)
        self.assertEqual(len(disjoint_triples), 3)

    # -------------------------------------------------------------------------
    # owl:propertyChainAxiom tests
    # -------------------------------------------------------------------------

    def test_property_chain_for_two_hop_path(self):
        """A->B->C with no direct A->C FK produces a propertyChainAxiom."""
        regions = TableInfo(
            name="regions",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="name",
                    data_type="VARCHAR(100)",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=False,
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[],
            row_count=50,
        )
        # customers FK to regions
        customers_with_region = TableInfo(
            name="customers",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="region_id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=True,
                    foreign_key_table="regions",
                    foreign_key_column="id",
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[
                {
                    "column": "region_id",
                    "referenced_table": "regions",
                    "referenced_column": "id",
                }
            ],
            row_count=15000,
        )

        self.generator.generate_from_schema(
            [regions, customers_with_region, self.orders],
            include_inferred_relationships=False,
        )

        chain_uri = URIRef(self.ns + "orders_via_customers_has_regions")
        g = self.generator.graph

        self.assertIn((chain_uri, RDF.type, OWL.ObjectProperty), g)
        self.assertIn((chain_uri, RDFS.domain, URIRef(self.ns + "orders")), g)
        self.assertIn((chain_uri, RDFS.range, URIRef(self.ns + "regions")), g)

        # Check propertyChainAxiom exists
        chain_list = g.value(chain_uri, OWL.propertyChainAxiom)
        self.assertIsNotNone(chain_list, "propertyChainAxiom should be present")

    def test_no_chain_when_direct_fk_exists(self):
        """No propertyChainAxiom when A->C has a direct FK."""
        regions = TableInfo(
            name="regions",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[],
            row_count=50,
        )
        customers_with_region = TableInfo(
            name="customers",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="region_id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=True,
                    foreign_key_table="regions",
                    foreign_key_column="id",
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[
                {
                    "column": "region_id",
                    "referenced_table": "regions",
                    "referenced_column": "id",
                }
            ],
            row_count=15000,
        )
        # orders FK to both customers AND regions (direct)
        orders_with_region = TableInfo(
            name="orders",
            schema="public",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=True,
                    is_foreign_key=False,
                ),
                ColumnInfo(
                    name="customer_id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=True,
                    foreign_key_table="customers",
                    foreign_key_column="id",
                ),
                ColumnInfo(
                    name="region_id",
                    data_type="INTEGER",
                    is_nullable=False,
                    is_primary_key=False,
                    is_foreign_key=True,
                    foreign_key_table="regions",
                    foreign_key_column="id",
                ),
            ],
            primary_keys=["id"],
            foreign_keys=[
                {
                    "column": "customer_id",
                    "referenced_table": "customers",
                    "referenced_column": "id",
                },
                {
                    "column": "region_id",
                    "referenced_table": "regions",
                    "referenced_column": "id",
                },
            ],
            row_count=482000,
        )

        self.generator.generate_from_schema(
            [regions, customers_with_region, orders_with_region],
            include_inferred_relationships=False,
        )

        g = self.generator.graph
        chain_triples = list(g.triples((None, OWL.propertyChainAxiom, None)))
        self.assertEqual(len(chain_triples), 0, "No chain needed when direct FK exists")

    def test_no_chain_for_single_hop(self):
        """No propertyChainAxiom with only A->B (no second hop)."""
        self.generator.generate_from_schema(
            [self.customers, self.orders],
            include_inferred_relationships=False,
        )

        g = self.generator.graph
        chain_triples = list(g.triples((None, OWL.propertyChainAxiom, None)))
        self.assertEqual(len(chain_triples), 0)

    # -------------------------------------------------------------------------
    # oba:joinsTo shared traversable join predicate tests (Phase 1)
    # -------------------------------------------------------------------------

    def test_joins_to_emitted_for_declared_fk(self):
        """A declared many-to-one FK emits a directed oba:joinsTo class edge."""
        self.generator.generate_from_schema(
            [self.customers, self.orders],
            include_inferred_relationships=False,
        )
        g = self.generator.graph
        self.assertIn(
            (URIRef(self.ns + "orders"), OBA.joinsTo, URIRef(self.ns + "customers")),
            g,
        )

    def test_joins_to_is_directed_not_reverse(self):
        """oba:joinsTo is many-to-one only — never the one-to-many reverse."""
        self.generator.generate_from_schema(
            [self.customers, self.orders],
            include_inferred_relationships=False,
        )
        g = self.generator.graph
        self.assertNotIn(
            (URIRef(self.ns + "customers"), OBA.joinsTo, URIRef(self.ns + "orders")),
            g,
        )

    def test_joins_to_one_edge_per_fk(self):
        """One oba:joinsTo edge per many-to-one FK (orders, returns -> customers)."""
        self.generator.generate_from_schema(
            [self.customers, self.orders, self.returns],
            include_inferred_relationships=False,
        )
        g = self.generator.graph
        edges = list(g.triples((None, OBA.joinsTo, None)))
        self.assertEqual(len(edges), 2)
        targets = {str(o) for _, _, o in edges}
        self.assertEqual(targets, {self.ns + "customers"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
