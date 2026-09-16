"""Build a source-verified, aggregate-only audience overview for an offline run."""

import hashlib
import json
import math
from pathlib import Path


class AudienceOverviewError(ValueError):
    """Raised when audience facts cannot be trusted for presentation."""


AUDIENCE_METRICS = (
    {"id": "SHOW_COUNT", "label": "整体展现次数", "sourceField": "live_show_count_for_roi2_v2", "unit": "count"},
    {"id": "TRANSACTION_AMOUNT", "label": "整体成交金额", "sourceField": "total_pay_order_gmv_include_coupon_for_roi2", "unit": "currency"},
    {"id": "CLICK_COUNT", "label": "整体点击次数", "sourceField": "live_watch_count_for_roi2_v2", "unit": "count"},
)

DIMENSIONS = {
    "user_group_label_name": ("eight_groups", "八大人群分布", "bar"),
    "gender": ("gender", "性别分布", "donut"),
    "age": ("age", "年龄分布", "donut"),
    "province_name": ("province", "省份分布", "bar"),
    "city_name": ("city", "城市分布", "bar"),
}

_DIMENSION_NAMES = {
    "user_group_label_name": "八大人群",
    "gender": "性别",
    "age": "年龄",
    "province_name": "省份",
    "city_name": "城市",
}

_AGE_ORDER = {label: index for index, label in enumerate(("18岁以下", "18-23", "24-30", "31-40", "41-50", "50岁以上"))}
_METRIC_BY_ID = {item["id"]: item for item in AUDIENCE_METRICS}


def _read_json(path, label, expected_type=None):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AudienceOverviewError("%s is unreadable" % label) from exc
    if expected_type is not None and not isinstance(payload, expected_type):
        expected = "object" if expected_type is dict else "list"
        raise AudienceOverviewError("%s must be a %s" % (label, expected))
    return payload


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AudienceOverviewError("%s is required" % label)
    return value.strip()


def _display_text(value, label):
    text = _required_text(value, label)
    if "://" in text or text.startswith(("/", "\\")):
        raise AudienceOverviewError("%s contains an unsafe value" % label)
    return text


def _safe_relative_path(run_root, value, label):
    path_text = _required_text(value, label)
    candidate = Path(path_text)
    if candidate.is_absolute() or "\\" in path_text or any(part in {"", ".", ".."} for part in candidate.parts):
        raise AudienceOverviewError("%s must be a safe relative path" % label)
    resolved = (run_root / candidate).resolve()
    try:
        resolved.relative_to(run_root.resolve())
    except ValueError as exc:
        raise AudienceOverviewError("%s must remain inside run" % label) from exc
    return candidate.as_posix(), resolved


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _manifest_scope(manifest):
    quality = manifest.get("quality")
    statuses = quality.get("categoryStatuses") if isinstance(quality, dict) else None
    if not isinstance(statuses, dict) or statuses.get("audience") != "available":
        raise AudienceOverviewError("audience status is unavailable")
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or type(counts.get("audienceFacts")) is not int:
        raise AudienceOverviewError("manifest audienceFacts count is missing")
    material_ids = manifest.get("materialIds")
    if not isinstance(material_ids, list) or not material_ids:
        raise AudienceOverviewError("manifest materialIds are missing")
    normalized = []
    for material_id in material_ids:
        value = _required_text(material_id, "manifest materialId")
        if value in normalized:
            raise AudienceOverviewError("manifest materialIds are duplicated")
        normalized.append(value)
    if counts.get("materials") is not None and counts.get("materials") != len(normalized):
        raise AudienceOverviewError("manifest materials count does not match materialIds")
    return counts["audienceFacts"], set(normalized)


def _verify_source_ref(run_root, ref, label, known=None, require_exists=True):
    if not isinstance(ref, dict):
        raise AudienceOverviewError("%s must be an object" % label)
    path_text, path = _safe_relative_path(run_root, ref.get("path"), "%s path" % label)
    if not path_text.startswith("rpa/diagnosis-records/") or not path_text.endswith(".json"):
        raise AudienceOverviewError("%s must point to a diagnosis record" % label)
    expected_hash = _required_text(ref.get("sha256"), "%s sha256" % label)
    if len(expected_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_hash):
        raise AudienceOverviewError("%s sha256 is invalid" % label)
    expected_size = ref.get("sizeBytes")
    if type(expected_size) is not int or expected_size < 0:
        raise AudienceOverviewError("%s sizeBytes is invalid" % label)
    expected = (expected_hash, expected_size)
    if known is not None:
        declared = known.get(path_text)
        if declared is None:
            raise AudienceOverviewError("%s is not declared" % label)
        if declared != expected:
            raise AudienceOverviewError("%s disagrees" % label)
    if require_exists and not path.is_file():
        raise AudienceOverviewError("%s file is missing" % label)
    if require_exists:
        actual_size = path.stat().st_size
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_size != expected_size:
            raise AudienceOverviewError("%s size mismatch" % label)
        if actual_hash != expected_hash:
            raise AudienceOverviewError("%s sha256 mismatch" % label)
    return path_text, expected_hash, expected_size


def _verify_sources(run_root, payload):
    source_refs = payload.get("sourceRefs")
    if not isinstance(source_refs, list) or not source_refs:
        raise AudienceOverviewError("audience sourceRefs are missing")
    known = {}
    for index, ref in enumerate(source_refs):
        label = "sourceRef %d" % index
        path_text, expected_hash, expected_size = _verify_source_ref(
            run_root, ref, label, require_exists=False
        )
        if path_text in known and known[path_text] != (expected_hash, expected_size):
            raise AudienceOverviewError("sourceRef declarations disagree")
        if path_text not in known:
            _verify_source_ref(run_root, ref, label)
        known[path_text] = (expected_hash, expected_size)
    return known


def _resolve_value(record, metric):
    metric_value = _finite_number(record.get("metricValue"))
    if metric_value is not None:
        return metric_value, None
    metrics = record["Metrics"]
    source = metrics[metric["sourceField"]]
    fallback = _finite_number(source.get("Value"))
    if fallback is not None:
        return fallback, "metric_fallback"
    return None, "metric_value_missing"


def _validate_record(run_root, record, scope_ids, source_refs):
    if not isinstance(record, dict):
        raise AudienceOverviewError("audience record must be an object")
    material_id = _required_text(record.get("materialId"), "audience materialId")
    if material_id not in scope_ids:
        raise AudienceOverviewError("audience materialId is outside manifest scope")
    dimension_key = record.get("dimensionKey")
    if dimension_key not in DIMENSIONS:
        raise AudienceOverviewError("unsupported audience dimension")
    dimensions = record.get("Dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != {dimension_key}:
        raise AudienceOverviewError("audience dimension shape is invalid")
    dimension_value = dimensions[dimension_key]
    if not isinstance(dimension_value, dict):
        raise AudienceOverviewError("audience dimension value is invalid")
    display_label = _display_text(dimension_value.get("ValueStr"), "audience dimension label")
    audience_value = record.get("audienceValue")
    if record.get("dimensionName") != _DIMENSION_NAMES[dimension_key]:
        raise AudienceOverviewError("audience dimension name is invalid")
    if not isinstance(audience_value, dict) or _display_text(audience_value.get("ValueStr"), "audienceValue label") != display_label:
        raise AudienceOverviewError("audienceValue label is inconsistent")
    display_metric = record.get("displayMetric")
    metric = _METRIC_BY_ID.get(display_metric)
    if metric is None or record.get("displayMetricName") != metric["label"]:
        raise AudienceOverviewError("audience metric is invalid")
    metrics = record.get("Metrics")
    if not isinstance(metrics, dict) or set(metrics) != {metric["sourceField"]} or not isinstance(metrics[metric["sourceField"]], dict):
        raise AudienceOverviewError("audience metric source field is invalid")
    source_ref = record.get("sourceRef")
    path_text, expected_hash, expected_size = _verify_source_ref(
        run_root, source_ref, "audience record sourceRef", source_refs, require_exists=False
    )
    if source_refs[path_text] != (expected_hash, expected_size):
        raise AudienceOverviewError("audience record sourceRef disagrees")
    return material_id, dimension_key, display_label, metric, _resolve_value(record, metric)


def _sort_bucket(item, dimension_id):
    if dimension_id == "age":
        return (_AGE_ORDER.get(item["label"], len(_AGE_ORDER)), item["label"])
    return (-item["value"], item["label"])


def _dimension_model(label, chart):
    return {"label": label, "chart": chart, "bucketsByMetric": {}}


def build_audience_overview(run_dir):
    run_root = Path(run_dir).resolve()
    if not run_root.is_dir():
        raise AudienceOverviewError("run directory is missing")
    fact_path = run_root / "fact-pack" / "audience.json"
    manifest_path = run_root / "fact-pack" / "manifest.json"
    payload = _read_json(fact_path, "audience facts", dict)
    manifest = _read_json(manifest_path, "fact-pack manifest", dict)
    expected_count, scope_ids = _manifest_scope(manifest)
    records = payload.get("audience")
    if payload.get("status") != "available" or not isinstance(records, list):
        raise AudienceOverviewError("audience facts are unavailable")
    if expected_count != len(records):
        raise AudienceOverviewError("manifest audienceFacts count does not match audience facts")
    source_refs = _verify_sources(run_root, payload)

    dimensions = {
        "eight_groups": _dimension_model("八大人群分布", "bar"),
        "gender": _dimension_model("性别分布", "donut"),
        "age": _dimension_model("年龄分布", "donut"),
        "geography": {
            "label": "地域分布",
            "chart": "bar",
            "levels": {
                "province": _dimension_model("省份分布", "bar"),
                "city": _dimension_model("城市分布", "bar"),
            },
        },
    }
    aggregates = {}
    gaps = {}
    for record in records:
        material_id, dimension_key, display_label, metric, resolved = _validate_record(
            run_root, record, scope_ids, source_refs
        )
        metric_id = metric["id"]
        dimension_id = DIMENSIONS[dimension_key][0]
        if dimension_id in {"province", "city"}:
            target = dimensions["geography"]["levels"][dimension_id]
        else:
            target = dimensions[dimension_id]
        key = (metric_id, dimension_id, display_label)
        if resolved[0] is None:
            gap_key = (resolved[1], metric_id, dimension_id)
            gaps[gap_key] = gaps.get(gap_key, 0) + 1
            continue
        bucket = aggregates.setdefault(key, {"value": 0.0, "recordCount": 0, "materials": set()})
        bucket["value"] += resolved[0]
        bucket["recordCount"] += 1
        bucket["materials"].add(material_id)

    for dimension_id, dimension in (
        ("eight_groups", dimensions["eight_groups"]),
        ("gender", dimensions["gender"]),
        ("age", dimensions["age"]),
        ("province", dimensions["geography"]["levels"]["province"]),
        ("city", dimensions["geography"]["levels"]["city"]),
    ):
        for metric in AUDIENCE_METRICS:
            metric_id = metric["id"]
            buckets = []
            for (bucket_metric, bucket_dimension, display_label), aggregate in aggregates.items():
                if bucket_metric != metric_id or bucket_dimension != dimension_id:
                    continue
                buckets.append((display_label, aggregate))
            total = sum(aggregate["value"] for _, aggregate in buckets)
            output = []
            for display_label, aggregate in sorted(
                buckets,
                key=lambda item: _sort_bucket({"label": item[0], "value": item[1]["value"]}, dimension_id),
            ):
                value = round(aggregate["value"], 10)
                normalized_total = round(total, 10)
                output.append({
                    "label": display_label,
                    "value": value if value % 1 else int(value),
                    "share": value / normalized_total if normalized_total else 0,
                    "total": normalized_total if normalized_total % 1 else int(normalized_total),
                    "recordCount": aggregate["recordCount"],
                    "materialCount": len(aggregate["materials"]),
                })
            dimension["bucketsByMetric"][metric_id] = output

    coverage = {}
    for dimension_id, dimension in (
        ("eight_groups", dimensions["eight_groups"]),
        ("gender", dimensions["gender"]),
        ("age", dimensions["age"]),
        ("province", dimensions["geography"]["levels"]["province"]),
        ("city", dimensions["geography"]["levels"]["city"]),
    ):
        buckets = dimension["bucketsByMetric"]["SHOW_COUNT"]
        coverage[dimension_id] = {
            "categoryCount": len(buckets),
            "validRecordCount": sum(item["recordCount"] for item in buckets),
            "materialCount": max((item["materialCount"] for item in buckets), default=0),
        }

    return {
        "schemaVersion": 1,
        "status": "available",
        "source": {
            "path": "fact-pack/audience.json",
            "sha256": hashlib.sha256(fact_path.read_bytes()).hexdigest(),
            "recordCount": len(records),
        },
        "materialScope": {"count": len(scope_ids)},
        "metrics": [dict(metric) for metric in AUDIENCE_METRICS],
        "dimensions": dimensions,
        "coverage": {
            "eight_groups": coverage["eight_groups"],
            "gender": coverage["gender"],
            "age": coverage["age"],
            "geography": {"province": coverage["province"], "city": coverage["city"]},
        },
        "gaps": [
            {"code": code, "metric": metric_id, "dimension": dimension_id, "count": count}
            for (code, metric_id, dimension_id), count in sorted(gaps.items())
        ],
    }
