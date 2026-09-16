import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from highlight_overview import HighlightOverviewError, build_highlight_overview  # noqa: E402


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


def _make_run(root, *, mutate=None):
    run_dir = root / "run-highlight"
    fact_dir = run_dir / "fact-pack"
    artifact = run_dir / "rpa" / "peak-frames" / "m-1" / "01_0s.jpg"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"fixture-jpeg")

    leaderboard = {
        "schemaVersion": 1,
        "materials": [{"materialId": "m-1", "materialName": "素材标题", "rank": 1}],
        "sourceRefs": [],
    }
    frame = {
        "materialId": "m-1",
        "durationSec": 0.0,
        "frameSequence": 1,
        "artifact": _ref(run_dir, artifact),
        "selectionEvidence": [{
            "metric": "SHARE_COUNT",
            "metricValue": 0.0,
            "mode": "global_top_values",
            "policyRank": 3,
            "selectionRank": 2,
        }],
    }
    highlights = {"schemaVersion": 1, "status": "available", "highlightFrames": [frame], "sourceRefs": []}
    _write_json(fact_dir / "material-leaderboard.json", leaderboard)
    _write_json(fact_dir / "highlight-frames.json", highlights)
    source_refs = [_ref(run_dir, artifact)]
    manifest = {
        "schemaVersion": 1,
        "materialIds": ["m-1"],
        "counts": {"materials": 1, "highlightFrames": 1},
        "files": ["manifest.json", "material-leaderboard.json", "highlight-frames.json"],
        "sourceFiles": source_refs,
        "quality": {"status": "built", "categoryStatuses": {"highlightFrames": "available", "materialLeaderboard": "available"}},
    }
    _write_json(fact_dir / "manifest.json", manifest)
    if mutate:
        mutate(run_dir, manifest, highlights["highlightFrames"][0])
        _write_json(fact_dir / "highlight-frames.json", highlights)
        _write_json(fact_dir / "manifest.json", manifest)
    return run_dir


class HighlightOverviewTests(unittest.TestCase):
    def test_projects_verified_frame_with_cover_description_metric_label_and_stable_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model = build_highlight_overview(_make_run(Path(temp_dir)))

        self.assertEqual(model["status"], "available")
        self.assertEqual(model["source"]["recordCount"], 1)
        self.assertEqual(len(model["cards"]), 1)
        card = model["cards"][0]
        self.assertEqual(card["id"], "m-1:1")
        self.assertEqual(card["materialId"], "m-1")
        self.assertEqual(card["materialRank"], 1)
        self.assertEqual(card["second"], 0.0)
        self.assertEqual(card["role"], "视频开场画面")
        self.assertEqual(card["description"], "封面画面")
        self.assertEqual(card["selectionConditions"], ["封面画面", "转发次数数值 Top2"])
        self.assertEqual(card["materialTitle"], "素材标题")
        self.assertEqual(card["selectionEvidence"], [{
            "metric": "SHARE_COUNT", "metricLabel": "整体转发次数", "value": 0.0, "mode": "global_top_values", "rank": 2, "policyRank": 3,
        }])

    def test_projects_a_cover_frame_without_treating_its_sentinel_as_metric_evidence(self):
        def mutate(_run_dir, _manifest, frame):
            frame["selectionEvidence"] = [{
                "metric": "ZERO_SECOND",
                "mode": "zero_second",
                "policyRank": 0,
                "durationSec": 0.0,
                "metricValue": None,
            }]

        with tempfile.TemporaryDirectory() as temp_dir:
            model = build_highlight_overview(_make_run(Path(temp_dir), mutate=mutate))

        card = model["cards"][0]
        self.assertEqual(card["description"], "封面画面")
        self.assertEqual(card["selectionConditions"], ["封面画面"])
        self.assertEqual(card["selectionEvidence"], [])

    def test_describes_nonzero_peak_using_persisted_metric_and_rank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            frames_path = run_dir / "fact-pack" / "highlight-frames.json"
            payload = json.loads(frames_path.read_text(encoding="utf-8"))
            payload["highlightFrames"][0].update({"durationSec": 2.0, "frameSequence": 3})
            payload["highlightFrames"][0]["selectionEvidence"][0].update({
                "metric": "CLICK_COUNT", "metricValue": 23.0, "mode": "local_peaks", "selectionRank": 1,
            })
            _write_json(frames_path, payload)
            model = build_highlight_overview(run_dir)

        card = model["cards"][0]
        self.assertEqual(model["cards"][0]["description"], "点击次数波峰 Top1")
        self.assertEqual(card["selectionConditions"], ["点击次数波峰 Top1"])
        self.assertEqual(card["artifact"]["path"], "rpa/peak-frames/m-1/01_0s.jpg")
        self.assertNotIn("diagnosis", json.dumps(model, ensure_ascii=False))

    def test_describes_global_metric_top_separately_from_local_peak(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            frames_path = run_dir / "fact-pack" / "highlight-frames.json"
            payload = json.loads(frames_path.read_text(encoding="utf-8"))
            payload["highlightFrames"][0].update({"durationSec": 4.0, "frameSequence": 5})
            payload["highlightFrames"][0]["selectionEvidence"][0].update({
                "metric": "SHARE_COUNT", "metricValue": 23.0, "mode": "global_top_values", "selectionRank": 2,
            })
            _write_json(frames_path, payload)
            model = build_highlight_overview(run_dir)

        self.assertEqual(model["cards"][0]["description"], "转发次数数值 Top2")
        self.assertEqual(model["cards"][0]["selectionConditions"], ["转发次数数值 Top2"])

    def test_lists_all_selection_conditions_when_one_frame_matches_multiple_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = _make_run(Path(temp_dir))
            frames_path = run_dir / "fact-pack" / "highlight-frames.json"
            payload = json.loads(frames_path.read_text(encoding="utf-8"))
            payload["highlightFrames"][0].update({"durationSec": 7.0, "frameSequence": 8})
            payload["highlightFrames"][0]["selectionEvidence"].append({
                "durationSec": 7.0, "metric": "CLICK_COUNT", "metricValue": 13.0,
                "mode": "local_peaks", "policyRank": 1, "selectionRank": 2,
            })
            _write_json(frames_path, payload)
            model = build_highlight_overview(run_dir)

        self.assertEqual(model["cards"][0]["selectionConditions"], [
            "转发次数数值 Top2", "点击次数波峰 Top2",
        ])

    def test_rejects_changed_artifact_size_or_hash(self):
        def mutate(run_dir, _manifest, _frame):
            (run_dir / "rpa" / "peak-frames" / "m-1" / "01_0s.jpg").write_bytes(b"changed")

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(HighlightOverviewError, "hash or size"):
                build_highlight_overview(_make_run(Path(temp_dir), mutate=mutate))

    def test_rejects_unsafe_artifact_path_and_missing_material_title(self):
        def mutate(_run_dir, _manifest, frame):
            frame["artifact"]["path"] = "../outside.jpg"

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(HighlightOverviewError, "relative|escapes"):
                build_highlight_overview(_make_run(Path(temp_dir), mutate=mutate))

        def remove_title(_run_dir, _manifest, _frame):
            leaderboard = _run_dir / "fact-pack" / "material-leaderboard.json"
            payload = json.loads(leaderboard.read_text(encoding="utf-8"))
            payload["materials"][0]["materialName"] = ""
            _write_json(leaderboard, payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(HighlightOverviewError, "material title"):
                build_highlight_overview(_make_run(Path(temp_dir), mutate=remove_title))

    def test_rejects_malformed_selection_evidence_without_projecting_a_partial_card(self):
        def mutate(_run_dir, _manifest, frame):
            frame["selectionEvidence"] = [{"metric": "CLICK_COUNT", "mode": "local_peaks"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(HighlightOverviewError, "selection evidence"):
                build_highlight_overview(_make_run(Path(temp_dir), mutate=mutate))


if __name__ == "__main__":
    unittest.main()
