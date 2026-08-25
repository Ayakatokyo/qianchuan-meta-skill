import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rpa_adapter  # noqa: E402


class FakeResponse:
    def __init__(self, payload, *, chunks=(), headers=None):
        self.payload = payload
        self.chunks = chunks
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        return iter(self.chunks)

    def close(self):
        return None


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        return self.response


class RpaAdapterTests(unittest.TestCase):
    def test_selects_exactly_one_rpa_authorization(self):
        account = rpa_adapter.select_rpa_account(
            {"data": [{"platform": rpa_adapter.PLATFORM, "accountId": "rpa-1"}]}
        )

        self.assertEqual(account, {"accountId": "rpa-1", "platform": rpa_adapter.PLATFORM})
        with self.assertRaisesRegex(rpa_adapter.RpaAdapterError, "rpa_account_unavailable"):
            rpa_adapter.select_rpa_account({"data": []})
        with self.assertRaisesRegex(rpa_adapter.RpaAdapterError, "rpa_account_ambiguous"):
            rpa_adapter.select_rpa_account({"data": [{"accountId": "one"}, {"accountId": "two"}]})

        self.assertEqual(
            rpa_adapter.select_rpa_account({"data": [{"shop_id": "rpa-shop-1", "account": "operator"}]}),
            {"accountId": "rpa-shop-1", "platform": rpa_adapter.PLATFORM},
        )

    def test_builds_custom_period_without_reusing_shop_id_as_account_id(self):
        params = rpa_adapter.build_diagnosis_params("material-1", "2026-08-01", "2026-08-24")

        self.assertEqual(params["date_range_type"], "CUSTOM")
        self.assertEqual(params["custom_start_date"], "2026-08-01")
        self.assertEqual(params["custom_end_date"], "2026-08-24")
        self.assertNotIn("shop_id", params)

    def test_download_rejects_oversized_content_and_leaves_no_partial_artifact(self):
        response = FakeResponse({}, chunks=[b"12345"], headers={"Content-Length": "5"})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file.csv"
            with self.assertRaisesRegex(rpa_adapter.RpaAdapterError, "maximum size"):
                rpa_adapter.download_file("https://example.test/file", target, session=FakeSession(response), max_bytes=4)
            self.assertFalse(target.exists())

    def test_gateway_receipt_redacts_tokens(self):
        summary = rpa_adapter.redact_gateway_payload({"authorization": "secret", "fileUrl": "https://x.test/a?token=private"})

        self.assertEqual(summary["authorization"], "[redacted]")
        self.assertEqual(summary["fileUrl"], "https://x.test/a")
