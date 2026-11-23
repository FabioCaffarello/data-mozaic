import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import collector_engineering.__main__ as collector_cli
from collector_engineering.authoring_tools import inspect_fixture, try_selector
from collector_engineering.backend_python_http import (
    check_capabilities,
    compile_build,
    generate_python,
    verify_build,
)
from collector_engineering.playbook_v2 import (
    IR_VERSION,
    SCHEMA,
    build_typed_ir,
    canonical_json,
    load_playbook_v2,
    parse_playbook,
    validate_playbook_v2,
)
from collector_engineering.playbooks import (
    PlaybookError,
    build_draft,
    validate_playbook,
)
from collector_engineering.reference_runtime import (
    RuntimeContext,
    RuntimeFailure,
    _convert,
    _html,
    _json_path,
    _reserve_output,
    discover_links,
    fetch_fixture,
    paginate,
    select_json_records,
    write_raw_references,
)
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/playbooks"
LOCKFILE = ROOT / "uv.lock"


class CapturePlaybookV2Tests(unittest.TestCase):
    def setUp(self):
        Draft202012Validator.check_schema(SCHEMA)
        self.html_dir = EXAMPLES / "html-simple"
        self.parsed = load_playbook_v2(self.html_dir / "capture-playbook.yaml")
        self.fixture_root = self.html_dir / "fixtures"

    @staticmethod
    def codes(result):
        return {item.code for item in result.diagnostics}

    def test_v1_semantics_remain_unchanged(self):
        fixture = ROOT / "tests/fixtures/collector-engineering/multipage"
        transcript = json.loads((fixture / "transcript.json").read_text())
        draft = build_draft(transcript)
        self.assertEqual([], validate_playbook(draft))
        self.assertEqual("capture-playbook-draft/v1", draft["dsl_version"])

    def test_contract_schema_matches_packaged_validator_schema(self):
        contract_schema = json.loads(
            (
                ROOT / "contracts/capture-playbook/draft/v2/CapturePlaybook.schema.json"
            ).read_text()
        )
        self.assertEqual(contract_schema, SCHEMA)

    def test_html_example_has_typed_canonical_ir_golden(self):
        ir = build_typed_ir(self.parsed, fixture_root=self.fixture_root)
        golden = ROOT / "tests/fixtures/collector-engineering/v2/html-simple.ir.json"
        digest = ROOT / "tests/fixtures/collector-engineering/v2/html-simple.ir.sha256"
        self.assertEqual(ir.document, json.loads(golden.read_text()))
        self.assertEqual(ir.sha256, digest.read_text().strip())
        self.assertEqual(IR_VERSION, ir.document["ir_version"])
        self.assertEqual(
            hashlib.sha256(canonical_json(ir.document)).hexdigest(), ir.sha256
        )

    def test_parser_rejects_duplicate_yaml_keys(self):
        with self.assertRaisesRegex(PlaybookError, "duplicate YAML key"):
            parse_playbook("dsl_version: x\ndsl_version: y\n")
        with self.assertRaisesRegex(PlaybookError, "anchors and aliases"):
            parse_playbook("value: &shared text\ncopy: *shared\n")
        with self.assertRaisesRegex(PlaybookError, "PLAYBOOK_PARSE_ERROR"):
            parse_playbook("value: !!python/object/apply:os.system ['id']\n")
        with self.assertRaisesRegex(PlaybookError, "PLAYBOOK_TOO_LARGE"):
            parse_playbook("x: " + ("a" * 1_000_001))
        document = copy.deepcopy(self.parsed.document)
        document["operations"][0]["id"] = []
        self.assertIn("SCHEMA_INVALID", self.codes(validate_playbook_v2(document)))
        document = copy.deepcopy(self.parsed.document)
        document["findings"][0]["finding_id"] = []
        self.assertIn("SCHEMA_INVALID", self.codes(validate_playbook_v2(document)))

    def test_fixture_index_is_bounded_and_malformed_entries_are_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for file in self.fixture_root.iterdir():
                (root / file.name).write_bytes(file.read_bytes())
            index = json.loads((root / "index.json").read_text())
            index["pages"] = [None]
            (root / "index.json").write_text(json.dumps(index))
            result = validate_playbook_v2(self.parsed, fixture_root=root)
            self.assertIn("FIXTURE_INDEX_INVALID", self.codes(result))

    def test_fixture_inspection_and_selector_samples_are_bounded(self):
        info = inspect_fixture(self.fixture_root, "https://html.fixture.test/catalog/")
        self.assertEqual("e-html-1", info["evidence_ref"])
        self.assertTrue(info["content_values_omitted"])
        result = try_selector(
            self.fixture_root,
            "https://html.fixture.test/catalog/",
            "article.product",
            evidence_ref="e-html-1",
            max_samples=1,
        )
        self.assertEqual(2, result["match_count"])
        self.assertEqual(1, len(result["samples"]))
        self.assertFalse(result["semantic_correctness_proven"])
        no_matches = try_selector(
            self.fixture_root,
            "https://html.fixture.test/catalog/",
            ".missing-card",
            evidence_ref="e-html-1",
        )
        self.assertEqual("SELECTOR_NO_MATCHES", no_matches["diagnostic"]["code"])
        invalid = try_selector(
            self.fixture_root,
            "https://html.fixture.test/catalog/",
            "[",
            evidence_ref="e-html-1",
        )
        self.assertEqual("SELECTOR_INVALID", invalid["diagnostic"]["code"])

    def test_html_malformed_markup_decimal_conversion_and_required_fields(self):
        parser = _html(b"<ul><li class='row'>one<li class='row'>two")
        self.assertEqual(2, len(parser.css("li.row")))
        decimal_field = {"type": "decimal", "required": True, "normalize": ["trim"]}
        self.assertEqual("12.5", _convert(" 12.500 ", decimal_field, "price"))
        integer_field = {"type": "integer", "required": True, "normalize": []}
        self.assertEqual(2024, _convert("2024", integer_field, "year"))
        with self.assertRaisesRegex(RuntimeFailure, "FIELD_CONVERSION_INVALID"):
            _convert("1,25", decimal_field, "price")
        with self.assertRaisesRegex(RuntimeFailure, "REQUIRED_FIELD_MISSING"):
            _convert(None, decimal_field, "price")
        with self.assertRaisesRegex(RuntimeFailure, "FIELD_CONVERSION_INVALID"):
            _convert(Decimal("1e1000"), decimal_field, "price")
        optional_string = {"type": "string", "required": False, "normalize": []}
        self.assertIsNone(_convert(None, optional_string, "optional"))
        self.assertEqual(
            "café 東京",
            _convert("café 東京", optional_string, "unicode"),
        )
        boolean_field = {"type": "boolean", "required": True, "normalize": []}
        self.assertTrue(_convert("true", boolean_field, "enabled"))
        self.assertFalse(_convert("false", boolean_field, "enabled"))
        with self.assertRaisesRegex(RuntimeFailure, "FIELD_CONVERSION_INVALID"):
            _convert("True", boolean_field, "enabled")
        with self.assertRaisesRegex(RuntimeFailure, "JSON_PATH_INVALID"):
            _json_path(["first", "second"], "/-1", expand=False)

    def test_json_extraction_rejects_duplicate_keys_and_noncanonical_array_indices(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            context = RuntimeContext.create(
                str(self.fixture_root),
                str(Path(temporary).resolve() / "output"),
                self.parsed.document["source"],
                self.parsed.document["budgets"],
            )
            with self.assertRaisesRegex(RuntimeFailure, "JSON_DOCUMENT_INVALID"):
                select_json_records(
                    context,
                    {
                        "body": b'{"items":[{"id":"first","id":"second"}]}',
                        "evidence_ref": "e-json",
                    },
                    array_path="/items/*",
                    max_matches=10,
                    evidence_refs=["e-json"],
                )
            self.assertEqual(
                [],
                select_json_records(
                    context,
                    {"body": b'{"items":[]}', "evidence_ref": "e-json"},
                    array_path="/items/*",
                    max_matches=10,
                    evidence_refs=["e-json"],
                ),
            )
        for path in ("/-1", "/+1", "/01", "/ 1"):
            with self.assertRaisesRegex(RuntimeFailure, "JSON_PATH_INVALID"):
                _json_path(["zero", "one"], path, expand=False)

    def test_output_byte_budget_is_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            context = RuntimeContext.create(
                str(self.fixture_root),
                str(root / "output"),
                self.parsed.document["source"],
                {**self.parsed.document["budgets"], "max_output_bytes": 10},
            )
            with self.assertRaisesRegex(RuntimeFailure, "BUDGET_OUTPUT_BYTES_EXCEEDED"):
                _reserve_output(context, 11)

    def test_runtime_reads_only_verified_fixture_and_emits_raw_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = root / "output"
            context = RuntimeContext.create(
                str(self.fixture_root),
                str(output),
                self.parsed.document["source"],
                self.parsed.document["budgets"],
            )
            document = fetch_fixture(
                context,
                url="https://html.fixture.test/catalog/",
                content_type="text/html",
                evidence_refs=["e-html-1"],
            )
            refs = write_raw_references(context, document)
            self.assertEqual("e-html-1", refs[0]["evidence_ref"])
            self.assertTrue((output / "raw-references.json").is_file())
            with self.assertRaisesRegex(RuntimeFailure, "URL_OUT_OF_SCOPE"):
                fetch_fixture(
                    context,
                    url="https://other.fixture.test/catalog/",
                    content_type="text/html",
                    evidence_refs=["e-html-1"],
                )

    def test_runtime_binds_fixture_index_to_compiled_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixtures = root / "fixtures"
            fixtures.mkdir()
            for file in self.fixture_root.iterdir():
                (fixtures / file.name).write_bytes(file.read_bytes())
            index_path = fixtures / "index.json"
            index = json.loads(index_path.read_text())
            expected_page = index["pages"][0]
            expected = {
                **{
                    key: expected_page[key]
                    for key in (
                        "url",
                        "evidence_ref",
                        "sha256",
                        "content_type",
                        "status",
                    )
                },
                "size_bytes": len((fixtures / expected_page["body_file"]).read_bytes()),
                "source_id": index["source"]["source_id"],
                "source_revision": index["source"]["revision"],
            }
            context = RuntimeContext.create(
                str(fixtures),
                str(root / "output"),
                self.parsed.document["source"],
                self.parsed.document["budgets"],
                [expected],
            )
            index["pages"][0]["sha256"] = "0" * 64
            index_path.write_text(json.dumps(index))
            with self.assertRaisesRegex(RuntimeFailure, "FIXTURE_IDENTITY_MISMATCH"):
                fetch_fixture(
                    context,
                    url=expected["url"],
                    content_type=expected["content_type"],
                    evidence_refs=[expected["evidence_ref"]],
                )

    def test_unknown_operator_and_version_are_stable_diagnostics(self):
        document = copy.deepcopy(self.parsed.document)
        document["operations"][0]["op"] = "plugin.run/v1"
        self.assertIn("UNKNOWN_OPERATION", self.codes(validate_playbook_v2(document)))
        document["operations"][0]["op"] = "http.fetch/v2"
        self.assertIn("UNKNOWN_OPERATION", self.codes(validate_playbook_v2(document)))

    def test_scope_source_revision_and_fixture_digest_are_checked(self):
        document = copy.deepcopy(self.parsed.document)
        document["operations"][0]["url"] = "https://outside.fixture.test/catalog/"
        self.assertIn(
            "URL_OUT_OF_SCOPE",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )
        document = copy.deepcopy(self.parsed.document)
        document["source"]["revision"] = "wrong"
        self.assertIn(
            "SOURCE_REVISION_MISMATCH",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for file in self.fixture_root.iterdir():
                (root / file.name).write_bytes(file.read_bytes())
            (root / "page.html").write_bytes(b"tampered")
            result = validate_playbook_v2(self.parsed, fixture_root=root)
            self.assertTrue(
                {"FIXTURE_DIGEST_MISMATCH", "FIXTURE_EVIDENCE_MISMATCH"}
                & self.codes(result)
            )

    def test_fetch_fixture_binding_and_output_sink_cardinality(self):
        document = copy.deepcopy(self.parsed.document)
        document["operations"][0]["url"] = "https://html.fixture.test/other/"
        self.assertIn(
            "FETCH_FIXTURE_MISSING",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )
        document = copy.deepcopy(self.parsed.document)
        document["operations"][0]["evidence_refs"] = ["evidence-other"]
        self.assertIn(
            "FETCH_EVIDENCE_MISMATCH",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )
        document = copy.deepcopy(self.parsed.document)
        duplicate = copy.deepcopy(document["operations"][-2])
        duplicate["id"] = "emit-again"
        duplicate["records"]["operation"] = "extract"
        document["operations"].append(duplicate)
        self.assertIn(
            "OUTPUT_SINK_CARDINALITY", self.codes(validate_playbook_v2(document))
        )

    def test_pagination_deduplicates_seed_and_links(self):
        parsed = load_playbook_v2(EXAMPLES / "multipage-catalog/capture-playbook.yaml")
        fixtures = EXAMPLES / "multipage-catalog/fixtures"
        with tempfile.TemporaryDirectory() as temporary:
            context = RuntimeContext.create(
                str(fixtures),
                str(Path(temporary).resolve() / "output"),
                parsed.document["source"],
                parsed.document["budgets"],
            )
            seed = fetch_fixture(
                context,
                url="https://pages.fixture.test/catalog/",
                content_type="text/html",
                evidence_refs=["e-multi-1"],
            )
            link_set = discover_links(
                context,
                seed,
                selector="a.next",
                max_links=5,
                evidence_refs=["e-multi-1"],
            )
            link_set["urls"] = [seed["url"], *link_set["urls"], *link_set["urls"]]
            before = context.requests_used
            pages = paginate(context, link_set, max_pages=2)
            self.assertEqual(2, len(pages))
            self.assertEqual(before + 1, context.requests_used)
            self.assertEqual(
                [
                    "https://pages.fixture.test/catalog/",
                    "https://pages.fixture.test/catalog/page-2",
                ],
                [page["url"] for page in pages],
            )

    def test_linux_docker_runner_mounts_the_versioned_runtime_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            site_packages = (
                root
                / "lib"
                / f"python{collector_cli.sys.version_info.major}.{collector_cli.sys.version_info.minor}"
                / "site-packages"
            )
            site_packages.mkdir(parents=True)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (root / "build").mkdir()
            (root / "build" / "manifest.json").write_text(
                json.dumps({"backend_id": "python-http-reference"})
            )
            output = root / "output"
            command = [
                collector_cli.sys.executable,
                str(root / "build" / "bot.py"),
                "--fixtures",
                str(fixtures),
                "--output",
                str(output),
            ]
            completed = collector_cli.subprocess.CompletedProcess(command, 0, "", "")
            with (
                patch.object(collector_cli.sys, "platform", "linux"),
                patch.object(collector_cli.sys, "prefix", str(root)),
                patch.object(
                    collector_cli.shutil, "which", return_value="/usr/bin/docker"
                ),
                patch.object(
                    collector_cli.subprocess, "run", return_value=completed
                ) as run,
            ):
                collector_cli._run_network_isolated(command)
            docker_command = run.call_args.args[0]
            self.assertNotIn("/runtime,readonly", docker_command)
            self.assertIn("PYTHONPATH=/deps", docker_command)

    def test_bindings_ports_types_fields_cardinality_and_selector_have_diagnostics(
        self,
    ):
        document = copy.deepcopy(self.parsed.document)
        document["operations"][1]["documents"]["operation"] = "missing"
        self.assertIn("BINDING_INVALID", self.codes(validate_playbook_v2(document)))
        document = copy.deepcopy(self.parsed.document)
        document["operations"][1]["documents"]["port"] = "missing"
        self.assertIn("OUTPUT_PORT_UNKNOWN", self.codes(validate_playbook_v2(document)))
        document = copy.deepcopy(self.parsed.document)
        document["operations"][1]["documents"] = {
            "operation": "start",
            "port": "document",
        }
        document["operations"][0]["content_type"] = "application/json"
        self.assertIn("TYPE_MISMATCH", self.codes(validate_playbook_v2(document)))
        document = copy.deepcopy(self.parsed.document)
        document["operations"][2]["fields"]["price"]["type"] = "integer"
        self.assertIn(
            "OUTPUT_CONTRACT_MISMATCH", self.codes(validate_playbook_v2(document))
        )
        document = copy.deepcopy(self.parsed.document)
        document["operations"][1]["cardinality"] = "one"
        self.assertIn(
            "CARDINALITY_INVALID",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )
        document = copy.deepcopy(self.parsed.document)
        document["operations"][1]["selector"] = "["
        self.assertIn(
            "SELECTOR_INVALID",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )
        document = copy.deepcopy(self.parsed.document)
        document["operations"][1]["selector"] = "aside.not-present"
        self.assertIn(
            "SELECTOR_NO_MATCHES",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )

    def test_missing_evidence_and_invalid_field_mapping_are_rejected(self):
        document = copy.deepcopy(self.parsed.document)
        document["operations"][1]["evidence_refs"] = ["unknown"]
        self.assertIn(
            "EVIDENCE_REFERENCE_MISSING", self.codes(validate_playbook_v2(document))
        )
        document = copy.deepcopy(self.parsed.document)
        document["operations"][2]["fields"]["price"]["source"] = "json_value"
        document["operations"][2]["fields"]["price"]["path"] = "/price"
        self.assertIn(
            "FIELD_MAPPING_INVALID", self.codes(validate_playbook_v2(document))
        )
        document = copy.deepcopy(self.parsed.document)
        document["operations"][2]["fields"]["title"]["selector"] = ".not-present"
        self.assertIn(
            "REQUIRED_FIELD_MISSING",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )
        json_example = load_playbook_v2(EXAMPLES / "json-api/capture-playbook.yaml")
        json_document = copy.deepcopy(json_example.document)
        json_document["operations"][2]["fields"]["id"]["path"] = "/absent"
        self.assertIn(
            "REQUIRED_FIELD_MISSING",
            self.codes(
                validate_playbook_v2(
                    json_document, fixture_root=EXAMPLES / "json-api/fixtures"
                )
            ),
        )

    def test_budget_cycles_unreachable_and_capability_mismatch(self):
        document = copy.deepcopy(self.parsed.document)
        document["budgets"]["max_requests"] = 0
        self.assertIn("SCHEMA_INVALID", self.codes(validate_playbook_v2(document)))
        document = copy.deepcopy(self.parsed.document)
        document["budgets"]["max_response_bytes"] = 10
        self.assertIn(
            "BUDGET_OVERFLOW",
            self.codes(validate_playbook_v2(document, fixture_root=self.fixture_root)),
        )
        multi = load_playbook_v2(EXAMPLES / "multipage-catalog/capture-playbook.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            multi_fixtures = EXAMPLES / "multipage-catalog/fixtures"
            for file in multi_fixtures.iterdir():
                (root / file.name).write_bytes(file.read_bytes())
            index_path = root / "index.json"
            index = json.loads(index_path.read_text())
            index["pages"] = [
                page for page in index["pages"] if page["url"].endswith("/")
            ]
            index_path.write_text(json.dumps(index))
            self.assertIn(
                "PAGINATION_FIXTURE_MISSING",
                self.codes(validate_playbook_v2(multi, fixture_root=root)),
            )
        document = copy.deepcopy(multi.document)
        document["budgets"]["max_pages"] = 1
        self.assertIn("BUDGET_OVERFLOW", self.codes(validate_playbook_v2(document)))
        document = copy.deepcopy(multi.document)
        document["operations"][2]["links"] = {"operation": "pages", "port": "links"}
        self.assertIn("DAG_CYCLE", self.codes(validate_playbook_v2(document)))
        document = copy.deepcopy(self.parsed.document)
        document["operations"].append(
            {
                "id": "unused",
                "op": "http.fetch/v1",
                "url": "https://html.fixture.test/catalog/",
                "content_type": "text/html",
                "evidence_refs": ["e-html-1"],
            }
        )
        self.assertIn(
            "OPERATION_UNREACHABLE", self.codes(validate_playbook_v2(document))
        )
        document = copy.deepcopy(self.parsed.document)
        document["capabilities"].remove("html.css_select")
        self.assertIn("CAPABILITY_MISMATCH", self.codes(validate_playbook_v2(document)))
        document["capabilities"].append("browser.rendered_dom")
        self.assertIn("CAPABILITY_MISMATCH", self.codes(validate_playbook_v2(document)))

    def test_browser_example_passes_semantics_then_fails_capability_check(self):
        parsed = load_playbook_v2(
            EXAMPLES / "browser-capability-unsupported/capture-playbook.yaml"
        )
        result = validate_playbook_v2(
            parsed, fixture_root=EXAMPLES / "browser-capability-unsupported/fixtures"
        )
        self.assertTrue(result.valid)
        ir = build_typed_ir(
            parsed, fixture_root=EXAMPLES / "browser-capability-unsupported/fixtures"
        )
        self.assertEqual(
            "browser.render_dom",
            next(
                node["operator_type"]
                for node in ir.document["nodes"]
                if node["operator_type"].startswith("browser.")
            ),
        )
        self.assertIn(
            "browser.rendered_dom",
            {item["capability"] for item in check_capabilities(ir)},
        )

    def test_generated_source_is_deterministic_specific_and_compilable(self):
        html_ir = build_typed_ir(self.parsed, fixture_root=self.fixture_root)
        json_parsed = load_playbook_v2(EXAMPLES / "json-api/capture-playbook.yaml")
        json_ir = build_typed_ir(
            json_parsed, fixture_root=EXAMPLES / "json-api/fixtures"
        )
        html_source = generate_python(html_ir)
        self.assertEqual(html_source, generate_python(html_ir))
        self.assertNotEqual(html_source, generate_python(json_ir))
        compile(html_source, "generated-bot.py", "exec")

        renamed = copy.deepcopy(self.parsed.document)
        ids = {
            "start": "fetch-page_1",
            "products": "select-products",
            "extract": "extract-records",
            "emit": "write-records",
            "raw_refs": "raw-refs",
        }
        for operation in renamed["operations"]:
            operation["id"] = ids[operation["id"]]
            for value in operation.values():
                if isinstance(value, dict) and "operation" in value:
                    value["operation"] = ids[value["operation"]]
        renamed_ir = build_typed_ir(renamed, fixture_root=self.fixture_root)
        compile(generate_python(renamed_ir), "hyphenated-operation-ids.py", "exec")

        injected = copy.deepcopy(html_ir.document)
        selector = "article.product'\n__import__('os').system('touch /tmp/payload')#"
        selected = next(
            node
            for node in injected["nodes"]
            if node["operator_type"] == "select.html_records"
        )
        selected["inputs"]["selector"] = selector
        generated = generate_python(type(html_ir)(injected)).decode("utf-8")
        compile(generated, "selector-is-data.py", "exec")
        self.assertIn(repr(selector), generated)
        self.assertNotIn("\n__import__('os')", generated)

    def test_build_manifest_is_deterministic_and_verifier_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = compile_build(
                self.parsed,
                fixture_root=self.fixture_root,
                output=root / "builds" / "first",
                lockfile=LOCKFILE,
            )
            second = compile_build(
                self.parsed,
                fixture_root=self.fixture_root,
                output=root / "second",
                lockfile=LOCKFILE,
            )
            self.assertEqual(first["build_id"], second["build_id"])
            self.assertEqual(first, verify_build(root / "builds" / "first"))
            nested = root / "builds" / "first" / "nested"
            nested.mkdir()
            (nested / "manifest.json").write_text("{}")
            with self.assertRaisesRegex(PlaybookError, "BUILD_FILE_SET_MISMATCH"):
                verify_build(root / "builds" / "first")
            (nested / "manifest.json").unlink()
            nested.rmdir()
            (root / "builds" / "first" / "extra.txt").write_text("extra")
            with self.assertRaisesRegex(PlaybookError, "BUILD_FILE_SET_MISMATCH"):
                verify_build(root / "builds" / "first")
            (root / "builds" / "first" / "extra.txt").unlink()
            (root / "builds" / "first" / "bot.py").write_text("# changed")
            with self.assertRaisesRegex(PlaybookError, "BUILD_DIGEST_MISMATCH"):
                verify_build(root / "builds" / "first")

    def test_build_verifier_rejects_missing_symlink_and_incompatible_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            build = root / "build"
            compile_build(
                self.parsed,
                fixture_root=self.fixture_root,
                output=build,
                lockfile=LOCKFILE,
            )
            runtime_file = build / "runtime/mozaic_bot_runtime/reference_runtime.py"
            saved_runtime_file = runtime_file.read_bytes()
            runtime_file.unlink()
            with self.assertRaisesRegex(
                PlaybookError, "BUILD_RUNTIME_FILE_SET_MISMATCH"
            ):
                verify_build(build)
            runtime_file.write_bytes(saved_runtime_file)

            outside = root / "outside.txt"
            outside.write_text("outside")
            bot = build / "bot.py"
            saved_bot = bot.read_bytes()
            bot.unlink()
            bot.symlink_to(outside)
            with self.assertRaisesRegex(PlaybookError, "BUILD_FILE_UNSAFE"):
                verify_build(build)
            bot.unlink()
            bot.write_bytes(saved_bot)

            manifest_path = build / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["backend_version"] = "incompatible"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(PlaybookError, "BUILD_VERSION_UNSUPPORTED"):
                verify_build(build)

    def test_build_rejects_existing_destination_dependency_change_and_partial_write(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(PlaybookError, "BUILD_ALREADY_EXISTS"):
                compile_build(
                    self.parsed,
                    fixture_root=self.fixture_root,
                    output=existing,
                    lockfile=LOCKFILE,
                )

            changed_lock = root / "uv.lock"
            changed_lock.write_bytes(LOCKFILE.read_bytes() + b"# changed\n")
            changed_build = root / "changed-lock-build"
            compile_build(
                self.parsed,
                fixture_root=self.fixture_root,
                output=changed_build,
                lockfile=changed_lock,
            )
            with self.assertRaisesRegex(
                PlaybookError, "BUILD_DEPENDENCIES_UNSUPPORTED"
            ):
                verify_build(changed_build)

            partial_build = root / "partial-build"
            original_open = Path.open

            def fail_readme(path, *args, **kwargs):
                if path == partial_build / "README.md":
                    raise OSError("synthetic disk write failure")
                return original_open(path, *args, **kwargs)

            with (
                patch.object(Path, "open", autospec=True, side_effect=fail_readme),
                self.assertRaisesRegex(OSError, "synthetic disk write failure"),
            ):
                compile_build(
                    self.parsed,
                    fixture_root=self.fixture_root,
                    output=partial_build,
                    lockfile=LOCKFILE,
                )
            self.assertFalse(partial_build.exists())

    def test_capability_failure_publishes_no_build(self):
        parsed = load_playbook_v2(
            EXAMPLES / "browser-capability-unsupported/capture-playbook.yaml"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = root / "must-not-exist"
            with self.assertRaisesRegex(
                PlaybookError, "CAPABILITY_UNSUPPORTED browser.rendered_dom"
            ):
                compile_build(
                    parsed,
                    fixture_root=EXAMPLES / "browser-capability-unsupported/fixtures",
                    output=output,
                    lockfile=LOCKFILE,
                )
            self.assertFalse(output.exists())

    def test_output_path_traversal_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outside = root / "outside"
            outside.mkdir()
            link = root / "link"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(PlaybookError, "BUILD_PATH_UNSAFE"):
                compile_build(
                    self.parsed,
                    fixture_root=self.fixture_root,
                    output=link / "build",
                    lockfile=LOCKFILE,
                )


if __name__ == "__main__":
    unittest.main()
