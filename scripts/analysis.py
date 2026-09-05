"""Strict, offline, source-linked commercial analysis contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any

from fact_pack import file_hash_and_size, verify_fact_pack


class AnalysisError(ValueError):
    """Raised when an analysis request or result violates the contract."""


_HANDOFF_SECTIONS = ("material_leaderboard", "audience", "scripts", "highlight_frames")
_SECTION_ALIASES = {"script": "scripts"}
_RETRIEVAL_OPERATORS = {"eq", "neq", "contains", "in", "gt", "gte", "lt", "lte"}
_FORBIDDEN_RETRIEVAL_KEYS = {"scope", "period", "ranking", "candidateTopN", "shopId", "advertiserId", "rpa", "querySpec", "highlightSpec"}
_RETRIEVAL_KEYS = {
    "schemaVersion", "runId", "factPackManifestSha256", "retrievalGoal", "sections", "materialScope",
    "fields", "filters", "groupComparisons", "calculations", "evidenceStrategy", "retrievalTrace",
} | _FORBIDDEN_RETRIEVAL_KEYS


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"{label} is unreadable") from exc


def load_analysis_registry(path: Path) -> dict[str, Any]:
    payload = _read_json(path, "analysis registry")
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("sections"), dict):
        raise AnalysisError("analysis registry schema is invalid")
    asset_registry_path = Path(path).parent / "asset-field-registry-v1.json"
    if asset_registry_path.is_file():
        asset_registry = _read_json(asset_registry_path, "asset field registry")
        material_schema = payload["sections"].get("material_leaderboard", {})
        explicit_fields = set(material_schema.get("fields", []))
        for entry in asset_registry.get("fields", []):
            if entry.get("kind") == "metric" and isinstance(entry.get("id"), str):
                field = f"metrics.{entry['id']}"
                if field not in explicit_fields:
                    material_schema.setdefault("fields", []).append(field)
        payload["sections"]["material_leaderboard"] = material_schema
    return payload


def load_analysis_capability_registry(path: Path) -> dict[str, Any]:
    """Load business-language dependency hints; values never contain run data."""
    payload = _read_json(path, "analysis capability registry")
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("capabilities"), dict):
        raise AnalysisError("analysis capability registry schema is invalid")
    for name, capability in payload["capabilities"].items():
        if not isinstance(name, str) or not name.strip() or not isinstance(capability, dict):
            raise AnalysisError("analysis capability registry entries are invalid")
        if not isinstance(capability.get("sections"), list) or not capability["sections"]:
            raise AnalysisError(f"capability {name} must declare sections")
        if not all(section in _HANDOFF_SECTIONS for section in capability["sections"]):
            raise AnalysisError(f"capability {name} references unknown section")
        for key in ("fields", "optionalFields", "clarifications"):
            if key in capability and not isinstance(capability[key], list):
                raise AnalysisError(f"capability {name}.{key} must be a list")
    return payload


def _keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise AnalysisError(f"unknown {label} fields: {', '.join(sorted(unknown))}")


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisError(f"{label} must be an object")
    return value


def _manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_handoff_sections(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AnalysisError("analysis handoff sections must be a nonempty list")
    sections = [_SECTION_ALIASES.get(item, item) if isinstance(item, str) else item for item in value]
    if any(section not in _HANDOFF_SECTIONS for section in sections):
        unknown = [str(section) for section in sections if section not in _HANDOFF_SECTIONS]
        raise AnalysisError(f"unknown analysis handoff section: {', '.join(unknown)}")
    if len(set(sections)) != len(sections):
        raise AnalysisError("analysis handoff sections must not contain duplicates")
    return sections


def _normalize_material_scope(scope: Any, material_ids: list[str], target_top_n: Any) -> dict[str, Any]:
    scope = _require_object(scope, "analysis handoff materialScope")
    _keys(scope, {"type", "value"}, "analysis handoff materialScope")
    scope_type = scope.get("type")
    if scope_type == "all":
        if "value" in scope and scope["value"] is not None:
            raise AnalysisError("analysis handoff all materialScope cannot contain a value")
        return {"rankRange": {"start": 1, "end": target_top_n}}
    if scope_type == "top_n":
        value = scope.get("value")
        if type(value) is not int or value <= 0 or value > target_top_n:
            raise AnalysisError("analysis handoff top_n materialScope is outside fact pack")
        return {"rankRange": {"start": 1, "end": value}}
    if scope_type in {"material_ids", "material_id_range"}:
        value = scope.get("value")
        if scope_type == "material_ids":
            if not isinstance(value, list) or not value:
                raise AnalysisError("analysis handoff material_ids scope must be a nonempty list")
            selected = [str(item) for item in value]
        else:
            if isinstance(value, dict):
                _keys(value, {"start", "end"}, "analysis handoff material_id_range")
                start, end = value.get("start"), value.get("end")
            elif isinstance(value, list) and len(value) == 2:
                start, end = value
            else:
                raise AnalysisError("analysis handoff material_id_range must contain start and end")
            if type(start) is not int or type(end) is not int or start <= 0 or end < start:
                raise AnalysisError("analysis handoff material_id_range is invalid")
            selected = material_ids[start - 1:end]
            if len(selected) != end - start + 1:
                raise AnalysisError("analysis handoff material_id_range is outside fact pack")
        unknown = [item for item in selected if item not in material_ids]
        if unknown:
            raise AnalysisError(f"analysis handoff material IDs outside fact pack: {', '.join(unknown)}")
        return {"materialIds": selected}
    raise AnalysisError("analysis handoff materialScope.type is unsupported")


def validate_analysis_handoff(request: dict[str, Any], fact_pack_dir: Path) -> dict[str, Any]:
    """Validate and normalize the stage-two request against an immutable fact pack."""
    request = _require_object(request, "analysis handoff request")
    _keys(
        request,
        {
            "stage", "runId", "factPackManifestSha256", "sections", "materialScope", "requestText", "analysisSpec",
            "selection", "factPackPath", "manifestSummary", "sourceRefs", "factRetrievalSpec",
        },
        "analysis handoff request",
    )
    if request.get("stage") != "analysis":
        raise AnalysisError("analysis handoff stage must be analysis")
    fact_pack_dir = Path(fact_pack_dir).resolve()
    manifest_path = fact_pack_dir / "manifest.json"
    if not manifest_path.is_file():
        raise AnalysisError("fact pack manifest is missing")
    manifest = _read_json(manifest_path, "fact-pack manifest")
    if not isinstance(manifest, dict):
        raise AnalysisError("fact-pack manifest must be an object")
    run_id = request.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise AnalysisError("analysis handoff runId must be nonempty")
    if run_id != fact_pack_dir.parent.name:
        raise AnalysisError("analysis handoff runId does not match fact pack")
    expected_hash = request.get("factPackManifestSha256")
    actual_hash = _manifest_sha256(manifest_path)
    if expected_hash != actual_hash:
        raise AnalysisError("analysis handoff manifest hash mismatch")
    material_ids = [str(item) for item in manifest.get("materialIds", [])]
    target_top_n = manifest.get("targetTopN")
    if type(target_top_n) is not int or target_top_n <= 0 or len(material_ids) != target_top_n:
        raise AnalysisError("fact-pack manifest material scope is invalid")
    sections = _normalize_handoff_sections(request.get("sections"))
    category_statuses = ((manifest.get("quality") or {}).get("categoryStatuses") or {})
    status_keys = {
        "material_leaderboard": "materialLeaderboard",
        "audience": "audience",
        "scripts": "scripts",
        "highlight_frames": "highlightFrames",
    }
    for section in sections:
        section_path = fact_pack_dir / {
            "material_leaderboard": "material-leaderboard.json",
            "audience": "audience.json",
            "scripts": "scripts.json",
            "highlight_frames": "highlight-frames.json",
        }[section]
        status = category_statuses.get(status_keys[section])
        if not section_path.is_file() or (status is not None and status not in {"available", "built"}):
            raise AnalysisError(f"analysis section {section} is unavailable")
    request_text = request.get("requestText")
    if not isinstance(request_text, str) or not request_text.strip():
        raise AnalysisError("analysis handoff requestText must be nonempty")
    analysis_spec = request.get("analysisSpec")
    if analysis_spec is not None and not isinstance(analysis_spec, dict):
        raise AnalysisError("analysis handoff analysisSpec must be an object or null")
    selection = _normalize_material_scope(request.get("materialScope"), material_ids, target_top_n)
    normalized = deepcopy(request)
    normalized.update({
        "sections": sections,
        "selection": selection,
        "factPackPath": "fact-pack",
        "manifestSummary": {
            "schemaVersion": manifest.get("schemaVersion"),
            "targetTopN": target_top_n,
            "materialIds": material_ids,
            "counts": deepcopy(manifest.get("counts", {})),
            "qualityStatus": (manifest.get("quality") or {}).get("status"),
        },
        "sourceRefs": deepcopy(manifest.get("sourceFiles", [])),
    })
    return normalized


def _fact_materials(fact_pack: dict[str, Any]) -> list[dict[str, Any]]:
    payload = fact_pack.get("materialLeaderboard") or fact_pack.get("material-leaderboard") or {}
    materials = payload.get("materials") if isinstance(payload, dict) else None
    if not isinstance(materials, list) or not all(isinstance(item, dict) for item in materials):
        raise AnalysisError("fact pack material leaderboard is invalid")
    return materials


def _field_entry(registry: dict[str, Any], section: str, field: str) -> dict[str, Any]:
    section_schema = registry["sections"].get(section)
    if not isinstance(section_schema, dict):
        raise AnalysisError(f"unknown analysis section: {section}")
    fields = section_schema.get("fields", [])
    if field not in fields and not (
        section == "material_leaderboard" and field.startswith("metrics.") and field in fields
    ):
        raise AnalysisError(f"unknown analysis field: {section}.{field}")
    return section_schema


def _selected_materials(selection: dict[str, Any], materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _keys(selection, {"materialIds", "rankRange"}, "selection")
    by_id = {str(item.get("materialId")): item for item in materials}
    material_ids = selection.get("materialIds")
    if material_ids is not None:
        if not isinstance(material_ids, list) or not material_ids:
            raise AnalysisError("selection.materialIds must be a nonempty list")
        unknown = [str(item) for item in material_ids if str(item) not in by_id]
        if unknown:
            raise AnalysisError(f"material IDs outside fact pack: {', '.join(unknown)}")
    rank_range = selection.get("rankRange")
    if rank_range is not None:
        rank_range = _require_object(rank_range, "selection.rankRange")
        _keys(rank_range, {"start", "end"}, "selection.rankRange")
        start, end = rank_range.get("start"), rank_range.get("end")
        if type(start) is not int or type(end) is not int or start <= 0 or end < start:
            raise AnalysisError("selection.rankRange must contain positive start <= end")
    selected = materials
    if material_ids is not None:
        selected = [by_id[str(item)] for item in material_ids]
    if rank_range is not None:
        selected = [item for item in selected if rank_range["start"] <= item.get("rank", 0) <= rank_range["end"]]
    if not selected:
        raise AnalysisError("selection resolves to no materials")
    return selected


def resolve_analysis_spec(payload: dict[str, Any], registry: dict[str, Any], fact_pack: dict[str, Any]) -> dict[str, Any]:
    payload = _require_object(payload, "AnalysisSpec")
    _keys(payload, {"schemaVersion", "sections", "selection", "objective", "questions", "metrics", "groupComparisons", "calculations", "evidenceStrategy", "output"}, "AnalysisSpec")
    if payload.get("schemaVersion") != 1:
        raise AnalysisError("AnalysisSpec schemaVersion must be 1")
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        raise AnalysisError("AnalysisSpec.sections must be a nonempty list")
    if len(set(sections)) != len(sections):
        raise AnalysisError("AnalysisSpec.sections must not contain duplicates")
    for section in sections:
        if section not in registry["sections"]:
            raise AnalysisError(f"unknown analysis section: {section}")
    objective = payload.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise AnalysisError("AnalysisSpec.objective must be nonempty")
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions or not all(isinstance(item, str) and item.strip() for item in questions):
        raise AnalysisError("AnalysisSpec.questions must be a nonempty string list")
    selection = _require_object(payload.get("selection"), "AnalysisSpec.selection")
    selected = _selected_materials(selection, _fact_materials(fact_pack))
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise AnalysisError("AnalysisSpec.metrics must be a nonempty list")
    metrics = []
    for item in raw_metrics:
        item = _require_object(item, "AnalysisSpec.metrics item")
        _keys(item, {"section", "field", "aggregation"}, "AnalysisSpec.metrics item")
        section, field, aggregation = item.get("section"), item.get("field"), item.get("aggregation")
        if section not in sections:
            raise AnalysisError(f"metric section is not selected: {section}")
        section_schema = _field_entry(registry, section, field)
        if aggregation not in section_schema.get("aggregations", []):
            raise AnalysisError(f"unsupported aggregation: {section}.{field}.{aggregation}")
        metrics.append({"section": section, "field": field, "aggregation": aggregation})
    raw_groups = payload.get("groupComparisons", [])
    if not isinstance(raw_groups, list):
        raise AnalysisError("AnalysisSpec.groupComparisons must be a list")
    groups = []
    for item in raw_groups:
        item = _require_object(item, "AnalysisSpec.groupComparisons item")
        _keys(item, {"section", "groupBy", "groups"}, "AnalysisSpec.groupComparisons item")
        section, group_by = item.get("section"), item.get("groupBy")
        if section not in sections:
            raise AnalysisError(f"group section is not selected: {section}")
        section_schema = _field_entry(registry, section, group_by)
        if group_by not in section_schema.get("groupableFields", []):
            raise AnalysisError(f"field is not groupable: {section}.{group_by}")
        requested_groups = item.get("groups")
        if requested_groups is not None and (not isinstance(requested_groups, list) or not requested_groups):
            raise AnalysisError("groupComparisons.groups must be a nonempty list")
        groups.append({"section": section, "groupBy": group_by, **({"groups": requested_groups} if requested_groups is not None else {})})
    calculations = payload.get("calculations", [])
    if not isinstance(calculations, list):
        raise AnalysisError("AnalysisSpec.calculations must be a list")
    normalized_calculations = []
    for item in calculations:
        item = _require_object(item, "AnalysisSpec.calculations item")
        _keys(item, {"id", "formula", "inputs"}, "AnalysisSpec.calculations item")
        formula, inputs = item.get("formula"), item.get("inputs")
        if formula not in registry.get("calculations", {}):
            raise AnalysisError(f"unsupported calculation formula: {formula}")
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            raise AnalysisError("calculation id must be nonempty")
        if not isinstance(inputs, list) or len(inputs) != registry["calculations"][formula]["inputs"] or not all(isinstance(value, str) for value in inputs):
            raise AnalysisError(f"calculation {item.get('id')} must declare exactly two input fields")
        declared_fields = {metric["field"] for metric in metrics}
        for input_field in inputs:
            if input_field not in declared_fields:
                raise AnalysisError(f"unknown analysis field in calculation: {input_field}")
        normalized_calculations.append({"id": item["id"], "formula": formula, "inputs": inputs})
    evidence = _require_object(payload.get("evidenceStrategy"), "AnalysisSpec.evidenceStrategy")
    _keys(evidence, {"mode", "includeArtifacts", "maxRows"}, "AnalysisSpec.evidenceStrategy")
    if evidence.get("mode") not in {"source_linked", "source_linked_with_artifacts"}:
        raise AnalysisError("evidenceStrategy.mode is unsupported")
    if type(evidence.get("includeArtifacts")) is not bool:
        raise AnalysisError("evidenceStrategy.includeArtifacts must be boolean")
    if "maxRows" in evidence and (type(evidence["maxRows"]) is not int or evidence["maxRows"] <= 0):
        raise AnalysisError("evidenceStrategy.maxRows must be a positive integer")
    output = _require_object(payload.get("output"), "AnalysisSpec.output")
    _keys(output, {"format"}, "AnalysisSpec.output")
    if output.get("format") not in {"json", "markdown", "json+markdown"}:
        raise AnalysisError("AnalysisSpec.output.format is unsupported")
    normalized = {
        "schemaVersion": 1,
        "sections": sections,
        "selection": deepcopy(selection),
        "selectedMaterialIds": [str(item["materialId"]) for item in selected],
        "objective": objective.strip(),
        "questions": [item.strip() for item in questions],
        "metrics": metrics,
        "groupComparisons": groups,
        "calculations": normalized_calculations,
        "evidenceStrategy": deepcopy(evidence),
        "output": deepcopy(output),
    }
    return normalized


def _retrieval_scalar(value: Any) -> Any:
    """Return a comparable display value while preserving missing as None."""
    if isinstance(value, dict):
        for key in ("ValueStr", "valueStr", "Value", "value"):
            if key in value:
                return value[key]
    return value


def _retrieval_material_selection(scope: dict[str, Any], materials: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [str(item.get("materialId")) for item in materials]
    target_top_n = len(ids)
    scope = _require_object(scope, "FactRetrievalSpec.materialScope")
    _keys(scope, {"type", "value"}, "FactRetrievalSpec.materialScope")
    kind = scope.get("type")
    if kind == "all":
        return {"rankRange": {"start": 1, "end": target_top_n}}
    if kind == "top_n":
        value = scope.get("value")
        if type(value) is not int or not 0 < value <= target_top_n:
            raise AnalysisError("FactRetrievalSpec top_n materialScope is outside fact pack")
        return {"rankRange": {"start": 1, "end": value}}
    if kind == "material_ids":
        value = scope.get("value")
        if not isinstance(value, list) or not value:
            raise AnalysisError("FactRetrievalSpec material_ids scope must be a nonempty list")
        selected = [str(item) for item in value]
        unknown = sorted(set(selected) - set(ids))
        if unknown:
            raise AnalysisError(f"FactRetrievalSpec material IDs outside fact pack: {', '.join(unknown)}")
        return {"materialIds": selected}
    if kind == "material_id_range":
        value = scope.get("value")
        if isinstance(value, dict):
            _keys(value, {"start", "end"}, "FactRetrievalSpec material_id_range")
            start, end = value.get("start"), value.get("end")
        elif isinstance(value, list) and len(value) == 2:
            start, end = value
        else:
            raise AnalysisError("FactRetrievalSpec material_id_range must contain start and end")
        if type(start) is not int or type(end) is not int or start <= 0 or end < start or end > target_top_n:
            raise AnalysisError("FactRetrievalSpec material_id_range is outside fact pack")
        return {"materialIds": ids[start - 1:end]}
    raise AnalysisError("FactRetrievalSpec materialScope.type is unsupported")


def _retrieval_to_analysis_spec(spec: dict[str, Any], registry: dict[str, Any], fact_pack: dict[str, Any]) -> dict[str, Any]:
    selection = spec["selection"]
    metrics = [
        {"section": item["section"], "field": item["field"], "aggregation": item["aggregation"]}
        for item in spec["fields"]
    ]
    # AnalysisSpec's calculation contract is intentionally reused for deterministic local formulas.
    return resolve_analysis_spec({
        "schemaVersion": 1,
        "sections": spec["sections"],
        "selection": selection,
        "objective": spec["retrievalGoal"],
        "questions": [spec["retrievalGoal"]],
        "metrics": metrics,
        "groupComparisons": [
            {"section": item["section"], "groupBy": item["groupBy"]}
            for item in spec["groupComparisons"]
        ],
        "calculations": spec["calculations"],
        "evidenceStrategy": {
            "mode": "source_linked_with_artifacts" if spec["evidenceStrategy"].get("includeArtifacts") else "source_linked",
            "includeArtifacts": bool(spec["evidenceStrategy"].get("includeArtifacts", False)),
            "maxRows": spec["evidenceStrategy"].get("maxRows"),
        },
        "output": {"format": "json+markdown"},
    }, registry, fact_pack)


def resolve_fact_retrieval_spec(
    payload: dict[str, Any],
    registry: dict[str, Any],
    fact_pack: dict[str, Any],
    *,
    run_id: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    """Validate a model-produced, fact-pack-only retrieval plan."""
    payload = _require_object(payload, "FactRetrievalSpec")
    _keys(payload, _RETRIEVAL_KEYS, "FactRetrievalSpec")
    if _FORBIDDEN_RETRIEVAL_KEYS.intersection(payload):
        raise AnalysisError("FactRetrievalSpec contains forbidden QuerySpec fields: %s" % ", ".join(sorted(_FORBIDDEN_RETRIEVAL_KEYS.intersection(payload))))
    if payload.get("schemaVersion") != 1:
        raise AnalysisError("FactRetrievalSpec schemaVersion must be 1")
    if payload.get("runId") != run_id or payload.get("factPackManifestSha256") != manifest_sha256:
        raise AnalysisError("FactRetrievalSpec run or manifest binding mismatch")
    goal = payload.get("retrievalGoal")
    if not isinstance(goal, str) or not goal.strip():
        raise AnalysisError("FactRetrievalSpec retrievalGoal must be nonempty")
    sections = _normalize_handoff_sections(payload.get("sections"))
    materials = _fact_materials(fact_pack)
    selection = _retrieval_material_selection(payload.get("materialScope"), materials)
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise AnalysisError("FactRetrievalSpec.fields must be a nonempty list")
    fields = []
    seen = set()
    for item in raw_fields:
        item = _require_object(item, "FactRetrievalSpec.fields item")
        _keys(item, {"section", "field", "aggregation"}, "FactRetrievalSpec.fields item")
        section, field, aggregation = item.get("section"), item.get("field"), item.get("aggregation")
        if section not in sections:
            raise AnalysisError(f"FactRetrievalSpec field section is not selected: {section}")
        schema = _field_entry(registry, section, field)
        if aggregation not in schema.get("aggregations", []):
            raise AnalysisError(f"FactRetrievalSpec unsupported aggregation: {section}.{field}.{aggregation}")
        key = (section, field, aggregation)
        if key not in seen:
            fields.append({"section": section, "field": field, "aggregation": aggregation})
            seen.add(key)
    filters = []
    raw_filters = payload.get("filters", [])
    if not isinstance(raw_filters, list):
        raise AnalysisError("FactRetrievalSpec.filters must be a list")
    for item in raw_filters:
        item = _require_object(item, "FactRetrievalSpec.filters item")
        _keys(item, {"section", "field", "operator", "value"}, "FactRetrievalSpec.filters item")
        section, field, operator = item.get("section"), item.get("field"), item.get("operator")
        if section not in sections:
            raise AnalysisError(f"FactRetrievalSpec filter section is not selected: {section}")
        _field_entry(registry, section, field)
        if operator not in _RETRIEVAL_OPERATORS:
            raise AnalysisError(f"FactRetrievalSpec unsupported filter operator: {operator}")
        if "value" not in item:
            raise AnalysisError("FactRetrievalSpec filter value is required")
        filters.append({"section": section, "field": field, "operator": operator, "value": deepcopy(item["value"])})
    groups = []
    raw_groups = payload.get("groupComparisons", [])
    if not isinstance(raw_groups, list):
        raise AnalysisError("FactRetrievalSpec.groupComparisons must be a list")
    for item in raw_groups:
        item = _require_object(item, "FactRetrievalSpec.groupComparisons item")
        _keys(item, {"section", "groupBy", "aggregations"}, "FactRetrievalSpec.groupComparisons item")
        section, group_by = item.get("section"), item.get("groupBy")
        if section not in sections:
            raise AnalysisError(f"FactRetrievalSpec group section is not selected: {section}")
        schema = _field_entry(registry, section, group_by)
        if group_by not in schema.get("groupableFields", []):
            raise AnalysisError(f"FactRetrievalSpec field is not groupable: {section}.{group_by}")
        aggregations = item.get("aggregations", ["count"])
        if not isinstance(aggregations, list) or not aggregations or any(value not in schema.get("aggregations", []) for value in aggregations):
            raise AnalysisError(f"FactRetrievalSpec invalid group aggregations: {section}.{group_by}")
        groups.append({"section": section, "groupBy": group_by, "aggregations": list(dict.fromkeys(aggregations))})
    calculations = payload.get("calculations", [])
    if not isinstance(calculations, list):
        raise AnalysisError("FactRetrievalSpec.calculations must be a list")
    normalized = {
        "schemaVersion": 1,
        "runId": run_id,
        "factPackManifestSha256": manifest_sha256,
        "retrievalGoal": goal.strip(),
        "sections": sections,
        "materialScope": deepcopy(payload["materialScope"]),
        "selection": selection,
        "fields": fields,
        "filters": filters,
        "groupComparisons": groups,
        "calculations": deepcopy(calculations),
        "evidenceStrategy": deepcopy(payload.get("evidenceStrategy", {})),
        "retrievalTrace": deepcopy(payload.get("retrievalTrace", [])),
    }
    evidence = normalized["evidenceStrategy"]
    _keys(evidence, {"includeArtifacts", "maxRows"}, "FactRetrievalSpec.evidenceStrategy")
    if "includeArtifacts" in evidence and type(evidence["includeArtifacts"]) is not bool:
        raise AnalysisError("FactRetrievalSpec evidenceStrategy.includeArtifacts must be boolean")
    if "maxRows" in evidence and (type(evidence["maxRows"]) is not int or evidence["maxRows"] <= 0):
        raise AnalysisError("FactRetrievalSpec evidenceStrategy.maxRows must be a positive integer")
    normalized["analysisSpec"] = _retrieval_to_analysis_spec(normalized, registry, fact_pack)
    normalized["selectedMaterialIds"] = normalized["analysisSpec"]["selectedMaterialIds"]
    return normalized


def analysis_fingerprint(spec: dict[str, Any]) -> str:
    encoded = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


PROMPT_VERSION = "qianchuan-analysis-prompt-v2"

_MODEL_RESULT_SCHEMA_SUMMARY = {
    "schemaVersion": 1,
    "contractVersion": 2,
    "type": "object",
    "required": ["schemaVersion", "contractVersion", "analysisSpecFingerprint", "claims"],
    "claimRequired": ["claimType", "statement", "section", "priority", "evidenceRefs", "confidence"],
    "claimTypes": ["fact", "calculation", "inference", "recommendation"],
    "sections": [*_HANDOFF_SECTIONS, "synthesis"],
    "priorityValues": ["high", "medium", "low"],
    "optionalClaimFields": ["businessImplication"],
    "calculationRequired": ["calculationId", "formula", "inputs"],
}


def _json_write(path: Path, payload: Any) -> None:
    _write_json(path, payload)


def _path_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _source_key(ref: dict[str, Any]) -> tuple[Any, ...]:
    indexes = ref.get("recordIndexes")
    if indexes is None and "recordIndex" in ref:
        indexes = [ref["recordIndex"]]
    return (ref.get("path"), ref.get("sha256"), ref.get("sizeBytes"), tuple(indexes or []))


def _append_ref(refs: list[dict[str, Any]], ref: Any) -> None:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        return
    indexes = ref.get("recordIndexes")
    if indexes is None and "recordIndex" in ref:
        indexes = [ref["recordIndex"]]
    indexes = sorted({index for index in indexes or [] if type(index) is int and index >= 0})
    for existing in refs:
        if (
            existing.get("path") == ref.get("path")
            and existing.get("sha256") == ref.get("sha256")
            and existing.get("sizeBytes") == ref.get("sizeBytes")
        ):
            existing_indexes = existing.get("recordIndexes")
            if existing_indexes is None and "recordIndex" in existing:
                existing_indexes = [existing["recordIndex"]]
            merged = sorted({index for index in (existing_indexes or []) + indexes if type(index) is int and index >= 0})
            if merged:
                existing["recordIndexes"] = merged
            existing.pop("recordIndex", None)
            return
    normalized = deepcopy(ref)
    normalized.pop("recordIndex", None)
    if indexes:
        normalized["recordIndexes"] = indexes
    else:
        normalized.pop("recordIndexes", None)
    refs.append(normalized)


def _collect_refs(value: Any, refs: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if {"path", "sha256", "sizeBytes"}.issubset(value):
            _append_ref(refs, value)
        for item in value.values():
            _collect_refs(item, refs)
    elif isinstance(value, list):
        for item in value:
            _collect_refs(item, refs)


def _section_rows(payload: dict[str, Any], section: str, selected_ids: set[str]) -> list[dict[str, Any]]:
    section_schema = payload
    collection = section_schema.get("materials") if section == "material_leaderboard" else section_schema.get("audience" if section == "audience" else "scripts" if section == "scripts" else "highlightFrames")
    if not isinstance(collection, list):
        return []
    return [row for row in collection if isinstance(row, dict) and str(row.get("materialId")) in selected_ids]


def _requested_fields(spec: dict[str, Any], section: str) -> list[tuple[str, str]]:
    return [(item["field"], item["aggregation"]) for item in spec["metrics"] if item["section"] == section]


def _aggregate(values: list[Any], aggregation: str) -> Any:
    present = [value for value in values if value is not None]
    if aggregation == "value":
        return present[0] if present else None
    if aggregation == "count":
        return len(present)
    if not present:
        return None
    if aggregation in {"sum", "avg", "min", "max"}:
        try:
            numbers = [float(value) for value in present]
        except (TypeError, ValueError):
            return None
        if aggregation == "sum":
            return sum(numbers)
        if aggregation == "avg":
            return sum(numbers) / len(numbers)
        if aggregation == "min":
            return min(numbers)
        return max(numbers)
    raise AnalysisError(f"unsupported aggregation: {aggregation}")


def _load_section(fact_pack_dir: Path, section: str) -> dict[str, Any]:
    filename = {
        "material_leaderboard": "material-leaderboard.json",
        "audience": "audience.json",
        "scripts": "scripts.json",
        "highlight_frames": "highlight-frames.json",
    }[section]
    payload = _read_json(fact_pack_dir / filename, f"{section} fact file")
    if not isinstance(payload, dict):
        raise AnalysisError(f"{section} fact file must be an object")
    return payload


def _project_row(row: dict[str, Any], fields: list[tuple[str, str]]) -> tuple[dict[str, Any], list[str]]:
    projected: dict[str, Any] = {}
    missing: list[str] = []
    for field, aggregation in fields:
        value = _path_value(row, field)
        if value is None:
            missing.append(field)
            continue
        if aggregation == "value":
            projected[field] = value
        else:
            projected[field] = _aggregate([value], aggregation)
    return projected, missing


def _matches_filter(row: dict[str, Any], item: dict[str, Any]) -> bool:
    actual = _retrieval_scalar(_path_value(row, item["field"]))
    expected = _retrieval_scalar(item.get("value"))
    if actual is None:
        return False
    op = item["operator"]
    if op == "eq":
        return actual == expected or str(actual) == str(expected)
    if op == "neq":
        return not (actual == expected or str(actual) == str(expected))
    if op == "contains":
        return str(expected) in str(actual)
    if op == "in":
        values = expected if isinstance(expected, list) else [expected]
        return any(actual == value or str(actual) == str(value) for value in values)
    try:
        left, right = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


def slice_fact_pack(run_dir: Path, fact_pack_dir: Path, spec: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    fact_pack_dir = Path(fact_pack_dir).resolve()
    selected_ids = set(spec["selectedMaterialIds"])
    sections: dict[str, Any] = {}
    all_refs: list[dict[str, Any]] = []
    missing_items: list[dict[str, Any]] = []
    max_rows = spec["evidenceStrategy"].get("maxRows")
    for section in spec["sections"]:
        payload = _load_section(fact_pack_dir, section)
        status = payload.get("status", "available")
        if status not in {"available", "built", None}:
            missing_items.append({"section": section, "code": "category_unavailable", "status": status})
        rows = _section_rows(payload, section, selected_ids)
        row_filters = [item for item in spec.get("filters", []) if item.get("section") == section]
        rows = [row for row in rows if not row_filters or all(_matches_filter(row, item) for item in row_filters)]
        if status not in {"available", "built", None}:
            rows = []
        fields = _requested_fields(spec, section)
        projected_rows: list[dict[str, Any]] = []
        section_refs: list[dict[str, Any]] = []
        for row in rows:
            projected, missing = _project_row(row, fields)
            result = {"materialId": str(row["materialId"]), "fields": projected}
            if isinstance(row.get("sourceRef"), dict):
                result["sourceRef"] = deepcopy(row["sourceRef"])
                _append_ref(section_refs, row["sourceRef"])
            if missing:
                result["missingFields"] = missing
                for field in missing:
                    missing_items.append({"section": section, "materialId": str(row.get("materialId")), "field": field, "code": "field_missing"})
            projected_rows.append(result)
            _collect_refs(projected, section_refs)
        row_material_ids = {row["materialId"] for row in projected_rows}
        for material_id in sorted(selected_ids - row_material_ids):
            missing_items.append({"section": section, "materialId": material_id, "code": "no_category_record"})
        if max_rows is not None and len(projected_rows) > max_rows:
            missing_items.append({"section": section, "code": "evidence_truncated", "rowCount": len(projected_rows), "maxRows": max_rows})
            projected_rows = projected_rows[:max_rows]
        if not projected_rows:
            missing_items.append({"section": section, "code": "no_selected_evidence", "materialIds": sorted(selected_ids)})
        for ref in section_refs:
            _append_ref(all_refs, ref)
        sections[section] = {"status": status, "rows": projected_rows, "sourceRefs": section_refs}

        # Group summaries are deterministic and intentionally descriptive, not business conclusions.
        for group in [item for item in spec.get("groupComparisons", []) if item.get("section") == section]:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                key = _retrieval_scalar(_path_value(row, group["groupBy"]))
                grouped.setdefault("<missing>" if key is None else str(key), []).append(row)
            summaries = []
            first_field = fields[0][0] if fields else None
            for key, group_rows in grouped.items():
                summary = {"group": key}
                vals = [_path_value(row, first_field) for row in group_rows] if first_field else []
                for aggregation in group.get("aggregations", []):
                    summary[aggregation] = _aggregate(vals, aggregation) if aggregation != "count" else len([value for value in vals if value is not None])
                summaries.append(summary)
            sections[section]["groupComparisons"] = summaries

    manifest_path = fact_pack_dir / "manifest.json"
    if manifest_path.is_file():
        data = manifest_path.read_bytes()
        manifest_ref = {"path": "fact-pack/manifest.json", "sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data)}
        _append_ref(all_refs, manifest_ref)
    calculations = []
    field_sections = {item["field"]: item["section"] for item in spec["metrics"]}
    for calculation in spec["calculations"]:
        input_fields = calculation["inputs"]
        section = field_sections[input_fields[0]]
        rows_by_material = {row["materialId"]: row for row in sections[section]["rows"]}
        values: dict[str, Any] = {}
        refs_by_material: dict[str, list[dict[str, Any]]] = {}
        for material_id, row in rows_by_material.items():
            raw_values = [row.get("fields", {}).get(field) for field in input_fields]
            if any(value is None for value in raw_values):
                missing_items.append({"section": section, "materialId": material_id, "calculation": calculation["id"], "code": "calculation_input_missing"})
                continue
            try:
                left, right = float(raw_values[0]), float(raw_values[1])
                formula = calculation["formula"]
                if formula == "difference" or formula == "percentage_point_difference":
                    calculated = left - right
                elif formula == "ratio":
                    calculated = None if right == 0 else left / right
                elif formula == "percentage_difference":
                    calculated = None if right == 0 else (left - right) / right * 100
                else:
                    raise AnalysisError(f"unsupported calculation formula: {formula}")
            except (TypeError, ValueError):
                calculated = None
            if calculated is None:
                missing_items.append({"section": section, "materialId": material_id, "calculation": calculation["id"], "code": "calculation_input_missing"})
                continue
            values[material_id] = int(calculated) if calculated.is_integer() else calculated
            if isinstance(row.get("sourceRef"), dict):
                refs_by_material[material_id] = [deepcopy(row["sourceRef"])]
        calculations.append({"id": calculation["id"], "formula": calculation["formula"], "inputs": input_fields, "values": values, "evidenceRefsByMaterial": refs_by_material})
    return {
        "schemaVersion": 1,
        "inputType": "source_linked_fact_slice",
        "selectedMaterialIds": spec["selectedMaterialIds"],
        "sections": sections,
        "sourceRefs": all_refs,
        "missingItems": missing_items,
        "calculations": calculations,
        "retrievalTrace": deepcopy(spec.get("retrievalTrace", [])),
    }


def retrieve_fact_slice(run_dir: Path, fact_pack_dir: Path, retrieval_spec: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    """Execute a normalized FactRetrievalSpec entirely against local fact-pack files."""
    spec = deepcopy(retrieval_spec.get("analysisSpec")) if isinstance(retrieval_spec.get("analysisSpec"), dict) else deepcopy(retrieval_spec)
    spec["filters"] = deepcopy(retrieval_spec.get("filters", []))
    spec["groupComparisons"] = deepcopy(retrieval_spec.get("groupComparisons", []))
    spec["retrievalTrace"] = deepcopy(retrieval_spec.get("retrievalTrace", []))
    evidence = slice_fact_pack(run_dir, fact_pack_dir, spec, registry)
    trace = retrieval_spec.get("retrievalTrace") or []
    if not trace:
        trace = [
            {
                "intent": retrieval_spec.get("retrievalGoal", ""),
                "section": field["section"],
                "fields": [field["field"]],
                "reason": "自然语言意图映射到事实包注册字段",
            }
            for field in retrieval_spec.get("fields", [])
        ]
    evidence["retrievalTrace"] = trace
    return evidence


_PROMPT_INSTRUCTIONS = {
    "material_leaderboard": "Use platform-provided leaderboard facts only. Do not infer creative meaning from material names or material/video types.",
    "audience": "Describe audience differences as associations/correlations only; do not infer causality or state causality. Keep metricValue and metricPercentage semantics distinct.",
    "scripts": "Use exact transcript fragments when quoting scripts. Do not turn a script label or extracted text into a platform performance fact.",
    "highlight_frames": "JPEGs are evidence for AI visual observation only. Mark visual observations as ai_inference, cite the JPEG artifact ref, and never present visual observations as platform raw facts.",
}


def compile_model_input(
    run_dir: Path,
    spec: dict[str, Any],
    evidence: dict[str, Any],
    registry: dict[str, Any],
    request_text: str | None = None,
) -> dict[str, Any]:
    sections = {}
    for section in spec["sections"]:
        sections[section] = {
            "prompt": _PROMPT_INSTRUCTIONS[section],
            "objective": spec["objective"],
            "questions": spec["questions"],
            "metrics": _requested_fields(spec, section),
            "groupComparisons": [item for item in spec["groupComparisons"] if item["section"] == section],
            "evidence": evidence["sections"].get(section, {"rows": [], "sourceRefs": []}),
        }
    manifest_ref = next((ref for ref in evidence["sourceRefs"] if ref["path"] == "fact-pack/manifest.json"), None)
    capability_hints = None
    capability_path = Path(__file__).resolve().parents[1] / "config" / "analysis-capability-registry-v1.json"
    if capability_path.is_file():
        capability_hints = load_analysis_capability_registry(capability_path)
    model_input = {
        "schemaVersion": 1,
        "inputType": "qianchuan_analysis_model_input",
        "promptVersion": PROMPT_VERSION,
        "runId": Path(run_dir).name,
        "analysisSpec": deepcopy(spec),
        "analysisSpecFingerprint": analysis_fingerprint(spec),
        "factPackManifestSha256": manifest_ref.get("sha256") if manifest_ref else None,
        "factPack": {"manifestRef": manifest_ref, "manifestSha256": manifest_ref.get("sha256") if manifest_ref else None},
        "sections": sections,
        "calculations": deepcopy(evidence.get("calculations", [])),
        "sourceRefs": deepcopy(evidence["sourceRefs"]),
        "capabilityHints": capability_hints,
        "modelConstraints": {
            "allowedSections": list(spec["sections"]),
            "allowedMaterialIds": list(spec["selectedMaterialIds"]),
            "onlyUseProvidedEvidence": True,
            "doNotUseExternalData": True,
            "doNotFillMissingData": True,
            "allClaimsRequireEvidenceRefs": True,
            "claimsMustUseSelectedSectionsOrSynthesis": True,
            "recommendationsMustReferenceEvidence": True,
            "businessImplicationExplainsAction": True,
        },
        "resultSchema": deepcopy(_MODEL_RESULT_SCHEMA_SUMMARY),
        "missingItems": deepcopy(evidence["missingItems"]),
        "handoff": {
            "modelMustReturn": "analysis-result.json",
            "contractVersion": 2,
            "claimTypes": ["fact", "calculation", "inference", "recommendation"],
            "allClaimsRequire": ["claimType", "section", "priority", "evidenceRefs", "confidence"],
            "sectionOrder": [*_HANDOFF_SECTIONS, "synthesis"],
            "recommendationsRequire": ["evidenceRefs", "businessImplication"],
        },
    }
    if request_text is not None:
        model_input["requestText"] = request_text
    return model_input


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


def _write_text(path: Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _safe_request(value: Any) -> Any:
    sensitive = {"authorization", "cookie", "session", "password", "token", "secret", "credential"}
    if isinstance(value, dict):
        return {key: _safe_request(item) for key, item in value.items() if str(key).lower() not in sensitive}
    if isinstance(value, list):
        return [_safe_request(item) for item in value]
    if isinstance(value, str):
        return value.replace("https://", "[redacted-url]").replace("http://", "[redacted-url]")
    return value


def _fact_pack_payload(fact_pack_dir: Path) -> dict[str, Any]:
    return {
        "manifest": _read_json(fact_pack_dir / "manifest.json", "fact-pack manifest"),
        "materialLeaderboard": _load_section(fact_pack_dir, "material_leaderboard"),
    }


def _manifest_ref(run_dir: Path, fact_pack_dir: Path) -> dict[str, Any]:
    path = fact_pack_dir / "manifest.json"
    relative = path.resolve().relative_to(Path(run_dir).resolve()).as_posix()
    digest, size = file_hash_and_size(path)
    return {"path": relative, "sha256": digest, "sizeBytes": size}


def _request_spec(request: dict[str, Any]) -> dict[str, Any]:
    if "analysisSpec" in request:
        spec = request["analysisSpec"]
    elif request.get("stage") == "analysis":
        spec = None
    elif "sections" in request:
        spec = request
    else:
        raise AnalysisError("analysis request requires structured analysisSpec; model parsing is not configured")
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise AnalysisError("analysisSpec must be an object")
    return spec


def _default_analysis_spec(request: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    default_fields = {
        "material_leaderboard": ("metrics.stat_cost_for_overall_roi2", "value"),
        "audience": ("metricPercentage", "value"),
        "scripts": ("text", "value"),
        "highlight_frames": ("durationSec", "value"),
    }
    metrics = []
    for section in request["sections"]:
        field, aggregation = default_fields[section]
        _field_entry(registry, section, field)
        metrics.append({"section": section, "field": field, "aggregation": aggregation})
    return {
        "schemaVersion": 1,
        "sections": request["sections"],
        "selection": deepcopy(request["selection"]),
        "objective": request["requestText"].strip(),
        "questions": [request["requestText"].strip()],
        "metrics": metrics,
        "groupComparisons": [],
        "calculations": [],
        "evidenceStrategy": {"mode": "source_linked", "includeArtifacts": False},
        "output": {"format": "json+markdown"},
    }


def _fact_pack_fingerprint(run_dir: Path, fact_pack_dir: Path) -> str:
    manifest = _read_json(fact_pack_dir / "manifest.json", "fact pack manifest")
    paths = {"fact-pack/manifest.json"}
    for name in ("material-leaderboard.json", "audience.json", "scripts.json", "highlight-frames.json"):
        paths.add(f"fact-pack/{name}")
    for ref in manifest.get("sourceFiles", []):
        if isinstance(ref, dict) and isinstance(ref.get("path"), str):
            paths.add(ref["path"])
    entries = []
    for relative in sorted(paths):
        path = run_dir / relative
        digest, size = file_hash_and_size(path)
        entries.append({"path": relative, "sha256": digest, "sizeBytes": size})
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prepare_analysis(run_dir: Path, request_path: Path, output_dir: Path | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    fact_pack_dir = run_dir / "fact-pack"
    output_dir = Path(output_dir).resolve() if output_dir else run_dir / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    request = _read_json(Path(request_path), "analysis request")
    if not isinstance(request, dict):
        raise AnalysisError("analysis request must be an object")
    nested_retrieval = request.get("factRetrievalSpec")
    if isinstance(nested_retrieval, dict):
        request = deepcopy(request)
        request.setdefault("stage", "analysis")
        request.setdefault("runId", nested_retrieval.get("runId"))
        request.setdefault("factPackManifestSha256", nested_retrieval.get("factPackManifestSha256"))
        request.setdefault("sections", nested_retrieval.get("sections"))
        request.setdefault("materialScope", nested_retrieval.get("materialScope"))
    if request.get("stage") == "analysis" or any(
        key in request for key in ("runId", "factPackManifestSha256", "materialScope")
    ):
        request = validate_analysis_handoff(request, fact_pack_dir)
    _write_json(output_dir / "analysis-request.json", _safe_request(request))
    fact_pack_fingerprint = None
    try:
        fact_pack_fingerprint = _fact_pack_fingerprint(run_dir, fact_pack_dir)
    except (AnalysisError, OSError):
        # Let the existing verifier produce the stable incomplete-fact-pack error.
        pass
    existing_receipt_path = output_dir / "analysis-receipt.json"
    existing_receipt = _read_json(existing_receipt_path, "analysis receipt") if existing_receipt_path.is_file() else None
    cached_fact_result = (existing_receipt or {}).get("validation", {}).get("factPack")
    if (
        fact_pack_fingerprint is not None
        and (existing_receipt or {}).get("factPackFingerprint") == fact_pack_fingerprint
        and isinstance(cached_fact_result, dict)
        and cached_fact_result.get("status") == "succeeded"
    ):
        fact_result = cached_fact_result
    else:
        fact_result = verify_fact_pack(run_dir, fact_pack_dir)
    if fact_result.get("status") != "succeeded":
        receipt = {
            "schemaVersion": 1,
            "runId": run_dir.name,
            "status": "blocked",
            "reason": "fact_pack_incomplete",
            "missingItems": [{"code": "fact_pack_incomplete", "errors": fact_result.get("errors", [])}],
            "validation": {"factPack": fact_result},
        }
        _write_json(output_dir / "analysis-receipt.json", receipt)
        raise AnalysisError("fact pack is incomplete; commercial analysis is blocked")
    registry = load_analysis_registry(Path(__file__).resolve().parents[1] / "config" / "analysis-registry-v1.json")
    fact_pack = _fact_pack_payload(fact_pack_dir)
    retrieval_spec = None
    if isinstance(request.get("factRetrievalSpec"), dict):
        retrieval_spec = resolve_fact_retrieval_spec(
            request["factRetrievalSpec"], registry, fact_pack,
            run_id=run_dir.name,
            manifest_sha256=_manifest_ref(run_dir, fact_pack_dir)["sha256"],
        )
        spec = retrieval_spec["analysisSpec"]
        evidence = retrieve_fact_slice(run_dir, fact_pack_dir, retrieval_spec, registry)
    else:
        request_spec = _request_spec(request)
        if request.get("stage") == "analysis" and request_spec is None:
            request_spec = _default_analysis_spec(request, registry)
        if request.get("stage") == "analysis":
            request_spec = deepcopy(request_spec)
            request_spec["sections"] = deepcopy(request["sections"])
            request_spec["selection"] = deepcopy(request["selection"])
        spec = resolve_analysis_spec(request_spec, registry, fact_pack)
        evidence = slice_fact_pack(run_dir, fact_pack_dir, spec, registry)
    model_input = compile_model_input(
        run_dir,
        spec,
        evidence,
        registry,
        request_text=request.get("requestText", "") if isinstance(request.get("requestText", ""), str) else "",
    )
    if retrieval_spec is not None:
        model_input["factRetrievalSpec"] = deepcopy(retrieval_spec)
        model_input["retrievalTrace"] = deepcopy(evidence.get("retrievalTrace", []))
    _write_json(output_dir / "analysis-spec.json", spec)
    _write_json(output_dir / "analysis-input.json", model_input)
    status = "awaiting_model_with_missing_data" if evidence["missingItems"] else "awaiting_model"
    receipt = {
        "schemaVersion": 1,
        "runId": run_dir.name,
        "stage": "analysis_prepared",
        "factPackFingerprint": fact_pack_fingerprint,
        "factPackManifestSha256": _manifest_ref(run_dir, fact_pack_dir)["sha256"],
        "analysisSpec": deepcopy(spec),
        "analysisSpecFingerprint": analysis_fingerprint(spec),
        "promptVersion": PROMPT_VERSION,
        "status": status,
        "sections": deepcopy(spec["sections"]),
        "selectedMaterialIds": deepcopy(spec["selectedMaterialIds"]),
        "sourceRefs": deepcopy(model_input["sourceRefs"]),
        "missingItems": deepcopy(evidence["missingItems"]),
        "validation": {"factPack": fact_result, "analysisInput": "succeeded"},
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(output_dir / "analysis-receipt.json", receipt)
    return receipt


def _available_input_refs(model_input: dict[str, Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    refs: dict[tuple[Any, ...], dict[str, Any]] = {}
    for ref in model_input.get("sourceRefs", []):
        if isinstance(ref, dict) and isinstance(ref.get("path"), str):
            refs[_source_key(ref)] = ref
    return refs


def _ref_is_present(ref: dict[str, Any], available: dict[tuple[Any, ...], dict[str, Any]]) -> bool:
    indexes = ref.get("recordIndexes")
    if indexes is None and "recordIndex" in ref:
        indexes = [ref["recordIndex"]]
    requested = set(indexes or [])
    for candidate in available.values():
        if (
            candidate.get("path") == ref.get("path")
            and candidate.get("sha256") == ref.get("sha256")
            and candidate.get("sizeBytes") == ref.get("sizeBytes")
        ):
            candidate_indexes = candidate.get("recordIndexes")
            if candidate_indexes is None and "recordIndex" in candidate:
                candidate_indexes = [candidate["recordIndex"]]
            if requested.issubset(set(candidate_indexes or [])):
                return True
    return False


def _source_path(run_dir: Path, ref: dict[str, Any], label: str) -> Path:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise AnalysisError(f"{label} must contain a relative source ref")
    relative = PurePosixPath(ref["path"])
    if relative.is_absolute() or ".." in relative.parts or "\\" in ref["path"]:
        raise AnalysisError(f"{label} path must be relative")
    path = Path(run_dir) / Path(*relative.parts)
    try:
        path.resolve().relative_to(Path(run_dir).resolve())
    except ValueError as exc:
        raise AnalysisError(f"{label} path escapes run directory") from exc
    return path


def _verify_source_ref(run_dir: Path, ref: dict[str, Any], label: str) -> None:
    path = _source_path(run_dir, ref, label)
    if not path.is_file():
        raise AnalysisError(f"{label} hash does not match source")
    digest, size = file_hash_and_size(path)
    if size != ref.get("sizeBytes") or digest != ref.get("sha256"):
        raise AnalysisError(f"{label} hash does not match source")


def _legacy_claim_section(claim: dict[str, Any], selected_sections: list[str]) -> tuple[str, str]:
    """Infer a legacy claim's section from source paths without changing its evidence."""
    matches = set()
    for ref in claim.get("evidenceRefs", []):
        path = str(ref.get("path", ""))
        if "material" in path and "material_leaderboard" in selected_sections:
            matches.add("material_leaderboard")
        if "audience" in path and "audience" in selected_sections:
            matches.add("audience")
        if "script" in path and "scripts" in selected_sections:
            matches.add("scripts")
        if ("frame" in path or path.lower().endswith((".jpg", ".jpeg", ".png"))) and "highlight_frames" in selected_sections:
            matches.add("highlight_frames")
    if len(matches) == 1:
        return matches.pop(), "legacy_inferred"
    return "synthesis", "legacy_inferred"


def _normalize_claim_contract(claim: dict[str, Any], selected_sections: list[str], legacy: bool) -> dict[str, Any]:
    normalized = deepcopy(claim)
    if legacy and ("section" not in normalized or "priority" not in normalized):
        section, mode = _legacy_claim_section(normalized, selected_sections)
        normalized.setdefault("section", section)
        normalized.setdefault("priority", "medium")
        normalized["classificationMode"] = mode
    return normalized


def validate_analysis_result(result: dict[str, Any], model_input: dict[str, Any], spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    result = _require_object(result, "analysis result")
    if result.get("schemaVersion") != 1:
        raise AnalysisError("analysis result schemaVersion must be 1")
    if "contractVersion" in result and result["contractVersion"] != 2:
        raise AnalysisError("analysis result contractVersion is unsupported")
    expected_fingerprint = model_input.get("analysisSpecFingerprint")
    if result.get("analysisSpecFingerprint") != expected_fingerprint:
        raise AnalysisError("analysis result fingerprint does not match model input")
    claims = result.get("claims")
    if not isinstance(claims, list):
        raise AnalysisError("analysis result claims must be a list")
    if not claims:
        raise AnalysisError("analysis result must contain at least one claim")
    refs = _available_input_refs(model_input)
    allowed_types = {"fact", "calculation", "inference", "recommendation"}
    normalized = deepcopy(result)
    selected_sections = list(spec.get("sections") or model_input.get("analysisSpec", {}).get("sections") or [])
    # Results without a contractVersion are historical artifacts; v2 results opt in explicitly.
    legacy = result.get("contractVersion") != 2
    for index, claim in enumerate(claims):
        label = f"claim[{index}]"
        claim = _require_object(claim, label)
        for required in ("claimType", "evidenceRefs", "confidence"):
            if required not in claim:
                raise AnalysisError(f"{label} missing {required}")
        if not legacy and "section" not in claim:
            raise AnalysisError(f"{label} missing section")
        if not legacy and "priority" not in claim:
            raise AnalysisError(f"{label} missing priority")
        normalized_claim = _normalize_claim_contract(claim, selected_sections, legacy)
        claim = normalized_claim
        normalized["claims"][index] = normalized_claim
        if claim["claimType"] not in allowed_types:
            raise AnalysisError(f"{label} has unsupported claimType")
        if claim.get("section") not in {*selected_sections, "synthesis"}:
            raise AnalysisError(f"{label} section is not selected or synthesis")
        if claim.get("priority") not in {"high", "medium", "low"}:
            raise AnalysisError(f"{label} priority is unsupported")
        if "businessImplication" in claim and not isinstance(claim["businessImplication"], str):
            raise AnalysisError(f"{label} businessImplication must be a string")
        if not legacy and claim["claimType"] == "recommendation" and not isinstance(claim.get("businessImplication"), str):
            raise AnalysisError(f"{label} recommendation requires businessImplication")
        if not isinstance(claim.get("statement"), str) or not claim["statement"].strip():
            raise AnalysisError(f"{label} statement must be nonempty")
        confidence = claim["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise AnalysisError(f"{label} confidence must be a number from 0 to 1")
        evidence_refs = claim["evidenceRefs"]
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise AnalysisError(f"{label} evidenceRefs must be a nonempty list")
        for ref in evidence_refs:
            if not isinstance(ref, dict) or not _ref_is_present(ref, refs):
                raise AnalysisError(f"{label} evidence ref is not present in model input")
            _verify_source_ref(run_dir, ref, f"{label} evidence ref")
        if claim["claimType"] == "calculation":
            if not claim.get("formula") or not isinstance(claim.get("inputs"), list) or not claim["inputs"]:
                raise AnalysisError(f"{label} calculation requires formula and inputs")
            declared = spec.get("calculations", [])
            declared_by_id = {item.get("id"): item for item in declared if isinstance(item, dict)}
            declared_item = declared_by_id.get(claim.get("calculationId"))
            if declared_item is None:
                raise AnalysisError(f"{label} calculation is not declared")
            formula_name = claim["formula"].get("name") if isinstance(claim["formula"], dict) else claim["formula"]
            claim_inputs = [item.get("field") if isinstance(item, dict) else item for item in claim["inputs"]]
            if formula_name != declared_item.get("formula") or claim_inputs != declared_item.get("inputs"):
                raise AnalysisError(f"{label} calculation does not match declared contract")
    return normalized


def validate_analysis_input(run_dir: Path, fact_pack_dir: Path, model_input: dict[str, Any], spec: dict[str, Any], registry: dict[str, Any]) -> None:
    expected_evidence = slice_fact_pack(run_dir, fact_pack_dir, spec, registry)
    expected = compile_model_input(
        run_dir,
        spec,
        expected_evidence,
        registry,
        request_text=model_input.get("requestText") if "requestText" in model_input else None,
    )
    if json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(model_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")):
        raise AnalysisError("analysis input does not match fact slice")


def _mark_model_awaiting(analysis_dir: Path, error: str) -> dict[str, Any]:
    receipt_path = Path(analysis_dir) / "analysis-receipt.json"
    receipt = _read_json(receipt_path, "analysis receipt")
    errors = list(receipt.get("errors", []))
    errors.append(error)
    receipt.update({
        "status": "awaiting_model",
        "errors": errors,
        "validation": {**(receipt.get("validation") or {}), "result": "awaiting_model", "errors": errors},
    })
    _write_json(receipt_path, receipt)
    for report_name in ("report.json", "report.md"):
        (Path(analysis_dir) / report_name).unlink(missing_ok=True)
    return {"status": "awaiting_model", "errors": errors}


def receive_analysis_result(run_dir: Path, result_path: Path, analysis_dir: Path | None = None) -> dict[str, Any]:
    """Receive a model-produced JSON result, then run the offline verifier."""
    run_dir = Path(run_dir).resolve()
    analysis_dir = Path(analysis_dir).resolve() if analysis_dir else run_dir / "analysis"
    result_path = Path(result_path)
    if not result_path.is_file():
        return _mark_model_awaiting(analysis_dir, "analysis result is missing; model handoff is incomplete")
    try:
        result = _read_json(result_path, "analysis result")
    except AnalysisError:
        return _mark_model_awaiting(analysis_dir, "analysis result is not valid JSON; model handoff is incomplete")
    if not isinstance(result, dict):
        return _mark_model_awaiting(analysis_dir, "analysis result must be a JSON object; model handoff is incomplete")

    result_hash, result_size = file_hash_and_size(result_path)
    receipt_path = analysis_dir / "analysis-receipt.json"
    if receipt_path.is_file():
        existing_receipt = _read_json(receipt_path, "analysis receipt")
        if (
            existing_receipt.get("status") == "succeeded"
            and existing_receipt.get("resultSha256") == result_hash
            and (analysis_dir / "analysis-result.json").is_file()
            and (analysis_dir / "report.json").is_file()
            and (analysis_dir / "report.md").is_file()
        ):
            return {
                "status": "succeeded",
                "errors": [],
                "report": str(analysis_dir / "report.json"),
                "reused": True,
            }
    _write_json(analysis_dir / "analysis-result.json", result)
    receipt = _read_json(receipt_path, "analysis receipt")
    receipt.update({"resultSha256": result_hash, "resultSizeBytes": result_size})
    _write_json(receipt_path, receipt)
    verification = verify_analysis(run_dir, analysis_dir)
    if verification.get("status") != "succeeded":
        for report_name in ("report.json", "report.md"):
            (analysis_dir / report_name).unlink(missing_ok=True)
    return verification


def render_reports(output_dir: Path, model_input: dict[str, Any], result: dict[str, Any], receipt: dict[str, Any]) -> None:
    output_dir = Path(output_dir)
    claims = [deepcopy(claim) for claim in result.get("claims", [])]
    claims_by_type = {claim_type: [] for claim_type in ("fact", "calculation", "inference", "recommendation")}
    for claim in claims:
        claims_by_type.setdefault(claim["claimType"], []).append(claim)
    evidence_index = []
    evidence_ids = {}
    for claim in claims:
        for ref in claim.get("evidenceRefs", []):
            key = json.dumps(_source_key(ref), ensure_ascii=False, sort_keys=True)
            if key not in evidence_ids:
                evidence_ids[key] = f"E{len(evidence_index) + 1}"
                evidence_index.append({"id": evidence_ids[key], "ref": deepcopy(ref)})
    claim_audit = []
    for index, claim in enumerate(claims):
        claim_audit.append({
            "id": claim.get("id", f"claim-{index + 1}"),
            "claimType": claim.get("claimType"),
            "section": claim.get("section", "synthesis"),
            "priority": claim.get("priority", "medium"),
            "confidence": claim.get("confidence"),
            "businessImplication": claim.get("businessImplication"),
            "formula": deepcopy(claim.get("formula")) if "formula" in claim else None,
            "inputs": deepcopy(claim.get("inputs")) if "inputs" in claim else None,
            "classificationMode": claim.get("classificationMode", "explicit"),
            "evidenceIds": [evidence_ids[json.dumps(_source_key(ref), ensure_ascii=False, sort_keys=True)] for ref in claim.get("evidenceRefs", [])],
        })
    chapters = [
        {"order": 1, "id": "executive_summary", "title": "核心商业结论"},
        {"order": 2, "id": "recommendations", "title": "优先行动建议"},
        {"order": 3, "id": "material_leaderboard", "title": "素材榜单分析"},
        {"order": 4, "id": "audience", "title": "人群分析"},
        {"order": 5, "id": "scripts", "title": "脚本分析"},
        {"order": 6, "id": "highlight_frames", "title": "高光帧分析"},
        {"order": 7, "id": "synthesis", "title": "跨分区综合洞察"},
        {"order": 8, "id": "limitations", "title": "数据限制与下一步"},
        {"order": 9, "id": "evidence_index", "title": "附录 A：证据索引"},
        {"order": 10, "id": "claim_audit", "title": "附录 B：结论审计"},
    ]
    report = {
        "schemaVersion": 1,
        "status": receipt.get("status"),
        "resultStatus": result.get("status") or receipt.get("resultStatus") or "verified",
        "completionType": "contract_verified" if result.get("status") == "fixture_only" else "model_result_verified",
        "runId": model_input.get("runId"),
        "promptVersion": model_input.get("promptVersion"),
        "analysisSpec": deepcopy(model_input.get("analysisSpec")),
        "analysisSpecFingerprint": model_input.get("analysisSpecFingerprint"),
        "claimsByType": claims_by_type,
        "claimsBySection": {section: [claim for claim in claims if claim.get("section") == section] for section in [*_HANDOFF_SECTIONS, "synthesis"]},
        "evidenceIndex": evidence_index,
        "claimAudit": claim_audit,
        "chapters": chapters,
        "missingItems": deepcopy(receipt.get("missingItems", [])),
        "sourceRefs": deepcopy(model_input.get("sourceRefs", [])),
    }
    _write_json(output_dir / "report.json", report)
    def claim_lines(claims, include_implication=True):
        lines = []
        for claim in claims:
            ids = [evidence_ids[json.dumps(_source_key(ref), ensure_ascii=False, sort_keys=True)] for ref in claim.get("evidenceRefs", [])]
            suffix = f"；业务含义：{claim['businessImplication']}" if include_implication and claim.get("businessImplication") else ""
            lines.append(f"- {claim['statement']}（证据：{', '.join(f'[{item}]' for item in ids)}{suffix}）")
        return lines or ["（无）"]

    selected_sections = set((model_input.get("analysisSpec") or {}).get("sections", []))
    all_claims = claims
    lines = ["# 千川商业分析报告", ""]
    for chapter in chapters:
        lines.extend([f"## {chapter['title']}", ""])
        if chapter["id"] == "executive_summary":
            lines.extend(claim_lines([claim for claim in all_claims if claim.get("section") == "synthesis" and claim.get("priority") == "high"]) + [""])
        elif chapter["id"] == "recommendations":
            recommendations = [claim for claim in all_claims if claim.get("claimType") == "recommendation"]
            recommendations.sort(key=lambda claim: {"high": 0, "medium": 1, "low": 2}.get(claim.get("priority"), 3))
            lines.extend(claim_lines(recommendations) + [""])
        elif chapter["id"] in {"material_leaderboard", "audience", "scripts", "highlight_frames"}:
            chapter_claims = [claim for claim in all_claims if claim.get("section") == chapter["id"] and claim.get("claimType") != "recommendation"]
            lines.extend(["关键发现", "", *claim_lines(chapter_claims), "", "分区建议", "", *claim_lines([claim for claim in all_claims if claim.get("section") == chapter["id"] and claim.get("claimType") == "recommendation"]), ""])
        elif chapter["id"] == "synthesis":
            lines.extend(claim_lines([claim for claim in all_claims if claim.get("section") == "synthesis"]) + [""])
        elif chapter["id"] == "limitations":
            lines.extend([f"- 缺失项：{len(report['missingItems'])}", f"- 可追溯证据：{len(evidence_index)} 条", ""])
        elif chapter["id"] == "evidence_index":
            lines.extend([f"- [{item['id']}] {item['ref'].get('path', '')}" for item in evidence_index] or ["（无）"])
            lines.append("")
        else:
            lines.extend([f"- {item['id']}：{item['claimType']} / {item['section']} / {item['priority']} / confidence={item['confidence']} / evidence={', '.join(item['evidenceIds'])}" for item in claim_audit] or ["（无）"])
            lines.append("")
    _write_text(output_dir / "report.md", "\n".join(lines))


def verify_analysis(run_dir: Path, analysis_dir: Path | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    analysis_dir = Path(analysis_dir).resolve() if analysis_dir else run_dir / "analysis"
    receipt_path = analysis_dir / "analysis-receipt.json"
    try:
        receipt = _read_json(receipt_path, "analysis receipt")
        model_input = _read_json(analysis_dir / "analysis-input.json", "analysis input")
        spec = _read_json(analysis_dir / "analysis-spec.json", "analysis spec")
        fact_result = verify_fact_pack(run_dir, run_dir / "fact-pack")
        errors: list[str] = []
        if fact_result.get("status") != "succeeded":
            errors.append("fact pack is incomplete")
        if model_input.get("analysisSpecFingerprint") != analysis_fingerprint(spec):
            errors.append("analysis input fingerprint mismatch")
        try:
            checked_refs = set()
            for index, ref in enumerate(model_input.get("sourceRefs", [])):
                cache_key = (ref.get("path"), ref.get("sha256"), ref.get("sizeBytes")) if isinstance(ref, dict) else None
                if cache_key not in checked_refs:
                    _verify_source_ref(run_dir, ref, f"analysis input sourceRefs[{index}]")
                    checked_refs.add(cache_key)
        except AnalysisError as exc:
            errors.append(str(exc))
        try:
            registry = load_analysis_registry(Path(__file__).resolve().parents[1] / "config" / "analysis-registry-v1.json")
            validate_analysis_input(run_dir, run_dir / "fact-pack", model_input, spec, registry)
        except AnalysisError as exc:
            errors.append(str(exc))
        if model_input.get("missingItems"):
            errors.append("analysis input has missing items; commercial analysis is incomplete")
        base_errors = list(errors)
        result_path = analysis_dir / "analysis-result.json"
        model_waiting = False
        if not result_path.is_file():
            errors.append("analysis-result.json is missing; model handoff is incomplete")
            model_waiting = True
        else:
            try:
                result = _read_json(result_path, "analysis result")
            except AnalysisError as exc:
                errors.append(str(exc))
                model_waiting = True
            else:
                try:
                    result = validate_analysis_result(result, model_input, spec, run_dir)
                except AnalysisError as exc:
                    errors.append(str(exc))
        if errors:
            if model_waiting and not base_errors:
                status = "awaiting_model"
            else:
                status = "incomplete" if model_input.get("missingItems") and len(errors) == 1 else "failed"
            receipt.update({"status": status, "validation": {"factPack": fact_result, "errors": errors}})
            _write_json(receipt_path, receipt)
            return {"status": status, "errors": errors}
        receipt.update({"status": "succeeded", "resultStatus": result.get("status") or "verified", "errors": [], "validation": {"factPack": fact_result, "result": "succeeded"}, "missingItems": model_input.get("missingItems", [])})
        _write_json(receipt_path, receipt)
        render_reports(analysis_dir, model_input, result, receipt)
        return {"status": "succeeded", "errors": [], "report": str(analysis_dir / "report.json")}
    except (AnalysisError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return {"status": "failed", "errors": [str(exc)]}
