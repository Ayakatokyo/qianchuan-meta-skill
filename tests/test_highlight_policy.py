import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from highlight_policy import HighlightPolicyError, select_highlight_frames  # noqa: E402


def interaction(metric, second, value):
    return {
        "recordType": "interaction_series",
        "materialId": "m-1",
        "interactionMetric": metric,
        "durationSec": second,
        "metricValue": value,
    }


class HighlightPolicyTests(unittest.TestCase):
    def test_merges_same_timestamp_and_keeps_all_metric_evidence(self):
        records = [
            interaction("CLICK_COUNT", 0, 0), interaction("CLICK_COUNT", 1, 1),
            interaction("CLICK_COUNT", 2, 8), interaction("CLICK_COUNT", 3, 1),
            interaction("CLICK_COUNT", 4, 0), interaction("SHARE_COUNT", 0, 0),
            interaction("SHARE_COUNT", 1, 1), interaction("SHARE_COUNT", 2, 4),
            interaction("SHARE_COUNT", 3, 1), interaction("SHARE_COUNT", 4, 0),
        ]
        selection = select_highlight_frames(
            records,
            {
                "schemaVersion": 1,
                "includeZeroSecond": True,
                "policies": [
                    {"metric": "CLICK_COUNT", "mode": "local_peaks", "maxFrames": 1},
                    {"metric": "SHARE_COUNT", "mode": "global_maximum", "maxFrames": 1},
                ],
            },
        )

        frame = next(item for item in selection["frames"] if item["durationSec"] == 2)
        self.assertEqual(
            {item["metric"] for item in frame["selectionEvidence"]},
            {"CLICK_COUNT", "SHARE_COUNT"},
        )
        self.assertEqual(len(selection["frames"]), 2)

    def test_global_maximum_uses_earliest_timestamp_for_ties(self):
        selection = select_highlight_frames(
            [interaction("LIKE_COUNT", 1, 5), interaction("LIKE_COUNT", 3, 9), interaction("LIKE_COUNT", 5, 9)],
            {"schemaVersion": 1, "includeZeroSecond": False, "policies": [{"metric": "LIKE_COUNT", "mode": "global_maximum", "maxFrames": 1}]},
        )

        self.assertEqual(selection["frames"][0]["durationSec"], 3)

    def test_rejects_unknown_metric_and_invalid_global_maximum_count(self):
        with self.assertRaisesRegex(HighlightPolicyError, "unsupported"):
            select_highlight_frames([], {"schemaVersion": 1, "policies": [{"metric": "PLAY_COUNT", "mode": "local_peaks", "maxFrames": 1}]})
        with self.assertRaisesRegex(HighlightPolicyError, "maxFrames"):
            select_highlight_frames([], {"schemaVersion": 1, "policies": [{"metric": "LIKE_COUNT", "mode": "global_maximum", "maxFrames": 2}]})
