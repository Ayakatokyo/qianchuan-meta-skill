import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from field_registry import load_field_registry  # noqa: E402
from material_query import MaterialQueryError, execute_material_query, parse_query_config  # noqa: E402


def origin_row(material_id, name, material_type, cost, net_settlement):
    return json.dumps(
        {
            "dimensions": {
                "material_id": {"Value": material_id},
                "roi2_material_video_name": {"ValueStr": name},
                "roi2_material_type_v3": {"ValueStr": material_type},
                "roi2_material_video_type": {"ValueStr": "短视频"},
            },
            "metrics": {
                "stat_cost_for_overall_roi2": {"Value": str(cost)},
                "total_order_settle_amount_for_roi2_1h": {"Value": str(net_settlement)},
            },
        },
        ensure_ascii=False,
    )


class MaterialQueryTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")
        self.query = parse_query_config(
            {
                "schemaVersion": 1,
                "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
                "advertiserId": "1770096415226951",
                "topN": 2,
                "dimensionFilters": [{"field": "material_type", "operator": "eq", "value": "直播素材"}],
                "metricFilters": [{"field": "net_settlement_roi", "operator": "gte", "value": 1.5}],
                "sort": {"field": "net_settlement_roi", "direction": "desc"},
            },
            self.registry,
        )

    def test_filters_after_period_aggregation_and_sorts_by_derived_roi(self):
        result = execute_material_query(
            [
                origin_row("m-1", "素材一", "直播素材", 10, 10),
                origin_row("m-1", "素材一", "直播素材", 10, 30),
                origin_row("m-2", "素材二", "直播素材", 10, 40),
                origin_row("m-3", "素材三", "图文", 10, 100),
            ],
            self.query,
            self.registry,
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-2", "m-1"])
        self.assertEqual(result["receipt"]["sort"]["field"], "net_settlement_roi")
        self.assertEqual(result["materials"][0]["sortValue"], "4")
        self.assertEqual(result["materials"][0]["metrics"]["overall_cost"], "10")
        self.assertEqual(result["receipt"]["excluded"][0]["materialId"], "m-3")

    def test_rejects_unknown_fields_and_excludes_zero_denominator(self):
        invalid = dict(self.query)
        invalid["sort"] = {"field": "arbitrary_sql", "direction": "desc"}
        with self.assertRaisesRegex(MaterialQueryError, "unknown field"):
            execute_material_query([], invalid, self.registry)

        result = execute_material_query(
            [origin_row("m-1", "素材一", "直播素材", 0, 30)], self.query, self.registry
        )
        self.assertEqual(result["materials"], [])
        self.assertEqual(result["receipt"]["excluded"][0]["reason"], "unavailable_metric_value")

    def test_excludes_aigc_dynamic_creative_collection_before_aggregation(self):
        query = dict(self.query)
        query["dimensionFilters"] = []
        query["metricFilters"] = []
        query["sort"] = {"field": "overall_cost", "direction": "desc"}
        result = execute_material_query(
            [
                origin_row("-2", "AIGC 动态创意", "直播素材", 100, 100),
                origin_row("-2", "AIGC 动态创意", "直播素材", 200, 200),
                origin_row("m-1", "可映射素材", "直播素材", 10, 10),
            ],
            query,
            self.registry,
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-1"])
        aigc_exclusions = [
            item for item in result["receipt"]["excluded"]
            if item["reason"] == "aigc_dynamic_creative_collection_not_single_material"
        ]
        self.assertEqual(aigc_exclusions, [
            {
                "materialId": "-2",
                "reason": "aigc_dynamic_creative_collection_not_single_material",
                "sourceRowCount": 2,
            }
        ])
