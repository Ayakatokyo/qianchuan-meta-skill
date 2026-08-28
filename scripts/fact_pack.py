"""Build and verify a redacted, source-linked fact pack for a qianchuan run."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
SENSITIVE_KEY_PARTS = (
    "videourl",
    "thumbnailurl",
    "accountid",
    "authorization",
    "cookie",
    "session",
    "password",
    "token",
)
FORBIDDEN_TEXT = ("/mnt/", "/Users/", "Authorization", "Cookie")


class FactPackError(ValueError):
    """Raised when a run cannot be converted into a safe fact pack."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_path(run_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError as exc:
        raise FactPackError(f"artifact is outside run directory: {path}") from exc


def _file_ref(run_dir: Path, path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise FactPackError(f"missing or empty source artifact: {path}")
    return {"path": _relative_path(run_dir, path), "sha256": _sha256(path), "sizeBytes": path.stat().st_size}


def _source_ref(run_dir: Path, path: Path, record_index: int | None = None) -> dict[str, Any]:
    ref = _file_ref(run_dir, path)
    if record_index is not None:
        ref["recordIndex"] = record_index
    return ref


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactPackError(f"cannot read JSON artifact: {path}") from exc


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items() if not _is_sensitive_key(str(key))}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _parse_literal(value: Any) -> tuple[str, Any]:
    if not isinstance(value, str):
        return "missing", None
    if not value:
        return "missing", None
    try:
        return "parsed", _sanitize(ast.literal_eval(value))
    except (ValueError, SyntaxError, TypeError):
        return "failed", value


def _records_for(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise FactPackError(f"diagnosis records must be a JSON object list: {path}")
    return payload


def _context(run_dir: Path) -> dict[str, Any]:
    receipt = _load_json(run_dir / "query-receipt.json")
    spec_path = run_dir / "resolved-query-spec.json"
    spec = _load_json(spec_path) if spec_path.is_file() else {}
    query = receipt.get("query", {}) if isinstance(receipt, dict) else {}
    period = query.get("period") or spec.get("period") or {}
    ranking = query.get("sort") or spec.get("ranking") or {}
    filters = {
        "dimensionFilters": query.get("dimensionFilters") or spec.get("filters", {}).get("dimensionFilters", []),
        "metricFilters": query.get("metricFilters") or spec.get("filters", {}).get("metricFilters", []),
    }
    return {
        "runId": run_dir.name,
        "period": _sanitize(period),
        "ranking": _sanitize(ranking),
        "filters": _sanitize(filters),
        "queryCoverage": {
            "sourceRecordCount": receipt.get("sourceRecordCount"),
            "candidateCount": receipt.get("candidateCount"),
            "passedCount": receipt.get("passedCount"),
            "returnedCount": receipt.get("returnedCount"),
        },
    }


def _material_identity(info: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "materialId", "materialName", "materialIdTag", "createTime", "materialUri", "douyinTitle",
        "videoDuration", "myTagEntry", "benchTagEntry", "analysisInfo",
    )
    output: dict[str, Any] = {}
    for field in fields:
        if field not in info:
            continue
        value = info[field]
        if field in {"myTagEntry", "benchTagEntry", "analysisInfo"}:
            status, parsed = _parse_literal(value)
            output[field] = parsed if status == "parsed" else {"parseStatus": status, "raw": _sanitize(value)}
        else:
            output[field] = _sanitize(value)
    return output


def _build_materials(run_dir: Path, top: dict[str, Any], diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    materials: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = [_source_ref(run_dir, run_dir / "top_materials.json")]
    for top_material in top.get("materials", []):
        material_id = str(top_material["materialId"])
        records_path, records = diagnosis[material_id]
        refs.append(_source_ref(run_dir, records_path))
        info_rows = [(index, row) for index, row in enumerate(records) if row.get("recordType") == "material_info"]
        detail_rows = [(index, row) for index, row in enumerate(records) if row.get("recordType") == "delivery_material_detail"]
        info = info_rows[0][1] if info_rows else {"materialId": material_id}
        material = {
            "rank": top_material.get("rank"),
            "materialId": material_id,
            "materialName": top_material.get("materialName", ""),
            "materialType": top_material.get("materialType", ""),
            "videoType": top_material.get("videoType", ""),
            "rankingMetric": top_material.get("rankingMetric"),
            "sortValue": top_material.get("sortValue"),
            "metrics": _sanitize(top_material.get("metrics", {})),
            "materialInfo": _material_identity(info),
            "dailyDetails": [],
            "sourceRefs": [
                _source_ref(run_dir, records_path, info_rows[0][0]) if info_rows else _source_ref(run_dir, records_path),
            ],
        }
        for index, detail in detail_rows:
            material["dailyDetails"].append({
                "record": _sanitize(detail),
                "sourceRef": _source_ref(run_dir, records_path, index),
            })
        materials.append(material)
    return {"schemaVersion": SCHEMA_VERSION, "materials": materials, "sourceRefs": refs}, refs


def _build_audience(run_dir: Path, material_ids: list[str], diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for material_id in material_ids:
        records_path, records = diagnosis[material_id]
        refs.append(_source_ref(run_dir, records_path))
        for index, row in enumerate(records):
            if row.get("recordType") != "crowd_analysis":
                continue
            dimensions = row.get("Dimensions") or {}
            dimension_key = row.get("dimensionKey")
            dimension_value = dimensions.get(dimension_key, {}) if isinstance(dimensions, dict) else {}
            facts.append({
                "materialId": material_id,
                "actualStartDate": row.get("actualStartDate"),
                "actualEndDate": row.get("actualEndDate"),
                "dimensionKey": dimension_key,
                "dimensionName": row.get("dimensionName"),
                "audienceValue": _sanitize(dimension_value),
                "displayMetric": row.get("displayMetric"),
                "displayMetricName": row.get("displayMetricName"),
                "metricValue": row.get("metricValue"),
                "metricPercentage": row.get("metricPercentage"),
                "dimensionTotal": row.get("dimensionTotal"),
                "Dimensions": _sanitize(dimensions),
                "Metrics": _sanitize(row.get("Metrics", {})),
                "sourceRef": _source_ref(run_dir, records_path, index),
            })
    return {"schemaVersion": SCHEMA_VERSION, "audience": facts, "sourceRefs": refs}, refs


def _build_scripts(run_dir: Path, material_ids: list[str], diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for material_id in material_ids:
        records_path, records = diagnosis[material_id]
        refs.append(_source_ref(run_dir, records_path))
        rows = [(index, row) for index, row in enumerate(records) if row.get("recordType") == "script_analysis"]
        index, row = rows[0] if rows else (None, {})
        status, parsed = _parse_literal(row.get("scriptInfo"))
        fact: dict[str, Any] = {
            "materialId": material_id,
            "materialUri": row.get("materialUri"),
            "parseStatus": status,
            "completeness": {
                "formulaLabels": bool(parsed.get("formula", {}).get("labels")) if status == "parsed" else False,
                "detailLabels": bool(parsed.get("detail")) if status == "parsed" else False,
                "transcript": bool(parsed.get("text")) if status == "parsed" else False,
                "structuredData": bool(parsed.get("data")) if status == "parsed" else False,
            },
            "sourceRef": _source_ref(run_dir, records_path, index) if index is not None else _source_ref(run_dir, records_path),
        }
        if status == "parsed":
            fact.update({
                "formula": parsed.get("formula", {}),
                "detail": parsed.get("detail", {}),
                "text": parsed.get("text", ""),
                "data": parsed.get("data", []),
            })
        elif status == "failed":
            fact["rawScriptInfo"] = _sanitize(parsed)
        facts.append(fact)
    return {"schemaVersion": SCHEMA_VERSION, "scripts": facts, "sourceRefs": refs}, refs


def _build_highlights(run_dir: Path, material_ids: list[str], diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    frames: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for material_id in material_ids:
        records_path, records = diagnosis[material_id]
        frame_dir = run_dir / "rpa" / "peak-frames" / material_id
        summary_path = frame_dir / "summary.json"
        summary = _load_json(summary_path)
        refs.extend([_source_ref(run_dir, records_path), _source_ref(run_dir, summary_path)])
        interaction_count = sum(1 for row in records if row.get("recordType") == "interaction_series")
        for sequence, item in enumerate(summary.get("frames", []), start=1):
            filename = Path(str(item.get("framePath", ""))).name
            frame_path = frame_dir / filename
            if frame_path.suffix.lower() != ".jpg":
                raise FactPackError(f"highlight frame is not JPEG: {frame_path}")
            frames.append({
                "materialId": material_id,
                "frameSequence": sequence,
                "durationSec": item.get("durationSec"),
                "artifact": _file_ref(run_dir, frame_path),
                "selectionEvidence": _sanitize(item.get("selectionEvidence", [])),
                "highlightPolicy": _sanitize(summary.get("highlightPolicy", {})),
                "unavailableMetrics": _sanitize(summary.get("unavailableMetrics", [])),
                "interactionSeries": {
                    "recordCount": interaction_count,
                    "sourceRef": _source_ref(run_dir, records_path),
                },
            })
    return {"schemaVersion": SCHEMA_VERSION, "highlightFrames": frames, "sourceRefs": refs}, refs


def _assert_no_sensitive(value: Any, location: str = "root") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                raise FactPackError(f"sensitive field in fact pack: {location}.{key}")
            _assert_no_sensitive(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_sensitive(item, f"{location}[{index}]")
    elif isinstance(value, str):
        if any(marker in value for marker in FORBIDDEN_TEXT):
            raise FactPackError(f"forbidden path or credential text in fact pack: {location}")


def build_fact_pack(run_dir: Path, output_dir: Path | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    output_dir = Path(output_dir).resolve() if output_dir else run_dir / "fact-pack"
    top = _load_json(run_dir / "top_materials.json")
    material_ids = [str(item["materialId"]) for item in top.get("materials", [])]
    if len(material_ids) != 3 or len(set(material_ids)) != 3:
        raise FactPackError("top_materials.json must contain exactly three unique materials")
    diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]] = {}
    for material_id in material_ids:
        path = run_dir / "rpa" / "diagnosis-records" / f"{material_id}.json"
        diagnosis[material_id] = (path, _records_for(path))
    output_dir.mkdir(parents=True, exist_ok=True)
    materials, material_refs = _build_materials(run_dir, top, diagnosis)
    audience, audience_refs = _build_audience(run_dir, material_ids, diagnosis)
    scripts, script_refs = _build_scripts(run_dir, material_ids, diagnosis)
    highlights, highlight_refs = _build_highlights(run_dir, material_ids, diagnosis)
    for name, payload in (
        ("material-leaderboard.json", materials),
        ("audience.json", audience),
        ("scripts.json", scripts),
        ("highlight-frames.json", highlights),
    ):
        _assert_no_sensitive(payload)
        _write_json(output_dir / name, payload)
    source_manifest_paths = [run_dir / "query-receipt.json", run_dir / "resolved-query-spec.json", run_dir / "source-data" / "field-manifest.json"]
    source_files = [_file_ref(run_dir, path) for path in source_manifest_paths if path.is_file()]
    source_files.extend(material_refs + audience_refs + script_refs + highlight_refs)
    unique_sources = {(item["path"], item.get("recordIndex")): item for item in source_files}
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "context": _context(run_dir),
        "materialIds": material_ids,
        "counts": {
            "materials": len(materials["materials"]),
            "audienceFacts": len(audience["audience"]),
            "scriptFacts": len(scripts["scripts"]),
            "highlightFrames": len(highlights["highlightFrames"]),
        },
        "files": [
            "manifest.json", "material-leaderboard.json", "audience.json", "scripts.json", "highlight-frames.json", "README.md",
        ],
        "sourceFiles": list(unique_sources.values()),
        "redactions": ["signed media links", "absolute paths", "RPA account and credential fields"],
        "quality": {"status": "built", "errors": [], "scriptParseStatuses": [item["parseStatus"] for item in scripts["scripts"]]},
    }
    _assert_no_sensitive(manifest)
    _write_json(output_dir / "manifest.json", manifest)
    readme = """# 千川 Top3 事实包\n\n本目录是 `runs/run_20260828_top3` 的脱敏规范化事实层，原始工件保持不变。\n\n- `material-leaderboard.json`：素材排名、102 个原始指标、素材信息和日明细。\n- `audience.json`：素材 × 人群维度 × 展示指标事实。\n- `scripts.json`：脚本标签、逐字稿和解析/缺失状态。\n- `highlight-frames.json`：高光帧、JPEG 文件证据和选帧依据。\n\n事实包只做分类和证据引用，不代表商业结论。所有 `sourceRef`/`artifact` 均为相对路径；带签名 URL、账号和会话字段已剔除。\n"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")
    return manifest


def verify_fact_pack(run_dir: Path, fact_pack_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    fact_pack_dir = Path(fact_pack_dir).resolve()
    errors: list[str] = []
    try:
        top = _load_json(run_dir / "top_materials.json")
        expected_ids = {str(item["materialId"]) for item in top.get("materials", [])}
        payloads = {name: _load_json(fact_pack_dir / name) for name in (
            "manifest.json", "material-leaderboard.json", "audience.json", "scripts.json", "highlight-frames.json",
        )}
        for name, payload in payloads.items():
            _assert_no_sensitive(payload, name)
        materials = payloads["material-leaderboard.json"]["materials"]
        audience = payloads["audience.json"]["audience"]
        scripts = payloads["scripts.json"]["scripts"]
        highlights = payloads["highlight-frames.json"]["highlightFrames"]
        if {str(item["materialId"]) for item in materials} != expected_ids:
            errors.append("material IDs do not match Top3")
        if len(materials) != 3 or any(len(item.get("metrics", {})) != 102 for item in materials):
            errors.append("leaderboard must contain three materials with 102 metrics each")
        if any(len(item.get("dailyDetails", [])) != 11 for item in materials):
            errors.append("each material must contain 11 daily details")
        source_audience_count = 0
        for material_id in expected_ids:
            records = _records_for(run_dir / "rpa" / "diagnosis-records" / f"{material_id}.json")
            source_audience_count += sum(row.get("recordType") == "crowd_analysis" for row in records)
        if len(audience) != source_audience_count:
            errors.append(f"audience count mismatch: {len(audience)} != {source_audience_count}")
        if len(scripts) != 3:
            errors.append("script fact count must be three")
        source_frame_count = 0
        for material_id in expected_ids:
            summary = _load_json(run_dir / "rpa" / "peak-frames" / material_id / "summary.json")
            source_frame_count += len(summary.get("frames", []))
        if len(highlights) != source_frame_count:
            errors.append(f"highlight frame count mismatch: {len(highlights)} != {source_frame_count}")
        for item in highlights:
            artifact = item.get("artifact", {})
            path = run_dir / artifact.get("path", "")
            if not artifact.get("path") or path.resolve().parent.parent.parent != (run_dir / "rpa").resolve() or not path.is_file():
                errors.append(f"invalid highlight artifact path: {artifact.get('path')}")
            elif artifact.get("sha256") != _sha256(path) or artifact.get("sizeBytes") != path.stat().st_size:
                errors.append(f"highlight artifact hash/size mismatch: {artifact.get('path')}")
        manifest_counts = payloads["manifest.json"].get("counts", {})
        expected_counts = {"materials": len(materials), "audienceFacts": len(audience), "scriptFacts": len(scripts), "highlightFrames": len(highlights)}
        if manifest_counts != expected_counts:
            errors.append("manifest counts do not match category files")
    except (FactPackError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    return {"status": "succeeded" if not errors else "failed", "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("run_dir", type=Path)
    build.add_argument("--output-dir", type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("run_dir", type=Path)
    verify.add_argument("--fact-pack-dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        output_dir = args.output_dir or args.run_dir / "fact-pack"
        manifest = build_fact_pack(args.run_dir, output_dir)
        print(f"manifest={output_dir / 'manifest.json'} counts={json.dumps(manifest['counts'], ensure_ascii=False, sort_keys=True)}")
        return 0
    result = verify_fact_pack(args.run_dir, args.fact_pack_dir or args.run_dir / "fact-pack")
    print(f"status={result['status']} errors={json.dumps(result['errors'], ensure_ascii=False)}")
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
