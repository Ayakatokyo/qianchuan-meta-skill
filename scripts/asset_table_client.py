import os
import re

import requests


TABLE_NAME = "ods_api_jlqc_uni_overall_roi_live_video_info_du"
_TABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AssetTableClientError(RuntimeError):
    pass


class AssetTableClient:
    def __init__(self, base_url, session_id, session=None, timeout=15):
        self.base_url = str(base_url or "").rstrip("/")
        self.session_id = str(session_id or "").strip()
        if not self.base_url or not self.session_id:
            raise AssetTableClientError("ENV_BACKEND_HOST and YCSESSIONID are required")
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method, path, **kwargs):
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                headers={"Accept": "application/json", "User-Agent": "qianchuan-meta-skill/1.0"},
                cookies={"YCSESSIONID": self.session_id},
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AssetTableClientError("asset table request failed") from exc
        if not isinstance(payload, dict) or str(payload.get("code")) != "200" or "data" not in payload:
            raise AssetTableClientError("asset table returned an invalid response")
        return payload["data"]

    def discover_table(self, table_name=TABLE_NAME):
        results = self._request("GET", "/search/searchAssetNodeByName", params={"retrievalWord": table_name, "limit": 20})
        if not isinstance(results, list):
            raise AssetTableClientError("asset table search result is invalid")
        table_id = next((item.get("objId") for item in results if isinstance(item, dict) and item.get("tableName") == table_name), None)
        if table_id is None:
            raise AssetTableClientError("asset table was not found")
        detail = self._request("POST", "/worktable/loadWorkTableDetail", params={"tableId": table_id})
        physical_table = detail.get("tableName") if isinstance(detail, dict) else None
        if not isinstance(physical_table, str) or not _TABLE_PATTERN.fullmatch(physical_table):
            raise AssetTableClientError("asset table physical name is invalid")
        return physical_table

    def iter_origin_rows(self, physical_table, start_date, end_date, advertiser_id):
        if not _TABLE_PATTERN.fullmatch(physical_table):
            raise AssetTableClientError("asset table physical name is invalid")
        if not str(advertiser_id).isdigit():
            raise AssetTableClientError("advertiser ID is invalid")
        offset = 0
        while True:
            sql = (
                "SELECT origin_data FROM {table} WHERE dt >= '{start}' AND dt <= '{end}' "
                "AND adv_id = '{advertiser}' ORDER BY origin_data LIMIT 2000 OFFSET {offset}"
            ).format(table=physical_table, start=start_date, end=end_date, advertiser=advertiser_id, offset=offset)
            payload = self._request("POST", "/analysis/previewDynamicSqlData", json={"dynamicParameterList": [], "sql": sql, "pageSize": 2000})
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise AssetTableClientError("asset table query rows are invalid")
            for row in rows:
                values = row.get("origs") if isinstance(row, dict) else None
                if not isinstance(values, list) or not values or not isinstance(values[0], str):
                    raise AssetTableClientError("asset table row is invalid")
                yield values[0]
            if len(rows) < 2000:
                return
            offset += 2000


def client_from_environment(environ=None):
    environ = os.environ if environ is None else environ
    return AssetTableClient(environ.get("ENV_BACKEND_HOST"), environ.get("YCSESSIONID") or environ.get("YUCE_AUTHORIZATION"))
