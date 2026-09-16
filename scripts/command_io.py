"""Command-scoped verified file reads and streaming copies."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


_CHUNK_SIZE = 1024 * 1024


class CommandFileCache:
    """Reuse verified digests and parsed JSON only within one command."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._digests: dict[Path, tuple[int, int, int, str, int]] = {}
        self._json: dict[Path, tuple[int, int, int, Any]] = {}
        self._physical_reads: dict[Path, int] = {}

    def _path(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        resolved.relative_to(self.root)
        return resolved

    def _count(self, path: Path) -> None:
        self._physical_reads[path] = self._physical_reads.get(path, 0) + 1

    def read_count(self, path: Path) -> int:
        return self._physical_reads.get(self._path(path), 0)

    def digest(self, path: Path) -> tuple[str, int]:
        resolved = self._path(path)
        stat = resolved.stat()
        cached = self._digests.get(resolved)
        fingerprint = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if cached is not None and cached[:3] == fingerprint:
            return cached[3], cached[4]
        digest = hashlib.sha256()
        size = 0
        self._count(resolved)
        with resolved.open("rb") as source:
            for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
                size += len(chunk)
        value = digest.hexdigest()
        self._digests[resolved] = (*fingerprint, value, size)
        return value, size

    def read_json(self, path: Path) -> Any:
        resolved = self._path(path)
        stat = resolved.stat()
        cached = self._json.get(resolved)
        fingerprint = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if cached is not None and cached[:3] == fingerprint:
            return cached[3]
        self._count(resolved)
        payload = resolved.read_bytes()
        value = json.loads(payload.decode("utf-8"))
        self._json[resolved] = (*fingerprint, value)
        self._digests[resolved] = (
            *fingerprint,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )
        return value

    def verify(self, path: Path, metadata: Mapping[str, Any]) -> tuple[str, int]:
        expected_hash = metadata.get("sha256")
        expected_size = metadata.get("sizeBytes", metadata.get("size_bytes"))
        if not isinstance(expected_hash, str):
            raise ValueError("sha256 must be a string")
        if expected_size is not None and type(expected_size) is not int:
            raise ValueError("size must be an integer")
        actual_hash, actual_size = self.digest(path)
        if actual_hash != expected_hash.lower():
            raise ValueError("sha256 does not match source")
        if expected_size is not None and expected_size != actual_size:
            raise ValueError("size does not match source")
        return actual_hash, actual_size

    def verify_json(self, path: Path, metadata: Mapping[str, Any]) -> Any:
        resolved = self._path(path)
        stat = resolved.stat()
        cached = self._json.get(resolved)
        fingerprint = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        if cached is not None and cached[:3] == fingerprint:
            self.verify(resolved, metadata)
            return cached[3]
        self._count(resolved)
        payload = resolved.read_bytes()
        actual_hash = hashlib.sha256(payload).hexdigest()
        expected_hash = metadata.get("sha256")
        expected_size = metadata.get("sizeBytes", metadata.get("size_bytes"))
        if not isinstance(expected_hash, str):
            raise ValueError("sha256 must be a string")
        if expected_size is not None and type(expected_size) is not int:
            raise ValueError("size must be an integer")
        if actual_hash != expected_hash.lower() or (expected_size is not None and len(payload) != expected_size):
            raise ValueError("JSON artifact hash or size does not match source")
        value = json.loads(payload.decode("utf-8"))
        self._json[resolved] = (*fingerprint, value)
        self._digests[resolved] = (*fingerprint, actual_hash, len(payload))
        return value

    def copy_verified(
        self,
        source: Path,
        destination: Path,
        expected_hash: str,
        expected_size: int,
    ) -> tuple[str, int]:
        if not isinstance(expected_hash, str):
            raise ValueError("sha256 must be a string")
        if type(expected_size) is not int:
            raise ValueError("size must be an integer")
        source_path = self._path(source)
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        temp_path = None
        self._count(source_path)
        try:
            with source_path.open("rb") as source_file, tempfile.NamedTemporaryFile(
                mode="wb", dir=destination_path.parent, prefix=destination_path.name + ".", suffix=".tmp", delete=False
            ) as destination_file:
                temp_path = Path(destination_file.name)
                for chunk in iter(lambda: source_file.read(_CHUNK_SIZE), b""):
                    destination_file.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            actual_hash = digest.hexdigest()
            if actual_hash != expected_hash.lower() or size != expected_size:
                raise ValueError("copied artifact hash or size mismatch")
            os.replace(temp_path, destination_path)
            temp_path = None
            return actual_hash, size
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
