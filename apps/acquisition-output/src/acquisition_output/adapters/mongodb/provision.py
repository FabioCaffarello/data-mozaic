"""Versioned, drift-aware BC05 MongoDB collection and index provisioning."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING
from pymongo.errors import CollectionInvalid

from .errors import MongoProvisioningDrift

SCHEMA_VERSION = 2
MIGRATION_CHECKSUMS = {
    1: "sha256:b90f70fe4290e60674fa047c59df8ee97e51137241f5605c2cc5a8f43defac74",
    2: "sha256:cc0aa810bbc48dfe0f148e454ed64de79b470e41afd3530933fc798056cac886",
}

COLLECTIONS: dict[str, dict[str, Any]] = {
    "output_sets": {
        "bsonType": "object",
        "required": [
            "_id",
            "tenant_id",
            "output_set_id",
            "binding",
            "spec",
            "state",
            "revision",
            "counters",
            "created_at_us",
        ],
        "properties": {
            "_id": {"bsonType": "string"},
            "tenant_id": {"bsonType": "string"},
            "output_set_id": {"bsonType": "string"},
            "state": {
                "enum": ["staging", "sealed", "published", "aborted", "rejected"]
            },
            "revision": {"bsonType": "int", "minimum": 0},
            "counters": {
                "bsonType": "object",
                "required": [
                    "member_count",
                    "encoded_output_bytes",
                    "artifact_reference_count",
                    "custody_reference_count",
                ],
            },
            "created_at_us": {"bsonType": "long"},
        },
    },
    "capture_outputs": {
        "bsonType": "object",
        "required": [
            "_id",
            "tenant_id",
            "output_set_id",
            "output_id",
            "observation_id",
            "operation_id",
            "intent_digest",
            "canonical_output_bytes",
            "artifact_reference_count",
            "custody_reference_count",
            "wire",
            "custody_receipts",
        ],
        "properties": {
            "_id": {"bsonType": "string"},
            "tenant_id": {"bsonType": "string"},
            "output_set_id": {"bsonType": "string"},
            "output_id": {"bsonType": "string"},
            "observation_id": {"bsonType": "string"},
            "operation_id": {"bsonType": "string"},
            "intent_digest": {"bsonType": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "canonical_output_bytes": {"bsonType": "int", "minimum": 1},
            "wire": {"bsonType": "object"},
            "custody_receipts": {"bsonType": "array"},
        },
    },
    "output_operations": {
        "bsonType": "object",
        "required": [
            "_id",
            "tenant_id",
            "operation_id",
            "intent_digest",
            "result_type",
            "result",
            "created_at_us",
        ],
        "properties": {
            "_id": {"bsonType": "string"},
            "tenant_id": {"bsonType": "string"},
            "operation_id": {"bsonType": "string"},
            "intent_digest": {"bsonType": "string"},
            "result_type": {
                "enum": ["output_receipt", "publication_receipt", "snapshot"]
            },
            "created_at_us": {"bsonType": "long"},
        },
    },
    "output_outbox": {
        "bsonType": "object",
        "required": [
            "_id",
            "event_id",
            "event_source",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "tenant_id",
            "occurred_at_us",
            "correlation_id",
            "causation_id",
            "payload",
            "state",
            "created_at_us",
        ],
        "properties": {
            "_id": {"bsonType": "string"},
            "event_id": {"bsonType": "string"},
            "event_type": {"bsonType": "string"},
            "state": {"enum": ["pending", "delivered"]},
            "payload": {"bsonType": "object"},
        },
    },
    "bc05_schema_migrations": {
        "bsonType": "object",
        "required": ["_id", "version", "applied_at_us", "checksum"],
        "properties": {
            "_id": {"bsonType": "string"},
            "version": {"bsonType": "int"},
            "applied_at_us": {"bsonType": "long"},
            "checksum": {"bsonType": "string"},
        },
    },
}

INDEXES: dict[str, tuple[tuple[str, tuple[tuple[str, int], ...], bool], ...]] = {
    "output_sets": (
        (
            "uq_tenant_set",
            (("tenant_id", ASCENDING), ("output_set_id", ASCENDING)),
            True,
        ),
    ),
    "capture_outputs": (
        (
            "uq_tenant_set_output",
            (
                ("tenant_id", ASCENDING),
                ("output_set_id", ASCENDING),
                ("output_id", ASCENDING),
            ),
            True,
        ),
    ),
    "output_operations": (
        (
            "uq_tenant_operation",
            (("tenant_id", ASCENDING), ("operation_id", ASCENDING)),
            True,
        ),
    ),
    "output_outbox": (
        ("uq_event_id", (("event_id", ASCENDING),), True),
        (
            "ix_pending_outbox",
            (
                ("state", ASCENDING),
                ("created_at_us", ASCENDING),
                ("event_id", ASCENDING),
            ),
            False,
        ),
        (
            "ix_tenant_pending_outbox",
            (
                ("tenant_id", ASCENDING),
                ("state", ASCENDING),
                ("created_at_us", ASCENDING),
                ("event_id", ASCENDING),
            ),
            False,
        ),
    ),
    "bc05_schema_migrations": (("uq_version", (("version", ASCENDING),), True),),
}


def provision(database: Any) -> None:
    """Create expected collections/indexes; fail closed on validator/index drift."""
    existing = set(database.list_collection_names())
    for name, schema in COLLECTIONS.items():
        if name not in existing:
            try:
                database.create_collection(
                    name,
                    validator={"$jsonSchema": schema},
                    validationLevel="strict",
                    validationAction="error",
                )
            except CollectionInvalid:
                pass
        actual = next(
            (item for item in database.list_collections(filter={"name": name})), None
        )
        if actual is None:
            raise MongoProvisioningDrift(f"collection {name} could not be inspected")
        expected_validator = {"$jsonSchema": schema}
        if actual.get("options", {}).get("validator") != expected_validator:
            raise MongoProvisioningDrift(
                f"collection {name} validator differs from BC05 schema version {SCHEMA_VERSION}"
            )
        if (
            actual.get("options", {}).get("validationLevel", "strict") != "strict"
            or actual.get("options", {}).get("validationAction", "error") != "error"
        ):
            raise MongoProvisioningDrift(
                f"collection {name} validation policy differs from BC05 requirements"
            )
        collection = database[name]
        current = {index["name"]: index for index in collection.list_indexes()}
        for index_name, keys, unique in INDEXES[name]:
            if index_name in current:
                index = current[index_name]
                if (
                    list(index["key"].items()) != list(keys)
                    or bool(index.get("unique", False)) != unique
                ):
                    raise MongoProvisioningDrift(
                        f"index {name}.{index_name} differs from BC05 requirements"
                    )
                continue
            collection.create_index(list(keys), name=index_name, unique=unique)
    migrations = database["bc05_schema_migrations"]
    for version, checksum in MIGRATION_CHECKSUMS.items():
        existing_migration = migrations.find_one({"version": version})
        if (
            version == 1
            and existing_migration
            and existing_migration["checksum"] == "sha256:bc05-mongodb-schema-v1"
        ):
            # Upgrade the checksum marker written by the local pre-release schema
            # prototype. No collections, indexes, or domain records are changed.
            migrations.update_one(
                {"version": version, "checksum": "sha256:bc05-mongodb-schema-v1"},
                {"$set": {"checksum": checksum}},
            )
            existing_migration["checksum"] = checksum
        if existing_migration and existing_migration["checksum"] != checksum:
            raise MongoProvisioningDrift(
                f"BC05 schema migration v{version} checksum differs"
            )
        if existing_migration is None:
            migrations.insert_one(
                {
                    "_id": f"v{version}",
                    "version": version,
                    "applied_at_us": int(datetime.now(UTC).timestamp() * 1_000_000),
                    "checksum": checksum,
                }
            )
