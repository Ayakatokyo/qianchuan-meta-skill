"""Build and verify a redacted, source-linked fact pack for a completed run."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

from path_safety import artifact_path, validate_material_id


SCHEMA_VERSION = 1
SENSITIVE_KEY_PARTS = (
    "videourl", "thumbnailurl", "materialuri", "accountid", "authorization", "cookie",
    "session", "password", "token", "credential", "secret",
)
_URL = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s=(])(?:/Users/|/mnt/|/private/|/tmp/|[A-Za-z]:[\\/])[^\s,;)]*", re.IGNORECASE)
_CREDENTIAL = re.compile(r"(?i)(authorization|cookie|session|password|token|secret)\s*[:=]\s*[^\s,;]+")


class FactPackError(ValueError):
    """Raised when a run cannot be converted into a safe fact pack."""


_FILE_HASH_CACHE: dict[str, tuple[str, int, int]] = {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clear_file_hash_cache() -> None:
    """Clear the process-local file hash cache (primarily useful for tests)."""
    _FILE_HASH_CACHE.clear()


def _cached_file_hash(path: Path) -> tuple[str, int]:
    path = Path(path).resolve()
    stat = path.stat()
    key = os.path.normcase(str(path))
    cached = _FILE_HASH_CACHE.get(key)
    if cached and cached[1] == stat.st_size and cached[2] == stat.st_mtime_ns:
        return cached[0], cached[1]
    digest = _sha256(path)
    _FILE_HASH_CACHE[key] = (digest, stat.st_size, stat.st_mtime_ns)
    return digest, stat.st_size


def file_hash_and_size(path: Path) -> tuple[str, int]:
    """Return a cached SHA-256 and byte size for a regular file."""
    return _cached_file_hash(path)


def _relative_path(run_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError as exc:
        raise FactPackError(f"artifact is outside run directory: {path}") from exc


def _file_ref(run_dir: Path, path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise FactPackError(f"missing or empty source artifact: {path}")
    digest, size = _cached_file_hash(path)
    return {"path": _relative_path(run_dir, path), "sha256": digest, "sizeBytes": size}


def _source_ref(run_dir: Path, path: Path, record_index: int | None = None) -> dict[str, Any]:
    ref = _file_ref(run_dir, path)
    if record_index is not None:
        ref["recordIndexes"] = [record_index]
    return ref


def _record_indexes(ref: dict[str, Any]) -> list[int]:
    indexes = ref.get("recordIndexes")
    if indexes is None and "recordIndex" in ref:
        indexes = [ref["recordIndex"]]
    if not isinstance(indexes, list):
        return []
    return sorted({index for index in indexes if type(index) is int and index >= 0})


def _aggregate_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated: dict[tuple[str, str, int], dict[str, Any]] = {}
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            continue
        key = (ref["path"], ref.get("sha256"), ref.get("sizeBytes"))
        target = aggregated.setdefault(key, {key_name: ref[key_name] for key_name in ("path", "sha256", "sizeBytes")})
        indexes = sorted(set(target.get("recordIndexes", [])) | set(_record_indexes(ref)))
        if indexes:
            target["recordIndexes"] = indexes
        target.pop("recordIndex", None)
    return list(aggregated.values())


def _collect_source_refs(value: Any, refs: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if {"path", "sha256", "sizeBytes"}.issubset(value):
            refs.append(value)
        for item in value.values():
            _collect_source_refs(item, refs)
    elif isinstance(value, list):
        for item in value:
            _collect_source_refs(item, refs)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FactPackError(f"cannot read JSON artifact: {path}") from exc


def _write_json(path: Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _safe_string(value: str) -> str:
    value = _URL.sub("[redacted-url]", value)
    value = _CREDENTIAL.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    value = _ABSOLUTE_PATH.sub(lambda match: f"{match.group(0)[:1]}[redacted-path]", value)
    return value


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items() if not _is_sensitive_key(str(key))}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _safe_string(value)
    return value


def _parse_literal(value: Any) -> tuple[str, Any]:
    if not isinstance(value, str) or not value:
        return "missing", None
    try:
        return "parsed", _sanitize(ast.literal_eval(value))
    except (ValueError, SyntaxError, TypeError):
        return "failed", _sanitize(value)


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
        if field not in info or _is_sensitive_key(field):
            continue
        value = info[field]
        if field in {"myTagEntry", "benchTagEntry", "analysisInfo"}:
            status, parsed = _parse_literal(value)
            output[field] = parsed if status == "parsed" else {"parseStatus": status, "raw": parsed}
        else:
            output[field] = _sanitize(value)
    return output


def _completed_selection(run_dir: Path, top: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    ready_path = run_dir / "rpa" / "top_materials_rpa_ready.json"
    verification_path = run_dir / "rpa" / "batch-verification.json"
    if not ready_path.is_file() or not verification_path.is_file():
        raise FactPackError("fact pack requires completed RPA artifacts")
    verification = _load_json(verification_path)
    ready = _load_json(ready_path)
    if not isinstance(verification, dict) or not isinstance(ready, dict):
        raise FactPackError("fact pack RPA completion artifacts are invalid")
    if not isinstance(top, dict):
        raise FactPackError("top materials artifact is invalid")
    collection = top.get("collection") if isinstance(top, dict) else None
    target_top_n = collection.get("targetTopN") if isinstance(collection, dict) else None
    if type(target_top_n) is not int or target_top_n <= 0:
        raise FactPackError("fact pack requires a positive targetTopN")
    if verification.get("status") != "succeeded" or verification.get("targetTopN") != target_top_n:
        raise FactPackError("fact pack requires completed RPA verification")
    selected = ready.get("materials") if isinstance(ready, dict) else None
    if ready.get("materialCount") != target_top_n or not isinstance(selected, list) or len(selected) != target_top_n or any(not isinstance(item, dict) for item in selected):
        raise FactPackError("fact pack requires a complete targetTopN RPA result")
    top_materials = top.get("materials")
    if not isinstance(top_materials, list):
        raise FactPackError("top materials artifact is invalid")
    top_by_id = {str(item.get("materialId")): item for item in top_materials if isinstance(item, dict)}
    result = []
    seen = set()
    for item in sorted(selected, key=lambda value: value.get("rank", 0)):
        try:
            material_id = validate_material_id(item.get("materialId"))
        except ValueError as exc:
            raise FactPackError("fact pack RPA selection contains an invalid material ID") from exc
        if material_id in seen or material_id not in top_by_id:
            raise FactPackError("fact pack RPA selection does not match top materials")
        seen.add(material_id)
        source = dict(top_by_id[material_id])
        source["rank"] = item.get("rank", source.get("rank"))
        result.append(source)
    if len(result) != target_top_n:
        raise FactPackError("fact pack RPA selection is incomplete")
    return target_top_n, result


def _build_materials(run_dir: Path, materials_in: list[dict[str, Any]], diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    materials: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = [_source_ref(run_dir, run_dir / "top_materials.json")]
    top_ref = refs[0]
    for top_material in materials_in:
        material_id = str(top_material["materialId"])
        records_path, records = diagnosis[material_id]
        record_ref = _source_ref(run_dir, records_path)
        refs.append(record_ref)
        info_rows = [(index, row) for index, row in enumerate(records) if row.get("recordType") == "material_info"]
        detail_rows = [(index, row) for index, row in enumerate(records) if row.get("recordType") == "delivery_material_detail"]
        info = info_rows[0][1] if info_rows else {"materialId": material_id}
        material = {
            "rank": top_material.get("rank"),
            "materialId": material_id,
            "materialName": _sanitize(top_material.get("materialName", "")),
            "materialType": _sanitize(top_material.get("materialType")),
            "videoType": _sanitize(top_material.get("videoType")),
            "platformDimensions": {"materialType": _sanitize(top_material.get("materialType")), "videoType": _sanitize(top_material.get("videoType"))},
            "rankingMetric": _sanitize(top_material.get("rankingMetric")),
            "sortValue": _sanitize(top_material.get("sortValue")),
            "metrics": _sanitize(top_material.get("metrics", {})),
            "materialInfo": _material_identity(info),
            "dailyDetails": [],
            "sourceRef": top_ref,
            "sourceRefs": [record_ref],
        }
        for index, detail in detail_rows:
            material["dailyDetails"].append({"record": _sanitize(detail), "sourceRef": _source_ref(run_dir, records_path, index)})
        materials.append(material)
    return {"schemaVersion": SCHEMA_VERSION, "materials": materials, "sourceRefs": _aggregate_refs(refs)}, refs


def _build_audience(run_dir: Path, material_ids: list[str], diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for material_id in material_ids:
        records_path, records = diagnosis[material_id]
        record_ref = _source_ref(run_dir, records_path)
        refs.append(record_ref)
        for index, row in enumerate(records):
            if row.get("recordType") != "crowd_analysis":
                continue
            dimensions = row.get("Dimensions") or {}
            dimension_key = row.get("dimensionKey")
            dimension_value = dimensions.get(dimension_key, {}) if isinstance(dimensions, dict) else {}
            facts.append({
                "materialId": material_id,
                "actualStartDate": _sanitize(row.get("actualStartDate")),
                "actualEndDate": _sanitize(row.get("actualEndDate")),
                "dimensionKey": _sanitize(dimension_key),
                "dimensionName": _sanitize(row.get("dimensionName")),
                "audienceValue": _sanitize(dimension_value),
                "displayMetric": _sanitize(row.get("displayMetric")),
                "displayMetricName": _sanitize(row.get("displayMetricName")),
                "metricValue": _sanitize(row.get("metricValue")),
                "metricPercentage": _sanitize(row.get("metricPercentage")),
                "dimensionTotal": _sanitize(row.get("dimensionTotal")),
                "Dimensions": _sanitize(dimensions),
                "Metrics": _sanitize(row.get("Metrics", {})),
                "sourceRef": _source_ref(run_dir, records_path, index),
            })
    status = "available" if facts else "no_audience_facts"
    return {"schemaVersion": SCHEMA_VERSION, "status": status, "audience": facts, "sourceRefs": _aggregate_refs(refs)}, refs


def _build_scripts(run_dir: Path, material_ids: list[str], diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for material_id in material_ids:
        records_path, records = diagnosis[material_id]
        record_ref = _source_ref(run_dir, records_path)
        refs.append(record_ref)
        rows = [(index, row) for index, row in enumerate(records) if row.get("recordType") == "script_analysis"]
        index, row = rows[0] if rows else (None, {})
        status, parsed = _parse_literal(row.get("scriptInfo"))
        fact: dict[str, Any] = {
            "materialId": material_id,
            "parseStatus": status,
            "completeness": {
                "formulaLabels": bool(parsed.get("formula", {}).get("labels")) if status == "parsed" else False,
                "detailLabels": bool(parsed.get("detail")) if status == "parsed" else False,
                "transcript": bool(parsed.get("text")) if status == "parsed" else False,
                "structuredData": bool(parsed.get("data")) if status == "parsed" else False,
            },
            "sourceRef": _source_ref(run_dir, records_path, index) if index is not None else record_ref,
        }
        if status == "parsed":
            fact.update({"formula": parsed.get("formula", {}), "detail": parsed.get("detail", {}), "text": parsed.get("text", ""), "data": parsed.get("data", [])})
        else:
            fact["error"] = {"code": "script_missing" if status == "missing" else "script_parse_failed", "message": "script source is missing" if status == "missing" else "script source could not be parsed"}
            if status == "failed":
                fact["rawScriptInfo"] = parsed
        facts.append(fact)
    return {"schemaVersion": SCHEMA_VERSION, "status": "available", "scripts": facts, "sourceRefs": _aggregate_refs(refs)}, refs


def _build_highlights(run_dir: Path, material_ids: list[str], diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    frames: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for material_id in material_ids:
        records_path, records = diagnosis[material_id]
        frame_dir = artifact_path(run_dir / "rpa" / "peak-frames", material_id)
        summary_path = frame_dir / "summary.json"
        summary = _load_json(summary_path)
        record_ref = _source_ref(run_dir, records_path)
        refs.extend([record_ref, _source_ref(run_dir, summary_path)])
        interaction_count = sum(1 for row in records if row.get("recordType") == "interaction_series")
        for sequence, item in enumerate(summary.get("frames", []), start=1):
            filename = Path(str(item.get("framePath", ""))).name
            frame_path = frame_dir / filename
            if frame_path.suffix.lower() != ".jpg":
                raise FactPackError(f"highlight frame is not JPEG: {frame_path}")
            frames.append({
                "materialId": material_id,
                "frameSequence": sequence,
                "durationSec": _sanitize(item.get("durationSec")),
                "artifact": _file_ref(run_dir, frame_path),
                "selectionEvidence": _sanitize(item.get("selectionEvidence", [])),
                "highlightPolicy": _sanitize(summary.get("highlightPolicy", {})),
                "unavailableMetrics": _sanitize(summary.get("unavailableMetrics", [])),
                "interactionSeries": {"recordCount": interaction_count, "sourceRef": record_ref},
            })
    status = "available" if frames else "no_highlight_frames"
    return {"schemaVersion": SCHEMA_VERSION, "status": status, "highlightFrames": frames, "sourceRefs": _aggregate_refs(refs)}, refs


def _assert_no_sensitive(value: Any, location: str = "root") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                raise FactPackError(f"sensitive field in fact pack: {location}.{key}")
            _assert_no_sensitive(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_sensitive(item, f"{location}[{index}]")
    elif isinstance(value, str) and (_URL.search(value) or _ABSOLUTE_PATH.search(value) or _CREDENTIAL.search(value)):
        raise FactPackError(f"forbidden URL, path or credential text in fact pack: {location}")


def _category_error(category: str, code: str, message: str) -> dict[str, str]:
    return {"category": category, "code": code, "message": message}


def build_fact_pack(run_dir: Path, output_dir: Path | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    output_dir = Path(output_dir).resolve() if output_dir else run_dir / "fact-pack"
    top = _load_json(run_dir / "top_materials.json")
    target_top_n, selected_materials = _completed_selection(run_dir, top)
    material_ids = [str(item["materialId"]) for item in selected_materials]
    diagnosis: dict[str, tuple[Path, list[dict[str, Any]]]] = {}
    for material_id in material_ids:
        path = artifact_path(run_dir / "rpa" / "diagnosis-records", material_id, ".json")
        diagnosis[material_id] = (path, _records_for(path))

    output_dir.mkdir(parents=True, exist_ok=True)
    materials, material_refs = _build_materials(run_dir, selected_materials, diagnosis)
    audience, audience_refs = _build_audience(run_dir, material_ids, diagnosis)
    scripts, script_refs = _build_scripts(run_dir, material_ids, diagnosis)
    highlights, highlight_refs = _build_highlights(run_dir, material_ids, diagnosis)
    category_payloads = (
        ("material-leaderboard.json", materials), ("audience.json", audience),
        ("scripts.json", scripts), ("highlight-frames.json", highlights),
    )
    for name, payload in category_payloads:
        _assert_no_sensitive(payload)
        _write_json(output_dir / name, payload)

    errors = []
    for script in scripts["scripts"]:
        if script["parseStatus"] == "missing":
            errors.append(_category_error("scripts", "script_missing", f"script source is missing for {script['materialId']}"))
        elif script["parseStatus"] == "failed":
            errors.append(_category_error("scripts", "script_parse_failed", f"script source could not be parsed for {script['materialId']}"))
    if audience["status"] == "no_audience_facts":
        errors.append(_category_error("audience", "audience_facts_unavailable", "no crowd_analysis records were available"))
    if highlights["status"] == "no_highlight_frames":
        errors.append(_category_error("highlightFrames", "no_highlight_frames", "RPA completed but no frame was selected"))
    source_manifest_paths = [run_dir / "query-receipt.json", run_dir / "resolved-query-spec.json", run_dir / "source-data" / "field-manifest.json"]
    source_files = [_file_ref(run_dir, path) for path in source_manifest_paths if path.is_file()]
    for payload in (materials, audience, scripts, highlights):
        _collect_source_refs(payload, source_files)
    unique_sources = _aggregate_refs(source_files)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "context": _context(run_dir),
        "targetTopN": target_top_n,
        "materialIds": material_ids,
        "counts": {
            "materials": len(materials["materials"]), "audienceFacts": len(audience["audience"]),
            "scriptFacts": len(scripts["scripts"]), "highlightFrames": len(highlights["highlightFrames"]),
        },
        "files": ["manifest.json", "material-leaderboard.json", "audience.json", "scripts.json", "highlight-frames.json", "README.md"],
        "sourceFiles": unique_sources,
        "redactions": ["signed media URLs", "absolute paths", "RPA account and credential fields"],
        "quality": {
            "status": "built_with_warnings" if errors or any(item["parseStatus"] != "parsed" for item in scripts["scripts"]) else "built",
            "errors": errors,
            "categoryStatuses": {"materialLeaderboard": "available", "audience": audience["status"], "scripts": "available", "highlightFrames": highlights["status"]},
            "scriptParseStatuses": [item["parseStatus"] for item in scripts["scripts"]],
        },
    }
    _assert_no_sensitive(manifest)
    _write_json(output_dir / "manifest.json", manifest)
    readme = f"""# 千川 Top{target_top_n} 事实包

本目录是已完成 RPA 运行的脱敏规范化事实层；原始查询、诊断、互动曲线和高光帧工件保持不变。`targetTopN={target_top_n}`，四个分类文件只覆盖成功 RPA 选中的目标素材。

- `material-leaderboard.json`：素材榜单、平台维度字段、指标和来源引用。
- `audience.json`：素材 × 人群维度 × 展示指标事实。
- `scripts.json`：脚本结构化内容，以及 `missing`/`failed`/`parsed` 状态。
- `highlight-frames.json`：高光帧、JPEG 哈希、选帧证据；没有帧时 `status=no_highlight_frames`。
- `manifest.json`：TopN、分类计数、来源哈希、脱敏和质量状态。

所有 `sourceRef`/`artifact` 都是运行目录内的相对路径并带 SHA-256 与大小。事实包不包含签名媒体 URL、绝对路径、账号、会话或凭证，也不把 `materialType`/`videoType` 推断成业务结论。

离线验证：`python scripts/run_meta_skill.py verify-fact-pack --run-dir /path/to/run --fact-pack-dir /path/to/run/fact-pack`。
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")
    return manifest


def _verify_file_ref(run_dir: Path, ref: Any, label: str) -> Path:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise FactPackError(f"{label} must contain a relative path")
    raw_path = ref["path"]
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in raw_path:
        raise FactPackError(f"{label} path is not relative")
    path = (run_dir / Path(*relative.parts)).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise FactPackError(f"{label} path escapes run directory") from exc
    if not path.is_file() or path.stat().st_size <= 0:
        raise FactPackError(f"{label} is missing or empty")
    digest, size = _cached_file_hash(path)
    if ref.get("sizeBytes") != size or ref.get("sha256") != digest:
        raise FactPackError(f"{label} hash or size mismatch")
    return path


def _verify_payload_refs(run_dir: Path, payload: Any, label: str) -> None:
    if isinstance(payload, dict):
        if "sourceRef" in payload:
            _verify_file_ref(run_dir, payload["sourceRef"], f"{label}.sourceRef")
        for index, ref in enumerate(payload.get("sourceRefs", [])):
            _verify_file_ref(run_dir, ref, f"{label}.sourceRefs[{index}]")
        if "artifact" in payload:
            _verify_file_ref(run_dir, payload["artifact"], f"{label}.artifact")
        for key, value in payload.items():
            _verify_payload_refs(run_dir, value, f"{label}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            _verify_payload_refs(run_dir, value, f"{label}[{index}]")


def verify_fact_pack(run_dir: Path, fact_pack_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    fact_pack_dir = Path(fact_pack_dir).resolve()
    errors: list[str] = []
    try:
        top = _load_json(run_dir / "top_materials.json")
        target_top_n, selected = _completed_selection(run_dir, top)
        expected_ids = {str(item["materialId"]) for item in selected}
        names = ("manifest.json", "material-leaderboard.json", "audience.json", "scripts.json", "highlight-frames.json")
        payloads = {name: _load_json(fact_pack_dir / name) for name in names}
        for name, payload in payloads.items():
            _assert_no_sensitive(payload, name)
            _verify_payload_refs(run_dir, payload, name)
        manifest = payloads["manifest.json"]
        materials = payloads["material-leaderboard.json"]["materials"]
        audience_payload = payloads["audience.json"]
        scripts = payloads["scripts.json"]["scripts"]
        highlights_payload = payloads["highlight-frames.json"]
        highlights = highlights_payload["highlightFrames"]
        if manifest.get("targetTopN") != target_top_n:
            errors.append("manifest targetTopN does not match completed RPA")
        if {str(item["materialId"]) for item in materials} != expected_ids or len(materials) != target_top_n:
            errors.append("material IDs or count do not match targetTopN")
        top_by_id = {str(item["materialId"]): item for item in selected}
        for item in materials:
            if set(item.get("metrics", {})) != set(top_by_id[str(item["materialId"])].get("metrics", {})):
                errors.append(f"leaderboard metrics do not match source for {item.get('materialId')}")
        if len(scripts) != target_top_n:
            errors.append("script fact count does not match targetTopN")
        if any(str(item.get("materialId")) not in expected_ids for item in audience_payload.get("audience", [])):
            errors.append("audience contains a material outside targetTopN")
        if any(str(item.get("materialId")) not in expected_ids for item in highlights):
            errors.append("highlight frames contain a material outside targetTopN")
        source_frame_count = 0
        for material_id in expected_ids:
            summary = _load_json(artifact_path(run_dir / "rpa" / "peak-frames", material_id) / "summary.json")
            source_frame_count += len(summary.get("frames", []))
        if len(highlights) != source_frame_count:
            errors.append(f"highlight frame count mismatch: {len(highlights)} != {source_frame_count}")
        expected_counts = {"materials": len(materials), "audienceFacts": len(audience_payload.get("audience", [])), "scriptFacts": len(scripts), "highlightFrames": len(highlights)}
        if manifest.get("counts") != expected_counts:
            errors.append("manifest counts do not match category files")
    except (FactPackError, KeyError, TypeError, OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(str(exc))
    return {"status": "succeeded" if not errors else "failed", "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build a fact pack from a completed RPA run")
    build.add_argument("run_dir", type=Path)
    build.add_argument("--output-dir", type=Path)
    verify = subparsers.add_parser("verify", help="verify a fact pack using only local files")
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
