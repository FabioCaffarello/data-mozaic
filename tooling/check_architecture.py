"""Enforce the current Python module dependency graph with stdlib AST parsing."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    (ROOT / "apps/scout/scout", "scout"),
    (
        ROOT / "apps/collector-engineering/collector_engineering",
        "collector_engineering",
    ),
    (
        ROOT / "libs/python/observability/src/mozaic_observability",
        "mozaic_observability",
    ),
    (ROOT / "tooling", "tooling"),
)

# Component -> allowed first-party components. External imports are not mapped.
ALLOWED = {
    "scout": set(),
    "scout.domain": set(),
    "scout.ports": {"scout.domain"},
    "scout.projections": {"scout.domain"},
    "scout.application": {
        "scout.domain",
        "scout.ports",
        "scout.projections",
        "mozaic_observability",
    },
    "scout.adapters": {"scout.domain", "scout.projections", "scout.tools"},
    "scout.edge": set(),
    "scout.fakes": {"scout.domain", "scout.projections", "scout.edge"},
    "scout.outbox_relay": {"mozaic_observability"},
    "scout.relay_worker": {"scout.adapters", "scout.outbox_relay"},
    "scout.tools": {"scout.domain"},
    "scout.demo": {
        "scout.application",
        "scout.domain",
        "scout.edge",
        "scout.fakes",
        "scout.outbox_relay",
        "scout.projections",
    },
    "scout.__main__": {
        "scout.demo",
    },
    "mozaic_observability": set(),
    "collector_engineering": set(),
    "tooling": {"scout.adapters", "scout.domain"},
}
INTERNAL_ROOTS = (
    "scout",
    "mozaic_observability",
    "collector_engineering",
    "tooling",
)


def module_name(path: Path, root: Path, prefix: str) -> str:
    relative = path.relative_to(root)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([prefix, *parts])


def component_for(module: str) -> str | None:
    for component in sorted(ALLOWED, key=len, reverse=True):
        if module == component or module.startswith(component + "."):
            return component
    return None


def imported_modules(tree: ast.Module, current_module: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = current_module.split(".")[:-1]
                keep = max(0, len(package_parts) - node.level + 1)
                base = package_parts[:keep]
                target = ".".join([*base, *(node.module or "").split(".")]).strip(".")
                if target:
                    modules.add(target)
                elif node.module is None:
                    modules.update(
                        ".".join([*base, alias.name]) for alias in node.names
                    )
            elif node.module:
                modules.add(node.module)
    return modules


def dependency_violations(imports: set[str], component: str, label: str) -> list[str]:
    errors = []
    allowed = ALLOWED[component]
    for imported in sorted(imports):
        if not any(
            imported == internal or imported.startswith(internal + ".")
            for internal in INTERNAL_ROOTS
        ):
            continue
        dependency = component_for(imported)
        if dependency is None:
            errors.append(f"{label}: unclassified internal import {imported}")
        elif dependency != component and dependency not in allowed:
            errors.append(f"{label}: {component} may not import {dependency}")
    return errors


def violations() -> list[str]:
    errors = []
    for source_root, prefix in SOURCE_ROOTS:
        for path in sorted(source_root.rglob("*.py")):
            current = module_name(path, source_root, prefix)
            component = component_for(current)
            if component is None:
                errors.append(
                    f"{path.relative_to(ROOT)}: module has no architecture component"
                )
                continue
            try:
                tree = ast.parse(path.read_text(), filename=str(path))
            except SyntaxError as error:
                errors.append(f"{path.relative_to(ROOT)}:{error.lineno}: {error.msg}")
                continue
            errors.extend(
                dependency_violations(
                    imported_modules(tree, current),
                    component,
                    str(path.relative_to(ROOT)),
                )
            )
    return errors


def main() -> int:
    errors = violations()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Architecture dependency checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
