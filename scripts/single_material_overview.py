"""Project manifest-bound, per-material facts for the fifth report section."""

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath

try:
    import audience_overview as _audience
    from highlight_overview import HighlightOverviewError, build_highlight_overview
except ImportError:  # pragma: no cover - package-style imports.
    from . import audience_overview as _audience
    from .highlight_overview import HighlightOverviewError, build_highlight_overview


class SingleMaterialOverviewError(ValueError):
    """Raised when a single-material fact projection is not trustworthy."""


_INTERACTION_METRICS = {
    "CLICK_COUNT": ("整体点击次数", "点击"),
    "LOSE_COUNT": ("整体流失数", "流失"),
    "LIKE_COUNT": ("整体点赞次数", "点赞"),
    "COMMENT_COUNT": ("整体评论次数", "评论"),
    "SHARE_COUNT": ("整体转发次数", "转发"),
    "FOLLOW_COUNT": ("整体新增粉丝数", "新增粉丝"),
}
_INTERACTION_METRIC_ALIASES = {
    "CLICK_COUNT": "CLICK_COUNT",
    "LOSE_COUNT": "LOSE_COUNT",
    "LIKE_COUNT": "LIKE_COUNT",
    "COMMENT_COUNT": "COMMENT_COUNT",
    "SHARE_COUNT": "SHARE_COUNT",
    "FOLLOW_COUNT": "FOLLOW_COUNT",
    "NEW_FOLLOWER_COUNT": "FOLLOW_COUNT",
}
_FACT_FILES = (
    "material-leaderboard.json",
    "audience.json",
    "scripts.json",
    "highlight-frames.json",
)


def _read_json(path, label, expected_type=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SingleMaterialOverviewError(f"{label} is unreadable") from exc
    if expected_type is not None and not isinstance(value, expected_type):
        expected = "object" if expected_type is dict else "list"
        raise SingleMaterialOverviewError(f"{label} must be a {expected}")
    return value


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SingleMaterialOverviewError(f"{label} is required")
    return value.strip()


def _safe_text(value, label):
    text = _required_text(value, label)
    if "://" in text or text.startswith(("/", "\\")) or re.search(r"(?:^|[\\/])(?:Users|home|var|tmp)(?:[\\/]|$)", text, re.IGNORECASE):
        raise SingleMaterialOverviewError(f"{label} contains an unsafe value")
    return text


def _safe_relative_path(run_root, value, label):
    text = _required_text(value, label)
    relative = PurePosixPath(text)
    if relative.is_absolute() or "\\" in text or any(part in {"", ".", ".."} for part in relative.parts):
        raise SingleMaterialOverviewError(f"{label} must be a safe relative path")
    resolved = (Path(run_root) / Path(*relative.parts)).resolve()
    try:
        resolved.relative_to(Path(run_root).resolve())
    except ValueError as exc:
        raise SingleMaterialOverviewError(f"{label} escapes run") from exc
    return relative.as_posix(), resolved


def _hash_ref(run_root, ref, label, allowed=None, require_exists=True):
    if not isinstance(ref, dict):
        raise SingleMaterialOverviewError(f"{label} must be an object")
    path_text, path = _safe_relative_path(run_root, ref.get("path"), f"{label} path")
    if allowed is not None and path_text not in allowed:
        raise SingleMaterialOverviewError(f"{label} is not declared by manifest")
    expected_hash = _required_text(ref.get("sha256"), f"{label} sha256")
    expected_size = ref.get("sizeBytes")
    if len(expected_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_hash):
        raise SingleMaterialOverviewError(f"{label} sha256 is invalid")
    if type(expected_size) is not int or expected_size < 0:
        raise SingleMaterialOverviewError(f"{label} sizeBytes is invalid")
    if require_exists and not path.is_file():
        raise SingleMaterialOverviewError(f"{label} file is missing")
    if require_exists:
        actual = path.read_bytes()
        if len(actual) != expected_size or hashlib.sha256(actual).hexdigest() != expected_hash:
            raise SingleMaterialOverviewError(f"{label} hash or size mismatch")
    return path_text, path, expected_hash, expected_size


def _manifest_scope(manifest):
    ids = manifest.get("materialIds")
    counts = manifest.get("counts")
    if not isinstance(ids, list) or not ids:
        raise SingleMaterialOverviewError("manifest materialIds are missing")
    normalized = [_required_text(item, "manifest materialId") for item in ids]
    if len(set(normalized)) != len(normalized):
        raise SingleMaterialOverviewError("manifest materialIds are duplicated")
    if not isinstance(counts, dict) or counts.get("materials") != len(normalized):
        raise SingleMaterialOverviewError("manifest materials count does not match materialIds")
    return normalized, set(normalized)


def _manifest_sources(run_root, manifest):
    refs = manifest.get("sourceFiles")
    if not isinstance(refs, list):
        raise SingleMaterialOverviewError("manifest sourceFiles are missing")
    known = {}
    for index, ref in enumerate(refs):
        path_text, _path, digest, size = _hash_ref(run_root, ref, f"manifest sourceFile {index}")
        previous = known.get(path_text)
        if previous is not None and previous != (digest, size):
            raise SingleMaterialOverviewError("manifest sourceFile declarations disagree")
        known[path_text] = (digest, size)
    return known


def _safe_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    if isinstance(value, str):
        compact = value.replace(",", "").strip()
        if not compact or compact in {"--", "null", "None", "nan"}:
            return None
        try:
            result = float(compact.rstrip("%"))
        except ValueError:
            return None
        return int(result) if result.is_integer() else result
    return None


def _record_indexes_match(records, ref, material_id, label):
    indexes = ref.get("recordIndexes") if isinstance(ref, dict) else None
    if indexes is None:
        return
    if not isinstance(indexes, list) or any(type(index) is not int or index < 0 or index >= len(records) for index in indexes):
        raise SingleMaterialOverviewError(f"{label} recordIndexes are invalid")
    for index in indexes:
        record = records[index]
        if not isinstance(record, dict) or str(record.get("materialId", "")).strip() != material_id:
            raise SingleMaterialOverviewError(f"{label} material association mismatch")


def _load_declared_records(run_root, ref, allowed, material_id, label):
    path_text, path, _digest, _size = _hash_ref(run_root, ref, label, allowed)
    records = _read_json(path, label, list)
    _record_indexes_match(records, ref, material_id, label)
    return path_text, records


def _leaderboard(run_root, payload, scope_ids, allowed):
    materials = payload.get("materials")
    if not isinstance(materials, list):
        raise SingleMaterialOverviewError("leaderboard materials are missing")
    if {str(item.get("materialId")) for item in materials if isinstance(item, dict)} != scope_ids:
        raise SingleMaterialOverviewError("leaderboard materialIds do not match manifest materialIds")
    output = []
    for index, item in enumerate(materials):
        if not isinstance(item, dict):
            raise SingleMaterialOverviewError(f"leaderboard material {index} is invalid")
        material_id = _required_text(str(item.get("materialId", "")), "leaderboard materialId")
        title = _safe_text(item.get("materialName"), "leaderboard material title")
        rank = item.get("rank")
        if type(rank) is not int or rank <= 0:
            raise SingleMaterialOverviewError("leaderboard rank is invalid")
        source_ref = item.get("sourceRef")
        _hash_ref(run_root, source_ref, f"leaderboard {material_id} sourceRef", allowed)
        fields = []
        metrics = item.get("metrics", [])
        metric_items = metrics if isinstance(metrics, list) else [
            {"key": key, "value": value} for key, value in metrics.items()
        ] if isinstance(metrics, dict) else []
        for metric in metric_items:
            if not isinstance(metric, dict) or not isinstance(metric.get("key"), str):
                continue
            value = metric.get("value")
            safe_value = _safe_number(value)
            fields.append({"id": metric["key"], "label": metric["key"], "value": safe_value if safe_value is not None else value})
        output.append({"materialId": material_id, "rank": rank, "title": title, "fields": fields})
    return sorted(output, key=lambda item: item["rank"])


def _interactions(run_root, frame_records, allowed, material_id):
    refs = []
    for frame in frame_records:
        series = frame.get("interactionSeries") if isinstance(frame, dict) else None
        if isinstance(series, dict) and isinstance(series.get("sourceRef"), dict):
            refs.append(series["sourceRef"])
    if not refs:
        return {"status": "unavailable", "metrics": [], "gaps": [{"code": "interaction_series_unavailable"}]}
    unique = {}
    for ref in refs:
        path_text, records = _load_declared_records(run_root, ref, allowed, material_id, "interaction sourceRef")
        unique[path_text] = records
    buckets = {metric_id: [] for metric_id in _INTERACTION_METRICS}
    for records in unique.values():
        for record in records:
            if not isinstance(record, dict) or record.get("recordType") != "interaction_series":
                continue
            if str(record.get("materialId", "")).strip() != material_id:
                raise SingleMaterialOverviewError("interaction record material association mismatch")
            metric_id = _INTERACTION_METRIC_ALIASES.get(record.get("interactionMetric"))
            value = _safe_number(record.get("metricValue"))
            second = _safe_number(record.get("durationSec"))
            if metric_id in buckets and value is not None and second is not None:
                buckets[metric_id].append({"second": second, "value": value})
    metrics = []
    for metric_id, (label, short_label) in _INTERACTION_METRICS.items():
        points = sorted({(item["second"], item["value"]) for item in buckets[metric_id]})
        metrics.append({"id": metric_id, "label": label, "shortLabel": short_label, "points": [{"second": second, "value": value} for second, value in points]})
    available = [item for item in metrics if item["points"]]
    return {"status": "available" if available else "unavailable", "metrics": metrics if available else [], "gaps": [] if available else [{"code": "interaction_series_values_unavailable"}]}


def _script_for_material(payload, run_root, allowed, material_id):
    records = payload.get("scripts")
    if not isinstance(records, list):
        return {"status": "unavailable", "formula": [], "text": None, "segments": [], "gaps": [{"code": "script_unavailable"}]}
    record = next((item for item in records if isinstance(item, dict) and str(item.get("materialId")) == material_id), None)
    if record is None:
        return {"status": "unavailable", "formula": [], "text": None, "segments": [], "gaps": [{"code": "script_material_missing"}]}
    _hash_ref(run_root, record.get("sourceRef"), f"script {material_id} sourceRef", allowed)
    if record.get("parseStatus") != "parsed" or not isinstance(record.get("data"), list):
        return {"status": "unavailable", "formula": [], "text": None, "segments": [], "gaps": [{"code": "formula_labels_unavailable"}]}
    formula = []
    for item in record["data"]:
        if not isinstance(item, dict):
            continue
        primary_node = item.get("level_1_label")
        primary = primary_node.get("label") if isinstance(primary_node, dict) else None
        if not isinstance(primary, str) or not primary.strip():
            continue
        primary = _safe_text(primary, "script formula label")
        secondary = []
        for node in item.get("level_2_labels", []):
            if isinstance(node, dict) and isinstance(node.get("label"), str) and node["label"].strip():
                secondary.append(_safe_text(node["label"], "script formula label"))
        formula.append({"primary": primary.strip(), "secondary": sorted(set(secondary))})
    detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
    segments = []
    for value in detail.values():
        if not isinstance(value, dict) or not isinstance(value.get("label"), str):
            continue
        segment_label = _safe_text(value["label"], "script detail label")
        snippets = value.get("list") if isinstance(value.get("list"), list) else []
        for snippet in snippets:
            if isinstance(snippet, str) and snippet.strip():
                segments.append({"label": segment_label, "text": _safe_text(snippet, "script detail text")})
    text = _safe_text(record["text"], "script text") if isinstance(record.get("text"), str) and record["text"].strip() else None
    return {"status": "available" if formula or text or segments else "unavailable", "formula": formula, "text": text, "segments": segments, "gaps": [] if formula or text or segments else [{"code": "script_content_unavailable"}]}


def _audience_for_material(payload, run_root, allowed, material_id):
    records = payload.get("audience")
    if not isinstance(records, list):
        return {"status": "unavailable", "dimensions": {}, "gaps": [{"code": "audience_unavailable"}]}
    selected = [item for item in records if isinstance(item, dict) and str(item.get("materialId")) == material_id]
    if not selected:
        return {"status": "unavailable", "dimensions": {}, "gaps": [{"code": "audience_material_missing"}]}
    dimensions = {}
    for record in selected:
        _hash_ref(run_root, record.get("sourceRef"), f"audience {material_id} sourceRef", allowed)
        dimension_key = record.get("dimensionKey")
        dimension_info = _audience.DIMENSIONS.get(dimension_key)
        if dimension_info is None:
            raise SingleMaterialOverviewError("unsupported audience dimension")
        dimensions.setdefault(dimension_info[0], {
            "label": dimension_info[1],
            "chart": dimension_info[2],
            "bucketsByMetric": {},
        })
        raw = record.get("metricValue")
        value = _safe_number(raw)
        if value is None and isinstance(record.get("Metrics"), dict):
            metric = _audience._METRIC_BY_ID.get(record.get("displayMetric"))
            if metric and isinstance(record["Metrics"].get(metric["sourceField"]), dict):
                value = _safe_number(record["Metrics"][metric["sourceField"]].get("Value"))
        label_node = record.get("audienceValue")
        label = label_node.get("ValueStr") or label_node.get("valueStr") if isinstance(label_node, dict) else None
        if not isinstance(label, str) or not label.strip() or value is None:
            continue
        metric_id = record.get("displayMetric")
        dimensions[dimension_info[0]]["bucketsByMetric"].setdefault(metric_id, []).append({
            "label": label.strip(),
            "value": value,
            "metricId": metric_id,
        })
    for item in dimensions.values():
        for metric_id, buckets in item["bucketsByMetric"].items():
            total = sum(bucket["value"] for bucket in buckets)
            for bucket in buckets:
                bucket["share"] = bucket["value"] / total if total else 0
                bucket["total"] = total
            item["bucketsByMetric"][metric_id] = buckets
    return {
        "status": "available" if dimensions else "unavailable",
        "metrics": [dict(metric) for metric in _audience.AUDIENCE_METRICS],
        "dimensions": dimensions,
        "gaps": [] if dimensions else [{"code": "audience_values_unavailable"}],
    }


def _video_artifact(run_root, manifest, allowed, material_id):
    artifacts = manifest.get("videoArtifacts")
    if not isinstance(artifacts, list):
        return {"status": "unavailable", "message": "本次详情数据未保存可播放视频"}
    selected = next((item for item in artifacts if isinstance(item, dict) and str(item.get("materialId")) == material_id), None)
    if selected is None:
        return {"status": "unavailable", "message": "本次详情数据未保存可播放视频"}
    path_text, _path, digest, size = _hash_ref(run_root, selected, f"video {material_id} artifact", allowed)
    if Path(path_text).suffix.lower() not in {".mp4", ".webm"}:
        raise SingleMaterialOverviewError("video artifact extension is invalid")
    return {"status": "available", "path": path_text, "sha256": digest, "sizeBytes": size}


def _ensure_fact_ids_in_scope(payload, key, scope_ids, label):
    records = payload.get(key)
    if not isinstance(records, list):
        return
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise SingleMaterialOverviewError(f"{label} record {index} is invalid")
        material_id = str(record.get("materialId", "")).strip()
        if material_id not in scope_ids:
            raise SingleMaterialOverviewError(f"{label} materialId is outside manifest scope")


def build_single_material_overview(run_dir: Path) -> dict:
    run_root = Path(run_dir).resolve()
    if not run_root.is_dir():
        raise SingleMaterialOverviewError("run directory is missing")
    fact_dir = run_root / "fact-pack"
    manifest = _read_json(fact_dir / "manifest.json", "fact-pack manifest", dict)
    material_ids, scope_ids = _manifest_scope(manifest)
    allowed = _manifest_sources(run_root, manifest)
    payloads = {name: _read_json(fact_dir / name, name, dict) for name in _FACT_FILES}
    _ensure_fact_ids_in_scope(payloads["audience.json"], "audience", scope_ids, "audience")
    _ensure_fact_ids_in_scope(payloads["scripts.json"], "scripts", scope_ids, "scripts")
    _ensure_fact_ids_in_scope(payloads["highlight-frames.json"], "highlightFrames", scope_ids, "highlight")
    leaderboard = _leaderboard(run_root, payloads["material-leaderboard.json"], scope_ids, allowed)
    highlight_payload = payloads["highlight-frames.json"]
    frames = highlight_payload.get("highlightFrames")
    if not isinstance(frames, list):
        frames = []
    try:
        highlight_model = build_highlight_overview(run_root)
    except HighlightOverviewError as exc:
        raise SingleMaterialOverviewError(str(exc)) from exc
    cards_by_material = {}
    for card in highlight_model.get("cards", []):
        material_id = card.get("materialId")
        if material_id not in scope_ids:
            raise SingleMaterialOverviewError("highlight materialId is outside manifest scope")
        cards_by_material.setdefault(material_id, []).append(card)
    material_models = []
    for material in leaderboard:
        material_id = material["materialId"]
        material_frames = [frame for frame in frames if isinstance(frame, dict) and str(frame.get("materialId")) == material_id]
        for frame in material_frames:
            _hash_ref(run_root, frame.get("artifact"), f"highlight {material_id} artifact", allowed)
        material_models.append({
            "materialId": material_id,
            "rank": material["rank"],
            "title": material["title"],
            "leaderboard": {"fields": material["fields"]},
            "interactions": _interactions(run_root, material_frames, allowed, material_id),
            "script": _script_for_material(payloads["scripts.json"], run_root, allowed, material_id),
            "audience": _audience_for_material(payloads["audience.json"], run_root, allowed, material_id),
            "highlights": {"status": "available" if cards_by_material.get(material_id) else "unavailable", "cards": cards_by_material.get(material_id, []), "video": _video_artifact(run_root, manifest, allowed, material_id)},
        })
    return {
        "schemaVersion": 1,
        "status": "available",
        "runId": run_root.name,
        "materialScope": {"count": len(material_ids), "materialIds": [item["materialId"] for item in leaderboard]},
        "source": {"files": [{"path": f"fact-pack/{name}", "sha256": hashlib.sha256((fact_dir / name).read_bytes()).hexdigest(), "sizeBytes": (fact_dir / name).stat().st_size} for name in _FACT_FILES]},
        "materials": material_models,
    }
