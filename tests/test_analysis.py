import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analysis import (  # noqa: E402
    AnalysisError,
    analysis_fingerprint,
    compile_model_input,
    load_analysis_registry,
    prepare_analysis,
    receive_analysis_result,
    render_reports,
    resolve_analysis_spec,
    slice_fact_pack,
    validate_analysis_handoff,
    validate_analysis_result,
    verify_analysis,
    resolve_fact_retrieval_spec,
    retrieve_fact_slice,
    load_analysis_capability_registry,
)


REGISTRY_PATH = ROOT / "config" / "analysis-registry-v1.json"


def fact_pack(materials=None):
    materials = materials or [
        {"rank": 1, "materialId": "m-1"},
        {"rank": 2, "materialId": "m-2"},
        {"rank": 3, "materialId": "m-3"},
    ]
    return {
        "manifest": {
            "schemaVersion": 1,
            "targetTopN": len(materials),
            "materialIds": [item["materialId"] for item in materials],
            "quality": {
                "status": "built",
                "categoryStatuses": {
                    "materialLeaderboard": "available",
                    "audience": "available",
                    "scripts": "available",
                    "highlightFrames": "available",
                },
            },
        },
        "materialLeaderboard": {"materials": materials},
    }


def spec(*sections, selection=None):
    return {
        "schemaVersion": 1,
        "sections": list(sections),
        "selection": selection or {"rankRange": {"start": 1, "end": 3}},
        "objective": "检查素材表现并形成可审计的分析输入",
        "questions": ["哪些素材值得进一步关注？"],
        "metrics": [
            {
                "section": "material_leaderboard",
                "field": "metrics.stat_cost_for_overall_roi2",
                "aggregation": "value",
            }
        ],
        "groupComparisons": [],
        "calculations": [],
        "evidenceStrategy": {"mode": "source_linked", "includeArtifacts": False},
        "output": {"format": "json+markdown"},
    }


def ref_for(run_dir, relative, content):
    path = Path(run_dir) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    data = path.read_bytes()
    return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data)}


def analysis_fact_pack(root):
    root = Path(root)
    source_ref = ref_for(root, "source/materials.json", "materials")
    audience_ref = ref_for(root, "source/audience.json", "audience")
    script_ref = ref_for(root, "source/scripts.json", "scripts")
    frame_ref = ref_for(root, "source/frames.json", "frames")
    jpeg_ref = ref_for(root, "rpa/peak-frames/m-1/01_2s.jpg", "jpeg")
    materials = [
        {
            "rank": 1,
            "materialId": "m-1",
            "materialName": "素材一",
            "materialType": "视频",
            "videoType": "短视频",
            "rankingMetric": "stat_cost_for_overall_roi2",
            "sortValue": "100",
            "metrics": {"stat_cost_for_overall_roi2": "100", "video_like_count_for_roi2": "8"},
            "sourceRef": source_ref,
        },
        {
            "rank": 2,
            "materialId": "m-2",
            "materialName": "素材二",
            "materialType": "视频",
            "videoType": "短视频",
            "rankingMetric": "stat_cost_for_overall_roi2",
            "sortValue": "80",
            "metrics": {"stat_cost_for_overall_roi2": "80", "video_like_count_for_roi2": "4"},
            "sourceRef": source_ref,
        },
    ]
    audience = [
        {"materialId": "m-1", "dimensionName": "年龄", "audienceValue": {"ValueStr": "25-34岁"}, "metricValue": 10, "metricPercentage": "20", "sourceRef": audience_ref},
        {"materialId": "m-2", "dimensionName": "年龄", "audienceValue": {"ValueStr": "35-44岁"}, "metricValue": 8, "metricPercentage": "16", "sourceRef": audience_ref},
    ]
    scripts = [{"materialId": "m-1", "parseStatus": "parsed", "text": "原文片段一", "detail": {"1": {"list": ["原文片段一"]}}, "sourceRef": script_ref}]
    frames = [{"materialId": "m-1", "frameSequence": 1, "durationSec": 2, "artifact": jpeg_ref, "selectionEvidence": [{"metric": "LIKE_COUNT", "metricValue": 3}], "sourceRef": frame_ref}]
    fact_dir = root / "fact-pack"
    fact_dir.mkdir(parents=True)
    (fact_dir / "material-leaderboard.json").write_text(json.dumps({"materials": materials}, ensure_ascii=False), encoding="utf-8")
    (fact_dir / "audience.json").write_text(json.dumps({"status": "available", "audience": audience}, ensure_ascii=False), encoding="utf-8")
    (fact_dir / "scripts.json").write_text(json.dumps({"status": "available", "scripts": scripts}, ensure_ascii=False), encoding="utf-8")
    (fact_dir / "highlight-frames.json").write_text(json.dumps({"status": "available", "highlightFrames": frames}, ensure_ascii=False), encoding="utf-8")
    (fact_dir / "manifest.json").write_text(json.dumps({"targetTopN": 2, "materialIds": ["m-1", "m-2"]}, ensure_ascii=False), encoding="utf-8")
    return fact_dir


class AnalysisSpecTests(unittest.TestCase):
    def test_loads_capability_registry_without_real_results(self):
        registry = load_analysis_capability_registry(ROOT / "config" / "analysis-capability-registry-v1.json")
        self.assertIn("成交质量比较", registry["capabilities"])
        self.assertNotIn("materials", json.dumps(registry, ensure_ascii=False))
    def test_resolves_fact_retrieval_spec_and_rejects_query_fields(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        manifest_hash = "a" * 64
        payload = {
            "schemaVersion": 1,
            "runId": "run-1",
            "factPackManifestSha256": manifest_hash,
            "retrievalGoal": "比较成交质量并解释退款差异",
            "sections": ["material_leaderboard", "audience"],
            "materialScope": {"type": "top_n", "value": 2},
            "fields": [
                {"section": "material_leaderboard", "field": "metrics.total_prepay_and_pay_settle_roi2_7d", "aggregation": "value"},
                {"section": "audience", "field": "metricPercentage", "aggregation": "value"},
            ],
            "filters": [{"section": "audience", "field": "dimensionName", "operator": "eq", "value": "八大人群"}],
            "groupComparisons": [{"section": "audience", "groupBy": "audienceValue", "aggregations": ["count", "avg"]}],
            "calculations": [],
            "evidenceStrategy": {"maxRows": 20},
        }
        normalized = resolve_fact_retrieval_spec(
            payload,
            registry,
            fact_pack(),
            run_id="run-1",
            manifest_sha256=manifest_hash,
        )
        self.assertEqual(normalized["retrievalGoal"], payload["retrievalGoal"])
        self.assertEqual(normalized["materialScope"], {"type": "top_n", "value": 2})
        self.assertEqual(normalized["filters"][0]["operator"], "eq")
        with self.assertRaisesRegex(AnalysisError, "forbidden|QuerySpec|period"):
            resolve_fact_retrieval_spec({**payload, "period": {"startDate": "2026-01-01", "endDate": "2026-01-02"}}, registry, fact_pack(), run_id="run-1", manifest_sha256=manifest_hash)

    def test_retrieves_filtered_rows_without_turning_missing_into_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            registry = load_analysis_registry(REGISTRY_PATH)
            manifest_hash = hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest()
            payload = {
                "schemaVersion": 1,
                "runId": root.name,
                "factPackManifestSha256": manifest_hash,
                "retrievalGoal": "比较素材表现",
                "sections": ["material_leaderboard", "audience"],
                "materialScope": {"type": "all"},
                "fields": [
                    {"section": "material_leaderboard", "field": "metrics.stat_cost_for_overall_roi2", "aggregation": "value"},
                    {"section": "material_leaderboard", "field": "metrics.total_cpc_for_roi2", "aggregation": "value"},
                    {"section": "audience", "field": "metricValue", "aggregation": "value"},
                ],
                "filters": [{"section": "audience", "field": "dimensionName", "operator": "eq", "value": "年龄"}],
                "groupComparisons": [],
                "calculations": [],
                "evidenceStrategy": {"maxRows": 1},
            }
            spec = resolve_fact_retrieval_spec(payload, registry, fact_pack(json.loads((fact_dir / "material-leaderboard.json").read_text())["materials"]), run_id=root.name, manifest_sha256=manifest_hash)
            evidence = retrieve_fact_slice(root, fact_dir, spec, registry)
        self.assertTrue(any(item["code"] == "field_missing" for item in evidence["missingItems"]))
        self.assertTrue(any(item["code"] == "evidence_truncated" for item in evidence["missingItems"]))
        self.assertIn("retrievalTrace", evidence)
    def test_validates_single_section_analysis_handoff_and_normalizes_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            manifest = json.loads((fact_dir / "manifest.json").read_text(encoding="utf-8"))
            manifest_hash = hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest()
            request = {
                "stage": "analysis",
                "runId": root.name,
                "factPackManifestSha256": manifest_hash,
                "sections": ["material_leaderboard"],
                "materialScope": {"type": "top_n", "value": 1},
                "requestText": "分析高消耗素材",
                "analysisSpec": spec("material_leaderboard", selection={"rankRange": {"start": 1, "end": 1}}),
            }

            normalized = validate_analysis_handoff(request, fact_dir)

        self.assertEqual(normalized["stage"], "analysis")
        self.assertEqual(normalized["runId"], root.name)
        self.assertEqual(normalized["factPackManifestSha256"], manifest_hash)
        self.assertEqual(normalized["sections"], ["material_leaderboard"])
        self.assertEqual(normalized["selection"], {"rankRange": {"start": 1, "end": 1}})
        self.assertEqual(normalized["manifestSummary"]["counts"], manifest.get("counts", {}))

    def test_prepare_analysis_accepts_bound_handoff_and_marks_analysis_prepared(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            request = {
                "stage": "analysis",
                "runId": root.name,
                "factPackManifestSha256": hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest(),
                "sections": ["material_leaderboard"],
                "materialScope": {"type": "top_n", "value": 1},
                "requestText": "分析高消耗素材",
                "analysisSpec": spec("material_leaderboard", selection={"rankRange": {"start": 1, "end": 1}}),
            }
            request_path = root / "analysis-request.json"
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")

            with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}):
                receipt = prepare_analysis(root, request_path, root / "analysis")
            model_input = json.loads((root / "analysis" / "analysis-input.json").read_text(encoding="utf-8"))

        self.assertEqual(receipt["stage"], "analysis_prepared")
        self.assertEqual(receipt["factPackManifestSha256"], request["factPackManifestSha256"])
        self.assertEqual(receipt["status"], "awaiting_model")
        self.assertEqual(receipt["sourceRefs"], model_input["sourceRefs"])

    def test_prepare_analysis_accepts_fact_retrieval_spec_and_emits_trace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            manifest_hash = hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest()
            request = {
                "stage": "analysis",
                "runId": root.name,
                "factPackManifestSha256": manifest_hash,
                "sections": ["material_leaderboard"],
                "materialScope": {"type": "all"},
                "requestText": "比较 7 日 ROI",
                "factRetrievalSpec": {
                    "schemaVersion": 1,
                    "runId": root.name,
                    "factPackManifestSha256": manifest_hash,
                    "retrievalGoal": "比较 7 日 ROI",
                    "sections": ["material_leaderboard"],
                    "materialScope": {"type": "all"},
                    "fields": [{"section": "material_leaderboard", "field": "metrics.stat_cost_for_overall_roi2", "aggregation": "value"}],
                    "filters": [], "groupComparisons": [], "calculations": [], "evidenceStrategy": {"maxRows": 10}
                }
            }
            request_path = root / "analysis-request.json"
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}):
                receipt = prepare_analysis(root, request_path, root / "analysis")
            model_input = json.loads((root / "analysis" / "analysis-input.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "awaiting_model")
        self.assertIn("factRetrievalSpec", model_input)
        self.assertTrue(model_input["retrievalTrace"])

    def test_real_run_replay_is_offline_and_preserves_missing_audience_metrics(self):
        run_dir = ROOT / "runs" / "run-20260826-20260830"
        fact_dir = run_dir / "fact-pack"
        manifest_hash = hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest()
        request = {
            "stage": "analysis",
            "runId": run_dir.name,
            "factPackManifestSha256": manifest_hash,
            "sections": ["material_leaderboard", "audience", "scripts"],
            "materialScope": {"type": "all"},
            "requestText": "比较三个素材的成交质量，重点看 7 日 ROI、退款率、完播率，并结合脚本和人群解释差异",
            "factRetrievalSpec": {
                "schemaVersion": 1,
                "runId": run_dir.name,
                "factPackManifestSha256": manifest_hash,
                "retrievalGoal": "比较三个素材的成交质量，重点看 7 日 ROI、退款率、完播率，并结合脚本和人群解释差异",
                "sections": ["material_leaderboard", "audience", "scripts"],
                "materialScope": {"type": "all"},
                "fields": [
                    {"section": "material_leaderboard", "field": "metrics.total_prepay_and_pay_settle_roi2_7d", "aggregation": "value"},
                    {"section": "material_leaderboard", "field": "metrics.total_refund_order_gmv_for_roi2_1h_rate", "aggregation": "value"},
                    {"section": "material_leaderboard", "field": "metrics.video_play_finish_rate_for_roi2_v2", "aggregation": "value"},
                    {"section": "audience", "field": "dimensionName", "aggregation": "value"},
                    {"section": "audience", "field": "audienceValue", "aggregation": "value"},
                    {"section": "audience", "field": "metricValue", "aggregation": "value"},
                    {"section": "scripts", "field": "text", "aggregation": "value"}
                ],
                "filters": [], "groupComparisons": [], "calculations": [], "evidenceStrategy": {"maxRows": 200}
            }
        }
        request_path = run_dir / "analysis-request-replay-test.json"
        try:
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}), patch("analysis.fetch_material_report", side_effect=AssertionError("network must not be called"), create=True):
                receipt = prepare_analysis(run_dir, request_path, run_dir / "analysis-replay-test")
            model_input = json.loads((run_dir / "analysis-replay-test" / "analysis-input.json").read_text(encoding="utf-8"))
        finally:
            request_path.unlink(missing_ok=True)
            shutil.rmtree(run_dir / "analysis-replay-test", ignore_errors=True)
        self.assertEqual(receipt["status"], "awaiting_model_with_missing_data")
        self.assertTrue(receipt["missingItems"])
        self.assertEqual(len(model_input["factRetrievalSpec"]["selectedMaterialIds"]), 3)
        self.assertTrue(model_input["retrievalTrace"])

    def test_validates_multi_section_analysis_handoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            request = {
                "stage": "analysis",
                "runId": root.name,
                "factPackManifestSha256": hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest(),
                "sections": ["material_leaderboard", "audience"],
                "materialScope": {"type": "all"},
                "requestText": "比较榜单与人群",
                "analysisSpec": spec("material_leaderboard", "audience"),
            }

            normalized = validate_analysis_handoff(request, fact_dir)

        self.assertEqual(normalized["sections"], ["material_leaderboard", "audience"])
        self.assertEqual(normalized["selection"], {"rankRange": {"start": 1, "end": 2}})

    def test_rejects_analysis_handoff_for_missing_section(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            (fact_dir / "audience.json").unlink()
            request = {
                "stage": "analysis",
                "runId": root.name,
                "factPackManifestSha256": hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest(),
                "sections": ["audience"],
                "materialScope": {"type": "all"},
                "requestText": "分析人群",
                "analysisSpec": spec("audience"),
            }

            with self.assertRaisesRegex(AnalysisError, "section.*unavailable|missing"):
                validate_analysis_handoff(request, fact_dir)

    def test_rejects_analysis_handoff_when_manifest_hash_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            manifest_path = fact_dir / "manifest.json"
            original_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            request = {
                "stage": "analysis",
                "runId": root.name,
                "factPackManifestSha256": original_hash,
                "sections": ["material_leaderboard"],
                "materialScope": {"type": "all"},
                "requestText": "分析榜单",
                "analysisSpec": spec("material_leaderboard"),
            }
            manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            with self.assertRaisesRegex(AnalysisError, "manifest.*mismatch|hash"):
                validate_analysis_handoff(request, fact_dir)

    def test_versioned_fixture_covers_three_requested_offline_scenarios(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "analysis" / "scenario-contracts.json").read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in fixture["scenarios"]], ["leaderboard-only", "audience-and-leaderboard", "all-sections"])
        self.assertEqual(fixture["scenarios"][2]["sections"], ["material_leaderboard", "audience", "scripts", "highlight_frames"])
        self.assertTrue(fixture["scenarios"][2]["requiresJpegArtifactEvidence"])

    def test_resolves_single_section_spec_with_stable_fingerprint(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        resolved = resolve_analysis_spec(spec("material_leaderboard"), registry, fact_pack())

        self.assertEqual(resolved["sections"], ["material_leaderboard"])
        self.assertEqual(resolved["selection"]["rankRange"], {"start": 1, "end": 3})
        self.assertEqual(resolved["metrics"][0]["field"], "metrics.stat_cost_for_overall_roi2")
        self.assertEqual(analysis_fingerprint(resolved), analysis_fingerprint(resolved))

    def test_resolves_all_four_sections(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        resolved = resolve_analysis_spec(
            spec("material_leaderboard", "audience", "scripts", "highlight_frames"), registry, fact_pack()
        )

        self.assertEqual(
            resolved["sections"], ["material_leaderboard", "audience", "scripts", "highlight_frames"]
        )

    def test_rejects_unknown_field(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        payload = spec("material_leaderboard")
        payload["metrics"][0]["field"] = "metrics.not_a_fact_field"

        with self.assertRaisesRegex(AnalysisError, "unknown analysis field"):
            resolve_analysis_spec(payload, registry, fact_pack())

    def test_rejects_material_id_outside_fact_pack(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        payload = spec("material_leaderboard", selection={"materialIds": ["m-99"]})

        with self.assertRaisesRegex(AnalysisError, "outside fact pack"):
            resolve_analysis_spec(payload, registry, fact_pack())

    def test_rejects_invalid_rank_range(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        payload = spec("material_leaderboard", selection={"rankRange": {"start": 3, "end": 1}})

        with self.assertRaisesRegex(AnalysisError, "rankRange"):
            resolve_analysis_spec(payload, registry, fact_pack())

    def test_rejects_undeclared_or_unsupported_calculation(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        payload = spec("material_leaderboard")
        payload["calculations"] = [{"id": "x", "formula": "invented_formula", "inputs": ["rank"]}]

        with self.assertRaisesRegex(AnalysisError, "unsupported calculation formula"):
            resolve_analysis_spec(payload, registry, fact_pack())

    def test_rejects_calculation_input_that_is_not_a_declared_metric(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        payload = spec("material_leaderboard")
        payload["calculations"] = [{"id": "x", "formula": "difference", "inputs": ["rank", "metrics.not_a_fact_field"]}]

        with self.assertRaisesRegex(AnalysisError, "unknown analysis field"):
            resolve_analysis_spec(payload, registry, fact_pack())

    def test_rejects_missing_analysis_question_and_output_contract(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        payload = spec("material_leaderboard")
        del payload["questions"]

        with self.assertRaisesRegex(AnalysisError, "questions"):
            resolve_analysis_spec(payload, registry, fact_pack())

    def test_accepts_transcript_and_frame_fields_only_in_their_sections(self):
        registry = load_analysis_registry(REGISTRY_PATH)
        payload = spec("scripts", "highlight_frames")
        payload["metrics"] = [
            {"section": "scripts", "field": "text", "aggregation": "value"},
            {"section": "highlight_frames", "field": "durationSec", "aggregation": "value"},
        ]
        resolved = resolve_analysis_spec(payload, registry, fact_pack())

        self.assertEqual(len(resolved["metrics"]), 2)


class AnalysisInputTests(unittest.TestCase):
    def test_compiles_complete_model_handoff_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            registry = load_analysis_registry(REGISTRY_PATH)
            payload = spec("material_leaderboard", selection={"rankRange": {"start": 1, "end": 1}})
            materials = json.loads((fact_dir / "material-leaderboard.json").read_text())["materials"]
            resolved = resolve_analysis_spec(payload, registry, {"materialLeaderboard": {"materials": materials}})
            evidence = slice_fact_pack(root, fact_dir, resolved, registry)
            model_input = compile_model_input(root, resolved, evidence, registry, request_text="分析高消耗素材并给出建议")
            manifest_hash = hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest()

        self.assertEqual(model_input["requestText"], "分析高消耗素材并给出建议")
        self.assertEqual(model_input["factPackManifestSha256"], manifest_hash)
        self.assertEqual(model_input["factPack"]["manifestSha256"], manifest_hash)
        self.assertEqual(model_input["modelConstraints"]["allowedMaterialIds"], ["m-1"])
        self.assertTrue(model_input["modelConstraints"]["doNotUseExternalData"])
        self.assertTrue(model_input["modelConstraints"]["doNotFillMissingData"])
        self.assertEqual(model_input["resultSchema"]["required"], ["schemaVersion", "contractVersion", "analysisSpecFingerprint", "claims"])
        self.assertEqual(
            model_input["resultSchema"]["claimRequired"],
            ["claimType", "statement", "section", "priority", "evidenceRefs", "confidence"],
        )

    def test_slices_only_requested_leaderboard_fields_and_selected_rank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            registry = load_analysis_registry(REGISTRY_PATH)
            payload = spec("material_leaderboard", selection={"rankRange": {"start": 1, "end": 1}})
            resolved = resolve_analysis_spec(payload, registry, {"materialLeaderboard": {"materials": json.loads((fact_dir / "material-leaderboard.json").read_text())["materials"]}})
            evidence = slice_fact_pack(root, fact_dir, resolved, registry)

        rows = evidence["sections"]["material_leaderboard"]["rows"]
        self.assertEqual([row["materialId"] for row in rows], ["m-1"])
        self.assertEqual(rows[0]["fields"], {"metrics.stat_cost_for_overall_roi2": "100"})
        self.assertEqual(rows[0]["sourceRef"]["path"], "source/materials.json")
        self.assertNotIn("video_like_count_for_roi2", json.dumps(evidence, ensure_ascii=False))

    def test_compiles_category_prompts_with_source_manifest_and_grouping_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            registry = load_analysis_registry(REGISTRY_PATH)
            payload = spec("material_leaderboard", "audience", "scripts", "highlight_frames")
            payload["metrics"] = [
                {"section": "material_leaderboard", "field": "metrics.stat_cost_for_overall_roi2", "aggregation": "value"},
                {"section": "audience", "field": "metricPercentage", "aggregation": "value"},
                {"section": "scripts", "field": "text", "aggregation": "value"},
                {"section": "highlight_frames", "field": "artifact", "aggregation": "value"},
            ]
            payload["groupComparisons"] = [{"section": "audience", "groupBy": "audienceValue", "groups": ["25-34岁", "35-44岁"]}]
            materials = json.loads((fact_dir / "material-leaderboard.json").read_text())["materials"]
            resolved = resolve_analysis_spec(payload, registry, {"materialLeaderboard": {"materials": materials}})
            evidence = slice_fact_pack(root, fact_dir, resolved, registry)
            model_input = compile_model_input(root, resolved, evidence, registry)

        self.assertEqual(model_input["promptVersion"], "qianchuan-analysis-prompt-v2")
        self.assertEqual(set(model_input["sections"]), {"material_leaderboard", "audience", "scripts", "highlight_frames"})
        self.assertIn("do not infer causality", model_input["sections"]["audience"]["prompt"])
        self.assertIn("source/materials.json", json.dumps(model_input, ensure_ascii=False))
        self.assertIn("source/frames.json", json.dumps(model_input, ensure_ascii=False))

    def test_model_input_carries_declared_calculation_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            registry = load_analysis_registry(REGISTRY_PATH)
            payload = spec("material_leaderboard")
            payload["metrics"] = [
                {"section": "material_leaderboard", "field": "metrics.stat_cost_for_overall_roi2", "aggregation": "value"},
                {"section": "material_leaderboard", "field": "metrics.video_like_count_for_roi2", "aggregation": "value"},
            ]
            payload["calculations"] = [{"id": "cost_minus_likes", "formula": "difference", "inputs": ["metrics.stat_cost_for_overall_roi2", "metrics.video_like_count_for_roi2"]}]
            materials = json.loads((fact_dir / "material-leaderboard.json").read_text())["materials"]
            resolved = resolve_analysis_spec(payload, registry, {"materialLeaderboard": {"materials": materials}})
            evidence = slice_fact_pack(root, fact_dir, resolved, registry)
            model_input = compile_model_input(root, resolved, evidence, registry)

        self.assertEqual(model_input["calculations"][0]["id"], "cost_minus_likes")
        self.assertEqual(model_input["calculations"][0]["formula"], "difference")

    def test_records_missing_selected_materials_for_a_partial_category(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            registry = load_analysis_registry(REGISTRY_PATH)
            payload = spec("scripts")
            payload["metrics"] = [{"section": "scripts", "field": "text", "aggregation": "value"}]
            materials = json.loads((fact_dir / "material-leaderboard.json").read_text())["materials"]
            resolved = resolve_analysis_spec(payload, registry, {"materialLeaderboard": {"materials": materials}})
            evidence = slice_fact_pack(root, fact_dir, resolved, registry)

        self.assertTrue(any(item.get("materialId") == "m-2" and item["code"] == "no_category_record" for item in evidence["missingItems"]))

    def test_emits_declared_calculation_with_formula_and_input_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            registry = load_analysis_registry(REGISTRY_PATH)
            payload = spec("material_leaderboard")
            payload["metrics"] = [
                {"section": "material_leaderboard", "field": "metrics.stat_cost_for_overall_roi2", "aggregation": "value"},
                {"section": "material_leaderboard", "field": "metrics.video_like_count_for_roi2", "aggregation": "value"},
            ]
            payload["calculations"] = [{"id": "cost_minus_likes", "formula": "difference", "inputs": ["metrics.stat_cost_for_overall_roi2", "metrics.video_like_count_for_roi2"]}]
            materials = json.loads((fact_dir / "material-leaderboard.json").read_text())["materials"]
            resolved = resolve_analysis_spec(payload, registry, {"materialLeaderboard": {"materials": materials}})
            evidence = slice_fact_pack(root, fact_dir, resolved, registry)

        calculation = evidence["calculations"][0]
        self.assertEqual(calculation["formula"], "difference")
        self.assertEqual(calculation["inputs"], ["metrics.stat_cost_for_overall_roi2", "metrics.video_like_count_for_roi2"])
        self.assertEqual(calculation["values"]["m-1"], 92)


class AnalysisResultTests(unittest.TestCase):
    def model_input(self, root):
        source_ref = ref_for(root, "source/facts.json", "facts")
        return {
            "schemaVersion": 1,
            "inputType": "qianchuan_analysis_model_input",
            "promptVersion": "qianchuan-analysis-prompt-v1",
            "analysisSpecFingerprint": "a" * 64,
            "sourceRefs": [source_ref],
            "sections": {"material_leaderboard": {"evidence": {"rows": [{"sourceRef": source_ref}]}}},
            "resultSchema": {"claimRequired": ["claimType", "statement", "section", "priority", "evidenceRefs", "confidence"]},
        }, source_ref

    def sectioned_model_input(self, root):
        model_input, source_ref = self.model_input(root)
        model_input["promptVersion"] = "qianchuan-analysis-prompt-v2"
        model_input["analysisSpec"] = {"sections": ["material_leaderboard", "audience"]}
        return model_input, source_ref

    def test_requires_section_and_priority_for_new_result_claims(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_input, source_ref = self.sectioned_model_input(Path(temp_dir))
            result = {
                "schemaVersion": 1,
                "contractVersion": 2,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"statement": "事实", "claimType": "fact", "evidenceRefs": [source_ref], "confidence": 1}],
            }

            with self.assertRaisesRegex(AnalysisError, "section"):
                validate_analysis_result(result, model_input, model_input["analysisSpec"], Path(temp_dir))

    def test_rejects_claim_section_outside_selected_sections_and_synthesis(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_input, source_ref = self.sectioned_model_input(Path(temp_dir))
            result = {
                "schemaVersion": 1,
                "contractVersion": 2,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"statement": "事实", "claimType": "fact", "section": "scripts", "priority": "high", "evidenceRefs": [source_ref], "confidence": 1}],
            }

            with self.assertRaisesRegex(AnalysisError, "section"):
                validate_analysis_result(result, model_input, model_input["analysisSpec"], Path(temp_dir))

    def test_requires_priority_and_business_implication_for_new_recommendations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_input, source_ref = self.sectioned_model_input(Path(temp_dir))
            result = {
                "schemaVersion": 1,
                "contractVersion": 2,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"statement": "建议", "claimType": "recommendation", "section": "audience", "evidenceRefs": [source_ref], "confidence": 1}],
            }

            with self.assertRaisesRegex(AnalysisError, "priority"):
                validate_analysis_result(result, model_input, model_input["analysisSpec"], Path(temp_dir))

            result["claims"][0]["priority"] = "high"
            with self.assertRaisesRegex(AnalysisError, "businessImplication"):
                validate_analysis_result(result, model_input, model_input["analysisSpec"], Path(temp_dir))

    def test_compile_model_input_describes_sectioned_claim_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_input, _ = self.model_input(root)

        self.assertEqual(model_input["promptVersion"], "qianchuan-analysis-prompt-v1")
        # This assertion intentionally specifies the new contract and fails before implementation.
        self.assertIn("section", model_input["resultSchema"]["claimRequired"])
        self.assertIn("priority", model_input["resultSchema"]["claimRequired"])

    def test_rejects_claim_without_required_audit_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_input, _source_ref = self.model_input(Path(temp_dir))
            result = {"schemaVersion": 1, "analysisSpecFingerprint": "a" * 64, "claims": [{"statement": "事实"}]}

            with self.assertRaisesRegex(AnalysisError, "claimType"):
                validate_analysis_result(result, model_input, {"sections": ["material_leaderboard"]}, Path(temp_dir))

    def test_rejects_calculation_without_formula_and_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_input, source_ref = self.model_input(Path(temp_dir))
            result = {
                "schemaVersion": 1,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"statement": "计算", "claimType": "calculation", "evidenceRefs": [source_ref], "confidence": 0.8}],
            }

            with self.assertRaisesRegex(AnalysisError, "formula and inputs"):
                validate_analysis_result(result, model_input, {"sections": ["material_leaderboard"]}, Path(temp_dir))

    def test_rejects_evidence_ref_not_present_in_model_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_input, source_ref = self.model_input(Path(temp_dir))
            result = {
                "schemaVersion": 1,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"statement": "事实", "claimType": "fact", "evidenceRefs": [{**source_ref, "sha256": "b" * 64}], "confidence": 1}],
            }

            with self.assertRaisesRegex(AnalysisError, "not present in model input"):
                validate_analysis_result(result, model_input, {"sections": ["material_leaderboard"]}, Path(temp_dir))

    def test_rejects_result_calculation_that_was_not_declared(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_input, source_ref = self.model_input(Path(temp_dir))
            spec_payload = {"sections": ["material_leaderboard"], "calculations": [{"id": "declared", "formula": "ratio", "inputs": ["a", "b"]}]}
            result = {
                "schemaVersion": 1,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"statement": "计算", "claimType": "calculation", "calculationId": "invented", "formula": {"name": "ratio"}, "inputs": [{"field": "a"}, {"field": "b"}], "evidenceRefs": [source_ref], "confidence": 0.8}],
            }

            with self.assertRaisesRegex(AnalysisError, "not declared"):
                validate_analysis_result(result, model_input, spec_payload, Path(temp_dir))

    def test_rejects_empty_claims_and_any_calculation_when_spec_declares_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_input, source_ref = self.model_input(Path(temp_dir))
            empty = {"schemaVersion": 1, "analysisSpecFingerprint": "a" * 64, "claims": []}
            with self.assertRaisesRegex(AnalysisError, "at least one claim"):
                validate_analysis_result(empty, model_input, {"sections": ["material_leaderboard"], "calculations": []}, Path(temp_dir))
            undeclared = {
                "schemaVersion": 1,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"statement": "计算", "claimType": "calculation", "formula": {"name": "ratio"}, "inputs": [{"field": "a"}, {"field": "b"}], "evidenceRefs": [source_ref], "confidence": 0.8}],
            }
            with self.assertRaisesRegex(AnalysisError, "not declared"):
                validate_analysis_result(undeclared, model_input, {"sections": ["material_leaderboard"], "calculations": []}, Path(temp_dir))

    def _prepared_analysis(self, root):
        fact_dir = analysis_fact_pack(root)
        request = {
            "stage": "analysis",
            "runId": root.name,
            "factPackManifestSha256": hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest(),
            "sections": ["material_leaderboard"],
            "materialScope": {"type": "top_n", "value": 1},
            "requestText": "分析高消耗素材",
            "analysisSpec": spec("material_leaderboard", selection={"rankRange": {"start": 1, "end": 1}}),
        }
        request_path = root / "request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}):
            prepare_analysis(root, request_path, root / "analysis")
        return json.loads((root / "analysis" / "analysis-input.json").read_text(encoding="utf-8"))

    def test_receives_valid_model_result_and_verifies_before_rendering_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_input = self._prepared_analysis(root)
            result_path = root / "model-result.json"
            result_path.write_text(json.dumps({
                "schemaVersion": 1,
                "analysisSpecFingerprint": model_input["analysisSpecFingerprint"],
                "claims": [{
                    "claimType": "fact",
                    "statement": "素材一的消耗为 100",
                    "evidenceRefs": [model_input["sections"]["material_leaderboard"]["evidence"]["rows"][0]["sourceRef"]],
                    "confidence": 1,
                }],
            }, ensure_ascii=False), encoding="utf-8")

            with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}):
                outcome = receive_analysis_result(root, result_path)

            receipt = json.loads((root / "analysis" / "analysis-receipt.json").read_text(encoding="utf-8"))
            report_exists = (root / "analysis" / "report.json").is_file()

        self.assertEqual(outcome["status"], "succeeded")
        self.assertTrue(report_exists)
        self.assertEqual(receipt["status"], "succeeded")

    def test_reuses_verified_result_when_result_hash_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_input = self._prepared_analysis(root)
            result_path = root / "model-result.json"
            result_path.write_text(json.dumps({
                "schemaVersion": 1,
                "analysisSpecFingerprint": model_input["analysisSpecFingerprint"],
                "claims": [{
                    "claimType": "fact",
                    "statement": "素材一的消耗为 100",
                    "evidenceRefs": [model_input["sections"]["material_leaderboard"]["evidence"]["rows"][0]["sourceRef"]],
                    "confidence": 1,
                }],
            }, ensure_ascii=False), encoding="utf-8")

            with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}), patch(
                "analysis.verify_analysis", wraps=verify_analysis
            ) as verify:
                first = receive_analysis_result(root, result_path)
                second = receive_analysis_result(root, result_path)

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertTrue(second["reused"])
        self.assertEqual(verify.call_count, 1)

    def test_missing_or_invalid_model_result_stays_awaiting_model_without_report(self):
        for invalid_payload in (None, "not-json"):
            with self.subTest(invalid_payload=invalid_payload), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self._prepared_analysis(root)
                result_path = root / "model-result.json"
                if invalid_payload is not None:
                    result_path.write_text(invalid_payload, encoding="utf-8")

                outcome = receive_analysis_result(root, result_path)
                receipt = json.loads((root / "analysis" / "analysis-receipt.json").read_text(encoding="utf-8"))

                self.assertEqual(outcome["status"], "awaiting_model")
                self.assertEqual(receipt["status"], "awaiting_model")
                self.assertFalse((root / "analysis" / "analysis-result.json").exists())
                self.assertFalse((root / "analysis" / "report.json").exists())

    def test_rejects_model_result_with_evidence_not_in_input_without_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_input = self._prepared_analysis(root)
            result_path = root / "model-result.json"
            result_path.write_text(json.dumps({
                "schemaVersion": 1,
                "analysisSpecFingerprint": model_input["analysisSpecFingerprint"],
                "claims": [{
                    "claimType": "fact",
                    "statement": "越权事实",
                    "evidenceRefs": [{"path": "source/unknown.json", "sha256": "a" * 64, "sizeBytes": 1}],
                    "confidence": 1,
                }],
            }, ensure_ascii=False), encoding="utf-8")

            with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}):
                outcome = receive_analysis_result(root, result_path)

            receipt = json.loads((root / "analysis" / "analysis-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(outcome["status"], "failed")
        self.assertIn("not present in model input", " ".join(outcome["errors"]))
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse((root / "analysis" / "report.json").exists())

    def test_verify_analysis_keeps_missing_model_result_awaiting_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._prepared_analysis(root)

            with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}):
                outcome = verify_analysis(root)

            receipt = json.loads((root / "analysis" / "analysis-receipt.json").read_text(encoding="utf-8"))
            report_exists = (root / "analysis" / "report.json").exists()

        self.assertEqual(outcome["status"], "awaiting_model")
        self.assertEqual(receipt["status"], "awaiting_model")
        self.assertFalse(report_exists)

    def test_verify_rejects_tampered_analysis_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            registry = load_analysis_registry(REGISTRY_PATH)
            materials = json.loads((fact_dir / "material-leaderboard.json").read_text())["materials"]
            resolved = resolve_analysis_spec(spec("material_leaderboard"), registry, {"materialLeaderboard": {"materials": materials}})
            evidence = slice_fact_pack(root, fact_dir, resolved, registry)
            model_input = compile_model_input(root, resolved, evidence, registry)
            model_input["sections"]["material_leaderboard"]["evidence"]["rows"][0]["fields"]["metrics.stat_cost_for_overall_roi2"] = "tampered"
            (root / "analysis").mkdir()
            (root / "analysis" / "analysis-input.json").write_text(json.dumps(model_input), encoding="utf-8")

            with self.assertRaisesRegex(AnalysisError, "does not match fact slice"):
                from analysis import validate_analysis_input
                validate_analysis_input(root, fact_dir, model_input, resolved, registry)

    def test_rejects_absolute_or_parent_traversal_evidence_refs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_ref = ref_for(root, "source/facts.json", "facts")
            model_input, _ = self.model_input(root)
            malicious_ref = {**source_ref, "path": "../outside.json"}
            model_input["sourceRefs"] = [malicious_ref]
            result = {
                "schemaVersion": 1,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"statement": "事实", "claimType": "fact", "evidenceRefs": [malicious_ref], "confidence": 1}],
            }

            with self.assertRaisesRegex(AnalysisError, "relative"):
                validate_analysis_result(result, model_input, {"sections": ["material_leaderboard"]}, root)

    def test_renders_separate_claim_layers_and_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_input, source_ref = self.model_input(root)
            result = {
                "schemaVersion": 1,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [
                    {"id": "f1", "statement": "事实", "claimType": "fact", "evidenceRefs": [source_ref], "confidence": 1},
                    {"id": "i1", "statement": "建议", "claimType": "recommendation", "evidenceRefs": [source_ref], "confidence": 0.4},
                ],
            }
            spec_payload = {"sections": ["material_leaderboard"], "output": {"format": "json+markdown"}}
            receipt = {"status": "succeeded", "missingItems": [], "validation": {"status": "succeeded"}}

            render_reports(root / "analysis", model_input, result, receipt)

            report = json.loads((root / "analysis" / "report.json").read_text(encoding="utf-8"))
            markdown = (root / "analysis" / "report.md").read_text(encoding="utf-8")

        self.assertEqual(report["claimsByType"]["fact"][0]["id"], "f1")
        self.assertEqual(report["claimsByType"]["recommendation"][0]["id"], "i1")
        self.assertEqual(report["resultStatus"], "verified")
        self.assertIn("source/facts.json", markdown)

    def test_renders_fixed_business_chapters_in_markdown_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_input, source_ref = self.model_input(root)
            result = {
                "schemaVersion": 1,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{
                    "id": "f1",
                    "statement": "事实",
                    "claimType": "fact",
                    "evidenceRefs": [source_ref],
                    "confidence": 1,
                    "section": "material_leaderboard",
                }],
            }
            render_reports(root / "analysis", model_input, result, {"status": "succeeded", "missingItems": []})
            report = json.loads((root / "analysis" / "report.json").read_text(encoding="utf-8"))
            markdown = (root / "analysis" / "report.md").read_text(encoding="utf-8")

        chapter_ids = [chapter["id"] for chapter in report["chapters"]]
        expected = [
            "executive_summary", "recommendations", "material_leaderboard", "audience",
            "scripts", "highlight_frames", "synthesis", "limitations", "evidence_index", "claim_audit",
        ]
        self.assertEqual(chapter_ids, expected)
        positions = [markdown.index("## " + chapter["title"]) for chapter in report["chapters"]]
        self.assertEqual(positions, sorted(positions))

    def test_legacy_claims_are_classified_by_source_and_ambiguous_claims_go_to_synthesis(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            refs = [
                ref_for(root, "source/material-leaderboard.json", "material"),
                ref_for(root, "source/audience.json", "audience"),
                ref_for(root, "source/scripts.json", "scripts"),
                ref_for(root, "source/highlight-frames.json", "frames"),
            ]
            model_input, _ = self.model_input(root)
            model_input["sourceRefs"] = refs
            claims = [{"claimType": "fact", "statement": label, "evidenceRefs": [ref], "confidence": 1} for label, ref in zip(("素材", "人群", "脚本", "高光"), refs)]
            claims.append({"claimType": "inference", "statement": "跨分区", "evidenceRefs": [refs[0], refs[1]], "confidence": 0.5})
            result = {"schemaVersion": 1, "analysisSpecFingerprint": "a" * 64, "claims": claims}

            normalized = validate_analysis_result(result, model_input, {"sections": ["material_leaderboard", "audience", "scripts", "highlight_frames"]}, root)

        self.assertEqual([claim["section"] for claim in normalized["claims"]], ["material_leaderboard", "audience", "scripts", "highlight_frames", "synthesis"])
        self.assertTrue(all(claim["classificationMode"] == "legacy_inferred" for claim in normalized["claims"]))

    def test_render_reports_creates_stable_evidence_index_and_claim_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_input, source_ref = self.model_input(root)
            result = {
                "schemaVersion": 1,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [{"id": "f1", "statement": "事实", "claimType": "fact", "evidenceRefs": [source_ref], "confidence": 1, "section": "material_leaderboard", "priority": "high", "businessImplication": "优先复用"}],
            }
            render_reports(root / "analysis", model_input, result, {"status": "succeeded", "missingItems": []})
            report = json.loads((root / "analysis" / "report.json").read_text(encoding="utf-8"))

        self.assertEqual(report["evidenceIndex"][0]["id"], "E1")
        self.assertEqual(report["claimAudit"][0]["evidenceIds"], ["E1"])
        self.assertEqual(report["claimAudit"][0]["classificationMode"], "explicit")

    def test_markdown_uses_business_order_and_keeps_audit_fields_in_appendices(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_input, source_ref = self.model_input(root)
            result = {
                "schemaVersion": 1,
                "contractVersion": 2,
                "analysisSpecFingerprint": "a" * 64,
                "claims": [
                    {"id": "rec-low", "statement": "低优先建议", "claimType": "recommendation", "section": "material_leaderboard", "priority": "low", "businessImplication": "观察", "evidenceRefs": [source_ref], "confidence": 0.4},
                    {"id": "fact", "statement": "素材关键发现", "claimType": "fact", "section": "material_leaderboard", "priority": "high", "businessImplication": "优先投入", "evidenceRefs": [source_ref], "confidence": 0.9},
                    {"id": "rec-high", "statement": "高优先建议", "claimType": "recommendation", "section": "synthesis", "priority": "high", "businessImplication": "立即执行", "evidenceRefs": [source_ref], "confidence": 0.8},
                ],
            }
            render_reports(root / "analysis", model_input, result, {"status": "succeeded", "missingItems": []})
            markdown = (root / "analysis" / "report.md").read_text(encoding="utf-8")
            report = json.loads((root / "analysis" / "report.json").read_text(encoding="utf-8"))

        headings = ["核心商业结论", "优先行动建议", "素材榜单分析", "人群分析", "脚本分析", "高光帧分析", "跨分区综合洞察", "数据限制与下一步", "附录 A：证据索引", "附录 B：结论审计"]
        self.assertEqual([markdown.index(f"## {heading}") for heading in headings], sorted(markdown.index(f"## {heading}") for heading in headings))
        self.assertLess(markdown.index("高优先建议"), markdown.index("低优先建议"))
        body, audit = markdown.split("## 附录 A：证据索引", 1)
        self.assertNotIn("confidence=", body)
        self.assertNotIn("source/facts.json", body)
        self.assertIn("confidence=", audit)
        self.assertIn("source/facts.json", audit)
        self.assertEqual(report["claimAudit"][1]["businessImplication"], "优先投入")

    def test_prepare_real_top6_offline_writes_model_input_and_waits_for_model(self):
        run_dir = ROOT / "runs" / "run-20260810-20260820"
        if not (run_dir / "fact-pack" / "manifest.json").is_file():
            self.skipTest("real Top6 fact pack is not present")
        with tempfile.TemporaryDirectory() as temp_dir:
            request_path = Path(temp_dir) / "analysis-request.json"
            request_path.write_text(json.dumps({"schemaVersion": 1, "requestText": "只看素材榜单", "analysisSpec": spec("material_leaderboard", selection={"rankRange": {"start": 1, "end": 2}})}, ensure_ascii=False), encoding="utf-8")
            output_dir = Path(temp_dir) / "analysis"
            receipt = prepare_analysis(run_dir, request_path, output_dir)
            self.assertEqual(receipt["status"], "awaiting_model")
            self.assertTrue((output_dir / "analysis-input.json").is_file())
            self.assertFalse((output_dir / "analysis-result.json").exists())

    def test_replays_real_run_reports_in_a_temporary_copy(self):
        run_dir = ROOT / "runs" / "run-20260810-20260820"
        if not (run_dir / "analysis" / "all-sections" / "analysis-result.json").is_file():
            self.skipTest("real analysis results are not present")
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_run = Path(temp_dir) / run_dir.name
            shutil.copytree(run_dir, copied_run)
            for name in ("leaderboard-only", "audience-and-leaderboard", "all-sections"):
                analysis_dir = copied_run / "analysis" / name
                model_input = json.loads((analysis_dir / "analysis-input.json").read_text(encoding="utf-8"))
                result = json.loads((analysis_dir / "analysis-result.json").read_text(encoding="utf-8"))
                receipt = json.loads((analysis_dir / "analysis-receipt.json").read_text(encoding="utf-8"))
                render_reports(analysis_dir, model_input, result, receipt)
                report = json.loads((analysis_dir / "report.json").read_text(encoding="utf-8"))
                markdown = (analysis_dir / "report.md").read_text(encoding="utf-8")
                self.assertEqual(len(report["claimAudit"]), len(result["claims"]))
                self.assertIn("## 附录 A：证据索引", markdown)
                self.assertIn("## 附录 B：结论审计", markdown)

    def test_prepare_analysis_reuses_successful_fact_pack_verification_when_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact_dir = analysis_fact_pack(root)
            request = {
                "stage": "analysis",
                "runId": root.name,
                "factPackManifestSha256": hashlib.sha256((fact_dir / "manifest.json").read_bytes()).hexdigest(),
                "sections": ["material_leaderboard"],
                "materialScope": {"type": "top_n", "value": 1},
                "requestText": "分析高消耗素材",
                "analysisSpec": spec("material_leaderboard", selection={"rankRange": {"start": 1, "end": 1}}),
            }
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            with patch("analysis.verify_fact_pack", return_value={"status": "succeeded", "errors": []}) as verify:
                first = prepare_analysis(root, request_path, root / "analysis")
                second = prepare_analysis(root, request_path, root / "analysis")

        self.assertEqual(first["status"], "awaiting_model")
        self.assertEqual(second["status"], "awaiting_model")
        self.assertEqual(verify.call_count, 1)

    def test_prepare_query_only_run_is_blocked_and_cannot_claim_analysis_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "top_materials.json").write_text(json.dumps({"collection": {"targetTopN": 1}, "materials": []}), encoding="utf-8")
            request_path = root / "request.json"
            request_path.write_text(json.dumps({"schemaVersion": 1, "analysisSpec": spec("material_leaderboard")}), encoding="utf-8")

            with self.assertRaisesRegex(AnalysisError, "fact pack is incomplete"):
                prepare_analysis(root, request_path, root / "analysis")
            receipt = json.loads((root / "analysis" / "analysis-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(receipt["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
