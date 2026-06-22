"""Integration regression tests for DatabaseManager.validate_sql_syntax().

These exercise the full gate (regex SQLInjectionValidator -> parser-based
analyze_sql_statement -> driver), not just the helper in isolation.
"""

import unittest
from unittest.mock import MagicMock

from src.database_manager import DatabaseManager


class TestValidateSqlSyntaxIntegration(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseManager()
        self.db.engine = MagicMock()  # pass the connection check
        self.db.connection_info = {"type": "postgresql"}
        # Driver "passes" syntactically valid read queries straight through.
        self.db._driver = MagicMock()
        self.db._driver.validate_sql_syntax = lambda q, vr: {**vr, "is_valid": True}

    def test_semicolon_inside_string_literal_is_not_multi_statement(self):
        # Regression: the regex ';' split used to wrongly reject this as
        # "multiple statements" before the parser could classify it.
        result = self.db.validate_sql_syntax(
            "SELECT a FROM t WHERE name = 'a;b'"
        )
        self.assertNotEqual(result.get("error_type"), "security_error")
        self.assertEqual(result["query_type"], "SELECT")
        self.assertTrue(result["is_valid"])

    def test_dml_hidden_after_cte_is_blocked_by_parser(self):
        # This passes the regex WITH...SELECT "safe pattern", so only the
        # parser-based gate catches the INSERT.
        result = self.db.validate_sql_syntax(
            "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"
        )
        self.assertFalse(result["is_valid"])
        self.assertEqual(result["error_type"], "forbidden_operation")

    def test_plain_multi_statement_still_blocked(self):
        result = self.db.validate_sql_syntax("SELECT 1; SELECT 2")
        self.assertFalse(result["is_valid"])

    def test_plain_select_allowed(self):
        result = self.db.validate_sql_syntax("SELECT a, b FROM public.t")
        self.assertTrue(result["is_valid"])
        self.assertEqual(result["query_type"], "SELECT")


if __name__ == "__main__":
    unittest.main()
