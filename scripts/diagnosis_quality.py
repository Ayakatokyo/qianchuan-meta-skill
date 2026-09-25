"""Compare report identities with RPA content without treating request IDs as proof."""

import hashlib
import json


_CONTENT_TYPES = ("crowd_analysis", "script_analysis", "interaction_series")
_IGNORED_KEYS = {"materialId", "videoUrl", "thumbnailUrl", "authorization", "localPath"}


def _digest(rows):
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in sorted(value.items()) if key not in _IGNORED_KEYS}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    payload = json.dumps(stable(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assess_diagnosis_attribution(materials, diagnosis):
    """Flag a repeated diagnostic body when its named source material is different.

    A shared video alone is insufficient: creators may reuse one video in several ads.
    The RPA ``materialId`` is intentionally excluded because it may be request-derived.
    """
    by_id = {str(item["materialId"]): item for item in materials}
    groups = {}
    for material_id, (_path, records) in diagnosis.items():
        info = next((row for row in records if row.get("recordType") == "material_info"), {})
        uri = str(info.get("materialUri") or "").strip()
        name = str(info.get("materialName") or "").strip()
        if not uri or not name:
            continue
        content = {}
        for kind in _CONTENT_TYPES:
            rows = [row for row in records if row.get("recordType") == kind]
            if rows:
                content[kind] = _digest(rows)
        groups.setdefault((uri, name), []).append((material_id, content))

    suspect = set()
    evidence = []
    for (_uri, diagnostic_name), members in groups.items():
        if len(members) < 2:
            continue
        common_types = [kind for kind in _CONTENT_TYPES if all(kind in content for _, content in members)]
        shared_types = [kind for kind in common_types if len({content[kind] for _, content in members}) == 1]
        if len(common_types) < 2 or len(shared_types) != len(common_types):
            continue
        ids = [material_id for material_id, _ in members]
        source_names = {material_id: str(by_id[material_id].get("materialName") or "").strip() for material_id in ids}
        if len(set(source_names.values())) < 2:
            continue
        matches = [material_id for material_id in ids if source_names[material_id] == diagnostic_name]
        excluded = [material_id for material_id in ids if material_id not in matches] if len(matches) == 1 else ids
        suspect.update(excluded)
        evidence.append({
            "code": "diagnosis_attribution_unverified",
            "materialIds": ids,
            "excludedMaterialIds": excluded,
            "matchingContentTypes": shared_types,
            "message": "报表素材 ID 与名称各自独立；跨素材诊断内容高度一致，部分诊断事实无法归属。",
        })
    return {"status": "suspect" if suspect else "clear", "excludedMaterialIds": sorted(suspect), "evidence": evidence}
