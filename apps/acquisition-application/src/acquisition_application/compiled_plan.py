"""Safe extraction of immutable plans from a verified BotBuild."""

from __future__ import annotations

import ast
import hashlib
import stat
from pathlib import Path
from typing import Any

from mozaic_bot_build import canonical_json, verify_bot_build

from .context import TrustedExecutionContext
from .result_bundle import FixtureImportError


def load_compiled_plan(
    build_root: Path, context: TrustedExecutionContext
) -> tuple[dict[str, Any], str]:
    """Verify the exact BC04-bound build before reading its literal PLAN.

    Generated Python is parsed as syntax and never executed. `PLAN` must be one
    dictionary literal and the embedded runtime identity must equal the
    manifest identity verified by mozaic-bot-build.
    """
    try:
        manifest_path = build_root / "manifest.json"
        manifest_stat = manifest_path.lstat()
        if (
            stat.S_ISLNK(manifest_stat.st_mode)
            or not stat.S_ISREG(manifest_stat.st_mode)
            or manifest_stat.st_size > 1_000_000
        ):
            raise FixtureImportError("BotBuild manifest is not a bounded regular file")
        raw_manifest = manifest_path.read_bytes()
        raw_digest = hashlib.sha256(raw_manifest).hexdigest()
        verified = verify_bot_build(build_root, raw_digest)
    except Exception as error:
        raise FixtureImportError("BotBuild integrity verification failed") from error
    manifest = verified.manifest
    bound_digest = (
        hashlib.sha256(canonical_json(manifest)).hexdigest()
        if context.execution_mode == "live-authorized"
        else verified.manifest_digest
    )
    if (
        bound_digest != context.build_manifest_digest.removeprefix("sha256:")
        or manifest.get("build_id") != context.build_id
        or (context.bot_id is not None and manifest.get("bot_id") != context.bot_id)
        or manifest.get("source", {}).get("source_id") != context.source_id
        or manifest.get("source", {}).get("source_revision")
        != context.source_revision_id
    ):
        raise FixtureImportError("BotBuild differs from the current BC04 binding")
    entrypoint = verified.root / "bot.py"
    try:
        entrypoint_stat = entrypoint.lstat()
    except OSError as error:
        raise FixtureImportError(
            "verified BotBuild entrypoint is unavailable"
        ) from error
    if (
        stat.S_ISLNK(entrypoint_stat.st_mode)
        or not stat.S_ISREG(entrypoint_stat.st_mode)
        or entrypoint_stat.st_size > 1_000_000
    ):
        raise FixtureImportError("verified BotBuild entrypoint is unavailable")
    try:
        tree = ast.parse(entrypoint.read_text(encoding="utf-8"), filename="bot.py")
    except (OSError, UnicodeError, SyntaxError) as error:
        raise FixtureImportError("verified BotBuild entrypoint is malformed") from error
    plans: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if name not in {"PLAN", "RUNTIME_IDENTITY"}:
            continue
        try:
            literal = ast.literal_eval(node.value)
        except (ValueError, TypeError) as error:
            raise FixtureImportError("BotBuild plan metadata is not literal") from error
        if name == "PLAN" and isinstance(literal, dict):
            plans.append(literal)
        elif name == "RUNTIME_IDENTITY" and isinstance(literal, dict):
            identities.append(literal)
        else:
            raise FixtureImportError("BotBuild plan metadata has invalid shape")
    if len(plans) != 1 or len(identities) != 1:
        raise FixtureImportError("BotBuild must declare one plan and runtime identity")
    if identities[0] != manifest.get("runtime_identity"):
        raise FixtureImportError("BotBuild runtime identity differs from manifest")
    plan = plans[0]
    if (
        plan.get("source", {}).get("source_id") != context.source_id
        or plan.get("source", {}).get("revision") != context.source_revision_id
    ):
        raise FixtureImportError("compiled plan source differs from current binding")
    bot_id = manifest.get("bot_id")
    if not isinstance(bot_id, str) or not bot_id:
        raise FixtureImportError("verified BotBuild bot identity is invalid")
    return plan, bot_id
