"""Regression tests: artifact downloads honor an explicit schema_name.

The file-source ontology and R2RML download paths must read the *requested*
schema's state, not whatever schema happens to be current in the session.
"""

from unittest.mock import MagicMock

from src.handler_context import HandlerContext
from src.handlers.ontology_artifacts import (
    download_ontology,
    download_r2rml,
    ensure_output_dir,
)
from src.session import SessionData


def _two_schema_session():
    """Session with schemas A and B (distinct files); current schema is B."""
    session = SessionData()
    session.set_current_schema("A")
    session.ontology_file = "ontology_A.ttl"
    session.r2rml_file = "r2rml_A.ttl"
    session.set_current_schema("B")
    session.ontology_file = "ontology_B.ttl"
    session.r2rml_file = "r2rml_B.ttl"
    # Current schema is now B; the request below explicitly asks for A.
    return session


def _services(session):
    return HandlerContext(get_session_data=lambda _ctx: session)


async def test_download_ontology_uses_requested_schema_not_current():
    session = _two_schema_session()
    conn_dir = ensure_output_dir()
    (conn_dir / "ontology_A.ttl").write_text("@prefix a: <x> .\n# A", encoding="utf-8")

    result = await download_ontology(
        MagicMock(), "A", "file", _services(session)
    )

    assert result["success"] is True
    # Must read A's file even though current schema is B.
    assert result["file_name"] == "ontology_A.ttl"


async def test_download_r2rml_uses_requested_schema_not_current():
    session = _two_schema_session()
    conn_dir = ensure_output_dir()
    (conn_dir / "r2rml_A.ttl").write_text('rr:baseIRI "http://a/" .\n', encoding="utf-8")

    result = await download_r2rml(MagicMock(), "A", _services(session))

    assert result["success"] is True
    assert result["file_name"] == "r2rml_A.ttl"
    assert result["schema_name"] == "A"
