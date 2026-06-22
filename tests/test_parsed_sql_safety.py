"""Tests for the dialect-aware parsed SQL safety gate (analyze_sql_statement).

These lock in the cases where the parser is strictly stronger than the legacy
regex/startswith heuristics.
"""

import unittest

from src.security import analyze_sql_statement


class TestAnalyzeSqlStatement(unittest.TestCase):
    def test_plain_select_is_read_only(self):
        r = analyze_sql_statement("SELECT a, b FROM public.t WHERE x > 1")
        self.assertTrue(r["parsed"])
        self.assertTrue(r["is_read_only"])
        self.assertEqual(r["query_type"], "SELECT")
        self.assertIn("t", r["affected_tables"])

    def test_cte_select_is_read_only(self):
        r = analyze_sql_statement("WITH c AS (SELECT 1 AS n) SELECT n FROM c")
        self.assertTrue(r["is_read_only"])
        self.assertEqual(r["query_type"], "CTE_SELECT")

    def test_union_is_read_only(self):
        r = analyze_sql_statement("SELECT a FROM t UNION ALL SELECT a FROM u")
        self.assertTrue(r["is_read_only"])
        self.assertEqual(r["query_type"], "SELECT")

    def test_insert_is_blocked(self):
        r = analyze_sql_statement("INSERT INTO t VALUES (1)")
        self.assertFalse(r["is_read_only"])
        self.assertEqual(r["query_type"], "WRITE")
        self.assertIn("Insert", r["write_operations"])

    def test_delete_and_drop_blocked(self):
        for sql in ("DELETE FROM t WHERE 1=1", "DROP TABLE t"):
            r = analyze_sql_statement(sql)
            self.assertEqual(r["query_type"], "WRITE", sql)
            self.assertFalse(r["is_read_only"], sql)

    def test_dml_hidden_after_cte_is_blocked(self):
        # The legacy startswith('WITH') check would WRONGLY allow this.
        r = analyze_sql_statement("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x")
        self.assertEqual(r["query_type"], "WRITE")
        self.assertIn("Insert", r["write_operations"])
        self.assertFalse(r["is_read_only"])

    def test_semicolon_in_string_is_single_statement(self):
        # The legacy ';' split would WRONGLY flag this as multiple statements.
        r = analyze_sql_statement("SELECT a FROM t WHERE name = 'a;b'")
        self.assertTrue(r["single_statement"])
        self.assertTrue(r["is_read_only"])

    def test_multiple_statements_detected(self):
        r = analyze_sql_statement("SELECT 1; DROP TABLE t")
        self.assertFalse(r["single_statement"])
        self.assertIsNotNone(r["error"])

    def test_parse_error_reported_not_raised(self):
        r = analyze_sql_statement("SELECT FROM WHERE FROM ((")
        # Either a parse error or a non-read-only classification, but never raises.
        self.assertIn("parsed", r)


if __name__ == "__main__":
    unittest.main()
