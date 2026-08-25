import re
from datetime import date
from decimal import Decimal, InvalidOperation

from field_registry import FieldRegistryError


class MaterialQueryError(ValueError):
    pass


_DIMENSION_OPERATORS = {"eq", "in", "contains", "regex"}
_METRIC_OPERATORS = {"eq", "gt", "gte", "lt", "lte", "between"}
_AIGC_DYNAMIC_CREATIVE_MATERIAL_ID = "-2"
_AIGC_DYNAMIC_CREATIVE_EXCLUSION_REASON = "aigc_dynamic_creative_collection_not_single_material"


def _text(value):
    return str(value).strip() if value is not None else ""


def _value_at(payload, path):
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _decimal(value, label):
    if isinstance(value, bool) or value is None:
        raise MaterialQueryError("%s must be a finite number" % label)
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MaterialQueryError("%s must be a finite number" % label) from exc
    if not number.is_finite():
        raise MaterialQueryError("%s must be a finite number" % label)
    return number


def _decimal_text(value):
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def _validate_period(value):
    if not isinstance(value, dict):
        raise MaterialQueryError("period must be an object")
    start = _text(value.get("startDate"))
    end = _text(value.get("endDate"))
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise MaterialQueryError("period dates must use YYYY-MM-DD") from exc
    if start_date > end_date:
        raise MaterialQueryError("period startDate must not be after endDate")
    return {"startDate": start, "endDate": end}


def _normalize_filter(raw, registry, expected_kind):
    if not isinstance(raw, dict):
        raise MaterialQueryError("query filter must be an object")
    field_id = _text(raw.get("field"))
    operator = _text(raw.get("operator"))
    try:
        entry = registry.validate_operator(field_id, operator)
    except FieldRegistryError as exc:
        raise MaterialQueryError(str(exc)) from exc
    if entry["kind"] != expected_kind and not (
        expected_kind == "metric" and entry["kind"] in {"atomic_metric", "derived_metric"}
    ):
        raise MaterialQueryError("field %s has the wrong filter type" % field_id)
    allowed = _DIMENSION_OPERATORS if expected_kind == "dimension" else _METRIC_OPERATORS
    if operator not in allowed:
        raise MaterialQueryError("operator %s is not valid for %s" % (operator, expected_kind))
    value = raw.get("value")
    if operator == "in":
        if not isinstance(value, list) or not value:
            raise MaterialQueryError("in filter requires a nonempty list")
        value = [_text(item) for item in value]
    elif operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise MaterialQueryError("between filter requires exactly two values")
        value = [_decimal(value[0], field_id), _decimal(value[1], field_id)]
        if value[0] > value[1]:
            raise MaterialQueryError("between lower value must not exceed upper value")
    elif expected_kind == "metric":
        value = _decimal(value, field_id)
    else:
        value = _text(value)
        if not value:
            raise MaterialQueryError("dimension filter value must be nonempty")
        if operator == "regex":
            try:
                re.compile(value)
            except re.error as exc:
                raise MaterialQueryError("dimension regex is invalid") from exc
    return {"field": field_id, "operator": operator, "value": value}


def parse_query_config(raw, registry):
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        raise MaterialQueryError("query schemaVersion must be 1")
    advertiser_id = _text(raw.get("advertiserId"))
    if not advertiser_id.isdigit():
        raise MaterialQueryError("advertiserId must contain digits only")
    shop_id = _text(raw.get("shopId"))
    if not shop_id:
        raise MaterialQueryError("shopId must be nonempty")
    top_n = raw.get("topN")
    if type(top_n) is not int or top_n <= 0:
        raise MaterialQueryError("topN must be a positive integer")
    dimension_filters = [_normalize_filter(item, registry, "dimension") for item in raw.get("dimensionFilters", [])]
    metric_filters = [_normalize_filter(item, registry, "metric") for item in raw.get("metricFilters", [])]
    sort = raw.get("sort")
    if not isinstance(sort, dict):
        raise MaterialQueryError("sort must be an object")
    field_id = _text(sort.get("field"))
    try:
        entry = registry.require(field_id)
    except FieldRegistryError as exc:
        raise MaterialQueryError(str(exc)) from exc
    if entry["kind"] not in {"atomic_metric", "derived_metric"} or not entry.get("sortable"):
        raise MaterialQueryError("sort field must be a sortable metric")
    direction = _text(sort.get("direction")).lower()
    if direction not in {"asc", "desc"}:
        raise MaterialQueryError("sort direction must be asc or desc")
    return {
        "schemaVersion": 1,
        "period": _validate_period(raw.get("period")),
        "shopId": shop_id,
        "advertiserId": advertiser_id,
        "topN": top_n,
        "dimensionFilters": dimension_filters,
        "metricFilters": metric_filters,
        "sort": {"field": field_id, "direction": direction},
    }


def _matches(value, item):
    operator = item["operator"]
    expected = item["value"]
    if operator == "eq":
        return value == expected
    if operator == "in":
        return value in expected
    if operator == "contains":
        return expected in value
    if operator == "regex":
        return re.search(expected, value) is not None
    if operator == "gt":
        return value > expected
    if operator == "gte":
        return value >= expected
    if operator == "lt":
        return value < expected
    if operator == "lte":
        return value <= expected
    if operator == "between":
        return expected[0] <= value <= expected[1]
    raise MaterialQueryError("unsupported operator")


def _metrics_for_row(row, registry):
    metrics = {}
    for entry in registry.entries():
        if entry["kind"] != "atomic_metric":
            continue
        raw = _value_at(row, entry["source"])
        if raw is not None:
            metrics[entry["id"]] = _decimal(raw, entry["id"])
    return metrics


def _derived_metrics(metrics, registry):
    result = dict(metrics)
    for entry in registry.entries():
        if entry["kind"] != "derived_metric":
            continue
        formula = entry["formula"]
        numerator = result.get(formula["numerator"])
        denominator = result.get(formula["denominator"])
        result[entry["id"]] = None if numerator is None or denominator in (None, Decimal("0")) else numerator / denominator
    return result


def execute_material_query(raw_rows, raw_query, registry):
    query = parse_query_config(raw_query, registry)
    aggregates = {}
    excluded = []
    aigc_source_row_count = 0
    for row in raw_rows:
        if not isinstance(row, dict):
            raise MaterialQueryError("material report record must be an object")
        material_id = _text(_value_at(row, registry.require("material_id")["source"]))
        if not material_id:
            raise MaterialQueryError("material report material ID is missing")
        if material_id == _AIGC_DYNAMIC_CREATIVE_MATERIAL_ID:
            aigc_source_row_count += 1
            continue
        dimensions = {
            entry["id"]: _text(_value_at(row, entry["source"]))
            for entry in registry.entries() if entry["kind"] == "dimension"
        }
        if any(not _matches(dimensions[item["field"]], item) for item in query["dimensionFilters"]):
            excluded.append({"materialId": material_id, "reason": "dimension_filter"})
            continue
        aggregate = aggregates.setdefault(material_id, {"dimensions": dimensions, "metrics": {}})
        for key, value in _metrics_for_row(row, registry).items():
            aggregate["metrics"][key] = aggregate["metrics"].get(key, Decimal("0")) + value

    if aigc_source_row_count:
        excluded.append({
            "materialId": _AIGC_DYNAMIC_CREATIVE_MATERIAL_ID,
            "reason": _AIGC_DYNAMIC_CREATIVE_EXCLUSION_REASON,
            "sourceRowCount": aigc_source_row_count,
        })

    candidates = []
    for material_id, aggregate in sorted(aggregates.items()):
        metrics = _derived_metrics(aggregate["metrics"], registry)
        unavailable = any(metrics.get(item["field"]) is None for item in query["metricFilters"])
        if unavailable or any(not _matches(metrics[item["field"]], item) for item in query["metricFilters"]):
            excluded.append({"materialId": material_id, "reason": "unavailable_metric_value" if unavailable else "metric_filter"})
            continue
        sort_value = metrics.get(query["sort"]["field"])
        if sort_value is None:
            excluded.append({"materialId": material_id, "reason": "unavailable_sort_value"})
            continue
        candidates.append({"materialId": material_id, "dimensions": aggregate["dimensions"], "metrics": metrics, "sortValue": sort_value})
    candidates.sort(key=lambda item: (item["sortValue"], item["materialId"]), reverse=query["sort"]["direction"] == "desc")
    materials = []
    for rank, candidate in enumerate(candidates[:query["topN"]], start=1):
        materials.append({
            "rank": rank,
            "materialId": candidate["materialId"],
            "materialName": candidate["dimensions"].get("material_name", ""),
            "materialType": candidate["dimensions"].get("material_type", ""),
            "videoType": candidate["dimensions"].get("video_type", ""),
            "rankingMetric": query["sort"]["field"],
            "sortValue": _decimal_text(candidate["sortValue"]),
            "metrics": {key: _decimal_text(value) if value is not None else None for key, value in candidate["metrics"].items()},
        })
    receipt = {
        "schemaVersion": 1,
        "registryVersion": registry.version,
        "query": query,
        "sort": query["sort"],
        "candidateCount": len(aggregates),
        "passedCount": len(candidates),
        "returnedCount": len(materials),
        "excluded": excluded,
    }
    return {"materials": materials, "receipt": receipt}
