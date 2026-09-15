import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


class SequenceSession(FakeSession):
    def __init__(self, responses):
        super().__init__(responses[-1])
        self.responses = list(responses)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.responses.pop(0)


def connector_detail():
    return {
        "success": True,
        "data": [{
            "connectorCode": rpa_adapter.dynamic_rpa.CONNECTOR_CODE,
            "platformCode": "LIVE_PLATFORM",
            "inParamJsonSchemaText": None,
            "inParamList": [
                {"param": "material_id", "isRequired": True, "dataTypeEnum": "STRING"},
                {"param": "date_range_type", "isRequired": True, "dataTypeEnum": "STRING"},
                {"param": "custom_start_date", "isRequired": True, "dataTypeEnum": "STRING"},
                {"param": "custom_end_date", "isRequired": True, "dataTypeEnum": "STRING"},
            ],
            "outParamList": [],
        }],
    }


class RpaAdapterTests(unittest.TestCase):
    def test_runtime_context_uses_authorization_as_session_fallback(self):
        context = rpa_adapter.runtime_context({
            "ENV_BACKEND_HOST": "https://gateway.test/",
            "YUCE_AUTHORIZATION": "authorization-token",
        })

        self.assertEqual(context["agentSession"], "authorization-token")
        self.assertEqual(context["cookieSession"], "authorization-token")

    def test_selects_exactly_one_rpa_authorization(self):
        account = rpa_adapter.select_rpa_account(
            {"data": [{"platform": "LIVE_PLATFORM", "accountId": "rpa-1"}]},
            "LIVE_PLATFORM",
        )

        self.assertEqual(account["accountId"], "rpa-1")
        self.assertEqual(account["rpaShopId"], "rpa-1")
        with self.assertRaisesRegex(rpa_adapter.RpaAdapterError, "rpa_account_unavailable"):
            rpa_adapter.select_rpa_account({"data": []}, "LIVE_PLATFORM")
        with self.assertRaisesRegex(rpa_adapter.RpaAdapterError, "rpa_account_ambiguous"):
            rpa_adapter.select_rpa_account({"data": [{"accountId": "one"}, {"accountId": "two"}]}, "LIVE_PLATFORM")

        legacy_account = rpa_adapter.select_rpa_account(
            {"data": [{"shop_id": "rpa-shop-1", "account": "operator"}]},
            "LIVE_PLATFORM",
        )
        self.assertEqual(legacy_account["rpaShopId"], "rpa-shop-1")

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

    def test_rejects_failed_async_task_before_download(self):
        session = SequenceSession([
            FakeResponse(connector_detail()),
            FakeResponse({"success": True, "data": {"task_group_id": "tg-1"}}),
            FakeResponse({"success": True, "data": {"status": "failed", "errors": ["remote"]}}),
        ])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(rpa_adapter.time, "sleep"):
                with self.assertRaisesRegex(rpa_adapter.RpaAdapterError, "task failed"):
                    rpa_adapter.fetch_diagnosis(
                        shop_id="report-shop",
                        rpa_account_id="rpa-shop",
                        material_id="m-1",
                        start_date="2026-08-01",
                        end_date="2026-08-02",
                        csv_dir=Path(directory),
                        environ={"ENV_BACKEND_HOST": "https://gateway.test", "YUCE_AUTHORIZATION": "auth", "YUCE_SESSION_ID": "session"},
                        session=session,
                    )

    def test_accepts_completed_async_record_payload(self):
        session = SequenceSession([
            FakeResponse(connector_detail()),
            FakeResponse({"success": True, "data": {"task_group_id": "tg-1"}}),
            FakeResponse({"success": True, "data": {"status": "completed", "payload": [{"materialId": "m-1"}]}}),
        ])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(rpa_adapter.time, "sleep"):
                records, csv_path, _receipt = rpa_adapter.fetch_diagnosis(
                    shop_id="report-shop",
                    rpa_account_id="rpa-shop",
                    material_id="m-1",
                    start_date="2026-08-01",
                    end_date="2026-08-02",
                    csv_dir=Path(directory),
                    environ={"ENV_BACKEND_HOST": "https://gateway.test", "YUCE_AUTHORIZATION": "auth", "YUCE_SESSION_ID": "session"},
                    session=session,
                )
        self.assertEqual(records, [{"materialId": "m-1"}])
        self.assertIsNone(csv_path)
