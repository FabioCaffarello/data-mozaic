import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

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
    _reserve_output,
    fetch_fixture,
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
        from collector_engineering.playbook_v2 import parse_playbook

        with self.assertRaisesRegex(PlaybookError, "duplicate YAML key"):
            parse_playbook("dsl_version: x\ndsl_version: y\n")

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
        multi = load_playbook_v2(EXAMPLES / "multipage-catalog/capture-playbook.yaml")
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
