import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qianchuan_report_client as client  # noqa: E402


def material_record():
    return {
        "dimensions": {
            "material_id": {"Value": "m-1", "ValueStr": "m-1"},
            "anchor_id": {"Value": "anchor-1", "ValueStr": "anchor-1"},
            "roi2_material_video_name": {"ValueStr": "素材"},
            "roi2_material_type_v3": {"ValueStr": "直播素材"},
            "roi2_material_video_type": {"ValueStr": "短视频"},
        },
        "metrics": {
            "stat_cost_for_overall_roi2": {"Value": 12.5},
            "total_order_settle_amount_for_roi2_1h": {"Value": 8},
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

        self.assertEqual(records, [material_record()])
        self.assertEqual(session.post_call["json"]["shop_id"], "shop-1")
        self.assertEqual(
            session.post_call["json"]["business_params"]["advertiserId"], 1770096415226951
        )
        self.assertEqual(
            session.post_call["json"]["business_params"]["startTime"], "2026-08-01 00:00:00"
        )
        self.assertEqual(session.post_call["cookies"], {"YCSESSIONID": "cookie-session"})
        self.assertNotIn("authorization-secret", gateway_response)
        self.assertNotIn("agent-session", gateway_response)
        self.assertNotIn("cookie-session", gateway_response)

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
        self.assertIsNotNone(session.get_call)
        self.assertNotIn("signed-secret", gateway_response)


if __name__ == "__main__":
    unittest.main()
