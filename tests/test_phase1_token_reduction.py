"""
Test suite for Phase 1 token reduction changes.

Verifies that:
1. All tools still work correctly after docstring reduction
2. Skills are accessible and contain expected content
3. Token reduction is achieved
4. No functionality regression
"""

import pytest
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import mcp


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
        assert len(instructions) < 2000, f"Instructions not condensed: {len(instructions)} chars"

        # Should contain skill references
        assert "/fan-trap-prevention" in instructions
        assert "/sql-best-practices" in instructions
        assert "/chart-examples" in instructions

    def test_execute_sql_query_docstring_condensed(self):
        """Verify execute_sql_query docstring is condensed."""
        from main import execute_sql_query

        docstring = execute_sql_query.__doc__
        assert docstring is not None, "execute_sql_query has no docstring"

        # Original was ~12,804 chars, should be ~2,400 chars
        assert len(docstring) < 4000, f"execute_sql_query docstring not condensed: {len(docstring)} chars"

        # Should reference skills
        assert "/fan-trap-prevention" in docstring or "/sql-best-practices" in docstring

    def test_generate_chart_docstring_condensed(self):
        """Verify generate_chart docstring is condensed."""
        from main import generate_chart

        docstring = generate_chart.__doc__
        assert docstring is not None, "generate_chart has no docstring"

        # Original was ~8,502 chars, should be ~1,600 chars
        assert len(docstring) < 3000, f"generate_chart docstring not condensed: {len(docstring)} chars"

        # Should reference chart-examples skill
        assert "/chart-examples" in docstring

    def test_validate_sql_syntax_docstring_condensed(self):
        """Verify validate_sql_syntax docstring is condensed."""
        from main import validate_sql_syntax

        docstring = validate_sql_syntax.__doc__
        assert docstring is not None, "validate_sql_syntax has no docstring"

        # Original was ~4,465 chars, should be ~1,200 chars
        assert len(docstring) < 2000, f"validate_sql_syntax docstring not condensed: {len(docstring)} chars"

    def test_analyze_schema_docstring_condensed(self):
        """Verify analyze_schema docstring is condensed."""
        from main import analyze_schema

        docstring = analyze_schema.__doc__
        assert docstring is not None, "analyze_schema has no docstring"

        # Original was ~3,775 chars, should be ~1,000 chars
        assert len(docstring) < 2000, f"analyze_schema docstring not condensed: {len(docstring)} chars"

    def test_all_tools_still_registered(self):
        """Verify all MCP tools are still registered."""
        # Get all tools from MCP server
        tools = mcp.list_tools()
        tool_names = [tool.name for tool in tools]

        expected_tools = [
            "connect_database",
            "diagnose_connection_issue",
            "list_schemas",
            "analyze_schema",
            "get_analysis_context",
            "sample_table_data",
            "generate_ontology",
            "load_my_ontology",
            "validate_sql_syntax",
            "execute_sql_query",
            "generate_chart",
            "get_server_info"
        ]

        for tool in expected_tools:
            assert tool in tool_names, f"Tool not registered: {tool}"

    def test_main_py_line_count_reduced(self):
        """Verify main.py has fewer lines after Phase 1."""
        main_py = Path(__file__).parent.parent / "src" / "main.py"
        backup = Path(__file__).parent.parent / "src" / "main.py.backup"

        # Count lines
        with open(main_py) as f:
            new_lines = sum(1 for _ in f)

        if backup.exists():
            with open(backup) as f:
                old_lines = sum(1 for _ in f)

            # Should have removed ~750 lines
            reduction = old_lines - new_lines
            assert reduction >= 700, f"Not enough lines removed: {reduction}"
            assert new_lines < 2200, f"main.py still too large: {new_lines} lines"

    def test_token_savings_estimate(self):
        """Estimate token savings achieved."""
        main_py = Path(__file__).parent.parent / "src" / "main.py"
        backup = Path(__file__).parent.parent / "src" / "main.py.backup"

        if not backup.exists():
            pytest.skip("No backup file for comparison")

        # Read both files
        new_content = main_py.read_text()
        old_content = backup.read_text()

        # Rough token estimate (chars / 4)
        old_tokens = len(old_content) // 4
        new_tokens = len(new_content) // 4
        savings = old_tokens - new_tokens

        # Should save at least 6,000 tokens (conservative)
        assert savings >= 6000, f"Insufficient token savings: {savings} tokens"

    def test_backup_exists(self):
        """Verify backup was created."""
        backup = Path(__file__).parent.parent / "src" / "main.py.backup"
        assert backup.exists(), "Backup file not created"
        assert backup.stat().st_size > 100000, "Backup file too small"


class TestFunctionalityPreserved:
    """Test that all functionality still works after changes."""

    def test_mcp_server_imports(self):
        """Verify MCP server imports successfully."""
        try:
            from main import mcp
            assert mcp is not None
        except ImportError as e:
            pytest.fail(f"Failed to import MCP server: {e}")

    def test_database_manager_imports(self):
        """Verify DatabaseManager imports successfully."""
        try:
            from database_manager import DatabaseManager
            assert DatabaseManager is not None
        except ImportError as e:
            pytest.fail(f"Failed to import DatabaseManager: {e}")

    def test_ontology_generator_imports(self):
        """Verify OntologyGenerator imports successfully."""
        try:
            from ontology_generator import OntologyGenerator
            assert OntologyGenerator is not None
        except ImportError as e:
            pytest.fail(f"Failed to import OntologyGenerator: {e}")

    def test_all_tool_functions_callable(self):
        """Verify all tool functions are callable."""
        from main import (
            connect_database,
            list_schemas,
            analyze_schema,
            generate_ontology,
            validate_sql_syntax,
            execute_sql_query,
            generate_chart
        )

        # All should be async functions
        import inspect
        assert inspect.iscoroutinefunction(connect_database)
        assert inspect.iscoroutinefunction(list_schemas)
        assert inspect.iscoroutinefunction(analyze_schema)
        assert inspect.iscoroutinefunction(generate_ontology)
        assert inspect.iscoroutinefunction(validate_sql_syntax)
        assert inspect.iscoroutinefunction(execute_sql_query)
        assert inspect.iscoroutinefunction(generate_chart)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
