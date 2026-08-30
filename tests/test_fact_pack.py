import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fact_pack import FactPackError, build_fact_pack, verify_fact_pack  # noqa: E402
from run_meta_skill import main as run_meta_skill_main  # noqa: E402


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_run(root, target_top_n, *, no_highlight=False, script_modes=None):
    root = Path(root)
    script_modes = script_modes or {}
    material_ids = [f"m-{index}" for index in range(1, target_top_n + 1)]
    materials = []
    for rank, material_id in enumerate(material_ids, start=1):
        materials.append({
            "rank": rank,
            "materialId": material_id,
            "materialName": f"素材{rank}",
            "materialType": "直播素材",
            "videoType": "短视频",
            "rankingMetric": "overall_cost",
            "sortValue": str(100 - rank),
            "metrics": {f"metric_{index:03d}": str(index) for index in range(102)},
        })
    _write_json(root / "top_materials.json", {
        "schemaVersion": 1,
        "collection": {"candidateTopN": target_top_n + 2, "targetTopN": target_top_n},
        "materials": materials,
    })
    _write_json(root / "query-receipt.json", {
        "query": {
            "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
            "sort": {"field": "overall_cost", "direction": "desc"},
            "dimensionFilters": [],
            "metricFilters": [],
        },
        "sourceRecordCount": 10,
        "candidateCount": target_top_n + 2,
        "passedCount": target_top_n,
        "returnedCount": target_top_n,
    })
    rpa_root = root / "rpa"
    _write_json(rpa_root / "batch-verification.json", {
        "status": "succeeded",
        "targetTopN": target_top_n,
        "succeededCount": target_top_n,
        "configurationFingerprint": "f" * 64,
    })
    _write_json(rpa_root / "top_materials_rpa_ready.json", {
        "schemaVersion": 1,
        "configurationFingerprint": "f" * 64,
        "targetTopN": target_top_n,
        "materialCount": target_top_n,
        "materials": [{"rank": rank, "materialId": material_id} for rank, material_id in enumerate(material_ids, start=1)],
    })
    for rank, material_id in enumerate(material_ids, start=1):
        record_dir = rpa_root / "diagnosis-records"
        records = [
            {
                "recordType": "material_info",
                "materialId": material_id,
                "materialName": f"素材{rank}",
                "materialType": "直播素材",
                "videoType": "短视频",
                "videoUrl": "https://signed.example/video.mp4?signature=secret",
                "thumbnailUrl": "https://signed.example/thumb.jpg?token=secret",
                "authorization": "Bearer secret",
                "localPath": "/Users/private/video.mp4",
            },
            {"recordType": "delivery_material_detail", "materialId": material_id, "statDate": "2026-08-01"},
            {"recordType": "crowd_analysis", "materialId": material_id, "dimensionKey": "age", "dimensionName": "年龄", "Dimensions": {"age": {"Value": "25-34", "ValueStr": "25-34岁"}}, "displayMetric": "showCount", "displayMetricName": "展现次数", "metricValue": "10", "metricPercentage": "20", "dimensionTotal": "50"},
        ]
        mode = script_modes.get(rank, "parsed")
        if mode == "parsed":
            records.append({"recordType": "script_analysis", "materialId": material_id, "scriptInfo": str({"formula": {"labels": ["开场"]}, "detail": {"labels": ["卖点"]}, "text": "逐字稿", "data": [{"label": "卖点"}]})})
        elif mode == "failed":
            records.append({"recordType": "script_analysis", "materialId": material_id, "scriptInfo": "not a python literal /Users/private/script.txt"})
        elif mode != "missing":
            raise ValueError(mode)
        if not no_highlight:
            records.append({"recordType": "interaction_series", "materialId": material_id, "interactionMetric": "CLICK_COUNT", "durationSec": 1, "metricValue": 2})
        record_path = record_dir / f"{material_id}.json"
        _write_json(record_path, records)
        frame_dir = rpa_root / "peak-frames" / material_id
        if no_highlight:
            _write_json(frame_dir / "summary.json", {"schemaVersion": 1, "status": "succeeded", "frames": [], "unavailableMetrics": [{"metric": "CLICK_COUNT", "reason": "missing_interaction_series"}]})
        else:
            frame_path = frame_dir / "01_1s.jpg"
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(b"\xff\xd8fixture\xff\xd9")
            _write_json(frame_dir / "summary.json", {"schemaVersion": 1, "status": "succeeded", "highlightPolicy": {"schemaVersion": 1}, "frames": [{"framePath": str(frame_path), "durationSec": 1, "selectionEvidence": [{"metric": "CLICK_COUNT"}]}], "unavailableMetrics": []})
    return material_ids


class FactPackTests(unittest.TestCase):
    def test_builds_top3_top6_and_arbitrary_n_from_successful_target(self):
        for target_top_n in (3, 6, 8):
            with self.subTest(target_top_n=target_top_n), tempfile.TemporaryDirectory() as temp_dir:
                run_dir = Path(temp_dir) / "run"
                make_run(run_dir, target_top_n)
                fact_dir = run_dir / "fact-pack"
                manifest = build_fact_pack(run_dir, fact_dir)
                leaderboard = json.loads((fact_dir / "material-leaderboard.json").read_text(encoding="utf-8"))

                self.assertEqual(manifest["targetTopN"], target_top_n)
                self.assertEqual(manifest["counts"]["materials"], target_top_n)
                self.assertEqual(len(leaderboard["materials"]), target_top_n)
                self.assertEqual(manifest["quality"]["status"], "built")
                self.assertEqual(verify_fact_pack(run_dir, fact_dir)["status"], "succeeded")

    def test_preserves_platform_dimensions_and_records_script_and_highlight_statuses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            make_run(run_dir, 3, no_highlight=True, script_modes={1: "missing", 2: "failed"})
            fact_dir = run_dir / "fact-pack"
            manifest = build_fact_pack(run_dir, fact_dir)
            leaderboard = json.loads((fact_dir / "material-leaderboard.json").read_text(encoding="utf-8"))
            scripts = json.loads((fact_dir / "scripts.json").read_text(encoding="utf-8"))["scripts"]
            highlights = json.loads((fact_dir / "highlight-frames.json").read_text(encoding="utf-8"))
            serialized = "\n".join(path.read_text(encoding="utf-8") for path in fact_dir.glob("*.json"))

            self.assertEqual(leaderboard["materials"][0]["platformDimensions"], {"materialType": "直播素材", "videoType": "短视频"})
            self.assertEqual(leaderboard["materials"][0]["materialType"], "直播素材")
            self.assertEqual(leaderboard["materials"][0]["videoType"], "短视频")
            self.assertEqual([item["parseStatus"] for item in scripts], ["missing", "failed", "parsed"])
            self.assertEqual(scripts[0]["error"]["code"], "script_missing")
            self.assertEqual(scripts[1]["error"]["code"], "script_parse_failed")
            self.assertEqual(highlights["status"], "no_highlight_frames")
            self.assertEqual(manifest["quality"]["status"], "built_with_warnings")
            self.assertEqual(
                [error["code"] for error in manifest["quality"]["errors"]],
                ["script_missing", "script_parse_failed", "no_highlight_frames"],
            )
            self.assertNotIn("signed.example", serialized)
            self.assertNotIn("/Users/", serialized)
            self.assertNotIn("authorization", serialized.lower())

    def test_does_not_build_query_only_run_and_reports_missing_rpa(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "query-only"
            _write_json(run_dir / "top_materials.json", {"collection": {"targetTopN": 3}, "materials": []})
            with self.assertRaisesRegex(FactPackError, "requires completed RPA"):
                build_fact_pack(run_dir)

    def test_build_and_verify_cli_commands_work_on_fixture_without_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            make_run(run_dir, 6)
            build_status = run_meta_skill_main(["build-fact-pack", "--run-dir", str(run_dir)])
            verify_status = run_meta_skill_main(["verify-fact-pack", "--run-dir", str(run_dir)])

        self.assertEqual(build_status, 0)
        self.assertEqual(verify_status, 0)

    def test_offline_verify_detects_source_hash_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            make_run(run_dir, 3)
            fact_dir = run_dir / "fact-pack"
            build_fact_pack(run_dir, fact_dir)
            records_path = run_dir / "rpa" / "diagnosis-records" / "m-1.json"
            records_path.write_text(records_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            result = verify_fact_pack(run_dir, fact_dir)

        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("hash or size mismatch" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
