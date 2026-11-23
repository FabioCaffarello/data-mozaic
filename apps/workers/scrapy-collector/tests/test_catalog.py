from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from mozaic_bot_build import canonical_json, derive_bot_id
from scrapy_collector.catalog.assembly import AssemblyError, assemble_catalog
from scrapy_collector.catalog.model import CatalogError, load_catalog, module_identity

ROOT = Path(__file__).resolve().parents[4]
LOCKFILE = ROOT / "uv.lock"


def _make_writable(root: Path) -> None:
    for directory, dirnames, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for dirname in dirnames:
            (current / dirname).chmod(0o700)
        current.chmod(0o700)


def _write_build(
    builds: Path, fixtures: Path, revision: str, body: bytes
) -> dict[str, str]:
    source_id = "catalog-test-source"
    playbook_id = "catalog-test-playbook"
    bot_id = derive_bot_id(playbook_id, source_id)
    stage = builds.parent / f"stage-{revision}"
    (stage / "runtime/mozaic_bot_runtime").mkdir(parents=True)
    profile = {
        "backend_id": "scrapy-fixture",
        "backend_version": "1.0.0",
        "execution_mode": "fixture-only",
        "capabilities": ["http.fetch.fixture", "records.extract"],
        "unsupported_capabilities": ["browser.rendered_dom"],
    }
    profile_bytes = canonical_json(profile)
    (stage / "capability-profile.json").write_bytes(profile_bytes)
    runtime_contents = {
        "__init__.py": b"",
        "identity.py": b"# identity\n",
        "reference_runtime.py": b"# reference\n",
        "syntax.py": b"# syntax\n",
        "scrapy_fixture_runtime.py": b"# scrapy runtime\n",
    }
    runtime_files = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in runtime_contents.items()
    }
    for name, data in runtime_contents.items():
        (stage / "runtime/mozaic_bot_runtime" / name).write_bytes(data)
    runtime_identity = {
        "package": "mozaic_bot_runtime",
        "version": "collector-runtime/1.0.0",
        "files": runtime_files,
        "sha256": hashlib.sha256(canonical_json(runtime_files)).hexdigest(),
    }
    files = {
        "bot.py": b"# spider placeholder\n",
        "capability-profile.json": profile_bytes,
        **{
            f"runtime/mozaic_bot_runtime/{name}": data
            for name, data in runtime_contents.items()
        },
    }
    for name, data in files.items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    lock_digest = hashlib.sha256(LOCKFILE.read_bytes()).hexdigest()
    ir_digest = hashlib.sha256(f"ir-{revision}".encode()).hexdigest()
    core = {
        "manifest_version": "bot-build-manifest/v2",
        "playbook_id": playbook_id,
        "bot_id": bot_id,
        "source": {"source_id": source_id, "source_revision": revision},
        "playbook_version": "capture-playbook-draft/v2",
        "playbook_sha256": None,
        "ir_version": "collector-ir/v1",
        "ir_sha256": ir_digest,
        "backend_id": "scrapy-fixture",
        "backend_version": "1.0.0",
        "capability_profile_id": "scrapy-fixture-v1",
        "capability_profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "compiler_version": "mozaic-collector-engineering/0.3.0",
        "runtime_identity": runtime_identity,
        "dependency_identity": {
            "lockfile_sha256": lock_digest,
            "packages": {"scrapy": "2.20.0", "selectolax": "0.4.13"},
        },
        "compatibility": {
            "worker_contract": "scrapy-collector/v1",
            "execution_mode": "fixture-only",
            "python_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
            "target_platform": "linux/amd64",
            "scrapy": "2.20.0",
            "scrapy_runtime": "scrapy-fixture-runtime/1.0.0",
        },
        "toolchain": {"python": sys.version.split()[0]},
        "dependency_lock_sha256": lock_digest,
        "generated_files": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
        "input_fixtures": [
            {
                "url": "https://fixture.test/page",
                "evidence_ref": "fixture-evidence",
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
                "content_type": "text/html",
                "status": 200,
                "source_id": source_id,
                "source_revision": revision,
            }
        ],
    }
    manifest = {**core, "build_id": hashlib.sha256(canonical_json(core)).hexdigest()}
    (stage / "manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    )
    build_id = manifest["build_id"]
    builds.mkdir(parents=True, exist_ok=True)
    builds.joinpath(build_id).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stage), str(builds / build_id))
    fixture_root = fixtures / build_id
    (fixture_root / "page.html").parent.mkdir(parents=True, exist_ok=True)
    (fixture_root / "page.html").write_bytes(body)
    (fixture_root / "index.json").write_bytes(
        canonical_json(
            {
                "complete": True,
                "source": {"source_id": source_id, "revision": revision},
                "pages": [
                    {
                        "url": "https://fixture.test/page",
                        "evidence_ref": "fixture-evidence",
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size_bytes": len(body),
                        "content_type": "text/html",
                        "status": 200,
                        "body_file": "page.html",
                    }
                ],
            }
        )
    )
    manifest_bytes = (builds / build_id / "manifest.json").read_bytes()
    return {
        "bot_id": bot_id,
        "build_id": build_id,
        "bot_build_manifest_digest": hashlib.sha256(manifest_bytes).hexdigest(),
    }


class CatalogAssemblyTests(unittest.TestCase):
    def test_assembles_two_bots_and_two_revisions_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builds, fixtures = root / "builds", root / "fixture-inputs"
            first = _write_build(builds, fixtures, "rev-1", b"first revision")
            second = _write_build(builds, fixtures, "rev-2", b"second revision")
            other_source = _write_build_other_source(builds, fixtures)
            self.assertEqual(first["bot_id"], second["bot_id"])
            self.assertNotEqual(first["build_id"], second["build_id"])
            self.assertNotEqual(first["bot_id"], other_source["bot_id"])
            source_manifest = root / "manifest.yaml"
            source_manifest.write_text(
                yaml.safe_dump(
                    {
                        "catalog_version": "bot-catalog-source/v1",
                        "entries": [
                            {**first, "state": "include"},
                            {**second, "state": "include"},
                            {**other_source, "state": "disabled"},
                        ],
                    },
                    sort_keys=False,
                )
            )
            outputs = [root / "assembly-a", root / "assembly-b"]
            for output in outputs:
                assemble_catalog(
                    manifest_path=source_manifest,
                    build_root=builds,
                    fixture_root=fixtures,
                    output=output,
                    lockfile=LOCKFILE,
                    target_platform="linux/amd64",
                )
            self.assertEqual(
                (outputs[0] / "catalog.json").read_bytes(),
                (outputs[1] / "catalog.json").read_bytes(),
            )
            self.assertEqual(
                (outputs[0] / "assembly.json").read_bytes(),
                (outputs[1] / "assembly.json").read_bytes(),
            )
            catalog = load_catalog(outputs[0])
            self.assertEqual(3, len(catalog.entries))
            self.assertEqual(
                first["build_id"],
                catalog.resolve(first["bot_id"], first["build_id"]).build_id,
            )
            self.assertNotIn(
                catalog.resolve(first["bot_id"], first["build_id"]).module_identity,
                sys.modules,
            )
            with self.assertRaisesRegex(CatalogError, "BOT_BUILD_DISABLED"):
                catalog.resolve(other_source["bot_id"], other_source["build_id"])
            self.assertFalse(
                (outputs[0] / "builds" / other_source["build_id"]).exists()
            )
            self.assertTrue(
                (outputs[0] / "fixtures" / first["build_id"] / "page.html").is_file()
            )
            for output in outputs:
                _make_writable(output)

    def test_rejects_yaml_alias_duplicate_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builds, fixtures = root / "builds", root / "fixtures"
            builds.mkdir()
            fixtures.mkdir()
            for content in (
                "catalog_version: bot-catalog-source/v1\nentries: &e []\nalias: *e\n",
                "catalog_version: bot-catalog-source/v1\ncatalog_version: bot-catalog-source/v1\nentries: []\n",
                "catalog_version: bot-catalog-source/v1\nentries: []\nextra: true\n",
            ):
                manifest = root / "manifest.yaml"
                manifest.write_text(content)
                with self.subTest(content=content), self.assertRaises(AssemblyError):
                    assemble_catalog(
                        manifest_path=manifest,
                        build_root=builds,
                        fixture_root=fixtures,
                        output=root / "out",
                        lockfile=LOCKFILE,
                        target_platform="linux/amd64",
                    )

    def test_catalog_digest_detects_tampering_and_module_identity_is_safe(self):
        self.assertTrue(
            module_identity("bot-" + "a" * 64, "b" * 64).split(".")[-1].startswith("b_")
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builds, fixtures = root / "builds", root / "fixtures"
            entry = _write_build(builds, fixtures, "rev-1", b"bytes")
            manifest = root / "manifest.yaml"
            manifest.write_text(
                yaml.safe_dump(
                    {
                        "catalog_version": "bot-catalog-source/v1",
                        "entries": [{**entry, "state": "include"}],
                    }
                )
            )
            output = root / "assembly"
            assemble_catalog(
                manifest_path=manifest,
                build_root=builds,
                fixture_root=fixtures,
                output=output,
                lockfile=LOCKFILE,
                target_platform="linux/amd64",
            )
            catalog_path = output / "catalog.json"
            catalog_path.chmod(0o644)
            original_catalog = catalog_path.read_bytes()
            with catalog_path.open("ab") as stream:
                stream.write(b" ")
            with self.assertRaisesRegex(
                CatalogError, "CATALOG_NONCANONICAL|ASSEMBLY_IDENTITY_MISMATCH"
            ):
                load_catalog(output)
            catalog_doc = json.loads(original_catalog)
            catalog_doc["entries"][0]["spider_name"] = "forged-spider"
            catalog_bytes = canonical_json(catalog_doc)
            catalog_path.write_bytes(catalog_bytes)
            assembly_path = output / "assembly.json"
            assembly_path.chmod(0o644)
            assembly_doc = json.loads(assembly_path.read_text(encoding="utf-8"))
            assembly_doc["catalog_sha256"] = hashlib.sha256(catalog_bytes).hexdigest()
            assembly_path.write_bytes(canonical_json(assembly_doc))
            with self.assertRaisesRegex(CatalogError, "ASSEMBLY_IDENTITY_MISMATCH"):
                load_catalog(output)
            _make_writable(output)


def _write_build_other_source(builds: Path, fixtures: Path) -> dict[str, str]:
    original = _write_build(builds, fixtures, "rev-x", b"other source")
    build_root = builds / original["build_id"]
    manifest_path = build_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["playbook_id"] = "catalog-test-playbook"
    manifest["source"] = {"source_id": "different-source", "source_revision": "rev-x"}
    manifest["bot_id"] = derive_bot_id(manifest["playbook_id"], "different-source")
    manifest["input_fixtures"][0]["source_id"] = "different-source"
    core = {key: value for key, value in manifest.items() if key != "build_id"}
    manifest["build_id"] = hashlib.sha256(canonical_json(core)).hexdigest()
    # The fixture fixture identity and generated file set are the same; create a new immutable directory.
    new_id = manifest["build_id"]
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    )
    new_root = builds / new_id
    shutil.move(str(build_root), str(new_root))
    fixture_old = fixtures / original["build_id"]
    fixture_index = json.loads((fixture_old / "index.json").read_text())
    fixture_index["source"]["source_id"] = "different-source"
    (fixture_old / "index.json").write_bytes(canonical_json(fixture_index))
    shutil.move(str(fixture_old), str(fixtures / new_id))
    return {
        "bot_id": manifest["bot_id"],
        "build_id": new_id,
        "bot_build_manifest_digest": hashlib.sha256(
            (new_root / "manifest.json").read_bytes()
        ).hexdigest(),
    }


if __name__ == "__main__":
    unittest.main()
