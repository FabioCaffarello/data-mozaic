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
        ROOT / "apps/workers/scrapy-collector/src/scrapy_collector",
        "scrapy_collector",
    ),
    (
        ROOT / "apps/execution-coordination/src/execution_coordination",
        "execution_coordination",
    ),
    (ROOT / "apps/acquisition-output/src/acquisition_output", "acquisition_output"),
    (
        ROOT / "apps/acquisition-application/src/acquisition_application",
        "acquisition_application",
    ),
    (
        ROOT / "apps/acquisition-lifecycle/src/acquisition_lifecycle",
        "acquisition_lifecycle",
    ),
    (ROOT / "apps/work-intake/src/work_intake", "work_intake"),
    (
        ROOT / "libs/python/bot-build/src/mozaic_bot_build",
        "mozaic_bot_build",
    ),
    (
        ROOT / "libs/python/observability/src/mozaic_observability",
        "mozaic_observability",
    ),
    (
        ROOT / "libs/python/contract-resolution/src/mozaic_contract_resolution",
        "mozaic_contract_resolution",
    ),
    (ROOT / "tooling", "tooling"),
)

# Component -> allowed first-party components. External imports are not mapped.
ALLOWED = {
    "scout": set(),
    "scout.domain": set(),
    "scout.discovery_content": set(),
    "scout.ports": {"scout.domain"},
    "scout.projections": {"scout.domain"},
    "scout.application": {
        "scout.domain",
        "scout.ports",
        "scout.projections",
        "scout.discovery_content",
        "mozaic_observability",
    },
    "scout.proposal_workflow": {
        "scout.application",
        "scout.domain",
        "scout.edge",
    },
    "scout.reviewer_identity": {"scout.domain"},
    "scout.adapters": {
        "scout.domain",
        "scout.ports",
        "scout.projections",
        "scout.tools",
    },
    "scout.edge": set(),
    "scout.fakes": {"scout.domain", "scout.projections", "scout.edge"},
    "scout.outbox_relay": {"mozaic_observability"},
    "scout.relay_worker": {"scout.adapters", "scout.outbox_relay"},
    "scout.tools": {"scout.domain", "scout.discovery_content"},
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
    "scout.tool_host": {
        "scout.adapters",
        "scout.application",
        "scout.domain",
        "scout.edge",
        "scout.proposal_workflow",
        "scout.tools",
        # The protected composition root wires BC15's public semantic
        # validator into the BC14 proposal workflow; neither domain imports it.
        "collector_engineering",
    },
    "scout.reviewer_cli": {
        "scout.adapters",
        "scout.application",
        "scout.domain",
        "scout.proposal_workflow",
        "scout.reviewer_identity",
        "scout.tools",
        "scout.tool_host",
        "acquisition_lifecycle",
        "collector_engineering",
    },
    "mozaic_observability": set(),
    "collector_engineering": {"mozaic_bot_build"},
    "acquisition_application": {
        "acquisition_application.application",
        "acquisition_application.context",
        "acquisition_application.identities",
        "acquisition_application.result_bundle",
    },
    "acquisition_application.application": {
        "acquisition_application.compiled_plan",
        "acquisition_application.context",
        "acquisition_application.identities",
        "acquisition_application.live_evidence",
        "acquisition_application.ports",
        "acquisition_application.result_bundle",
        "acquisition_output.application",
        "acquisition_output.domain",
        "collector_engineering",
        "execution_coordination.durable_domain",
        "execution_coordination.domain",
        "execution_coordination.durable_application",
        "mozaic_bot_build",
        "mozaic_contract_resolution",
    },
    "acquisition_application.context": {
        "execution_coordination.durable_domain",
        "acquisition_output.domain",
    },
    "acquisition_application.ports": {
        "acquisition_output.domain",
        "execution_coordination.durable_domain",
    },
    "acquisition_application.result_bundle": {
        "acquisition_application.plan_validation",
        "acquisition_output.domain",
        "execution_coordination.domain",
    },
    "acquisition_application.live_evidence": {
        "acquisition_application.plan_validation",
        "acquisition_application.result_bundle",
        "collector_engineering",
    },
    "acquisition_application.plan_validation": {
        "acquisition_application.result_bundle",
        "collector_engineering",
    },
    "acquisition_application.compiled_plan": {
        "acquisition_application.context",
        "acquisition_application.result_bundle",
        "mozaic_bot_build",
    },
    "acquisition_application.identities": {"acquisition_output.domain"},
    "acquisition_lifecycle": {
        "acquisition_lifecycle.application",
        "acquisition_lifecycle.domain",
        "acquisition_lifecycle.ports",
        "acquisition_lifecycle.adapters",
        "collector_engineering",
        "execution_coordination.adapters",
        "mozaic_bot_build",
        "mozaic_contract_resolution",
    },
    "acquisition_lifecycle.domain": set(),
    "acquisition_lifecycle.ports": {"acquisition_lifecycle.domain"},
    "acquisition_lifecycle.application": {
        "acquisition_lifecycle.domain",
        "acquisition_lifecycle.ports",
        "collector_engineering",
        "mozaic_bot_build",
        "mozaic_contract_resolution",
    },
    "acquisition_lifecycle.adapters": {
        "acquisition_lifecycle.domain",
        "acquisition_lifecycle.ports",
        "execution_coordination.adapters",
        "execution_coordination.durable_ports",
    },
    "work_intake": {
        "work_intake.domain",
        "work_intake.ports",
        "work_intake.application",
        "work_intake.adapters",
    },
    "work_intake.domain": {"mozaic_contract_resolution"},
    "work_intake.ports": {"work_intake.domain", "mozaic_contract_resolution"},
    "work_intake.application": {
        "work_intake.domain",
        "work_intake.ports",
        "mozaic_contract_resolution",
    },
    "work_intake.adapters": {
        "work_intake.domain",
        "mozaic_contract_resolution",
        "acquisition_lifecycle",
        "acquisition_lifecycle.application",
        "acquisition_lifecycle.domain",
        "execution_coordination.durable_application",
        "execution_coordination.domain",
    },
    "acquisition_application.adapters": {
        "acquisition_application.result_bundle",
        "acquisition_application.ports",
        "acquisition_output.domain",
    },
    "mozaic_bot_build": set(),
    "mozaic_contract_resolution": set(),
    "scrapy_collector": {"mozaic_bot_build"},
    "execution_coordination": {"execution_coordination.domain"},
    "execution_coordination.domain": set(),
    "execution_coordination.ports": {"execution_coordination.domain"},
    "execution_coordination.application": {
        "execution_coordination.domain",
        "execution_coordination.ports",
    },
    "execution_coordination.durable_domain": {"execution_coordination.domain"},
    "execution_coordination.durable_ports": {
        "execution_coordination.durable_domain",
    },
    # The explicit BC13 composition root is the only adapter module that wires
    # infrastructure adapters into the WorkIntake application use case.
    "work_intake.adapters.composition": {
        "work_intake.application",
        "work_intake.adapters",
        "acquisition_lifecycle.application",
        "execution_coordination.durable_application",
        "mozaic_contract_resolution",
    },
    "execution_coordination.durable_application": {
        "execution_coordination.domain",
        "execution_coordination.durable_domain",
        "execution_coordination.durable_ports",
    },
    "execution_coordination.adapters": {
        "execution_coordination.domain",
        "execution_coordination.ports",
        "execution_coordination.durable_domain",
        "execution_coordination.durable_ports",
    },
    "acquisition_output": set(),
    "acquisition_output.domain": set(),
    "acquisition_output.ports": {"acquisition_output.domain"},
    "acquisition_output.application": {
        "acquisition_output.domain",
        "acquisition_output.ports",
    },
    "acquisition_output.memory": {
        "acquisition_output.domain",
        "acquisition_output.ports",
    },
    "acquisition_output.wire": {"acquisition_output.domain"},
    "acquisition_output.adapters": {"acquisition_output.domain"},
    "acquisition_output.adapters.http_custody": {
        "acquisition_output.domain",
        "acquisition_output.adapters",
    },
    "acquisition_output.adapters.publication_grant": {
        "acquisition_output.domain",
    },
    "acquisition_output.adapters.mongodb": {
        "acquisition_output.domain",
        "acquisition_output.ports",
        "acquisition_output.adapters",
        "acquisition_output.wire",
    },
    "tooling": {
        "scout.adapters",
        "scout.domain",
        "acquisition_output.application",
        "acquisition_output.domain",
        "acquisition_output.adapters.mongodb",
        "acquisition_application",
        "acquisition_application.result_bundle",
        "acquisition_application.adapters",
        "acquisition_lifecycle.adapters",
        "acquisition_lifecycle.application",
        "acquisition_lifecycle.domain",
        "acquisition_output.adapters.http_custody",
        "acquisition_output.adapters.publication_grant",
        "acquisition_output.wire",
        "execution_coordination.adapters",
        "execution_coordination.application",
        "execution_coordination.domain",
        "execution_coordination.durable_application",
        "execution_coordination.durable_domain",
        "execution_coordination",
        "collector_engineering",
        "mozaic_bot_build",
        "mozaic_contract_resolution",
        "scrapy_collector",
        "work_intake",
        "work_intake.adapters",
        "work_intake.adapters.composition",
        "work_intake.domain",
    },
}
INTERNAL_ROOTS = (
    "scout",
    "mozaic_observability",
    "mozaic_bot_build",
    "scrapy_collector",
    "acquisition_output",
    "execution_coordination",
    "collector_engineering",
    "acquisition_application",
    "acquisition_lifecycle",
    "work_intake",
    "tooling",
)
SCOUT_DOMAIN_FORBIDDEN = {
    "mcp",
    "psycopg",
    "jsonschema",
    "http",
    "urllib",
    "pathlib",
    "os",
}


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
            if current == "scout.domain":
                for imported in imported_modules(tree, current):
                    root_name = imported.split(".", 1)[0]
                    if root_name in SCOUT_DOMAIN_FORBIDDEN:
                        errors.append(
                            f"{path.relative_to(ROOT)}: domain imports forbidden infrastructure {root_name}"
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
