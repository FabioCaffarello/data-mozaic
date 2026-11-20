import json
import stat
import tempfile
import unittest
from pathlib import Path

from scout.tool_host.composition import _private_policy_snapshot


class ProtectedBrokerConfigTests(unittest.TestCase):
    def test_policy_is_snapshotted_outside_workspace_as_owner_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            policy = workspace / "authoring-policy.json"
            original = {"version": 1, "sources": [], "approvals": []}
            policy.write_text(json.dumps(original), encoding="utf-8")
            policy.chmod(0o600)
            private = root / "home" / ".local/state/data-mozaic/scout-bc15/policy.json"

            snapshot = _private_policy_snapshot(policy, private)
            policy.write_text('{"version":1,"sources":["changed"]}', encoding="utf-8")

            self.assertEqual(original, json.loads(snapshot.read_text(encoding="utf-8")))
            self.assertEqual(0o400, stat.S_IMODE(snapshot.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(snapshot.parent.stat().st_mode))
            self.assertFalse(snapshot.is_relative_to(workspace))

    def test_policy_snapshot_rejects_group_readable_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "policy.json"
            source.write_text('{"version":1}', encoding="utf-8")
            source.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "owner-only"):
                _private_policy_snapshot(source, root / "private/policy.json")

    def test_policy_snapshot_rejects_symlink_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual.json"
            actual.write_text('{"version":1}', encoding="utf-8")
            actual.chmod(0o600)
            source = root / "policy.json"
            source.symlink_to(actual)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                _private_policy_snapshot(source, root / "private/policy.json")


if __name__ == "__main__":
    unittest.main()
