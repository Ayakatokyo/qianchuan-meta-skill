import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_meta_skill  # noqa: E402
from field_registry import load_field_registry  # noqa: E402


def origin_row(material_id, cost, amount):
    return {
        "dimensions": {
            "material_id": {"Value": material_id},
            "roi2_material_video_name": {"ValueStr": "素材" + material_id},
            "roi2_material_type_v3": {"ValueStr": "直播素材"},
            "roi2_material_video_type": {"ValueStr": "短视频"},
        },
        "metrics": {
            "stat_cost_for_overall_roi2": {"Value": str(cost)},
            "total_order_settle_amount_for_roi2_1h": {"Value": str(amount)},
        },
    }


class MetaSkillCliTests(unittest.TestCase):
    def test_prepare_analysis_command_dispatches_to_offline_preparer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            request = root / "analysis-request.json"
            request.write_text("{}", encoding="utf-8")
            with patch.object(run_meta_skill, "prepare_analysis", return_value={"status": "awaiting_model"}) as prepare:
                status = run_meta_skill.main([
                    "prepare-analysis", "--run-dir", str(root), "--analysis-request", str(request),
                    "--output-dir", str(root / "analysis"),
                ])

        self.assertEqual(status, 0)
        prepare.assert_called_once_with(root, request, root / "analysis")

    def test_verify_analysis_command_returns_nonzero_when_contract_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(run_meta_skill, "verify_analysis", return_value={"status": "failed", "errors": ["bad result"]}):
                status = run_meta_skill.main(["verify-analysis", "--run-dir", str(root), "--analysis-dir", str(root / "analysis")])

        self.assertEqual(status, 1)

    def test_connector_order_by_uses_direct_api_metric_rankings(self):
        registry = load_field_registry(run_meta_skill.REGISTRY_PATH)

        self.assertEqual(
            run_meta_skill._connector_order_by(
                {"sort": {"field": "total_order_settle_amount_for_roi2_1h", "direction": "asc"}}, registry
            ),
            [{"field": "total_order_settle_amount_for_roi2_1h", "type": 1}],
        )
        self.assertEqual(
            run_meta_skill._connector_order_by(
                {"sort": {"field": "total_prepay_and_pay_settle_roi2_1h", "direction": "desc"}}, registry
            ),
            [{"field": "total_prepay_and_pay_settle_roi2_1h", "type": 2}],
        )

    def write_config(self, directory):
        path = directory / "config.json"
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "query": {
                        "schemaVersion": 1,
                        "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
                        "shopId": "shop-1",
                        "advertiserId": "1770096415226951",
                        "topN": 1,
                        "dimensionFilters": [],
                        "metricFilters": [],
                        "sort": {"field": "stat_cost_for_overall_roi2", "direction": "desc"},
                    },
                    "highlightPolicy": {
                        "schemaVersion": 1,
                        "includeZeroSecond": True,
                        "policies": [{"metric": "CLICK_COUNT", "mode": "local_peaks", "maxFrames": 3}],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def write_specs(self, directory):
        query_path = directory / "query-spec.json"
        highlight_path = directory / "highlight-spec.json"
        query_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "source": "qianchuan_direct_material_report",
                    "scope": {"shopId": "shop-1", "advertiserId": "1770096415226951"},
                    "period": {"startDate": "2026-08-01", "endDate": "2026-08-07"},
                    "filters": {"dimensionFilters": [], "metricFilters": []},
                    "ranking": {"field": "stat_cost_for_overall_roi2", "direction": "desc"},
                    "collection": {"targetTopN": 1},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        highlight_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "includeZeroSecond": True,
                    "metrics": {
                        "CLICK_COUNT": {"enabled": True, "strategy": "local_peaks", "maxFrames": 3},
                        "LOSE_COUNT": {"enabled": False},
                        "LIKE_COUNT": {"enabled": False},
                        "COMMENT_COUNT": {"enabled": False},
                        "SHARE_COUNT": {"enabled": False},
                        "NEW_FOLLOWER_COUNT": {"enabled": False},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return query_path, highlight_path

    def test_query_accepts_independent_specs_and_persists_resolved_audit_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            output_dir = root / "run"
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]) as fetch:
                status = run_meta_skill.main(
                    [
                        "query", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path),
                        "--output-dir", str(output_dir),
                    ]
                )

            receipt = json.loads((output_dir / "query-receipt.json").read_text(encoding="utf-8"))
            fingerprint = json.loads((output_dir / "configuration-fingerprint.json").read_text(encoding="utf-8"))
            resolved_query_exists = (output_dir / "resolved-query-spec.json").is_file()
            resolved_highlight_exists = (output_dir / "resolved-highlight-spec.json").is_file()

        self.assertEqual(status, 0)
        self.assertEqual(fetch.call_args.args[:4], ("shop-1", "1770096415226951", "2026-08-01", "2026-08-07"))
        self.assertEqual(
            fetch.call_args.kwargs["order_by"], [{"field": "stat_cost_for_overall_roi2", "type": 2}]
        )
        self.assertTrue(resolved_query_exists)
        self.assertTrue(resolved_highlight_exists)
        self.assertEqual(receipt["configurationFingerprint"], fingerprint["configurationFingerprint"])

    def test_query_uses_candidate_pool_size_and_persists_both_collection_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            query = json.loads(query_path.read_text(encoding="utf-8"))
            query["collection"] = {"candidateTopN": 2, "targetTopN": 1}
            query_path.write_text(json.dumps(query), encoding="utf-8")
            output_dir = root / "run"
            with patch.object(
                run_meta_skill,
                "fetch_material_report",
                return_value=[origin_row("m-1", 10, 2), origin_row("m-2", 9, 1)],
            ):
                status = run_meta_skill.main(
                    ["query", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path), "--output-dir", str(output_dir)]
                )
            payload = json.loads((output_dir / "top_materials.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(payload["collection"], {"candidateTopN": 2, "targetTopN": 1})
        self.assertEqual([item["materialId"] for item in payload["materials"]], ["m-1", "m-2"])

    def test_query_collects_direct_report_records_and_stops_after_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["query"]["shopId"] = "shop-1"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            output_dir = root / "run"
            records = [origin_row("m-1", 2, 2), origin_row("m-2", 10, 1)]
            with patch.object(run_meta_skill, "fetch_material_report", return_value=records) as fetch:
                status = run_meta_skill.main(
                    ["query", "--config", str(config_path), "--output-dir", str(output_dir)]
                )

            top_materials = json.loads((output_dir / "top_materials.json").read_text(encoding="utf-8"))
            receipt = json.loads((output_dir / "query-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(fetch.call_args.args[:4], ("shop-1", "1770096415226951", "2026-08-01", "2026-08-07"))
        self.assertEqual(
            fetch.call_args.kwargs["order_by"], [{"field": "stat_cost_for_overall_roi2", "type": 2}]
        )
        self.assertEqual(fetch.call_args.kwargs["output_dir"], output_dir / "source-data")
        self.assertEqual([item["materialId"] for item in top_materials["materials"]], ["m-2"])
        self.assertEqual(receipt["source"], {"type": "direct_material_report"})
        self.assertFalse((output_dir / "diagnosis-records").exists())

    def test_validate_checks_config_without_collecting_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            with patch.object(run_meta_skill, "fetch_material_report") as collect:
                status = run_meta_skill.main(["validate", "--config", str(config_path)])

        self.assertEqual(status, 0)
        collect.assert_not_called()

    def test_validate_serializes_numeric_metric_filters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["query"]["metricFilters"] = [
                {"field": "total_prepay_and_pay_settle_roi2_1h", "operator": "gte", "value": 1.2}
            ]
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("builtins.print") as output:
                status = run_meta_skill.main(["validate", "--config", str(config_path)])

        self.assertEqual(status, 0)
        normalized = json.loads(output.call_args.args[0])
        self.assertEqual(normalized["query"]["metricFilters"][0]["value"], "1.2")

    def test_query_writes_candidates_and_receipt_then_stops(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            output_dir = root / "run"
            with patch.object(
                run_meta_skill,
                "fetch_material_report",
                return_value=[origin_row("m-1", 2, 2), origin_row("m-2", 10, 1)],
            ) as collect:
                status = run_meta_skill.main(
                    ["query", "--config", str(config_path), "--output-dir", str(output_dir)]
                )

            top_materials = json.loads((output_dir / "top_materials.json").read_text(encoding="utf-8"))
            receipt = json.loads((output_dir / "query-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        collect.assert_called_once()
        self.assertEqual([item["materialId"] for item in top_materials["materials"]], ["m-2"])
        self.assertEqual(receipt["returnedCount"], 1)
        self.assertFalse((output_dir / "diagnosis-records").exists())

    def test_query_receipt_preserves_source_data_quality_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            broken = origin_row("m-broken", 99, 99)
            del broken["dimensions"]["material_id"]
            output_dir = root / "run"
            with patch.object(
                run_meta_skill,
                "fetch_material_report",
                return_value=[broken, origin_row("m-1", 10, 20)],
            ):
                status = run_meta_skill.main(
                    ["query", "--config", str(config_path), "--output-dir", str(output_dir)]
                )
            receipt = json.loads((output_dir / "query-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(receipt["source"], {"type": "direct_material_report"})
        self.assertEqual(receipt["sourceRecordCount"], 2)
        self.assertEqual(receipt["skippedSourceRecordCount"], 1)
        self.assertEqual(receipt["exclusionSummary"]["missing_material_id"], 1)

    def test_select_frames_uses_configured_policy_without_collecting_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            records_path = root / "records.json"
            records_path.write_text("[]", encoding="utf-8")
            output_dir = root / "frames"
            with patch.object(run_meta_skill, "fetch_material_report") as collect, patch.object(
                run_meta_skill,
                "extract_highlight_frames",
                return_value={"status": "succeeded", "frames": []},
            ) as extract:
                status = run_meta_skill.main(
                    [
                        "select-frames", "--config", str(config_path), "--records-json", str(records_path),
                        "--video", "video.mp4", "--output-dir", str(output_dir),
                    ]
                )

        self.assertEqual(status, 0)
        collect.assert_not_called()
        self.assertEqual(extract.call_args.args[1]["policies"][0]["metric"], "CLICK_COUNT")

    def test_select_frames_accepts_an_independent_highlight_spec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _query_path, highlight_path = self.write_specs(root)
            records_path = root / "records.json"
            records_path.write_text("[]", encoding="utf-8")
            with patch.object(run_meta_skill, "fetch_material_report") as collect, patch.object(
                run_meta_skill, "extract_highlight_frames", return_value={"status": "succeeded", "frames": []}
            ) as extract:
                status = run_meta_skill.main(
                    [
                        "select-frames", "--highlight-spec", str(highlight_path),
                        "--records-json", str(records_path), "--video", "video.mp4", "--output-dir", str(root / "frames"),
                    ]
                )

        self.assertEqual(status, 0)
        collect.assert_not_called()
        self.assertEqual(extract.call_args.args[1]["policies"][0]["metric"], "CLICK_COUNT")

    def test_query_has_no_asset_table_dependency(self):
        self.assertNotIn(
            "asset_table_client",
            (ROOT / "scripts" / "run_meta_skill.py").read_text(encoding="utf-8"),
        )
        self.assertFalse((ROOT / "scripts" / "asset_table_client.py").exists())

    def test_run_requires_execute_rpa_before_resolving_rpa_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            output_dir = root / "run"
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]) as collect, patch.object(
                run_meta_skill, "list_rpa_accounts", side_effect=AssertionError("RPA account must not be resolved")
            ), patch.object(run_meta_skill, "run_rpa_pipeline", side_effect=AssertionError("RPA pipeline must not run")):
                status = run_meta_skill.main(
                    ["run", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path), "--output-dir", str(output_dir)]
                )

            receipt = json.loads((output_dir / "run-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        collect.assert_called_once()
        self.assertFalse(receipt["executionAuthorized"])
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["stages"]["query"]["status"], "succeeded")
        self.assertEqual(receipt["stages"]["rpa"]["status"], "not_requested")
        self.assertEqual(receipt["stages"]["batchVerification"]["status"], "not_requested")
        self.assertEqual(receipt["stages"]["factPack"]["status"], "not_requested")

    def test_run_executes_rpa_only_with_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            output_dir = root / "run"
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]) as collect, patch.object(
                run_meta_skill, "list_rpa_accounts", return_value={"accountId": "rpa-1", "platform": "RPA_JULIANG_QC_ACCOUNT_PASSWORD"}
            ) as accounts, patch.object(
                run_meta_skill, "run_rpa_pipeline", return_value={"succeeded": True, "readyPath": str(output_dir / "rpa" / "top_materials_rpa_ready.json"), "verificationPath": str(output_dir / "rpa" / "batch-verification.json")}
            ), patch.object(
                run_meta_skill, "build_fact_pack", return_value={"targetTopN": 1, "counts": {"materials": 1}}
            ) as pipeline:
                status = run_meta_skill.main(
                    ["run", "--execute-rpa", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path), "--output-dir", str(output_dir)]
                )

            receipt = json.loads((output_dir / "run-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        collect.assert_called_once()
        accounts.assert_called_once()
        pipeline.assert_called_once()
        self.assertTrue(receipt["executionAuthorized"])
        self.assertEqual(receipt["stages"]["rpa"]["status"], "succeeded")
        self.assertEqual(receipt["stages"]["batchVerification"]["status"], "succeeded")

    def test_authorized_run_passes_selected_material_and_resolved_period_to_rpa(self):
        invocation = {}

        def pipeline_side_effect(*_args, **kwargs):
            fetch_diagnosis = kwargs["fetch_diagnosis"]
            fetch_diagnosis({"materialId": "m-1"}, {"csv": Path(temp_dir) / "csv"})
            return {
                "succeeded": True,
                "readyPath": str(output_dir / "rpa" / "top_materials_rpa_ready.json"),
                "verificationPath": str(output_dir / "rpa" / "batch-verification.json"),
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            output_dir = root / "run"

            def isolated_side_effect(payload, **_kwargs):
                invocation.update(payload)
                return {"records": []}

            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]), patch.object(
                run_meta_skill, "list_rpa_accounts", return_value={"accountId": "rpa-1", "platform": "RPA"}
            ), patch.object(run_meta_skill, "run_isolated", side_effect=isolated_side_effect), patch.object(
                run_meta_skill, "run_rpa_pipeline", side_effect=pipeline_side_effect
            ), patch.object(
                run_meta_skill, "build_fact_pack", return_value={"targetTopN": 1, "counts": {"materials": 1}}
            ):
                status = run_meta_skill.main(
                    [
                        "run", "--execute-rpa", "--query-spec", str(query_path),
                        "--highlight-spec", str(highlight_path), "--output-dir", str(output_dir),
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(invocation["materialId"], "m-1")
        self.assertEqual(invocation["startDate"], "2026-08-01")
        self.assertEqual(invocation["endDate"], "2026-08-07")

    def test_run_uses_legacy_config_in_rpa_next_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            output_dir = root / "run"
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]), patch.object(
                run_meta_skill, "list_rpa_accounts", side_effect=AssertionError("RPA account must not be resolved")
            ), patch("builtins.print") as output:
                status = run_meta_skill.main(["run", "--config", str(config_path), "--output-dir", str(output_dir)])

        self.assertEqual(status, 0)
        self.assertIn(
            "next_command=run --execute-rpa --config %s --output-dir %s" % (config_path, output_dir),
            [call.args[0] for call in output.call_args_list],
        )

    def test_run_returns_nonzero_for_partial_rpa_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]), patch.object(
                run_meta_skill, "list_rpa_accounts", return_value={"accountId": "rpa-1", "platform": "RPA_JULIANG_QC_ACCOUNT_PASSWORD"}
            ), patch.object(run_meta_skill, "run_rpa_pipeline", return_value={"succeeded": False, "verificationPath": "batch-verification.json"}):
                status = run_meta_skill.main(
                    ["run", "--execute-rpa", "--query-spec", str(query_path), "--highlight-spec", str(highlight_path), "--output-dir", str(root / "run")]
                )

        self.assertEqual(status, 2)

    def test_successful_rpa_run_builds_fact_pack_and_records_its_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            output_dir = root / "run"
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]), patch.object(
                run_meta_skill, "list_rpa_accounts", return_value={"accountId": "rpa-1", "platform": "RPA"}
            ), patch.object(
                run_meta_skill, "run_rpa_pipeline", return_value={
                    "succeeded": True,
                    "readyPath": str(output_dir / "rpa" / "top_materials_rpa_ready.json"),
                    "verificationPath": str(output_dir / "rpa" / "batch-verification.json"),
                }
            ), patch.object(
                run_meta_skill, "build_fact_pack", return_value={"targetTopN": 1, "counts": {"materials": 1}}
            ) as build_pack:
                status = run_meta_skill.main(
                    [
                        "run", "--execute-rpa", "--query-spec", str(query_path),
                        "--highlight-spec", str(highlight_path), "--output-dir", str(output_dir),
                    ]
                )

            receipt = json.loads((output_dir / "run-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        build_pack.assert_called_once_with(output_dir)
        self.assertEqual(receipt["stages"]["factPack"]["status"], "succeeded")

    def test_run_fails_and_marks_fact_pack_stage_when_build_cannot_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            query_path, highlight_path = self.write_specs(root)
            output_dir = root / "run"
            with patch.object(run_meta_skill, "fetch_material_report", return_value=[origin_row("m-1", 10, 2)]), patch.object(
                run_meta_skill, "list_rpa_accounts", return_value={"accountId": "rpa-1", "platform": "RPA"}
            ), patch.object(
                run_meta_skill, "run_rpa_pipeline", return_value={"succeeded": True, "readyPath": "ready.json", "verificationPath": "verify.json"}
            ), patch.object(
                run_meta_skill, "build_fact_pack", side_effect=run_meta_skill.FactPackError("fact pack requires completed RPA artifacts")
            ):
                status = run_meta_skill.main([
                    "run", "--execute-rpa", "--query-spec", str(query_path),
                    "--highlight-spec", str(highlight_path), "--output-dir", str(output_dir),
                ])

            receipt = json.loads((output_dir / "run-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["stages"]["factPack"]["status"], "failed")

    def test_fact_pack_commands_are_explicit_and_offline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            fact_dir = run_dir / "fact-pack"
            with patch.object(
                run_meta_skill, "build_fact_pack", return_value={"targetTopN": 6, "counts": {"materials": 6}}
            ) as build_pack, patch("builtins.print") as output:
                build_status = run_meta_skill.main([
                    "build-fact-pack", "--run-dir", str(run_dir), "--output-dir", str(fact_dir)
                ])
            with patch.object(
                run_meta_skill, "verify_fact_pack", return_value={"status": "succeeded", "errors": []}
            ) as verify_pack:
                verify_status = run_meta_skill.main([
                    "verify-fact-pack", "--run-dir", str(run_dir), "--fact-pack-dir", str(fact_dir)
                ])

        self.assertEqual(build_status, 0)
        self.assertEqual(verify_status, 0)
        build_pack.assert_called_once_with(str(run_dir), str(fact_dir))
        verify_pack.assert_called_once_with(str(run_dir), str(fact_dir))
        self.assertIn('"targetTopN": 6', output.call_args.args[0])
