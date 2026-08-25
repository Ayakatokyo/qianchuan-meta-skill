"""Fetch normalized Qianchuan live-video material report records."""

import csv
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit

import requests


FUNCTION_CODE = "OCEANENGINE_UNI_PROMOTION_PLAN_DATA_REQUESTS_ZDY"
DATA_TOPIC = "OVERALL_ROI_LIVE_MATERIAL_VIDEO"
DIMENSIONS = [
    "material_id",
    "anchor_id",
    "roi2_material_video_name",
    "roi2_material_type_v3",
    "roi2_material_video_type",
]
METRICS = [
    "stat_cost_for_overall_roi2",
    "basic_stat_cost_for_roi2_v2",
    "total_pay_order_count_for_roi2",
    "total_order_settle_count_for_roi2_1h",
    "total_order_settle_amount_for_roi2_1h",
    "total_pay_order_gmv_include_coupon_for_roi2",
    "total_refund_order_count_for_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_all",
]
FILTERS = [
    {"field": "ecp_app_id", "operator": 7, "values": ["1", "2"]},
    {"field": "roi2_material_type_v3", "operator": 7, "values": ["3"]},
]
_SENSITIVE_KEY = re.compile(r"(?:authorization|cookie|session|secret|token)", re.I)


class ReportClientError(RuntimeError):
    pass


def _runtime_context(environ):
    api_base = str(environ.get("ENV_BACKEND_HOST", "")).strip().rstrip("/")
    authorization = str(environ.get("YUCE_AUTHORIZATION", "")).strip()
    agent_session = str(environ.get("YUCE_SESSION_ID") or environ.get("YCSESSIONID") or authorization).strip()
    cookie_session = str(environ.get("YCSESSIONID") or authorization).strip()
    missing = [name for name, value in (
        ("ENV_BACKEND_HOST", api_base),
        ("YUCE_AUTHORIZATION", authorization),
        ("agent session", agent_session),
        ("cookie session", cookie_session),
    ) if not value]
    if missing:
        raise ReportClientError("missing report runtime environment: " + ", ".join(missing))
    return {
        "apiBase": api_base,
        "authorization": authorization,
        "agentSession": agent_session,
        "cookieSession": cookie_session,
    }


def _business_params(advertiser_id, start_date, end_date, *, order_by=None):
    if not str(advertiser_id).isdigit():
        raise ReportClientError("advertiser ID is invalid")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(start_date)) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(end_date)):
        raise ReportClientError("report dates must use YYYY-MM-DD")
    return {
        "advertiserId": int(advertiser_id),
        "startTime": str(start_date) + " 00:00:00",
        "endTime": str(end_date) + " 23:59:59",
        "dataTopic": DATA_TOPIC,
        "dimensions": list(DIMENSIONS),
        "metrics": list(METRICS),
        "filters": list(FILTERS),
        "orderBy": list(order_by or [{"field": "stat_cost_for_overall_roi2", "type": 2}]),
    }


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _redact(value):
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if _SENSITIVE_KEY.search(str(key)):
            result[key] = "[redacted]"
        elif str(key) == "fileUrl" and isinstance(item, str):
            result[key] = _redact_url(item)
        else:
            result[key] = _redact(item)
    return result


def _redact_url(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _validate_records(records):
    if not isinstance(records, list) or not records:
        raise ReportClientError("material report payload must be a non-empty record list")
    if any(not isinstance(record, dict) for record in records):
        raise ReportClientError("material report payload contains a non-object record")
    return records


def _write_stage_status(source_dir, stage, status, **details):
    payload = {"stage": stage, "status": status}
    payload.update(details)
    _write_json(Path(source_dir) / (stage + "-status.json"), payload)


def _records_from_result(result, source_dir, session):
    data = result.get("data")
    if isinstance(data, list):
        _write_stage_status(source_dir, "result-download", "not_required")
        _write_stage_status(source_dir, "result-parse", "not_required")
        return _validate_records(data)
    if not isinstance(data, dict):
        raise ReportClientError("material report response is missing data")
    content = data.get("result_content")
    if data.get("result_type") == "files":
        return _records_from_files(content, source_dir, session)
    if data.get("result_type") == "payload":
        content = content.get("payload") if isinstance(content, dict) else content
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ReportClientError("material report payload is not valid JSON") from exc
    if isinstance(content, dict) and "payload" in content:
        content = content["payload"]
    _write_stage_status(source_dir, "result-download", "not_required")
    _write_stage_status(source_dir, "result-parse", "not_required")
    return _validate_records(content)


def _records_from_files(content, source_dir, session):
    if not isinstance(content, dict) or content.get("status") != "COMPLETED" or content.get("failedInstances", 0) != 0:
        raise ReportClientError("material report files task did not complete")
    files = content.get("files")
    if not isinstance(files, list) or not files or not isinstance(files[0], dict):
        raise ReportClientError("material report files response has no result file")
    file_url = files[0].get("fileUrl")
    if not isinstance(file_url, str) or not file_url.strip():
        raise ReportClientError("material report result file URL is missing")
    filename = Path(urlsplit(file_url).path).name or "material-report.csv"
    raw_path = source_dir / filename
    try:
        _download_file(file_url, raw_path, session)
    except ReportClientError as exc:
        _write_stage_status(
            source_dir, "result-download", "failed", filename=filename, error=str(exc)
        )
        _write_stage_status(source_dir, "result-parse", "not_started")
        raise ReportClientError(
            "material report gateway completed, but result-file download failed; "
            "see result-download-status.json"
        ) from exc
    _write_stage_status(source_dir, "result-download", "completed", filename=filename)
    structured_path = raw_path.with_suffix(raw_path.suffix + ".structured.json")
    records = []
    try:
        with raw_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.reader(handle), start=1):
                if len(row) != 1:
                    raise ReportClientError("material report result file row %s is not single-column JSON" % row_number)
                record = json.loads(row[0])
                if not isinstance(record, dict):
                    raise ReportClientError("material report result file row %s is not an object" % row_number)
                records.append(record)
    except (OSError, UnicodeError, csv.Error, json.JSONDecodeError) as exc:
        _write_stage_status(source_dir, "result-parse", "failed", filename=filename, error=str(exc))
        raise ReportClientError(
            "material report result file could not be structured; see result-parse-status.json"
        ) from exc
    _write_json(structured_path, records)
    _write_stage_status(
        source_dir, "result-parse", "completed", filename=filename, recordCount=len(records)
    )
    return _validate_records(records)


def _download_file(file_url, target_path, session):
    try:
        response = session.get(file_url, stream=True, timeout=300)
        response.raise_for_status()
        staging = target_path.with_name("." + target_path.name + ".tmp")
        with staging.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        if staging.stat().st_size == 0:
            raise ReportClientError("material report result file is empty")
        os.replace(staging, target_path)
    except (OSError, requests.RequestException) as exc:
        raise ReportClientError("material report result file download failed") from exc
    finally:
        close = getattr(locals().get("response"), "close", None)
        if callable(close):
            close()


def _validate_gateway_result(result):
    if not isinstance(result, dict):
        raise ReportClientError("material report gateway returned a non-object response")
    if result.get("success") is False:
        raise ReportClientError("material report gateway returned failure: %s" % (result.get("msg") or result))
    content = result.get("data", {}).get("result_content", {}) if isinstance(result.get("data"), dict) else {}
    if isinstance(content, dict) and content.get("status") == "FAILED":
        failures = content.get("failedErrors") or []
        detail = failures[0].get("errorMsg") if failures and isinstance(failures[0], dict) else None
        raise ReportClientError("material report task failed: %s" % (detail or content))


def fetch_material_report(
    shop_id, advertiser_id, start_date, end_date, *, output_dir, order_by=None, environ=None, session=None
):
    if not str(shop_id or "").strip():
        raise ReportClientError("shop ID is required")
    context = _runtime_context(os.environ if environ is None else environ)
    source_dir = Path(output_dir)
    source_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "authorization": context["authorization"],
        "agent_session_id": context["agentSession"],
        "data_source_type": "api",
        "platform": "OCEANENGINE",
        "function_code": FUNCTION_CODE,
        "response_content_type": "fetch",
        "business_params": _business_params(advertiser_id, start_date, end_date, order_by=order_by),
        "shop_id": str(shop_id).strip(),
    }
    session = requests.Session() if session is None else session
    try:
        response = session.post(
            context["apiBase"] + "/adg/v1/agent/invoke",
            json=payload,
            headers={"Content-Type": "application/json"},
            cookies={"YCSESSIONID": context["cookieSession"]},
            timeout=3600,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ReportClientError("material report request failed") from exc
    _validate_gateway_result(result)
    _write_json(source_dir / "gateway-response.json", _redact(result))
    _write_stage_status(
        source_dir,
        "gateway",
        "completed",
        resultType=result.get("data", {}).get("result_type") if isinstance(result.get("data"), dict) else None,
    )
    return _records_from_result(result, source_dir, session)
