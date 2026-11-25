"""Canonical identity for first-party runtime files embedded in BotBuilds."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from .playbook_v2 import canonical_json
from .syntax import PlaybookError

RUNTIME_PACKAGE = "mozaic_bot_runtime"
RUNTIME_PACKAGE_VERSION = "collector-runtime/1.1.0"
SUPPORTED_RUNTIME_PACKAGE_VERSIONS = frozenset(
    {"collector-runtime/1.0.0", RUNTIME_PACKAGE_VERSION}
)
SCRAPY_RUNTIME_MODULES = (
    "reference_runtime.py",
    "syntax.py",
    "scrapy_fixture_runtime.py",
    "scrapy_live_runtime.py",
    "scrapy_framework.py",
)


def runtime_bundle_files(module_names: Iterable[str]) -> tuple[dict[str, bytes], dict]:
    """Copy only first-party modules consumed by a generated runner."""
    package = Path(__file__).resolve().parent
    names = {"__init__.py", "bot_runtime_identity.py", *module_names}
    files = {}
    for name in sorted(names):
        target_name = "identity.py" if name == "bot_runtime_identity.py" else name
        files[f"runtime/{RUNTIME_PACKAGE}/{target_name}"] = (
            package / name
        ).read_bytes()
    per_file = {
        name.removeprefix(f"runtime/{RUNTIME_PACKAGE}/"): hashlib.sha256(
            data
        ).hexdigest()
        for name, data in sorted(files.items())
    }
    digest = hashlib.sha256(canonical_json(per_file)).hexdigest()
    return files, {
        "package": RUNTIME_PACKAGE,
        "version": RUNTIME_PACKAGE_VERSION,
        "files": per_file,
        "sha256": digest,
    }


def verify_runtime_identity(build_dir: Path, identity: dict) -> None:
    if (
        not isinstance(identity, dict)
        or identity.get("package") != RUNTIME_PACKAGE
        or identity.get("version") not in SUPPORTED_RUNTIME_PACKAGE_VERSIONS
        or not isinstance(identity.get("files"), dict)
    ):
        raise PlaybookError("[BUILD_RUNTIME_UNSUPPORTED] incompatible runtime identity")
    files = identity["files"]
    expected_digest = hashlib.sha256(canonical_json(files)).hexdigest()
    if identity.get("sha256") != expected_digest:
        raise PlaybookError("[BUILD_RUNTIME_IDENTITY_INVALID] aggregate digest differs")
    root = build_dir / "runtime" / RUNTIME_PACKAGE
    if root.is_symlink() or not root.is_dir():
        raise PlaybookError(
            "[BUILD_RUNTIME_MISSING] bundled runtime package is missing"
        )
    actual_names = {path.name for path in root.iterdir() if path.is_file()}
    expected_names = {Path(name).name for name in files}
    if actual_names != expected_names:
        raise PlaybookError(
            "[BUILD_RUNTIME_FILE_SET_MISMATCH] runtime file set differs"
        )
    actual = {}
    for relative, expected in sorted(files.items()):
        if Path(relative).name != relative or relative in {"", ".", ".."}:
            raise PlaybookError("[BUILD_RUNTIME_PATH_UNSAFE] runtime path is unsafe")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise PlaybookError(f"[BUILD_RUNTIME_FILE_MISSING] {relative}")
        actual[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual[relative] != expected:
            raise PlaybookError(f"[BUILD_RUNTIME_DIGEST_MISMATCH] {relative}")
    if hashlib.sha256(canonical_json(actual)).hexdigest() != identity["sha256"]:
        raise PlaybookError(
            "[BUILD_RUNTIME_DIGEST_MISMATCH] aggregate runtime digest differs"
        )
