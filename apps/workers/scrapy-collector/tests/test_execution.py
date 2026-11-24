from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scrapy_collector.execution.context import ExecutionContext
from scrapy_collector.execution.launcher import run


class LauncherLifecycleTests(unittest.TestCase):
    def _run_with_child(self, wait_side_effect, *, stderr: bytes = b""):
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fixtures = root / "fixtures"
        fixtures.mkdir()
        entry = SimpleNamespace(bot_id=f"bot-{'a' * 64}", build_id="b" * 64)
        catalog = SimpleNamespace(
            root=root / "catalog",
            assembly_id="c" * 64,
            resolve=Mock(return_value=entry),
            fixture_root=Mock(return_value=fixtures),
        )
        returncode = (
            -signal.SIGTERM if isinstance(wait_side_effect, BaseException) else 73
        )
        wait = Mock(return_value=returncode)
        if isinstance(wait_side_effect, BaseException):
            wait.side_effect = wait_side_effect
        child = SimpleNamespace(
            pid=43210,
            returncode=returncode,
            wait=wait,
            poll=Mock(return_value=returncode),
        )

        def spawn(_command, **kwargs):
            if stderr:
                kwargs["stderr"].write(stderr)
            return child

        with (
            patch(
                "scrapy_collector.execution.launcher._verify_selected",
                return_value=root / "build",
            ),
            patch(
                "scrapy_collector.execution.launcher.subprocess.Popen",
                side_effect=spawn,
            ),
            patch(
                "scrapy_collector.execution.launcher.signal.getsignal",
                return_value=signal.SIG_DFL,
            ),
            patch("scrapy_collector.execution.launcher.signal.signal"),
            patch("scrapy_collector.execution.launcher._terminate") as terminate,
        ):
            result = run(
                catalog,
                entry.bot_id,
                entry.build_id,
                ExecutionContext(root / "outputs"),
            )
        return result, child, terminate, root

    def test_cancellation_terminates_child_and_publishes_no_output(self):
        result, child, terminate, root = self._run_with_child(KeyboardInterrupt())

        self.assertEqual("cancelled", result["state"])
        self.assertEqual(
            "[EXECUTION_CANCELLED] launcher interrupted", result["diagnostic"]
        )
        terminate.assert_called_once_with(child, 2.0)
        self.assertFalse((root / "outputs" / result["execution_id"]).exists())
        self.assertEqual([], list((root / "outputs").iterdir()))

    def test_child_crash_captures_diagnostic_and_publishes_no_output(self):
        result, _child, terminate, root = self._run_with_child(
            73, stderr=b"child crashed\n"
        )

        self.assertEqual("failed", result["state"])
        self.assertIn("child crashed", result["diagnostic"])
        terminate.assert_not_called()
        self.assertFalse((root / "outputs" / result["execution_id"]).exists())
        self.assertEqual([], list((root / "outputs").iterdir()))


if __name__ == "__main__":
    unittest.main()
