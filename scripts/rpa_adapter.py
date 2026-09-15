"""Bounded, redacted adapter for Qianchuan material-diagnosis RPA."""

import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from path_safety import artifact_path, validate_material_id
from rpa_csv_parser import RpaCsvError, parse_diagnosis_csv
import rpa_dynamic_client as dynamic_rpa


DEFAULT_CSV_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_VIDEO_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_DEADLINE_SEC = 600
_SENSITIVE_KEY = re.compile(r"authorization|cookie|session|secret|token", re.I)


class RpaAdapterError(RuntimeError):
    def __init__(self, message, error_type="rpa_gateway_client_error"):
        super().__init__(message)
        self.error_type = error_type


def runtime_context(environ=None):
    environment = os.environ if environ is None else environ
    api_base = str(environment.get("ENV_BACKEND_HOST", "")).strip().rstrip("/")
    authorization = str(environment.get("YUCE_AUTHORIZATION", "")).strip()
    # The report connector accepts a gateway authorization token as the final
    # session fallback.  Keep RPA on the same runtime contract: local Codex
    # provides only YUCE_AUTHORIZATION in some execution surfaces, while the
    # hosted runner also injects a dedicated session value.
    agent_session = str(
        environment.get("YUCE_SESSION_ID")
        or environment.get("YCSESSIONID")
        or authorization
    ).strip()
    cookie_session = str(environment.get("YCSESSIONID") or authorization).strip()
    missing = [label for label, value in (("ENV_BACKEND_HOST", api_base), ("YUCE_AUTHORIZATION", authorization), ("agent session", agent_session)) if not value]
    if missing:
        raise RpaAdapterError("missing RPA runtime environment: " + ", ".join(missing))
    return {"apiBase": api_base, "authorization": authorization, "agentSession": agent_session, "cookieSession": cookie_session}


def redact_gateway_payload(value):
    if isinstance(value, list):
        return [redact_gateway_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if _SENSITIVE_KEY.search(str(key)):
            result[key] = "[redacted]"
        elif key == "fileUrl" and isinstance(item, str):
            parsed = urlsplit(item)
            result[key] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        else:
            result[key] = redact_gateway_payload(item)
    return result


def select_rpa_account(payload, platform):
    accounts = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(accounts, dict):
        accounts = accounts.get("accounts") or accounts.get("list")
    if not isinstance(accounts, list):
        accounts = []
    matching = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        platform = item.get("platform")
        account_id = str(
            item.get("accountId")
            or item.get("account_id")
            or item.get("id")
            or item.get("shop_id")
            or item.get("shopId")
            or ""
        ).strip()
        if account_id and (item.get("platform") in (None, "", platform)):
            matching.append({"accountId": account_id, "platform": item.get("platform") or platform})
    if not matching:
        raise RpaAdapterError("rpa_account_unavailable: no authorized RPA account", "rpa_account_unavailable")
    unique = {(item["accountId"], item["platform"]) for item in matching}
    if len(unique) != 1:
        raise RpaAdapterError("rpa_account_ambiguous: multiple authorized RPA accounts", "rpa_account_ambiguous")
    selected = matching[0]
    selected["rpaShopId"] = selected["accountId"]
    return selected


def build_diagnosis_params(material_id, start_date, end_date):
    try:
        material_id = validate_material_id(material_id)
    except ValueError as exc:
        raise RpaAdapterError("candidate_pool_invalid: %s" % exc, "candidate_pool_invalid") from exc
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(start_date)) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(end_date)):
        raise RpaAdapterError("RPA dates must use YYYY-MM-DD")
    if end_date < start_date:
        raise RpaAdapterError("RPA end date cannot precede start date")
    return {"material_id": material_id, "date_range_type": "CUSTOM", "custom_start_date": str(start_date), "custom_end_date": str(end_date)}


def download_file(url, target_path, *, session=None, timeout=300, max_bytes=DEFAULT_CSV_MAX_BYTES, deadline_sec=DEFAULT_DOWNLOAD_DEADLINE_SEC):
    session = requests.Session() if session is None else session
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent))
    response = None
    try:
        response = session.get(url, stream=True, timeout=timeout)
        response.raise_for_status()
        content_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
        if content_length is not None and int(content_length) > max_bytes:
            raise RpaAdapterError("downloaded RPA artifact exceeds maximum size")
        started = time.monotonic()
        written = 0
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if time.monotonic() - started > deadline_sec:
                    raise RpaAdapterError("RPA artifact download exceeded total deadline")
                if chunk:
                    written += len(chunk)
                    if written > max_bytes:
                        raise RpaAdapterError("downloaded RPA artifact exceeds maximum size")
                    handle.write(chunk)
        if written == 0:
            raise RpaAdapterError("downloaded RPA artifact is empty")
        os.replace(temporary, target)
        return target
    except (OSError, ValueError, requests.RequestException) as exc:
        if isinstance(exc, RpaAdapterError):
            raise
        raise RpaAdapterError("unable to download RPA artifact") from exc
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        if response is not None and callable(getattr(response, "close", None)):
            response.close()


def _gateway_json(response, action):
    try:
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RpaAdapterError(f"unable to {action}") from exc
    if not isinstance(body, dict) or body.get("success") is not True:
        raise RpaAdapterError(f"{action} returned an unsuccessful result")
    return body


def fetch_connector_detail(*, environ=None, session=None):
    context = runtime_context(environ)
    session = requests.Session() if session is None else session
    response = session.post(
        context["apiBase"] + dynamic_rpa.DETAIL_PATH,
        json={"connectorCode": dynamic_rpa.CONNECTOR_CODE},
        headers={"Content-Type": "application/json;charset=UTF-8"},
        cookies={"YCSESSIONID": context["cookieSession"]},
        timeout=30,
    )
    body = _gateway_json(response, "fetch RPA connector detail")
    data = body.get("data")
    if not isinstance(data, list):
        raise RpaAdapterError("RPA connector detail data must be a list")
    matches = [
        item for item in data
        if isinstance(item, dict) and item.get("connectorCode") == dynamic_rpa.CONNECTOR_CODE
    ]
    if not matches:
        raise RpaAdapterError(
            "RPA connector is unavailable, offline, or unsupported for BIZ_EXPERT"
        )
    if len(matches) != 1:
        raise RpaAdapterError("RPA connector detail is ambiguous")
    try:
        dynamic_rpa.normalized_detail(matches[0])
    except dynamic_rpa.ContractError as exc:
        raise RpaAdapterError(str(exc)) from exc
    return matches[0]


def list_rpa_accounts(*, environ=None, session=None):
    context = runtime_context(environ)
    session = requests.Session() if session is None else session
    detail = fetch_connector_detail(environ=environ, session=session)
    try:
        platform = dynamic_rpa.resolve_platform(detail)
    except dynamic_rpa.ContractError as exc:
        raise RpaAdapterError(str(exc)) from exc
    try:
        response = session.post(context["apiBase"] + "/adg/v1/agent/rpa/authorizations/query", json={"authorization": context["authorization"], "platform": platform}, headers={"Content-Type": "application/json"}, cookies={"YCSESSIONID": context["cookieSession"]}, timeout=30)
        return select_rpa_account(_gateway_json(response, "list RPA accounts"), platform)
    except RpaAdapterError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise RpaAdapterError("unable to list RPA accounts") from exc


def fetch_diagnosis(*, shop_id, rpa_account_id, material_id, start_date, end_date, csv_dir, environ=None, session=None, timeout_sec=3600, csv_max_bytes=DEFAULT_CSV_MAX_BYTES, download_deadline_sec=DEFAULT_DOWNLOAD_DEADLINE_SEC):
    if not str(shop_id or "").strip() or not str(rpa_account_id or "").strip():
        raise RpaAdapterError("shop ID and RPA account ID are required")
    context = runtime_context(environ)
    session = requests.Session() if session is None else session
    params = build_diagnosis_params(material_id, start_date, end_date)
    try:
        detail = fetch_connector_detail(environ=environ, session=session)
        dynamic_rpa.validate_business_params(detail, params)
        platform = dynamic_rpa.resolve_platform(detail)
        submit_payload = {
            "authorization": context["authorization"],
            "agent_session_id": context["agentSession"],
            "data_source_type": dynamic_rpa.DATA_SOURCE_TYPE,
            "platform": platform,
            "function_code": dynamic_rpa.CONNECTOR_CODE,
            "business_params": params,
            "shop_id": str(shop_id).strip(),
        }
        response = session.post(
            context["apiBase"] + dynamic_rpa.FETCH_SUBMIT_PATH,
            json=submit_payload,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            cookies={"YCSESSIONID": context["cookieSession"]},
            timeout=min(30, timeout_sec),
        )
        submit_data = _gateway_json(response, "submit RPA fetch task").get("data")
        task_group_id = submit_data.get("task_group_id") if isinstance(submit_data, dict) else None
        if not isinstance(task_group_id, str) or not task_group_id:
            raise RpaAdapterError("RPA fetch task response has no task_group_id")
        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpaAdapterError(f"RPA fetch task timed out: {task_group_id}")
            time.sleep(min(dynamic_rpa.POLL_INTERVAL_SECONDS, remaining))
            response = session.post(
                context["apiBase"] + dynamic_rpa.FETCH_STATUS_PATH,
                json={"authorization": context["authorization"], "task_group_id": task_group_id},
                headers={"Content-Type": "application/json;charset=UTF-8"},
                cookies={"YCSESSIONID": context["cookieSession"]},
                timeout=min(30, max(deadline - time.monotonic(), 0.001)),
            )
            result = _gateway_json(response, "query RPA fetch task").get("data")
            if not isinstance(result, dict):
                raise RpaAdapterError("RPA fetch task status has no data object")
            result.setdefault("task_group_id", task_group_id)
            status = str(result.get("status") or "").lower()
            if status in dynamic_rpa.SUCCESS_STATUSES:
                break
            if status == "failed":
                raise RpaAdapterError(f"RPA fetch task failed: {task_group_id}")
            if status not in dynamic_rpa.RUNNING_STATUSES:
                raise RpaAdapterError(f"RPA fetch task returned unknown status: {status}")
    except (dynamic_rpa.ContractError, requests.RequestException, ValueError) as exc:
        raise RpaAdapterError("unable to fetch RPA material diagnosis") from exc
    records = result.get("payload") or result.get("records")
    if isinstance(records, list):
        return records, None, redact_gateway_payload(result)
    files = list(dynamic_rpa._iter_file_objects(result))
    if files:
        try:
            url = dynamic_rpa._file_url(files[0])
        except dynamic_rpa.ContractError as exc:
            raise RpaAdapterError("rpa_csv_artifact_unavailable: gateway response has no CSV") from exc
        csv_path = artifact_path(csv_dir, material_id, ".csv")
        download_file(url, csv_path, session=session, max_bytes=csv_max_bytes, deadline_sec=download_deadline_sec)
        try:
            return parse_diagnosis_csv(csv_path), csv_path, redact_gateway_payload(result)
        except RpaCsvError as exc:
            raise RpaAdapterError("rpa_csv_invalid: %s" % exc, "rpa_csv_invalid") from exc
    raise RpaAdapterError("rpa_records_invalid: gateway result has no records or downloadable CSV")


def fetch_diagnosis_worker(params):
    """Picklable isolated-process entrypoint used by the CLI RPA pipeline."""
    records, csv_path, gateway_receipt = fetch_diagnosis(
        shop_id=params["shopId"],
        rpa_account_id=params["rpaAccountId"],
        material_id=params["materialId"],
        start_date=params["startDate"],
        end_date=params["endDate"],
        csv_dir=Path(params["csvDir"]),
        timeout_sec=params["gatewayTimeoutSec"],
        csv_max_bytes=params["csvMaxBytes"],
        download_deadline_sec=params["downloadDeadlineSec"],
    )
    return {"records": records, "csvPath": str(csv_path) if csv_path else None, "gatewayReceipt": gateway_receipt}
