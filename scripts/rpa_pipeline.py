"""Resumable, bounded RPA material processing without report-generation concerns."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from rpa_csv_parser import RpaCsvError, parse_diagnosis_csv
from rpa_dynamic_client import CONNECTOR_CODE
from frame_extractor import extract_highlight_frames
from path_safety import artifact_path, validate_material_id
from rpa_adapter import DEFAULT_VIDEO_MAX_BYTES, DEFAULT_DOWNLOAD_DEADLINE_SEC, download_file


DEFAULT_RPA_OPTIONS = {
    "rpaConcurrency": 3,
    "materialTimeoutSec": 600,
    "csvMaxBytes": 100 * 1024 * 1024,
    "videoMaxBytes": 1024 * 1024 * 1024,
    "downloadDeadlineSec": 600,
}


class RpaPipelineError(RuntimeError):
    def __init__(self, message, error_type="rpa_pipeline_error"):
        super().__init__(message)
        self.error_type = error_type


FIRST_BATCH_NO_CSV_MESSAGE = "首批素材详情 RPA 未返回有效 CSV，当前入参可能有问题，已暂停后续所有 RPA。请检查账号、日期和素材定位参数，修正后使用新运行目录重新验证首批。"
FIRST_BATCH_UNCONFIRMED_MESSAGE = "首批素材详情 RPA 结果尚未确认，已暂停后续所有 RPA。请先核实已提交任务的远端状态，不要直接重提或补位；确认旧任务结束并复核入参后使用新运行目录。"


def assert_detail_rpa_not_paused(output_dir):
    """Reusable preflight for derived skills; call before any new remote work."""
    path = _paths(output_dir)["manifest"]
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RpaPipelineError("RPA manifest is unreadable; refusing new submissions") from exc
    if not isinstance(manifest, dict):
        raise RpaPipelineError("RPA manifest must be an object; refusing new submissions")
    gate = manifest.get("firstBatchCsvGate") or {}
    if not isinstance(gate, dict) or (gate and gate.get("status") not in {"pending", "passed", "blocked", "unconfirmed"}):
        raise RpaPipelineError("RPA first-batch gate is invalid; refusing new submissions")
    if gate.get("status") in {"pending", "blocked", "unconfirmed"}:
        if gate["status"] == "pending":
            gate.update(status="unconfirmed", errorCode="first_batch_results_unconfirmed", message=FIRST_BATCH_UNCONFIRMED_MESSAGE)
            manifest.update(status="paused", firstBatchCsvGate=gate)
            _write_json(path, manifest)
        error = RpaPipelineError(gate.get("message") or FIRST_BATCH_UNCONFIRMED_MESSAGE,
                                 gate.get("errorCode") or "first_batch_results_unconfirmed")
        error.first_batch_csv_gate = gate
        raise error


def _update_first_batch_gate(gate, entries):
    first = [entry for entry in entries if entry["materialId"] in gate["materialIds"]]
    gate["outcomes"] = {entry["materialId"]: (
        "unconfirmed" if entry.get("rpaStatus") == "unconfirmed" else
        "valid_csv" if entry.get("validDiagnosisCsv") else "no_valid_csv") for entry in first}
    gate["validCsvCount"] = sum(entry.get("validDiagnosisCsv") is True for entry in first)
    if "unconfirmed" in gate["outcomes"].values():
        gate.update(status="unconfirmed", errorCode="first_batch_results_unconfirmed", message=FIRST_BATCH_UNCONFIRMED_MESSAGE)
    elif gate["validCsvCount"]:
        gate.update(status="passed", errorCode=None, message=None)
    else:
        gate.update(status="blocked", errorCode="first_batch_no_valid_csv", message=FIRST_BATCH_NO_CSV_MESSAGE)


def resolve_rpa_options(overrides=None):
    options = dict(DEFAULT_RPA_OPTIONS)
    options.update(overrides or {})
    positive_integer_fields = ("rpaConcurrency", "materialTimeoutSec", "csvMaxBytes", "videoMaxBytes", "downloadDeadlineSec")
    for field in positive_integer_fields:
        if type(options.get(field)) is not int or options[field] <= 0:
            raise RpaPipelineError("RPA option %s must be a positive integer" % field, "invalid_rpa_options")
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


def _artifact_metadata(run_root, path):
    run_root = Path(run_root).resolve()
    artifact = Path(path).resolve()
    try:
        relative = artifact.relative_to(run_root)
    except ValueError as exc:
        raise RpaPipelineError("artifact is outside the run directory", "artifact_path_invalid") from exc
    if not artifact.is_file() or artifact.stat().st_size <= 0:
        raise RpaPipelineError("artifact is missing or empty", "artifact_path_invalid")
    return {
        "artifactPath": relative.as_posix(),
        "sha256": _sha256(artifact),
        "sizeBytes": artifact.stat().st_size,
    }


def _paths(output_dir):
    root = Path(output_dir) / "rpa"
    return {
        "root": root,
        "manifest": root / "batch-manifest.json",
        "csv": root / "diagnosis-csv",
        "records": root / "diagnosis-records",
        "receipts": root / "rpa-receipts",
        "frames": root / "peak-frames",
        "videos": root / "videos",
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


def _source_completed(entry):
    return entry.get("rpaStatus") == "selected" and entry.get("recordsPersisted") is True


def _completed(entry):
    """A reusable run has both source evidence and any requested image assets."""
    return _source_completed(entry) and entry.get("postprocessStatus") in {"succeeded", "not_requested"}


def _completed_artifacts_are_valid(entry, paths):
    # A failed optional image task must not make durable source evidence look stale.
    if not _source_completed(entry):
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
    video_path = entry.get("videoPath")
    if entry.get("postprocessStatus") == "succeeded" and (
        not video_path or not within(paths["videos"], Path(video_path)) or not Path(video_path).is_file() or Path(video_path).stat().st_size <= 0
    ):
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


def _persist(paths, entries, fingerprint, period, options, account=None, gate=None):
    payload = {
        "schemaVersion": 1,
        "configurationFingerprint": fingerprint,
        "period": period,
        "rpaOptions": options,
        "rpaAccount": account,
        "firstBatchCsvGate": gate,
        "status": "paused" if gate and gate.get("status") in {"blocked", "unconfirmed"} else "running",
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
    if not csv_path:
        raise RpaPipelineError("RPA returned no downloadable CSV", "rpa_csv_artifact_unavailable")
    try:
        csv_records = parse_diagnosis_csv(csv_path)
    except RpaCsvError as exc:
        raise RpaPipelineError(str(exc), "rpa_csv_invalid") from exc
    if not any(str(record.get("materialId") or "") == entry["materialId"] and
               record.get("recordType") == "material_info" for record in csv_records):
        raise RpaPipelineError("CSV has no material_info for the requested material", "rpa_csv_invalid")
    # CSV validity is independent of subsequent persistence/video/frame failures.
    entry["validDiagnosisCsv"] = True
    records = csv_records
    retained_csv = artifact_path(paths["csv"], entry["materialId"], ".csv")
    _copy_file_atomically(csv_path, retained_csv)
    records_path = artifact_path(paths["records"], entry["materialId"], ".json")
    _write_json(records_path, records)
    run_root = paths["root"].parent
    csv_artifact = _artifact_metadata(run_root, retained_csv) if retained_csv else None
    records_artifact = _artifact_metadata(run_root, records_path)
    receipt_path = artifact_path(paths["receipts"], entry["materialId"], ".json")
    _write_json(receipt_path, {
        "schemaVersion": 1,
        "materialId": entry["materialId"],
        "clientAttemptId": result.get("clientAttemptId") or entry["materialId"],
        "state": "artifact_downloaded",
        "csvPath": str(retained_csv) if retained_csv else None,
        "csvSizeBytes": retained_csv.stat().st_size if retained_csv else None,
        "csvSha256": _sha256(retained_csv) if retained_csv else None,
    })
    receipt = {
        "schemaVersion": 1,
        "materialId": entry["materialId"],
        "clientAttemptId": result.get("clientAttemptId") or entry["materialId"],
        "state": "records_persisted",
        "diagnosisCsvArtifact": csv_artifact,
        "diagnosisRecordsArtifact": records_artifact,
        "csvSha256": csv_artifact["sha256"] if csv_artifact else None,
        "sourceType": "csv" if retained_csv else "payload",
    }
    _write_json(receipt_path, receipt)
    entry.update({
        "rpaStatus": "selected",
        "diagnosisCsvPath": str(retained_csv),
        "diagnosisRecordsPath": str(records_path),
        "diagnosisCsvArtifact": csv_artifact,
        "diagnosisRecordsArtifact": records_artifact,
        "recordsPersisted": True,
    })
    return records


def _failure(entry, error):
    error_type = error.error_type if isinstance(error, RpaPipelineError) else getattr(error, "error_type", "rpa_gateway_client_error")
    entry.update({"rpaStatus": "unconfirmed" if error_type in {"rpa_result_unconfirmed", "rpa_request_timeout"} or "rpa_request_timeout" in str(error) else "rejected", "postprocessStatus": "not_started", "status": "failed", "failureReason": error_type, "errorType": type(error).__name__, "errorMessage": str(error)})


def _ready_payload(entries, fingerprint):
    selected = sorted((entry for entry in entries if _source_completed(entry)), key=lambda item: item.get("rank", 0))
    # The selected entries keep runtime paths for resume. The public ready payload replaces them with portable references.
    def material(item):
        records_path = Path(item["diagnosisRecordsPath"])
        root = records_path.parents[2]
        frames = []
        for frame in item.get("highlightFrames", []):
            portable = {key: value for key, value in frame.items() if key != "framePath"}
            portable["artifact"] = _artifact_metadata(root, frame["framePath"])
            frames.append(portable)
        payload = {
            "rank": item["rank"],
            "materialId": item["materialId"],
            "diagnosisCsvArtifact": _artifact_metadata(root, item["diagnosisCsvPath"]) if item.get("diagnosisCsvPath") else None,
            "diagnosisRecordsArtifact": _artifact_metadata(root, records_path),
            "highlightFrames": frames,
        }
        video_path = item.get("videoPath")
        if video_path and Path(video_path).is_file():
            payload["videoArtifact"] = _artifact_metadata(root, video_path)
        summary_path = item.get("highlightSummaryPath")
        if summary_path and Path(summary_path).is_file():
            payload["highlightSummaryArtifact"] = _artifact_metadata(root, summary_path)
        return payload
    return {"schemaVersion": 1, "configurationFingerprint": fingerprint, "materialCount": len(selected), "materials": [material(item) for item in selected]}


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
    """Create a postprocessor that retains a verified local video for the report."""
    normalized_options = resolve_rpa_options(options)

    def postprocess(entry, records, paths):
        video_url = _video_url(records, entry["materialId"])
        video_path = artifact_path(paths["videos"], entry["materialId"], ".mp4")
        video_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            download_video(
                video_url,
                video_path,
                max_bytes=normalized_options.get("videoMaxBytes", DEFAULT_VIDEO_MAX_BYTES),
                deadline_sec=normalized_options.get("downloadDeadlineSec", DEFAULT_DOWNLOAD_DEADLINE_SEC),
            )
        except Exception as exc:
            raise RpaPipelineError("video_download_failed: %s" % exc, "video_download_failed") from exc
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise RpaPipelineError("video_download_failed: downloaded video is missing or empty", "video_download_failed")
        if policy is not None:
            output_dir = paths["frames"] / entry["materialId"]
            try:
                summary = extract_frames(records, policy, video_path, output_dir)
            except Exception as exc:
                raise RpaPipelineError("frame_extraction_failed: %s" % exc, "frame_extraction_failed") from exc
            frames = summary.get("frames") if isinstance(summary, dict) else None
            if not isinstance(frames, list) or any(not _is_jpeg(item.get("framePath")) for item in frames if isinstance(item, dict)):
                raise RpaPipelineError("frame_verification_failed: extracted JPEG is missing or invalid", "frame_verification_failed")
            return {"frames": frames, "summaryPath": str(output_dir / "summary.json"), "videoPath": str(video_path)}
        return {"frames": [], "videoPath": str(video_path)}

    return postprocess


def _verify(paths, entries, fingerprint, target_top_n):
    selected = sorted((entry for entry in entries if _source_completed(entry)), key=lambda item: item.get("rank", 0))[:target_top_n]
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
        video_path = Path(entry.get("videoPath") or "")
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            errors.append("videoPath missing for %s" % entry["materialId"])
    asset_statuses = [entry.get("postprocessStatus") for entry in selected]
    payload = {
        "schemaVersion": 1, "configurationFingerprint": fingerprint, "targetTopN": target_top_n,
        "succeededCount": len(selected), "status": "succeeded" if len(selected) >= target_top_n and not errors else "partial_failed",
        "assetStatus": "not_requested" if asset_statuses and all(status == "not_requested" for status in asset_statuses) else ("succeeded" if asset_statuses and all(status == "succeeded" for status in asset_statuses) else "partial_failed"),
        "errors": errors,
    }
    _write_json(paths["verification"], payload)
    return payload


def run_rpa_pipeline(output_dir, candidate_payload, *, configuration_fingerprint, period, options=None, account=None, fetch_diagnosis, postprocess=None, highlights_enabled=True, progress_callback=None):
    """Execute candidate RPA waves and return a persisted batch outcome.

    ``fetch_diagnosis`` and ``postprocess`` isolate external effects for deterministic
    tests and for the CLI's concrete RPA/video adapters.
    """
    assert_detail_rpa_not_paused(output_dir)
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
    gate = None
    if paths["manifest"].exists():
        gate = json.loads(paths["manifest"].read_text(encoding="utf-8")).get("firstBatchCsvGate")
    _persist(paths, entries, configuration_fingerprint, period, options, account, gate)

    def process_rpa(entry):
        started = time.monotonic()
        entry.update({"rpaStatus": "submitted", "status": "running", "failureReason": None})
        _write_json(artifact_path(paths["receipts"], entry["materialId"], ".json"), {
            "schemaVersion": 1,
            "materialId": entry["materialId"],
            "clientAttemptId": entry["materialId"],
            "state": "submitted",
        })
        try:
            result = fetch_diagnosis(entry, paths)
            records = _retain_records(entry, result, paths)
            entry["timingsSec"]["rpa"] = time.monotonic() - started
            return entry, records
        except Exception as exc:
            _failure(entry, exc)
            entry["timingsSec"]["rpa"] = time.monotonic() - started
            return entry, None

    def complete_postprocess(future, entry):
        try:
            postprocessed = future.result()
            frames = postprocessed.get("frames") if isinstance(postprocessed, dict) else None
            video_path = postprocessed.get("videoPath") if isinstance(postprocessed, dict) else None
            if not isinstance(frames, list) or not video_path:
                raise RpaPipelineError("video_download_failed: postprocess did not return a retained video", "video_download_failed")
            entry.update({"postprocessStatus": "succeeded", "status": "succeeded", "highlightFrames": frames, "videoPath": video_path})
            entry["highlightSummaryPath"] = postprocessed.get("summaryPath")
        except Exception as exc:
            entry.update({"postprocessStatus": "failed", "status": "partial", "failureReason": getattr(exc, "error_type", "frame_extraction_failed"), "errorType": type(exc).__name__, "errorMessage": str(exc)})
        _persist(paths, entries, configuration_fingerprint, period, options, account, gate)
        if progress_callback:
            try:
                progress_callback("rpa_processing", {"completed": sum(_source_completed(item) for item in entries), "total": target_top_n}, entry["materialId"])
            except Exception:
                pass

    next_index = 0
    pending_postprocess = {}
    with ThreadPoolExecutor(max_workers=options["rpaConcurrency"]) as postprocess_executor:
        while next_index < len(entries) and sum(_source_completed(item) for item in entries) < target_top_n:
            remaining = target_top_n - sum(_source_completed(item) for item in entries)
            wave_size = min(options["rpaConcurrency"], max(1, remaining))
            wave = [item for item in entries[next_index:next_index + wave_size] if not _source_completed(item)]
            next_index += wave_size
            if not wave:
                continue
            if gate is None:
                gate = {"status": "pending", "connectorCode": CONNECTOR_CODE, "materialIds": [item["materialId"] for item in wave],
                        "validCsvCount": 0, "outcomes": {}}
                _persist(paths, entries, configuration_fingerprint, period, options, account, gate)
            if progress_callback:
                for item in wave:
                    try:
                        progress_callback("rpa_processing", {"completed": sum(_source_completed(entry) for entry in entries), "total": target_top_n}, item["materialId"])
                    except Exception:
                        pass
            with ThreadPoolExecutor(max_workers=len(wave)) as detail_executor:
                detail_futures = [detail_executor.submit(process_rpa, entry) for entry in wave]
                for future in as_completed(detail_futures):
                    entry, records = future.result()
                    if records is not None:
                        entry["postprocessAttemptCount"] += 1
                        entry["postprocessStatus"] = "running"
                        _persist(paths, entries, configuration_fingerprint, period, options, account, gate)
                        media_future = postprocess_executor.submit(postprocess, entry, records, paths)
                        pending_postprocess[media_future] = entry
                    for media_future in [item for item in pending_postprocess if item.done()]:
                        complete_postprocess(media_future, pending_postprocess.pop(media_future))
            if gate["status"] == "pending":
                _update_first_batch_gate(gate, entries)
            _persist(paths, entries, configuration_fingerprint, period, options, account, gate)
            if gate["status"] in {"blocked", "unconfirmed"}:
                break

        for media_future in as_completed(list(pending_postprocess)):
            complete_postprocess(media_future, pending_postprocess[media_future])

    verification = _verify(paths, entries, configuration_fingerprint, target_top_n)
    if gate and gate["status"] in {"blocked", "unconfirmed"}:
        verification.update(status="paused", firstBatchCsvGate=gate)
        _write_json(paths["verification"], verification)
    manifest = _persist(paths, entries, configuration_fingerprint, period, options, account, gate)
    manifest["status"] = verification["status"]
    _write_json(paths["manifest"], manifest)
    if verification["status"] == "succeeded":
        _write_json(paths["ready"], _ready_payload(entries, configuration_fingerprint))
    else:
        paths["ready"].unlink(missing_ok=True)
    return {"succeeded": verification["status"] == "succeeded", "firstBatchCsvGate": gate, "manifestPath": str(paths["manifest"]), "verificationPath": str(paths["verification"]), "readyPath": str(paths["ready"]), "entries": entries}
