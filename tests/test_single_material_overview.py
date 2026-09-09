import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from single_material_overview import SingleMaterialOverviewError, build_single_material_overview  # noqa: E402


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _ref(run_dir, path):
    data = path.read_bytes()
    return {
        "path": path.relative_to(run_dir).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sizeBytes": len(data),
    }


def _make_run(root, *, tamper=None, include_video=False):
    run_dir = root / "run-single-material"
    diagnosis = run_dir / "rpa" / "diagnosis-records" / "m-1.json"
    diagnosis.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"recordType": "interaction_series", "materialId": "m-1", "durationSec": 0, "interactionMetric": "CLICK_COUNT", "interactionMetricName": "整体点击次数", "metricValue": 3},
        {"recordType": "interaction_series", "materialId": "m-1", "durationSec": 1, "interactionMetric": "CLICK_COUNT", "interactionMetricName": "整体点击次数", "metricValue": 4},
    ]
    diagnosis.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    diagnosis_ref = _ref(run_dir, diagnosis)
    artifact = run_dir / "rpa" / "peak-frames" / "m-1" / "01_0s.jpg"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"fixture-image")
    artifact_ref = _ref(run_dir, artifact)

    leaderboard = {
        "schemaVersion": 1,
        "materials": [{
            "rank": 1,
            "materialId": "m-1",
            "materialName": "素材一",
            "metrics": [{"key": "stat_cost_for_overall_roi2", "value": "12.50"}],
            "sourceRef": {"path": "top_materials.json", "sha256": "0" * 64, "sizeBytes": 0},
        }],
        "sourceRefs": [],
    }
    top_materials = run_dir / "top_materials.json"
    top_materials.write_text('{"materials":[{"materialId":"m-1"}]}', encoding="utf-8")
    leaderboard["materials"][0]["sourceRef"] = _ref(run_dir, top_materials)
    audience = {
        "schemaVersion": 1,
        "status": "available",
        "audience": [{
            "materialId": "m-1",
            "dimensionKey": "gender",
            "dimensionName": "性别",
            "Dimensions": {"gender": {"ValueStr": "女"}},
            "audienceValue": {"ValueStr": "女"},
            "displayMetric": "SHOW_COUNT",
            "displayMetricName": "整体展现次数",
            "metricValue": 9,
            "Metrics": {"live_show_count_for_roi2_v2": {"Value": 9}},
            "sourceRef": diagnosis_ref,
        }],
        "sourceRefs": [diagnosis_ref],
    }
    scripts = {
        "schemaVersion": 1,
        "status": "available",
        "scripts": [{
            "materialId": "m-1",
            "parseStatus": "parsed",
            "completeness": {"formulaLabels": True, "transcript": True},
            "formula": {"labels": [{"level_1_label": 2, "level_2_labels": [6]}]},
            "data": [{"level_1_label": {"label": "商品卖点"}, "level_2_labels": [{"label": "适用场景"}]}],
            "text": "一个明确的脚本片段",
            "detail": {"6": {"label": "适用场景", "list": ["一个明确的脚本片段"]}},
            "sourceRef": diagnosis_ref,
        }],
        "sourceRefs": [diagnosis_ref],
    }
    highlights = {
        "schemaVersion": 1,
        "status": "available",
        "highlightFrames": [{
            "materialId": "m-1",
            "durationSec": 0,
            "frameSequence": 1,
            "artifact": artifact_ref,
            "selectionEvidence": [{"metric": "CLICK_COUNT", "metricValue": 3, "mode": "local_peaks", "policyRank": 1, "selectionRank": 1}],
            "interactionSeries": {"recordCount": 2, "sourceRef": diagnosis_ref},
        }],
        "sourceRefs": [diagnosis_ref],
    }
    fact_dir = run_dir / "fact-pack"
    for name, payload in (
        ("material-leaderboard.json", leaderboard),
        ("audience.json", audience),
        ("scripts.json", scripts),
        ("highlight-frames.json", highlights),
    ):
        _write_json(fact_dir / name, payload)
    manifest = {
        "schemaVersion": 1,
        "materialIds": ["m-1"],
        "counts": {"materials": 1, "audienceFacts": 1, "scriptFacts": 1, "highlightFrames": 1},
        "files": ["manifest.json", "material-leaderboard.json", "audience.json", "scripts.json", "highlight-frames.json"],
        "sourceFiles": [diagnosis_ref, _ref(run_dir, top_materials), artifact_ref],
        "quality": {"categoryStatuses": {"materialLeaderboard": "available", "audience": "available", "scripts": "available", "highlightFrames": "available"}},
    }
    _write_json(fact_dir / "manifest.json", manifest)
    if include_video:
        video = run_dir / "rpa" / "videos" / "m-1.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"fixture-video")
        manifest["sourceFiles"].append(_ref(run_dir, video))
        manifest["videoArtifacts"] = [{"materialId": "m-1", "path": _ref(run_dir, video)["path"], "sha256": _ref(run_dir, video)["sha256"], "sizeBytes": video.stat().st_size}]
        _write_json(fact_dir / "manifest.json", manifest)
    if tamper:
        tamper(run_dir, manifest)
    return run_dir


class SingleMaterialOverviewTests(unittest.TestCase):
    def test_projects_only_manifest_detail_material_and_fact_scoped_modules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model = build_single_material_overview(_make_run(Path(temp_dir)))

        self.assertEqual(model["status"], "available")
        self.assertEqual(model["materialScope"], {"count": 1, "materialIds": ["m-1"]})
        self.assertEqual([item["materialId"] for item in model["materials"]], ["m-1"])
        material = model["materials"][0]
        self.assertEqual(material["interactions"]["metrics"][0]["points"][1]["value"], 4)
        self.assertEqual(material["script"]["formula"][0]["primary"], "商品卖点")
        self.assertEqual(material["audience"]["metrics"][0]["id"], "SHOW_COUNT")
        self.assertEqual(material["audience"]["dimensions"]["gender"]["bucketsByMetric"]["SHOW_COUNT"][0]["value"], 9)
        self.assertEqual(material["highlights"]["video"]["status"], "unavailable")

    def test_rejects_changed_declared_source_hash(self):
        def tamper(run_dir, _manifest):
            path = run_dir / "rpa" / "diagnosis-records" / "m-1.json"
            path.write_text("changed", encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(SingleMaterialOverviewError, "hash or size"):
                build_single_material_overview(_make_run(Path(temp_dir), tamper=tamper))

    def test_rejects_fact_record_associated_with_non_detail_material(self):
        def tamper(run_dir, _manifest):
            path = run_dir / "fact-pack" / "audience.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["audience"][0]["materialId"] = "outside"
            _write_json(path, payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(SingleMaterialOverviewError, "outside manifest scope"):
                build_single_material_overview(_make_run(Path(temp_dir), tamper=tamper))

    def test_keeps_original_values_for_all_six_interaction_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            diagnosis = run_dir / "rpa" / "diagnosis-records" / "m-1.json"
            records = json.loads(diagnosis.read_text(encoding="utf-8"))
            aliases = [("CLICK_COUNT", "整体点击次数"), ("LOSE_COUNT", "整体流失数"), ("LIKE_COUNT", "整体点赞次数"), ("COMMENT_COUNT", "整体评论次数"), ("SHARE_COUNT", "整体转发次数"), ("FOLLOW_COUNT", "整体新增粉丝数")]
            records.extend({"recordType": "interaction_series", "materialId": "m-1", "durationSec": index + 2, "interactionMetric": metric, "interactionMetricName": label, "metricValue": index + 0.5} for index, (metric, label) in enumerate(aliases[1:], 1))
            diagnosis.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            # Update every fact-pack reference after the intentional fixture rewrite.
            ref = _ref(run_dir, diagnosis)
            for name in ("audience.json", "scripts.json", "highlight-frames.json"):
                path = run_dir / "fact-pack" / name
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["sourceRefs"] = [ref]
                collection = payload.get("audience") or payload.get("scripts") or payload.get("highlightFrames")
                collection[0]["sourceRef"] = ref
                if name == "highlight-frames.json":
                    collection[0]["interactionSeries"]["sourceRef"] = ref
                _write_json(path, payload)
            manifest_path = run_dir / "fact-pack" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["sourceFiles"][0] = ref
            _write_json(manifest_path, manifest)
            model = build_single_material_overview(run_dir)

        metrics = {item["id"]: item for item in model["materials"][0]["interactions"]["metrics"]}
        self.assertEqual(set(metrics), {"CLICK_COUNT", "LOSE_COUNT", "LIKE_COUNT", "COMMENT_COUNT", "SHARE_COUNT", "FOLLOW_COUNT"})
        self.assertEqual(metrics["FOLLOW_COUNT"]["points"][0]["value"], 5.5)

    def test_missing_dimension_is_a_local_gap_and_video_artifact_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            path = run_dir / "fact-pack" / "audience.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["audience"] = []
            _write_json(path, payload)
            model = build_single_material_overview(run_dir)

        material = model["materials"][0]
        self.assertEqual(material["audience"]["status"], "unavailable")
        self.assertEqual(material["highlights"]["video"]["message"], "本次详情数据未保存可播放视频")
        self.assertNotIn("http", json.dumps(model, ensure_ascii=False).lower())


if __name__ == "__main__":
    unittest.main()
