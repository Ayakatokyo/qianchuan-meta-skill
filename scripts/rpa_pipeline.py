"""Resumable, bounded RPA material processing without report-generation concerns."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from frame_extractor import extract_highlight_frames
from path_safety import artifact_path, validate_material_id
from rpa_adapter import DEFAULT_VIDEO_MAX_BYTES, DEFAULT_DOWNLOAD_DEADLINE_SEC, download_file


DEFAULT_RPA_OPTIONS = {
    "rpaConcurrency": 3,
    "materialTimeoutSec": 600,
    "csvWaitWindowSec": 120,
    "recoveryAttempts": 2,
    "recoveryBackoffSec": 2,
    "csvMaxBytes": 100 * 1024 * 1024,
    "videoMaxBytes": 1024 * 1024 * 1024,
    "downloadDeadlineSec": 600,
}


class RpaPipelineError(RuntimeError):
    def __init__(self, message, error_type="rpa_pipeline_error"):
        super().__init__(message)
        self.error_type = error_type


def resolve_rpa_options(overrides=None):
    options = dict(DEFAULT_RPA_OPTIONS)
    options.update(overrides or {})
    positive_integer_fields = ("rpaConcurrency", "materialTimeoutSec", "csvWaitWindowSec", "recoveryAttempts", "csvMaxBytes", "videoMaxBytes", "downloadDeadlineSec")
    for field in positive_integer_fields:
        if type(options.get(field)) is not int or options[field] <= 0:
            raise RpaPipelineError("RPA option %s must be a positive integer" % field, "invalid_rpa_options")
    if not isinstance(options.get("recoveryBackoffSec"), (int, float)) or options["recoveryBackoffSec"] < 0:
        raise RpaPipelineError("RPA option recoveryBackoffSec must be non-negative", "invalid_rpa_options")
    return options


def _write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _copy_file_atomically(source, target):
    source = Path(source)
    if not source.is_file() or source.stat().st_size <= 0:
        raise RpaPipelineError("rpa_csv_artifact_unavailable: downloaded CSV is missing", "rpa_csv_artifact_unavailable")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent))
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            while chunk := input_handle.read(1024 * 1024):
                output_handle.write(chunk)
        os.replace(temporary, target)
    except OSError as exc:
        Path(temporary).unlink(missing_ok=True)
        raise RpaPipelineError("unable to retain RPA CSV", "rpa_csv_artifact_unavailable") from exc


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _paths(output_dir):
    root = Path(output_dir) / "rpa"
    return {
        "root": root,
        "manifest": root / "batch-manifest.json",
        "csv": root / "diagnosis-csv",
        "records": root / "diagnosis-records",
        "receipts": root / "rpa-receipts",
        "frames": root / "peak-frames",
        "ready": root / "top_materials_rpa_ready.json",
        "verification": root / "batch-verification.json",
    }


def _entry(candidate, rank):
    material_id = validate_material_id(candidate.get("materialId"))
    result = dict(candidate)
    result.update({
        "rank": rank,
        "materialId": material_id,
        "rpaStatus": "pending",
        "postprocessStatus": "pending",
        "status": "pending",
        "failureReason": None,
        "errorType": None,
        "errorMessage": None,
        "diagnosisCsvPath": None,
        "diagnosisRecordsPath": None,
        "recordsPersisted": False,
        "postprocessAttemptCount": 0,
        "timingsSec": {},
    })
    return result


def _completed(entry):
    return entry.get("status") == "succeeded" and entry.get("rpaStatus") == "selected" and entry.get("postprocessStatus") == "succeeded" and entry.get("recordsPersisted") is True


def _completed_artifacts_are_valid(entry, paths):
    if not _completed(entry):
        return False
    try:
        csv_path = Path(entry["diagnosisCsvPath"]) if entry.get("diagnosisCsvPath") else None
        records_path = Path(entry["diagnosisRecordsPath"])
        receipt_path = artifact_path(paths["receipts"], entry["materialId"], ".json")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        records = json.loads(records_path.read_text(encoding="utf-8"))
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    def within(root, path):
        if path is None:
            return True
        try:
            root_path = Path(root).resolve()
            return os.path.commonpath((str(root_path), str(path.resolve()))) == str(root_path)
        except ValueError:
            return False
    if not within(paths["csv"], csv_path) or not within(paths["records"], records_path):
        return False
    frames = entry.get("highlightFrames", [])
    if not isinstance(frames, list):
        return False
    for frame in frames:
        if not isinstance(frame, dict):
            return False
        frame_path = frame.get("framePath")
        if frame_path and (not within(paths["frames"], Path(frame_path)) or not _is_jpeg(frame_path)):
            return False
    return (
        (not csv_path or (csv_path.is_file() and csv_path.stat().st_size > 0))
        and receipt.get("state") == "records_persisted"
        and receipt.get("materialId") == entry["materialId"]
        and (not csv_path or receipt.get("csvSha256") == _sha256(csv_path))
        and isinstance(records, list)
        and any(isinstance(record, dict) and str(record.get("materialId") or "") == entry["materialId"] for record in records)
    )


def _load_resume(paths, fingerprint, period):
    manifest_path = paths["manifest"]
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RpaPipelineError("batch manifest is unreadable") from exc
    if payload.get("configurationFingerprint") != fingerprint:
        raise RpaPipelineError("configuration fingerprint does not match existing RPA manifest")
    if payload.get("period") != period:
        raise RpaPipelineError("period does not match existing RPA manifest")
    entries = payload.get("materials")
    if not isinstance(entries, list):
        raise RpaPipelineError("batch manifest is invalid")
    return {item.get("materialId"): item for item in entries if isinstance(item, dict) and item.get("materialId")}


def _persist(paths, entries, fingerprint, period, options, account=None):
    payload = {
        "schemaVersion": 1,
        "configurationFingerprint": fingerprint,
        "period": period,
        "rpaOptions": options,
        "rpaAccount": account,
        "materials": entries,
        "updatedAtEpochSec": time.time(),
    }
    _write_json(paths["manifest"], payload)
    return payload


def _retain_records(entry, result, paths):
    records = result.get("records")
    if not isinstance(records, list) or not any(isinstance(item, dict) and str(item.get("materialId") or "") == entry["materialId"] for item in records):
        raise RpaPipelineError("rpa_records_invalid: records do not contain the candidate material", "rpa_records_invalid")
    csv_path = result.get("csvPath")
    retained_csv = None
    if csv_path:
        retained_csv = artifact_path(paths["csv"], entry["materialId"], ".csv")
        _copy_file_atomically(csv_path, retained_csv)
    records_path = artifact_path(paths["records"], entry["materialId"], ".json")
    _write_json(records_path, records)
    receipt = {
        "schemaVersion": 1,
        "materialId": entry["materialId"],
        "clientAttemptId": result.get("clientAttemptId") or entry["materialId"],
        "state": "records_persisted",
        "csvPath": str(retained_csv) if retained_csv else None,
        "csvSizeBytes": retained_csv.stat().st_size if retained_csv else None,
        "csvSha256": _sha256(retained_csv) if retained_csv else None,
        "sourceType": "csv" if retained_csv else "payload",
        "recordsPath": str(records_path),
        "gatewayReceipt": result.get("gatewayReceipt") or {},
    }
    _write_json(artifact_path(paths["receipts"], entry["materialId"], ".json"), receipt)
    entry.update({"rpaStatus": "selected", "diagnosisCsvPath": str(retained_csv), "diagnosisRecordsPath": str(records_path), "recordsPersisted": True})
    return records


def _failure(entry, error):
    error_type = error.error_type if isinstance(error, RpaPipelineError) else getattr(error, "error_type", "rpa_gateway_client_error")
    entry.update({"rpaStatus": "rejected", "postprocessStatus": "not_started", "status": "failed", "failureReason": error_type, "errorType": type(error).__name__, "errorMessage": str(error)})


def _ready_payload(entries, fingerprint):
    selected = sorted((entry for entry in entries if _completed(entry)), key=lambda item: item.get("rank", 0))
    return {"schemaVersion": 1, "configurationFingerprint": fingerprint, "materialCount": len(selected), "materials": [{"rank": item["rank"], "materialId": item["materialId"], "diagnosisCsvPath": item["diagnosisCsvPath"], "diagnosisRecordsPath": item["diagnosisRecordsPath"], "highlightFrames": item.get("highlightFrames", [])} for item in selected]}


def _video_url(records, material_id):
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("recordType") == "material_info" and str(record.get("materialId") or "") == material_id:
            url = str(record.get("videoUrl") or "").strip()
            if url:
                return url
    raise RpaPipelineError("video_download_failed: diagnosis records contain no video URL", "video_download_failed")


def _is_jpeg(path):
    try:
        payload = Path(path).read_bytes()
    except OSError:
        return False
    return len(payload) >= 4 and payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9"


def build_postprocessor(policy, options, *, download_video=download_file, extract_frames=extract_highlight_frames):
    """Create a postprocessor that never retains the downloaded source video."""
    normalized_options = resolve_rpa_options(options)

    def postprocess(entry, records, paths):
        video_url = _video_url(records, entry["materialId"])
        with tempfile.TemporaryDirectory(prefix="qianchuan-rpa-video-") as temporary_dir:
            video_path = Path(temporary_dir) / (entry["materialId"] + ".mp4")
            try:
                download_video(
                    video_url,
                    video_path,
                    max_bytes=normalized_options.get("videoMaxBytes", DEFAULT_VIDEO_MAX_BYTES),
                    deadline_sec=normalized_options.get("downloadDeadlineSec", DEFAULT_DOWNLOAD_DEADLINE_SEC),
                )
            except Exception as exc:
                raise RpaPipelineError("video_download_failed: %s" % exc, "video_download_failed") from exc
            output_dir = paths["frames"] / entry["materialId"]
            try:
                summary = extract_frames(records, policy, video_path, output_dir)
            except Exception as exc:
                raise RpaPipelineError("frame_extraction_failed: %s" % exc, "frame_extraction_failed") from exc
        frames = summary.get("frames") if isinstance(summary, dict) else None
        if not isinstance(frames, list) or any(not _is_jpeg(item.get("framePath")) for item in frames if isinstance(item, dict)):
            raise RpaPipelineError("frame_verification_failed: extracted JPEG is missing or invalid", "frame_verification_failed")
        return {"frames": frames, "summaryPath": str(output_dir / "summary.json")}

    return postprocess


def _verify(paths, entries, fingerprint, target_top_n):
    selected = sorted((entry for entry in entries if _completed(entry)), key=lambda item: item.get("rank", 0))[:target_top_n]
    errors = []
    for entry in selected:
        for key in ("diagnosisCsvPath", "diagnosisRecordsPath"):
            if key == "diagnosisCsvPath" and not entry.get(key):
                continue
            path = Path(entry[key] or "")
            if not path.is_file() or path.stat().st_size <= 0:
                errors.append("%s missing for %s" % (key, entry["materialId"]))
        for frame in entry.get("highlightFrames", []):
            if not isinstance(frame, dict) or not _is_jpeg(frame.get("framePath")):
                errors.append("invalid highlight frame for %s" % entry["materialId"])
    payload = {"schemaVersion": 1, "configurationFingerprint": fingerprint, "targetTopN": target_top_n, "succeededCount": len(selected), "status": "succeeded" if len(selected) >= target_top_n and not errors else "partial_failed", "errors": errors}
    _write_json(paths["verification"], payload)
    return payload


def run_rpa_pipeline(output_dir, candidate_payload, *, configuration_fingerprint, period, options=None, account=None, fetch_diagnosis, postprocess):
    """Execute candidate RPA waves and return a persisted batch outcome.

    ``fetch_diagnosis`` and ``postprocess`` isolate external effects for deterministic
    tests and for the CLI's concrete RPA/video adapters.
    """
    options = resolve_rpa_options(options)
    collection = candidate_payload.get("collection") if isinstance(candidate_payload, dict) else None
    candidates = candidate_payload.get("materials") if isinstance(candidate_payload, dict) else None
    if not isinstance(collection, dict) or not isinstance(candidates, list):
        raise RpaPipelineError("candidate_pool_invalid: invalid candidate artifact", "candidate_pool_invalid")
    target_top_n = collection.get("targetTopN")
    candidate_top_n = collection.get("candidateTopN")
    if type(target_top_n) is not int or type(candidate_top_n) is not int or target_top_n <= 0 or candidate_top_n < target_top_n:
        raise RpaPipelineError("candidate_pool_invalid: invalid collection sizes", "candidate_pool_invalid")
    paths = _paths(output_dir)
    resumed = _load_resume(paths, configuration_fingerprint, period)
    entries = []
    for rank, candidate in enumerate(candidates[:candidate_top_n], start=1):
        try:
            entry = _entry(candidate, rank)
        except ValueError as exc:
            raise RpaPipelineError("candidate_pool_invalid: %s" % exc, "candidate_pool_invalid") from exc
        existing = resumed.get(entry["materialId"])
        if existing and _completed_artifacts_are_valid(existing, paths):
            entry = existing
        elif existing:
            entry["recoveredFromInvalidArtifacts"] = True
        entries.append(entry)
    material_ids = [entry["materialId"] for entry in entries]
    if len(material_ids) != len(set(material_ids)):
        raise RpaPipelineError("candidate_pool_invalid: duplicate candidate material IDs are not allowed", "candidate_pool_invalid")
    _persist(paths, entries, configuration_fingerprint, period, options, account)

    def process_rpa(entry):
        started = time.monotonic()
        entry.update({"rpaStatus": "submitted", "status": "running", "failureReason": None})
        entry["rpaRecoveryAttempts"] = []
        for attempt in range(options["recoveryAttempts"]):
            try:
                result = fetch_diagnosis(entry, paths)
                records = _retain_records(entry, result, paths)
                entry["timingsSec"]["rpa"] = time.monotonic() - started
                return entry, records
            except Exception as exc:
                entry["rpaRecoveryAttempts"].append({"attempt": attempt + 1, "errorType": type(exc).__name__, "errorMessage": str(exc)})
                if attempt + 1 < options["recoveryAttempts"]:
                    time.sleep(options["recoveryBackoffSec"] * (2 ** attempt))
                    continue
                _failure(entry, exc)
                entry["timingsSec"]["rpa"] = time.monotonic() - started
                return entry, None

    next_index = 0
    while next_index < len(entries) and sum(_completed(item) for item in entries) < target_top_n:
        remaining = target_top_n - sum(_completed(item) for item in entries)
        wave_size = min(options["rpaConcurrency"], max(1, remaining))
        wave = [item for item in entries[next_index:next_index + wave_size] if not _completed(item)]
        next_index += wave_size
        if not wave:
            continue
        with ThreadPoolExecutor(max_workers=len(wave)) as executor:
            futures = [executor.submit(process_rpa, entry) for entry in wave]
            wave_results = [future.result() for future in as_completed(futures)]
        for entry, records in sorted(wave_results, key=lambda value: value[0]["rank"]):
            if records is None:
                continue
            entry["postprocessAttemptCount"] += 1
            entry["postprocessStatus"] = "running"
            try:
                postprocessed = postprocess(entry, records, paths)
                frames = postprocessed.get("frames") if isinstance(postprocessed, dict) else None
                if not isinstance(frames, list):
                    raise RpaPipelineError("frame_verification_failed: postprocess did not return frames", "frame_verification_failed")
                entry.update({"postprocessStatus": "succeeded", "status": "succeeded", "highlightFrames": frames})
            except Exception as exc:
                entry.update({"postprocessStatus": "failed", "status": "failed", "failureReason": getattr(exc, "error_type", "frame_extraction_failed"), "errorType": type(exc).__name__, "errorMessage": str(exc)})
            _persist(paths, entries, configuration_fingerprint, period, options, account)
        _persist(paths, entries, configuration_fingerprint, period, options, account)

    verification = _verify(paths, entries, configuration_fingerprint, target_top_n)
    if verification["status"] == "succeeded":
        _write_json(paths["ready"], _ready_payload(entries, configuration_fingerprint))
    else:
        paths["ready"].unlink(missing_ok=True)
    return {"succeeded": verification["status"] == "succeeded", "manifestPath": str(paths["manifest"]), "verificationPath": str(paths["verification"]), "readyPath": str(paths["ready"]), "entries": entries}
