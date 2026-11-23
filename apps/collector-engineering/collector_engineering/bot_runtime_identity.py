"""Small self-check shipped with generated BotBuild runtime packages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def verify(
    package_dir: Path, expected_files: dict[str, str], expected_sha256: str
) -> None:
    package_dir = package_dir.resolve(strict=True)
    actual_names = {path.name for path in package_dir.iterdir() if path.is_file()}
    if actual_names != set(expected_files):
        raise RuntimeError(
            "[BUILD_RUNTIME_FILE_SET_MISMATCH] imported runtime files differ"
        )
    actual = {}
    for name, expected in sorted(expected_files.items()):
        path = package_dir / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"[BUILD_RUNTIME_FILE_MISSING] {name}")
        actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual[name] != expected:
            raise RuntimeError(f"[BUILD_RUNTIME_DIGEST_MISMATCH] {name}")
    canonical = (
        json.dumps(actual, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    if hashlib.sha256(canonical).hexdigest() != expected_sha256:
        raise RuntimeError("[BUILD_RUNTIME_DIGEST_MISMATCH] aggregate digest differs")
