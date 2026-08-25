#!/usr/bin/env python3
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile

from frame_extractor import FrameExtractionError, extract_highlight_frames
from field_registry import FieldRegistryError, load_field_registry
from highlight_policy import HighlightPolicyError, normalize_highlight_policy
from material_query import MaterialQueryError, execute_material_query, parse_query_config
from qianchuan_report_client import ReportClientError, fetch_material_report


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "asset-field-registry-v1.json"


class MetaSkillError(RuntimeError):
    pass


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


def load_config(path):
    payload = _read_json(path, "meta-skill config")
    if payload.get("schemaVersion") != 1:
        raise MetaSkillError("meta-skill config schemaVersion must be 1")
    registry = load_field_registry(REGISTRY_PATH)
    try:
        query = parse_query_config(payload.get("query"), registry)
        policy = normalize_highlight_policy(payload.get("highlightPolicy"))
    except (MaterialQueryError, HighlightPolicyError) as exc:
        raise MetaSkillError(str(exc)) from exc
    return registry, query, policy


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


def collect_report_records(query, output_dir):
    return fetch_material_report(
        query["shopId"],
        query["advertiserId"],
        query["period"]["startDate"],
        query["period"]["endDate"],
        output_dir=Path(output_dir) / "source-data",
    )


def _top_materials_payload(result, query):
    return {
        "schemaVersion": 1,
        "actualStartDate": query["period"]["startDate"],
        "actualEndDate": query["period"]["endDate"],
        "materialCount": len(result["materials"]),
        "rankingMetric": query["sort"]["field"],
        "filters": {"advertiserId": query["advertiserId"], "dimensionFilters": query["dimensionFilters"], "metricFilters": query["metricFilters"]},
        "materials": result["materials"],
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Query confirmed Qianchuan asset fields and prepare custom highlight policy.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--config", required=True)
    query = commands.add_parser("query")
    query.add_argument("--config", required=True)
    query.add_argument("--output-dir", required=True)
    frames = commands.add_parser("select-frames")
    frames.add_argument("--config", required=True)
    frames.add_argument("--records-json", required=True)
    frames.add_argument("--video", required=True)
    frames.add_argument("--output-dir", required=True)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        registry, query, policy = load_config(args.config)
        if args.command == "validate":
            print(json.dumps(_json_safe({"valid": True, "registryVersion": registry.version, "query": query, "highlightPolicy": policy}), ensure_ascii=False))
            return 0
        if args.command == "select-frames":
            records = _read_json(args.records_json, "diagnosis records", list)
            summary = extract_highlight_frames(records, policy, args.video, args.output_dir)
            print("frame_summary=%s" % (Path(args.output_dir) / "summary.json"))
            return 0
        output_dir = Path(args.output_dir)
        result = execute_material_query(collect_report_records(query, output_dir), query, registry)
        result["receipt"]["source"] = {"type": "direct_material_report"}
        _write_json(output_dir / "top_materials.json", _top_materials_payload(result, query))
        _write_json(output_dir / "query-receipt.json", result["receipt"])
        _write_json(output_dir / "highlight-policy.json", policy)
        print("top_materials_json=%s" % (output_dir / "top_materials.json"))
        print("query_receipt=%s" % (output_dir / "query-receipt.json"))
        return 0
    except (MetaSkillError, MaterialQueryError, FieldRegistryError, FrameExtractionError, ReportClientError) as exc:
        print("meta skill failed: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
