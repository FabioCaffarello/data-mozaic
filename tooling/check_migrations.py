"""Check the repository's explicitly numbered SQL migration paths."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = re.compile(r"^(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.sql$")


def main() -> int:
    paths = sorted((ROOT / "migrations").rglob("*.sql"))
    seen: set[tuple[Path, str]] = set()
    for path in paths:
        match = MIGRATION.fullmatch(path.name)
        if not match:
            raise SystemExit(f"invalid migration name: {path.relative_to(ROOT)}")
        key = (path.parent, match.group(1))
        if key in seen:
            raise SystemExit(f"duplicate migration sequence in {path.parent}")
        seen.add(key)
    print(f"Checked {len(paths)} SQL migration names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
