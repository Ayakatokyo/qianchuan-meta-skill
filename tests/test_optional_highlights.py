import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from config_specs import resolve_highlight_request
from rpa_pipeline import run_rpa_pipeline


class OptionalHighlightTests(unittest.TestCase):
    def test_omitted_highlight_request_defaults_to_off_without_a_spec(self):
        self.assertEqual(resolve_highlight_request(None), {
            "enabled": False,
            "spec": None,
            "requestSource": "default_off",
        })

    def test_disabled_highlights_reject_a_rule(self):
        with self.assertRaisesRegex(ValueError, "highlight_request_conflict"):
            resolve_highlight_request({"schemaVersion": 1, "includeZeroSecond": True, "metrics": {
                "CLICK_COUNT": {"enabled": False}, "LOSE_COUNT": {"enabled": False},
                "LIKE_COUNT": {"enabled": False}, "COMMENT_COUNT": {"enabled": False},
                "SHARE_COUNT": {"enabled": False}, "NEW_FOLLOWER_COUNT": {"enabled": False},
            }}, enabled=False)

    def test_disabled_highlights_still_retain_a_video_asset(self):
        postprocess_calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "input.csv"
            csv_path.write_text("materialId\x01recordType\nm-1\x01material_info\n", encoding="utf-8")

            def postprocess(entry, _records, paths):
                video_path = paths["videos"] / (entry["materialId"] + ".mp4")
                video_path.parent.mkdir(parents=True, exist_ok=True)
                video_path.write_bytes(b"fixture-video")
                postprocess_calls.append(True)
                return {"frames": [], "videoPath": str(video_path)}

            result = run_rpa_pipeline(
                root,
                {"materials": [{"materialId": "m-1", "materialName": "素材"}], "collection": {"candidateTopN": 1, "targetTopN": 1}},
                configuration_fingerprint="f",
                period={"startDate": "2026-08-01", "endDate": "2026-08-02"},
                options={"rpaConcurrency": 1},
                fetch_diagnosis=lambda entry, _paths: {"records": [{"materialId": entry["materialId"]}], "csvPath": csv_path},
                postprocess=postprocess,
                highlights_enabled=False,
            )
            self.assertTrue((root / "rpa" / "videos" / "m-1.mp4").is_file())

        self.assertTrue(result["succeeded"])
        self.assertEqual(postprocess_calls, [True])
        self.assertEqual(result["entries"][0]["postprocessStatus"], "succeeded")
