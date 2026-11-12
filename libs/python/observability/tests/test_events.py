import ast
import asyncio
import logging
import threading
import unittest
from pathlib import Path

from mozaic_observability import bind_context, log_event


class StructuredEventTests(unittest.TestCase):
    def test_library_source_has_no_bounded_context_imports(self):
        source_root = Path(__file__).parents[1] / "src" / "mozaic_observability"
        for module in source_root.glob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                self.assertFalse(
                    any(
                        name in {"scout", "apps"}
                        or name.startswith(("scout.", "apps."))
                        for name in names
                    ),
                    f"{module} imports a deployable or bounded-context module",
                )

    def test_allowlist_omits_sensitive_and_free_form_fields(self):
        logger = logging.getLogger("mozaic-observability-test")
        logger.setLevel(logging.INFO)
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger.addHandler(handler)
        try:
            log_event(
                logger,
                logging.INFO,
                "scout.custody.verified",
                artifact_id="not-allowlisted",
                session_id="session-1",
                password="never-log-this",
                content="private payload",
            )
        finally:
            logger.removeHandler(handler)
        self.assertEqual(records[0].mozaic_fields, {"session_id": "session-1"})
        self.assertNotIn("never-log-this", records[0].getMessage())
        self.assertNotIn("private payload", repr(records[0].mozaic_fields))

    def test_context_is_restored_and_isolated_between_threads(self):
        logger = logging.getLogger("mozaic-context-test")
        logger.setLevel(logging.INFO)
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger.addHandler(handler)
        try:
            with bind_context(command_id="parent"):
                thread = threading.Thread(
                    target=lambda: log_event(logger, logging.INFO, "child.event")
                )
                thread.start()
                thread.join()
                log_event(logger, logging.INFO, "parent.event")
            log_event(logger, logging.INFO, "empty.event")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(records[0].mozaic_context, {})
        self.assertEqual(records[1].mozaic_context, {"command_id": "parent"})
        self.assertEqual(records[2].mozaic_context, {})

    def test_context_isolation_between_async_tasks(self):
        async def scenario():
            logger = logging.getLogger("mozaic-async-test")
            logger.setLevel(logging.INFO)
            records = []
            handler = logging.Handler()
            handler.emit = records.append
            logger.addHandler(handler)

            async def emit_for(command_id):
                with bind_context(command_id=command_id):
                    await asyncio.sleep(0)
                    log_event(logger, logging.INFO, "async.event")

            try:
                await asyncio.gather(emit_for("a"), emit_for("b"))
            finally:
                logger.removeHandler(handler)
            return {record.mozaic_context["command_id"] for record in records}

        self.assertEqual(asyncio.run(scenario()), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
