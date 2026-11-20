import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tooling.scout_release_artifact import runtime_config_differences
from tooling.verify_scout_bundle import verify_bundle

COMMIT = "1" * 40
TREE = "2" * 40
TARGET = "linux/amd64"


class ReleaseBundleVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "bundle"
        self.root.mkdir()
        self.image_id = self._write_image("linux", "amd64")
        self._write_bundle()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _write_image(self, os_name: str, architecture: str) -> str:
        config = {
            "architecture": architecture,
            "os": os_name,
            "config": {
                "Entrypoint": [
                    "python",
                    "-m",
                    "collector_engineering.protected_worker",
                ],
                "Cmd": None,
                "User": "65532:65532",
                "WorkingDir": "/opt/bc15",
            },
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "a" * 64]},
        }
        raw = json.dumps(config, separators=(",", ":")).encode()
        image_id = "sha256:" + self._sha(raw)
        archive_path = self.root / "bc15-worker-image.tar"
        with tarfile.open(archive_path, "w") as archive:
            manifest = [
                {
                    "Config": f"blobs/sha256/{image_id.removeprefix('sha256:')}",
                    "RepoTags": None,
                    "Layers": [],
                }
            ]
            manifest_bytes = json.dumps(manifest).encode()
            import io

            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
            config_info = tarfile.TarInfo(
                f"blobs/sha256/{image_id.removeprefix('sha256:')}"
            )
            config_info.size = len(raw)
            archive.addfile(config_info, io.BytesIO(raw))
        return image_id

    def _write_bundle(self):
        (self.root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        (self.root / "requirements.txt").write_text(
            "demo-package==1.0 \\\n+    --hash=sha256:" + "b" * 64 + "\n",
            encoding="utf-8",
        )
        (self.root / "Dockerfile.protected").write_text(
            "FROM python@sha256:" + "c" * 64 + "\n"
        )
        (self.root / "install_scout_bundle.sh").write_text("#!/bin/sh\n")
        (self.root / "verify_scout_bundle.py").write_text("# verifier\n")
        (self.root / "scout_release_artifact.py").write_text("# release checks\n")
        for directory in ("wheelhouse", "wheels"):
            (self.root / directory).mkdir()
            (self.root / directory / "demo-1.0-py3-none-any.whl").write_bytes(b"wheel")
        archive = self.root / "bc15-worker-image.tar"
        image = {
            "format": 2,
            "worker_image_id": self.image_id,
            "build_image_id": self.image_id,
            "archive_config_digest": self.image_id,
            "imported_image_id": self.image_id,
            "archive_sha256": self._sha(archive.read_bytes()),
            "source_commit": COMMIT,
            "git_tree": TREE,
            "requested_platform": TARGET,
            "os": "linux",
            "architecture": "amd64",
            "uv_lock_sha256": self._sha((self.root / "uv.lock").read_bytes()),
            "dockerfile_sha256": self._sha(
                (self.root / "Dockerfile.protected").read_bytes()
            ),
            "base_image_id": "sha256:" + "9" * 64,
            "base_image_os": "linux",
            "base_image_architecture": "amd64",
            "base_image_repo_digests": [],
            "base_image_reference_digest": "sha256:" + "c" * 64,
            "repo_digests": [],
            "entrypoint": ["python", "-m", "collector_engineering.protected_worker"],
            "cmd": None,
            "user": "65532:65532",
            "working_dir": "/opt/bc15",
            "runtime_config": {
                "Entrypoint": [
                    "python",
                    "-m",
                    "collector_engineering.protected_worker",
                ],
                "Cmd": None,
                "User": "65532:65532",
                "WorkingDir": "/opt/bc15",
            },
            "source_tree_digest_sha256": "d" * 64,
        }
        (self.root / "worker-manifest.json").write_text(
            json.dumps(image), encoding="utf-8"
        )
        files = {
            path.relative_to(self.root).as_posix(): self._sha(path.read_bytes())
            for path in self.root.rglob("*")
            if path.is_file()
        }
        manifest = {
            "format": 2,
            "source_commit": COMMIT,
            "git_tree": TREE,
            "target_platform": TARGET,
            "source_tree_digest_sha256": "e" * 64,
            "uv_lock_sha256": self._sha((self.root / "uv.lock").read_bytes()),
            "requirements_sha256": self._sha(
                (self.root / "requirements.txt").read_bytes()
            ),
            "dockerfile_sha256": self._sha(
                (self.root / "Dockerfile.protected").read_bytes()
            ),
            "worker_image_id": self.image_id,
            "worker_manifest_sha256": self._sha(
                (self.root / "worker-manifest.json").read_bytes()
            ),
            "worker_archive_sha256": self._sha(archive.read_bytes()),
            "worker_archive_config_digest": self.image_id,
            "repo_digests": [],
            "files": files,
        }
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _refresh_file_hashes(self):
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"] = {
            path.relative_to(self.root).as_posix(): self._sha(path.read_bytes())
            for path in self.root.rglob("*")
            if path.is_file() and path != manifest_path
        }
        manifest_path.write_text(json.dumps(manifest))

    def _refresh_worker_and_bundle_hashes(self):
        worker_path = self.root / "worker-manifest.json"
        worker = json.loads(worker_path.read_text())
        archive = self.root / "bc15-worker-image.tar"
        worker["archive_sha256"] = self._sha(archive.read_bytes())
        worker_path.write_text(json.dumps(worker))
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["worker_manifest_sha256"] = self._sha(worker_path.read_bytes())
        manifest["worker_archive_sha256"] = self._sha(archive.read_bytes())
        manifest["worker_image_id"] = worker["worker_image_id"]
        manifest["worker_archive_config_digest"] = worker["archive_config_digest"]
        manifest_path.write_text(json.dumps(manifest))
        self._refresh_file_hashes()

    def _verify(self, target=TARGET, **kwargs):
        return verify_bundle(
            self.root,
            expected_commit=COMMIT,
            expected_tree=TREE,
            target_platform=target,
            **kwargs,
        )

    def test_valid_bundle_is_accepted(self):
        manifest = self._verify()
        self.assertEqual(self.image_id, manifest["worker_image_id"])

    def test_staging_verifier_does_not_add_bytecode_to_bundle(self):
        tooling = Path(__file__).resolve().parents[1] / "tooling"
        verifier = self.root / "verify_scout_bundle.py"
        shutil.copy2(tooling / verifier.name, verifier)
        shutil.copy2(
            tooling / "scout_release_artifact.py",
            self.root / "scout_release_artifact.py",
        )
        self._refresh_file_hashes()
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(verifier),
                str(self.root),
                "--expected-commit",
                COMMIT,
                "--expected-tree",
                TREE,
                "--target-platform",
                TARGET,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(self.root.rglob("__pycache__")))

    def test_wrong_image_id_is_rejected(self):
        worker = json.loads((self.root / "worker-manifest.json").read_text())
        worker["worker_image_id"] = "sha256:" + "f" * 64
        (self.root / "worker-manifest.json").write_text(json.dumps(worker))
        self._refresh_worker_and_bundle_hashes()
        with self.assertRaisesRegex(ValueError, "build image ID mismatch"):
            self._verify()

    def test_archive_from_another_image_is_rejected(self):
        other_image = self._write_image("linux", "amd64")
        self.assertEqual(self.image_id, other_image)
        # Replacing the archive with a different valid config must disagree with
        # the worker identity even after all internal file hashes are refreshed.
        config = json.dumps(
            {
                "architecture": "amd64",
                "os": "linux",
                "config": {"Entrypoint": ["different"], "Cmd": None},
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "b" * 64]},
            },
            separators=(",", ":"),
        ).encode()
        import io

        archive = self.root / "bc15-worker-image.tar"
        altered_id = self._sha(config)
        with tarfile.open(archive, "w") as output:
            for name, data in (
                (
                    "manifest.json",
                    json.dumps(
                        [
                            {
                                "Config": f"blobs/sha256/{altered_id}",
                                "RepoTags": None,
                                "Layers": [],
                            }
                        ]
                    ).encode(),
                ),
                (f"blobs/sha256/{altered_id}", config),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))
        self._refresh_worker_and_bundle_hashes()
        with self.assertRaisesRegex(
            ValueError, "archive config blob does not match image ID"
        ):
            self._verify()

    def test_wrong_platform_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "target platform mismatch"):
            self._verify(target="linux/arm64")

    def test_runtime_config_comparison_normalizes_empty_docker_fields(self):
        self.assertEqual(
            [],
            runtime_config_differences(
                {"Entrypoint": ["worker"], "Env": None},
                {"Entrypoint": ["worker"]},
            ),
        )
        self.assertEqual(
            ["Entrypoint"],
            runtime_config_differences(
                {"Entrypoint": ["worker"]}, {"Entrypoint": ["other"]}
            ),
        )

    def test_incompatible_wheel_is_rejected(self):
        (
            self.root / "wheelhouse/aio-1.0-cp312-cp312-macosx_11_0_arm64.whl"
        ).write_bytes(b"bad wheel")
        self._refresh_file_hashes()
        with self.assertRaisesRegex(ValueError, "incompatible"):
            self._verify()

    def test_lockfile_divergence_is_rejected(self):
        (self.root / "uv.lock").write_text("changed\n")
        self._refresh_file_hashes()
        with self.assertRaisesRegex(ValueError, "uv.lock hash mismatch"):
            self._verify()

    def test_modified_file_is_rejected(self):
        (self.root / "requirements.txt").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "bundle digest mismatch"):
            self._verify()

    def test_missing_file_is_rejected(self):
        (self.root / "Dockerfile.protected").unlink()
        with self.assertRaisesRegex(ValueError, "bundle file set differs"):
            self._verify()

    def test_manifest_worker_id_inconsistency_is_rejected(self):
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["worker_image_id"] = "sha256:" + "f" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "bundle worker image ID mismatch"):
            self._verify()

    @patch("tooling.verify_scout_bundle.shutil.which", return_value="/usr/bin/docker")
    @patch("tooling.verify_scout_bundle.subprocess.run")
    def test_missing_imported_image_is_rejected(self, docker_run, _which):
        docker_run.return_value.returncode = 1
        with self.assertRaisesRegex(ValueError, "image is absent"):
            self._verify(docker_host="tcp://docker:2375")


if __name__ == "__main__":
    unittest.main()
