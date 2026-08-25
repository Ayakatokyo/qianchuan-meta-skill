import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_meta_skill  # noqa: E402


def origin_row(material_id, cost, amount):
    return json.dumps(
        {
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
        },
        ensure_ascii=False,
    )


class MetaSkillCliTests(unittest.TestCase):
    def write_config(self, directory):
        path = directory / "config.json"
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "query": {
                        "schemaVersion": 1,
                        "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
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

    def test_validate_checks_config_without_collecting_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            with patch.object(run_meta_skill, "collect_origin_rows") as collect:
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
                "collect_origin_rows",
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
            with patch.object(run_meta_skill, "collect_origin_rows") as collect, patch.object(
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
