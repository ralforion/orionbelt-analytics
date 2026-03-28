"""Regression tests for code review findings (2026-03-28).

Covers:
1. _clear_session_state clears OBQC validator on connection change
2. _clear_session_state clears Oxigraph store on connection change
3. _reconnect() supports BigQuery, DuckDB, Databricks, MySQL
4. Dremio PAT auth in the main connect_database handler
"""

import unittest
from unittest.mock import Mock, patch, AsyncMock, MagicMock
import os

from src.session import SessionData
from src.database_manager import DatabaseManager


class TestClearSessionStateResetsValidator(unittest.TestCase):
    """Issue 1: OBQC validator must be cleared on connection change."""

    def test_clear_session_state_resets_obqc_validator(self):
        """After _clear_session_state, obqc_validator should be None."""
        from src.main import _clear_session_state

        session = SessionData()
        session.obqc_validator = Mock()  # simulate a live validator

        _clear_session_state(session, reason="test")

        self.assertIsNone(session.obqc_validator)

    def test_clear_session_state_resets_oxigraph_store(self):
        """After _clear_session_state, oxigraph_store and flag should be cleared."""
        from src.main import _clear_session_state

        session = SessionData()
        session.oxigraph_store = Mock()
        session.oxigraph_initialized = True

        _clear_session_state(session, reason="test")

        self.assertIsNone(session.oxigraph_store)
        self.assertFalse(session.oxigraph_initialized)

    def test_clear_session_state_still_clears_original_fields(self):
        """Existing fields (schema, ontology, graphrag) must still be cleared."""
        from src.main import _clear_session_state

        session = SessionData()
        session.schema_file = "schema.json"
        session.ontology_file = "ontology.ttl"
        session.r2rml_file = "r2rml.ttl"
        session.loaded_ontology = "<ttl>"
        session.loaded_ontology_path = "/tmp/onto.ttl"
        session.graphrag_manager = Mock()
        session.graphrag_initialized = True

        _clear_session_state(session, reason="test")

        self.assertIsNone(session.schema_file)
        self.assertIsNone(session.ontology_file)
        self.assertIsNone(session.r2rml_file)
        self.assertIsNone(session.loaded_ontology)
        self.assertIsNone(session.loaded_ontology_path)
        self.assertIsNone(session.graphrag_manager)
        self.assertFalse(session.graphrag_initialized)


class TestReconnectAllBackends(unittest.TestCase):
    """Issue 3: _reconnect() must cover all 8 supported database types."""

    def setUp(self):
        self.db_manager = DatabaseManager()

    def test_reconnect_bigquery(self):
        self.db_manager._last_connection_params = {
            "type": "bigquery",
            "project_id": "my-project",
            "dataset": "my_dataset",
            "credentials_path": "/path/to/creds.json",
            "credentials_json": None,
        }
        with patch.object(self.db_manager, "connect_bigquery", return_value=True) as m:
            self.db_manager._reconnect()
            m.assert_called_once_with(
                "my-project", "my_dataset", "/path/to/creds.json", None,
            )

    def test_reconnect_duckdb(self):
        self.db_manager._last_connection_params = {
            "type": "duckdb",
            "database_path": "/tmp/test.duckdb",
            "motherduck_token": None,
            "read_only": False,
        }
        with patch.object(self.db_manager, "connect_duckdb", return_value=True) as m:
            self.db_manager._reconnect()
            m.assert_called_once_with("/tmp/test.duckdb", None, False)

    def test_reconnect_databricks(self):
        self.db_manager._last_connection_params = {
            "type": "databricks",
            "server_hostname": "host.databricks.com",
            "http_path": "/sql/1.0/warehouses/abc",
            "access_token": "dapi123",
            "catalog": "main",
            "schema": "default",
        }
        with patch.object(self.db_manager, "connect_databricks", return_value=True) as m:
            self.db_manager._reconnect()
            m.assert_called_once_with(
                "host.databricks.com", "/sql/1.0/warehouses/abc",
                "dapi123", "main", "default",
            )

    def test_reconnect_mysql(self):
        self.db_manager._last_connection_params = {
            "type": "mysql",
            "host": "localhost",
            "port": 3306,
            "database": "testdb",
            "username": "root",
            "password": "secret",
            "charset": "utf8mb4",
        }
        with patch.object(self.db_manager, "connect_mysql", return_value=True) as m:
            self.db_manager._reconnect()
            m.assert_called_once_with(
                "localhost", 3306, "testdb", "root", "secret", "utf8mb4",
            )

    def test_reconnect_unsupported_type_raises(self):
        self.db_manager._last_connection_params = {"type": "oracle"}
        with self.assertRaises(RuntimeError) as ctx:
            self.db_manager._reconnect()
        self.assertIn("Unsupported database type", str(ctx.exception))

    def test_reconnect_failure_raises(self):
        self.db_manager._last_connection_params = {
            "type": "mysql",
            "host": "localhost",
            "port": 3306,
            "database": "testdb",
            "username": "root",
            "password": "secret",
            "charset": "utf8mb4",
        }
        with patch.object(self.db_manager, "connect_mysql", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                self.db_manager._reconnect()
            self.assertIn("Failed to reconnect", str(ctx.exception))


class TestDremioPATHandler(unittest.IsolatedAsyncioTestCase):
    """Issue 4: connect_database handler should prefer DREMIO_URI + DREMIO_PAT."""

    async def test_dremio_pat_auth_preferred(self):
        """When DREMIO_URI and DREMIO_PAT are set, PAT-based auth is used."""
        from src.handlers.connection import connect_database

        mock_ctx = Mock()
        mock_ctx.info = AsyncMock()
        mock_ctx.warning = AsyncMock()

        mock_db_manager = Mock()
        mock_db_manager.connect_dremio = Mock(return_value=True)
        mock_db_manager._connection_id = "test123"

        mock_session = SessionData()
        mock_get_db_manager = Mock(return_value=mock_db_manager)
        mock_get_session_data = Mock(return_value=mock_session)
        mock_create_error = Mock()
        mock_fingerprint = Mock(return_value="fp1")
        mock_clear = Mock()

        env = {
            "DREMIO_URI": "https://dremio.example.com",
            "DREMIO_PAT": "my-secret-pat",
        }
        with patch.dict(os.environ, env, clear=False):
            result = await connect_database(
                ctx=mock_ctx,
                db_type="dremio",
                get_session_db_manager=mock_get_db_manager,
                get_session_data=mock_get_session_data,
                create_error_response=mock_create_error,
                _get_connection_fingerprint=mock_fingerprint,
                _clear_session_state=mock_clear,
            )

        mock_db_manager.connect_dremio.assert_called_once_with(
            uri="https://dremio.example.com", pat="my-secret-pat",
        )

    async def test_dremio_falls_back_to_legacy(self):
        """When DREMIO_URI/PAT are absent, legacy host/user/password auth is used."""
        from src.handlers.connection import connect_database

        mock_ctx = Mock()
        mock_ctx.info = AsyncMock()
        mock_ctx.warning = AsyncMock()

        mock_db_manager = Mock()
        mock_db_manager.connect_dremio = Mock(return_value=True)
        mock_db_manager._connection_id = "test456"

        mock_session = SessionData()
        mock_get_db_manager = Mock(return_value=mock_db_manager)
        mock_get_session_data = Mock(return_value=mock_session)
        mock_create_error = Mock()
        mock_fingerprint = Mock(return_value="fp2")
        mock_clear = Mock()

        env = {
            "DREMIO_HOST": "dremio-host",
            "DREMIO_PORT": "9047",
            "DREMIO_USERNAME": "admin",
            "DREMIO_PASSWORD": "pass123",
        }
        # Ensure PAT vars are absent
        cleaned = {k: v for k, v in os.environ.items()
                   if k not in ("DREMIO_URI", "DREMIO_PAT")}
        cleaned.update(env)
        with patch.dict(os.environ, cleaned, clear=True):
            result = await connect_database(
                ctx=mock_ctx,
                db_type="dremio",
                get_session_db_manager=mock_get_db_manager,
                get_session_data=mock_get_session_data,
                create_error_response=mock_create_error,
                _get_connection_fingerprint=mock_fingerprint,
                _clear_session_state=mock_clear,
            )

        mock_db_manager.connect_dremio.assert_called_once_with(
            host="dremio-host",
            port=9047,
            username="admin",
            password="pass123",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
