"""Bounded runtime entrypoint for one already-authorized compiled live BotBuild."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run() -> None:
    build = Path(os.environ.get("MOZAIC_LIVE_BUILD_DIRECTORY", "/opt/live-bot"))
    manifest = build / "manifest.json"
    bot = build / "bot.py"
    if (
        build.is_symlink()
        or not build.is_dir()
        or manifest.is_symlink()
        or bot.is_symlink()
    ):
        raise ValueError("compiled BotBuild path is unsafe")
    if (
        not manifest.is_file()
        or not bot.is_file()
        or manifest.stat().st_size > 1_000_000
    ):
        raise ValueError("compiled BotBuild is incomplete or oversized")

    retained = os.environ.get("MOZAIC_RESULT_DIRECTORY")
    if retained:
        output = Path(retained)
        if output.exists() or output.is_symlink():
            raise ValueError("result directory must be new")
        if output.parent.is_symlink() or not output.parent.is_dir():
            raise ValueError("result parent must be an existing regular directory")
        _execute(bot, output)
    else:
        with tempfile.TemporaryDirectory(prefix="mozaic-live-result-") as temporary:
            _execute(bot, Path(temporary) / "result")


def _execute(bot: Path, output: Path) -> None:
    command = [sys.executable, "-B", str(bot), "--output", str(output)]
    parameters = os.environ.get("MOZAIC_INPUT_PARAMETERS_FILE", "")
    if parameters:
        parameter_path = Path(parameters)
        if parameter_path.is_symlink() or not parameter_path.is_file():
            raise ValueError("input parameter document is unavailable")
        if parameter_path.stat().st_size > 64_000:
            raise ValueError("input parameter document exceeds its bound")
        command.extend(("--input-parameters-file", str(parameter_path)))
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        # Do not forward worker output: provider content and diagnostics are untrusted.
        raise RuntimeError("compiled BotBuild execution failed")
    if not output.is_dir() or output.is_symlink():
        raise ValueError("compiled BotBuild did not produce a result directory")
    total = 0
    for entry in output.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise ValueError("worker produced an unsafe result entry")
        total += entry.stat().st_size
        if total > 16_777_216:
            raise ValueError("worker result exceeds the bounded output profile")
    # The operator owns the private TemporaryDirectory containing the bind
    # mount, while the container may use a distinct non-root UID on Linux.
    # Permit that owner to remove this validated temporary result after import.
    # The host parent remains mode 0700, so this does not expose result files to
    # other host users.
    output.chmod(0o777)


def main() -> int:
    try:
        _run()
        print(json.dumps({"state": "completed"}, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(
            json.dumps(
                {"state": "failed", "diagnostic": str(error)},
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
