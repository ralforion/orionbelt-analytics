"""Resetting the cache reaches the database metadata, and empty means empty.

Two ways the server kept answering from stale or missing knowledge:

* `reset_cache` cleared the session's schema state but not the five-minute
  metadata cache in `DatabaseManager`, so the discovery that followed read the
  same table list the user had just asked to get rid of.
* Views were looked up by truthiness. A schema with no views read exactly like
  a schema nobody had discovered, so every call went back to the database, and
  the empty answer was never cached to stop it.
"""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from src.database_manager import ColumnInfo, DatabaseManager, TableInfo
from src.handler_context import HandlerContext
from src.handlers import graphrag as graphrag_handler
from src.handlers import schema as schema_handler
from src.session import SchemaCache, SessionData


class TestTheMetadataCacheIsDropped:
    """What `reset_cache` has to reach for a rediscovery to see new tables."""

    def test_the_manager_can_drop_its_cache(self):
        manager = DatabaseManager()
        manager._store_in_cache("get_tables:public", ["orders"])
        manager._store_in_cache("get_views:public", [])

        assert manager.clear_metadata_cache() == 2
        assert manager._get_from_cache("get_tables:public") is None
        assert manager.clear_metadata_cache() == 0

    async def test_reset_cache_clears_the_database_metadata(self):
        manager = DatabaseManager()
        manager._store_in_cache("get_tables:public", ["stale_table"])
        session = SessionData()
        session.db_manager = manager
        services = HandlerContext(get_session_data=lambda _ctx: session)

        result = await schema_handler.reset_cache(Mock(), "schema", services)

        assert manager._get_from_cache("get_tables:public") is None
        assert any("database metadata" in entry for entry in result["cleared_caches"])

    async def test_resetting_only_the_ontology_leaves_the_metadata(self):
        manager = DatabaseManager()
        manager._store_in_cache("get_tables:public", ["orders"])
        session = SessionData()
        session.db_manager = manager
        services = HandlerContext(get_session_data=lambda _ctx: session)

        await schema_handler.reset_cache(Mock(), "ontology", services)

        assert manager._get_from_cache("get_tables:public") == ["orders"]

    async def test_a_session_without_a_manager_still_resets(self):
        session = SessionData()
        services = HandlerContext(get_session_data=lambda _ctx: session)

        result = await schema_handler.reset_cache(Mock(), "all", services)

        assert "schema" in result["cleared_caches"]


class TestEmptyIsNotUnknown:
    """A schema with no views must not be looked up forever."""

    def test_the_cache_distinguishes_them(self):
        cache = SchemaCache()

        assert cache.has_cached_views("public") is False

        cache.cache_views("public", [])

        assert cache.has_cached_views("public") is True
        assert cache.get_cached_views("public") == []

    def test_the_session_exposes_the_distinction(self):
        session = SessionData()

        assert session.has_cached_views("public") is False

        session.cache_views("public", [])

        assert session.has_cached_views("public") is True

    def test_another_schema_is_still_unknown(self):
        session = SessionData()
        session.cache_views("public", [])

        assert session.has_cached_views("analytics") is False


class TestTheCallerStopsRepeatingTheWork:
    """init_graphrag asked the database for views on every call."""

    def _session(self) -> SessionData:
        session = SessionData()
        # A discovered schema, so the handler reaches the views at all.
        session.cache_schema_analysis(
            "public",
            [
                TableInfo(
                    name="orders",
                    schema="public",
                    columns=[
                        ColumnInfo(
                            name="id",
                            data_type="INTEGER",
                            is_nullable=False,
                            is_primary_key=True,
                            is_foreign_key=False,
                        )
                    ],
                    primary_keys=["id"],
                    foreign_keys=[],
                )
            ],
        )
        return session

    def _db(self, views: list[Any]) -> Mock:
        manager = Mock()
        manager.get_views.return_value = views
        manager.get_tables.return_value = []
        manager.analyze_tables.return_value = {}
        manager.prefetch_schema_constraints.return_value = None
        manager.has_engine.return_value = True
        return manager

    async def _init(self, session: SessionData, db: Mock, monkeypatch) -> None:
        monkeypatch.setattr(graphrag_handler, "GraphRAGManager", Mock())
        monkeypatch.setattr(graphrag_handler, "_save_graphrag_state", AsyncMock())
        monkeypatch.setattr(graphrag_handler, "update_workspace_section", AsyncMock())
        monkeypatch.setattr(
            graphrag_handler, "get_active_version_number", AsyncMock(return_value=1)
        )
        services = HandlerContext(
            get_session_data=lambda _ctx: session,
            get_session_db_manager=lambda _ctx: db,
            create_error_response=lambda *a, **k: {"error": a[0] if a else ""},
        )
        await graphrag_handler.initialize_graphrag(Mock(), "public", "tfidf", services)

    async def test_a_schema_without_views_is_asked_once(self, monkeypatch):
        session = self._session()
        db = self._db([])

        await self._init(session, db, monkeypatch)
        first = db.get_views.call_count
        session.graphrag_manager = None
        await self._init(session, db, monkeypatch)

        assert first == 1
        assert db.get_views.call_count == 1
        assert session.has_cached_views("public") is True

    async def test_discovered_views_are_still_used(self, monkeypatch):
        session = self._session()
        view = Mock()
        view.name = "v_revenue"
        view.definition = "SELECT 1"
        view.source_tables = []
        session.cache_views("public", [view])
        db = self._db([])

        await self._init(session, db, monkeypatch)

        db.get_views.assert_not_called()

    async def test_a_failure_to_fetch_views_is_not_cached_as_empty(self, monkeypatch):
        session = self._session()
        db = self._db([])
        db.get_views.side_effect = RuntimeError("catalog unavailable")

        await self._init(session, db, monkeypatch)

        # Nothing was learned, so the next call must ask again.
        assert session.has_cached_views("public") is False


@pytest.mark.parametrize("cache_type", ["schema", "all"])
async def test_reset_reports_what_it_cleared(cache_type):
    manager = DatabaseManager()
    manager._store_in_cache("get_views:public", [])
    session = SessionData()
    session.db_manager = manager
    services = HandlerContext(get_session_data=lambda _ctx: session)

    result = await schema_handler.reset_cache(Mock(), cache_type, services)

    assert "schema" in result["cleared_caches"]
    assert any("database metadata" in entry for entry in result["cleared_caches"])
