import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from frame_extractor import extract_highlight_frames  # noqa: E402


def interaction(metric, second, value):
    return {
        "recordType": "interaction_series",
        "materialId": "m-1",
        "interactionMetric": metric,
        "durationSec": second,
        "metricValue": value,
    }


class FrameExtractorTests(unittest.TestCase):
    def test_extracts_deduplicated_frames_and_persists_selection_evidence(self):
        records = [
            interaction("CLICK_COUNT", 0, 0), interaction("CLICK_COUNT", 1, 1),
            interaction("CLICK_COUNT", 2, 8), interaction("CLICK_COUNT", 3, 1),
            interaction("CLICK_COUNT", 4, 0), interaction("SHARE_COUNT", 0, 0),
            interaction("SHARE_COUNT", 1, 1), interaction("SHARE_COUNT", 2, 4),
            interaction("SHARE_COUNT", 3, 1), interaction("SHARE_COUNT", 4, 0),
        ]
        policy = {
            "schemaVersion": 1,
            "includeZeroSecond": True,
            "policies": [
                {"metric": "CLICK_COUNT", "mode": "local_peaks", "maxFrames": 1},
                {"metric": "SHARE_COUNT", "mode": "global_maximum", "maxFrames": 1},
            ],
        }
        calls = []

        def fake_extract(_video, timestamp, destination):
            calls.append(timestamp)
            Path(destination).write_bytes(b"jpeg")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            summary = extract_highlight_frames(records, policy, "video.mp4", output_dir, extractor=fake_extract)
            saved = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(calls, [0.0, 2.0])
        self.assertEqual(len(summary["frames"]), 2)
        self.assertEqual(len(saved["frames"][1]["selectionEvidence"]), 2)
        self.assertTrue(saved["frames"][1]["framePath"].endswith(".jpg"))

