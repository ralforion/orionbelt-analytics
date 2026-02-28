"""Tests for Oxigraph RDF store and auto-persist functionality.

This module tests:
- Oxigraph store initialization
- Auto-persist behavior in generate_ontology()
- Connection-scoped RDF stores
- Schema hash detection for version tracking
- Fallback behavior when Oxigraph not available
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from src.oxigraph_store import OxigraphStoreManager, OXIGRAPH_AVAILABLE
from src.database_manager import TableInfo, ColumnInfo


class TestOxigraphStoreManager:
    """Test OxigraphStoreManager functionality."""

    @pytest.fixture
    def temp_store_dir(self):
        """Create a temporary directory for Oxigraph store."""
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir)

    @pytest.mark.skipif(not OXIGRAPH_AVAILABLE, reason="Oxigraph not available")
    def test_store_initialization(self, temp_store_dir):
        """Test Oxigraph store initialization."""
        store = OxigraphStoreManager(store_path=temp_store_dir)

        assert store is not None
        assert store.store_path == temp_store_dir
        assert (temp_store_dir).exists()

    @pytest.mark.skipif(not OXIGRAPH_AVAILABLE, reason="Oxigraph not available")
    def test_load_ontology(self, temp_store_dir):
        """Test loading ontology into Oxigraph store."""
        store = OxigraphStoreManager(store_path=temp_store_dir)

        # Simple ontology TTL
        ontology_ttl = """
        @prefix ns: <http://example.com/ontology/> .
        @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .

        ns:Person rdf:type owl:Class ;
            rdfs:label "Person" .

        ns:name rdf:type owl:DatatypeProperty ;
            rdfs:domain ns:Person ;
            rdfs:label "name" .
        """

        graph_uri = "http://example.com/schema/test"
        triple_count = store.load_ontology(ontology_ttl, graph_uri, "test")

        assert triple_count > 0
        assert triple_count >= 4  # At least 4 triples in the sample

    @pytest.mark.skipif(not OXIGRAPH_AVAILABLE, reason="Oxigraph not available")
    def test_export_graph(self, temp_store_dir):
        """Test exporting graph from Oxigraph store."""
        store = OxigraphStoreManager(store_path=temp_store_dir)

        # Load sample ontology
        ontology_ttl = """
        @prefix ns: <http://example.com/ontology/> .
        @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .

        ns:Customer rdf:type owl:Class .
        """

        graph_uri = "http://example.com/schema/export_test"
        store.load_ontology(ontology_ttl, graph_uri, "export_test")

        # Export the graph
        exported_ttl = store.export_graph(graph_uri, format="turtle")

        assert exported_ttl is not None
        assert len(exported_ttl) > 0
        assert "Customer" in exported_ttl or "ns:Customer" in exported_ttl

    @pytest.mark.skipif(not OXIGRAPH_AVAILABLE, reason="Oxigraph not available")
    def test_connection_scoped_stores(self, temp_store_dir):
        """Test that different connection_ids create separate stores."""
        # Connection 1
        conn1_path = temp_store_dir / "conn1" / "store"
        store1 = OxigraphStoreManager(store_path=conn1_path)

        ontology1 = """
        @prefix ns: <http://example.com/ontology/> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .

        ns:Table1 owl:Class .
        """
        store1.load_ontology(ontology1, "http://example.com/schema/db1", "db1")

        # Connection 2
        conn2_path = temp_store_dir / "conn2" / "store"
        store2 = OxigraphStoreManager(store_path=conn2_path)

        ontology2 = """
        @prefix ns: <http://example.com/ontology/> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .

        ns:Table2 owl:Class .
        """
        store2.load_ontology(ontology2, "http://example.com/schema/db2", "db2")

        # Verify isolation
        export1 = store1.export_graph("http://example.com/schema/db1")
        export2 = store2.export_graph("http://example.com/schema/db2")

        assert "Table1" in export1 or "ns:Table1" in export1
        assert "Table2" not in export1
        assert "Table2" in export2 or "ns:Table2" in export2
        assert "Table1" not in export2


class TestAutoPersistBehavior:
    """Test auto-persist functionality in generate_ontology()."""

    @pytest.fixture
    def mock_context(self):
        """Create a mock FastMCP Context."""
        ctx = Mock()
        ctx.info = Mock()
        return ctx

    @pytest.fixture
    def sample_tables(self):
        """Create sample table data."""
        return [
            TableInfo(
                name="users",
                schema="public",
                columns=[
                    ColumnInfo(
                        name="id",
                        data_type="integer",
                        is_nullable=False,
                        is_primary_key=True,
                        is_foreign_key=False,
                    ),
                    ColumnInfo(
                        name="name",
                        data_type="varchar",
                        is_nullable=False,
                        is_primary_key=False,
                        is_foreign_key=False,
                    ),
                ],
                primary_keys=["id"],
                foreign_keys=[],
            )
        ]

    @pytest.mark.skipif(not OXIGRAPH_AVAILABLE, reason="Oxigraph not available")
    @patch('src.main.get_oxigraph_store')
    @patch('src.main.get_session_data')
    async def test_auto_persist_enabled_returns_summary(
        self, mock_session, mock_get_store, mock_context, sample_tables
    ):
        """Test that auto_persist=True returns a summary instead of full TTL."""
        from src.main import generate_ontology

        # Mock session
        mock_session_data = Mock()
        mock_session_data.ontology_file = None
        mock_session_data.get_last_analyzed_schema.return_value = None
        mock_session_data.tables_info = sample_tables
        mock_session.return_value = mock_session_data

        # Mock Oxigraph store
        mock_store = Mock()
        mock_store.load_ontology.return_value = 10  # 10 triples stored
        mock_get_store.return_value = mock_store

        # Call with auto_persist=True (default)
        result = await generate_ontology(
            mock_context,
            schema_name="public",
            auto_persist=True
        )

        # Should return summary, not full TTL
        assert isinstance(result, str)
        assert "✅" in result or "Ontology generated" in result
        assert "Triples stored:" in result
        assert "10" in result

        # Should NOT contain full ontology TTL
        assert "@prefix" not in result
        assert "owl:Class" not in result

    @patch('src.main.OXIGRAPH_AVAILABLE', False)
    @patch('src.main.get_session_data')
    async def test_auto_persist_fallback_when_oxigraph_unavailable(
        self, mock_session, mock_context, sample_tables
    ):
        """Test fallback to full TTL when Oxigraph is not available."""
        from src.main import generate_ontology

        # Mock session
        mock_session_data = Mock()
        mock_session_data.ontology_file = None
        mock_session_data.get_last_analyzed_schema.return_value = None
        mock_session_data.tables_info = sample_tables
        mock_session.return_value = mock_session_data

        # Call with auto_persist=True (should fallback)
        result = await generate_ontology(
            mock_context,
            schema_name="public",
            auto_persist=True
        )

        # Should fallback to full TTL
        assert isinstance(result, str)
        assert "@prefix" in result or "# Ontology file:" in result

    @patch('src.main.get_session_data')
    async def test_auto_persist_false_returns_full_ttl(
        self, mock_session, mock_context, sample_tables
    ):
        """Test that auto_persist=False returns full TTL (legacy behavior)."""
        from src.main import generate_ontology

        # Mock session
        mock_session_data = Mock()
        mock_session_data.ontology_file = None
        mock_session_data.get_last_analyzed_schema.return_value = None
        mock_session_data.tables_info = sample_tables
        mock_session.return_value = mock_session_data

        # Call with auto_persist=False
        result = await generate_ontology(
            mock_context,
            schema_name="public",
            auto_persist=False
        )

        # Should return full TTL
        assert isinstance(result, str)
        assert "@prefix" in result
        # Should contain ontology content
        assert "users" in result.lower() or "User" in result


class TestSchemaHashDetection:
    """Test schema hash calculation for version detection."""

    def test_schema_hash_same_for_identical_schemas(self):
        """Test that identical schemas produce the same hash."""
        from src.main import _calculate_schema_hash

        tables1 = [
            TableInfo(
                name="users",
                schema="public",
                columns=[
                    ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True, is_foreign_key=False),
                    ColumnInfo(name="name", data_type="varchar", is_nullable=False, is_primary_key=False, is_foreign_key=False),
                ],
                primary_keys=["id"],
                foreign_keys=[],
            )
        ]

        tables2 = [
            TableInfo(
                name="users",
                schema="public",
                columns=[
                    ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True, is_foreign_key=False),
                    ColumnInfo(name="name", data_type="varchar", is_nullable=False, is_primary_key=False, is_foreign_key=False),
                ],
                primary_keys=["id"],
                foreign_keys=[],
            )
        ]

        hash1 = _calculate_schema_hash(tables1)
        hash2 = _calculate_schema_hash(tables2)

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex digest

    def test_schema_hash_different_for_changed_schemas(self):
        """Test that schema changes produce different hashes."""
        from src.main import _calculate_schema_hash

        tables1 = [
            TableInfo(
                name="users",
                schema="public",
                columns=[
                    ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True, is_foreign_key=False),
                ],
                primary_keys=["id"],
                foreign_keys=[],
            )
        ]

        # Added a column
        tables2 = [
            TableInfo(
                name="users",
                schema="public",
                columns=[
                    ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True, is_foreign_key=False),
                    ColumnInfo(name="email", data_type="varchar", is_nullable=True, is_primary_key=False, is_foreign_key=False),
                ],
                primary_keys=["id"],
                foreign_keys=[],
            )
        ]

        hash1 = _calculate_schema_hash(tables1)
        hash2 = _calculate_schema_hash(tables2)

        assert hash1 != hash2

    def test_schema_hash_ignores_row_count(self):
        """Test that row count changes don't affect schema hash."""
        from src.main import _calculate_schema_hash

        tables1 = [
            TableInfo(
                name="users",
                schema="public",
                columns=[
                    ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True, is_foreign_key=False),
                ],
                primary_keys=["id"],
                foreign_keys=[],
                row_count=100,
            )
        ]

        tables2 = [
            TableInfo(
                name="users",
                schema="public",
                columns=[
                    ColumnInfo(name="id", data_type="integer", is_nullable=False, is_primary_key=True, is_foreign_key=False),
                ],
                primary_keys=["id"],
                foreign_keys=[],
                row_count=500,  # Different row count
            )
        ]

        hash1 = _calculate_schema_hash(tables1)
        hash2 = _calculate_schema_hash(tables2)

        # Hashes should be the same (row count excluded)
        assert hash1 == hash2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
