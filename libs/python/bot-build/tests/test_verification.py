from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mozaic_bot_build import (
    BotBuildVerificationError,
    canonical_json,
    derive_bot_id,
    verify_bot_build,
)


def make_build(
    root: Path,
    *,
    manifest_version: str = "bot-build-manifest/v2",
    runtime_identity_bytes: bytes = b"# identity\n",
    execution_mode: str = "fixture-only",
) -> str:
    runtime_files = {"__init__.py": b"", "identity.py": runtime_identity_bytes}
    runtime_hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in runtime_files.items()
    }
    runtime_digest = hashlib.sha256(canonical_json(runtime_hashes)).hexdigest()
    runtime_identity = {
        "package": "mozaic_bot_runtime",
        "version": "collector-runtime/1.0.0",
        "files": runtime_hashes,
        "sha256": runtime_digest,
    }
    generated = {"bot.py": b"# generated\n"}
    generated.update(
        {
            f"runtime/mozaic_bot_runtime/{name}": data
            for name, data in runtime_files.items()
        }
    )
    for name, data in generated.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    core = {
        "manifest_version": manifest_version,
        "runtime_identity": runtime_identity,
        "generated_files": {
            name: hashlib.sha256(data).hexdigest() for name, data in generated.items()
        },
    }
    if manifest_version == "bot-build-manifest/v2":
        core.update(
            {
                "playbook_id": "fixture-playbook",
                "bot_id": derive_bot_id("fixture-playbook", "fixture-source"),
                "source": {"source_id": "fixture-source", "source_revision": "rev-1"},
                "playbook_version": "capture-playbook-draft/v2",
                "playbook_sha256": None,
                "ir_version": "collector-ir/v1",
                "ir_sha256": "b" * 64,
                "backend_id": "scrapy-fixture",
                "backend_version": "1.0.0",
                "capability_profile_id": "scrapy-fixture-v1",
                "capability_profile_sha256": "c" * 64,
                "compiler_version": "0.3.0",
                "toolchain": {"python": "3.12.0"},
                "dependency_lock_sha256": "a" * 64,
                "input_fixtures": (
                    [
                        {
                            "url": "https://fixture.test/",
                            "evidence_ref": "e-1",
                            "sha256": "d" * 64,
                            "size_bytes": 0,
                            "source_id": "fixture-source",
                            "source_revision": "rev-1",
                        }
                    ]
                    if execution_mode == "fixture-only"
                    else []
                ),
                "dependency_identity": {
                    "lockfile_sha256": "a" * 64,
                    "packages": {"scrapy": "2.20.0"},
                },
                "compatibility": {
                    "worker_contract": "scrapy-collector/v1",
                    "execution_mode": execution_mode,
                    "python_minor": "3.12",
                    "target_platform": "linux/amd64",
                    **(
                        {"runtime_profile": "scrapy-live-http-v1"}
                        if execution_mode == "live-authorized"
                        else {}
                    ),
                },
            }
        )
        if execution_mode == "live-authorized":
            core["live_request"] = {
                "method": "GET",
                "url": "https://api.example.test/v1/items",
            }
    manifest = {**core, "build_id": hashlib.sha256(canonical_json(core)).hexdigest()}
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    )
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


class BotBuildVerificationTests(unittest.TestCase):
    def test_accepts_explicitly_supported_v1_and_v2(self):
        for version in ("bot-build-manifest/v1", "bot-build-manifest/v2"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "build"
                root.mkdir()
                digest = make_build(root, manifest_version=version)
                verified = verify_bot_build(root, digest, supported_versions=(version,))
                self.assertEqual(version, verified.manifest["manifest_version"])
                self.assertEqual(digest, verified.manifest_digest)

    def test_accepts_explicit_live_manifest_without_fixture_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            digest = make_build(root, execution_mode="live-authorized")
            verified = verify_bot_build(
                root, digest, supported_versions=("bot-build-manifest/v2",)
            )
            self.assertEqual(
                "live-authorized", verified.manifest["compatibility"]["execution_mode"]
            )
            self.assertEqual([], verified.manifest["input_fixtures"])

    def test_rejects_live_manifest_that_contains_fixture_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            make_build(root, execution_mode="live-authorized")
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            fixture = {
                "url": "https://fixture.test/",
                "evidence_ref": "e-1",
                "sha256": "d" * 64,
                "size_bytes": 0,
                "source_id": "fixture-source",
                "source_revision": "rev-1",
            }
            manifest["input_fixtures"] = [fixture]
            core = {key: value for key, value in manifest.items() if key != "build_id"}
            manifest["build_id"] = hashlib.sha256(canonical_json(core)).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, indent=2) + "\n"
            )
            with self.assertRaises(BotBuildVerificationError):
                verify_bot_build(
                    root,
                    hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    supported_versions=("bot-build-manifest/v2",),
                )

    def test_rejects_unsupported_version_and_expected_manifest_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            digest = make_build(root)
            with self.assertRaisesRegex(
                BotBuildVerificationError, "BUILD_VERSION_UNSUPPORTED"
            ):
                verify_bot_build(
                    root, digest, supported_versions=("bot-build-manifest/v1",)
                )
            with self.assertRaisesRegex(
                BotBuildVerificationError, "BUILD_MANIFEST_DIGEST_MISMATCH"
            ):
                verify_bot_build(root, "0" * 64)

    def test_rejects_extra_missing_and_changed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            digest = make_build(root)
            (root / "unexpected.py").write_text("# extra\n")
            with self.assertRaisesRegex(
                BotBuildVerificationError, "BUILD_FILE_SET_MISMATCH"
            ):
                verify_bot_build(root, digest)
            (root / "unexpected.py").unlink()
            (root / "bot.py").write_text("# changed\n")
            with self.assertRaisesRegex(
                BotBuildVerificationError, "BUILD_DIGEST_MISMATCH"
            ):
                verify_bot_build(root, digest)

    def test_rejects_runtime_drift_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            digest = make_build(root)
            runtime = root / "runtime/mozaic_bot_runtime/identity.py"
            runtime.write_text("# changed\n")
            with self.assertRaisesRegex(
                BotBuildVerificationError, "BUILD_RUNTIME_DIGEST_MISMATCH"
            ):
                verify_bot_build(root, digest)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            digest = make_build(root)
            target = root / "bot.py"
            target.unlink()
            target.symlink_to(root / "manifest.json")
            with self.assertRaisesRegex(BotBuildVerificationError, "BUILD_FILE_UNSAFE"):
                verify_bot_build(root, digest)

    def test_rejects_oversized_runtime_before_hashing_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            digest = make_build(
                root,
                runtime_identity_bytes=b"x" * (16 * 1024 * 1024 + 1),
            )
            with self.assertRaisesRegex(
                BotBuildVerificationError, "BUILD_SIZE_LIMIT_EXCEEDED"
            ):
                verify_bot_build(root, digest)

    def test_bot_identity_is_stable_for_revision_and_namespaced_by_source(self):
        stable_a = derive_bot_id("same-playbook", "source-a")
        stable_b = derive_bot_id("same-playbook", "source-b")
        self.assertEqual(stable_a, derive_bot_id("same-playbook", "source-a"))
        self.assertNotEqual(stable_a, stable_b)

    def test_v2_rejects_unknown_contract_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            make_build(root)
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["unrecognized_future_field"] = True
            core = {key: value for key, value in manifest.items() if key != "build_id"}
            manifest["build_id"] = hashlib.sha256(canonical_json(core)).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, indent=2) + "\n"
            )
            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(
                BotBuildVerificationError, "BUILD_MANIFEST_INVALID"
            ):
                verify_bot_build(root, digest)


if __name__ == "__main__":
    unittest.main()
