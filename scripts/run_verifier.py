"""Offline integrity verification for completed portable RPA runs."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re


class RunVerificationError(RuntimeError):
    pass


def _read_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunVerificationError("%s is unreadable" % label) from exc


def _artifact_path(run_root, artifact):
    if not isinstance(artifact, dict):
        raise RunVerificationError("artifact metadata must be an object")
    raw_path = artifact.get("artifactPath")
    relative = PurePosixPath(raw_path) if isinstance(raw_path, str) else None
    if not raw_path or relative is None or relative.is_absolute() or ".." in relative.parts or "\\" in raw_path:
        raise RunVerificationError("artifactPath must be a normalized relative path")
    target = (Path(run_root) / Path(*relative.parts)).resolve()
    try:
        target.relative_to(Path(run_root).resolve())
    except ValueError as exc:
        raise RunVerificationError("artifactPath escapes the run directory") from exc
    if not target.is_file() or target.stat().st_size <= 0:
        raise RunVerificationError("artifact is missing or empty: %s" % raw_path)
    expected_size = artifact.get("sizeBytes")
    if type(expected_size) is not int or expected_size != target.stat().st_size:
        raise RunVerificationError("artifact sizeBytes mismatch: %s" % raw_path)
    expected_hash = artifact.get("sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise RunVerificationError("artifact sha256 is invalid: %s" % raw_path)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != expected_hash:
        raise RunVerificationError("artifact sha256 mismatch: %s" % raw_path)
    return target


def _verify_records(path, material_id):
    records = _read_json(path, "diagnosis records")
    if not isinstance(records, list) or not any(
        isinstance(record, dict) and str(record.get("materialId") or "") == str(material_id) for record in records
    ):
        raise RunVerificationError("diagnosis records do not contain material %s" % material_id)


def _verify_summary(path):
    summary = _read_json(path, "highlight summary")
    if not isinstance(summary, dict) or summary.get("status") != "succeeded":
        raise RunVerificationError("highlight summary is not succeeded")


def _verify_jpeg(path):
    payload = Path(path).read_bytes()
    if len(payload) < 4 or payload[:2] != b"\xff\xd8" or payload[-2:] != b"\xff\xd9":
        raise RunVerificationError("highlight frame is not a valid JPEG")


def verify_completed_run(output_dir):
    run_root = Path(output_dir).resolve()
    batch = _read_json(run_root / "rpa" / "batch-verification.json", "batch verification")
    ready = _read_json(run_root / "rpa" / "top_materials_rpa_ready.json", "RPA ready payload")
    if not isinstance(batch, dict) or batch.get("status") != "succeeded":
        raise RunVerificationError("batch verification is not succeeded")
    if not isinstance(ready, dict) or batch.get("configurationFingerprint") != ready.get("configurationFingerprint"):
        raise RunVerificationError("configuration fingerprint mismatch")
    materials = ready.get("materials")
    if not isinstance(materials, list) or ready.get("materialCount") != len(materials):
        raise RunVerificationError("ready material count is invalid")
    if batch.get("targetTopN") != len(materials) or batch.get("succeededCount") != len(materials):
        raise RunVerificationError("batch success count does not match ready materials")
    verified_files = 0
    for material in materials:
        if not isinstance(material, dict) or not str(material.get("materialId") or ""):
            raise RunVerificationError("ready material is invalid")
        csv_artifact = material.get("diagnosisCsvArtifact")
        if csv_artifact is not None:
            _artifact_path(run_root, csv_artifact)
            verified_files += 1
        records_path = _artifact_path(run_root, material.get("diagnosisRecordsArtifact"))
        _verify_records(records_path, material["materialId"])
        verified_files += 1
        video_artifact = material.get("videoArtifact")
        if video_artifact is not None:
            _artifact_path(run_root, video_artifact)
            verified_files += 1
        summary_artifact = material.get("highlightSummaryArtifact")
        if summary_artifact is not None:
            summary_path = _artifact_path(run_root, summary_artifact)
            _verify_summary(summary_path)
            verified_files += 1
        frames = material.get("highlightFrames")
        if not isinstance(frames, list):
            raise RunVerificationError("highlight frames are invalid")
        for frame in frames:
            if not isinstance(frame, dict):
                raise RunVerificationError("highlight frame is invalid")
            frame_path = _artifact_path(run_root, frame.get("artifact"))
            _verify_jpeg(frame_path)
            verified_files += 1
    return {
        "valid": True,
        "configurationFingerprint": ready["configurationFingerprint"],
        "verifiedMaterialCount": len(materials),
        "verifiedFileCount": verified_files,
    }
