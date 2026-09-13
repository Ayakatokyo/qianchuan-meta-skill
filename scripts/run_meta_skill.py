#!/usr/bin/env python3
import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from config_specs import (
    ConfigSpecError, highlight_spec_to_policy, legacy_config_to_specs, query_spec_to_query_config,
    resolve_highlight_request, resolve_highlight_spec, resolve_query_spec, spec_fingerprint, write_resolved_specs,
)
from analysis import AnalysisError, prepare_analysis, receive_analysis_result, verify_analysis
from frame_extractor import FrameExtractionError, extract_highlight_frames
from fact_pack import FactPackError, build_fact_pack, verify_fact_pack
from field_registry import FieldRegistryError, load_field_registry
from material_query import MaterialQueryError, execute_material_query
from overview_report import OverviewReportError, write_overview
from qianchuan_report_client import ReportClientError, fetch_material_report
from rpa_adapter import RpaAdapterError, fetch_diagnosis_worker, list_rpa_accounts
from rpa_pipeline import RpaPipelineError, build_postprocessor, resolve_rpa_options, run_rpa_pipeline
from rpa_runtime import RpaRuntimeError, run_isolated
from run_receipt import RunReceipt
from run_verifier import RunVerificationError, verify_completed_run


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "asset-field-registry-v1.json"


class MetaSkillError(RuntimeError):
    pass


def _ensure_analysis_assets():
    required = (
        ROOT / "config" / "analysis-registry-v1.json",
        ROOT / "config" / "asset-field-registry-v1.json",
        ROOT / "config" / "analysis-capability-registry-v1.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise MetaSkillError("phase two configuration is incomplete; missing: %s" % ", ".join(missing))


PHASES = (
    "awaiting_query_input", "querying", "rpa_processing", "fact_pack_building", "fact_pack_verifying", "fact_pack_ready",
    "awaiting_analysis_input", "planning_fact_retrieval", "fact_retrieval_ready", "analysis_preparing", "analysis_prepared", "awaiting_model", "analysis_verifying", "analysis_verified", "report_ready",
    "query_failed", "fact_pack_failed", "fact_retrieval_failed", "analysis_failed",
)
PHASE_STATES = PHASES


_SUCCESSFUL_PHASES = {
    "querying", "rpa_processing", "fact_pack_building", "fact_pack_verifying", "fact_pack_ready",
    "awaiting_analysis_input", "planning_fact_retrieval", "fact_retrieval_ready", "analysis_preparing", "analysis_prepared", "awaiting_model",
    "analysis_verifying", "analysis_verified", "report_ready",
}
_TERMINAL_PHASES = {
    "fact_pack_ready", "awaiting_analysis_input", "analysis_prepared", "awaiting_model",
    "analysis_verified", "report_ready",
}


def _read_json(path, label, expected_type=dict):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetaSkillError("%s is unreadable" % label) from exc
    if not isinstance(payload, expected_type):
        expected_label = "object" if expected_type is dict else "list"
        raise MetaSkillError("%s must be an %s" % (label, expected_label))
    return payload


def _json_safe(value):
    if isinstance(value, Decimal):
        result = format(value, "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _load_specs(args, registry, *, require_query):
    query_spec_path = getattr(args, "query_spec", None)
    highlight_spec_path = getattr(args, "highlight_spec", None)
    if args.config and (query_spec_path or highlight_spec_path):
        raise MetaSkillError("--config cannot be combined with --query-spec or --highlight-spec")
    try:
        requested = getattr(args, "highlight_mode", None)
        enabled = {"on": True, "off": False}.get(requested)
        if args.config:
            query_spec, legacy_highlight_spec = legacy_config_to_specs(_read_json(args.config, "meta-skill config"), registry)
            highlight_request = resolve_highlight_request(legacy_highlight_spec, enabled=enabled)
        else:
            if require_query and not query_spec_path:
                raise MetaSkillError("--query-spec is required")
            query_spec = resolve_query_spec(_read_json(query_spec_path, "QuerySpec"), registry) if query_spec_path else None
            raw_highlight_spec = _read_json(highlight_spec_path, "HighlightSpec") if highlight_spec_path else None
            highlight_request = resolve_highlight_request(raw_highlight_spec, enabled=enabled)
    except (ConfigSpecError, MaterialQueryError) as exc:
        raise MetaSkillError(str(exc)) from exc
    highlight_spec = highlight_request["spec"]
    query = query_spec_to_query_config(query_spec) if query_spec is not None else None
    return query_spec, highlight_spec, query, highlight_spec_to_policy(highlight_spec) if highlight_spec is not None else None


def _write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(_json_safe(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def build_fact_pack_handoff(run_dir):
    """Build the durable, user-facing handoff from a verified fact-pack manifest."""
    run_dir = Path(run_dir).resolve()
    fact_pack_dir = run_dir / "fact-pack"
    manifest_path = fact_pack_dir / "manifest.json"
    if not manifest_path.is_file():
        raise MetaSkillError("fact pack manifest is missing")
    manifest = _read_json(manifest_path, "fact-pack manifest")
    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    return {
        "stage": "fact_pack_ready",
        "runId": run_dir.name,
        "factPackPath": "fact-pack",
        "factPackManifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "manifestSummary": {
            "schemaVersion": manifest.get("schemaVersion"),
            "targetTopN": manifest.get("targetTopN"),
            "materialIds": manifest.get("materialIds", []),
            "counts": manifest.get("counts", {}),
            "qualityStatus": quality.get("status"),
            "categoryStatuses": quality.get("categoryStatuses", {}),
        },
        "counts": manifest.get("counts", {}),
        "missingItems": quality.get("errors", []),
        "sourceRefs": manifest.get("sourceFiles", []),
    }


def _set_run_phase(run_dir, phase, **details):
    if phase not in PHASES:
        raise MetaSkillError("unknown run phase: %s" % phase)
    receipt_path = Path(run_dir) / "run-receipt.json"
    if not receipt_path.is_file():
        return
    receipt = _read_json(receipt_path, "run receipt")
    now = time.time()
    phase_states = receipt.setdefault("phaseStates", {})
    previous_phase = receipt.get("phase")
    if previous_phase and previous_phase != phase:
        previous = phase_states.setdefault(previous_phase, {"status": "running", "startedAtEpochSec": now})
        if previous.get("status") == "running":
            previous["status"] = "succeeded" if phase in _SUCCESSFUL_PHASES else "failed"
            previous["finishedAtEpochSec"] = now
            previous["durationSec"] = max(0, now - (previous.get("startedAtEpochSec") or now))
            if phase in _SUCCESSFUL_PHASES:
                receipt["lastSuccessfulPhase"] = previous_phase
    state = phase_states.setdefault(phase, {
        "status": "running", "startedAtEpochSec": now, "finishedAtEpochSec": None,
        "durationSec": None, "progress": None, "currentObject": None, "error": None,
    })
    state.setdefault("startedAtEpochSec", now)
    state.setdefault("progress", None)
    state.setdefault("currentObject", None)
    state.setdefault("error", None)
    state["status"] = "failed" if phase.endswith("failed") else "running"
    if "progress" in details:
        state["progress"] = details["progress"]
    if "currentObject" in details:
        state["currentObject"] = details["currentObject"]
    if "error" in details and details["error"] is not None:
        state["error"] = str(details["error"])
    if details.get("phaseComplete"):
        state["status"] = "succeeded"
        state["finishedAtEpochSec"] = now
        state["durationSec"] = max(0, now - (state.get("startedAtEpochSec") or now))
    if phase in _TERMINAL_PHASES or details.get("phaseComplete"):
        state["status"] = "succeeded"
        state["finishedAtEpochSec"] = now
        state["durationSec"] = max(0, now - (state.get("startedAtEpochSec") or now))
    receipt["phase"] = phase
    for key, value in details.items():
        if value is not None:
            receipt[key] = value
    if phase in _SUCCESSFUL_PHASES and (phase in _TERMINAL_PHASES or details.get("phaseComplete")):
        receipt["lastSuccessfulPhase"] = details.get("lastSuccessfulPhase", phase)
    _write_json(receipt_path, receipt)


def _load_handoff(run_dir):
    run_dir = Path(run_dir).resolve()
    handoff_path = run_dir / "analysis-handoff.json"
    if handoff_path.is_file():
        handoff = _read_json(handoff_path, "analysis handoff")
    else:
        receipt = _read_json(run_dir / "run-receipt.json", "run receipt")
        handoff = receipt.get("handoff")
    if not isinstance(handoff, dict):
        raise MetaSkillError("run is not awaiting analysis input")
    return handoff


def continue_analysis(run_dir, analysis_request, output_dir=None):
    """Resume stage two without invoking query, RPA, or fact-pack collection."""
    from analysis import validate_analysis_handoff

    run_dir = Path(run_dir).resolve()
    handoff = _load_handoff(run_dir)
    request = _read_json(analysis_request, "analysis request")
    if not isinstance(request, dict):
        raise MetaSkillError("analysis request must be an object")
    forbidden = {"scope", "period", "ranking", "candidateTopN", "shopId", "advertiserId", "rpa", "querySpec", "highlightSpec"}
    present = forbidden.intersection(request)
    if present:
        raise MetaSkillError("stage-two request contains forbidden QuerySpec fields: %s" % ", ".join(sorted(present)))
    if request.get("stage") != "analysis":
        request = {**request, "stage": "analysis"}
    for key in ("runId", "factPackManifestSha256"):
        if key not in request and key in handoff:
            request[key] = handoff[key]
    if "sections" not in request and "sections" in handoff:
        request["sections"] = handoff["sections"]
    if "materialScope" not in request:
        request["materialScope"] = {"type": "all"}
    _set_run_phase(run_dir, "planning_fact_retrieval", currentObject="FactRetrievalSpec")
    validated = validate_analysis_handoff(request, run_dir / "fact-pack")
    with tempfile.TemporaryDirectory() as temp_dir:
        normalized_request = Path(temp_dir) / "analysis-request.json"
        _write_json(normalized_request, validated)
        result = prepare_analysis(run_dir, normalized_request, Path(output_dir) if output_dir else None)
    if "factRetrievalSpec" in validated:
        _set_run_phase(
            run_dir,
            "fact_retrieval_ready",
            phaseComplete=True,
            retrievalGoal=validated["factRetrievalSpec"].get("retrievalGoal"),
        )
    phase = "analysis_prepared" if result.get("status", "").startswith("awaiting_model") else "analysis_failed"
    _set_run_phase(run_dir, phase, analysisReceipt=str((Path(output_dir) if output_dir else run_dir / "analysis") / "analysis-receipt.json"))
    return result


def _connector_order_by(query, registry):
    if not query.get("sort"):
        return None
    entry = registry.require(query["sort"]["field"])
    source = entry.get("source")
    if not isinstance(source, list) or len(source) < 2 or source[0] != "metrics":
        raise MetaSkillError("sort field must map to a direct report metric")
    return [{"field": source[1], "type": 2 if query["sort"]["direction"] == "desc" else 1}]


def collect_report_records(query, output_dir, registry):
    return fetch_material_report(
        query["shopId"],
        query["advertiserId"],
        query["period"]["startDate"],
        query["period"]["endDate"],
        output_dir=Path(output_dir) / "source-data",
        order_by=_connector_order_by(query, registry),
    )


def _top_materials_payload(result, query):
    return {
        "schemaVersion": 1,
        "actualStartDate": query["period"]["startDate"],
        "actualEndDate": query["period"]["endDate"],
        "materialCount": len(result["materials"]),
        "collection": {
            "candidateTopN": query["candidateTopN"],
            "targetTopN": query["targetTopN"],
        },
        "rankingMetric": query["sort"]["field"] if query.get("sort") else None,
        "filters": {"advertiserId": query["advertiserId"], "dimensionFilters": query["dimensionFilters"], "metricFilters": query["metricFilters"]},
        "materials": result["materials"],
    }


def _write_query_artifacts(output_dir, query_spec, highlight_spec, query, policy, registry):
    output_dir = Path(output_dir)
    write_resolved_specs(output_dir, query_spec, highlight_spec)
    fingerprint = spec_fingerprint(query_spec, highlight_spec)
    result = execute_material_query(collect_report_records(query, output_dir, registry), query, registry)
    result["receipt"]["source"] = {"type": "direct_material_report"}
    result["receipt"]["configurationFingerprint"] = fingerprint
    top_materials = _top_materials_payload(result, query)
    top_materials["configurationFingerprint"] = fingerprint
    _write_json(output_dir / "top_materials.json", top_materials)
    _write_json(output_dir / "query-receipt.json", result["receipt"])
    _write_json(output_dir / "highlight-policy.json", policy)
    return top_materials, fingerprint


def _reusable_top_materials(output_dir, fingerprint):
    output_dir = Path(output_dir)
    top_path = output_dir / "top_materials.json"
    fingerprint_path = output_dir / "configuration-fingerprint.json"
    if not top_path.exists() and not fingerprint_path.exists():
        return None
    if not top_path.exists() or not fingerprint_path.exists():
        raise MetaSkillError("existing query artifacts are incomplete and cannot be reused")
    persisted = _read_json(fingerprint_path, "configuration fingerprint")
    if persisted.get("configurationFingerprint") != fingerprint:
        raise MetaSkillError("configuration fingerprint does not match existing query artifacts")
    payload = _read_json(top_path, "top materials")
    if payload.get("configurationFingerprint") != fingerprint:
        raise MetaSkillError("top materials fingerprint does not match existing query artifacts")
    return payload


def _rpa_options_from_args(args):
    values = {
        "rpaConcurrency": args.rpa_concurrency,
        "materialTimeoutSec": args.material_timeout_sec,
        "csvWaitWindowSec": args.csv_wait_window_sec,
        "recoveryAttempts": args.recovery_attempts,
        "recoveryBackoffSec": args.recovery_backoff_sec,
        "csvMaxBytes": args.csv_max_bytes,
        "videoMaxBytes": args.video_max_bytes,
        "downloadDeadlineSec": args.download_deadline_sec,
    }
    return resolve_rpa_options({key: value for key, value in values.items() if value is not None})


def build_parser():
    parser = argparse.ArgumentParser(description="Query Qianchuan assets, run authorized diagnosis, and build a redacted fact pack.")
    commands = parser.add_subparsers(dest="command", required=True)
    render_overview = commands.add_parser("render-overview", help="render a completed run as an offline material overview")
    render_overview.add_argument("--run-dir", required=True)
    render_overview.add_argument("--output-dir")
    validate = commands.add_parser("validate")
    for command in (validate,):
        command.add_argument("--config")
        command.add_argument("--query-spec")
        command.add_argument("--highlight-spec")
        command.add_argument("--highlight-mode", choices=("on", "off"))
    query = commands.add_parser("query")
    query.add_argument("--config")
    query.add_argument("--query-spec")
    query.add_argument("--highlight-spec")
    query.add_argument("--highlight-mode", choices=("on", "off"))
    query.add_argument("--output-dir", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config")
    run.add_argument("--query-spec")
    run.add_argument("--highlight-spec")
    run.add_argument("--highlight-mode", choices=("on", "off"))
    run.add_argument("--output-dir", required=True)
    run.add_argument("--execute-rpa", action="store_true")
    run.add_argument("--rpa-concurrency", type=int)
    run.add_argument("--material-timeout-sec", type=int)
    run.add_argument("--csv-wait-window-sec", type=int)
    run.add_argument("--recovery-attempts", type=int)
    run.add_argument("--recovery-backoff-sec", type=float)
    run.add_argument("--csv-max-bytes", type=int)
    run.add_argument("--video-max-bytes", type=int)
    run.add_argument("--download-deadline-sec", type=int)
    workflow = commands.add_parser("workflow", help="run the complete two-stage workflow; stage one stops at analysis handoff")
    workflow.add_argument("--config")
    workflow.add_argument("--query-spec")
    workflow.add_argument("--highlight-spec")
    workflow.add_argument("--highlight-mode", choices=("on", "off"))
    workflow.add_argument("--output-dir", required=True)
    workflow.add_argument("--execute-rpa", action="store_true")
    workflow.add_argument("--rpa-concurrency", type=int)
    workflow.add_argument("--material-timeout-sec", type=int)
    workflow.add_argument("--csv-wait-window-sec", type=int)
    workflow.add_argument("--recovery-attempts", type=int)
    workflow.add_argument("--recovery-backoff-sec", type=float)
    workflow.add_argument("--csv-max-bytes", type=int)
    workflow.add_argument("--video-max-bytes", type=int)
    workflow.add_argument("--download-deadline-sec", type=int)
    frames = commands.add_parser("select-frames")
    frames.add_argument("--config")
    frames.add_argument("--highlight-spec")
    frames.add_argument("--records-json", required=True)
    frames.add_argument("--video", required=True)
    frames.add_argument("--output-dir", required=True)
    verify_run = commands.add_parser("verify-run")
    verify_run.add_argument("--output-dir", required=True)
    build_fact = commands.add_parser("build-fact-pack", help="build categorized facts from a completed RPA run")
    build_fact.add_argument("--run-dir", required=True)
    build_fact.add_argument("--output-dir")
    verify_fact = commands.add_parser("verify-fact-pack", help="verify a fact pack offline without external services")
    verify_fact.add_argument("--run-dir", required=True)
    verify_fact.add_argument("--fact-pack-dir")
    prepare_analysis_parser = commands.add_parser("prepare-analysis", help="prepare a source-linked model input from a completed fact pack")
    prepare_analysis_parser.add_argument("--run-dir", required=True)
    prepare_analysis_parser.add_argument("--analysis-request", required=True)
    prepare_analysis_parser.add_argument("--output-dir")
    verify_analysis_parser = commands.add_parser("verify-analysis", help="verify a model result and render the standard analysis report offline")
    verify_analysis_parser.add_argument("--run-dir", required=True)
    verify_analysis_parser.add_argument("--analysis-dir")
    continue_analysis_parser = commands.add_parser("continue-analysis", help="resume commercial analysis from a fact-pack handoff")
    continue_analysis_parser.add_argument("--run-dir", required=True)
    continue_analysis_parser.add_argument("--analysis-request", required=True)
    continue_analysis_parser.add_argument("--output-dir")
    receive_analysis_parser = commands.add_parser("receive-analysis-result", help="receive and verify a model-produced analysis result")
    receive_analysis_parser.add_argument("--run-dir", required=True)
    receive_analysis_parser.add_argument("--analysis-result", required=True)
    receive_analysis_parser.add_argument("--analysis-dir")
    return parser


def main(argv=None):
    receipt = None
    try:
        args = build_parser().parse_args(argv)
        if args.command == "render-overview":
            run_dir = Path(args.run_dir)
            output_dir = Path(args.output_dir) if args.output_dir else run_dir / "overview"
            result = write_overview(run_dir, output_dir, REGISTRY_PATH)
            print(json.dumps(_json_safe(result), ensure_ascii=False))
            return 0
        if args.command == "continue-analysis":
            try:
                _ensure_analysis_assets()
                result = continue_analysis(Path(args.run_dir), Path(args.analysis_request), Path(args.output_dir) if args.output_dir else None)
            except (MetaSkillError, AnalysisError) as exc:
                try:
                    request_payload = _read_json(Path(args.analysis_request), "analysis request")
                except MetaSkillError:
                    request_payload = {}
                failed_phase = "fact_retrieval_failed" if isinstance(request_payload, dict) and "factRetrievalSpec" in request_payload else "analysis_failed"
                _set_run_phase(
                    Path(args.run_dir),
                    failed_phase,
                    error=str(exc),
                    errorDetails=exc.to_dict() if isinstance(exc, AnalysisError) else None,
                )
                raise
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "receive-analysis-result":
            _set_run_phase(Path(args.run_dir), "analysis_verifying", progress={"completed": 0, "total": 1}, currentObject="analysis-result.json")
            try:
                result = receive_analysis_result(
                    Path(args.run_dir),
                    Path(args.analysis_result),
                    Path(args.analysis_dir) if args.analysis_dir else None,
                )
            except (MetaSkillError, AnalysisError) as exc:
                _set_run_phase(Path(args.run_dir), "analysis_failed", error=str(exc))
                raise
            if result.get("status") == "succeeded":
                _set_run_phase(Path(args.run_dir), "analysis_verifying", progress={"completed": 1, "total": 1}, currentObject="analysis-result.json", phaseComplete=True)
                _set_run_phase(Path(args.run_dir), "analysis_verified", lastSuccessfulPhase="analysis_verified")
                _set_run_phase(
                    Path(args.run_dir),
                    "report_ready",
                    lastSuccessfulPhase="analysis_verified",
                    report=str(result.get("report", "")),
                )
            elif result.get("status") == "awaiting_model":
                _set_run_phase(Path(args.run_dir), "awaiting_model", analysisStatus="awaiting_model")
                _set_run_phase(
                    Path(args.run_dir),
                    "analysis_prepared",
                    analysisStatus="awaiting_model",
                    analysisErrors=result.get("errors", []),
                )
            else:
                _set_run_phase(
                    Path(args.run_dir),
                    "analysis_failed",
                    analysisErrors=result.get("errors", []),
                )
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("status") in {"succeeded", "awaiting_model"} else 1
        if args.command == "prepare-analysis":
            _ensure_analysis_assets()
            _set_run_phase(Path(args.run_dir), "analysis_preparing", progress={"completed": 0, "total": 1}, currentObject="analysis-input.json")
            try:
                result = prepare_analysis(Path(args.run_dir), Path(args.analysis_request), Path(args.output_dir) if args.output_dir else None)
            except (MetaSkillError, AnalysisError) as exc:
                _set_run_phase(Path(args.run_dir), "analysis_failed", error=str(exc))
                raise
            if result.get("status", "").startswith("awaiting_model"):
                _set_run_phase(Path(args.run_dir), "analysis_preparing", progress={"completed": 1, "total": 1}, currentObject="analysis-input.json", phaseComplete=True)
                _set_run_phase(Path(args.run_dir), "awaiting_model", lastSuccessfulPhase="awaiting_model")
                _set_run_phase(
                    Path(args.run_dir),
                    "analysis_prepared",
                    analysisReceipt=str((Path(args.output_dir) if args.output_dir else Path(args.run_dir) / "analysis") / "analysis-receipt.json"),
                )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "verify-analysis":
            _set_run_phase(Path(args.run_dir), "analysis_verifying", progress={"completed": 0, "total": 1}, currentObject="analysis-result.json")
            result = verify_analysis(Path(args.run_dir), Path(args.analysis_dir) if args.analysis_dir else None)
            if result.get("status") == "succeeded":
                _set_run_phase(Path(args.run_dir), "analysis_verifying", progress={"completed": 1, "total": 1}, currentObject="analysis-result.json", phaseComplete=True)
                _set_run_phase(Path(args.run_dir), "analysis_verified", lastSuccessfulPhase="analysis_verified")
                _set_run_phase(Path(args.run_dir), "report_ready", lastSuccessfulPhase="analysis_verified", report=str(result.get("report", "")))
            elif result.get("status") in {"failed", "incomplete"}:
                _set_run_phase(Path(args.run_dir), "analysis_failed", analysisErrors=result.get("errors", []))
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["status"] == "succeeded" else 1
        if args.command == "verify-run":
            print(json.dumps(verify_completed_run(args.output_dir), ensure_ascii=False))
            return 0
        if args.command == "build-fact-pack":
            output_dir = args.output_dir or str(Path(args.run_dir) / "fact-pack")
            manifest = build_fact_pack(args.run_dir, output_dir)
            print(json.dumps({"status": "succeeded", "manifest": str(Path(output_dir) / "manifest.json"), "targetTopN": manifest["targetTopN"], "counts": manifest["counts"]}, ensure_ascii=False))
            return 0
        if args.command == "verify-fact-pack":
            fact_pack_dir = args.fact_pack_dir or str(Path(args.run_dir) / "fact-pack")
            result = verify_fact_pack(args.run_dir, fact_pack_dir)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["status"] == "succeeded" else 1
        registry = load_field_registry(REGISTRY_PATH)
        query_spec, highlight_spec, query, policy = _load_specs(
            args, registry, require_query=args.command in {"validate", "query", "run", "workflow"}
        )
        if args.command == "validate":
            print(json.dumps(_json_safe({
                "valid": True,
                "registryVersion": registry.version,
                "querySpec": query_spec,
                "highlightSpec": highlight_spec,
                "configurationFingerprint": spec_fingerprint(query_spec, highlight_spec),
                "query": query,
                "highlightPolicy": policy,
            }), ensure_ascii=False))
            return 0
        if args.command == "select-frames":
            records = _read_json(args.records_json, "diagnosis records", list)
            summary = extract_highlight_frames(records, policy, args.video, args.output_dir)
            print("frame_summary=%s" % (Path(args.output_dir) / "summary.json"))
            return 0
        output_dir = Path(args.output_dir)
        fingerprint = spec_fingerprint(query_spec, highlight_spec)
        if args.command == "query":
            _write_query_artifacts(output_dir, query_spec, highlight_spec, query, policy, registry)
            print("top_materials_json=%s" % (output_dir / "top_materials.json"))
            print("query_receipt=%s" % (output_dir / "query-receipt.json"))
            return 0
        if args.command == "workflow":
            args.command = "run"
        receipt = RunReceipt(output_dir, fingerprint, "run", args.execute_rpa)
        _set_run_phase(output_dir, "querying")
        receipt.start_stage("query")
        top_materials = _reusable_top_materials(output_dir, fingerprint)
        if top_materials is None:
            top_materials, fingerprint = _write_query_artifacts(output_dir, query_spec, highlight_spec, query, policy, registry)
            receipt.finish_stage("query", reused=False)
        else:
            receipt.finish_stage("query", reused=True)
        if not args.execute_rpa:
            receipt.finish(True)
            _set_run_phase(output_dir, "querying", progress={"completed": 1, "total": 1}, phaseComplete=True)
            spec_args = "--config %s" % args.config if args.config else "--query-spec %s --highlight-spec %s" % (args.query_spec, args.highlight_spec)
            print("top_materials_json=%s" % (output_dir / "top_materials.json"))
            print("query_receipt=%s" % (output_dir / "query-receipt.json"))
            print("next_command=run --execute-rpa %s --output-dir %s" % (spec_args, output_dir))
            return 0
        options = _rpa_options_from_args(args)
        _write_json(output_dir / "resolved-rpa-options.json", options)
        _set_run_phase(output_dir, "rpa_processing")
        receipt.start_stage("rpa")
        account = list_rpa_accounts()

        def fetch(entry, paths):
            return run_isolated(
                {
                    "shopId": account.get("rpaShopId") or account["accountId"],
                    "rpaAccountId": account["accountId"],
                    "materialId": entry["materialId"],
                    "startDate": query["period"]["startDate"],
                    "endDate": query["period"]["endDate"],
                    "csvDir": str(paths["csv"]),
                    "gatewayTimeoutSec": options["materialTimeoutSec"],
                    "csvMaxBytes": options["csvMaxBytes"],
                    "downloadDeadlineSec": options["downloadDeadlineSec"],
                },
                timeout_sec=options["materialTimeoutSec"],
                worker=fetch_diagnosis_worker,
            )

        outcome = run_rpa_pipeline(
            output_dir,
            top_materials,
            configuration_fingerprint=fingerprint,
            period=query["period"],
            options=options,
            account={"accountId": account["accountId"], "platform": account["platform"]},
            fetch_diagnosis=fetch,
            postprocess=build_postprocessor(policy if highlight_spec is not None else None, options),
            highlights_enabled=highlight_spec is not None,
            progress_callback=lambda phase, progress, current: _set_run_phase(
                output_dir, phase, progress=progress, currentObject=current
            ),
        )
        receipt.finish_stage("rpa")
        receipt.start_stage("batchVerification")
        if not outcome["succeeded"]:
            receipt.fail_stage("batchVerification", RpaPipelineError("RPA batch partial failure"))
            receipt.finish(False)
            _set_run_phase(
                output_dir,
                "fact_pack_failed",
                lastSuccessfulPhase="rpa_processing",
                error="RPA batch partial failure",
            )
            print("rpa batch partial failure; verification=%s" % outcome["verificationPath"], file=sys.stderr)
            return 2
        receipt.finish_stage("batchVerification", verificationPath=outcome["verificationPath"])
        receipt.start_stage("factPack")
        _set_run_phase(output_dir, "fact_pack_building", progress={"completed": 0, "total": 1}, currentObject="fact-pack")
        manifest = build_fact_pack(output_dir)
        _set_run_phase(output_dir, "fact_pack_building", progress={"completed": 1, "total": 1}, currentObject="fact-pack", phaseComplete=True)
        receipt.finish_stage(
            "factPack",
            manifestPath=str(output_dir / "fact-pack" / "manifest.json"),
            targetTopN=manifest["targetTopN"],
            counts=manifest["counts"],
        )
        fact_pack_dir = output_dir / "fact-pack"
        _set_run_phase(output_dir, "fact_pack_verifying", progress={"completed": 0, "total": 1}, currentObject="fact-pack")
        if (fact_pack_dir / "manifest.json").is_file():
            fact_pack_validation = verify_fact_pack(output_dir, fact_pack_dir)
            if fact_pack_validation.get("status") != "succeeded":
                error = FactPackError("fact pack validation failed: %s" % "; ".join(fact_pack_validation.get("errors", [])))
                receipt.fail_stage("factPack", error)
                receipt.finish(False)
                _set_run_phase(output_dir, "fact_pack_failed", lastSuccessfulPhase="rpa_processing", factPackValidation=fact_pack_validation)
                print("fact pack validation failed", file=sys.stderr)
                return 2
            _set_run_phase(output_dir, "fact_pack_verifying", progress={"completed": 1, "total": 1}, currentObject="fact-pack", phaseComplete=True)
            handoff = build_fact_pack_handoff(output_dir)
            _write_json(output_dir / "analysis-handoff.json", handoff)
        receipt.finish(True)
        if (output_dir / "analysis-handoff.json").is_file():
            _set_run_phase(
                output_dir,
                "awaiting_analysis_input",
                lastSuccessfulPhase="fact_pack_ready",
                handoff=handoff,
                factPackValidation=fact_pack_validation,
            )
        print("top_materials_json=%s" % (output_dir / "top_materials.json"))
        print("query_receipt=%s" % (output_dir / "query-receipt.json"))
        print("top_materials_rpa_ready=%s" % outcome["readyPath"])
        print("fact_pack_manifest=%s" % (output_dir / "fact-pack" / "manifest.json"))
        if (output_dir / "analysis-handoff.json").is_file():
            print("phase=awaiting_analysis_input")
            print("analysis_handoff=%s" % (output_dir / "analysis-handoff.json"))
            print("next_step=awaiting_analysis_input")
            print("analysis_prompt=请继续输入分析目标、分区、素材范围和关注指标")
        return 0
    except (MetaSkillError, AnalysisError, ConfigSpecError, MaterialQueryError, FieldRegistryError, FrameExtractionError, ReportClientError, RpaAdapterError, RpaPipelineError, RpaRuntimeError, RunVerificationError, FactPackError, OverviewReportError) as exc:
        if receipt is not None:
            failed_stage = receipt.active_stage
            receipt.fail_active_stage(exc)
            receipt.finish(False)
            failed_phase = "query_failed" if failed_stage == "query" else "fact_pack_failed"
            _set_run_phase(
                Path(receipt.path).parent,
                failed_phase,
                lastSuccessfulPhase="querying" if failed_stage and failed_stage != "query" else None,
                error=str(exc),
            )
        if isinstance(exc, AnalysisError):
            print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        else:
            print("meta skill failed: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
