"""Build a source-verified, aggregate-only script formula overview."""

import hashlib
import json
from pathlib import Path, PurePosixPath


class ScriptFormulaOverviewError(ValueError):
    """Raised when script facts cannot be trusted for presentation."""


def _read_json(path, label, expected_type=None):
    try:
        payload = Path(path).read_text(encoding="utf-8")
        value = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScriptFormulaOverviewError("%s is unreadable" % label) from exc
    if expected_type is not None and not isinstance(value, expected_type):
        expected = "object" if expected_type is dict else "list"
        raise ScriptFormulaOverviewError("%s must be a %s" % (label, expected))
    return value


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ScriptFormulaOverviewError("%s is required" % label)
    return value.strip()


def _safe_relative_path(run_root, value, label):
    path_text = _required_text(value, label)
    relative = PurePosixPath(path_text)
    if relative.is_absolute() or "\\" in path_text or any(part in {"", ".", ".."} for part in relative.parts):
        raise ScriptFormulaOverviewError("%s must be a safe relative path" % label)
    resolved = (run_root / Path(*relative.parts)).resolve()
    try:
        resolved.relative_to(run_root.resolve())
    except ValueError as exc:
        raise ScriptFormulaOverviewError("%s must remain inside run" % label) from exc
    return relative.as_posix(), resolved


def _verify_source_ref(run_root, ref, label, known=None):
    if not isinstance(ref, dict):
        raise ScriptFormulaOverviewError("%s must be an object" % label)
    path_text, path = _safe_relative_path(run_root, ref.get("path"), "%s path" % label)
    if not path_text.startswith("rpa/diagnosis-records/") or not path_text.endswith(".json"):
        raise ScriptFormulaOverviewError("%s must point to a diagnosis record" % label)
    if known is not None and path_text not in known:
        raise ScriptFormulaOverviewError("%s is not declared" % label)
    expected_hash = _required_text(ref.get("sha256"), "%s sha256" % label)
    if len(expected_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_hash):
        raise ScriptFormulaOverviewError("%s sha256 is invalid" % label)
    expected_size = ref.get("sizeBytes")
    if type(expected_size) is not int or expected_size < 0:
        raise ScriptFormulaOverviewError("%s sizeBytes is invalid" % label)
    if not path.is_file():
        raise ScriptFormulaOverviewError("%s file is missing" % label)
    actual_size = path.stat().st_size
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_size != expected_size:
        raise ScriptFormulaOverviewError("%s size mismatch" % label)
    if actual_hash != expected_hash:
        raise ScriptFormulaOverviewError("%s sha256 mismatch" % label)
    return path_text, expected_hash, expected_size


def _manifest_scope(manifest):
    quality = manifest.get("quality")
    statuses = quality.get("categoryStatuses") if isinstance(quality, dict) else None
    if not isinstance(statuses, dict) or statuses.get("scripts") != "available":
        raise ScriptFormulaOverviewError("manifest scripts status is unavailable")
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or type(counts.get("scriptFacts")) is not int:
        raise ScriptFormulaOverviewError("manifest scriptFacts count is missing")
    material_ids = manifest.get("materialIds")
    if not isinstance(material_ids, list) or not material_ids:
        raise ScriptFormulaOverviewError("manifest materialIds are missing")
    normalized = []
    for material_id in material_ids:
        value = _required_text(material_id, "manifest materialId")
        if value in normalized:
            raise ScriptFormulaOverviewError("manifest materialIds are duplicated")
        normalized.append(value)
    if counts.get("materials") is not None and counts.get("materials") != len(normalized):
        raise ScriptFormulaOverviewError("manifest materials count does not match materialIds")
    return counts["scriptFacts"], normalized


def _verified_source_refs(run_root, payload):
    refs = payload.get("sourceRefs")
    if not isinstance(refs, list) or not refs:
        raise ScriptFormulaOverviewError("scripts sourceRefs are missing")
    known = {}
    for index, ref in enumerate(refs):
        path_text, expected_hash, expected_size = _verify_source_ref(run_root, ref, "scripts sourceRef %d" % index)
        declared = (expected_hash, expected_size)
        if path_text in known and known[path_text] != declared:
            raise ScriptFormulaOverviewError("scripts sourceRef declarations disagree")
        known[path_text] = declared
    return known


def _label(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bucket_list(material_sets, total):
    buckets = []
    for label, materials in material_sets.items():
        value = len(materials)
        buckets.append({
            "label": label,
            "value": value,
            "share": value / total if total else 0,
            "total": total,
        })
    return sorted(buckets, key=lambda item: (-item["value"], item["label"]))


def build_script_formula_overview(run_dir: Path) -> dict:
    """Project only verified structured formula labels into safe aggregates."""
    run_root = Path(run_dir).resolve()
    if not run_root.is_dir():
        raise ScriptFormulaOverviewError("run directory is missing")
    fact_path = run_root / "fact-pack" / "scripts.json"
    manifest_path = run_root / "fact-pack" / "manifest.json"
    payload = _read_json(fact_path, "script facts", dict)
    manifest = _read_json(manifest_path, "fact-pack manifest", dict)
    expected_count, material_ids = _manifest_scope(manifest)
    records = payload.get("scripts")
    if payload.get("status") != "available" or not isinstance(records, list):
        raise ScriptFormulaOverviewError("script facts are unavailable")
    if expected_count != len(records):
        raise ScriptFormulaOverviewError("manifest scriptFacts count does not match script facts")
    manifest_ids = set(material_ids)
    record_ids = [_required_text(record.get("materialId"), "script materialId") for record in records if isinstance(record, dict)]
    if len(record_ids) != len(records) or len(set(record_ids)) != len(record_ids) or set(record_ids) != manifest_ids:
        raise ScriptFormulaOverviewError("script materialIds do not match manifest materialIds")

    source_refs = _verified_source_refs(run_root, payload)
    primary_materials = {}
    secondary_materials = {}
    primary_pairs = set()
    missing_secondary_pairs = set()
    gap_entries = set()

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ScriptFormulaOverviewError("script record %d must be an object" % index)
        material_id = _required_text(record.get("materialId"), "script materialId")
        record_ref = record.get("sourceRef")
        ref_path, ref_hash, ref_size = _verify_source_ref(
            run_root, record_ref, "script record %d sourceRef" % index, source_refs
        )
        if source_refs[ref_path] != (ref_hash, ref_size):
            raise ScriptFormulaOverviewError("script record sourceRef disagrees")

        parsed = record.get("parseStatus") == "parsed"
        completeness = record.get("completeness")
        formula_complete = isinstance(completeness, dict) and completeness.get("formulaLabels") is True
        data = record.get("data")
        if not parsed or not formula_complete:
            gap_code = "formula_labels_unavailable"
            gap_entries.add((gap_code, material_id, index))
            continue
        if not isinstance(data, list):
            data = []

        valid_primary_count = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            primary_node = item.get("level_1_label")
            primary = _label(primary_node.get("label") if isinstance(primary_node, dict) else None)
            if primary is None:
                continue
            valid_primary_count += 1
            primary_pairs.add((material_id, primary))
            primary_materials.setdefault(primary, set()).add(material_id)
            secondary_values = item.get("level_2_labels")
            valid_secondaries = set()
            if isinstance(secondary_values, list):
                for secondary_node in secondary_values:
                    if isinstance(secondary_node, dict):
                        secondary = _label(secondary_node.get("label"))
                        if secondary is not None:
                            valid_secondaries.add(secondary)
            if not valid_secondaries:
                missing_secondary_pairs.add((material_id, primary))
            for secondary in valid_secondaries:
                secondary_materials.setdefault(primary, {}).setdefault(secondary, set()).add(material_id)

        if valid_primary_count == 0:
            gap_entries.add(("formula_labels_empty", material_id, index))

    gaps = []
    for code in ("formula_labels_unavailable", "formula_labels_empty"):
        count = len({entry for entry in gap_entries if entry[0] == code})
        if count:
            gaps.append({"code": code, "count": count})
    missing_secondary_count = len(missing_secondary_pairs)
    if missing_secondary_count:
        gaps.append({"code": "missing_level_2_label", "count": missing_secondary_count})

    script_hash = hashlib.sha256(fact_path.read_bytes()).hexdigest()
    primary_buckets = _bucket_list(primary_materials, len(set(material_id for material_id, _ in primary_pairs)))
    primary_totals = {bucket["label"]: bucket["total"] for bucket in primary_buckets}
    secondary_by_primary = {}
    for primary in (bucket["label"] for bucket in primary_buckets):
        secondary_by_primary[primary] = _bucket_list(secondary_materials.get(primary, {}), primary_totals[primary])

    return {
        "schemaVersion": 1,
        "status": "available",
        "source": {
            "path": "fact-pack/scripts.json",
            "sha256": script_hash,
            "recordCount": len(records),
        },
        "materialScope": {"count": len(material_ids)},
        "coverage": {"formulaMaterialCount": len(set(material_id for material_id, _ in primary_pairs))},
        "primaryBuckets": primary_buckets,
        "secondaryBucketsByPrimary": secondary_by_primary,
        "gaps": gaps,
    }
