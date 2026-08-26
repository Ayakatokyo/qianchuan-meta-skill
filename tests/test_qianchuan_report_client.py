import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qianchuan_report_client as client  # noqa: E402


def material_record(*, omit_metrics=(), omit_dimensions=()):
    return {
        "dimensions": {
            field: {"Value": "m-1" if field == "material_id" else field, "ValueStr": "素材" if field == "roi2_material_video_name" else field}
            for field in client.DEFAULT_DIMENSIONS if field not in omit_dimensions
        },
        "metrics": {
            field: {"Value": 1, "ValueStr": "1"}
            for field in client.DEFAULT_METRICS if field not in omit_metrics
        },
    }


def valid_env():
    return {
        "ENV_BACKEND_HOST": "http://gateway.test/api",
        "YUCE_AUTHORIZATION": "authorization-secret",
        "YUCE_SESSION_ID": "agent-session",
        "YCSESSIONID": "cookie-session",
    }


class FakeResponse:
    def __init__(self, payload=None, content=b""):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size=1024 * 1024):
        del chunk_size
        yield self.content

    def close(self):
        return None


class FakeSession:
    def __init__(self, response, download_response=None):
        self.response = response
        self.download_response = download_response
        self.post_call = None
        self.get_call = None

    def post(self, *args, **kwargs):
        self.post_call = {"args": args, **kwargs}
        return self.response

    def get(self, *args, **kwargs):
        self.get_call = {"args": args, **kwargs}
        return self.download_response


class QianchuanReportClientTests(unittest.TestCase):
    def test_default_contract_is_an_aggregate_eight_dimension_102_metric_request(self):
        self.assertEqual(client.DEFAULT_DIMENSIONS, [
            "ecp_app_id",
            "roi2_material_type_v3",
            "roi2_material_video_play_info",
            "roi2_material_video_name",
            "roi2_material_video_type",
            "material_create_time_v2",
            "material_id",
            "anchor_id",
        ])
        self.assertEqual(len(client.DEFAULT_METRICS), 102)
        self.assertEqual(len(client.DEFAULT_METRICS), len(set(client.DEFAULT_METRICS)))
        self.assertEqual(
            hashlib.sha256("\0".join(client.DEFAULT_METRICS).encode()).hexdigest(),
            "ef5af9efb7c10495f8d0ab9476df4655c976e2745ecbbc2ca9987a8a68980aae",
        )
        self.assertTrue({
            "stat_cost_for_overall_roi2",
            "total_prepay_and_pay_settle_overall_roi2_1h",
            "total_cost_per_pay_order_settle_for_overall_roi2_1h",
            "total_prepay_and_pay_settle_roi2_1h",
        }.issubset(client.DEFAULT_METRICS))
        self.assertFalse({
            "roi2_material_advice",
            "roi2_material_advice_reason",
            "stat_time_day",
            "range_stat_time_hour",
        }.intersection(client.DEFAULT_DIMENSIONS))

    def test_rejects_nonempty_record_missing_a_requested_metric_key(self):
        missing_field = "video_play_finish_rate_for_roi2_v2"
        session = FakeSession(FakeResponse({
            "success": True,
            "data": {"result_type": "payload", "result_content": {
                "payload": [material_record(omit_metrics={missing_field})],
            }},
        }))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(client.ReportClientError, missing_field):
                client.fetch_material_report(
                    "shop-1", "1770096415226951", "2026-08-01", "2026-08-07",
                    output_dir=root, environ=valid_env(), session=session,
                )
            self.assertFalse((root / "field-manifest.json").exists())

    def test_fetches_payload_with_shop_and_advertiser_ids(self):
        session = FakeSession(FakeResponse({
            "success": True,
            "data": {"result_type": "payload", "result_content": {"payload": [material_record()]}},
        }))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records = client.fetch_material_report(
                "shop-1", "1770096415226951", "2026-08-01", "2026-08-07",
                output_dir=root, environ=valid_env(), session=session,
            )

            gateway_response = (root / "gateway-response.json").read_text(encoding="utf-8")
            manifest = json.loads((root / "field-manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(records, [material_record()])
        self.assertEqual(session.post_call["json"]["shop_id"], "shop-1")
        self.assertEqual(
            session.post_call["json"]["business_params"]["advertiserId"], 1770096415226951
        )
        business_params = session.post_call["json"]["business_params"]
        self.assertEqual(len(business_params["dimensions"]), 8)
        self.assertEqual(len(business_params["metrics"]), 102)
        self.assertIn("stat_cost_for_overall_roi2", business_params["metrics"])
        self.assertIn("stat_cost_for_roi2", business_params["metrics"])
        self.assertEqual(
            business_params["orderBy"], [{"field": "stat_cost_for_overall_roi2", "type": 2}]
        )
        self.assertEqual(
            session.post_call["json"]["business_params"]["startTime"], "2026-08-01 00:00:00"
        )
        self.assertEqual(session.post_call["cookies"], {"YCSESSIONID": "cookie-session"})
        self.assertNotIn("authorization-secret", gateway_response)
        self.assertNotIn("agent-session", gateway_response)
        self.assertNotIn("cookie-session", gateway_response)
        self.assertEqual(len(manifest["dimensions"]), 8)
        self.assertEqual(len(manifest["metrics"]), 102)
        self.assertEqual(manifest["recordCount"], 1)
        self.assertNotIn("authorization-secret", json.dumps(manifest))
        self.assertNotIn("agent-session", json.dumps(manifest))
        self.assertNotIn("cookie-session", json.dumps(manifest))

    def test_uses_the_resolved_gateway_sort(self):
        session = FakeSession(FakeResponse({
            "success": True,
            "data": {"result_type": "payload", "result_content": {"payload": [material_record()]}},
        }))
        with tempfile.TemporaryDirectory() as temp_dir:
            client.fetch_material_report(
                "shop-1", "1770096415226951", "2026-08-01", "2026-08-07",
                output_dir=Path(temp_dir),
                order_by=[{"field": "total_order_settle_amount_for_roi2_1h", "type": 1}],
                environ=valid_env(),
                session=session,
            )

        self.assertEqual(
            session.post_call["json"]["business_params"]["orderBy"],
            [{"field": "total_order_settle_amount_for_roi2_1h", "type": 1}],
        )

    def test_reports_gateway_task_failure_detail(self):
        session = FakeSession(FakeResponse({
            "success": True,
            "data": {"result_type": "files", "result_content": {
                "status": "FAILED", "failedErrors": [{"errorMsg": "invalid metric"}],
            }},
        }))
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(client.ReportClientError, "invalid metric"):
                client.fetch_material_report(
                    "shop-1", "1770096415226951", "2026-08-01", "2026-08-07",
                    output_dir=Path(temp_dir), environ=valid_env(), session=session,
                )

    def test_files_response_writes_structured_records_and_redacts_url(self):
        csv_content = (
            '"' + json.dumps(material_record(), ensure_ascii=False).replace('"', '""') + '"\r\n'
        ).encode("utf-8")
        session = FakeSession(
            FakeResponse({
                "success": True,
                "data": {
                    "result_type": "files",
                    "result_content": {
                        "status": "COMPLETED",
                        "failedInstances": 0,
                        "files": [{"fileUrl": "http://gateway.test/report.csv?token=signed-secret"}],
                    },
                },
            }),
            FakeResponse(content=csv_content),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records = client.fetch_material_report(
                "shop-1", "1770096415226951", "2026-08-01", "2026-08-07",
                output_dir=root, environ=valid_env(), session=session,
            )
            gateway_response = (root / "gateway-response.json").read_text(encoding="utf-8")

            self.assertEqual(records, [material_record()])
            self.assertTrue((root / "report.csv").is_file())
            self.assertTrue((root / "report.csv.structured.json").is_file())
            manifest = json.loads((root / "field-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["recordCount"], 1)
        self.assertIsNotNone(session.get_call)
        self.assertNotIn("signed-secret", gateway_response)

    def test_keeps_gateway_receipt_and_stage_status_when_csv_download_fails(self):
        result = {
            "success": True,
            "data": {"result_type": "files", "result_content": {
                "status": "COMPLETED", "failedInstances": 0,
                "files": [{"fileUrl": "http://gateway.test/report.csv?token=signed-secret"}],
            }},
        }
        session = FakeSession(FakeResponse(result), FakeResponse(requests.RequestException("network unavailable")))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(client.ReportClientError, "gateway completed"):
                client.fetch_material_report(
                    "shop-1", "1770096415226951", "2026-08-01", "2026-08-07",
                    output_dir=root, environ=valid_env(), session=session,
                )

            gateway = json.loads((root / "gateway-status.json").read_text(encoding="utf-8"))
            download = json.loads((root / "result-download-status.json").read_text(encoding="utf-8"))
            parse = json.loads((root / "result-parse-status.json").read_text(encoding="utf-8"))
            receipt = (root / "gateway-response.json").read_text(encoding="utf-8")

        self.assertEqual(gateway["status"], "completed")
        self.assertEqual(download["status"], "failed")
        self.assertEqual(parse["status"], "not_started")
        self.assertNotIn("signed-secret", receipt)


if __name__ == "__main__":
    unittest.main()
