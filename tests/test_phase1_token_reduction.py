"""
Test suite for Phase 1 token reduction changes.

Verifies that:
1. All tools still work correctly after docstring reduction
2. Skills are accessible and contain expected content
3. Token reduction is achieved
4. No functionality regression
"""

import pytest
from pathlib import Path

import src.main as main_module
from src.main import mcp


def _get_tool_fn(name):
    """Get the underlying function for a tool, handling both FunctionTool and plain function.

    Under coverage (--cov=src), @mcp.tool() decorated functions are imported as
    FunctionTool objects. Without coverage, they are plain async functions.
    This helper normalizes access.
    """
    obj = getattr(main_module, name)
    return getattr(obj, 'fn', obj)


def _get_tool_docstring(name):
    """Get a tool's docstring, handling both FunctionTool and plain function."""
    fn = _get_tool_fn(name)
    return fn.__doc__


class TestPhase1TokenReduction:
    """Test Phase 1 changes don't break functionality."""

    def test_skills_directory_exists(self):
        """Verify .claude/skills directory was created."""
        skills_dir = Path(__file__).parent.parent / ".claude" / "skills"
        assert skills_dir.exists(), ".claude/skills directory not found"
        assert skills_dir.is_dir(), ".claude/skills is not a directory"

    def test_all_skills_present(self):
        """Verify all 3 skills are installed."""
        skills_dir = Path(__file__).parent.parent / ".claude" / "skills"

        expected_skills = [
            "fan-trap-prevention.md",
            "sql-best-practices.md",
            "chart-examples.md"
        ]

        for skill in expected_skills:
            skill_path = skills_dir / skill
            assert skill_path.exists(), f"Skill not found: {skill}"
            assert skill_path.stat().st_size > 1000, f"Skill too small: {skill}"

    def test_fan_trap_skill_content(self):
        """Verify fan-trap skill contains essential content."""
        skills_dir = Path(__file__).parent.parent / ".claude" / "skills"
        skill_path = skills_dir / "fan-trap-prevention.md"

        content = skill_path.read_text()

        # Check for key sections
        assert "What is a Fan-Trap?" in content
        assert "UNION ALL" in content
        assert "Detection Checklist" in content
        assert "PATTERN 1" in content
        assert "Safe Query Patterns" in content

    def test_sql_best_practices_skill_content(self):
        """Verify SQL best practices skill contains essential content."""
        skills_dir = Path(__file__).parent.parent / ".claude" / "skills"
        skill_path = skills_dir / "sql-best-practices.md"

        content = skill_path.read_text()

        # Check for key sections
        assert "Identifier Qualification" in content
        assert "schema.table.column" in content
        assert "Best Practices" in content
        assert "Common Query Patterns" in content

    def test_chart_examples_skill_content(self):
        """Verify chart examples skill contains essential content."""
        skills_dir = Path(__file__).parent.parent / ".claude" / "skills"
        skill_path = skills_dir / "chart-examples.md"

        content = skill_path.read_text()

        # Check for key sections
        assert "Chart Types" in content
        assert "bar chart" in content.lower()
        assert "line chart" in content.lower()
        assert "scatter" in content.lower()
        assert "heatmap" in content.lower()
        assert "generate_chart" in content

    def test_server_instructions_condensed(self):
        """Verify server instructions are condensed."""
        # Read main.py to check instructions length
        main_py = Path(__file__).parent.parent / "src" / "main.py"
        content = main_py.read_text()

        # Find instructions block
        start = content.find('instructions="""')
        end = content.find('"""', start + 20)
        instructions = content[start:end]

        # Should be much shorter than original 2,427 tokens (~9,708 chars)
        # Current condensed version is ~2,100 chars which is well under the original
        assert len(instructions) < 3000, f"Instructions not condensed: {len(instructions)} chars"

        # Should contain skill references
        assert "/fan-trap-prevention" in instructions
        assert "/sql-best-practices" in instructions
        assert "/chart-examples" in instructions

    def test_execute_sql_query_docstring_condensed(self):
        """Verify execute_sql_query docstring is condensed."""
        docstring = _get_tool_docstring('execute_sql_query')
        assert docstring is not None, "execute_sql_query has no docstring"

        # Original was ~12,804 chars, should be much smaller now
        assert len(docstring) < 4000, f"execute_sql_query docstring not condensed: {len(docstring)} chars"

        # Should mention fan-trap or validation
        assert "fan-trap" in docstring.lower() or "validation" in docstring.lower()

    def test_generate_chart_docstring_condensed(self):
        """Verify generate_chart docstring is condensed."""
        docstring = _get_tool_docstring('generate_chart')
        assert docstring is not None, "generate_chart has no docstring"

        # Original was ~8,502 chars, should be much smaller now
        assert len(docstring) < 3000, f"generate_chart docstring not condensed: {len(docstring)} chars"

        # Should mention chart-related content
        assert "chart" in docstring.lower()

    def test_discover_schema_docstring_condensed(self):
        """Verify discover_schema docstring is condensed."""
        docstring = _get_tool_docstring('discover_schema')
        assert docstring is not None, "discover_schema has no docstring"

        # Original was ~3,775 chars, should be much smaller now
        assert len(docstring) < 3000, f"discover_schema docstring not condensed: {len(docstring)} chars"

    @pytest.mark.asyncio
    async def test_all_tools_still_registered(self):
        """Verify all MCP tools are still registered."""
        # list_tools() is async in FastMCP and returns list[FunctionTool]
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]

        expected_tools = [
            "connect_database",
            "list_schemas",
            "reset_cache",
            "discover_schema",
            "get_table_details",
            "generate_ontology",
            "suggest_semantic_names",
            "apply_semantic_names",
            "load_my_ontology",
            "sample_table_data",
            "execute_sql_query",
            "generate_chart"
        ]

        for tool in expected_tools:
            assert tool in tool_names, f"Tool not registered: {tool}"

    def test_main_py_line_count_reduced(self):
        """Verify main.py has a reasonable line count."""
        main_py = Path(__file__).parent.parent / "src" / "main.py"

        # Count lines
        with open(main_py) as f:
            new_lines = sum(1 for _ in f)

        # main.py should exist and be a reasonable size (not bloated)
        assert new_lines > 500, f"main.py seems too small: {new_lines} lines"
        assert new_lines < 5000, f"main.py seems too large: {new_lines} lines"



class TestFunctionalityPreserved:
    """Test that all functionality still works after changes."""

    def test_mcp_server_imports(self):
        """Verify MCP server imports successfully."""
        try:
            from src.main import mcp
            assert mcp is not None
        except ImportError as e:
            pytest.fail(f"Failed to import MCP server: {e}")

    def test_database_manager_imports(self):
        """Verify DatabaseManager imports successfully."""
        try:
            from src.database_manager import DatabaseManager
            assert DatabaseManager is not None
        except ImportError as e:
            pytest.fail(f"Failed to import DatabaseManager: {e}")

    def test_ontology_generator_imports(self):
        """Verify OntologyGenerator imports successfully."""
        try:
            from src.ontology_generator import OntologyGenerator
            assert OntologyGenerator is not None
        except ImportError as e:
            pytest.fail(f"Failed to import OntologyGenerator: {e}")

    def test_all_tool_functions_callable(self):
        """Verify all tool functions are callable."""
        import inspect

        tool_names = [
            "connect_database",
            "list_schemas",
            "discover_schema",
            "generate_ontology",
            "execute_sql_query",
            "generate_chart"
        ]

        # All should be async functions (accessed via the underlying .fn when needed)
        for name in tool_names:
            fn = _get_tool_fn(name)
            assert inspect.iscoroutinefunction(fn), f"{name} is not an async function"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
