import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from config_specs import (  # noqa: E402
    ConfigSpecError,
    highlight_spec_to_policy,
    query_spec_to_query_config,
    legacy_config_to_specs,
    resolve_highlight_spec,
    resolve_query_spec,
    spec_fingerprint,
    write_resolved_specs,
)
from field_registry import load_field_registry  # noqa: E402


def query_spec(**overrides):
    spec = {
        "schemaVersion": 1,
        "source": "qianchuan_direct_material_report",
        "scope": {"shopId": "shop-1", "advertiserId": "1770096415226951"},
        "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
        "filters": {
            "dimensionFilters": [{"field": "material_type", "operator": "eq", "value": "直播素材"}],
            "metricFilters": [{"field": "net_settlement_roi", "operator": "gte", "value": 1.5}],
        },
        "ranking": {"field": "overall_cost", "direction": "desc"},
        "collection": {"targetTopN": 9},
    }
    spec.update(overrides)
    return spec


def highlight_spec(**overrides):
    metrics = {
        "CLICK_COUNT": {"enabled": True, "strategy": "local_peaks", "maxFrames": 3},
        "LOSE_COUNT": {"enabled": False},
        "LIKE_COUNT": {"enabled": False},
        "COMMENT_COUNT": {"enabled": False},
        "SHARE_COUNT": {"enabled": True, "strategy": "global_maximum", "maxFrames": 1},
        "NEW_FOLLOWER_COUNT": {"enabled": False},
    }
    spec = {"schemaVersion": 1, "includeZeroSecond": True, "metrics": metrics}
    spec.update(overrides)
    return spec


class ConfigSpecTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")

    def test_query_spec_resolves_to_existing_deterministic_query_contract(self):
        resolved = resolve_query_spec(query_spec(), self.registry)
        query = query_spec_to_query_config(resolved)

        self.assertEqual(resolved["filters"]["dimensionFilters"][0]["field"], "material_type")
        self.assertEqual(query["shopId"], "shop-1")
        self.assertEqual(query["topN"], 9)
        self.assertEqual(query["sort"], {"field": "overall_cost", "direction": "desc"})
        self.assertEqual(query["metricFilters"][0]["value"], "1.5")

    def test_query_spec_rejects_unknown_and_unregistered_fields(self):
        invalid = query_spec()
        invalid["unexpected"] = True
        with self.assertRaisesRegex(ConfigSpecError, "unknown QuerySpec fields"):
            resolve_query_spec(invalid, self.registry)

        invalid = query_spec(filters={"dimensionFilters": [], "metricFilters": [{"field": "raw_sql", "operator": "gte", "value": 1}]})
        with self.assertRaisesRegex(ConfigSpecError, "unknown field"):
            resolve_query_spec(invalid, self.registry)

    def test_highlight_spec_requires_explicit_rule_for_every_qianchuan_metric(self):
        resolved = resolve_highlight_spec(highlight_spec())
        policy = highlight_spec_to_policy(resolved)

        self.assertEqual([item["metric"] for item in policy["policies"]], ["CLICK_COUNT", "SHARE_COUNT"])
        self.assertTrue(policy["includeZeroSecond"])
        missing = highlight_spec()
        del missing["metrics"]["LIKE_COUNT"]
        with self.assertRaisesRegex(ConfigSpecError, "all six"):
            resolve_highlight_spec(missing)

    def test_highlight_spec_rejects_invalid_enabled_rule(self):
        invalid = highlight_spec()
        invalid["metrics"]["CLICK_COUNT"] = {"enabled": True, "strategy": "global_maximum", "maxFrames": 2}
        with self.assertRaisesRegex(ConfigSpecError, "maxFrames"):
            resolve_highlight_spec(invalid)

    def test_legacy_config_keeps_its_duplicate_metric_rejection(self):
        legacy = {
            "schemaVersion": 1,
            "query": query_spec_to_query_config(resolve_query_spec(query_spec(), self.registry)),
            "highlightPolicy": {
                "schemaVersion": 1,
                "includeZeroSecond": True,
                "policies": [
                    {"metric": "CLICK_COUNT", "mode": "local_peaks", "maxFrames": 1},
                    {"metric": "CLICK_COUNT", "mode": "global_maximum", "maxFrames": 1},
                ],
            },
        }
        with self.assertRaisesRegex(ConfigSpecError, "duplicate"):
            legacy_config_to_specs(legacy, self.registry)

    def test_resolved_specs_and_fingerprint_are_stable_and_atomic(self):
        query = resolve_query_spec(query_spec(), self.registry)
        highlights = resolve_highlight_spec(highlight_spec())
        fingerprint = spec_fingerprint(query, highlights)
        self.assertEqual(fingerprint, spec_fingerprint(query, highlights))
        with tempfile.TemporaryDirectory() as directory:
            paths = write_resolved_specs(directory, query, highlights)
            self.assertEqual(json.loads(paths["query"].read_text(encoding="utf-8")), query)
            self.assertEqual(json.loads(paths["highlight"].read_text(encoding="utf-8")), highlights)
            self.assertEqual(json.loads(paths["fingerprint"].read_text(encoding="utf-8")), {"schemaVersion": 1, "configurationFingerprint": fingerprint})
