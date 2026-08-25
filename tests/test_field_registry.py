import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from field_registry import FieldRegistryError, load_field_registry  # noqa: E402


class FieldRegistryTests(unittest.TestCase):
    def test_loads_confirmed_dimension_and_derived_metric(self):
        registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")

        self.assertEqual(registry.require("material_type")["kind"], "dimension")
        self.assertEqual(registry.require("net_settlement_roi")["kind"], "derived_metric")

    def test_rejects_unknown_fields_and_invalid_operators(self):
        registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")

        with self.assertRaisesRegex(FieldRegistryError, "unknown field"):
            registry.require("arbitrary_sql")
        with self.assertRaisesRegex(FieldRegistryError, "does not allow"):
            registry.validate_operator("overall_cost", "regex")

