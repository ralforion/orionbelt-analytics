"""Every main tool's result names the most likely next call.

Hints existed but scattered: some results had a next_steps dict, some a
next_step string, and the clearest went out only as progress messages most
clients never show the model. Now one rule set, read from the session's
state, and one shape: next_steps, a list of {tool, arguments, why}.
"""

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client

import src.main as main_module
import src.server_state as state_module
from src.handlers import connection as connection_handler
from src.handlers.next_steps import attach, for_tool
from src.main import mcp
from src.server_state import ServerState


def _session(**state: Any) -> Any:
    cached = state.pop("cached", False)
    values = {
        "current_schema": "main",
        "working_schema": "main",
        "ontology_file": None,
        "loaded_ontology": None,
        "ontology_enriched": False,
        "get_cached_schema": lambda _schema: [object()] if cached else None,
    }
    values.update(state)
    return SimpleNamespace(**values)


def _tools(steps: list[dict[str, Any]]) -> list[str]:
    return [step["tool"] for step in steps]


class TestTheRules:
    def test_a_fresh_connection_discovers_first(self):
        assert _tools(for_tool("connect_database", _session(), "ok"))[0] == (
            "discover_schema"
        )

    def test_a_discovered_schema_needs_an_ontology(self):
        steps = for_tool("discover_schema", _session(cached=True), {})

        assert _tools(steps) == ["generate_ontology"]

    def test_a_restored_workspace_goes_straight_to_questions(self):
        session = _session(cached=True, ontology_file="o.ttl")

        assert _tools(for_tool("connect_database", session, "ok"))[0] == (
            "graphrag_query_context"
        )

    def test_after_an_ontology_naming_and_validation_are_offered(self):
        tools = _tools(for_tool("generate_ontology", _session(cached=True), "ok"))

        assert tools[:2] == ["graphrag_query_context", "execute_sql_query"]
        assert "suggest_semantic_names" in tools
        assert "validate_relationship" in tools

    def test_an_enriched_ontology_is_not_offered_naming_again(self):
        session = _session(cached=True, ontology_enriched=True)

        assert "suggest_semantic_names" not in _tools(
            for_tool("generate_ontology", session, "ok")
        )

    def test_an_upload_that_did_not_activate_says_what_to_fix(self):
        steps = for_tool("load_my_ontology", _session(), {"activated": False})

        assert _tools(steps) == ["load_my_ontology"]
        assert "oba_requirements" in steps[0]["why"]

    def test_a_refuted_relationship_points_to_another_route(self):
        steps = for_tool("validate_relationship", _session(), {"status": "refuted"})

        assert _tools(steps) == ["graphrag_find_join_path"]

    def test_a_query_result_can_be_charted(self):
        steps = for_tool("execute_sql_query", _session(), {"success": True})

        assert _tools(steps) == ["generate_chart"]

    @pytest.mark.parametrize(
        "databases, arguments",
        [([{"name": "a"}], set()), ([{"name": "a"}, {"name": "b"}], {"database"})],
    )
    def test_one_database_needs_no_choice(self, databases, arguments):
        (step,) = for_tool("list_databases", _session(), {"databases": databases})

        assert set(step["arguments"]) == arguments


class TestAttaching:
    STEPS = [{"tool": "t", "arguments": {"a": 1}, "why": "because"}]

    def test_a_dict_gets_one_uniform_field(self):
        result = attach({"ok": 1, "next_step": "old"}, self.STEPS)

        assert result == {"ok": 1, "next_steps": self.STEPS}

    def test_a_success_with_empty_error_fields_still_gets_them(self):
        result = attach(
            {"success": True, "error": None, "error_type": None}, self.STEPS
        )

        assert result["next_steps"] == self.STEPS

    def test_text_gets_a_section(self):
        result = attach("Connected.", self.STEPS)

        assert result.startswith("Connected.")
        assert "## Next step\n- t(a=1) -- because" in result

    @pytest.mark.parametrize(
        "failed",
        [
            {"success": False, "error": "x"},
            {"error": "x", "error_type": "y"},
            {"success": None, "error": "x", "error_type": "y"},
            json.dumps({"error": "x", "error_type": "y"}),
        ],
    )
    def test_an_error_keeps_its_own_guidance(self, failed):
        assert attach(failed, self.STEPS) == failed


@pytest.fixture
def duck(monkeypatch, tmp_path):
    import duckdb

    path = tmp_path / "n.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR)")
    con.execute("INSERT INTO customers VALUES (1, 'a'), (2, 'b')")
    con.close()
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    monkeypatch.setattr("src.paths.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setenv("AUTO_GRAPHRAG", "false")
    monkeypatch.setenv("OBA_SHACL_VALIDATE", "false")
    monkeypatch.delenv("OBA_DATABASES", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(path))
    yield fresh
    fresh.cleanup()


async def test_the_workflow_names_each_next_call(duck):
    async with Client(mcp) as client:
        connected = (
            await client.call_tool("connect_database", {"db_type": "duckdb"})
        ).data
        handle = re.search(r"(ob_[a-z0-9]{6})", connected).group(1)
        on = {"connection": handle}
        discovered = (await client.call_tool("discover_schema", on)).data
        generated = await client.call_tool(
            "generate_ontology", {**on, "auto_persist": False}
        )
        queried = (
            await client.call_tool(
                "execute_sql_query",
                {
                    **on,
                    "sql_query": "SELECT id, name FROM main.customers",
                    "checklist_completed": True,
                },
            )
        ).data

    assert "## Next step\n- discover_schema()" in connected
    assert _tools(discovered["next_steps"]) == ["generate_ontology"]
    generated_text = (
        generated.data if isinstance(generated.data, str) else str(generated.data)
    )
    assert "graphrag_query_context(" in generated_text
    assert _tools(queried["next_steps"]) == ["generate_chart"]
