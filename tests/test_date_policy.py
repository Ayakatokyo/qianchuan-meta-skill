import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from date_policy import validate_period  # noqa: E402
from field_registry import load_field_registry  # noqa: E402
from material_query import MaterialQueryError, parse_query_config  # noqa: E402
from qianchuan_report_client import ReportClientError, _business_params  # noqa: E402
from rpa_adapter import RpaAdapterError, build_diagnosis_params  # noqa: E402


class DatePolicyTests(unittest.TestCase):
    def test_t_plus_2_boundary_and_invalid_dates(self):
        today = date(2026, 9, 25)
        self.assertEqual(validate_period("2026-09-22", "2026-09-23", today=today)["endDate"], "2026-09-23")
        for end in ("2026-09-24", "2026-09-25", "2026-09-26"):
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, "today-2"):
                validate_period("2026-09-22", end, today=today)
        with self.assertRaisesRegex(ValueError, "after endDate"):
            validate_period("2026-09-23", "2026-09-22", today=today)
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            validate_period("2026-09-01", "2026-09-31", today=today)

    def test_live_entry_points_reject_yesterday_before_request(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        start, end = (today - timedelta(days=3)).isoformat(), (today - timedelta(days=1)).isoformat()
        registry = load_field_registry(ROOT / "config" / "asset-field-registry-v1.json")
        with self.assertRaisesRegex(MaterialQueryError, "today-2"):
            parse_query_config({
                "schemaVersion": 1, "shopId": "shop-1", "advertiserId": "123",
                "period": {"startDate": start, "endDate": end}, "topN": 1,
            }, registry)
        with self.assertRaisesRegex(ReportClientError, "today-2"):
            _business_params("123", start, end)
        with self.assertRaisesRegex(RpaAdapterError, "today-2"):
            build_diagnosis_params("123", start, end)


if __name__ == "__main__":
    unittest.main()
