"""Regression tests for the AUTO_ONTOLOGY background generation path.

Guards against a regression where the background task called a non-existent
``OntologyGenerator.generate_ontology(dict)`` instead of the real
``generate_from_schema(List[TableInfo])`` API. The bug was masked because the
task swallows exceptions, so it silently produced no ontology.
"""

import types
from pathlib import Path

from src.database_manager import ColumnInfo, TableInfo
from src.handlers import graphrag as graphrag_handler


def _sample_tables():
    """Two related tables (customers 1:N orders) for ontology generation."""
    customers = TableInfo(
        name="customers",
        schema="public",
        columns=[
            ColumnInfo(
                name="id",
                data_type="INTEGER",
                is_nullable=False,
                is_primary_key=True,
                is_foreign_key=False,
                comment="Customer ID",
            ),
            ColumnInfo(
                name="name",
                data_type="VARCHAR(100)",
                is_nullable=False,
                is_primary_key=False,
                is_foreign_key=False,
                comment="Customer name",
            ),
        ],
        primary_keys=["id"],
        foreign_keys=[],
        comment="Customers table",
        row_count=10,
    )
    orders = TableInfo(
        name="orders",
        schema="public",
        columns=[
            ColumnInfo(
                name="id",
                data_type="INTEGER",
                is_nullable=False,
                is_primary_key=True,
                is_foreign_key=False,
                comment="Order ID",
            ),
            ColumnInfo(
                name="customer_id",
                data_type="INTEGER",
                is_nullable=False,
                is_primary_key=False,
                is_foreign_key=True,
                foreign_key_table="customers",
                foreign_key_column="id",
                comment="Owning customer",
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
        comment="Orders table",
        row_count=50,
    )
    return [customers, orders]


class _FakeSchemaState:
    def __init__(self):
        self.ontology = types.SimpleNamespace(ontology_file=None)


class _FakeSession:
    """Minimal session surface used by _auto_generate_ontology_background."""

    def __init__(self):
        self.connection_id = None  # forces ensure_output_dir() (no real connection)
        self.oxigraph_store = None  # skip the RDF storage branch
        self._schema_state = _FakeSchemaState()

    def get_or_create_schema_state(self, schema_name):
        return self._schema_state


async def test_auto_ontology_background_generates_file():
    """The background task must produce a TTL ontology via generate_from_schema."""
    session = _FakeSession()
    schema_name = "public"

    await graphrag_handler._auto_generate_ontology_background(
        schema_name=schema_name,
        tables_info=_sample_tables(),
        session=session,
        ctx=None,
    )

    # Regression assertion: the task recorded an ontology file (it did NOT
    # swallow an AttributeError on a missing generator method).
    recorded = session._schema_state.ontology.ontology_file
    assert recorded is not None, "background task produced no ontology file"
    assert recorded.startswith(f"ontology_{schema_name}_")
    assert recorded.endswith(".ttl")

    # The file exists and contains a real ontology with both classes.
    from src.paths import ensure_output_dir

    ttl_path = Path(ensure_output_dir()) / recorded
    try:
        assert ttl_path.is_file()
        content = ttl_path.read_text(encoding="utf-8")
        assert "owl:Ontology" in content or "owl:Class" in content
        assert "Customers" in content and "Orders" in content
    finally:
        if ttl_path.exists():
            ttl_path.unlink()
