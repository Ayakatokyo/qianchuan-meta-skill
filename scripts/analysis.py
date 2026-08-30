"""Strict, offline, source-linked commercial analysis contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from fact_pack import verify_fact_pack


class AnalysisError(ValueError):
    """Raised when an analysis request or result violates the contract."""


_HANDOFF_SECTIONS = ("material_leaderboard", "audience", "scripts", "highlight_frames")
_SECTION_ALIASES = {"script": "scripts"}


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
            "selection", "factPackPath", "manifestSummary", "sourceRefs",
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


def analysis_fingerprint(spec: dict[str, Any]) -> str:
    encoded = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


PROMPT_VERSION = "qianchuan-analysis-prompt-v1"

_MODEL_RESULT_SCHEMA_SUMMARY = {
    "schemaVersion": 1,
    "type": "object",
    "required": ["schemaVersion", "analysisSpecFingerprint", "claims"],
    "claimRequired": ["claimType", "statement", "evidenceRefs", "confidence"],
    "claimTypes": ["fact", "calculation", "inference", "recommendation"],
    "calculationRequired": ["calculationId", "formula", "inputs"],
}


def _json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _path_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _source_key(ref: dict[str, Any]) -> tuple[Any, ...]:
    return (ref.get("path"), ref.get("recordIndex"))


def _append_ref(refs: list[dict[str, Any]], ref: Any) -> None:
    if isinstance(ref, dict) and isinstance(ref.get("path"), str) and _source_key(ref) not in {_source_key(item) for item in refs}:
        refs.append(deepcopy(ref))


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
    }


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
        "modelConstraints": {
            "allowedSections": list(spec["sections"]),
            "allowedMaterialIds": list(spec["selectedMaterialIds"]),
            "onlyUseProvidedEvidence": True,
            "doNotUseExternalData": True,
            "doNotFillMissingData": True,
            "allClaimsRequireEvidenceRefs": True,
        },
        "resultSchema": deepcopy(_MODEL_RESULT_SCHEMA_SUMMARY),
        "missingItems": deepcopy(evidence["missingItems"]),
        "handoff": {
            "modelMustReturn": "analysis-result.json",
            "claimTypes": ["fact", "calculation", "inference", "recommendation"],
            "allClaimsRequire": ["claimType", "evidenceRefs", "confidence"],
        },
    }
    if request_text is not None:
        model_input["requestText"] = request_text
    return model_input


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    data = path.read_bytes()
    relative = path.resolve().relative_to(Path(run_dir).resolve()).as_posix()
    return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data)}


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
    return _manifest_ref(run_dir, fact_pack_dir)["sha256"]


def prepare_analysis(run_dir: Path, request_path: Path, output_dir: Path | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    fact_pack_dir = run_dir / "fact-pack"
    output_dir = Path(output_dir).resolve() if output_dir else run_dir / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    request = _read_json(Path(request_path), "analysis request")
    if not isinstance(request, dict):
        raise AnalysisError("analysis request must be an object")
    if request.get("stage") == "analysis" or any(
        key in request for key in ("runId", "factPackManifestSha256", "materialScope")
    ):
        request = validate_analysis_handoff(request, fact_pack_dir)
    _write_json(output_dir / "analysis-request.json", _safe_request(request))
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
    _write_json(output_dir / "analysis-spec.json", spec)
    _write_json(output_dir / "analysis-input.json", model_input)
    status = "awaiting_model_with_missing_data" if evidence["missingItems"] else "awaiting_model"
    receipt = {
        "schemaVersion": 1,
        "runId": run_dir.name,
        "stage": "analysis_prepared",
        "factPackFingerprint": _fact_pack_fingerprint(run_dir, fact_pack_dir),
        "factPackManifestSha256": _fact_pack_fingerprint(run_dir, fact_pack_dir),
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
    if not path.is_file() or path.stat().st_size != ref.get("sizeBytes") or hashlib.sha256(path.read_bytes()).hexdigest() != ref.get("sha256"):
        raise AnalysisError(f"{label} hash does not match source")


def validate_analysis_result(result: dict[str, Any], model_input: dict[str, Any], spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    result = _require_object(result, "analysis result")
    if result.get("schemaVersion") != 1:
        raise AnalysisError("analysis result schemaVersion must be 1")
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
    for index, claim in enumerate(claims):
        label = f"claim[{index}]"
        claim = _require_object(claim, label)
        for required in ("claimType", "evidenceRefs", "confidence"):
            if required not in claim:
                raise AnalysisError(f"{label} missing {required}")
        if claim["claimType"] not in allowed_types:
            raise AnalysisError(f"{label} has unsupported claimType")
        if not isinstance(claim.get("statement"), str) or not claim["statement"].strip():
            raise AnalysisError(f"{label} statement must be nonempty")
        confidence = claim["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise AnalysisError(f"{label} confidence must be a number from 0 to 1")
        evidence_refs = claim["evidenceRefs"]
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise AnalysisError(f"{label} evidenceRefs must be a nonempty list")
        for ref in evidence_refs:
            if not isinstance(ref, dict) or _source_key(ref) not in refs or refs[_source_key(ref)] != ref:
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

    _write_json(analysis_dir / "analysis-result.json", result)
    verification = verify_analysis(run_dir, analysis_dir)
    if verification.get("status") != "succeeded":
        for report_name in ("report.json", "report.md"):
            (analysis_dir / report_name).unlink(missing_ok=True)
    return verification


def render_reports(output_dir: Path, model_input: dict[str, Any], result: dict[str, Any], receipt: dict[str, Any]) -> None:
    output_dir = Path(output_dir)
    claims_by_type = {claim_type: [] for claim_type in ("fact", "calculation", "inference", "recommendation")}
    for claim in result.get("claims", []):
        claims_by_type.setdefault(claim["claimType"], []).append(deepcopy(claim))
    report = {
        "schemaVersion": 1,
        "status": receipt.get("status"),
        "resultStatus": result.get("status", "unknown"),
        "completionType": "contract_verified" if result.get("status") == "fixture_only" else "model_result_verified",
        "runId": model_input.get("runId"),
        "promptVersion": model_input.get("promptVersion"),
        "analysisSpec": deepcopy(model_input.get("analysisSpec")),
        "analysisSpecFingerprint": model_input.get("analysisSpecFingerprint"),
        "claimsByType": claims_by_type,
        "missingItems": deepcopy(receipt.get("missingItems", [])),
        "sourceRefs": deepcopy(model_input.get("sourceRefs", [])),
    }
    _write_json(output_dir / "report.json", report)
    lines = ["# 千川商业分析报告", "", f"- 校验状态：{report['status']}", f"- 结果状态：{report['resultStatus']}", f"- 分析指纹：{report['analysisSpecFingerprint']}", ""]
    for claim_type, label in (("fact", "事实"), ("calculation", "计算"), ("inference", "AI 推断"), ("recommendation", "建议")):
        lines.extend([f"## {label}", ""])
        claims = claims_by_type[claim_type]
        if not claims:
            lines.append("（无）")
        for claim in claims:
            refs = ", ".join(ref.get("path", "") for ref in claim.get("evidenceRefs", []))
            lines.append(f"- {claim['statement']}（confidence={claim['confidence']}；evidence={refs}）")
        lines.append("")
    if report["missingItems"]:
        lines.extend(["## 缺失项", "", *[f"- {item}" for item in report["missingItems"]], ""])
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


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
        receipt.update({"status": "succeeded", "resultStatus": result.get("status", "unknown"), "errors": [], "validation": {"factPack": fact_result, "result": "succeeded"}, "missingItems": model_input.get("missingItems", [])})
        _write_json(receipt_path, receipt)
        render_reports(analysis_dir, model_input, result, receipt)
        return {"status": "succeeded", "errors": [], "report": str(analysis_dir / "report.json")}
    except (AnalysisError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return {"status": "failed", "errors": [str(exc)]}
