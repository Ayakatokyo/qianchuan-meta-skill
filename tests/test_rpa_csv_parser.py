import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rpa_csv_parser import RpaCsvError, parse_diagnosis_csv  # noqa: E402


class RpaCsvParserTests(unittest.TestCase):
    def write_csv(self, text):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "diagnosis.csv"
        path.write_text(text, encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return path

    def test_parses_unit_separator_and_structured_values(self):
        path = self.write_csv(
            "recordType\x01materialId\x01durationSec\x01metricValue\x01Metrics\n"
            "interaction\x01m-1\x013\x017\x01{\"CLICK_COUNT\": {\"Value\": \"7\"}}\n"
        )

        records = parse_diagnosis_csv(path)

        self.assertEqual(records[0]["durationSec"], 3.0)
        self.assertEqual(records[0]["metricValue"], 7.0)
        self.assertEqual(records[0]["Metrics"]["CLICK_COUNT"]["Value"], "7")

    def test_rejects_non_finite_metric_value(self):
        path = self.write_csv("recordType\x01metricValue\ninteraction\x01NaN\n")

        with self.assertRaisesRegex(RpaCsvError, "finite number"):
            parse_diagnosis_csv(path)

    def test_normalizes_show_count_from_structured_metrics(self):
        path = self.write_csv(
            "recordType\x01displayMetric\x01metricValue\x01Metrics\n"
            "crowd_analysis\x01SHOW_COUNT\x01\x01{\"audience\": {\"Value\": \"12\"}}\n"
        )
        records = parse_diagnosis_csv(path)
        self.assertEqual(records[0]["metricValue"], 12.0)
