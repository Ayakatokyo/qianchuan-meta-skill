"""Build a redacted, offline overview of every material in a completed run."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from field_registry import FieldRegistryError, load_field_registry


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

    return {
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
main { max-width: 1500px; margin: 0 auto; }
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
.field-modal-selected-header strong { font-size: 14px; }
.field-modal-selected-header span { color: var(--muted); font-size: 12px; }
.field-modal-selected-header button { min-height: 28px; padding: 4px 8px; border: 0; color: var(--accent); font-size: 12px; }
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
@media (max-width: 900px) { .field-modal-body { grid-template-columns: 160px minmax(0, 1fr); } .field-modal-selected { grid-column: 2; max-height: 190px; border-top: 1px solid var(--line); } }
@media (max-width: 700px) { body { padding: 18px 12px; } h1 { font-size: 24px; } input[type=search] { min-width: 0; } .field-modal { padding: 10px; } .field-modal-panel { height: calc(100vh - 20px); } .field-modal-body { display: flex; flex-direction: column; } .field-modal-groups { display: flex; gap: 6px; padding: 10px; overflow-x: auto; overflow-y: hidden; border-right: 0; border-bottom: 1px solid var(--line); } .field-modal-groups button { flex: 0 0 auto; width: auto; margin: 0; white-space: nowrap; } .field-modal-selected { max-height: 190px; border-top: 1px solid var(--line); border-left: 0; } .field-option-grid { grid-template-columns: 1fr; } .field-modal-footer { align-items: flex-end; flex-direction: column; } .field-modal-footer-actions { width: 100%; } .field-modal-footer-actions button { flex: 1; } .field-modal-footer p { align-self: flex-start; } .material-name-column { min-width: 300px; width: 300px; } }
</style>
</head>
<body>
<main>
<h1>千川素材榜单数据工作台</h1>
<div class="toolbar">
  <input id="search" type="search" placeholder="输入素材标题或 ID" aria-label="输入素材标题或 ID">
  <button id="field-button" type="button">自定义指标</button>
</div>
<section id="field-management" class="field-modal" role="dialog" aria-modal="true" aria-labelledby="field-management-title" hidden>
  <div class="field-modal-backdrop" data-field-action="cancel"></div>
  <div class="field-modal-panel">
    <header class="field-modal-header">
      <div><h2 id="field-management-title">自定义指标</h2><p>选择后将按照自定义指标顺序呈现表格列</p></div>
      <button class="field-modal-close" type="button" data-field-action="cancel" aria-label="关闭自定义指标">×</button>
    </header>
    <div class="field-modal-body">
      <nav id="field-groups" class="field-modal-groups" aria-label="字段分组"></nav>
      <section class="field-modal-main" aria-label="可选指标">
        <div class="field-modal-search"><input id="field-search" type="search" placeholder="搜索指标" aria-label="搜索指标"></div>
        <div id="field-options" class="field-options"></div>
      </section>
      <aside class="field-modal-selected" aria-label="已选择指标">
        <div class="field-modal-selected-header"><div><strong>已选择</strong> <span id="selected-count"></span></div><button type="button" data-field-action="reset">重置</button></div>
        <ol id="selected-fields" class="field-modal-selected-list"></ol>
      </aside>
    </div>
    <footer class="field-modal-footer">
      <p>素材名称为固定列，其他列按自定义指标顺序排列</p>
      <div class="field-modal-footer-actions"><button type="button" data-field-action="cancel">取消</button><button class="primary" type="button" data-field-action="confirm">确定</button></div>
    </footer>
  </div>
</section>
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

  function fieldMatches(field, query) {
    const normalizedQuery = normalize(query);
    return !normalizedQuery || normalize(field.label).includes(normalizedQuery) || normalize(field.id).includes(normalizedQuery);
  }

  function renderFieldDialog() {
    state.draftFieldIds = orderedVisibleFieldIds(state.draftFieldIds || state.visibleFieldIds);
    fieldGroups.innerHTML = fieldGroupNames.map(group => {
      const count = orderedPresentationFields.filter(field => field.group === group).length;
      const active = !state.fieldSearch && state.activeFieldGroup === group ? " active" : "";
      return `<button type="button" class="${active.trim()}" data-field-group="${escaped(group)}">${escaped(group)}<span>${count}</span></button>`;
    }).join("");

    const searchQuery = normalize(state.fieldSearch);
    const visibleFields = searchQuery
      ? orderedPresentationFields.filter(field => fieldMatches(field, searchQuery))
      : orderedPresentationFields.filter(field => field.group === state.activeFieldGroup);
    const grouped = {};
    visibleFields.forEach(field => (grouped[field.group] ||= []).push(field));
    fieldOptions.innerHTML = Object.entries(grouped).map(([group, fields]) => `<section class="field-option-section"><h3>${escaped(group)}</h3><div class="field-option-grid">${fields.map(field => {
      const checked = state.draftFieldIds.includes(field.id) ? " checked" : "";
      const disabled = requiredFieldIds.includes(field.id) ? " disabled" : "";
      return `<label class="field-option"><input type="checkbox" data-field-id="${escaped(field.id)}"${checked}${disabled}><span>${escaped(field.label)}</span></label>`;
    }).join("")}</div></section>`).join("") || '<div class="empty">没有匹配字段</div>';

    const selected = state.draftFieldIds.map(id => fieldMap[id]).filter(Boolean);
    selectedCount.textContent = `（${selected.length}）`;
    selectedFields.innerHTML = selected.map(field => {
      const required = requiredFieldIds.includes(field.id);
      return `<li class="field-modal-selected-item"><span class="field-drag-handle" aria-hidden="true">☷</span><span class="field-selected-label" title="${escaped(field.label)}">${escaped(field.label)}</span>${required ? '<span class="field-required">固定</span>' : `<button type="button" data-remove-field-id="${escaped(field.id)}" aria-label="移除${escaped(field.label)}">×</button>`}</li>`;
    }).join("");
  }

  function openFieldDialog() {
    state.draftFieldIds = orderedVisibleFieldIds(state.visibleFieldIds);
    state.fieldSearch = "";
    state.activeFieldGroup = fieldGroupNames[0] || "";
    fieldSearch.value = "";
    fieldModal.hidden = false;
    document.body.classList.add("modal-open");
    renderFieldDialog();
    fieldSearch.focus();
  }

  function closeFieldDialog() {
    fieldModal.hidden = true;
    state.draftFieldIds = null;
    state.fieldSearch = "";
    document.body.classList.remove("modal-open");
  }

  function confirmFieldDialog() {
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
  fieldModal.addEventListener("click", event => {
    const action = event.target.closest("[data-field-action]")?.dataset.fieldAction;
    if (action === "cancel") closeFieldDialog();
    if (action === "confirm") confirmFieldDialog();
    if (action === "reset") {
      state.draftFieldIds = orderedVisibleFieldIds(defaultFieldIds);
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
    if (removeId && !requiredFieldIds.includes(removeId)) {
      state.draftFieldIds = orderedVisibleFieldIds(state.draftFieldIds.filter(id => id !== removeId));
      renderFieldDialog();
    }
  });
  fieldModal.addEventListener("change", event => {
    const input = event.target.closest("input[data-field-id]");
    if (!input) return;
    const selected = input.checked ? [...state.draftFieldIds, input.dataset.fieldId] : state.draftFieldIds.filter(id => id !== input.dataset.fieldId);
    state.draftFieldIds = orderedVisibleFieldIds(selected);
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
  });
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


def write_overview(run_dir, output_dir, registry_path):
    model = build_overview_data(run_dir, registry_path)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    data_path = output_path / "overview-data.json"
    html_path = output_path / "index.html"
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
