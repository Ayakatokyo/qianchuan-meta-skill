"""Atomic, redacted lifecycle receipts for meta-skill runs."""

import json
import os
from pathlib import Path
import re
import tempfile
import time


_SENSITIVE_VALUE = re.compile(r"(?i)(authorization|cookie|session|secret|token)(\s*[=:]\s*)([^\s,;]+)")


def _redact_message(value):
    return _SENSITIVE_VALUE.sub(r"\1\2[redacted]", str(value))


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


class RunReceipt:
    STAGES = ("query", "rpa", "batchVerification", "factPack")

    def __init__(self, output_dir, configuration_fingerprint, command, execution_authorized):
        started = time.time()
        rpa_status = "not_started" if execution_authorized else "not_requested"
        self.path = Path(output_dir) / "run-receipt.json"
        self.payload = {
            "schemaVersion": 1,
            "configurationFingerprint": configuration_fingerprint,
            "command": command,
            "executionAuthorized": execution_authorized,
            "status": "running",
            "startedAtEpochSec": started,
            "finishedAtEpochSec": None,
            "stages": {
                "query": self._stage("not_started"),
                "rpa": self._stage(rpa_status),
                "batchVerification": self._stage(rpa_status),
                "factPack": self._stage(rpa_status),
            },
        }
        self.active_stage = None
        self._persist()

    @staticmethod
    def _stage(status):
        return {"status": status, "startedAtEpochSec": None, "finishedAtEpochSec": None, "durationSec": None, "error": None}

    def _persist(self):
        _write_json(self.path, self.payload)

    def start_stage(self, name):
        if name not in self.STAGES:
            raise ValueError("unknown lifecycle stage")
        stage = self.payload["stages"][name]
        if stage["status"] == "not_requested":
            raise ValueError("cannot start an unrequested lifecycle stage")
        stage.update({"status": "running", "startedAtEpochSec": time.time(), "finishedAtEpochSec": None, "durationSec": None, "error": None})
        self.active_stage = name
        self._persist()

    def finish_stage(self, name, **details):
        stage = self.payload["stages"][name]
        finished = time.time()
        started = stage.get("startedAtEpochSec") or finished
        stage.update({"status": "succeeded", "finishedAtEpochSec": finished, "durationSec": max(0, finished - started), "error": None})
        stage.update(details)
        if self.active_stage == name:
            self.active_stage = None
        self._persist()

    def fail_stage(self, name, error):
        stage = self.payload["stages"][name]
        finished = time.time()
        started = stage.get("startedAtEpochSec") or finished
        stage.update({
            "status": "failed",
            "finishedAtEpochSec": finished,
            "durationSec": max(0, finished - started),
            "error": {"type": type(error).__name__, "message": _redact_message(error)},
        })
        if self.active_stage == name:
            self.active_stage = None
        self._persist()

    def fail_active_stage(self, error):
        if self.active_stage:
            self.fail_stage(self.active_stage, error)

    def finish(self, succeeded):
        self.payload["status"] = "succeeded" if succeeded else "failed"
        self.payload["finishedAtEpochSec"] = time.time()
        self._persist()
