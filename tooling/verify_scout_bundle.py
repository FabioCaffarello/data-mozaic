"""Verify a broker bundle's internal SHA-256 manifest before installation."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1]).resolve(strict=True)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative, expected in manifest["files"].items():
        path = root / relative
        resolved = path.resolve(strict=True)
        if not path.is_file() or path.is_symlink() or not resolved.is_relative_to(root):
            raise SystemExit(f"invalid bundle file: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise SystemExit(f"bundle digest mismatch: {relative}")
    print(f"verified {len(manifest['files'])} bundle files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
