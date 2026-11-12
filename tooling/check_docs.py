"""Check local Markdown links and fenced blocks without external dependencies."""

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def check(path: Path) -> list[str]:
    errors = []
    fence_char = None
    fence_len = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence_char is None:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None
            continue
        if fence_char:
            continue
        for match in LINK.finditer(line):
            target = match.group(1).split(maxsplit=1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.exists():
                errors.append(f"{path.relative_to(ROOT)}:{number}: missing {target}")
    if fence_char:
        errors.append(f"{path.relative_to(ROOT)}: unclosed code fence")
    return errors


def main() -> int:
    files = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").rglob("*.md"))
    errors = [error for path in files for error in check(path)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Checked {len(files)} Markdown files: local links and code fences OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
