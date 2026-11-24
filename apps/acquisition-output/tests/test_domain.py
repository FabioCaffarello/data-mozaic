from __future__ import annotations

import json
import math
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from acquisition_output.domain import (
    ArtifactDescription,
    ArtifactItem,
    ArtifactOutput,
    ArtifactRef,
    CompositeOutput,
    ExecutionBinding,
    InvalidTransition,
    MemberManifestEntry,
    OutputConflict,
    OutputError,
    OutputIdentity,
    OutputSet,
    OutputSetBinding,
    OutputSetManifest,
    OutputSetSnapshot,
    OutputSetSpec,
    OutputSetState,
    PayloadContractRef,
    ProducerIdentity,
    RecordOutput,
    ScopedOperationIdentity,
    SourceRevisionRef,
    canonical_digest,
    canonical_json,
    derive_output_id,
    manifest_digest,
    output_intent_digest,
    stream_manifest_digest,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = PayloadContractRef(
    "example.catalog.item",
    "1.0.0",
    "sha256:" + "1" * 64,
)
ARTIFACT_DIGEST = "sha256:" + "a" * 64
OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def binding(
    *, tenant: str = "tenant-a", source_id: str = "source-a", run: str | None = None
) -> OutputSetBinding:
    execution = ExecutionBinding(
        run, f"attempt-{run}" if run else None, 7 if run else None
    )
    return OutputSetBinding(
        tenant,
        SourceRevisionRef(source_id, "revision-1"),
        ProducerIdentity("fixture-consumer", "1.0.0", "fixture-capability", "v1"),
        execution,
    )


def record(
    *,
    set_id: str = "set-a",
    output_id: str = "output-a",
    observation_id: str = "observation-a",
    operation_id: str = "stage-a",
    tenant: str = "tenant-a",
    source_id: str = "source-a",
    payload: dict | None = None,
) -> RecordOutput:
    selected_payload = (
        {"name": "Synthetic item", "price": 12.5, "active": True}
        if payload is None
        else payload
    )
    return RecordOutput(
        identity=OutputIdentity(output_id, observation_id, OBSERVED_AT, "item-1"),
        operation=ScopedOperationIdentity(tenant, operation_id),
        output_set_id=set_id,
        binding=binding(tenant=tenant, source_id=source_id),
        payload_contract=CONTRACT,
        payload=selected_payload,
    )


def set_spec(
    *, set_id: str = "set-a", tenant: str = "tenant-a", expected: int | None = None
) -> OutputSetSpec:
    return OutputSetSpec(set_id, binding(tenant=tenant), (CONTRACT,), expected)


class OutputValueTests(unittest.TestCase):
    def test_record_payload_is_copied_and_frozen(self):
        payload = {"name": "initial", "details": ["a"]}
        output = record(payload=payload)
        payload["name"] = "changed"
        payload["details"].append("b")
        self.assertEqual(output.payload["name"], "initial")
        self.assertEqual(output.payload["details"], ("a",))
        with self.assertRaises(TypeError):
            output.payload["name"] = "mutation"

    def test_empty_payload_is_valid_and_null_is_not_omission(self):
        empty = record(payload={})
        explicit_null = record(output_id="output-null", payload={"value": None})
        self.assertEqual(empty.to_wire()["payload"], {})
        self.assertIn("value", explicit_null.to_wire()["payload"])
        self.assertNotEqual(
            output_intent_digest(empty), output_intent_digest(explicit_null)
        )

    def test_record_rejects_non_object_payload_and_oversized_payload(self):
        with self.assertRaises(OutputError):
            RecordOutput(
                identity=OutputIdentity("o", "obs", OBSERVED_AT),
                operation=ScopedOperationIdentity("tenant-a", "op"),
                output_set_id="set-a",
                binding=binding(),
                payload_contract=CONTRACT,
                payload=[],  # type: ignore[arg-type]
            )
        huge = {"value": "x" * 1_100_000}
        with self.assertRaises(OutputError):
            record(payload=huge)

    def test_payload_rejects_unsupported_numbers_and_python_objects(self):
        for value in (
            Decimal("1.25"),
            float("nan"),
            math.inf,
            2**53,
            b"bytes",
            object(),
        ):
            with self.subTest(value=repr(value)), self.assertRaises(OutputError):
                record(payload={"value": value})

    def test_payload_rejects_lone_surrogate_and_empty_or_non_string_keys(self):
        for payload in ({"value": "\ud800"}, {"": 1}, {1: "one"}):
            with self.subTest(payload=repr(payload)), self.assertRaises(OutputError):
                record(payload=payload)

    def test_artifact_and_composite_require_nonempty_unique_refs(self):
        item = ArtifactItem(
            ArtifactRef("artifact-a", ARTIFACT_DIGEST, 42, "application/pdf")
        )
        artifact = ArtifactOutput(
            identity=OutputIdentity("artifact-output", "obs-artifact", OBSERVED_AT),
            operation=ScopedOperationIdentity("tenant-a", "stage-artifact"),
            output_set_id="set-a",
            binding=binding(),
            artifacts=(item,),
        )
        composite = CompositeOutput(
            identity=OutputIdentity("composite-output", "obs-composite", OBSERVED_AT),
            operation=ScopedOperationIdentity("tenant-a", "stage-composite"),
            output_set_id="set-a",
            binding=binding(),
            payload_contract=CONTRACT,
            payload={},
            artifacts=(item,),
        )
        self.assertEqual(artifact.output_type, "artifact")
        self.assertEqual(composite.output_type, "composite")
        with self.assertRaises(OutputError):
            ArtifactDescription()
        with self.assertRaises(OutputError):
            ArtifactOutput(
                identity=OutputIdentity("bad", "obs", OBSERVED_AT),
                operation=ScopedOperationIdentity("tenant-a", "bad-op"),
                output_set_id="set-a",
                binding=binding(),
                artifacts=(),
            )
        with self.assertRaises(OutputError):
            ArtifactOutput(
                identity=OutputIdentity("bad-duplicate", "obs", OBSERVED_AT),
                operation=ScopedOperationIdentity("tenant-a", "bad-op-2"),
                output_set_id="set-a",
                binding=binding(),
                artifacts=(item, item),
            )

    def test_artifact_reference_validates_identity_digest_and_size(self):
        with self.assertRaises(OutputError):
            ArtifactRef("artifact-a", "bad-digest", 1)
        with self.assertRaises(OutputError):
            ArtifactRef("artifact-a", ARTIFACT_DIGEST, -1)
        with self.assertRaises(OutputError):
            ArtifactRef("artifact-a", ARTIFACT_DIGEST, True)  # type: ignore[arg-type]
        with self.assertRaises(OutputError):
            ArtifactRef("artifact-a", ARTIFACT_DIGEST, 1, "")

    def test_artifact_description_has_display_semantics_not_paths(self):
        description = ArtifactDescription(file_name="summary.pdf", role="attachment")
        self.assertEqual(
            description.to_wire(), {"file_name": "summary.pdf", "role": "attachment"}
        )
        with self.assertRaises(OutputError):
            ArtifactDescription(file_name="../secret.pdf")

    def test_run_attempt_must_be_a_pair_and_fence_needs_execution(self):
        with self.assertRaises(OutputError):
            ExecutionBinding(run_id="run-only")
        with self.assertRaises(OutputError):
            ExecutionBinding(fencing_token=1)
        with self.assertRaises(OutputError):
            ExecutionBinding("run", "attempt", 0)

    def test_producer_release_and_capability_are_optional_paired_extensions(self):
        capability = ProducerIdentity("manual-ingest", "2", "file-import", "v1")
        self.assertIsNone(capability.release_id)
        with self.assertRaises(OutputError):
            ProducerIdentity("producer", "1", release_id="release-without-digest")
        with self.assertRaises(OutputError):
            ProducerIdentity(
                "producer", "1", capability_id="capability-without-version"
            )

    def test_derived_output_id_is_stable_observation_identity_not_content_identity(
        self,
    ):
        producer = ProducerIdentity("source-adapter", "1")
        first = derive_output_id(
            tenant_id="tenant-a",
            source=SourceRevisionRef("source-a", "revision-1"),
            producer=producer,
            output_key="main-record",
            observation_id="obs-1",
        )
        repeat = derive_output_id(
            tenant_id="tenant-a",
            source=SourceRevisionRef("source-a", "revision-1"),
            producer=producer,
            output_key="main-record",
            observation_id="obs-1",
        )
        later = derive_output_id(
            tenant_id="tenant-a",
            source=SourceRevisionRef("source-a", "revision-1"),
            producer=producer,
            output_key="main-record",
            observation_id="obs-2",
        )
        other_output = derive_output_id(
            tenant_id="tenant-a",
            source=SourceRevisionRef("source-a", "revision-1"),
            producer=producer,
            output_key="attachment",
            observation_id="obs-1",
        )
        other_source = derive_output_id(
            tenant_id="tenant-a",
            source=SourceRevisionRef("source-b", "revision-1"),
            producer=producer,
            output_key="main-record",
            observation_id="obs-1",
        )
        self.assertEqual(first, repeat)
        self.assertNotEqual(first, later)
        self.assertNotEqual(first, other_output)
        self.assertNotEqual(first, other_source)
        self.assertTrue(first.startswith("out-"))

    def test_payload_digest_and_output_intent_are_distinct(self):
        first = record()
        changed_observation = record(
            output_id="output-b", observation_id="observation-b", operation_id="stage-b"
        )
        self.assertEqual(
            first.to_wire()["payload_digest"],
            changed_observation.to_wire()["payload_digest"],
        )
        self.assertNotEqual(
            output_intent_digest(first), output_intent_digest(changed_observation)
        )

    def test_observation_time_is_timezone_aware_and_normalized_to_utc(self):
        offset_time = datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3)))
        identity = OutputIdentity("output-time", "obs-time", offset_time)
        self.assertEqual(identity.observed_at, OBSERVED_AT)
        with self.assertRaises(OutputError):
            OutputIdentity("output-naive", "obs-naive", datetime(2026, 1, 1))

    def test_rfc8785_canonicalization_vectors(self):
        path = (
            ROOT / "contracts/acquisition-output/draft/v1/canonicalization-vectors.json"
        )
        data = json.loads(path.read_text())
        for vector in data["vectors"]:
            with self.subTest(vector=vector["name"]):
                canonical = canonical_json(vector["input"])
                self.assertEqual(canonical.decode("utf-8"), vector["canonical_utf8"])
                self.assertEqual(canonical_digest(vector["input"]), vector["sha256"])

    def test_output_set_seals_expected_count_and_manifest_digest(self):
        root = OutputSet(set_spec(expected=1)).record_staged(
            record(), custody_receipts=0
        )
        self.assertEqual(root.state, OutputSetState.STAGING)
        with self.assertRaises(OutputConflict):
            root.seal(
                member_count=0,
                encoded_output_bytes=0,
                manifest_digest="sha256:" + "0" * 64,
                at=root.created_at,
            )
        with self.assertRaises(OutputConflict):
            root.seal(
                member_count=1,
                encoded_output_bytes=root.encoded_output_bytes + 1,
                manifest_digest="sha256:" + "0" * 64,
                at=root.created_at,
            )

    def test_streaming_manifest_digest_matches_v1_materialized_digest(self):
        entries = (
            MemberManifestEntry("output-a", "obs-a", "op-a", "sha256:" + "a" * 64),
            MemberManifestEntry("output-b", "obs-b", "op-b", "sha256:" + "b" * 64),
        )
        root = OutputSet(set_spec()).record_staged(record(), custody_receipts=0)
        snapshot = OutputSetSnapshot(
            output_set_id=root.output_set_id,
            binding=root.binding,
            state=OutputSetState.STAGING,
            payload_contracts=root.spec.payload_contracts,
            member_count=2,
            encoded_output_bytes=123,
            artifact_reference_count=0,
            custody_reference_count=0,
            manifest_digest=None,
            created_at=root.created_at,
            sealed_at=None,
            published_at=None,
        )
        full_manifest = OutputSetManifest(
            output_set_id=snapshot.output_set_id,
            binding=snapshot.binding,
            state=snapshot.state,
            payload_contracts=snapshot.payload_contracts,
            member_count=snapshot.member_count,
            encoded_output_bytes=snapshot.encoded_output_bytes,
            artifact_reference_count=snapshot.artifact_reference_count,
            custody_reference_count=snapshot.custody_reference_count,
            members=entries,
            manifest_digest=None,
            created_at=snapshot.created_at,
            sealed_at=None,
            published_at=None,
        )
        expected = manifest_digest(full_manifest)
        self.assertEqual(stream_manifest_digest(snapshot, iter(entries)), expected)
        with self.assertRaises(OutputError):
            stream_manifest_digest(snapshot, iter(reversed(entries)))

    def test_fencing_token_rejects_bool_and_out_of_interoperable_range(self):
        with self.assertRaises(OutputError):
            ExecutionBinding(run_id="run", attempt_id="attempt", fencing_token=True)
        with self.assertRaises(OutputError):
            ExecutionBinding(run_id="run", attempt_id="attempt", fencing_token=1 << 53)

    def test_artifact_size_rejects_out_of_interoperable_range(self):
        with self.assertRaises(OutputError):
            ArtifactRef("artifact", ARTIFACT_DIGEST, 1 << 53)

    def test_output_set_state_machine_rejects_invalid_transitions(self):
        root = OutputSet(set_spec())
        with self.assertRaises(InvalidTransition):
            root.publish(manifest_digest="sha256:" + "0" * 64, at=root.created_at)
        aborted = root.abort("fixture cancelled")
        self.assertEqual(aborted.state, OutputSetState.ABORTED)
        self.assertEqual(aborted.abort("fixture cancelled"), aborted)
        with self.assertRaises(InvalidTransition):
            aborted.record_staged(record(), custody_receipts=0)
        with self.assertRaises(OutputConflict):
            aborted.abort("different reason")

    def test_publication_rejects_artifact_membership_without_custody_confirmation(self):
        root = OutputSet(set_spec())
        invalid = replace(
            root,
            state=OutputSetState.SEALED,
            manifest_digest="sha256:" + "0" * 64,
            artifact_reference_count=1,
            custody_reference_count=0,
        )
        with self.assertRaises(OutputError):
            invalid.publish(manifest_digest=invalid.manifest_digest, at=OBSERVED_AT)


if __name__ == "__main__":
    unittest.main()
