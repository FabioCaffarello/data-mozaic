from __future__ import annotations

import unittest
from datetime import UTC, datetime

from acquisition_output.adapters.mongodb import mapper
from acquisition_output.domain import (
    ArtifactItem,
    ArtifactOutput,
    ArtifactRef,
    OutputIdentity,
    OutputSet,
    OutputSetSnapshot,
    OutputSetSpec,
    ScopedOperationIdentity,
    output_intent_digest,
)
from test_domain import ARTIFACT_DIGEST, OBSERVED_AT, binding, record, set_spec


class MongoBsonMappingTests(unittest.TestCase):
    def test_record_wire_round_trip_preserves_intent_digest_and_unicode(self):
        output = record(
            payload={"name": "São José 🧩", "nested": {"nullable": None, "n": 1.25}}
        )
        document = mapper.output_document(output, output_intent_digest(output), ())
        from bson import BSON

        restored = BSON.encode(document)
        value = BSON(restored).decode()
        mapped = mapper.output_from_document(value)
        self.assertEqual(mapped.to_wire(), output.to_wire())
        self.assertEqual(output_intent_digest(mapped), output_intent_digest(output))
        self.assertEqual(mapper.bson_size(document), len(restored))

    def test_artifact_output_omits_absent_payload_digest(self):
        output = ArtifactOutput(
            identity=OutputIdentity(
                "artifact-output", "artifact-observation", OBSERVED_AT
            ),
            operation=ScopedOperationIdentity("tenant-a", "artifact-op"),
            output_set_id="set-a",
            binding=binding(),
            artifacts=(ArtifactItem(ArtifactRef("artifact-a", ARTIFACT_DIGEST, 42)),),
        )
        document = mapper.output_document(output, output_intent_digest(output), ())
        self.assertNotIn("payload_digest", document)
        self.assertNotIn("payload_digest", document["wire"])

    def test_domain_microseconds_round_trip_without_bson_millisecond_loss(self):
        instant = datetime(2026, 11, 24, 12, 34, 56, 123456, tzinfo=UTC)
        stored = mapper.datetime_to_epoch_us(instant)
        self.assertEqual(mapper.datetime_from_epoch_us(stored), instant)

    def test_snapshot_and_manifest_mapping_stays_bounded(self):
        spec: OutputSetSpec = set_spec()
        root = OutputSet(spec=spec)
        document = mapper.set_document(root)
        restored = mapper.snapshot_from_document(document)
        self.assertIsInstance(restored, OutputSetSnapshot)
        self.assertEqual(restored.output_set_id, spec.output_set_id)
        self.assertFalse(hasattr(restored, "members"))

    def test_operation_storage_identity_is_stable_and_not_domain_identity(self):
        first = mapper.operation_storage_id("tenant", "operation")
        second = mapper.operation_storage_id("tenant", "operation")
        self.assertEqual(first, second)
        self.assertNotEqual(first, "operation")
        self.assertEqual(
            ScopedOperationIdentity("tenant", "operation").operation_id, "operation"
        )


if __name__ == "__main__":
    unittest.main()
