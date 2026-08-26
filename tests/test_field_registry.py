import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from field_registry import FieldRegistryError, load_field_registry  # noqa: E402
from qianchuan_report_client import DEFAULT_DIMENSIONS, DEFAULT_METRICS  # noqa: E402


class FieldRegistryTests(unittest.TestCase):
    def test_exposes_every_fixed_report_field_for_queryspec(self):
        registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")

        dimensions = [entry["id"] for entry in registry.entries() if entry["kind"] == "dimension"]
        metrics = [entry["id"] for entry in registry.entries() if entry["kind"] == "metric"]

        self.assertEqual(dimensions, DEFAULT_DIMENSIONS)
        self.assertEqual(metrics, DEFAULT_METRICS)
        self.assertEqual(len(metrics), 102)
        for field_id in metrics:
            self.assertTrue(registry.require(field_id)["sortable"])
            self.assertEqual(registry.require(field_id)["source"], ["metrics", field_id, "Value"])

    def test_exposes_direct_api_fields_and_rejects_business_aliases(self):
        registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")

        self.assertEqual(registry.require("roi2_material_type_v3")["kind"], "dimension")
        self.assertEqual(registry.require("total_prepay_and_pay_settle_roi2_1h")["kind"], "metric")
        self.assertEqual(registry.require("total_prepay_and_pay_settle_roi2_1h")["label"], "净成交ROI")
        with self.assertRaisesRegex(FieldRegistryError, "unknown field"):
            registry.require("net_settlement_roi")

    def test_rejects_unknown_fields_and_invalid_operators(self):
        registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")

        with self.assertRaisesRegex(FieldRegistryError, "unknown field"):
            registry.require("arbitrary_sql")
        with self.assertRaisesRegex(FieldRegistryError, "does not allow"):
            registry.validate_operator("stat_cost_for_overall_roi2", "regex")

    def test_does_not_expose_asset_table_only_stat_date_dimension(self):
        registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")

        with self.assertRaisesRegex(FieldRegistryError, "unknown field"):
            registry.require("stat_date")
