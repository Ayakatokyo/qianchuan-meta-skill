"""Project manifest-bound highlight-frame facts for the offline overview."""

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

try:
    from command_io import CommandFileCache
except ImportError:  # pragma: no cover
    from .command_io import CommandFileCache


SCHEMA_VERSION = 1
_REQUIRED_FACT_FILES = {"manifest.json", "material-leaderboard.json", "highlight-frames.json"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_SAFE_HASH = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_TEXT = re.compile(r"(?:https?|file)://|(?:^|[\\/])(?:Users|home|var|tmp)(?:[\\/]|$)", re.IGNORECASE)
_METRIC_LABELS = {
    "CLICK_COUNT": "整体点击次数",
    "LOSE_COUNT": "整体流失数",
    "LIKE_COUNT": "整体点赞次数",
    "COMMENT_COUNT": "整体评论次数",
    "SHARE_COUNT": "整体转发次数",
    "NEW_FOLLOWER_COUNT": "整体新增粉丝数",
}
class HighlightOverviewError(ValueError):
    """Raised when persisted highlight facts cannot be safely projected."""


def _read_json(path, label, io_cache=None):
    try:
        value = io_cache.read_json(Path(path)) if io_cache is not None else json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HighlightOverviewError("%s is unreadable" % label) from exc
    if not isinstance(value, dict):
        raise HighlightOverviewError("%s must be an object" % label)
    return value


def _safe_relative_path(run_dir, raw_path, label):
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HighlightOverviewError("%s path must be relative" % label)
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in raw_path:
        raise HighlightOverviewError("%s path is not relative" % label)
    candidate = (Path(run_dir) / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(Path(run_dir).resolve())
    except ValueError as exc:
        raise HighlightOverviewError("%s path escapes run directory" % label) from exc
    return candidate, relative.as_posix()


def _verified_file(run_dir, reference, label, *, require_image=False, io_cache=None):
    if not isinstance(reference, dict):
        raise HighlightOverviewError("%s must be a file reference" % label)
    expected_hash = reference.get("sha256")
    expected_size = reference.get("sizeBytes")
    if not isinstance(expected_hash, str) or _SAFE_HASH.fullmatch(expected_hash) is None:
        raise HighlightOverviewError("%s hash is invalid" % label)
    if type(expected_size) is not int or expected_size <= 0:
        raise HighlightOverviewError("%s size is invalid" % label)
    path, relative = _safe_relative_path(run_dir, reference.get("path"), label)
    if require_image and path.suffix.lower() not in _IMAGE_SUFFIXES:
        raise HighlightOverviewError("%s must be an image" % label)
    try:
        if io_cache is not None:
            actual_hash, actual_size = io_cache.verify(path, reference)
        else:
            actual_size = path.stat().st_size
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise HighlightOverviewError("%s is missing" % label) from exc
    except ValueError as exc:
        raise HighlightOverviewError("%s hash or size mismatch" % label) from exc
    if actual_size != expected_size or actual_hash != expected_hash:
        raise HighlightOverviewError("%s hash or size mismatch" % label)
    return path, relative, actual_hash, actual_size


def _verify_manifest_sources(run_dir, manifest, io_cache=None):
    refs = manifest.get("sourceFiles")
    if not isinstance(refs, list):
        raise HighlightOverviewError("manifest sourceFiles must be a list")
    verified = {}
    for index, reference in enumerate(refs):
        _path, relative, actual_hash, actual_size = _verified_file(
            run_dir, reference, "manifest sourceFiles[%d]" % index, io_cache=io_cache
        )
        previous = verified.get(relative)
        current = (actual_hash, actual_size)
        if previous is not None and previous != current:
            raise HighlightOverviewError("manifest source file has conflicting hashes")
        verified[relative] = current
    return verified


def _safe_scalar(value, label):
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
        raise HighlightOverviewError("%s is invalid" % label)
    if isinstance(value, float) and not math.isfinite(value):
        raise HighlightOverviewError("%s is invalid" % label)
    if isinstance(value, str) and (not value.strip() or "\n" in value or "\r" in value or _FORBIDDEN_TEXT.search(value)):
        raise HighlightOverviewError("%s is invalid" % label)
    return value


def _material_titles(run_dir, manifest, io_cache=None):
    payload_path = Path(run_dir) / "fact-pack" / "material-leaderboard.json"
    payload = _read_json(payload_path, "material leaderboard", io_cache)
    materials = payload.get("materials")
    material_ids = manifest.get("materialIds")
    if not isinstance(materials, list) or not isinstance(material_ids, list):
        raise HighlightOverviewError("material leaderboard is invalid")
    allowed_ids = {str(value) for value in material_ids}
    titles = {}
    ranks = {}
    for item in materials:
        if not isinstance(item, dict):
            raise HighlightOverviewError("material leaderboard item is invalid")
        material_id = item.get("materialId")
        title = item.get("materialName")
        rank = item.get("rank")
        if not isinstance(material_id, (str, int)) or not str(material_id).strip() or str(material_id) not in allowed_ids:
            raise HighlightOverviewError("material leaderboard material is invalid")
        if not isinstance(title, str) or not title.strip() or _FORBIDDEN_TEXT.search(title):
            raise HighlightOverviewError("material title is required")
        if type(rank) is not int or rank < 1:
            raise HighlightOverviewError("material rank is invalid")
        key = str(material_id).strip()
        if key in titles:
            raise HighlightOverviewError("material leaderboard has duplicate material")
        titles[key] = title.strip()
        ranks[key] = rank
    if set(titles) != allowed_ids or len(materials) != manifest.get("counts", {}).get("materials"):
        raise HighlightOverviewError("material leaderboard does not match manifest")
    return titles, ranks


def _selection_evidence(value, duration):
    if not isinstance(value, list) or not value:
        raise HighlightOverviewError("selection evidence is invalid")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise HighlightOverviewError("selection evidence is invalid")
        if item.get("metric") == "ZERO_SECOND" and item.get("mode") == "zero_second":
            if (
                duration != 0
                or item.get("metricValue") is not None
                or item.get("policyRank") != 0
                or "selectionRank" in item
                or item.get("durationSec") != 0
            ):
                raise HighlightOverviewError("cover selection evidence is invalid")
            continue
        metric = _safe_scalar(item.get("metric"), "selection evidence metric")
        mode = _safe_scalar(item.get("mode"), "selection evidence mode")
        metric_value = _safe_scalar(item.get("metricValue"), "selection evidence value")
        selection_rank = item.get("selectionRank")
        policy_rank = item.get("policyRank")
        if type(selection_rank) is not int or selection_rank < 1 or type(policy_rank) is not int or policy_rank < 1:
            raise HighlightOverviewError("selection evidence rank is invalid")
        result.append({
            "metric": metric,
            "metricLabel": _METRIC_LABELS.get(str(metric), "指标"),
            "value": metric_value,
            "mode": mode,
            "rank": selection_rank,
            "policyRank": policy_rank,
        })
    return result


def _selection_condition(item):
    metric_label = item["metricLabel"].removeprefix("整体")
    mode = item["mode"]
    if mode == "local_peaks":
        return "%s波峰 Top%d" % (metric_label, item["rank"])
    if mode == "global_top_values":
        return "%s数值 Top%d" % (metric_label, item["rank"])
    return "%s Top%d" % (metric_label, item["rank"])


def _selection_conditions(duration, evidence):
    conditions = ["封面画面"] if duration == 0 else []
    conditions.extend(_selection_condition(item) for item in evidence)
    return conditions


def _frame_card(run_dir, frame, titles, ranks, manifest_source_paths, io_cache=None):
    if not isinstance(frame, dict):
        raise HighlightOverviewError("highlight frame is invalid")
    material_id = str(frame.get("materialId", "")).strip()
    if material_id not in titles:
        raise HighlightOverviewError("highlight frame material is invalid")
    duration = frame.get("durationSec")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        raise HighlightOverviewError("highlight frame second is invalid")
    sequence = frame.get("frameSequence")
    if type(sequence) is not int or sequence < 1:
        raise HighlightOverviewError("highlight frame sequence is invalid")
    artifact = frame.get("artifact")
    path, relative, actual_hash, actual_size = _verified_file(
        run_dir, artifact, "highlight frame artifact", require_image=True, io_cache=io_cache
    )
    if path.parent.name != material_id or "peak-frames" not in path.parts:
        raise HighlightOverviewError("highlight frame artifact material is invalid")
    if relative not in manifest_source_paths:
        raise HighlightOverviewError("highlight frame artifact is not manifest-bound")
    evidence = _selection_evidence(frame.get("selectionEvidence"), duration)
    selection_conditions = _selection_conditions(duration, evidence)
    card_id = "%s:%d" % (material_id, sequence)
    return {
        "id": card_id,
        "materialId": material_id,
        "materialRank": ranks[material_id],
        "second": duration,
        "frameSequence": sequence,
        "role": "视频开场画面" if duration == 0 else "指标高光帧",
        # Keep the legacy summary field for consumers that still read it; the
        # overview UI renders the complete selectionConditions list instead.
        "description": selection_conditions[0],
        "selectionConditions": selection_conditions,
        "selectionEvidence": evidence,
        "materialTitle": titles[material_id],
        "artifact": {"path": relative, "sha256": actual_hash, "sizeBytes": actual_size},
    }


def _build_highlight_overview(run_dir, io_cache=None):
    """Build a safe, deterministic projection from a completed run's fact pack."""
    run_root = Path(run_dir).resolve()
    io_cache = io_cache or CommandFileCache(run_root)
    fact_dir = run_root / "fact-pack"
    manifest = _read_json(fact_dir / "manifest.json", "fact-pack manifest", io_cache)
    declared_files = manifest.get("files")
    if not isinstance(declared_files, list) or not _REQUIRED_FACT_FILES.issubset(set(declared_files)):
        raise HighlightOverviewError("fact-pack manifest files are incomplete")
    source_paths = _verify_manifest_sources(run_root, manifest, io_cache)
    titles, ranks = _material_titles(run_root, manifest, io_cache)
    highlights_path = fact_dir / "highlight-frames.json"
    highlights = _read_json(highlights_path, "highlight frames", io_cache)
    if highlights.get("status") != "available" or not isinstance(highlights.get("highlightFrames"), list):
        raise HighlightOverviewError("highlight frames are unavailable")
    expected_count = manifest.get("counts", {}).get("highlightFrames")
    frames = highlights["highlightFrames"]
    if type(expected_count) is not int or expected_count != len(frames):
        raise HighlightOverviewError("highlight frame count does not match manifest")
    cards = [_frame_card(run_root, frame, titles, ranks, source_paths, io_cache) for frame in frames]
    if len({card["id"] for card in cards}) != len(cards):
        raise HighlightOverviewError("highlight frame IDs are not unique")
    cards.sort(key=lambda card: (card["materialRank"], card["second"], card["frameSequence"], card["materialId"]))
    source_hash, source_size = io_cache.digest(highlights_path)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "available",
        "source": {
            "path": "fact-pack/highlight-frames.json",
            "sha256": source_hash,
            "sizeBytes": source_size,
            "recordCount": len(cards),
        },
        "cards": cards,
    }


def build_highlight_overview(run_dir, io_cache=None):
    try:
        return _build_highlight_overview(run_dir, io_cache)
    except HighlightOverviewError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise HighlightOverviewError("highlight facts are invalid") from exc
