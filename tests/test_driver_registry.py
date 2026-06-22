"""Parity tests for the central driver registry.

These lock the previously-drifting lists together: the supported-database list,
the driver classes, the sqlglot dialect map, and OBQC's dialect map must all
describe exactly the same set of databases.
"""

import unittest

from sqlglot.dialects.dialect import Dialect

from src.constants import DB_SQLGLOT_DIALECTS, SUPPORTED_DB_TYPES
from src.drivers import DATABASE_REGISTRY, get_driver_class, supported_db_types
from src.obqc_validator import OBQCValidator


class TestDriverRegistryParity(unittest.TestCase):
    def test_supported_types_derive_from_canonical_map(self):
        self.assertEqual(SUPPORTED_DB_TYPES, list(DB_SQLGLOT_DIALECTS))

    def test_registry_matches_supported_types(self):
        self.assertEqual(supported_db_types(), SUPPORTED_DB_TYPES)
        self.assertEqual(set(DATABASE_REGISTRY), set(SUPPORTED_DB_TYPES))

    def test_obqc_dialect_map_is_the_canonical_map(self):
        self.assertEqual(OBQCValidator.DIALECT_MAP, DB_SQLGLOT_DIALECTS)

    def test_each_driver_class_declares_matching_db_type(self):
        for db_type, meta in DATABASE_REGISTRY.items():
            with self.subTest(db_type=db_type):
                self.assertEqual(meta.driver_cls.db_type, db_type)
                self.assertEqual(meta.dialect, DB_SQLGLOT_DIALECTS[db_type])

    def test_every_dialect_resolves_in_sqlglot(self):
        for db_type, meta in DATABASE_REGISTRY.items():
            with self.subTest(db_type=db_type):
                Dialect.get_or_raise(meta.dialect)

    def test_get_driver_class_lookup_and_error(self):
        from src.drivers import PostgreSQLDriver

        self.assertIs(get_driver_class("postgresql"), PostgreSQLDriver)
        with self.assertRaises(ValueError):
            get_driver_class("nosuchdb")


if __name__ == "__main__":
    unittest.main()
