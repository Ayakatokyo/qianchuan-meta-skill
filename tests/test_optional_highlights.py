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

    def test_source_rpa_succeeds_without_postprocessing_or_media_assets(self):
        postprocess_calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "input.csv"
            csv_path.write_text("data", encoding="utf-8")
            result = run_rpa_pipeline(
                root,
                {"materials": [{"materialId": "m-1", "materialName": "素材"}], "collection": {"candidateTopN": 1, "targetTopN": 1}},
                configuration_fingerprint="f",
                period={"startDate": "2026-08-01", "endDate": "2026-08-02"},
                options={"rpaConcurrency": 1},
                fetch_diagnosis=lambda entry, _paths: {"records": [{"materialId": entry["materialId"]}], "csvPath": csv_path},
                postprocess=lambda *_args: postprocess_calls.append(True),
                highlights_enabled=False,
            )

        self.assertTrue(result["succeeded"])
        self.assertEqual(postprocess_calls, [])
        self.assertEqual(result["entries"][0]["postprocessStatus"], "not_requested")
