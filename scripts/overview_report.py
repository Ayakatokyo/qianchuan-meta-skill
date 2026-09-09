"""Build a redacted, offline overview of every material in a completed run."""

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from field_registry import FieldRegistryError, load_field_registry

try:
    from audience_overview import AudienceOverviewError, build_audience_overview
except ImportError:  # pragma: no cover - supports package-style imports.
    from .audience_overview import AudienceOverviewError, build_audience_overview
try:
    from script_formula_overview import ScriptFormulaOverviewError, build_script_formula_overview
except ImportError:  # pragma: no cover - supports package-style imports.
    from .script_formula_overview import ScriptFormulaOverviewError, build_script_formula_overview
try:
    from highlight_overview import HighlightOverviewError, build_highlight_overview
except ImportError:  # pragma: no cover - supports package-style imports.
    from .highlight_overview import HighlightOverviewError, build_highlight_overview
try:
    from single_material_overview import SingleMaterialOverviewError, build_single_material_overview
except ImportError:  # pragma: no cover - supports package-style imports.
    from .single_material_overview import SingleMaterialOverviewError, build_single_material_overview


SCHEMA_VERSION = 1
OVERVIEW_KIND = "qianchuan_material_leaderboard_overview"
PAGE_SIZE = 10
_SENSITIVE_FIELD_IDS = {"roi2_material_video_play_info", "anchor_id"}
_DEFAULT_FIELD_IDS = (
    "roi2_material_video_name",
    "material_duration_sec",
    "material_create_time_v2",
    "material_source",
    "stat_cost_for_overall_roi2",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_order_settle_amount_for_roi2_1h",
)
_FIELD_GROUP_ORDER = (
    "素材基础信息",
    "整体指标",
    "净成交指标",
    "周期结算",
    "追投调控",
    "视频互动",
)
_SENSITIVE_CONTEXT_FIELDS = {"anchor_id", "shop_id", "advertiser_id", "account_id", "shopId", "advertiserId"}
_SENSITIVE_TEXT = re.compile(r"(?:https?|file)://|(?:^|[\\/])(?:Users|home|var|tmp)(?:[\\/]|$)", re.IGNORECASE)
_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


class OverviewReportError(ValueError):
    """Raised when a run cannot produce a trustworthy overview."""


def _read_json(path, label, expected_type=None):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OverviewReportError("%s is unreadable" % label) from exc
    if expected_type is not None and not isinstance(payload, expected_type):
        expected = "object" if expected_type is dict else "list"
        raise OverviewReportError("%s must be a %s" % (label, expected))
    return payload


def _text(value, label):
    result = str(value or "").strip()
    if not result:
        raise OverviewReportError("%s is required" % label)
    return result


def _inside(base, candidate):
    base_path = Path(base).resolve()
    candidate_path = Path(candidate).resolve()
    try:
        candidate_path.relative_to(base_path)
    except ValueError as exc:
        raise OverviewReportError("path must remain inside %s" % base_path.name) from exc
    return candidate_path


def _structured_source_path(run_dir):
    source_dir = Path(run_dir) / "source-data"
    status = _read_json(source_dir / "result-parse-status.json", "result parse status", dict)
    filename = _text(status.get("filename"), "result parse filename")
    if Path(filename).name != filename or "\\" in filename or filename in {".", ".."}:
        raise OverviewReportError("source path filename must be a safe file name")
    return _inside(source_dir, source_dir / (filename + ".structured.json")), status


def _source_value(record, source):
    value = record
    for key in source:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if isinstance(value, dict):
        if "ValueStr" in value:
            value = value["ValueStr"]
        elif "Value" in value:
            value = value["Value"]
    return value


def _safe_scalar(value):
    if value is None or isinstance(value, bool):
        return None if value is None else value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or _SENSITIVE_TEXT.search(value):
        return None
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    return value


def _number_or_text(value):
    value = _safe_scalar(value)
    if value is None or isinstance(value, (int, float)):
        return value
    if not isinstance(value, str) or value.lower() in {"null", "none", "nan", "--"}:
        return None
    compact = value.replace(",", "")
    if not _NUMBER.fullmatch(compact):
        return value
    if "." not in compact:
        return int(compact)
    number = float(compact)
    return number if math.isfinite(number) else None


def _field_group(field_id, kind):
    if kind == "dimension" or field_id.startswith("material_"):
        return "素材基础信息"
    if field_id.startswith("additional_delivery_"):
        return "追投调控"
    if re.search(r"_(?:7d|14d|30d|90d)(?:_|$)", field_id):
        return "周期结算"
    if any(token in field_id for token in ("settle", "refund", "no_refund")):
        return "净成交指标"
    if field_id.startswith("video_") or field_id.startswith("live_"):
        return "视频互动"
    return "整体指标"


def _value_type(field_id, kind, derived=False):
    if derived and field_id == "material_duration_sec":
        return "duration"
    if field_id == "material_create_time_v2":
        return "date"
    if kind == "metric" or field_id == "material_source":
        return "number"
    return "text"


def _field_entry(entry, *, derived=False, source=None, unit=None):
    field_id = entry["id"]
    kind = entry.get("kind", "dimension")
    return {
        "id": field_id,
        "label": entry.get("label") or field_id,
        "kind": kind,
        "group": _field_group(field_id, kind),
        "unit": unit if unit is not None else entry.get("unit"),
        "source": list(source if source is not None else entry.get("source", [])),
        "sortable": bool(entry.get("sortable", False) or derived),
        "valueType": _value_type(field_id, kind, derived=derived),
        "derived": derived,
    }


def _derived_fields():
    return [
        {
            "id": "material_duration_sec",
            "label": "素材时长",
            "kind": "dimension",
            "group": "素材基础信息",
            "unit": "秒",
            "source": ["dimensions", "roi2_material_video_play_info", "ValueStr", "VideoDuration"],
            "sortable": True,
            "valueType": "duration",
            "derived": True,
        },
        {
            "id": "material_source",
            "label": "素材来源",
            "kind": "dimension",
            "group": "素材基础信息",
            "unit": None,
            "source": ["dimensions", "roi2_material_video_play_info", "ValueStr", "LegoSource"],
            "sortable": True,
            "valueType": "number",
            "derived": True,
        },
    ]


def _query_context(receipt):
    query = receipt.get("query") if isinstance(receipt.get("query"), dict) else {}
    period = query.get("period") if isinstance(query.get("period"), dict) else {}
    safe_period = {
        key: _safe_scalar(period.get(key))
        for key in ("startDate", "endDate")
        if _safe_scalar(period.get(key)) is not None
    }

    def safe_filters(key):
        items = query.get(key)
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            field = _safe_scalar(item.get("field"))
            operator = _safe_scalar(item.get("operator"))
            value = item.get("value")
            if field is None or operator is None or field in _SENSITIVE_CONTEXT_FIELDS:
                continue
            if isinstance(value, list):
                value = [_safe_scalar(part) for part in value]
            else:
                value = _safe_scalar(value)
            result.append({"field": field, "operator": operator, "value": value})
        return result

    ranking = receipt.get("sort") if isinstance(receipt.get("sort"), dict) else query.get("sort", {})
    safe_ranking = {
        key: _safe_scalar(ranking.get(key))
        for key in ("field", "direction")
        if _safe_scalar(ranking.get(key)) is not None
    }
    return {
        "period": safe_period,
        "filters": {
            "dimensions": safe_filters("dimensionFilters"),
            "metrics": safe_filters("metricFilters"),
        },
        "ranking": safe_ranking,
    }


def _play_info(record):
    value = _source_value(record, ["dimensions", "roi2_material_video_play_info"])
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _field_ids(field_manifest):
    dimensions = field_manifest.get("dimensions")
    metrics = field_manifest.get("metrics")
    if not isinstance(dimensions, list) or not isinstance(metrics, list):
        raise OverviewReportError("field manifest dimensions and metrics must be lists")
    result = []
    for field_id in dimensions + metrics:
        field_id = _text(field_id, "field manifest field")
        if field_id in result:
            raise OverviewReportError("field manifest has duplicate field: %s" % field_id)
        result.append(field_id)
    return result


def build_overview_data(run_dir, registry_path):
    run_root = Path(run_dir).resolve()
    if not run_root.is_dir():
        raise OverviewReportError("run directory is missing")
    structured_path, status = _structured_source_path(run_root)
    status_count = status.get("recordCount")
    if type(status_count) is not int or status_count < 0:
        raise OverviewReportError("result parse recordCount must be a non-negative integer")
    source_rows = _read_json(structured_path, "structured source", list)
    if len(source_rows) != status_count:
        raise OverviewReportError("recordCount does not match structured source")

    field_manifest = _read_json(run_root / "source-data" / "field-manifest.json", "field manifest", dict)
    declared_ids = _field_ids(field_manifest)
    query_receipt = _read_json(run_root / "query-receipt.json", "query receipt", dict)
    registry = load_field_registry(registry_path)

    if field_manifest.get("recordCount") is not None and field_manifest.get("recordCount") != status_count:
        raise OverviewReportError("field manifest recordCount does not match source")
    if query_receipt.get("sourceRecordCount") is not None and query_receipt.get("sourceRecordCount") != status_count:
        raise OverviewReportError("query receipt sourceRecordCount does not match source")

    fields = []
    declared_set = set(declared_ids)
    for field_id in declared_ids:
        try:
            entry = registry.require(field_id)
        except FieldRegistryError as exc:
            raise OverviewReportError(str(exc)) from exc
        if field_id in _SENSITIVE_FIELD_IDS or field_id == "material_id":
            continue
        fields.append(_field_entry(entry))
    if "roi2_material_video_play_info" in declared_set:
        fields.extend(_derived_fields())

    group_order = {group: index for index, group in enumerate(_FIELD_GROUP_ORDER)}
    default_order = {field_id: index for index, field_id in enumerate(_DEFAULT_FIELD_IDS)}
    fields.sort(key=lambda field: (
        group_order.get(field["group"], len(group_order)),
        default_order.get(field["id"], len(default_order)),
    ))

    field_by_id = {field["id"]: field for field in fields}
    rows = []
    for index, record in enumerate(source_rows):
        if not isinstance(record, dict):
            raise OverviewReportError("source record %d must be an object" % index)
        values = {}
        for field in fields:
            field_id = field["id"]
            if field.get("derived"):
                continue
            value = _source_value(record, field["source"])
            if field["valueType"] == "number":
                value = _number_or_text(value)
            else:
                value = _safe_scalar(value)
            values[field_id] = value

        material_id = _source_value(record, ["dimensions", "material_id"])
        if material_id is None or str(material_id).strip() == "":
            raise OverviewReportError("source record %d has no material_id" % index)
        play_info = _play_info(record)
        duration = _number_or_text(play_info.get("VideoDuration"))
        source = _number_or_text(play_info.get("LegoSource"))
        if "material_duration_sec" in field_by_id:
            values["material_duration_sec"] = duration
        if "material_source" in field_by_id:
            values["material_source"] = source
        rows.append({"materialId": str(material_id).strip(), "values": values})

    source_hash = hashlib.sha256(structured_path.read_bytes()).hexdigest()
    relative_source = structured_path.relative_to(run_root).as_posix()
    default_field_ids = [field_id for field_id in _DEFAULT_FIELD_IDS if field_id in field_by_id]
    default_fields = set(default_field_ids)
    for field in fields:
        field["defaultVisible"] = field["id"] in default_fields

    model = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": OVERVIEW_KIND,
        "runId": run_root.name,
        "source": {
            "path": relative_source,
            "sha256": source_hash,
            "recordCount": len(rows),
        },
        "queryContext": _query_context(query_receipt),
        "fields": fields,
        "defaultFieldIds": default_field_ids,
        "rows": rows,
    }
    try:
        model["audienceOverview"] = build_audience_overview(run_root)
    except AudienceOverviewError:
        model["audienceOverview"] = {
            "schemaVersion": 1,
            "status": "unavailable",
            "gaps": [{"code": "audience_unavailable"}],
        }
    try:
        model["scriptFormulaOverview"] = build_script_formula_overview(run_root)
    except ScriptFormulaOverviewError:
        model["scriptFormulaOverview"] = {
            "schemaVersion": 1,
            "status": "unavailable",
            "gaps": [{"code": "script_formula_unavailable"}],
        }
    try:
        model["highlightOverview"] = build_highlight_overview(run_root)
    except HighlightOverviewError:
        model["highlightOverview"] = {
            "schemaVersion": 1,
            "status": "unavailable",
            "gaps": [{"code": "highlight_frames_unavailable"}],
        }
    try:
        model["singleMaterialOverview"] = build_single_material_overview(run_root)
        for material in model["singleMaterialOverview"].get("materials", []):
            for field in material.get("leaderboard", {}).get("fields", []):
                registered = field_by_id.get(field.get("id"))
                if registered:
                    field["label"] = registered["label"]
    except SingleMaterialOverviewError:
        model["singleMaterialOverview"] = {
            "schemaVersion": 1,
            "status": "unavailable",
            "materialScope": {"count": 0, "materialIds": []},
            "materials": [],
            "gaps": [{"code": "single_material_unavailable"}],
        }
    return model


def _json_for_script(model):
    return json.dumps(model, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def render_overview_html(model):
    embedded = _json_for_script(model)
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>千川素材榜单数据工作台</title>
<style>
:root { color-scheme: light; --bg: #f4f7fb; --surface: #ffffff; --surface-muted: #f8fafc; --line: #dfe6f0; --line-strong: #c9d5e5; --text: #172033; --muted: #63708a; --accent: #2563eb; --accent-soft: #eff6ff; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--text); background: var(--bg); }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px; background: var(--bg); }
main { max-width: 1120px; margin: 0 auto; }
h1 { margin: 0 0 22px; font-size: 30px; line-height: 1.25; letter-spacing: -.02em; }
.toolbar, .summary, .pager { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.toolbar { padding: 14px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; box-shadow: 0 8px 24px rgba(34, 64, 112, .06); }
input[type=search] { min-width: 300px; flex: 1; height: 44px; padding: 10px 13px; border: 1px solid var(--line-strong); border-radius: 9px; background: var(--surface); color: var(--text); font-size: 15px; outline: none; }
input[type=search]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37, 99, 235, .14); }
button { min-height: 40px; padding: 8px 14px; border: 1px solid var(--line-strong); border-radius: 9px; background: var(--surface); color: var(--text); cursor: pointer; font-size: 14px; transition: border-color .16s ease, background .16s ease, box-shadow .16s ease; }
button:hover:not(:disabled) { border-color: var(--accent); background: var(--accent-soft); }
button:focus-visible, input:focus-visible { outline: 3px solid rgba(37, 99, 235, .24); outline-offset: 2px; }
button:disabled { cursor: default; opacity: .45; }
body.modal-open { overflow: hidden; }
.field-modal[hidden] { display: none; }
.field-modal { position: fixed; inset: 0; z-index: 20; display: grid; place-items: center; padding: 24px; }
.field-modal-backdrop { position: absolute; inset: 0; background: rgba(15, 23, 42, .42); backdrop-filter: blur(2px); }
.field-modal-panel { position: relative; display: flex; flex-direction: column; width: min(1180px, 100%); height: min(760px, calc(100vh - 48px)); overflow: hidden; background: var(--surface); border: 1px solid var(--line); border-radius: 16px; box-shadow: 0 24px 70px rgba(15, 23, 42, .24); }
.field-modal-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 22px 24px 16px; border-bottom: 1px solid var(--line); }
.field-modal-header h2 { margin: 0; font-size: 20px; line-height: 1.3; }
.field-modal-header p { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
.field-modal-close { min-width: 36px; min-height: 36px; padding: 0; border: 0; border-radius: 50%; color: var(--muted); font-size: 24px; line-height: 1; }
.field-modal-body { display: grid; grid-template-columns: 190px minmax(0, 1fr) 290px; flex: 1; min-height: 0; }
.field-modal-groups { min-width: 0; padding: 14px 10px; overflow: auto; background: #f8fafc; border-right: 1px solid var(--line); }
.field-modal-groups button { display: flex; align-items: center; justify-content: space-between; width: 100%; min-height: 38px; margin-bottom: 4px; padding: 8px 10px; border: 0; border-radius: 8px; color: #45546b; text-align: left; }
.field-modal-groups button:hover:not(:disabled) { background: #eaf2ff; color: #1d4ed8; }
.field-modal-groups button.active { background: var(--accent-soft); color: #1d4ed8; font-weight: 650; box-shadow: inset 3px 0 0 var(--accent); }
.field-modal-groups button span { color: var(--muted); font-size: 12px; font-weight: 500; }
.field-modal-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.field-modal-search { padding: 14px 20px; border-bottom: 1px solid var(--line); }
.field-modal-search input { width: 100%; min-width: 0; height: 40px; padding: 8px 12px; border: 1px solid var(--line-strong); border-radius: 8px; background: var(--surface); color: var(--text); font-size: 14px; outline: none; }
.field-modal-search input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37, 99, 235, .14); }
.field-options { min-height: 0; overflow: auto; padding: 18px 22px 24px; }
.field-option-section + .field-option-section { margin-top: 22px; }
.field-option-section h3 { margin: 0 0 10px; font-size: 14px; color: #263752; }
.field-option-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 12px; }
.field-option { display: flex; align-items: flex-start; gap: 9px; min-height: 40px; padding: 9px 10px; border: 1px solid transparent; border-radius: 8px; color: #34445e; font-size: 14px; line-height: 1.35; cursor: pointer; }
.field-option:hover { background: #f8fafc; border-color: #e6ebf3; }
.field-option:has(input:checked) { background: var(--accent-soft); border-color: #bfdbfe; color: #1e3a8a; }
.field-option input { flex: 0 0 auto; width: 16px; height: 16px; margin: 1px 0 0; accent-color: var(--accent); }
.field-modal-selected { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: #fbfcfe; border-left: 1px solid var(--line); }
.field-modal-selected-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 16px 16px 12px; border-bottom: 1px solid var(--line); }
.field-modal-selection-actions { display: flex; gap: 4px; }
.field-modal-selected-header strong { font-size: 14px; }
.field-modal-selected-header span { color: var(--muted); font-size: 12px; }
.field-modal-selection-actions button { min-height: 28px; padding: 4px 7px; border: 0; color: var(--accent); font-size: 12px; }
.field-modal-selected-list { min-height: 0; margin: 0; padding: 12px; overflow: auto; list-style: none; }
.field-modal-selected-item { display: flex; align-items: center; gap: 8px; min-height: 38px; margin-bottom: 7px; padding: 7px 8px; background: #f1f3f7; border-radius: 7px; color: #34445e; font-size: 13px; }
.field-modal-selected-item .field-drag-handle { color: #8a96a8; font-size: 16px; line-height: 1; }
.field-modal-selected-item .field-selected-label { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.field-modal-selected-item .field-required { flex: 0 0 auto; color: var(--muted); font-size: 11px; }
.field-modal-selected-item button { min-width: 24px; min-height: 24px; padding: 0; border: 0; color: var(--muted); font-size: 18px; line-height: 1; }
.field-modal-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 24px; border-top: 1px solid var(--line); }
.field-modal-footer p { margin: 0; color: var(--muted); font-size: 12px; }
.field-modal-footer-actions { display: flex; gap: 10px; }
.field-modal-footer-actions .primary { border-color: var(--accent); background: var(--accent); color: white; }
.field-modal-footer-actions .primary:hover:not(:disabled) { border-color: #1d4ed8; background: #1d4ed8; }
.table-wrap { margin-top: 16px; overflow: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; box-shadow: 0 8px 24px rgba(34, 64, 112, .06); }
table { width: 100%; border-collapse: separate; border-spacing: 0; min-width: 900px; }
th, td { padding: 13px 14px; border-bottom: 1px solid #edf0f5; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; z-index: 2; background: var(--surface-muted); white-space: nowrap; font-size: 13px; letter-spacing: .01em; }
th button { min-height: 30px; padding: 4px 0; border: 0; background: transparent; font: inherit; font-weight: 650; }
th button:hover:not(:disabled) { background: transparent; }
tbody tr:hover td { background: #fbfdff; }
.material-name-column { position: sticky; left: 0; z-index: 1; min-width: 380px; width: 380px; background: var(--surface); box-shadow: 8px 0 14px -14px rgba(23, 32, 51, .65); }
th.material-name-column { z-index: 4; background: var(--surface-muted); }
tbody tr:hover .material-name-column { background: #fbfdff; }
.material-title { max-width: 350px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 520; }
.material-id { margin-top: 5px; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
mark { padding: 0 2px; background: #fff0a8; border-radius: 3px; }
.empty { padding: 44px; text-align: center; color: var(--muted); }
.pager { justify-content: space-between; padding: 14px 2px; color: var(--muted); font-size: 14px; }
.section-nav { display: flex; gap: 8px; margin: 0 0 16px; }
.section-nav button.active { border-color: var(--accent); background: var(--accent); color: white; }
[data-section-panel][hidden] { display: none; }
.audience-section { display: grid; gap: 16px; }
.chart-grid { display: grid; gap: 16px; }
.chart-grid { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
.audience-card { width: 100%; min-width: 0; overflow: hidden; padding: 18px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; box-shadow: 0 8px 24px rgba(34, 64, 112, .06); }
.metric-switcher, .geo-switcher { display: flex; gap: 8px; flex-wrap: wrap; }
.metric-switcher button.active, .geo-switcher button.active { border-color: var(--accent); background: var(--accent-soft); color: #1d4ed8; font-weight: 650; }
.chart-card h3 { margin: 0; font-size: 16px; }
.chart-box { min-width: 0; max-width: 100%; min-height: 260px; overflow-x: auto; overflow-y: hidden; }
.chart-box svg { display: block; min-width: 0; max-width: 100%; height: 290px; }
.chart-bar { fill: #5b8def; cursor: pointer; }
.chart-bar:hover, .chart-bar:focus { fill: #2563eb; outline: none; }
.chart-axis { stroke: #dfe6f0; stroke-width: 1; }
.chart-label { fill: #63708a; font-size: 12px; }
.chart-value { fill: #172033; font-size: 11px; font-weight: 650; }
.donut-layout { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(150px, 1fr); align-items: center; gap: 12px; }
.donut-layout svg { min-width: 220px; height: 250px; }
.donut-segment { cursor: pointer; stroke: white; stroke-width: 2; outline: none; transform-box: view-box; transform-origin: 116px 124px; transition: transform .24s cubic-bezier(.22, .61, .36, 1), filter .24s ease, stroke-width .24s ease; }
.donut-segment:hover, .donut-segment:focus { filter: brightness(.9); }
.donut-segment.active { stroke: white; stroke-width: 2; filter: brightness(.92); }
.donut-center-label { fill: #63708a; font-size: 12px; text-anchor: middle; }
.donut-center-value { fill: #172033; font-size: 18px; font-weight: 700; text-anchor: middle; }
.legend { display: grid; gap: 7px; align-content: center; }
.legend-item { display: grid; grid-template-columns: 10px minmax(0, 1fr); gap: 7px; align-items: start; min-height: 32px; padding: 4px 6px; border: 0; border-radius: 6px; text-align: left; font-size: 12px; }
.legend-item:hover, .legend-item:focus { background: var(--accent-soft); outline: none; }
.legend-item.active { background: var(--accent-soft); box-shadow: inset 3px 0 0 var(--accent); }
.legend-swatch { width: 10px; height: 10px; margin-top: 3px; border-radius: 3px; }
.legend-detail { display: block; margin-top: 2px; color: var(--muted); }
.geo-controls { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.geo-slider { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px; }
.geo-slider input { width: min(300px, 50vw); accent-color: var(--accent); }
#audience-content { min-width: 0; }
#audience-geography { display: block; width: 100%; max-width: 100%; overflow: hidden; }
.geo-chart-scroll { width: 100%; max-width: 100%; overflow-x: auto; overflow-y: hidden; }
.geo-chart-scroll svg { max-width: none; margin: 0 auto; }
.script-section { display: grid; gap: 16px; }
.script-card-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.script-card-header h3 { margin: 0; }
.script-card-actions { display: flex; align-items: center; justify-content: flex-end; gap: 12px; }
.script-card-hint { color: var(--muted); font-size: 12px; }
.script-card-header button { min-height: 34px; padding: 6px 10px; }
.script-formula-svg { width: min(760px, 100%); height: 290px; margin: 0 auto; }
.script-formula-donut { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(180px, 1fr); align-items: center; gap: 14px; }
.script-formula-donut svg { min-width: 220px; height: 270px; }
.script-formula-bar { fill: #5b8def; cursor: pointer; }
.script-formula-bar:hover, .script-formula-bar:focus { fill: #2563eb; outline: none; }
.script-formula-segment { transform-box: view-box; transform-origin: 116px 130px; transition: transform .24s cubic-bezier(.22, .61, .36, 1), filter .24s ease, stroke-width .24s ease; }
.script-formula-segment.active { filter: brightness(.9); stroke-width: 3; }
.highlight-section { display: grid; gap: 16px; }
.single-material-section { display: grid; gap: 16px; }
.single-material-layout { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 16px; align-items: start; }
.single-material-selector-shell { position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow: hidden; padding: 14px; background: var(--surface); border: 1px solid var(--line-strong); border-radius: 14px; box-shadow: 0 8px 24px rgba(34, 64, 112, .06); }
.single-material-selector { max-height: calc(100vh - 60px); overflow: auto; }
.single-material-selector-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 2px 10px; color: var(--text); font-size: 13px; }
.single-material-selector-header span { color: var(--muted); font-size: 11px; }
.single-material-items { display: grid; gap: 10px; }
.single-material-selector button { display: block; width: 100%; min-height: 62px; padding: 9px 10px; text-align: left; }
.single-material-selector button.active { border-color: var(--accent); background: var(--accent-soft); color: #1d4ed8; box-shadow: inset 3px 0 0 var(--accent); }
.single-material-selector .single-material-rank { font-weight: 700; }
.single-material-selector .single-material-id { display: block; margin-top: 3px; color: var(--muted); font-size: 11px; }
.single-material-main { min-width: 0; }
.single-material-detail-card { min-width: 0; padding: 16px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; box-shadow: 0 8px 24px rgba(34, 64, 112, .06); }
.single-material-topbar { display: grid; grid-template-columns: minmax(180px, .75fr) minmax(0, 1.25fr); gap: 14px; align-items: stretch; margin-bottom: 12px; }
.single-material-header { display: flex; min-height: 96px; flex-direction: column; justify-content: center; margin: 0; padding: 14px 16px; background: var(--surface-muted); border: 1px solid var(--line); border-radius: 12px; }
.single-material-header h2 { margin: 0; font-size: 22px; line-height: 1.35; overflow-wrap: anywhere; }
.single-material-header p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
.single-material-video-card { min-width: 0; min-height: 96px; overflow: hidden; background: var(--surface-muted); border: 1px solid var(--line); border-radius: 12px; }
.single-material-video-card video { display: block; width: 100%; height: 100%; min-height: 96px; object-fit: cover; background: #0f172a; }
.single-material-video-card .single-video-unavailable { min-height: 96px; height: 100%; border: 0; border-radius: 0; background: transparent; }
.single-material-tabs { position: sticky; top: 0; z-index: 8; display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 14px; padding: 10px 0 12px; background: var(--bg); border-bottom: 1px solid var(--line); }
.single-material-tabs button.active { border-color: var(--accent); background: var(--accent); color: white; }
.single-material-panel { scroll-margin-top: 76px; }
.single-material-panel h3 { margin: 0 0 12px; font-size: 16px; }
.single-delivery-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; color: var(--muted); font-size: 12px; }
.single-delivery-header h3 { color: var(--text); }
.single-field-summary { margin-bottom: 8px; color: var(--muted); font-size: 12px; }
.single-field-chooser { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; padding: 10px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-muted); font-size: 12px; }
.single-field-chooser label { display: inline-flex; gap: 5px; align-items: center; }
.single-metric-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.single-metric-card { min-width: 0; padding: 13px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-muted); }
.single-metric-card small { display: block; color: var(--muted); overflow-wrap: anywhere; }
.single-metric-card strong { display: block; margin-top: 8px; font-size: 18px; }
.single-interaction-controls { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.single-interaction-controls button.active, .single-formula-chip.active { border-color: var(--accent); background: var(--accent-soft); color: #1d4ed8; }
.single-chart { min-height: 230px; overflow-x: auto; overflow-y: hidden; }
.single-chart svg { display: block; min-width: 0; height: 210px; }
.single-chart-line { fill: none; stroke: var(--accent); stroke-width: 2.5; }
.single-scale { display: flex; gap: 10px; align-items: center; margin-top: 10px; color: var(--muted); font-size: 12px; }
.single-scale input { flex: 1; min-width: 0; accent-color: var(--accent); }
.single-scale output { min-width: 48px; color: var(--text); font-variant-numeric: tabular-nums; text-align: right; }
.single-formula-chips { display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 12px; }
.single-formula-group { display: grid; gap: 6px; min-width: min(100%, 360px); padding: 7px 9px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-muted); }
.single-formula-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.single-formula-secondary-row { padding-left: 22px; }
.single-formula-level { color: var(--muted); font-size: 11px; font-weight: 650; }
.single-formula-connector { color: var(--muted); font-size: 13px; }
.single-formula-chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--text); }
.single-formula-chip-primary { cursor: default; }
.single-formula-chip-secondary { border-style: dashed; }
.single-script-formula-header, .single-script-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.single-script-formula-header span, .single-script-heading span { color: var(--muted); font-size: 12px; }
.single-formula-dot { width: 8px; height: 8px; border-radius: 2px; }
.single-script-text { line-height: 2; white-space: pre-wrap; }
.single-script-text mark { padding: 1px 2px; background: color-mix(in srgb, var(--single-script-highlight) 15%, transparent); border-bottom: 2px solid var(--single-script-highlight); border-radius: 3px; }
.single-script-text mark:not(.active) { opacity: .56; }
.single-audience-workspace { display: grid; gap: 16px; }
.single-audience-metric-switcher { display: flex; gap: 8px; flex-wrap: wrap; }
.single-audience-metric-switcher button.active { border-color: var(--accent); background: var(--accent-soft); color: #1d4ed8; font-weight: 650; }
.single-audience-subsection { min-width: 0; padding: 14px; background: var(--surface-muted); border: 1px solid var(--line); border-radius: 10px; }
.single-audience-subsection h4 { margin: 0 0 10px; font-size: 14px; }
.single-audience-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.single-audience-grid > .single-audience-subsection { padding: 0; background: transparent; border: 0; border-radius: 0; }
.single-audience-dimension { min-width: 0; padding: 0; }
.single-audience-dimension h4 { margin: 0 0 10px; }
.single-audience-chart-box { min-width: 0; max-width: 100%; min-height: 250px; overflow-x: auto; overflow-y: hidden; }
.single-audience-chart-box svg { display: block; max-width: 100%; height: 290px; }
.single-audience-donut-layout { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(150px, 1fr); align-items: center; gap: 12px; }
.single-audience-donut-layout svg { min-width: 220px; height: 250px; }
.single-audience-geo-controls { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.single-audience-geo-switcher { display: flex; gap: 8px; flex-wrap: wrap; }
.single-audience-geo-switcher button.active { border-color: var(--accent); background: var(--accent-soft); color: #1d4ed8; font-weight: 650; }
.single-audience-geo-slider { display: flex; gap: 10px; align-items: center; margin-top: 12px; color: var(--muted); font-size: 12px; }
.single-audience-geo-slider input { flex: 1; min-width: 0; accent-color: var(--accent); }
.single-audience-geo-slider output { min-width: 120px; text-align: right; }
.single-audience-geo-chart { width: 100%; max-width: 100%; overflow-x: auto; overflow-y: hidden; }
.single-audience-geo-chart svg { display: block; max-width: none; }
.single-audience-bucket { display: grid; grid-template-columns: minmax(90px, 1fr) 2fr auto; gap: 8px; align-items: center; margin-top: 7px; font-size: 12px; }
.single-audience-bar { height: 8px; overflow: hidden; border-radius: 99px; background: #e6edf7; }
.single-audience-bar span { display: block; height: 100%; background: #5b8def; }
.single-highlight-layout { display: block; }
.single-video-unavailable { display: grid; min-height: 260px; place-items: center; padding: 20px; color: var(--muted); text-align: center; border: 1px dashed var(--line-strong); border-radius: 12px; background: var(--surface-muted); }
.single-highlight-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.single-highlight-card { min-width: 0; max-width: 170px; justify-self: center; padding: 6px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-muted); }
.single-highlight-card button { display: block; width: 100%; padding: 0; border: 0; background: transparent; }
.single-highlight-card img { display: block; width: 100%; aspect-ratio: 9 / 16; object-fit: cover; border-radius: 7px; }
.single-highlight-card p { margin: 7px 2px 0; color: var(--muted); font-size: 11px; line-height: 1.5; }
.single-highlight-pager { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; color: var(--muted); font-size: 12px; }
.single-lightbox[hidden] { display: none; }
.single-lightbox { position: fixed; inset: 0; z-index: 45; display: grid; place-items: center; padding: 20px; background: rgba(15, 23, 42, .78); }
.single-lightbox img { max-width: min(90vw, 900px); max-height: 85vh; object-fit: contain; }
@media (max-width: 760px) { .single-material-layout { grid-template-columns: 1fr; } .single-material-selector-shell { position: static; max-height: 210px; } .single-material-selector { max-height: 180px; } .single-material-topbar { grid-template-columns: 1fr; } .single-material-video-card, .single-material-header { min-height: 96px; } .single-metric-grid, .single-audience-grid { grid-template-columns: 1fr; } .single-audience-donut-layout { grid-template-columns: 1fr; } .single-highlight-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .single-highlight-card { max-width: 150px; } }
.highlight-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 14px; }
.highlight-card { min-width: 0; overflow: hidden; padding: 12px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; box-shadow: 0 8px 24px rgba(34, 64, 112, .06); }
.highlight-image-button { display: block; width: 100%; min-height: 0; padding: 0; border: 0; background: transparent; }
.highlight-image-button img { display: block; width: 100%; aspect-ratio: 9 / 16; object-fit: cover; border-radius: 9px; background: #e9eef6; }
.highlight-conditions, .highlight-values { margin: 11px 0 0; padding: 0; list-style: none; text-align: center; }
.highlight-conditions { color: var(--muted); font-size: 12px; font-weight: 700; line-height: 1.55; }
.highlight-conditions li + li, .highlight-values li + li { margin-top: 4px; }
.highlight-values { margin-bottom: 12px; color: #34445e; font-size: 12px; line-height: 1.55; }
.highlight-material { display: block; width: 100%; margin-top: 12px; color: var(--accent); font-size: 12px; line-height: 1.55; text-align: center; overflow-wrap: anywhere; word-break: break-word; text-decoration: none; cursor: pointer; }
.highlight-material:hover, .highlight-material:focus-visible { text-decoration: underline; }
.highlight-pager { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; color: var(--muted); font-size: 14px; }
.highlight-modal[hidden] { display: none; }
.highlight-modal { position: fixed; inset: 0; z-index: 40; display: grid; place-items: center; padding: 24px; }
.highlight-modal-backdrop { position: absolute; inset: 0; background: rgba(15, 23, 42, .72); }
.highlight-modal-panel { position: relative; display: flex; flex-direction: column; width: min(980px, 100%); max-height: calc(100vh - 48px); overflow: hidden; background: var(--surface); border-radius: 16px; box-shadow: 0 24px 70px rgba(15, 23, 42, .35); }
.highlight-modal-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 18px; border-bottom: 1px solid var(--line); }
.highlight-modal-header h2 { margin: 0; font-size: 16px; }
.highlight-modal-close { min-width: 36px; min-height: 36px; padding: 0; border: 0; border-radius: 50%; font-size: 24px; line-height: 1; }
.highlight-modal-body { display: grid; place-items: center; min-height: 0; padding: 18px; overflow: auto; }
.highlight-modal-body img { display: block; max-width: 100%; max-height: calc(100vh - 150px); object-fit: contain; }
.chart-tooltip { position: fixed; z-index: 30; max-width: 280px; padding: 10px 12px; pointer-events: none; background: #172033; color: white; border-radius: 8px; box-shadow: 0 8px 24px rgba(15, 23, 42, .2); font-size: 12px; line-height: 1.65; }
.chart-tooltip[hidden] { display: none; }
@media (max-width: 900px) { .field-modal-body { grid-template-columns: 160px minmax(0, 1fr); } .field-modal-selected { grid-column: 2; max-height: 190px; border-top: 1px solid var(--line); } }
@media (max-width: 1100px) { .highlight-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (max-width: 700px) { body { padding: 18px 12px; } h1 { font-size: 24px; } input[type=search] { min-width: 0; } .field-modal { padding: 10px; } .field-modal-panel { height: calc(100vh - 20px); } .field-modal-body { display: flex; flex-direction: column; } .field-modal-groups { display: flex; gap: 6px; padding: 10px; overflow-x: auto; overflow-y: hidden; border-right: 0; border-bottom: 1px solid var(--line); } .field-modal-groups button { flex: 0 0 auto; width: auto; margin: 0; white-space: nowrap; } .field-modal-selected { max-height: 190px; border-top: 1px solid var(--line); border-left: 0; } .field-option-grid { grid-template-columns: 1fr; } .field-modal-footer { align-items: flex-end; flex-direction: column; } .field-modal-footer-actions { width: 100%; } .field-modal-footer-actions button { flex: 1; } .field-modal-footer p { align-self: flex-start; } .material-name-column { min-width: 300px; width: 300px; } .chart-grid { grid-template-columns: 1fr; } .donut-layout, .script-formula-donut { grid-template-columns: 1fr; } .donut-layout svg, .script-formula-donut svg { margin: 0 auto; } .highlight-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
</head>
<body>
<main>
<h1>千川素材榜单数据工作台</h1>
<nav class="section-nav" aria-label="报告部分">
  <button type="button" class="active" data-section="materials">素材榜单</button>
  <button type="button" data-section="audience">人群数据</button>
  <button type="button" data-section="scripts">脚本内容公式</button>
  <button type="button" data-section="highlights">高光帧数据</button>
  <button type="button" data-section="single-material">单素材数据</button>
</nav>
<section id="field-management" class="field-modal" role="dialog" aria-modal="true" aria-labelledby="field-management-title" hidden>
  <div class="field-modal-backdrop" data-field-action="cancel"></div>
  <div class="field-modal-panel">
    <header class="field-modal-header">
      <div><h2 id="field-management-title">自定义指标</h2><p id="field-management-description">选择后将按照自定义指标顺序呈现表格列</p></div>
      <button class="field-modal-close" type="button" data-field-action="cancel" aria-label="关闭自定义指标">×</button>
    </header>
    <div class="field-modal-body">
      <nav id="field-groups" class="field-modal-groups" aria-label="字段分组"></nav>
      <section class="field-modal-main" aria-label="可选指标">
        <div class="field-modal-search"><input id="field-search" type="search" placeholder="搜索指标" aria-label="搜索指标"></div>
        <div id="field-options" class="field-options"></div>
      </section>
      <aside class="field-modal-selected" aria-label="已选择指标">
        <div class="field-modal-selected-header"><div><strong>已选择</strong> <span id="selected-count"></span></div><div class="field-modal-selection-actions"><button type="button" data-field-action="select-all">全选</button><button type="button" data-field-action="clear-all">全不选</button></div></div>
        <ol id="selected-fields" class="field-modal-selected-list"></ol>
      </aside>
    </div>
    <footer class="field-modal-footer">
      <p id="field-management-footer-note">素材名称为固定列，其他列按自定义指标顺序排列</p>
      <div class="field-modal-footer-actions"><button type="button" data-field-action="cancel">取消</button><button class="primary" type="button" data-field-action="confirm">确定</button></div>
    </footer>
  </div>
</section>
<section id="materials-panel" data-section-panel="materials">
<div class="toolbar">
  <input id="search" type="search" placeholder="输入素材标题或 ID" aria-label="输入素材标题或 ID">
  <button id="field-button" type="button">自定义指标</button>
</div>
<div id="summary" class="summary"></div>
<div id="table-container" class="table-wrap"></div>
<div class="pager">
  <span id="range"></span>
  <span>
    <button id="first" type="button">首</button>
    <button id="previous" type="button">前</button>
    <span id="pages"></span>
    <button id="next" type="button">后</button>
    <button id="last" type="button">末</button>
  </span>
</div>
</section>
<section id="highlights-panel" class="highlight-section" data-section-panel="highlights" hidden>
  <div id="highlights-unavailable" class="audience-card empty" hidden>高光帧数据不可用</div>
  <div id="highlights-content">
    <div id="highlight-summary" class="summary"></div>
    <div id="highlight-grid" class="highlight-grid"></div>
    <div class="highlight-pager">
      <span id="highlight-range"></span>
      <span>
        <button id="highlight-first" type="button">首</button>
        <button id="highlight-previous" type="button">前</button>
        <span id="highlight-pages"></span>
        <button id="highlight-next" type="button">后</button>
        <button id="highlight-last" type="button">末</button>
      </span>
    </div>
</div>
</section>
<section id="single-material-panel" class="single-material-section" data-section-panel="single-material" hidden>
  <div id="single-material-unavailable" class="audience-card empty" hidden>单素材详情数据不可用</div>
  <div id="single-material-content" class="single-material-layout" hidden>
    <aside class="single-material-selector-shell" aria-label="详情素材选择"><div id="single-material-selector" class="single-material-selector"></div></aside>
    <div class="single-material-main">
      <div class="single-material-detail-card">
        <div class="single-material-topbar"><div id="single-material-video" class="single-material-video-card"></div><header class="single-material-header"><h2 id="single-material-title"></h2><p id="single-material-id"></p></header></div>
        <nav id="single-material-tabs" class="single-material-tabs" aria-label="单素材数据板块">
          <button type="button" data-single-anchor="delivery">投放数据</button><button type="button" data-single-anchor="interactions">互动时序分析</button><button type="button" data-single-anchor="script">脚本分析</button><button type="button" data-single-anchor="audience">人群分析</button><button type="button" data-single-anchor="highlights">高光帧数据</button>
        </nav>
        <section id="single-material-delivery" class="audience-card single-material-panel" data-single-panel="delivery"><div class="single-delivery-header"><h3>投放数据</h3><button id="single-field-button" type="button" aria-expanded="false">自定义指标</button></div><div id="single-delivery-content"></div></section>
        <section id="single-material-interactions" class="audience-card single-material-panel" data-single-panel="interactions"><h3>互动时序分析</h3><div id="single-interaction-content"></div></section>
        <section id="single-material-script" class="audience-card single-material-panel" data-single-panel="script"><h3>脚本分析</h3><div id="single-script-content"></div></section>
        <section id="single-material-audience" class="audience-card single-material-panel" data-single-panel="audience"><h3>人群分析</h3><div id="single-audience-content"></div></section>
        <section id="single-material-highlights" class="audience-card single-material-panel" data-single-panel="highlights"><h3>高光帧数据</h3><div id="single-highlights-content"></div></section>
      </div>
    </div>
  </div>
</section>
<section id="audience-panel" class="audience-section" data-section-panel="audience" hidden>
  <div id="audience-unavailable" class="audience-card empty" hidden>人群数据不可用</div>
  <div id="audience-content">
    <div id="audience-metric-switcher" class="metric-switcher" aria-label="人群数据指标">
      <button type="button" class="active" data-audience-metric="SHOW_COUNT">整体展现次数</button>
      <button type="button" data-audience-metric="TRANSACTION_AMOUNT">整体成交金额</button>
      <button type="button" data-audience-metric="CLICK_COUNT">整体点击次数</button>
    </div>
    <div class="audience-card chart-card"><h3>八大人群分布</h3><div id="audience-eight-groups" class="chart-box"></div></div>
    <div class="chart-grid">
      <div class="audience-card chart-card"><h3>性别分布</h3><div class="donut-layout"><div id="audience-gender" class="chart-box"></div><div id="legend-gender" class="legend" aria-label="性别图例"></div></div></div>
      <div class="audience-card chart-card"><h3>年龄分布</h3><div class="donut-layout"><div id="audience-age" class="chart-box"></div><div id="legend-age" class="legend" aria-label="年龄图例"></div></div></div>
    </div>
    <div class="audience-card chart-card">
      <div class="geo-controls"><div><h3>地域分布</h3></div><div class="geo-switcher" aria-label="地域层级"><button type="button" class="active" data-geo-level="province">省份</button><button type="button" data-geo-level="city">城市</button></div></div>
      <div class="geo-slider"><label for="geo-visible-count">数据窗口</label><input id="geo-visible-count" type="range" min="1" max="1" value="1"><span id="geo-visible-label">显示前 1 / 1 个</span></div>
      <div id="audience-geography" class="chart-box"></div>
    </div>
  </div>
</section>
<section id="scripts-panel" class="script-section" data-section-panel="scripts" hidden>
  <div class="audience-card chart-card" data-script-formula-card>
    <div class="script-card-header"><h3 id="script-formula-title">一级内容公式</h3><div class="script-card-actions"><span id="script-formula-hint" class="script-card-hint">点击一级内容公式柱状图可以显示二级内容公式分布</span></div></div>
    <div id="script-formula-chart" class="chart-box"></div>
  </div>
  <div id="script-formula-secondary-card" class="audience-card chart-card" hidden>
    <div class="script-card-header"><h3 id="script-formula-secondary-title">二级内容公式分布</h3><div class="script-card-actions"><button id="script-formula-secondary-back" type="button">收起二级内容公式</button></div></div>
    <div class="script-formula-donut"><div id="script-formula-secondary-chart" class="chart-box"></div><div id="script-formula-secondary-legend" class="legend" aria-label="二级内容公式图例"></div></div>
  </div>
</section>
<div id="chart-tooltip" class="chart-tooltip" role="status" aria-live="polite" hidden></div>
<section id="highlight-modal" class="highlight-modal" role="dialog" aria-modal="true" aria-labelledby="highlight-modal-title" hidden>
  <div class="highlight-modal-backdrop" data-highlight-action="close"></div>
  <div class="highlight-modal-panel">
    <header class="highlight-modal-header"><h2 id="highlight-modal-title">高光帧预览</h2><button id="highlight-modal-close" class="highlight-modal-close" type="button" data-highlight-action="close" aria-label="关闭高光帧预览">×</button></header>
    <div class="highlight-modal-body"><img id="highlight-modal-image" alt=""></div>
  </div>
</section>
<section id="single-lightbox" class="single-lightbox" role="dialog" aria-modal="true" aria-labelledby="single-lightbox-title" hidden><div><h2 id="single-lightbox-title" class="sr-only">单素材高光帧预览</h2><button id="single-lightbox-close" type="button" aria-label="关闭单素材高光帧预览">关闭</button><img id="single-lightbox-image" alt=""></div></section>
</main>
<script id="overview-model" type="application/json">""" + embedded + r"""</script>
<script>
(() => {
  const model = JSON.parse(document.getElementById("overview-model").textContent);
  const pageSize = 10;
  const storageKey = `qianchuan-overview:${model.runId}:v${model.schemaVersion}`;
  const initialSort = model.queryContext.ranking || {};
  const presentationFields = model.fields.filter(field => field.id !== "material_id");
  const fieldGroupOrder = ["素材基础信息", "整体指标", "净成交指标", "周期结算", "追投调控", "视频互动"];
  const fieldGroupRank = Object.fromEntries(fieldGroupOrder.map((group, index) => [group, index]));
  const presentationFieldIndex = Object.fromEntries(presentationFields.map((field, index) => [field.id, index]));
  const orderedPresentationFields = [...presentationFields].sort((left, right) => {
    const groupResult = (fieldGroupRank[left.group] ?? fieldGroupOrder.length) - (fieldGroupRank[right.group] ?? fieldGroupOrder.length);
    return groupResult || presentationFieldIndex[left.id] - presentationFieldIndex[right.id];
  });
  const fieldMap = Object.fromEntries(orderedPresentationFields.map(field => [field.id, field]));
  const orderedFieldIds = orderedPresentationFields.map(field => field.id);
  const requiredFieldIds = ["roi2_material_video_name"].filter(id => fieldMap[id]);
  const fieldGroupNames = fieldGroupOrder.filter(group => presentationFields.some(field => field.group === group));
  fieldGroupNames.push(...[...new Set(presentationFields.map(field => field.group))].filter(group => !fieldGroupOrder.includes(group)));
  function orderedVisibleFieldIds(fieldIds) {
    const selected = new Set(Array.isArray(fieldIds) ? fieldIds : []);
    requiredFieldIds.forEach(id => selected.add(id));
    return orderedFieldIds.filter(id => selected.has(id));
  }
  const defaultFieldIds = Array.isArray(model.defaultFieldIds) ? model.defaultFieldIds.filter(id => fieldMap[id]) : presentationFields.filter(field => field.defaultVisible).map(field => field.id);
  const state = { query: "", visibleFieldIds: orderedVisibleFieldIds(defaultFieldIds), draftFieldIds: null, fieldSearch: "", activeFieldGroup: fieldGroupNames[0] || "", sort: { field: initialSort.field || null, direction: initialSort.direction || "none" }, page: 1 };
  const search = document.getElementById("search");
  const fieldModal = document.getElementById("field-management");
  const fieldGroups = document.getElementById("field-groups");
  const fieldSearch = document.getElementById("field-search");
  const fieldOptions = document.getElementById("field-options");
  const selectedFields = document.getElementById("selected-fields");
  const selectedCount = document.getElementById("selected-count");
  const tableContainer = document.getElementById("table-container");
  const audience = model.audienceOverview || { status: "unavailable", gaps: [{ code: "audience_unavailable" }] };
  const audienceState = { metricId: "SHOW_COUNT", geoLevel: "province", geoVisibleCount: 10, donutSelection: { gender: null, age: null }, tooltip: null };
  const chartTooltip = document.getElementById("chart-tooltip");
  const metricMap = Object.fromEntries((audience.metrics || []).map(metric => [metric.id, metric]));
  const scriptFormula = model.scriptFormulaOverview || { status: "unavailable", gaps: [{ code: "script_formula_unavailable" }] };
  const scriptFormulaState = { selectedPrimary: null, selectedSecondary: null, tooltip: null };
  const highlightOverview = model.highlightOverview || { status: "unavailable", gaps: [{ code: "highlight_frames_unavailable" }] };
  const highlightPageSize = 12;
  const highlightState = { page: 1, modalCard: null, modalTrigger: null };
  const highlightGrid = document.getElementById("highlight-grid");
  const highlightsContent = document.getElementById("highlights-content");
  const highlightsUnavailable = document.getElementById("highlights-unavailable");
  const highlightModal = document.getElementById("highlight-modal");
  const highlightModalImage = document.getElementById("highlight-modal-image");
  const highlightModalTitle = document.getElementById("highlight-modal-title");
  const singleMaterialOverview = model.singleMaterialOverview || { status: "unavailable", materialScope: { count: 0, materialIds: [] }, materials: [] };
  const singleMaterials = Array.isArray(singleMaterialOverview.materials) ? singleMaterialOverview.materials : [];
  const singleMaterialById = Object.fromEntries(singleMaterials.map(item => [item.materialId, item]));
  const singleState = { selectedMaterialId: singleMaterials[0]?.materialId || null, anchor: "delivery", interactionMetric: null, chartScale: 100, framePage: 1, selectedFormula: null, audienceMetricId: "SHOW_COUNT", audienceGeoLevel: "province", audienceGeoVisible: 10, audienceDonutSelection: { gender: null, age: null }, deliveryFields: {} };
  const singleHighlightPageSize = 8;
  const fieldDialog = { scope: "leaderboard", materialId: null };
  let singleLightboxTrigger = null;
  const singleMaterialUnavailable = document.getElementById("single-material-unavailable");
  const singleMaterialContent = document.getElementById("single-material-content");
  const singleMaterialSelector = document.getElementById("single-material-selector");
  const singleMaterialVideo = document.getElementById("single-material-video");
  const singleLightbox = document.getElementById("single-lightbox");
  const singleLightboxImage = document.getElementById("single-lightbox-image");

  function singleCurrentMaterial() {
    if (!singleMaterialById[singleState.selectedMaterialId]) singleState.selectedMaterialId = singleMaterials[0]?.materialId || null;
    return singleMaterialById[singleState.selectedMaterialId] || null;
  }

  function singleValue(value) {
    if (value == null || value === "") return "—";
    return typeof value === "number" ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value) : escaped(value);
  }

  function singleEmpty(label) { return `<div class="empty">暂无${escaped(label)}有效数据</div>`; }

  const singleDefaultFieldIds = [
    "stat_cost_for_overall_roi2",
    "stat_cost_for_roi2",
    "total_pay_order_count_for_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "live_show_count_for_roi2_v2",
    "live_watch_count_for_roi2_v2",
    "live_cvr_rate_for_roi2_v2",
  ];

  function defaultSingleFieldIds(catalog) {
    const available = new Set(catalog.map(field => field.id));
    const preferred = singleDefaultFieldIds.filter(id => available.has(id));
    return preferred.length ? preferred : catalog.slice(0, Math.min(8, catalog.length)).map(field => field.id);
  }

  function singleRenderDelivery(material) {
    const target = document.getElementById("single-delivery-content");
    const fields = material.leaderboard?.fields || [];
    const selectedIds = Object.prototype.hasOwnProperty.call(singleState.deliveryFields, material.materialId)
      ? singleState.deliveryFields[material.materialId]
      : defaultSingleFieldIds(fields);
    const visibleFields = fields.filter(field => selectedIds.includes(field.id));
    target.innerHTML = fields.length ? `<div id="single-field-summary" class="single-field-summary">已展示指标 ${visibleFields.length} / ${fields.length}</div><div class="single-metric-grid">${visibleFields.map(field => `<div class="single-metric-card"><small>${escaped(field.label || field.id)}</small><strong>${singleValue(field.value)}</strong></div>`).join("") || singleEmpty("投放数据")}</div>` : singleEmpty("投放数据");
  }

  function singleRenderVideo(material) {
    const video = material.highlights?.video || {};
    if (video.status === "available" && video.path) {
      singleMaterialVideo.innerHTML = `<video controls preload="metadata" src="${escaped(video.path)}" aria-label="${escaped(material.title)}视频预览"></video>`;
      return;
    }
    singleMaterialVideo.innerHTML = `<div class="single-video-unavailable">${escaped(video.message || "本次详情数据未保存可播放视频")}</div>`;
  }

  function singleRenderInteractions(material) {
    const target = document.getElementById("single-interaction-content");
    const metrics = material.interactions?.metrics || [];
    if (material.interactions?.status !== "available" || !metrics.length) { target.innerHTML = singleEmpty("互动时序"); return; }
    if (!metrics.some(item => item.id === singleState.interactionMetric)) singleState.interactionMetric = metrics[0].id;
    const metric = metrics.find(item => item.id === singleState.interactionMetric) || metrics[0];
    const points = metric.points || [];
    const minSecond = points.length ? Math.min(...points.map(item => Number(item.second))) : 0;
    const maxSecond = points.length ? Math.max(...points.map(item => Number(item.second))) : 1;
    const maxValue = Math.max(...points.map(item => Number(item.value)), 1);
    const width = 760, height = 210, left = 40, top = 16, right = 16, bottom = 32;
    const chartPoints = points.map(item => {
      const x = left + Math.min(1, Math.max(0, (Number(item.second) - minSecond) / Math.max(1, maxSecond - minSecond))) * (width - left - right);
      const y = height - bottom - (Number(item.value) / maxValue) * (height - top - bottom);
      return { ...item, x, y };
    });
    const line = chartPoints.map((item, index) => `${index ? "L" : "M"} ${item.x.toFixed(2)} ${item.y.toFixed(2)}`).join(" ");
    const dots = chartPoints.map(item => `<circle cx="${item.x.toFixed(2)}" cy="${item.y.toFixed(2)}" r="4" fill="var(--accent)" tabindex="0" data-single-point="${escaped(item.second)}" aria-label="${escaped(item.second)}秒，${escaped(item.value)}"><title>${escaped(item.second)}秒：${escaped(item.value)}</title></circle>`).join("");
    const scaledWidth = Math.round(width * singleState.chartScale / 100);
    target.innerHTML = `<div class="single-interaction-controls">${metrics.map(item => `<button type="button" class="${item.id === metric.id ? "active" : ""}" data-single-interaction="${escaped(item.id)}">${escaped(item.label)}</button>`).join("")}</div><div class="single-chart"><svg style="width:${scaledWidth}px;height:${height}px" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escaped(metric.label)}秒级折线图"><line class="chart-axis" x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}"></line><path class="single-chart-line" d="${line}"></path>${dots}</svg></div><label class="single-scale"><span>图表比例</span><input type="range" min="70" max="150" step="1" value="${singleState.chartScale}" aria-label="互动图表比例" data-single-chart-scale><output>${singleState.chartScale}%</output></label>`;
    target.querySelectorAll("[data-single-interaction]").forEach(button => button.addEventListener("click", () => { singleState.interactionMetric = button.dataset.singleInteraction; singleRenderInteractions(material); }));
    const scaleInput = target.querySelector("[data-single-chart-scale]");
    const scaleOutput = target.querySelector(".single-scale output");
    const chartScroll = target.querySelector(".single-chart");
    const chartSvg = target.querySelector(".single-chart svg");
    scaleInput.addEventListener("input", event => { const scrollLeft = chartScroll.scrollLeft; singleState.chartScale = Number(event.currentTarget.value); chartSvg.style.width = `${Math.round(width * singleState.chartScale / 100)}px`; chartScroll.scrollLeft = scrollLeft; scaleOutput.textContent = `${singleState.chartScale}%`; });
  }

  function singleRenderScript(material) {
    const target = document.getElementById("single-script-content");
    const script = material.script || {};
    if (script.status !== "available") { target.innerHTML = singleEmpty("脚本"); return; }
    const formulas = Array.isArray(script.formula) ? script.formula : [];
    const formulaGroups = formulas.map(item => ({ primary: item.primary, secondary: Array.isArray(item.secondary) ? item.secondary.filter(Boolean) : [] })).filter(item => item.primary);
    const chips = formulaGroups.flatMap(item => [{ label: item.primary, kind: "一级公式", related: [item.primary, ...item.secondary] }, ...item.secondary.map(label => ({ label, kind: "二级公式", related: [label] }))]);
    const selected = singleState.selectedFormula;
    const segments = Array.isArray(script.segments) ? script.segments : [];
    const palette = ["#16a34a", "#ef4444", "#f59e0b", "#2563eb", "#14b8a6", "#ec4899", "#8b5cf6"];
    const labelColors = {};
    chips.forEach((chip, index) => { if (!labelColors[chip.label]) labelColors[chip.label] = palette[index % palette.length]; });
    const selectedChip = chips.find(chip => chip.label === selected);
    const activeLabels = new Set(selectedChip?.related || []);
    function linkedScriptText(text) {
      const matches = [];
      segments.forEach(segment => {
        if (!segment.text) return;
        let from = 0;
        while (from < text.length) {
          const start = text.indexOf(segment.text, from);
          if (start < 0) break;
          matches.push({ start, end: start + segment.text.length, label: segment.label });
          from = start + segment.text.length;
        }
      });
      matches.sort((left, right) => left.start - right.start || right.end - left.end);
      const nonOverlapping = [];
      matches.forEach(match => { if (!nonOverlapping.some(item => match.start < item.end && match.end > item.start)) nonOverlapping.push(match); });
      let cursor = 0;
      return nonOverlapping.map(match => { const before = escaped(text.slice(cursor, match.start)); const body = escaped(text.slice(match.start, match.end)); const active = !selected || activeLabels.has(match.label); const color = labelColors[match.label] || "#64748b"; cursor = match.end; return `${before}<mark class="single-script-highlight${active ? " active" : ""}" style="--single-script-highlight:${color}" title="${escaped(match.label)}">${body}</mark>`; }).join("") + escaped(text.slice(cursor));
    }
    const segmentMarkup = segments.length && !script.text ? segments.map(segment => { const active = !selected || activeLabels.has(segment.label); const color = labelColors[segment.label] || "#64748b"; return `<p><strong>${escaped(segment.label)}：</strong><mark class="single-script-highlight${active ? " active" : ""}" style="--single-script-highlight:${color}">${escaped(segment.text)}</mark></p>`; }).join("") : "";
    const formulaMarkup = formulaGroups.map(group => `<div class="single-formula-group"><div class="single-formula-row single-formula-primary-row"><span class="single-formula-level">一级</span><span class="single-formula-chip single-formula-chip-primary"><span class="single-formula-dot" style="background:${labelColors[group.primary]}"></span>${escaped(group.primary)}</span></div><div class="single-formula-row single-formula-secondary-row"><span class="single-formula-level">二级</span>${group.secondary.length ? group.secondary.map(label => `<span class="single-formula-connector" aria-hidden="true">+</span><button type="button" class="single-formula-chip single-formula-chip-secondary ${selected === label ? "active" : ""}" data-single-formula="${escaped(label)}" data-single-formula-level="secondary"><span class="single-formula-dot" style="background:${labelColors[label]}"></span>${escaped(label)}</button>`).join("") : `<span class="single-formula-level">暂无二级内容公式</span>`}</div></div>`).join("");
    target.innerHTML = `<div class="single-script-formula-header"><strong>本视频内容公式</strong><span>${selected ? `已联动：${escaped(selected)}` : "一级与二级内容公式已同时展示，点击二级内容公式可联动脚本证据"}</span></div><div class="single-formula-chips">${formulaMarkup || singleEmpty("内容公式")}</div>${script.text ? `<div class="single-script-heading"><strong>本视频脚本</strong><span>彩色片段对应已解析的脚本标签</span></div><div class="single-script-text">${linkedScriptText(script.text)}</div>` : ""}${segmentMarkup || (!script.text ? singleEmpty("脚本内容") : "")}`;
    target.querySelectorAll("[data-single-formula]").forEach(button => button.addEventListener("click", () => { singleState.selectedFormula = singleState.selectedFormula === button.dataset.singleFormula ? null : button.dataset.singleFormula; singleRenderScript(material); }));
  }

  function singleRenderAudience(material) {
    const target = document.getElementById("single-audience-content");
    const audienceModel = material.audience || {};
    const dimensions = audienceModel.dimensions || {};
    const metrics = Array.isArray(audienceModel.metrics) && audienceModel.metrics.length ? audienceModel.metrics : (audience.metrics || []);
    if (audienceModel.status !== "available" || !Object.keys(dimensions).length || !metrics.length) { target.innerHTML = singleEmpty("人群"); return; }
    const metricMap = Object.fromEntries(metrics.map(item => [item.id, item]));
    if (!metricMap[singleState.audienceMetricId]) singleState.audienceMetricId = metrics[0].id;
    const metric = metricMap[singleState.audienceMetricId] || metrics[0];
    const buckets = id => dimensions[id]?.bucketsByMetric?.[metric.id] || [];
    const geography = buckets(singleState.audienceGeoLevel);
    const count = Math.min(Math.max(1, singleState.audienceGeoVisible), Math.max(1, geography.length));
    singleState.audienceGeoVisible = count;
    target.innerHTML = `<div class="single-audience-workspace"><div class="single-audience-metric-switcher" aria-label="单素材人群指标">${metrics.map(item => `<button type="button" class="${item.id === metric.id ? "active" : ""}" data-single-audience-metric="${escaped(item.id)}">${escaped(item.label)}</button>`).join("")}</div><div class="single-audience-subsection"><h4>八大人群分布</h4><div id="single-audience-eight-groups" class="single-audience-chart-box"></div></div><div class="single-audience-grid"><div class="single-audience-subsection"><h4>性别分布</h4><div class="single-audience-donut-layout"><div id="single-audience-gender" class="single-audience-chart-box"></div><div id="single-audience-legend-gender" class="legend" aria-label="单素材性别图例"></div></div></div><div class="single-audience-subsection"><h4>年龄分布</h4><div class="single-audience-donut-layout"><div id="single-audience-age" class="single-audience-chart-box"></div><div id="single-audience-legend-age" class="legend" aria-label="单素材年龄图例"></div></div></div></div><div class="single-audience-subsection"><div class="single-audience-geo-controls"><h4>地域分布</h4><div class="single-audience-geo-switcher" aria-label="单素材地域层级">${["province", "city"].map(id => `<button type="button" class="${id === singleState.audienceGeoLevel ? "active" : ""}" data-single-audience-geo="${id}">${id === "province" ? "省份" : "城市"}</button>`).join("")}</div></div><div id="single-audience-geography" class="single-audience-geo-chart"></div><label class="single-audience-geo-slider"><span>数据窗口</span><input type="range" min="1" max="${Math.max(1, geography.length)}" value="${geography.length ? count : 1}" aria-label="地域数据窗口" data-single-audience-geo-range><output>${geography.length ? `显示前 ${count} / ${geography.length} 个` : "暂无地域数据"}</output></label></div></div>`;
    const singleAudienceBars = (container, values, dimension, visibleCount) => {
      if (!values.length) { container.innerHTML = chartEmpty(dimensions[dimension]?.label || "图表"); return; }
      const shown = values.slice(0, visibleCount || values.length);
      const isGeography = dimension === "province" || dimension === "city";
      const band = isGeography ? (shown.length > 24 ? 56 : shown.length > 14 ? 64 : 74) : 74;
      const width = dimension === "eight_groups" ? Math.max(760, (container.clientWidth || 900) - 8) : Math.max(560, shown.length * band);
      const height = 290, left = 42, bottom = 72, plotHeight = height - bottom - 20;
      const max = Math.max(...shown.map(item => Number(item.value)), 1);
      const itemBand = (width - left - 18) / shown.length;
      const barRatio = isGeography ? (shown.length > 24 ? .68 : shown.length > 14 ? .60 : .52) : .52;
      const bars = shown.map((bucket, index) => { const barHeight = Math.max(2, Number(bucket.value) / max * plotHeight); const barWidth = itemBand * barRatio; const x = left + index * itemBand + (itemBand - barWidth) / 2; const y = height - bottom - barHeight; const center = x + barWidth / 2; const label = bucket.label.length > 8 ? `${bucket.label.slice(0, 8)}…` : bucket.label; return `<rect class="chart-bar" data-single-chart-index="${index}" data-chart-rank="${index + 1}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="5" tabindex="0" role="img" aria-label="${escaped(bucket.label)} ${escaped(metric.label)} ${escaped(formatAudienceValue(bucket.value, metric))}"></rect><text class="chart-value" x="${center.toFixed(2)}" y="${Math.max(14, y - 6).toFixed(2)}" text-anchor="middle">${escaped(formatAudienceValue(bucket.value, metric))}</text><text class="chart-label" x="${center.toFixed(2)}" y="${height - 42}" text-anchor="middle">${escaped(label)}</text>`; }).join("");
      const svgStyle = dimension === "eight_groups" ? `style="width:100%;height:${height}px"` : `style="width:${width}px;height:${height}px"`;
      container.innerHTML = `<svg ${svgStyle} preserveAspectRatio="none" viewBox="0 0 ${width} ${height}" role="group" aria-label="${escaped(dimensions[dimension]?.label || "柱状图")}"><line class="chart-axis" x1="${left}" y1="${height - bottom}" x2="${width - 12}" y2="${height - bottom}"></line>${bars}</svg>`;
      container.querySelectorAll("[data-single-chart-index]").forEach(element => { const bucket = shown[Number(element.dataset.singleChartIndex)]; const rank = Number(element.dataset.chartRank); element.addEventListener("pointerenter", event => showChartTooltip(event, bucket, metric, dimension, rank)); element.addEventListener("pointerleave", hideChartTooltip); element.addEventListener("focus", event => showChartTooltip(event, bucket, metric, dimension, rank)); element.addEventListener("blur", hideChartTooltip); });
    };
    const singleAudienceDonut = (container, legend, values, dimension) => {
      if (!values.length) { container.innerHTML = chartEmpty(dimensions[dimension]?.label || "图表"); legend.innerHTML = ""; return; }
      const colors = ["#2563eb", "#60a5fa", "#14b8a6", "#f59e0b", "#ef6a6a", "#8b5cf6", "#64748b", "#22c55e"]; let cursor = -Math.PI / 2;
      const segments = values.map((bucket, index) => { const end = cursor + Math.max(0, bucket.share) * Math.PI * 2; const angle = (cursor + end) / 2; const path = donutPath(cursor, end, 92, 56, 116, 124); cursor = end; return `<path class="donut-segment" data-single-donut-dimension="${dimension}" data-single-donut-index="${index}" data-donut-angle="${angle.toFixed(6)}" d="${path}" fill="${colors[index % colors.length]}" tabindex="0" role="img" aria-label="${escaped(bucket.label)} ${escaped(metric.label)} ${escaped(formatAudienceValue(bucket.value, metric))}"></path>`; }).join("");
      container.innerHTML = `<svg viewBox="0 0 232 250" role="group" aria-label="${escaped(dimensions[dimension]?.label || "环形图")}">${segments}<text class="donut-center-label" x="116" y="118">${escaped(metric.label)}</text><text class="donut-center-value" x="116" y="143">${escaped(formatAudienceValue(values[0].total, metric))}</text></svg>`;
      legend.innerHTML = values.map((bucket, index) => `<button type="button" class="legend-item" data-single-donut-dimension="${dimension}" data-single-donut-index="${index}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)} ${escaped(metric.label)} ${escaped(formatAudienceValue(bucket.value, metric))}"><span class="legend-swatch" style="background:${colors[index % colors.length]}"></span><span>${escaped(bucket.label)}<span class="legend-detail">${escaped(formatAudienceValue(bucket.value, metric))} · ${new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 }).format(bucket.share)}</span></span></button>`).join("");
      const updateSelection = index => { singleState.audienceDonutSelection[dimension] = index == null ? null : Number(index); target.querySelectorAll(`[data-single-donut-dimension="${dimension}"]`).forEach(element => { const selected = Number(element.dataset.singleDonutIndex) === singleState.audienceDonutSelection[dimension]; element.classList.toggle("active", selected); element.setAttribute("aria-pressed", String(selected)); if (element.classList.contains("donut-segment")) { const angle = Number(element.dataset.donutAngle || 0); const distance = selected ? 8 : 0; element.style.transform = `translate(${(Math.cos(angle) * distance).toFixed(2)}px, ${(Math.sin(angle) * distance).toFixed(2)}px)`; } }); };
      [...container.querySelectorAll("[data-single-donut-index]"), ...legend.querySelectorAll("[data-single-donut-index]")].forEach(element => { const bucket = values[Number(element.dataset.singleDonutIndex)]; element.addEventListener("pointerenter", event => showChartTooltip(event, bucket, metric, dimension, Number(element.dataset.singleDonutIndex) + 1)); element.addEventListener("pointerleave", hideChartTooltip); element.addEventListener("focus", event => showChartTooltip(event, bucket, metric, dimension, Number(element.dataset.singleDonutIndex) + 1)); element.addEventListener("blur", hideChartTooltip); element.addEventListener("click", () => updateSelection(singleState.audienceDonutSelection[dimension] === Number(element.dataset.singleDonutIndex) ? null : Number(element.dataset.singleDonutIndex))); });
      updateSelection(singleState.audienceDonutSelection[dimension] != null && singleState.audienceDonutSelection[dimension] < values.length ? singleState.audienceDonutSelection[dimension] : null);
    };
    singleAudienceBars(document.getElementById("single-audience-eight-groups"), buckets("eight_groups"), "eight_groups");
    singleAudienceDonut(document.getElementById("single-audience-gender"), document.getElementById("single-audience-legend-gender"), buckets("gender"), "gender");
    singleAudienceDonut(document.getElementById("single-audience-age"), document.getElementById("single-audience-legend-age"), buckets("age"), "age");
    const renderSingleAudienceGeography = () => {
      const currentGeography = buckets(singleState.audienceGeoLevel);
      const currentCount = Math.min(Math.max(1, singleState.audienceGeoVisible), Math.max(1, currentGeography.length));
      singleState.audienceGeoVisible = currentCount;
      const range = target.querySelector("[data-single-audience-geo-range]");
      const output = target.querySelector(".single-audience-geo-slider output");
      range.max = String(Math.max(1, currentGeography.length));
      range.value = String(currentGeography.length ? currentCount : 1);
      output.textContent = currentGeography.length ? `显示前 ${currentCount} / ${currentGeography.length} 个` : "暂无地域数据";
      singleAudienceBars(document.getElementById("single-audience-geography"), currentGeography, singleState.audienceGeoLevel, currentCount);
    };
    renderSingleAudienceGeography();
    target.querySelectorAll("[data-single-audience-metric]").forEach(button => button.addEventListener("click", () => { singleState.audienceMetricId = button.dataset.singleAudienceMetric; singleState.audienceGeoVisible = 10; hideChartTooltip(); singleRenderAudience(material); }));
    target.querySelectorAll("[data-single-audience-geo]").forEach(button => button.addEventListener("click", () => { singleState.audienceGeoLevel = button.dataset.singleAudienceGeo; singleState.audienceGeoVisible = 10; hideChartTooltip(); singleRenderAudience(material); }));
    target.querySelector("[data-single-audience-geo-range]").addEventListener("input", event => { singleState.audienceGeoVisible = Number(event.currentTarget.value); renderSingleAudienceGeography(); });
  }

  function singleOpenLightbox(card, trigger) {
    singleLightboxTrigger = trigger;
    singleLightboxImage.src = card.artifact.path;
    singleLightboxImage.alt = `${singleValue(card.second)}秒，高光帧`;
    singleLightbox.hidden = false;
    document.body.classList.add("modal-open");
    document.getElementById("single-lightbox-close").focus();
  }

  function singleCloseLightbox() {
    singleLightbox.hidden = true;
    document.body.classList.remove("modal-open");
    if (singleLightboxTrigger) singleLightboxTrigger.focus();
    singleLightboxTrigger = null;
  }

  function singleRenderHighlights(material) {
    const target = document.getElementById("single-highlights-content");
    const highlights = material.highlights || {};
    const cards = Array.isArray(highlights.cards) ? highlights.cards : [];
    if (highlights.status !== "available" || !cards.length) { target.innerHTML = singleEmpty("高光帧"); return; }
    const pageCount = Math.max(1, Math.ceil(cards.length / singleHighlightPageSize));
    singleState.framePage = Math.min(singleState.framePage, pageCount);
    const start = (singleState.framePage - 1) * singleHighlightPageSize;
    const shown = cards.slice(start, start + singleHighlightPageSize);
    target.innerHTML = `<div class="single-highlight-layout"><div class="single-highlight-grid">${shown.map((card, index) => `<article class="single-highlight-card"><button type="button" data-single-frame-index="${start + index}" aria-label="放大查看${escaped(card.second)}秒高光帧"><img src="${escaped(card.artifact.path)}" alt="${escaped(card.second)}秒高光帧"></button><p>${escaped(card.description || card.role || "高光帧")} · ${escaped(card.second)}秒</p></article>`).join("")}</div><div class="single-highlight-pager"><span>第 ${singleState.framePage} / ${pageCount} 页 · 共 ${cards.length} 帧</span><span><button type="button" data-single-frame-page="previous" ${singleState.framePage <= 1 ? "disabled" : ""}>前</button><button type="button" data-single-frame-page="next" ${singleState.framePage >= pageCount ? "disabled" : ""}>后</button></span></div></div>`;
    target.querySelectorAll("[data-single-frame-index]").forEach(button => button.addEventListener("click", () => singleOpenLightbox(cards[Number(button.dataset.singleFrameIndex)], button)));
    target.querySelectorAll("[data-single-frame-page]").forEach(button => button.addEventListener("click", () => { singleState.framePage += button.dataset.singleFramePage === "next" ? 1 : -1; singleRenderHighlights(material); }));
  }

  function singleRender() {
    const available = singleMaterialOverview.status === "available" && singleMaterials.length;
    singleMaterialUnavailable.hidden = Boolean(available);
    singleMaterialContent.hidden = !available;
    if (!available) return;
    const material = singleCurrentMaterial();
    singleMaterialSelector.innerHTML = `<div class="single-material-selector-header"><strong>详情素材选择</strong><span>${singleMaterials.length} 个素材</span></div><div class="single-material-items">${singleMaterials.map(item => `<button type="button" class="${item.materialId === material.materialId ? "active" : ""}" data-single-material-id="${escaped(item.materialId)}" aria-label="第${escaped(item.rank)}名，${escaped(item.title)}"><span class="single-material-rank">#${escaped(item.rank)} ${escaped(item.title)}</span><span class="single-material-id">素材ID：${escaped(item.materialId)}</span></button>`).join("")}</div>`;
    document.getElementById("single-material-title").textContent = material.title;
    document.getElementById("single-material-id").textContent = `素材ID：${material.materialId} · 榜单第 ${material.rank} 名`;
    singleRenderVideo(material); singleRenderDelivery(material); singleRenderInteractions(material); singleRenderScript(material); singleRenderAudience(material); singleRenderHighlights(material);
    document.querySelectorAll("[data-single-anchor]").forEach(button => button.classList.toggle("active", button.dataset.singleAnchor === singleState.anchor));
    singleMaterialSelector.querySelectorAll("[data-single-material-id]").forEach(button => button.addEventListener("click", () => { singleState.selectedMaterialId = button.dataset.singleMaterialId; singleState.framePage = 1; singleState.selectedFormula = null; singleState.interactionMetric = null; singleRender(); }));
  }

  function normalize(value) {
    return String(value == null ? "" : value).normalize("NFKC").toLocaleLowerCase().trim();
  }

  function loadVisibleFields() {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "null");
      const migrated = Array.isArray(stored) ? stored.filter(id => fieldMap[id]) : null;
      if (Array.isArray(migrated)) state.visibleFieldIds = orderedVisibleFieldIds(migrated);
    } catch (_) { /* localStorage may be disabled in offline pages. */ }
  }

  function saveVisibleFields() {
    try { localStorage.setItem(storageKey, JSON.stringify(state.visibleFieldIds)); } catch (_) { /* offline page remains usable. */ }
  }

  function missing(value) { return value == null || value === ""; }

  function compareValues(left, right, field) {
    const leftMissing = missing(left);
    const rightMissing = missing(right);
    if (leftMissing || rightMissing) return leftMissing === rightMissing ? 0 : (leftMissing ? 1 : -1);
    if (field.valueType === "number" || field.valueType === "duration") return Number(left) - Number(right);
    if (field.valueType === "date") return String(left).localeCompare(String(right));
    return normalize(left).localeCompare(normalize(right));
  }

  function matchingRows() {
    const needle = state.query;
    const normalizedNeedle = normalize(needle);
    const rows = model.rows.filter(row => {
      if (!normalizedNeedle) return true;
      return normalize(row.materialId).includes(normalize(needle)) || normalize(row.values.roi2_material_video_name).includes(normalize(needle));
    });
    if (state.sort.field && state.sort.direction !== "none" && fieldMap[state.sort.field]) {
      const field = fieldMap[state.sort.field];
      rows.sort((left, right) => {
        const result = compareValues(left.values[field.id], right.values[field.id], field);
        if (result !== 0) return state.sort.direction === "asc" ? result : -result;
        return normalize(left.materialId).localeCompare(normalize(right.materialId));
      });
    }
    return rows;
  }

  function escaped(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;"}[char]));
  }

  function highlighted(value) {
    const text = escaped(value == null ? "" : value);
    const query = escaped(state.query.trim());
    if (!query) return text;
    const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
    return text.replace(pattern, match => `<mark>${match}</mark>`);
  }

  function display(value) {
    if (missing(value)) return "—";
    if (typeof value === "number") return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value);
    return escaped(value);
  }

  function highlightSecond(value) {
    const number = Number(value);
    return Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  }

  function highlightMetricLabel(metric) {
    return ({
      CLICK_COUNT: "整体点击次数",
      LOSE_COUNT: "整体流失数",
      LIKE_COUNT: "整体点赞次数",
      COMMENT_COUNT: "整体评论次数",
      SHARE_COUNT: "整体转发次数",
      NEW_FOLLOWER_COUNT: "整体新增粉丝数",
    })[metric] || "指标";
  }

  function highlightMetricDisplayLabel(item) {
    const label = item && item.metricLabel ? item.metricLabel : highlightMetricLabel(item && item.metric);
    return String(label).replace(/^整体/, "");
  }

  function highlightConditions(row) {
    const conditions = Array.isArray(row.selectionConditions) ? row.selectionConditions.filter(Boolean) : [];
    return conditions.length ? conditions : [row.description || `高光帧 ${highlightSecond(row.second)}s`];
  }

  function renderHighlights() {
    const available = highlightOverview.status === "available";
    highlightsUnavailable.hidden = available;
    highlightsContent.hidden = !available;
    if (!available) return;
    const cards = Array.isArray(highlightOverview.cards) ? highlightOverview.cards : [];
    const pageCount = Math.max(1, Math.ceil(cards.length / highlightPageSize));
    highlightState.page = Math.min(highlightState.page, pageCount);
    const pageCards = cards.slice((highlightState.page - 1) * highlightPageSize, highlightState.page * highlightPageSize);
    if (!cards.length) {
      highlightGrid.innerHTML = '<div class="empty">暂无可展示的高光帧</div>';
    } else {
      highlightGrid.innerHTML = pageCards.map((row, index) => {
        const cardIndex = (highlightState.page - 1) * highlightPageSize + index;
        const conditions = highlightConditions(row);
        const alt = `${conditions.join("、")}，${row.materialTitle}`;
        const conditionMarkup = conditions.map(condition => `<li>${escaped(condition)}</li>`).join("");
        const values = (Array.isArray(row.selectionEvidence) ? row.selectionEvidence : []).map(item => `<li>${escaped(highlightSecond(row.second))}s · ${escaped(highlightMetricDisplayLabel(item))} ${escaped(item.value)}</li>`).join("");
        return `<article class="highlight-card"><button class="highlight-image-button" type="button" data-highlight-index="${cardIndex}" aria-label="点击放大查看：${escaped(alt)}"><img src="${escaped(row.artifact.path)}" alt="${escaped(alt)}"></button><ul class="highlight-conditions" aria-label="筛选条件">${conditionMarkup}</ul><ul class="highlight-values" aria-label="高光帧数值">${values}</ul><a href="#single-material-data" class="highlight-material" data-highlight-material-id="${escaped(row.materialId)}" aria-label="查看素材详情：${escaped(row.materialTitle)}">${escaped(row.materialTitle)}</a></article>`;
      }).join("");
      highlightGrid.querySelectorAll("button[data-highlight-index]").forEach(button => {
        const open = () => openHighlightModal(Number(button.dataset.highlightIndex), button);
        button.addEventListener("click", open);
        button.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " " || event.key === "Space") { event.preventDefault(); open(); }
        });
      });
      highlightGrid.querySelectorAll("[data-highlight-material-id]").forEach(link => {
        link.addEventListener("click", event => {
          event.preventDefault();
          requestHighlightMaterialDetail(event.currentTarget.dataset.highlightMaterialId);
        });
      });
    }
    const start = cards.length ? (highlightState.page - 1) * highlightPageSize + 1 : 0;
    const end = Math.min(highlightState.page * highlightPageSize, cards.length);
    document.getElementById("highlight-summary").textContent = `共 ${cards.length} 张`;
    document.getElementById("highlight-range").textContent = cards.length ? `当前 ${start}–${end} / ${cards.length}` : "暂无记录";
    document.getElementById("highlight-pages").textContent = `${highlightState.page} / ${pageCount}`;
    document.getElementById("highlight-first").disabled = highlightState.page <= 1;
    document.getElementById("highlight-previous").disabled = highlightState.page <= 1;
    document.getElementById("highlight-next").disabled = highlightState.page >= pageCount;
    document.getElementById("highlight-last").disabled = highlightState.page >= pageCount;
  }

  function openHighlightModal(index, trigger) {
    const card = highlightOverview.cards?.[index];
    if (!card) return;
    const conditions = highlightConditions(card);
    highlightState.modalCard = card;
    highlightState.modalTrigger = trigger;
    highlightModalTitle.textContent = `${conditions.join("、")} · ${card.materialTitle}`;
    highlightModalImage.src = card.artifact.path;
    highlightModalImage.alt = `${conditions.join("、")}，${card.materialTitle}`;
    highlightModal.hidden = false;
    document.body.classList.add("modal-open");
    document.getElementById("highlight-modal-close").focus();
  }

  function requestHighlightMaterialDetail(materialId) {
    // Keep the fifth-section navigation contract without changing tabs until that section exists.
    document.dispatchEvent(new CustomEvent("highlight-material-detail", { detail: { materialId } }));
  }

  function closeHighlightModal() {
    highlightModal.hidden = true;
    document.body.classList.remove("modal-open");
    const trigger = highlightState.modalTrigger;
    highlightState.modalCard = null;
    highlightState.modalTrigger = null;
    if (trigger) trigger.focus();
  }

  function formatAudienceValue(value, metric) {
    const options = metric && metric.unit === "currency"
      ? { minimumFractionDigits: 2, maximumFractionDigits: 2 }
      : { maximumFractionDigits: 2 };
    const formatted = new Intl.NumberFormat("zh-CN", options).format(Number(value) || 0);
    return metric && metric.unit === "currency" ? `${formatted} 元` : formatted;
  }

  function audienceDimension(id) {
    if (id === "province" || id === "city") return audience.dimensions?.geography?.levels?.[id];
    return audience.dimensions?.[id];
  }

  function audienceBuckets(id, metricId) {
    return audienceDimension(id)?.bucketsByMetric?.[metricId] || [];
  }

  function hideChartTooltip() {
    chartTooltip.hidden = true;
    audienceState.tooltip = null;
  }

  function showChartTooltip(event, bucket, metric, dimension, rank) {
    const source = event.currentTarget || event.target;
    const rect = source && source.getBoundingClientRect ? source.getBoundingClientRect() : null;
    chartTooltip.innerHTML = `<div>分类：${escaped(bucket.label)}</div><div>${escaped(metric.label)}：${formatAudienceValue(bucket.value, metric)}</div><div>占比：${new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 }).format(bucket.share)}</div><div>本维度总值：${formatAudienceValue(bucket.total, metric)}</div>${rank ? `<div>排名：${rank}</div>` : ""}`;
    chartTooltip.hidden = false;
    audienceState.tooltip = { bucket, metricId: metric.id, dimension, rank };
    const left = rect ? rect.right + 8 : 16;
    const top = rect ? rect.top : 16;
    chartTooltip.style.left = `${Math.min(Math.max(12, left), Math.max(12, window.innerWidth - chartTooltip.offsetWidth - 12))}px`;
    chartTooltip.style.top = `${Math.min(Math.max(12, top), Math.max(12, window.innerHeight - chartTooltip.offsetHeight - 12))}px`;
  }

  function chartEmpty(label) {
    return `<div class="empty">暂无${escaped(label)}有效数据</div>`;
  }

  function attachChartInteractions(container, buckets, metric, dimension) {
    container.querySelectorAll("[data-chart-index]").forEach((element, index) => {
      const bucket = buckets[Number(element.dataset.chartIndex)];
      const rank = dimension === "province" || dimension === "city" ? Number(element.dataset.chartRank) : index + 1;
      element.addEventListener("pointerenter", event => showChartTooltip(event, bucket, metric, dimension, rank));
      element.addEventListener("pointerleave", hideChartTooltip);
      element.addEventListener("focus", event => showChartTooltip(event, bucket, metric, dimension, rank));
      element.addEventListener("blur", hideChartTooltip);
    });
  }

  function setDonutSelection(dimension, index) {
    audienceState.donutSelection[dimension] = index == null ? null : Number(index);
    document.querySelectorAll(`[data-donut-dimension="${dimension}"][data-chart-index]`).forEach(element => {
      const selected = Number(element.dataset.chartIndex) === audienceState.donutSelection[dimension];
      element.classList.toggle("active", selected);
      if (element.tagName.toLowerCase() === "button") element.setAttribute("aria-pressed", String(selected));
      if (element.classList.contains("donut-segment")) {
        const angle = Number(element.dataset.donutAngle || 0);
        const distance = selected ? 8 : 0;
        const offsetX = Math.cos(angle) * distance;
        const offsetY = Math.sin(angle) * distance;
        element.style.transform = `translate(${offsetX.toFixed(2)}px, ${offsetY.toFixed(2)}px)`;
      }
    });
  }

  function toggleDonutSelection(dimension, index) {
    const currentIndex = audienceState.donutSelection[dimension];
    const nextIndex = currentIndex === Number(index) ? null : Number(index);
    setDonutSelection(dimension, nextIndex);
  }

  function renderBars(containerId, buckets, metric, dimension, visibleCount) {
    const container = document.getElementById(containerId);
    if (!buckets.length) { container.innerHTML = chartEmpty(audienceDimension(dimension)?.label || "图表"); return; }
    const shown = buckets.slice(0, visibleCount || buckets.length);
    const isGeography = dimension === "province" || dimension === "city";
    const geoBand = shown.length > 24 ? 56 : shown.length > 14 ? 64 : 74;
    const width = dimension === "eight_groups"
      ? Math.max(760, (container.clientWidth || 900) - 8)
      : Math.max(560, shown.length * (isGeography ? geoBand : 74));
    const height = 290;
    const left = 42;
    const bottom = 72;
    const plotHeight = height - bottom - 20;
    const max = Math.max(...shown.map(bucket => Number(bucket.value)), 1);
    const band = (width - left - 18) / shown.length;
    const geoBarRatio = isGeography ? (shown.length > 24 ? .68 : shown.length > 14 ? .60 : .52) : .52;
    const geoLabelStep = 1;
    const geoValueStep = 1;
    const bars = shown.map((bucket, index) => {
      const barHeight = Math.max(2, Number(bucket.value) / max * plotHeight);
      const barWidth = band * geoBarRatio;
      const x = left + index * band + (band - barWidth) / 2;
      const y = height - bottom - barHeight;
      const showAxisLabel = !isGeography || index % geoLabelStep === 0 || index === shown.length - 1;
      const showValue = !isGeography || index % geoValueStep === 0 || index === shown.length - 1;
      const labelLimit = 8;
      const label = bucket.label.length > labelLimit ? `${bucket.label.slice(0, labelLimit)}…` : bucket.label;
      const center = x + barWidth / 2;
      const valueMarkup = showValue ? `<text class="chart-value" x="${center.toFixed(2)}" y="${Math.max(14, y - 6).toFixed(2)}" text-anchor="middle">${escaped(formatAudienceValue(bucket.value, metric))}</text>` : "";
      const labelMarkup = showAxisLabel ? `<text class="chart-label" x="${center.toFixed(2)}" y="${height - 42}" text-anchor="middle">${escaped(label)}</text>` : "";
      return `<rect class="chart-bar" data-chart-index="${index}" data-chart-rank="${index + 1}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="5" tabindex="0" role="img" aria-label="${escaped(bucket.label)} ${escaped(metric.label)} ${escaped(formatAudienceValue(bucket.value, metric))}"></rect>${valueMarkup}${labelMarkup}`;
    }).join("");
    const svgStyle = dimension === "eight_groups" ? `style="width:100%;height:${height}px"` : `style="width:${width}px;height:${height}px"`;
    const svgMarkup = `<svg ${svgStyle} preserveAspectRatio="none" viewBox="0 0 ${width} ${height}" role="group" aria-label="${escaped(audienceDimension(dimension)?.label || "柱状图")}"><line class="chart-axis" x1="${left}" y1="${height - bottom}" x2="${width - 12}" y2="${height - bottom}"></line>${bars}</svg>`;
    container.innerHTML = isGeography ? `<div class="geo-chart-scroll">${svgMarkup}</div>` : svgMarkup;
    attachChartInteractions(container, shown, metric, dimension);
  }

  function donutPath(start, end, outer, inner, cx, cy) {
    const point = (radius, angle) => `${(cx + radius * Math.cos(angle)).toFixed(2)} ${(cy + radius * Math.sin(angle)).toFixed(2)}`;
    const sweep = end - start;
    const fullTurn = Math.PI * 2;
    if (sweep >= fullTurn - 0.0001) {
      const midpoint = start + Math.PI;
      return `M ${point(outer, start)} A ${outer} ${outer} 0 1 1 ${point(outer, midpoint)} A ${outer} ${outer} 0 1 1 ${point(outer, start)} L ${point(inner, start)} A ${inner} ${inner} 0 1 0 ${point(inner, midpoint)} A ${inner} ${inner} 0 1 0 ${point(inner, start)} Z`;
    }
    const large = end - start > Math.PI ? 1 : 0;
    return `M ${point(outer, start)} A ${outer} ${outer} 0 ${large} 1 ${point(outer, end)} L ${point(inner, end)} A ${inner} ${inner} 0 ${large} 0 ${point(inner, start)} Z`;
  }

  function renderDonut(containerId, legendId, buckets, metric, dimension) {
    const container = document.getElementById(containerId);
    const legend = document.getElementById(legendId);
    if (!buckets.length) { container.innerHTML = chartEmpty(audienceDimension(dimension)?.label || "图表"); legend.innerHTML = ""; return; }
    const colors = ["#2563eb", "#60a5fa", "#14b8a6", "#f59e0b", "#ef6a6a", "#8b5cf6", "#64748b", "#22c55e"];
    const total = buckets[0].total;
    let cursor = -Math.PI / 2;
    const segments = buckets.map((bucket, index) => {
      const end = cursor + Math.max(0, bucket.share) * Math.PI * 2;
      const angle = (cursor + end) / 2;
      const path = donutPath(cursor, end, 92, 56, 116, 124);
      cursor = end;
      return `<path class="donut-segment" data-donut-dimension="${dimension}" data-chart-index="${index}" data-donut-angle="${angle.toFixed(6)}" d="${path}" fill="${colors[index % colors.length]}" tabindex="0" role="img" aria-label="${escaped(bucket.label)} ${escaped(metric.label)} ${escaped(formatAudienceValue(bucket.value, metric))}"></path>`;
    }).join("");
    container.innerHTML = `<svg viewBox="0 0 232 250" role="group" aria-label="${escaped(audienceDimension(dimension)?.label || "环形图")}">${segments}<text class="donut-center-label" x="116" y="118">${escaped(metric.label)}</text><text class="donut-center-value" x="116" y="143">${escaped(formatAudienceValue(total, metric))}</text></svg>`;
    legend.innerHTML = buckets.map((bucket, index) => `<button type="button" class="legend-item" data-donut-dimension="${dimension}" data-chart-index="${index}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)} ${escaped(metric.label)} ${escaped(formatAudienceValue(bucket.value, metric))}"><span class="legend-swatch" style="background:${colors[index % colors.length]}"></span><span>${escaped(bucket.label)}<span class="legend-detail">${escaped(formatAudienceValue(bucket.value, metric))} · ${new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 }).format(bucket.share)}</span></span></button>`).join("");
    attachChartInteractions(container, buckets, metric, dimension);
    attachChartInteractions(legend, buckets, metric, dimension);
    const selectedIndex = audienceState.donutSelection[dimension];
    setDonutSelection(dimension, selectedIndex != null && selectedIndex < buckets.length ? selectedIndex : null);
    document.querySelectorAll(`[data-donut-dimension="${dimension}"][data-chart-index]`).forEach(element => element.addEventListener("click", event => {
      toggleDonutSelection(dimension, event.currentTarget.dataset.chartIndex);
    }));
  }

  function renderGeography() {
    if (audience.status !== "available") return;
    const metric = metricMap[audienceState.metricId] || audience.metrics[0];
    const geography = audienceBuckets(audienceState.geoLevel, audienceState.metricId);
    audienceState.geoVisibleCount = Math.min(Math.max(1, audienceState.geoVisibleCount), Math.max(1, geography.length));
    const slider = document.getElementById("geo-visible-count");
    slider.min = geography.length ? 1 : 1;
    slider.max = Math.max(1, geography.length);
    slider.value = geography.length ? audienceState.geoVisibleCount : 1;
    document.getElementById("geo-visible-label").textContent = `显示前 ${geography.length ? audienceState.geoVisibleCount : 0} / ${geography.length} 个`;
    renderBars("audience-geography", geography, metric, audienceState.geoLevel, audienceState.geoVisibleCount);
  }

  function renderAudience() {
    const content = document.getElementById("audience-content");
    const unavailable = document.getElementById("audience-unavailable");
    const available = audience.status === "available";
    content.hidden = !available;
    unavailable.hidden = available;
    document.querySelectorAll("[data-audience-metric]").forEach(button => button.classList.toggle("active", button.dataset.audienceMetric === audienceState.metricId));
    document.querySelectorAll("[data-geo-level]").forEach(button => button.classList.toggle("active", button.dataset.geoLevel === audienceState.geoLevel));
    if (!available) return;
    const metric = metricMap[audienceState.metricId] || audience.metrics[0];
    renderBars("audience-eight-groups", audienceBuckets("eight_groups", audienceState.metricId), metric, "eight_groups");
    renderDonut("audience-gender", "legend-gender", audienceBuckets("gender", audienceState.metricId), metric, "gender");
    renderDonut("audience-age", "legend-age", audienceBuckets("age", audienceState.metricId), metric, "age");
    renderGeography();
  }

  function hideScriptFormulaTooltip() {
    chartTooltip.hidden = true;
    scriptFormulaState.tooltip = null;
  }

  function formulaShare(bucket) {
    return new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 }).format(bucket.share);
  }

  function showScriptFormulaTooltip(event, bucket, level) {
    const source = event.currentTarget || event.target;
    const rect = source && source.getBoundingClientRect ? source.getBoundingClientRect() : null;
    chartTooltip.innerHTML = `<div>${escaped(level)}：${escaped(bucket.label)}</div><div>覆盖素材数：${escaped(bucket.value)}</div><div>占比：${escaped(formulaShare(bucket))}</div><div>${level === "一级内容公式" ? "有公式素材总数" : "当前一级覆盖素材总数"}：${escaped(bucket.total)}</div>`;
    chartTooltip.hidden = false;
    scriptFormulaState.tooltip = { bucket, level };
    const left = rect ? rect.right + 8 : 16;
    const top = rect ? rect.top : 16;
    chartTooltip.style.left = `${Math.min(Math.max(12, left), Math.max(12, window.innerWidth - chartTooltip.offsetWidth - 12))}px`;
    chartTooltip.style.top = `${Math.min(Math.max(12, top), Math.max(12, window.innerHeight - chartTooltip.offsetHeight - 12))}px`;
  }

  function attachScriptFormulaInteractions(container, buckets, level) {
    container.querySelectorAll("[data-script-formula-index], [data-script-formula-secondary-index]").forEach(element => {
      const rawIndex = element.dataset.scriptFormulaSecondaryIndex ?? element.dataset.scriptFormulaIndex;
      const bucket = buckets[Number(rawIndex)];
      element.addEventListener("pointerenter", event => showScriptFormulaTooltip(event, bucket, level));
      element.addEventListener("pointerleave", hideScriptFormulaTooltip);
      element.addEventListener("focus", event => showScriptFormulaTooltip(event, bucket, level));
      element.addEventListener("blur", hideScriptFormulaTooltip);
      if (level === "二级内容公式") {
        element.addEventListener("click", event => toggleScriptFormulaSelection(event.currentTarget.dataset.scriptFormulaSecondaryIndex));
      }
    });
  }

  function setScriptFormulaSelection(index) {
    scriptFormulaState.selectedSecondary = index == null ? null : Number(index);
    document.querySelectorAll("#script-formula-secondary-chart [data-script-formula-secondary-index], #script-formula-secondary-legend [data-script-formula-secondary-index]").forEach(element => {
      const selected = Number(element.dataset.scriptFormulaSecondaryIndex) === scriptFormulaState.selectedSecondary;
      element.classList.toggle("active", selected);
      element.setAttribute("aria-pressed", String(selected));
      if (element.classList.contains("script-formula-segment")) {
        const angle = Number(element.dataset.scriptFormulaAngle || 0);
        const distance = selected ? 8 : 0;
        element.style.transform = `translate(${(Math.cos(angle) * distance).toFixed(2)}px, ${(Math.sin(angle) * distance).toFixed(2)}px)`;
      }
    });
  }

  function toggleScriptFormulaSelection(index) {
    const nextIndex = scriptFormulaState.selectedSecondary === Number(index) ? null : Number(index);
    setScriptFormulaSelection(nextIndex);
  }

  function renderScriptFormula() {
    const chart = document.getElementById("script-formula-chart");
    const secondaryCard = document.getElementById("script-formula-secondary-card");
    const secondaryChart = document.getElementById("script-formula-secondary-chart");
    const secondaryLegend = document.getElementById("script-formula-secondary-legend");
    const secondaryTitle = document.getElementById("script-formula-secondary-title");
    const title = document.getElementById("script-formula-title");
    const hint = document.getElementById("script-formula-hint");
    const available = scriptFormula.status === "available";
    hideScriptFormulaTooltip();
    if (!available) {
      title.textContent = "一级内容公式";
      hint.hidden = true;
      chart.innerHTML = '<div class="empty">脚本内容公式不可用</div>';
      secondaryCard.hidden = true;
      secondaryChart.innerHTML = "";
      secondaryLegend.innerHTML = "";
      return;
    }
    const primaryBuckets = Array.isArray(scriptFormula.primaryBuckets) ? scriptFormula.primaryBuckets : [];
    scriptFormulaState.selectedSecondary = null;
    title.textContent = "一级内容公式";
    hint.hidden = false;
    if (!primaryBuckets.length) {
      chart.innerHTML = '<div class="empty">暂无可展示的一级内容公式</div>';
      secondaryCard.hidden = true;
      return;
    }

    const width = Math.max(560, Math.min(760, primaryBuckets.length * 170, (chart.clientWidth || 900) - 8));
    const height = 290;
    const left = 42;
    const bottom = 72;
    const plotHeight = height - bottom - 20;
    const max = Math.max(...primaryBuckets.map(bucket => Number(bucket.value)), 1);
    const band = (width - left - 18) / primaryBuckets.length;
    const bars = primaryBuckets.map((bucket, index) => {
      const barHeight = Math.max(2, Number(bucket.value) / max * plotHeight);
      const barWidth = band * .52;
      const x = left + index * band + (band - barWidth) / 2;
      const y = height - bottom - barHeight;
      const center = x + barWidth / 2;
      const shortLabel = bucket.label.length > 8 ? `${bucket.label.slice(0, 8)}…` : bucket.label;
      return `<rect class="script-formula-bar" data-script-formula-index="${index}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="5" tabindex="0" role="button" aria-label="${escaped(bucket.label)}，覆盖 ${escaped(bucket.value)} 个素材，占比 ${escaped(formulaShare(bucket))}" data-script-primary="${escaped(bucket.label)}"></rect><text class="chart-value" x="${center.toFixed(2)}" y="${Math.max(14, y - 6).toFixed(2)}" text-anchor="middle">${escaped(bucket.value)}</text><text class="chart-label" x="${center.toFixed(2)}" y="${height - 42}" text-anchor="middle">${escaped(shortLabel)}</text>`;
    }).join("");
    chart.innerHTML = `<svg class="script-formula-svg" style="width:${width}px;height:${height}px" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="group" aria-label="一级内容公式柱状图"><line class="chart-axis" x1="${left}" y1="${height - bottom}" x2="${width - 12}" y2="${height - bottom}"></line>${bars}</svg>`;
    attachScriptFormulaInteractions(chart, primaryBuckets, "一级内容公式");
    chart.querySelectorAll("[data-script-primary]").forEach(element => {
      const select = () => { scriptFormulaState.selectedPrimary = element.dataset.scriptPrimary; renderScriptFormula(); };
      element.addEventListener("click", select);
      element.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
      });
    });

    if (scriptFormulaState.selectedPrimary == null) {
      secondaryCard.hidden = true;
      secondaryChart.innerHTML = "";
      secondaryLegend.innerHTML = "";
      return;
    }

    const primary = scriptFormulaState.selectedPrimary;
    const secondaryBuckets = scriptFormula.secondaryBucketsByPrimary?.[primary] || [];
    secondaryCard.hidden = false;
    secondaryTitle.textContent = `${primary} · 二级内容公式分布`;
    if (!secondaryBuckets.length) {
      secondaryChart.innerHTML = `<div class="empty">暂无该一级内容公式的二级公式数据</div>`;
      secondaryLegend.innerHTML = "";
      return;
    }
    const colors = ["#2563eb", "#60a5fa", "#14b8a6", "#f59e0b", "#ef6a6a", "#8b5cf6", "#64748b", "#22c55e"];
    const secondaryValueTotal = secondaryBuckets.reduce((sum, bucket) => sum + Math.max(0, Number(bucket.value) || 0), 0);
    let cursor = -Math.PI / 2;
    const segments = secondaryBuckets.map((bucket, index) => {
      const sliceShare = secondaryValueTotal ? Math.max(0, Number(bucket.value) || 0) / secondaryValueTotal : 0;
      const end = cursor + sliceShare * Math.PI * 2;
      const angle = (cursor + end) / 2;
      const path = donutPath(cursor, end, 96, 58, 116, 130);
      cursor = end;
      return `<path class="donut-segment script-formula-segment" data-script-formula-secondary-index="${index}" data-script-formula-angle="${angle.toFixed(6)}" d="${path}" fill="${colors[index % colors.length]}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖 ${escaped(bucket.value)} 个素材，占比 ${escaped(formulaShare(bucket))}"></path>`;
    }).join("");
    const total = secondaryBuckets[0].total;
    secondaryChart.innerHTML = `<svg viewBox="0 0 232 270" role="group" aria-label="${escaped(primary)}二级内容公式环形图">${segments}<text class="donut-center-label" x="116" y="124">当前一级覆盖素材数</text><text class="donut-center-value" x="116" y="150">${escaped(total)}</text></svg>`;
    secondaryLegend.innerHTML = secondaryBuckets.map((bucket, index) => `<button type="button" class="legend-item" data-script-formula-secondary-index="${index}" tabindex="0" role="img" aria-pressed="false" aria-label="${escaped(bucket.label)}，覆盖 ${escaped(bucket.value)} 个素材，占比 ${escaped(formulaShare(bucket))}"><span class="legend-swatch" style="background:${colors[index % colors.length]}"></span><span>${escaped(bucket.label)}<span class="legend-detail">${escaped(bucket.value)} 个素材 · ${escaped(formulaShare(bucket))}</span></span></button>`).join("");
    attachScriptFormulaInteractions(secondaryChart, secondaryBuckets, "二级内容公式");
    attachScriptFormulaInteractions(secondaryLegend, secondaryBuckets, "二级内容公式");
    setScriptFormulaSelection(null);
  }

  function fieldMatches(field, query) {
    const normalizedQuery = normalize(query);
    return !normalizedQuery || normalize(field.label).includes(normalizedQuery) || normalize(field.id).includes(normalizedQuery);
  }

  function dialogCatalog() {
    if (fieldDialog.scope !== "single") return orderedPresentationFields;
    const material = singleMaterialById[fieldDialog.materialId];
    const rawFields = material?.leaderboard?.fields || [];
    const enriched = rawFields.map(field => ({ ...field, group: field.group || fieldMap[field.id]?.group || "其他指标", label: field.label && field.label !== field.id ? field.label : fieldMap[field.id]?.label || field.id }));
    const rank = Object.fromEntries(orderedPresentationFields.map((field, index) => [field.id, index]));
    return enriched.sort((left, right) => (rank[left.id] ?? orderedPresentationFields.length) - (rank[right.id] ?? orderedPresentationFields.length));
  }

  function dialogGroupNames(catalog) {
    return [...new Set(catalog.map(field => field.group || "其他指标"))];
  }

  function dialogRequiredIds(catalog) {
    return fieldDialog.scope === "single" ? [] : requiredFieldIds.filter(id => catalog.some(field => field.id === id));
  }

  function orderedDialogFieldIds(fieldIds, catalog) {
    const selected = new Set(Array.isArray(fieldIds) ? fieldIds : []);
    dialogRequiredIds(catalog).forEach(id => selected.add(id));
    return catalog.map(field => field.id).filter(id => selected.has(id));
  }

  function renderFieldDialog() {
    const catalog = dialogCatalog();
    const groups = dialogGroupNames(catalog);
    const requiredIds = dialogRequiredIds(catalog);
    const hasSingleSelection = fieldDialog.scope === "single" && Object.prototype.hasOwnProperty.call(singleState.deliveryFields, fieldDialog.materialId);
    const initialIds = fieldDialog.scope === "single"
      ? (Array.isArray(state.draftFieldIds) ? state.draftFieldIds : (hasSingleSelection ? singleState.deliveryFields[fieldDialog.materialId] : defaultSingleFieldIds(catalog)))
      : (Array.isArray(state.draftFieldIds) ? state.draftFieldIds : state.visibleFieldIds);
    state.draftFieldIds = orderedDialogFieldIds(initialIds, catalog);
    fieldGroups.innerHTML = groups.map(group => {
      const count = catalog.filter(field => field.group === group).length;
      const active = !state.fieldSearch && state.activeFieldGroup === group ? " active" : "";
      return `<button type="button" class="${active.trim()}" data-field-group="${escaped(group)}">${escaped(group)}<span>${count}</span></button>`;
    }).join("");

    const searchQuery = normalize(state.fieldSearch);
    const visibleFields = searchQuery
      ? catalog.filter(field => fieldMatches(field, searchQuery))
      : catalog.filter(field => field.group === state.activeFieldGroup);
    const grouped = {};
    visibleFields.forEach(field => (grouped[field.group] ||= []).push(field));
    fieldOptions.innerHTML = Object.entries(grouped).map(([group, fields]) => `<section class="field-option-section"><h3>${escaped(group)}</h3><div class="field-option-grid">${fields.map(field => {
      const checked = state.draftFieldIds.includes(field.id) ? " checked" : "";
      const disabled = requiredIds.includes(field.id) ? " disabled" : "";
      return `<label class="field-option"><input type="checkbox" data-field-id="${escaped(field.id)}"${checked}${disabled}><span>${escaped(field.label)}</span></label>`;
    }).join("")}</div></section>`).join("") || '<div class="empty">没有匹配字段</div>';

    const selected = state.draftFieldIds.map(id => catalog.find(field => field.id === id)).filter(Boolean);
    selectedCount.textContent = `（${selected.length}）`;
    selectedFields.innerHTML = selected.map(field => {
      const required = requiredIds.includes(field.id);
      return `<li class="field-modal-selected-item"><span class="field-drag-handle" aria-hidden="true">☷</span><span class="field-selected-label" title="${escaped(field.label)}">${escaped(field.label)}</span>${required ? '<span class="field-required">固定</span>' : `<button type="button" data-remove-field-id="${escaped(field.id)}" aria-label="移除${escaped(field.label)}">×</button>`}</li>`;
    }).join("");
  }

  function openFieldDialog() {
    fieldDialog.scope = "leaderboard";
    fieldDialog.materialId = null;
    state.draftFieldIds = orderedVisibleFieldIds(state.visibleFieldIds);
    state.fieldSearch = "";
    state.activeFieldGroup = fieldGroupNames[0] || "";
    fieldSearch.value = "";
    document.getElementById("field-management-title").textContent = "自定义指标";
    document.getElementById("field-management-description").textContent = "选择后将按照自定义指标顺序呈现表格列";
    document.getElementById("field-management-footer-note").textContent = "素材名称为固定列，其他列按自定义指标顺序排列";
    fieldModal.hidden = false;
    document.body.classList.add("modal-open");
    renderFieldDialog();
    fieldSearch.focus();
  }

  function openSingleFieldDialog(material) {
    fieldDialog.scope = "single";
    fieldDialog.materialId = material.materialId;
    const catalog = dialogCatalog();
    const hasSingleSelection = Object.prototype.hasOwnProperty.call(singleState.deliveryFields, material.materialId);
    state.draftFieldIds = orderedDialogFieldIds(hasSingleSelection ? singleState.deliveryFields[material.materialId] : defaultSingleFieldIds(catalog), catalog);
    state.fieldSearch = "";
    state.activeFieldGroup = dialogGroupNames(catalog)[0] || "";
    fieldSearch.value = "";
    document.getElementById("field-management-title").textContent = "自定义指标";
    document.getElementById("field-management-description").textContent = "选择后将按照自定义指标顺序呈现当前素材的投放指标";
    document.getElementById("field-management-footer-note").textContent = "当前素材名称已在详情标题中固定展示，其他指标按自定义顺序排列";
    fieldModal.hidden = false;
    document.getElementById("single-field-button").setAttribute("aria-expanded", "true");
    document.body.classList.add("modal-open");
    renderFieldDialog();
    fieldSearch.focus();
  }

  function closeFieldDialog() {
    fieldModal.hidden = true;
    document.getElementById("single-field-button").setAttribute("aria-expanded", "false");
    state.draftFieldIds = null;
    state.fieldSearch = "";
    fieldDialog.scope = "leaderboard";
    fieldDialog.materialId = null;
    document.body.classList.remove("modal-open");
  }

  function confirmFieldDialog() {
    if (fieldDialog.scope === "single") {
      const material = singleMaterialById[fieldDialog.materialId];
      if (material) singleState.deliveryFields[material.materialId] = [...state.draftFieldIds];
      closeFieldDialog();
      if (material) singleRenderDelivery(material);
      return;
    }
    state.visibleFieldIds = orderedVisibleFieldIds(state.draftFieldIds || state.visibleFieldIds);
    state.page = 1;
    saveVisibleFields();
    closeFieldDialog();
    render();
  }

  function sortLabel(field) {
    if (state.sort.field !== field.id || state.sort.direction === "none") return "";
    return state.sort.direction === "asc" ? " ↑" : " ↓";
  }

  function renderTable(rows) {
    const tableFieldIds = orderedVisibleFieldIds(state.visibleFieldIds);
    state.visibleFieldIds = tableFieldIds;
    const visibleFields = tableFieldIds.map(id => fieldMap[id]).filter(Boolean);
    if (!visibleFields.length) { tableContainer.innerHTML = '<div class="empty">没有可选字段</div>'; return; }
    if (!rows.length) { tableContainer.innerHTML = '<div class="empty">没有匹配素材</div>'; return; }
    const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
    state.page = Math.min(state.page, pageCount);
    const pageRows = rows.slice((state.page - 1) * pageSize, state.page * pageSize);
    const header = visibleFields.map(field => `<th scope="col" class="${field.id === "roi2_material_video_name" ? "material-name-column" : ""}"><button type="button" data-sort-id="${escaped(field.id)}">${escaped(field.label)}${sortLabel(field)}</button></th>`).join("");
    const body = pageRows.map(row => `<tr>${visibleFields.map(field => {
      if (field.id === "roi2_material_video_name") return `<td class="material-name-column"><div class="material-title">${highlighted(row.values[field.id])}</div><div class="material-id">素材ID：${highlighted(row.materialId)}</div></td>`;
      return `<td>${display(row.values[field.id])}</td>`;
    }).join("")}</tr>`).join("");
    tableContainer.innerHTML = `<table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
    tableContainer.querySelectorAll("button[data-sort-id]").forEach(button => button.addEventListener("click", event => {
      const field = event.currentTarget.dataset.sortId;
      const next = state.sort.field !== field ? "asc" : state.sort.direction === "none" ? "asc" : state.sort.direction === "asc" ? "desc" : "none";
      state.sort = { field, direction: next };
      state.page = 1;
      render();
    }));
  }

  function renderPager(total) {
    const pageCount = Math.max(1, Math.ceil(total / pageSize));
    state.page = Math.min(state.page, pageCount);
    const start = total ? (state.page - 1) * pageSize + 1 : 0;
    const end = Math.min(state.page * pageSize, total);
    document.getElementById("summary").textContent = `共 ${total} 条`;
    document.getElementById("range").textContent = total ? `显示 ${start}-${end} / ${total}` : "暂无记录";
    document.getElementById("pages").textContent = `${state.page} / ${pageCount}`;
    document.getElementById("first").disabled = state.page <= 1;
    document.getElementById("previous").disabled = state.page <= 1;
    document.getElementById("next").disabled = state.page >= pageCount;
    document.getElementById("last").disabled = state.page >= pageCount;
  }

  function render() {
    const rows = matchingRows();
    renderTable(rows);
    renderPager(rows.length);
  }

  loadVisibleFields();
  document.getElementById("field-button").addEventListener("click", openFieldDialog);
  document.getElementById("single-field-button").addEventListener("click", () => {
    const material = singleCurrentMaterial();
    if (material) openSingleFieldDialog(material);
  });
  fieldModal.addEventListener("click", event => {
    const action = event.target.closest("[data-field-action]")?.dataset.fieldAction;
    if (action === "cancel") closeFieldDialog();
    if (action === "confirm") confirmFieldDialog();
    if (action === "select-all" || action === "clear-all") {
      const catalog = dialogCatalog();
      state.draftFieldIds = orderedDialogFieldIds(action === "select-all" ? catalog.map(field => field.id) : [], catalog);
      renderFieldDialog();
    }
    const group = event.target.closest("[data-field-group]")?.dataset.fieldGroup;
    if (group) {
      state.activeFieldGroup = group;
      state.fieldSearch = "";
      fieldSearch.value = "";
      renderFieldDialog();
    }
    const removeId = event.target.closest("[data-remove-field-id]")?.dataset.removeFieldId;
    const requiredIds = dialogRequiredIds(dialogCatalog());
    if (removeId && !requiredIds.includes(removeId)) {
      state.draftFieldIds = orderedDialogFieldIds(state.draftFieldIds.filter(id => id !== removeId), dialogCatalog());
      renderFieldDialog();
    }
  });
  fieldModal.addEventListener("change", event => {
    const input = event.target.closest("input[data-field-id]");
    if (!input) return;
    const selected = new Set(state.draftFieldIds);
    if (input.checked) selected.add(input.dataset.fieldId);
    else selected.delete(input.dataset.fieldId);
    state.draftFieldIds = orderedDialogFieldIds([...selected], dialogCatalog());
    renderFieldDialog();
  });
  fieldSearch.addEventListener("input", event => {
    state.fieldSearch = event.target.value;
    renderFieldDialog();
    fieldSearch.focus();
    fieldSearch.setSelectionRange(state.fieldSearch.length, state.fieldSearch.length);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !fieldModal.hidden) closeFieldDialog();
    if (event.key === "Escape" && !highlightModal.hidden) closeHighlightModal();
    if (event.key === "Escape" && !singleLightbox.hidden) singleCloseLightbox();
    if (event.key === "Escape") hideChartTooltip();
    if (event.key === "Escape") hideScriptFormulaTooltip();
  });
  highlightModal.addEventListener("click", event => {
    if (event.target.closest("[data-highlight-action='close']")) closeHighlightModal();
  });
  document.getElementById("single-lightbox-close").addEventListener("click", singleCloseLightbox);
  singleLightbox.addEventListener("click", event => { if (event.target === singleLightbox) singleCloseLightbox(); });
  document.querySelectorAll("[data-single-anchor]").forEach(button => button.addEventListener("click", () => {
    singleState.anchor = button.dataset.singleAnchor;
    document.getElementById(`single-material-${singleState.anchor}`).scrollIntoView({ behavior: "smooth", block: "start" });
    document.querySelectorAll("[data-single-anchor]").forEach(item => item.classList.toggle("active", item === button));
  }));
  if ("IntersectionObserver" in window) {
    const singleObserver = new IntersectionObserver(entries => entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      singleState.anchor = entry.target.dataset.singlePanel;
      document.querySelectorAll("[data-single-anchor]").forEach(item => item.classList.toggle("active", item.dataset.singleAnchor === singleState.anchor));
    }), { rootMargin: "-15% 0px -70% 0px" });
    document.querySelectorAll("[data-single-panel]").forEach(panel => singleObserver.observe(panel));
  }
  document.querySelectorAll("[data-section]").forEach(button => button.addEventListener("click", event => {
    const section = event.currentTarget.dataset.section;
    document.querySelectorAll("[data-section]").forEach(item => item.classList.toggle("active", item.dataset.section === section));
    document.querySelectorAll("[data-section-panel]").forEach(panel => { panel.hidden = panel.dataset.sectionPanel !== section; });
    if (section === "audience") renderAudience();
    if (section === "scripts") renderScriptFormula();
    if (section === "highlights") renderHighlights();
    if (section === "single-material") singleRender();
    if (section !== "scripts") hideScriptFormulaTooltip();
    if (section !== "audience") hideChartTooltip();
  }));
  document.getElementById("script-formula-secondary-back").addEventListener("click", () => {
    scriptFormulaState.selectedPrimary = null;
    renderScriptFormula();
  });
  document.querySelectorAll("[data-audience-metric]").forEach(button => button.addEventListener("click", event => {
    audienceState.metricId = event.currentTarget.dataset.audienceMetric;
    const total = audienceBuckets(audienceState.geoLevel, audienceState.metricId).length;
    audienceState.geoVisibleCount = Math.min(10, Math.max(1, total));
    hideChartTooltip();
    renderAudience();
  }));
  document.querySelectorAll("[data-geo-level]").forEach(button => button.addEventListener("click", event => {
    audienceState.geoLevel = event.currentTarget.dataset.geoLevel;
    const total = audienceBuckets(audienceState.geoLevel, audienceState.metricId).length;
    audienceState.geoVisibleCount = Math.min(10, Math.max(1, total));
    hideChartTooltip();
    renderGeography();
  }));
  document.getElementById("geo-visible-count").addEventListener("input", event => {
    audienceState.geoVisibleCount = Number(event.currentTarget.value);
    hideChartTooltip();
    renderGeography();
  });
  document.getElementById("highlight-first").addEventListener("click", () => { highlightState.page = 1; renderHighlights(); });
  document.getElementById("highlight-previous").addEventListener("click", () => { highlightState.page = Math.max(1, highlightState.page - 1); renderHighlights(); });
  document.getElementById("highlight-next").addEventListener("click", () => { highlightState.page += 1; renderHighlights(); });
  document.getElementById("highlight-last").addEventListener("click", () => { highlightState.page = Math.max(1, Math.ceil((highlightOverview.cards || []).length / highlightPageSize)); renderHighlights(); });
  renderAudience();
  renderScriptFormula();
  renderHighlights();
  singleRender();
  search.addEventListener("input", event => { state.query = event.target.value; state.page = 1; render(); });
  document.getElementById("first").addEventListener("click", () => { state.page = 1; render(); });
  document.getElementById("previous").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); render(); });
  document.getElementById("next").addEventListener("click", () => { state.page += 1; render(); });
  document.getElementById("last").addEventListener("click", () => { state.page = Math.max(1, Math.ceil(matchingRows().length / pageSize)); render(); });
  render();
})();
</script>
</body>
</html>
"""


def _write_atomic(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _copy_highlight_assets(model, run_dir, output_dir):
    overview = model.get("highlightOverview")
    if not isinstance(overview, dict) or overview.get("status") != "available":
        return
    cards = overview.get("cards")
    if not isinstance(cards, list) or not cards:
        return
    run_root = Path(run_dir).resolve()
    asset_dir = Path(output_dir) / "assets" / "highlights"
    asset_dir.mkdir(parents=True, exist_ok=True)
    for card in cards:
        artifact = card.get("artifact") if isinstance(card, dict) else None
        if not isinstance(artifact, dict):
            raise OverviewReportError("highlight artifact is missing")
        source = (run_root / Path(*PurePosixPath(artifact["path"]).parts)).resolve()
        try:
            source.relative_to(run_root)
        except ValueError as exc:
            raise OverviewReportError("highlight artifact path escapes run") from exc
        suffix = source.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise OverviewReportError("highlight artifact extension is invalid")
        expected_hash = artifact.get("sha256")
        expected_size = artifact.get("sizeBytes")
        destination = asset_dir / (expected_hash + suffix)
        shutil.copyfile(source, destination)
        actual_bytes = destination.read_bytes()
        actual_hash = hashlib.sha256(actual_bytes).hexdigest()
        if actual_hash != expected_hash or len(actual_bytes) != expected_size:
            raise OverviewReportError("highlight preview asset hash or size mismatch")
        artifact.clear()
        artifact.update({"path": (Path("assets") / "highlights" / destination.name).as_posix(), "sha256": actual_hash, "sizeBytes": len(actual_bytes)})


def _copy_single_material_assets(model, run_dir, output_dir):
    overview = model.get("singleMaterialOverview")
    if not isinstance(overview, dict) or overview.get("status") != "available":
        return
    run_root = Path(run_dir).resolve()
    base_output = Path(output_dir)

    def copy_artifact(artifact, folder, suffixes):
        if not isinstance(artifact, dict):
            raise OverviewReportError("single-material artifact is missing")
        path_text = artifact.get("path")
        relative = PurePosixPath(path_text) if isinstance(path_text, str) else None
        if relative is None or relative.is_absolute() or "\\" in path_text or any(part in {"", ".", ".."} for part in relative.parts):
            raise OverviewReportError("single-material artifact path is unsafe")
        source = (run_root / Path(*relative.parts)).resolve()
        try:
            source.relative_to(run_root)
        except ValueError as exc:
            raise OverviewReportError("single-material artifact path escapes run") from exc
        if source.suffix.lower() not in suffixes or not source.is_file():
            raise OverviewReportError("single-material artifact is unavailable")
        expected_hash, expected_size = artifact.get("sha256"), artifact.get("sizeBytes")
        data = source.read_bytes()
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hash or len(data) != expected_size:
            raise OverviewReportError("single-material artifact hash or size mismatch")
        destination_dir = base_output / "assets" / folder
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / (actual_hash + source.suffix.lower())
        shutil.copyfile(source, destination)
        copied = destination.read_bytes()
        if hashlib.sha256(copied).hexdigest() != actual_hash or len(copied) != len(data):
            raise OverviewReportError("single-material preview asset hash or size mismatch")
        artifact.update({"path": (Path("assets") / folder / destination.name).as_posix(), "sha256": actual_hash, "sizeBytes": len(copied)})

    for material in overview.get("materials", []):
        if not isinstance(material, dict):
            continue
        highlights = material.get("highlights")
        if not isinstance(highlights, dict):
            continue
        for card in highlights.get("cards", []):
            if isinstance(card, dict):
                copy_artifact(card.get("artifact"), "single-material/highlights", {".jpg", ".jpeg", ".png", ".webp"})
        video = highlights.get("video")
        if isinstance(video, dict) and video.get("status") == "available":
            copy_artifact(video, "single-material/videos", {".mp4", ".webm"})


def write_overview(run_dir, output_dir, registry_path):
    model = build_overview_data(run_dir, registry_path)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    data_path = output_path / "overview-data.json"
    html_path = output_path / "index.html"
    _copy_highlight_assets(model, run_dir, output_path)
    _copy_single_material_assets(model, run_dir, output_path)
    data_text = json.dumps(model, ensure_ascii=False, indent=2) + "\n"
    html_text = render_overview_html(model)
    _write_atomic(data_path, data_text)
    try:
        _write_atomic(html_path, html_text)
    except Exception:
        try:
            data_path.unlink()
        except OSError:
            pass
        raise
    run_root = Path(run_dir).resolve()
    try:
        output_paths = [path.resolve().relative_to(run_root).as_posix() for path in (data_path, html_path)]
    except ValueError:
        output_paths = [path.name for path in (data_path, html_path)]
    return {
        "status": "succeeded",
        "outputPaths": output_paths,
        "recordCount": model["source"]["recordCount"],
        "sourceSha256": model["source"]["sha256"],
    }
