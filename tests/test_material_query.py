import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from field_registry import load_field_registry  # noqa: E402
from material_query import MaterialQueryError, execute_material_query, parse_query_config  # noqa: E402


def origin_row(material_id, name, material_type, cost, net_settlement, roi):
    return {
        "dimensions": {
            "material_id": {"Value": material_id},
            "roi2_material_video_name": {"ValueStr": name},
            "roi2_material_type_v3": {"ValueStr": material_type},
            "roi2_material_video_type": {"ValueStr": "短视频"},
        },
        "metrics": {
            "stat_cost_for_overall_roi2": {"Value": str(cost)},
            "total_order_settle_amount_for_roi2_1h": {"Value": str(net_settlement)},
            "total_prepay_and_pay_settle_roi2_1h": {"Value": str(roi)},
        },
    }


class MaterialQueryTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")
        self.query = parse_query_config(
            {
                "schemaVersion": 1,
                "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
                "shopId": "shop-1",
                "advertiserId": "1770096415226951",
                "topN": 2,
                "dimensionFilters": [{"field": "roi2_material_type_v3", "operator": "eq", "value": "直播素材"}],
                "metricFilters": [{"field": "total_prepay_and_pay_settle_roi2_1h", "operator": "gte", "value": 1.5}],
                "sort": {"field": "total_prepay_and_pay_settle_roi2_1h", "direction": "desc"},
            },
            self.registry,
        )

    def test_filters_and_sorts_direct_api_values_without_deriving_metrics(self):
        result = execute_material_query(
            [
                origin_row("m-1", "素材一", "直播素材", 10, 10, 1.5),
                origin_row("m-2", "素材二", "直播素材", 10, 40, 2),
                origin_row("m-3", "素材三", "图文", 10, 100, 10),
            ],
            self.query,
            self.registry,
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-2", "m-1"])
        self.assertEqual(result["receipt"]["sort"]["field"], "total_prepay_and_pay_settle_roi2_1h")
        self.assertEqual(result["materials"][0]["sortValue"], "2")
        self.assertEqual(result["materials"][0]["metrics"]["total_prepay_and_pay_settle_roi2_1h"], "2")
        self.assertEqual(result["receipt"]["excluded"][0]["materialId"], "m-3")

    def test_preserves_source_order_when_sort_is_not_configured(self):
        query = parse_query_config(
            {
                "schemaVersion": 1,
                "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
                "shopId": "shop-1",
                "advertiserId": "1770096415226951",
                "topN": 2,
                "dimensionFilters": [],
                "metricFilters": [],
            },
            self.registry,
        )

        result = execute_material_query(
            [
                origin_row("m-2", "素材二", "直播素材", 20, 40, 2),
                origin_row("m-1", "素材一", "直播素材", 10, 10, 1.5),
            ],
            query,
            self.registry,
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-2", "m-1"])
        self.assertIsNone(result["receipt"]["sort"])
        self.assertIsNone(result["materials"][0]["rankingMetric"])
        self.assertIsNone(result["materials"][0]["sortValue"])

    def test_rejects_business_aliases_and_uses_direct_zero_values(self):
        invalid = dict(self.query)
        invalid["sort"] = {"field": "net_settlement_roi", "direction": "desc"}
        with self.assertRaisesRegex(MaterialQueryError, "unknown field"):
            execute_material_query([], invalid, self.registry)

        query = dict(self.query)
        query["metricFilters"] = [{"field": "total_prepay_and_pay_settle_roi2_1h", "operator": "eq", "value": 0}]
        query["sort"] = {"field": "stat_cost_for_overall_roi2", "direction": "desc"}
        result = execute_material_query(
            [origin_row("m-1", "素材一", "直播素材", 0, 30, 0)], query, self.registry
        )
        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-1"])

    def test_resolves_verified_display_fields_to_api_fields_before_filtering_and_sorting(self):
        query = dict(self.query)
        query["metricFilters"] = [{"field": "整体展现次数", "operator": "gte", "value": 100}]
        query["sort"] = {"field": "整体点击次数", "direction": "desc"}
        first = origin_row("m-1", "素材一", "直播素材", 10, 20, 2)
        second = origin_row("m-2", "素材二", "直播素材", 20, 30, 3)
        first["metrics"].update({"live_show_count_for_roi2_v2": {"Value": "100"}, "live_watch_count_for_roi2_v2": {"Value": "10"}})
        second["metrics"].update({"live_show_count_for_roi2_v2": {"Value": "200"}, "live_watch_count_for_roi2_v2": {"Value": "20"}})

        result = execute_material_query([first, second], query, self.registry)

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-2", "m-1"])
        self.assertEqual(result["receipt"]["sort"]["field"], "live_watch_count_for_roi2_v2")
        self.assertEqual(result["receipt"]["query"]["metricFilters"][0]["field"], "live_show_count_for_roi2_v2")

    def test_excludes_aigc_dynamic_creative_collection_before_duplicate_validation(self):
        query = dict(self.query)
        query["dimensionFilters"] = []
        query["metricFilters"] = []
        query["sort"] = {"field": "stat_cost_for_overall_roi2", "direction": "desc"}
        result = execute_material_query(
            [
                origin_row("-2", "AIGC 动态创意", "直播素材", 100, 100, 1),
                origin_row("-2", "AIGC 动态创意", "直播素材", 200, 200, 1),
                origin_row("m-1", "可映射素材", "直播素材", 10, 10, 1),
            ],
            query,
            self.registry,
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-1"])
        self.assertIn(
            {
                "materialId": "-2",
                "reason": "aigc_dynamic_creative_collection_not_single_material",
                "sourceRowCount": 2,
            },
            result["receipt"]["excluded"],
        )

    def test_requires_shop_id_and_accepts_direct_report_records(self):
        raw_query = {
            "schemaVersion": 1,
            "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
            "advertiserId": "1770096415226951",
            "topN": 1,
            "dimensionFilters": [],
            "metricFilters": [],
            "sort": {"field": "stat_cost_for_overall_roi2", "direction": "desc"},
        }
        with self.assertRaisesRegex(MaterialQueryError, "shopId"):
            parse_query_config(raw_query, self.registry)

        raw_query["shopId"] = "shop-1"
        result = execute_material_query(
            [origin_row("m-1", "素材一", "直播素材", 10, 20, 2)], raw_query, self.registry
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-1"])
        self.assertEqual(result["receipt"]["query"]["shopId"], "shop-1")

    def test_skips_missing_material_id_without_aborting_other_candidates(self):
        broken = origin_row("m-broken", "缺失ID", "直播素材", 99, 99, 2)
        del broken["dimensions"]["material_id"]
        result = execute_material_query(
            [broken, origin_row("m-1", "可用素材", "直播素材", 10, 20, 2)],
            self.query,
            self.registry,
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-1"])
        self.assertEqual(result["receipt"]["sourceRecordCount"], 2)
        self.assertEqual(result["receipt"]["skippedSourceRecordCount"], 1)
        self.assertIn({"sourceRowIndex": 1, "reason": "missing_material_id"}, result["receipt"]["excluded"])

    def test_excludes_missing_active_dimension_with_a_data_quality_reason(self):
        broken = origin_row("m-broken", "缺少类型", "直播素材", 10, 20, 2)
        del broken["dimensions"]["roi2_material_type_v3"]
        result = execute_material_query(
            [broken, origin_row("m-1", "可用素材", "直播素材", 10, 20, 2)],
            self.query,
            self.registry,
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-1"])
        self.assertIn(
            {"materialId": "m-broken", "reason": "unavailable_dimension_value", "field": "roi2_material_type_v3"},
            result["receipt"]["excluded"],
        )

    def test_excludes_missing_active_metric_with_a_data_quality_reason(self):
        broken = origin_row("m-broken", "缺少ROI", "直播素材", 10, 20, 2)
        del broken["metrics"]["total_prepay_and_pay_settle_roi2_1h"]
        result = execute_material_query(
            [broken, origin_row("m-1", "可用素材", "直播素材", 10, 20, 2)],
            self.query,
            self.registry,
        )

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-1"])
        self.assertIn(
            {"materialId": "m-broken", "reason": "unavailable_metric_value"}, result["receipt"]["excluded"]
        )

    def test_rejects_duplicate_non_aigc_material_ids(self):
        with self.assertRaisesRegex(MaterialQueryError, "duplicate material_id"):
            execute_material_query(
                [
                    origin_row("m-1", "素材一", "直播素材", 10, 20, 2),
                    origin_row("m-1", "素材一", "直播素材", 20, 40, 2),
                ],
                self.query,
                self.registry,
            )

    def test_accepts_a_newly_exposed_metric_for_filtering_and_ranking(self):
        first = origin_row("m-1", "素材一", "直播素材", 10, 20, 2)
        second = origin_row("m-2", "素材二", "直播素材", 20, 30, 3)
        first["metrics"]["video_play_finish_rate_for_roi2_v2"] = {"Value": "10"}
        second["metrics"]["video_play_finish_rate_for_roi2_v2"] = {"Value": "30"}
        query = dict(self.query)
        query["metricFilters"] = [
            {"field": "video_play_finish_rate_for_roi2_v2", "operator": "gte", "value": 20}
        ]
        query["sort"] = {"field": "video_play_finish_rate_for_roi2_v2", "direction": "desc"}

        result = execute_material_query([first, second], query, self.registry)

        self.assertEqual([item["materialId"] for item in result["materials"]], ["m-2"])
        self.assertEqual(result["materials"][0]["rankingMetric"], "video_play_finish_rate_for_roi2_v2")
