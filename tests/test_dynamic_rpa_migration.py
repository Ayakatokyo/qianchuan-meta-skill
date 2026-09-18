import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rpa_adapter  # noqa: E402


CONNECTOR_CODE = "rpa.conn.juliang.qc.material.analysis.video.live.diagnosis"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class RoutingSession:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/dcConnector/v3/listByConnectorCode"):
            return FakeResponse({
                "success": True,
                "data": [{
                    "connectorCode": CONNECTOR_CODE,
                    "platformCode": "QC_DYNAMIC_PLATFORM",
                    "inParamJsonSchemaText": json.dumps({
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "properties": {
                            "material_id": {"type": "string"},
                            "date_type": {"type": "string"},
                            "custom_start_date": {"type": "string"},
                            "custom_end_date": {"type": "string"},
                        },
                        "required": [
                            "material_id", "date_type",
                            "custom_start_date", "custom_end_date",
                        ],
                        "additionalProperties": False,
                    }),
                    "inParamList": [{"param": "must_not_be_used"}],
                    "outParamList": [],
                }],
            })
        if url.endswith("/adg/v1/agent/rpa/authorizations/query"):
            return FakeResponse({
                "success": True,
                "data": [{"platform": "QC_DYNAMIC_PLATFORM", "accountId": "rpa-1"}],
            })
        if url.endswith("/adg/v1/agent/fetch/tasks"):
            return FakeResponse({"success": True, "data": {"task_group_id": "tg-1"}})
        if url.endswith("/adg/v1/agent/fetch/tasks/status"):
            return FakeResponse({
                "success": True,
                "data": {"status": "completed", "payload": [{"materialId": "material-1"}]},
            })
        raise AssertionError(f"unexpected endpoint: {url}")


class DynamicRpaMigrationTests(unittest.TestCase):
    def test_generated_client_requires_explicit_output_directory(self):
        client_path = ROOT / "scripts" / "rpa_dynamic_client.py"
        self.assertTrue(client_path.is_file(), "dynamic RPA client is missing")
        spec = importlib.util.spec_from_file_location("qian_dynamic_client", client_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.workspace_root = ROOT

        self.assertEqual(module.CONNECTOR_CODE, CONNECTOR_CODE)
        with self.assertRaises(SystemExit):
            module.build_parser().parse_args([
                "fetch", "--shop-id", "shop-1", "--params-json", "{}",
            ])

    def test_generated_client_preserves_legacy_session_cookie_fallback(self):
        client_path = ROOT / "scripts" / "rpa_dynamic_client.py"
        spec = importlib.util.spec_from_file_location("qian_dynamic_auth_client", client_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with mock.patch.dict(module.os.environ, {
            "ENV_BACKEND_HOST": "https://gateway.test",
            "YUCE_AUTHORIZATION": "authorization",
            "YCSESSIONID": "legacy-session",
        }, clear=True):
            runtime = module.runtime_config(require_session=True)

        self.assertEqual(runtime["session_id"], "legacy-session")
        self.assertEqual(runtime["cookies"], {"YCSESSIONID": "legacy-session"})

    def test_generated_client_uses_authorization_cookie_when_only_agent_session_exists(self):
        client_path = ROOT / "scripts" / "rpa_dynamic_client.py"
        spec = importlib.util.spec_from_file_location("qian_dynamic_auth_fallback_client", client_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with mock.patch.dict(module.os.environ, {
            "ENV_BACKEND_HOST": "https://gateway.test",
            "YUCE_AUTHORIZATION": "authorization",
            "YUCE_SESSION_ID": "agent-session",
        }, clear=True):
            runtime = module.runtime_config(require_session=True)

        self.assertEqual(runtime["session_id"], "agent-session")
        self.assertEqual(runtime["cookies"], {"YCSESSIONID": "authorization"})

    def test_account_lookup_uses_live_connector_platform(self):
        session = RoutingSession()
        account = rpa_adapter.list_rpa_accounts(
            environ={
                "ENV_BACKEND_HOST": "https://gateway.test",
                "YUCE_AUTHORIZATION": "auth",
                "YUCE_SESSION_ID": "session",
            },
            session=session,
        )

        self.assertEqual(account["platform"], "QC_DYNAMIC_PLATFORM")
        self.assertTrue(session.posts[0][0].endswith("/dcConnector/v3/listByConnectorCode"))
        self.assertEqual(session.posts[1][1]["json"]["platform"], "QC_DYNAMIC_PLATFORM")

    def test_fetch_validates_live_schema_and_uses_async_task_endpoints(self):
        session = RoutingSession()
        with mock.patch.object(rpa_adapter.time, "sleep"):
            records, csv_path, _receipt = rpa_adapter.fetch_diagnosis(
                shop_id="report-shop",
                rpa_account_id="rpa-1",
                material_id="material-1",
                start_date="2026-09-01",
                end_date="2026-09-02",
                csv_dir=ROOT / "千川数据资源" / "test",
                environ={
                    "ENV_BACKEND_HOST": "https://gateway.test",
                    "YUCE_AUTHORIZATION": "auth",
                    "YUCE_SESSION_ID": "session",
                },
                session=session,
            )

        self.assertEqual(records, [{"materialId": "material-1"}])
        self.assertIsNone(csv_path)
        urls = [url for url, _kwargs in session.posts]
        self.assertIn("https://gateway.test/adg/v1/agent/fetch/tasks", urls)
        self.assertIn("https://gateway.test/adg/v1/agent/fetch/tasks/status", urls)
        submit = next(kwargs for url, kwargs in session.posts if url.endswith("/fetch/tasks"))
        self.assertEqual(submit["json"]["platform"], "QC_DYNAMIC_PLATFORM")
        self.assertEqual(submit["json"]["function_code"], CONNECTOR_CODE)


if __name__ == "__main__":
    unittest.main()
