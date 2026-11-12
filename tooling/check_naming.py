"""Check naming rules for first-party Python module paths."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = (
    ROOT / "apps/scout/scout",
    ROOT / "apps/collector-engineering/collector_engineering",
    ROOT / "libs/python/observability/src",
    ROOT / "tooling",
)
MODULE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
GENERIC_MODULES = {"common", "utils", "helpers", "shared"}


def violations(root: Path = ROOT) -> list[str]:
    errors = []
    for configured_root in PYTHON_ROOTS:
        source_root = root / configured_root.relative_to(ROOT)
        for path in sorted(source_root.rglob("*.py")):
            if path.name in {"__init__.py", "__main__.py"}:
                continue
            if not MODULE_NAME.fullmatch(path.stem):
                errors.append(f"{path.relative_to(root)}: use snake_case module names")
            if path.stem in GENERIC_MODULES:
                errors.append(
                    f"{path.relative_to(root)}: generic module name is prohibited"
                )
    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Python module naming checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
