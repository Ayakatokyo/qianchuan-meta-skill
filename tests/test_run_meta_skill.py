import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_meta_skill  # noqa: E402
from field_registry import load_field_registry  # noqa: E402


def origin_row(material_id, cost, amount):
    return {
        "dimensions": {
            "material_id": {"Value": material_id},
            "roi2_material_video_name": {"ValueStr": "素材" + material_id},
            "roi2_material_type_v3": {"ValueStr": "直播素材"},
            "roi2_material_video_type": {"ValueStr": "短视频"},
        },
        "metrics": {
            "stat_cost_for_overall_roi2": {"Value": str(cost)},
            "total_order_settle_amount_for_roi2_1h": {"Value": str(amount)},
        },
    }


class MetaSkillCliTests(unittest.TestCase):
    def test_connector_order_by_maps_atomic_and_derived_rankings(self):
        registry = load_field_registry(run_meta_skill.REGISTRY_PATH)

        self.assertEqual(
            run_meta_skill._connector_order_by(
                {"sort": {"field": "net_settlement_amount", "direction": "asc"}}, registry
            ),
            [{"field": "total_order_settle_amount_for_roi2_1h", "type": 1}],
        )
        self.assertEqual(
            run_meta_skill._connector_order_by(
                {"sort": {"field": "net_settlement_roi", "direction": "desc"}}, registry
            ),
            [{"field": "stat_cost_for_overall_roi2", "type": 2}],
        )

    def write_config(self, directory):
        path = directory / "config.json"
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "query": {
                        "schemaVersion": 1,
                        "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
                        "shopId": "shop-1",
                        "advertiserId": "1770096415226951",
                        "topN": 1,
                        "dimensionFilters": [],
                        "metricFilters": [],
                        "sort": {"field": "overall_cost", "direction": "desc"},
                    },
                    "highlightPolicy": {
                        "schemaVersion": 1,
                        "includeZeroSecond": True,
                        "policies": [{"metric": "CLICK_COUNT", "mode": "local_peaks", "maxFrames": 3}],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def write_specs(self, directory):
        query_path = directory / "query-spec.json"
        highlight_path = directory / "highlight-spec.json"
        query_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "source": "qianchuan_direct_material_report",
                    "scope": {"shopId": "shop-1", "advertiserId": "1770096415226951"},
                    "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
                    "filters": {"dimensionFilters": [], "metricFilters": []},
                    "ranking": {"field": "overall_cost", "direction": "desc"},
                    "collection": {"targetTopN": 1},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        highlight_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "includeZeroSecond": True,
                    "metrics": {
                        "CLICK_COUNT": {"enabled": True, "strategy": "local_peaks", "maxFrames": 3},
                        "LOSE_COUNT": {"enabled": False},
                        "LIKE_COUNT": {"enabled": False},
                        "COMMENT_COUNT": {"enabled": False},
                        "SHARE_COUNT": {"enabled": False},
                        "NEW_FOLLOWER_COUNT": {"enabled": False},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return query_path, highlight_path

    def test_query_accepts_independent_specs_and_persists_resolved_audit_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            output_dir = root / "run"
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]) as fetch:
                status = run_meta_skill.main(
                    [
                        "query", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path),
                        "--output-dir", str(output_dir),
                    ]
                )

            receipt = json.loads((output_dir / "query-receipt.json").read_text(encoding="utf-8"))
            fingerprint = json.loads((output_dir / "configuration-fingerprint.json").read_text(encoding="utf-8"))
            resolved_query_exists = (output_dir / "resolved-query-spec.json").is_file()
            resolved_highlight_exists = (output_dir / "resolved-highlight-spec.json").is_file()

        self.assertEqual(status, 0)
        self.assertEqual(fetch.call_args.args[:4], ("shop-1", "1770096415226951", "2026-08-01", "2026-08-07"))
        self.assertEqual(
            fetch.call_args.kwargs["order_by"], [{"field": "stat_cost_for_overall_roi2", "type": 2}]
        )
        self.assertTrue(resolved_query_exists)
        self.assertTrue(resolved_highlight_exists)
        self.assertEqual(receipt["configurationFingerprint"], fingerprint["configurationFingerprint"])

    def test_query_uses_candidate_pool_size_and_persists_both_collection_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            query = json.loads(query_path.read_text(encoding="utf-8"))
            query["collection"] = {"candidateTopN": 2, "targetTopN": 1}
            query_path.write_text(json.dumps(query), encoding="utf-8")
            output_dir = root / "run"
            with patch.object(
                run_meta_skill,
                "fetch_material_report",
                return_value=[origin_row("m-1", 10, 2), origin_row("m-2", 9, 1)],
            ):
                status = run_meta_skill.main(
                    ["query", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path), "--output-dir", str(output_dir)]
                )
            payload = json.loads((output_dir / "top_materials.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(payload["collection"], {"candidateTopN": 2, "targetTopN": 1})
        self.assertEqual([item["materialId"] for item in payload["materials"]], ["m-1", "m-2"])

    def test_query_collects_direct_report_records_and_stops_after_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["query"]["shopId"] = "shop-1"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            output_dir = root / "run"
            records = [origin_row("m-1", 2, 2), origin_row("m-2", 10, 1)]
            with patch.object(run_meta_skill, "fetch_material_report", return_value=records) as fetch:
                status = run_meta_skill.main(
                    ["query", "--config", str(config_path), "--output-dir", str(output_dir)]
                )

            top_materials = json.loads((output_dir / "top_materials.json").read_text(encoding="utf-8"))
            receipt = json.loads((output_dir / "query-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(fetch.call_args.args[:4], ("shop-1", "1770096415226951", "2026-08-01", "2026-08-07"))
        self.assertEqual(
            fetch.call_args.kwargs["order_by"], [{"field": "stat_cost_for_overall_roi2", "type": 2}]
        )
        self.assertEqual(fetch.call_args.kwargs["output_dir"], output_dir / "source-data")
        self.assertEqual([item["materialId"] for item in top_materials["materials"]], ["m-2"])
        self.assertEqual(receipt["source"], {"type": "direct_material_report"})
        self.assertFalse((output_dir / "diagnosis-records").exists())

    def test_validate_checks_config_without_collecting_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            with patch.object(run_meta_skill, "fetch_material_report") as collect:
                status = run_meta_skill.main(["validate", "--config", str(config_path)])

        self.assertEqual(status, 0)
        collect.assert_not_called()

    def test_validate_serializes_numeric_metric_filters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["query"]["metricFilters"] = [
                {"field": "net_settlement_roi", "operator": "gte", "value": 1.2}
            ]
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("builtins.print") as output:
                status = run_meta_skill.main(["validate", "--config", str(config_path)])

        self.assertEqual(status, 0)
        normalized = json.loads(output.call_args.args[0])
        self.assertEqual(normalized["query"]["metricFilters"][0]["value"], "1.2")

    def test_query_writes_candidates_and_receipt_then_stops(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            output_dir = root / "run"
            with patch.object(
                run_meta_skill,
                "fetch_material_report",
                return_value=[origin_row("m-1", 2, 2), origin_row("m-2", 10, 1)],
            ) as collect:
                status = run_meta_skill.main(
                    ["query", "--config", str(config_path), "--output-dir", str(output_dir)]
                )

            top_materials = json.loads((output_dir / "top_materials.json").read_text(encoding="utf-8"))
            receipt = json.loads((output_dir / "query-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        collect.assert_called_once()
        self.assertEqual([item["materialId"] for item in top_materials["materials"]], ["m-2"])
        self.assertEqual(receipt["returnedCount"], 1)
        self.assertFalse((output_dir / "diagnosis-records").exists())

    def test_select_frames_uses_configured_policy_without_collecting_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            records_path = root / "records.json"
            records_path.write_text("[]", encoding="utf-8")
            output_dir = root / "frames"
            with patch.object(run_meta_skill, "fetch_material_report") as collect, patch.object(
                run_meta_skill,
                "extract_highlight_frames",
                return_value={"status": "succeeded", "frames": []},
            ) as extract:
                status = run_meta_skill.main(
                    [
                        "select-frames", "--config", str(config_path), "--records-json", str(records_path),
                        "--video", "video.mp4", "--output-dir", str(output_dir),
                    ]
                )

        self.assertEqual(status, 0)
        collect.assert_not_called()
        self.assertEqual(extract.call_args.args[1]["policies"][0]["metric"], "CLICK_COUNT")

    def test_select_frames_accepts_an_independent_highlight_spec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _query_path, highlight_path = self.write_specs(root)
            records_path = root / "records.json"
            records_path.write_text("[]", encoding="utf-8")
            with patch.object(run_meta_skill, "fetch_material_report") as collect, patch.object(
                run_meta_skill, "extract_highlight_frames", return_value={"status": "succeeded", "frames": []}
            ) as extract:
                status = run_meta_skill.main(
                    [
                        "select-frames", "--highlight-spec", str(highlight_path),
                        "--records-json", str(records_path), "--video", "video.mp4", "--output-dir", str(root / "frames"),
                    ]
                )

        self.assertEqual(status, 0)
        collect.assert_not_called()
        self.assertEqual(extract.call_args.args[1]["policies"][0]["metric"], "CLICK_COUNT")

    def test_query_has_no_asset_table_dependency(self):
        self.assertNotIn(
            "asset_table_client",
            (ROOT / "scripts" / "run_meta_skill.py").read_text(encoding="utf-8"),
        )
        self.assertFalse((ROOT / "scripts" / "asset_table_client.py").exists())

    def test_run_queries_candidates_then_invokes_rpa_without_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            output_dir = root / "run"
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]) as collect, patch.object(
                run_meta_skill, "list_rpa_accounts", return_value={"accountId": "rpa-1", "platform": "RPA_JULIANG_QC_ACCOUNT_PASSWORD"}
            ) as accounts, patch.object(run_meta_skill, "run_rpa_pipeline", return_value={"succeeded": True, "readyPath": str(output_dir / "rpa" / "top_materials_rpa_ready.json")}) as pipeline:
                status = run_meta_skill.main(
                    ["run", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path), "--output-dir", str(output_dir)]
                )

            options = json.loads((output_dir / "resolved-rpa-options.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        collect.assert_called_once()
        accounts.assert_called_once()
        pipeline.assert_called_once()
        self.assertEqual(options["rpaConcurrency"], 3)
        self.assertNotIn("authorization", options)

    def test_run_returns_nonzero_for_partial_rpa_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]), patch.object(
                run_meta_skill, "list_rpa_accounts", return_value={"accountId": "rpa-1", "platform": "RPA_JULIANG_QC_ACCOUNT_PASSWORD"}
            ), patch.object(run_meta_skill, "run_rpa_pipeline", return_value={"succeeded": False, "verificationPath": "batch-verification.json"}):
                status = run_meta_skill.main(
                    ["run", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path), "--output-dir", str(root / "run")]
                )

        self.assertEqual(status, 2)
