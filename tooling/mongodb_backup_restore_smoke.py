"""Non-destructive mongodump/mongorestore smoke into an isolated DB namespace."""

from __future__ import annotations

import os
import subprocess
from uuid import uuid4

from acquisition_output.adapters.mongodb.config import MongoProvisioningSettings
from pymongo import MongoClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = "deployments/compose/mongodb.compose.yaml"


def run_compose_exec(shell: str) -> None:
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env.mongodb",
        "-f",
        COMPOSE,
        "exec",
        "-T",
        "mongodb",
        "bash",
        "-ec",
        shell,
    ]
    completed = subprocess.run(
        command, check=False, cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    if completed.returncode:
        raise RuntimeError(
            f"MongoDB backup/restore tooling failed with exit {completed.returncode}"
        )


def signature(database):
    sets = list(
        database.output_sets.find(
            {},
            {
                "tenant_id": 1,
                "output_set_id": 1,
                "state": 1,
                "manifest_digest": 1,
                "counters.member_count": 1,
                "publication_receipt": 1,
            },
        ).sort([("tenant_id", 1), ("output_set_id", 1)])
    )
    operations = list(
        database.output_operations.find(
            {},
            {"tenant_id": 1, "operation_id": 1, "intent_digest": 1, "result_type": 1},
        ).sort([("tenant_id", 1), ("operation_id", 1)])
    )
    outputs = list(
        database.capture_outputs.find(
            {}, {"tenant_id": 1, "output_set_id": 1, "output_id": 1, "intent_digest": 1}
        ).sort([("tenant_id", 1), ("output_set_id", 1), ("output_id", 1)])
    )
    events = list(
        database.output_outbox.find(
            {}, {"event_id": 1, "event_type": 1, "aggregate_id": 1, "state": 1}
        ).sort("event_id", 1)
    )

    def clean(rows):
        return [
            {key: value for key, value in row.items() if key != "_id"} for row in rows
        ]

    return {
        "sets": clean(sets),
        "operations": clean(operations),
        "outputs": clean(outputs),
        "events": clean(events),
    }


def metadata_signature(database):
    names = (
        "output_sets",
        "capture_outputs",
        "output_operations",
        "output_outbox",
        "bc05_schema_migrations",
    )
    result = {}
    for name in names:
        definition = next(
            item for item in database.list_collections(filter={"name": name})
        )
        indexes = list(database[name].list_indexes())
        result[name] = {
            "validator": definition.get("options", {}).get("validator"),
            "indexes": sorted(
                (
                    index["name"],
                    list(index["key"].items()),
                    index.get("unique", False),
                )
                for index in indexes
            ),
        }
    return result


def main() -> int:
    settings = MongoProvisioningSettings.from_env()
    client = MongoClient(
        settings.uri,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=3000,
        timeoutMS=20000,
    )
    client.admin.command("ping")
    source = client[settings.database]
    restore_name = f"bc05_restore_smoke_{uuid4().hex[:12]}"
    expected = signature(source)
    expected_metadata = metadata_signature(source)
    archive = f"/tmp/{restore_name}.archive"
    root = '"$MONGO_INITDB_ROOT_USERNAME"'
    password = '"$MONGO_INITDB_ROOT_PASSWORD"'
    common = f"--username {root} --password {password} --authenticationDatabase admin --host 127.0.0.1"
    run_compose_exec(
        f"mongodump {common} --db {settings.database} --archive={archive} && "
        f"mongorestore {common} --archive={archive} --nsFrom='{settings.database}.*' "
        f"--nsTo='{restore_name}.*'"
    )
    restored = client[restore_name]
    actual = signature(restored)
    if actual != expected:
        raise RuntimeError(
            "restored BC05 identity, manifest, receipt, operation or event signature differs"
        )
    if metadata_signature(restored) != expected_metadata:
        raise RuntimeError("restored BC05 collection validators or indexes differ")
    print(
        f"Backup/restore smoke verified into isolated namespace {restore_name}; namespace retained for inspection."
    )
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
