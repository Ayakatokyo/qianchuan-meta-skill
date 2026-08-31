import hashlib
import json
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
        self.assertEqual(model_input["resultSchema"]["required"], ["schemaVersion", "analysisSpecFingerprint", "claims"])
        self.assertEqual(
            model_input["resultSchema"]["claimRequired"],
            ["claimType", "statement", "evidenceRefs", "confidence"],
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

        self.assertEqual(model_input["promptVersion"], "qianchuan-analysis-prompt-v1")
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
        }, source_ref

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
            "executive_summary", "scope_quality", "material_leaderboard", "audience",
            "scripts", "highlight_frames", "synthesis", "recommendations", "evidence_limitations",
        ]
        self.assertEqual(chapter_ids, expected)
        positions = [markdown.index("## " + chapter["title"]) for chapter in report["chapters"]]
        self.assertEqual(positions, sorted(positions))

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
