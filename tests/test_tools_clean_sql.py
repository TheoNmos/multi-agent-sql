"""Tests for SQL cleaning used across tools and execution probes."""

from __future__ import annotations

import unittest

from app.agents.tools import clean_sql


class CleanSqlTests(unittest.TestCase):
    def test_strips_trailing_semicolon_and_newlines(self) -> None:
        raw = "SELECT 1;\n\n"
        self.assertEqual(clean_sql(raw), "SELECT 1")

    def test_collapses_whitespace(self) -> None:
        raw = "SELECT\n  id\nFROM\n  users"
        self.assertEqual(clean_sql(raw), "SELECT id FROM users")


if __name__ == "__main__":
    unittest.main()
