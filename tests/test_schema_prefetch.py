"""Tests for schema prefetch selection and column-type caching helpers."""

from __future__ import annotations

import unittest

from app.db.schema_prefetch import (  # noqa: I001
    column_type_cache_key,
    seed_column_types_from_table_info,
    select_tables_for_sample_prefetch,
)


class SelectTablesForSamplePrefetchTests(unittest.TestCase):
    def test_small_schema_prefetches_all(self) -> None:
        tables = ["users", "orders", "products"]
        selected = select_tables_for_sample_prefetch("orders by country", tables, max_tables=32)
        self.assertEqual(selected, tables)

    def test_large_schema_prefers_name_matches(self) -> None:
        tables = [f"table_{i}" for i in range(50)]
        tables.extend(["patient", "diagnosis", "lab_result"])
        selected = select_tables_for_sample_prefetch(
            "patient diagnosis lab results",
            tables,
            max_tables=10,
        )
        self.assertLessEqual(len(selected), 10)
        self.assertIn("patient", selected)
        self.assertIn("diagnosis", selected)

    def test_no_tokens_returns_prefix_of_catalog(self) -> None:
        tables = [f"t{i}" for i in range(40)]
        selected = select_tables_for_sample_prefetch("???", tables, max_tables=15)
        self.assertEqual(len(selected), 15)
        self.assertEqual(selected, tables[:15])


class ColumnTypeCacheTests(unittest.TestCase):
    def test_seed_from_table_info(self) -> None:
        cache: dict[tuple[str, str], str | None] = {}
        seed_column_types_from_table_info(
            {
                "users": {
                    "columns": [
                        {"name": "id", "type": "integer"},
                        {"name": "name", "type": "text"},
                    ]
                }
            },
            cache,
        )
        self.assertEqual(cache[column_type_cache_key("users", "id")], "integer")
        self.assertEqual(cache[column_type_cache_key("Users", "NAME")], "text")


if __name__ == "__main__":
    unittest.main()
