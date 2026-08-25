#!/usr/bin/env python3
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile

from config_specs import (
    ConfigSpecError, highlight_spec_to_policy, legacy_config_to_specs, query_spec_to_query_config,
    resolve_highlight_spec, resolve_query_spec, spec_fingerprint, write_resolved_specs,
)
from frame_extractor import FrameExtractionError, extract_highlight_frames
from field_registry import FieldRegistryError, load_field_registry
from material_query import MaterialQueryError, execute_material_query
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


def _load_specs(args, registry, *, require_query):
    query_spec_path = getattr(args, "query_spec", None)
    highlight_spec_path = getattr(args, "highlight_spec", None)
    if args.config and (query_spec_path or highlight_spec_path):
        raise MetaSkillError("--config cannot be combined with --query-spec or --highlight-spec")
    try:
        if args.config:
            query_spec, highlight_spec = legacy_config_to_specs(_read_json(args.config, "meta-skill config"), registry)
        else:
            if not highlight_spec_path or (require_query and not query_spec_path):
                raise MetaSkillError("--query-spec and --highlight-spec are required")
            query_spec = resolve_query_spec(_read_json(query_spec_path, "QuerySpec"), registry) if query_spec_path else None
            highlight_spec = resolve_highlight_spec(_read_json(highlight_spec_path, "HighlightSpec"))
    except (ConfigSpecError, MaterialQueryError) as exc:
        raise MetaSkillError(str(exc)) from exc
    query = query_spec_to_query_config(query_spec) if query_spec is not None else None
    return query_spec, highlight_spec, query, highlight_spec_to_policy(highlight_spec)


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


def _connector_order_by(query, registry):
    entry = registry.require(query["sort"]["field"])
    source = entry.get("source")
    if isinstance(source, list) and len(source) >= 2 and source[0] == "metrics":
        field = source[1]
    else:
        # Derived metrics are ranked locally after aggregation; overall cost is a stable gateway fallback.
        field = "stat_cost_for_overall_roi2"
    return [{"field": field, "type": 2 if query["sort"]["direction"] == "desc" else 1}]


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
        "rankingMetric": query["sort"]["field"],
        "filters": {"advertiserId": query["advertiserId"], "dimensionFilters": query["dimensionFilters"], "metricFilters": query["metricFilters"]},
        "materials": result["materials"],
    }


def build_parser():
    parser = argparse.ArgumentParser(description="Query confirmed Qianchuan asset fields and prepare custom highlight policy.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    for command in (validate,):
        command.add_argument("--config")
        command.add_argument("--query-spec")
        command.add_argument("--highlight-spec")
    query = commands.add_parser("query")
    query.add_argument("--config")
    query.add_argument("--query-spec")
    query.add_argument("--highlight-spec")
    query.add_argument("--output-dir", required=True)
    frames = commands.add_parser("select-frames")
    frames.add_argument("--config")
    frames.add_argument("--highlight-spec")
    frames.add_argument("--records-json", required=True)
    frames.add_argument("--video", required=True)
    frames.add_argument("--output-dir", required=True)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        registry = load_field_registry(REGISTRY_PATH)
        query_spec, highlight_spec, query, policy = _load_specs(
            args, registry, require_query=args.command in {"validate", "query"}
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
        print("top_materials_json=%s" % (output_dir / "top_materials.json"))
        print("query_receipt=%s" % (output_dir / "query-receipt.json"))
        return 0
    except (MetaSkillError, ConfigSpecError, MaterialQueryError, FieldRegistryError, FrameExtractionError, ReportClientError) as exc:
        print("meta skill failed: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
