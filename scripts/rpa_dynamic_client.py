#!/usr/bin/env python3
"""Runtime template for connector-code-only YUCE RPA fetch skills."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

# workspace 根目录 = bash_execute_safe 的工作目录（cwd 已锁定为 workspace root）
workspace_root = Path.cwd()

# 导入同目录模块（如 wanxiangtai_api 等）
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from runtime_paths import RuntimePathError, external_runtime_path


CONNECTOR_CODE = 'rpa.conn.juliang.qc.material.analysis.video.live.diagnosis'
DATA_SOURCE_TYPE = "rpa"

DETAIL_PATH = "/dcConnector/v3/listByConnectorCode"
AUTHORIZATIONS_PATH = "/adg/v1/agent/rpa/authorizations/query"
FETCH_SUBMIT_PATH = "/adg/v1/agent/fetch/tasks"
FETCH_STATUS_PATH = "/adg/v1/agent/fetch/tasks/status"

HTTP_REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 3600
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 3600
RUNNING_STATUSES = {"pending", "running"}
SUCCESS_STATUSES = {"completed", "partial_success"}
FILE_URL_KEYS = ("fileUrl", "file_url", "downloadUrl", "download_url", "url")


class ContractError(RuntimeError):
    """Connector metadata or dynamic input contract is unusable."""


class ValidationContractError(ContractError):
    """Business parameters do not satisfy the connector contract."""


def runtime_config(require_session: bool = False) -> dict[str, Any]:
    required = ["ENV_BACKEND_HOST", "YUCE_AUTHORIZATION"]
    missing = [name for name in required if not os.environ.get(name)]
    session_id = os.environ.get("YUCE_SESSION_ID") or os.environ.get("YCSESSIONID")
    if require_session and not session_id:
        missing.append("YUCE_SESSION_ID or YCSESSIONID")
    if missing:
        raise ContractError("缺少运行时环境变量：" + ", ".join(missing))
    authorization = os.environ["YUCE_AUTHORIZATION"]
    # YUCE_SESSION_ID identifies the agent run; it is not a gateway session
    # cookie.  Keep the CLI client aligned with the RPA and report clients.
    cookie_session = os.environ.get("YCSESSIONID") or authorization
    return {
        "api_base": os.environ["ENV_BACKEND_HOST"].rstrip("/"),
        "authorization": authorization,
        "session_id": session_id,
        "headers": {
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
        },
        "cookies": {"YCSESSIONID": cookie_session},
    }


def _request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = HTTP_REQUEST_TIMEOUT_SECONDS,
    require_session: bool = False,
) -> dict[str, Any]:
    auth = runtime_config(require_session=require_session)
    response = requests.request(
        method,
        f"{auth['api_base']}{path}",
        json=payload,
        headers=auth["headers"],
        cookies=auth["cookies"],
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise ContractError(f"接口 {path} 返回非 JSON 内容") from exc
    if not isinstance(body, dict):
        raise ContractError(f"接口 {path} 返回值不是 JSON 对象")
    if body.get("success") is not True:
        message = body.get("msg") or body.get("message") or "未知错误"
        raise ContractError(f"接口 {path} 调用失败：{message}")
    return body


def _connector_code() -> str:
    code = CONNECTOR_CODE.strip()
    if not code or code == "__CONNECTOR_CODE__":
        raise ContractError("客户端尚未写入有效 CONNECTOR_CODE")
    return code


def fetch_connector_detail() -> dict[str, Any]:
    code = _connector_code()
    body = _request_json("POST", DETAIL_PATH, payload={"connectorCode": code})
    data = body.get("data")
    if not isinstance(data, list):
        raise ContractError("连接器详情响应的 data 不是数组")
    matches = [item for item in data if isinstance(item, dict) and item.get("connectorCode") == code]
    if not matches:
        raise ContractError(
            f"连接器 {code} 无可用详情：可能无匹配、未上线或不支持 BIZ_EXPERT"
        )
    if len(matches) != 1:
        ids = [str(item.get("id") or "") for item in matches]
        raise ContractError(f"连接器 {code} 返回 {len(matches)} 条详情，无法唯一选择：{ids}")
    return matches[0]


def parse_schema_text(detail: dict[str, Any]) -> dict[str, Any] | None:
    text = detail.get("inParamJsonSchemaText")
    if text is None:
        return None
    if not isinstance(text, str) or not text.strip():
        raise ContractError("inParamJsonSchemaText 非 null 但不是有效 JSON Schema 原文")
    try:
        schema = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"连接器 JSON Schema 原文解析失败：{exc}") from exc
    if not isinstance(schema, dict):
        raise ContractError("连接器 JSON Schema 顶层必须是对象")
    return schema


def resolve_platform(detail: dict[str, Any]) -> str:
    platform_code = detail.get("platformCode")
    if not isinstance(platform_code, str) or not platform_code.strip():
        raise ContractError("连接器详情缺少有效 platformCode")
    return platform_code.strip()


def normalized_detail(detail: dict[str, Any]) -> dict[str, Any]:
    schema = parse_schema_text(detail)
    input_mode = "schema" if schema is not None else "list"
    in_params = detail.get("inParamList") if schema is None else None
    if schema is None and not isinstance(in_params, list):
        raise ContractError("列表模式下 inParamList 必须是数组")
    return {
        "connectorCode": detail.get("connectorCode"),
        "name": detail.get("name"),
        "platformCode": detail.get("platformCode"),
        "platform": resolve_platform(detail),
        "connectionTypeList": detail.get("connectionTypeList") or [],
        "inputMode": input_mode,
        "inputSchema": schema,
        "inputParams": in_params,
        "outputParams": detail.get("outParamList") or [],
    }


def _format_json_path(parts: Iterable[Any]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(part)):
            path += f".{part}"
        else:
            path += f"[{json.dumps(str(part), ensure_ascii=False)}]"
    return path


def validate_schema_params(schema: dict[str, Any], params: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from jsonschema.exceptions import SchemaError
    except ImportError as exc:
        raise ContractError("Schema 模式需要 jsonschema>=4.18") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ContractError(f"连接器 JSON Schema 配置无效：{exc.message}") from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(params),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if not errors:
        return
    lines = []
    for error in errors:
        data_path = _format_json_path(error.absolute_path)
        schema_path = _format_json_path(error.absolute_schema_path)
        lines.append(f"- {data_path}: {error.message} (schema: {schema_path})")
    raise ValidationContractError("业务入参不符合 JSON Schema：\n" + "\n".join(lines))


def _type_matches(data_type: str, value: Any) -> bool:
    normalized = data_type.strip().upper().replace("-", "_")
    if normalized in {"STRING", "STR", "TEXT", "DATE", "DATETIME"}:
        return isinstance(value, str)
    if normalized in {"INTEGER", "INT", "LONG"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized in {"NUMBER", "FLOAT", "DOUBLE", "DECIMAL"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized in {"BOOLEAN", "BOOL"}:
        return isinstance(value, bool)
    if normalized in {"ARRAY", "LIST"}:
        return isinstance(value, list)
    if normalized in {"OBJECT", "MAP", "JSON"}:
        return isinstance(value, dict)
    return True


def validate_list_params(in_param_list: list[Any], params: dict[str, Any]) -> None:
    definitions = {
        str(item.get("param")): item
        for item in in_param_list
        if isinstance(item, dict) and item.get("param")
    }
    unknown = sorted(set(params) - set(definitions))
    errors = []
    if unknown:
        errors.append(f"未知字段：{unknown}")
    for name, definition in definitions.items():
        value = params.get(name)
        if definition.get("isRequired") is True and (name not in params or value is None or value == ""):
            errors.append(f"{name} 为必填字段")
            continue
        if name not in params or value is None:
            continue
        data_type = str(definition.get("dataTypeEnum") or "")
        if data_type and not _type_matches(data_type, value):
            errors.append(f"{name} 类型错误，期望 {data_type}，实际 {type(value).__name__}")
        raw_enum = definition.get("valEnumList")
        enum_values = [item.get("val") for item in raw_enum if isinstance(item, dict) and "val" in item] if isinstance(raw_enum, list) else []
        if enum_values:
            invalid_values = [item for item in value if item not in enum_values] if isinstance(value, list) else ([] if value in enum_values else [value])
            if invalid_values:
                errors.append(f"{name} 包含非法枚举值 {invalid_values}；允许值：{enum_values}")
    if errors:
        raise ValidationContractError("业务入参不符合 inParamList：\n- " + "\n- ".join(errors))


def validate_business_params(detail: dict[str, Any], params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValidationContractError("business_params 顶层必须是 JSON 对象")
    schema = parse_schema_text(detail)
    if schema is not None:
        validate_schema_params(schema, params)
    else:
        in_param_list = detail.get("inParamList")
        if not isinstance(in_param_list, list):
            raise ContractError("列表模式下 inParamList 必须是数组")
        validate_list_params(in_param_list, params)
    return params


def load_params(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "params_json", None):
        raw = args.params_json
    elif getattr(args, "params_file", None):
        path = workspace_root / args.params_file
        if not path.is_file():
            raise ValidationContractError(f"参数文件不存在：{args.params_file}")
        raw = path.read_text(encoding="utf-8")
    else:
        raw = "{}"
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationContractError(f"业务入参不是合法 JSON：{exc}") from exc
    if not isinstance(params, dict):
        raise ValidationContractError("业务入参顶层必须是 JSON 对象")
    return params


def list_accounts(detail: dict[str, Any]) -> dict[str, Any]:
    auth = runtime_config()
    platform = resolve_platform(detail)
    return _request_json(
        "POST",
        AUTHORIZATIONS_PATH,
        payload={"authorization": auth["authorization"], "platform": platform},
    )


def _response_data(body: dict[str, Any], operation: str) -> dict[str, Any]:
    data = body.get("data")
    if not isinstance(data, dict):
        raise ContractError(f"{operation}响应缺少 data 对象")
    return data


def _submit_fetch_task(detail: dict[str, Any], shop_id: str, params: dict[str, Any], timeout: float) -> str:
    auth = runtime_config(require_session=True)
    payload = {
        "authorization": auth["authorization"],
        "agent_session_id": auth["session_id"],
        "data_source_type": DATA_SOURCE_TYPE,
        "platform": resolve_platform(detail),
        "function_code": _connector_code(),
        "business_params": params,
        "shop_id": shop_id,
    }
    body = _request_json(
        "POST",
        FETCH_SUBMIT_PATH,
        payload=payload,
        timeout=timeout,
        require_session=True,
    )
    data = _response_data(body, "RPA 取数任务下发")
    task_group_id = data.get("task_group_id")
    if not isinstance(task_group_id, str) or not task_group_id:
        raise ContractError("RPA 取数任务下发响应缺少 task_group_id")
    return task_group_id


def _query_fetch_task(task_group_id: str, timeout: float) -> dict[str, Any]:
    auth = runtime_config()
    body = _request_json(
        "POST",
        FETCH_STATUS_PATH,
        payload={"authorization": auth["authorization"], "task_group_id": task_group_id},
        timeout=timeout,
    )
    return _response_data(body, "RPA 取数任务状态查询")


def invoke(detail: dict[str, Any], shop_id: str, params: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    remaining = deadline - time.monotonic()
    task_group_id = _submit_fetch_task(detail, shop_id, params, min(HTTP_REQUEST_TIMEOUT_SECONDS, remaining))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"RPA 取数超过 {POLL_TIMEOUT_SECONDS} 秒（task_group_id={task_group_id}）")
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"RPA 取数超过 {POLL_TIMEOUT_SECONDS} 秒（task_group_id={task_group_id}）")
        result = dict(_query_fetch_task(task_group_id, min(HTTP_REQUEST_TIMEOUT_SECONDS, remaining)))
        result.setdefault("task_group_id", task_group_id)
        status = result.get("status")
        if status in SUCCESS_STATUSES:
            return result
        if status == "failed":
            raise ContractError(
                f"RPA 取数任务失败（task_group_id={task_group_id}）："
                + json.dumps(result.get("errors") or [], ensure_ascii=False)
            )
        if status not in RUNNING_STATUSES:
            raise ContractError(f"RPA 取数任务返回未知状态 {status!r}（task_group_id={task_group_id}）")


def _iter_file_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if any(isinstance(value.get(key), str) and value.get(key) for key in FILE_URL_KEYS):
            yield value
        for child in value.values():
            yield from _iter_file_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_file_objects(child)


def _file_url(file_item: dict[str, Any]) -> str:
    for key in FILE_URL_KEYS:
        value = file_item.get(key)
        if isinstance(value, str) and value:
            return value
    raise ContractError("文件对象缺少下载 URL")


def _safe_filename(file_item: dict[str, Any], url: str, index: int) -> str:
    candidate = str(file_item.get("fileName") or file_item.get("name") or "").strip()
    if not candidate:
        candidate = Path(urlparse(url).path).name
    if not candidate:
        candidate = f"result-{index}.csv"
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate)
    return candidate or f"result-{index}.csv"


def parse_csv_records(text: str) -> list[Any]:
    normalized = text.lstrip("\ufeff")
    rows = [row for row in csv.reader(io.StringIO(normalized)) if row and any(cell.strip() for cell in row)]
    if not rows:
        return []
    parsed_single = []
    single_column_json = all(len(row) == 1 for row in rows)
    if single_column_json:
        for row in rows:
            try:
                parsed_single.append(json.loads(row[0]))
            except json.JSONDecodeError:
                single_column_json = False
                break
    if single_column_json:
        flattened = []
        for value in parsed_single:
            if isinstance(value, list):
                flattened.extend(value)
            else:
                flattened.append(value)
        return flattened
    reader = csv.DictReader(io.StringIO(normalized))
    if not reader.fieldnames:
        return rows
    return [dict(row) for row in reader]


def _actual_fields(records: list[Any]) -> list[str]:
    fields = set()
    for record in records:
        if isinstance(record, dict):
            fields.update(str(key) for key in record)
    return sorted(fields)


def download_and_parse_files(
    detail: dict[str, Any],
    result: dict[str, Any],
    output_dir: Path,
    preview_limit: int,
) -> list[dict[str, Any]]:
    auth = runtime_config()
    files = list(_iter_file_objects(result))
    if not files:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_fields = {
        str(item.get("fieldName"))
        for item in detail.get("outParamList") or []
        if isinstance(item, dict) and item.get("fieldName")
    }
    summaries = []
    for index, file_item in enumerate(files, start=1):
        url = _file_url(file_item)
        response = requests.get(url, cookies=auth["cookies"], timeout=DOWNLOAD_TIMEOUT_SECONDS)
        response.raise_for_status()
        filename = _safe_filename(file_item, url, index)
        csv_path = output_dir / filename
        csv_path.write_bytes(response.content)
        try:
            text = response.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            encoding = response.encoding or response.apparent_encoding or "utf-8"
            text = response.content.decode(encoding, errors="replace")
        records = parse_csv_records(text)
        records_path = output_dir / f"{csv_path.stem}.records.json"
        records_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        actual_fields = set(_actual_fields(records))
        summaries.append(
            {
                "sourceFile": str(csv_path.relative_to(workspace_root)),
                "recordsFile": str(records_path.relative_to(workspace_root)),
                "recordCount": len(records),
                "actualFields": sorted(actual_fields),
                "expectedFields": sorted(expected_fields),
                "missingExpectedFields": sorted(expected_fields - actual_fields),
                "unexpectedFields": sorted(actual_fields - expected_fields) if expected_fields else [],
                "preview": records[:preview_limit],
            }
        )
    return summaries


def cmd_detail(_args: argparse.Namespace) -> None:
    print(json.dumps(normalized_detail(fetch_connector_detail()), ensure_ascii=False, indent=2))


def cmd_schema(_args: argparse.Namespace) -> None:
    detail = fetch_connector_detail()
    normalized = normalized_detail(detail)
    output = {
        "connectorCode": normalized["connectorCode"],
        "name": normalized["name"],
        "inputMode": normalized["inputMode"],
        "schema": normalized["inputSchema"],
        "inParamList": normalized["inputParams"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_validate(args: argparse.Namespace) -> None:
    detail = fetch_connector_detail()
    params = validate_business_params(detail, load_params(args))
    print(json.dumps({"valid": True, "connectorCode": _connector_code(), "businessParams": params}, ensure_ascii=False, indent=2))


def cmd_list_accounts(_args: argparse.Namespace) -> None:
    detail = fetch_connector_detail()
    print(json.dumps(list_accounts(detail), ensure_ascii=False, indent=2))


def cmd_fetch(args: argparse.Namespace) -> None:
    detail = fetch_connector_detail()
    params = validate_business_params(detail, load_params(args))
    result = invoke(detail, args.shop_id, params)
    output_dir = external_runtime_path(
        workspace_root / args.output_dir,
        label="RPA output directory",
    )
    file_summaries = [] if args.no_download else download_and_parse_files(detail, result, output_dir, args.preview_limit)
    print(
        json.dumps(
            {
                "connectorCode": _connector_code(),
                "connectorName": detail.get("name"),
                "status": result.get("status"),
                "taskGroupId": result.get("task_group_id"),
                "errors": result.get("errors") or [],
                "files": file_summaries,
                "rawResult": result if args.include_raw_result else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_self_test(_args: argparse.Namespace) -> None:
    schema_detail = {
        "inParamJsonSchemaText": json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"date": {"type": "string"}},
                "required": ["date"],
                "additionalProperties": False,
            }
        )
    }
    validate_business_params(schema_detail, {"date": "2026-09-04"})
    try:
        validate_business_params(schema_detail, {})
    except ValidationContractError:
        pass
    else:
        raise AssertionError("schema invalid sample unexpectedly passed")
    list_detail = {
        "inParamJsonSchemaText": None,
        "inParamList": [
            {"param": "limit", "isRequired": True, "dataTypeEnum": "INTEGER"},
            {
                "param": "status",
                "isRequired": False,
                "dataTypeEnum": "STRING",
                "valEnumList": [{"val": "OPEN"}, {"val": "CLOSED"}],
            },
        ],
    }
    validate_business_params(list_detail, {"limit": 10, "status": "OPEN"})
    for invalid in ({}, {"limit": "10"}, {"limit": 10, "status": "OTHER"}, {"limit": 10, "extra": 1}):
        try:
            validate_business_params(list_detail, invalid)
        except ValidationContractError:
            pass
        else:
            raise AssertionError(f"list invalid sample unexpectedly passed: {invalid}")
    assert parse_csv_records('"{""id"":1}"\n"{""id"":2}"\n') == [{"id": 1}, {"id": 2}]
    assert parse_csv_records("id,name\n1,A\n") == [{"id": "1", "name": "A"}]
    assert resolve_platform({"platformCode": "RPA_DOUDIAN", "connectionTypeList": []}) == "RPA_DOUDIAN"
    assert resolve_platform({"platformCode": "DOUDIAN", "connectionTypeList": []}) == "DOUDIAN"
    print("self-test passed")


def add_params_source(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--params-json", help="完整 business_params JSON 对象")
    group.add_argument("--params-file", help="相对 workspace 根的 JSON 文件路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="预策 RPA 动态参数客户端")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("detail", help="按 connectorCode 获取并规范化连接器详情").set_defaults(func=cmd_detail)
    subparsers.add_parser("schema", help="打印实时 JSON Schema 或 inParamList").set_defaults(func=cmd_schema)
    subparsers.add_parser("list-accounts", help="按详情返回的 platformCode 列出 RPA 授权账号").set_defaults(func=cmd_list_accounts)

    validate_parser = subparsers.add_parser("validate", help="按实时连接器契约校验业务 JSON")
    add_params_source(validate_parser)
    validate_parser.set_defaults(func=cmd_validate)

    fetch_parser = subparsers.add_parser("fetch", help="校验、异步取数、下载并解析 CSV")
    fetch_parser.add_argument("--shop-id", required=True, help="RPA 授权账号对应的 shop_id")
    add_params_source(fetch_parser)
    fetch_parser.add_argument("--output-dir", required=True, help="输出目录；必须位于技能安装目录之外")
    fetch_parser.add_argument("--preview-limit", type=int, default=5, help="每个文件输出的记录预览条数")
    fetch_parser.add_argument("--no-download", action="store_true", help="仅返回终态，不下载文件")
    fetch_parser.add_argument("--include-raw-result", action="store_true", help="在输出中保留完整终态结果")
    fetch_parser.set_defaults(func=cmd_fetch)

    subparsers.add_parser("self-test", help="离线检查校验、平台透传和 CSV 解析").set_defaults(func=cmd_self_test)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
        return 0
    except (ValidationContractError, RuntimePathError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ContractError, requests.RequestException, OSError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
