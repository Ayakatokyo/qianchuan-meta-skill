import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from resolve_advertiser_id import resolve_advertiser_id  # noqa: E402


class ResolveAdvertiserIdTests(unittest.TestCase):
    def setUp(self):
        self.rpa = {"success": True, "data": [{"shop_id": "rpa-7", "account": "12345"}]}
        self.api = [{"shop_id": "api-9", "advertiser_id": "12345"}]

    def test_resolves_numeric_account_with_distinct_authorization_ids(self):
        self.assertEqual(resolve_advertiser_id("api-9", "rpa-7", rpa_accounts=self.rpa,
                                               api_authorizations=self.api), "12345")

    def test_rejects_missing_or_duplicate_rpa_authorization(self):
        for rows in ([], self.rpa["data"] * 2):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "does not resolve to one"):
                resolve_advertiser_id("api-9", "rpa-7", rpa_accounts={"success": True, "data": rows},
                                      api_authorizations=self.api)

    def test_rejects_non_numeric_account_before_api_query(self):
        with patch("resolve_advertiser_id.list_authorized_advertisers") as api_query:
            with self.assertRaisesRegex(ValueError, "not a numeric"):
                resolve_advertiser_id("api-9", "rpa-7", rpa_accounts={"success": True, "data": [
                    {"shop_id": "rpa-7", "account": "user@example.com"}]})
            api_query.assert_not_called()

    def test_rejects_missing_or_duplicate_api_authorization(self):
        for rows in ([], self.api * 2):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "not uniquely authorized"):
                resolve_advertiser_id("api-9", "rpa-7", rpa_accounts=self.rpa, api_authorizations=rows)


if __name__ == "__main__":
    unittest.main()
