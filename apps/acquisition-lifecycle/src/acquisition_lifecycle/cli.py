"""Narrow local operator commands for acquisition lifecycle records."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from mozaic_contract_resolution import ContractReference, ImmutableContractRegistry
from psycopg import Error as PsycopgError

from .adapters.bc04_authority import BC04AuthoritySnapshotAdapter
from .adapters.postgres import PostgresLifecycleStore
from .adapters.signing import Ed25519AuthoritySigner
from .application import AcquisitionLifecycle
from .domain import LifecycleError, PolicyRevisionRef, SourceRevisionRef

ROOT = Path(__file__).resolve().parents[4]


class LocalOperatorApproval:
    """Explicit local operator allowlist; it is not a general identity service."""

    def __init__(self, actors: set[str]):
        self._actors = actors

    def assert_can_approve(self, actor_id: str, release_id: str) -> str:
        if actor_id not in self._actors:
            from .domain import LifecycleError

            raise LifecycleError("operator is not authorized for release approval")
        return f"operator://{actor_id}/release/{release_id}"


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise ValueError("lifecycle document path is unsafe or exceeds the size limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("lifecycle document contains duplicate properties")
            value[key] = item
        return value

    loaded = json.loads(
        path.read_bytes(),
        object_pairs_hook=pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("lifecycle document contains a non-finite number")
        ),
    )
    if not isinstance(loaded, dict):
        raise TypeError("lifecycle document root must be an object")
    return loaded


def _secret_file(env_name: str, expected_size: int, *, private: bool) -> bytes:
    value = os.environ.get(env_name)
    if not value:
        raise ValueError(f"{env_name} is required")
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{env_name} must identify a regular file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if private and mode & 0o077:
        raise ValueError(f"{env_name} permissions must be owner-only")
    content = path.read_bytes()
    if len(content) != expected_size:
        raise ValueError(f"{env_name} has an invalid byte length")
    return content


def _application(
    *, require_signer: bool = True
) -> tuple[AcquisitionLifecycle, PostgresLifecycleStore]:
    dsn = os.environ.get("LIFECYCLE_DATABASE_URL") or os.environ.get(
        "BC04_DATABASE_URL"
    )
    if not dsn:
        raise LifecycleError("LIFECYCLE_DATABASE_URL or BC04_DATABASE_URL is required")
    store = PostgresLifecycleStore(dsn, ROOT / "migrations")
    bc04 = PostgresDurableRunRepository(dsn)
    contract_root = Path(
        os.environ.get(
            "MOZAIC_PAYLOAD_CONTRACT_ROOT", ROOT / "contracts/payload/draft/v1"
        )
    )
    contracts = ImmutableContractRegistry(contract_root)
    if require_signer:
        key_id = os.environ.get("MOZAIC_AUTHORITY_KEY_ID", "")
        seed = _secret_file("MOZAIC_AUTHORITY_SIGNING_KEY_FILE", 32, private=True)
        trusted_public = _secret_file(
            "MOZAIC_AUTHORITY_TRUSTED_PUBLIC_KEY_FILE", 32, private=False
        )
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        derived = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if derived != trusted_public:
            raise LifecycleError(
                "configured authority signing and trust keys do not match"
            )
        signer = Ed25519AuthoritySigner(key_id, private_key)
        public_keys = {key_id: trusted_public}
    else:
        signer = _NoopSigner()
        public_keys = {}
    actors = {
        actor.strip()
        for actor in os.environ.get("MOZAIC_RELEASE_APPROVERS", "").split(",")
        if actor.strip()
    }
    application = AcquisitionLifecycle(
        store,
        contracts,
        BC04AuthoritySnapshotAdapter(bc04),
        signer,
        public_keys,
        LocalOperatorApproval(actors),
        authority_id=os.environ.get(
            "MOZAIC_AUTHORITY_ID", "local-acquisition-authority"
        ),
    )
    return application, store


class _NoopSigner:
    key_id = "unavailable"

    def sign(self, canonical_snapshot: bytes) -> str:
        raise RuntimeError("authority signing is unavailable in this command")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acquisition-lifecycle")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    for name in ("register-source", "register-policy"):
        command = commands.add_parser(name)
        command.add_argument("--document", type=Path, required=True)
        command.add_argument("--actor", required=True)
    source_revoke = commands.add_parser("revoke-source")
    source_revoke.add_argument("--source-id", required=True)
    source_revoke.add_argument("--revision-id", required=True)
    source_revoke.add_argument("--actor", required=True)
    source_resolve = commands.add_parser("resolve-source")
    source_resolve.add_argument("--source-id", required=True)
    source_resolve.add_argument("--revision-id", required=True)
    policy_revoke = commands.add_parser("revoke-policy")
    policy_revoke.add_argument("--policy-id", required=True)
    policy_revoke.add_argument("--version", required=True)
    policy_revoke.add_argument("--actor", required=True)
    policy_resolve = commands.add_parser("resolve-policy")
    policy_resolve.add_argument("--policy-id", required=True)
    policy_resolve.add_argument("--version", required=True)
    playbook = commands.add_parser("register-playbook")
    playbook.add_argument("--path", type=Path, required=True)
    playbook.add_argument("--actor", required=True)
    build = commands.add_parser("register-build")
    build.add_argument("--path", type=Path, required=True)
    build.add_argument("--runtime-image-digest", required=True)
    build.add_argument("--actor", required=True)
    candidate = commands.add_parser("create-candidate")
    candidate.add_argument("--document", type=Path, required=True)
    candidate.add_argument("--actor", required=True)
    for name in ("approve", "revoke", "resolve-release", "inspect-release"):
        command = commands.add_parser(name)
        command.add_argument("--release-id", required=True)
        if name in {"approve", "revoke"}:
            command.add_argument("--actor", required=True)
    commands.add_parser("publish-authority")
    commands.add_parser("inspect")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "migrate":
            dsn = os.environ.get("LIFECYCLE_OWNER_DATABASE_URL") or os.environ.get(
                "BC04_OWNER_DATABASE_URL"
            )
            if not dsn:
                raise LifecycleError(
                    "LIFECYCLE_OWNER_DATABASE_URL or BC04_OWNER_DATABASE_URL is required for migrations"
                )
            store = PostgresLifecycleStore(dsn, ROOT / "migrations")
            store.migrate()
            store.grant_runtime_role(os.environ.get("LIFECYCLE_APP_ROLE", "bc04_app"))
            print("acquisition lifecycle migrations current; runtime grants applied")
            return 0
        app, store = _application(require_signer=args.command == "publish-authority")
        if args.command == "register-source":
            result = app.register_source_revision(
                _json(args.document), actor_id=args.actor
            )
            print(
                json.dumps(
                    {
                        "source_id": result.source_id,
                        "revision_id": result.revision_id,
                        "digest": result.digest,
                    }
                )
            )
        elif args.command == "resolve-source":
            print(
                json.dumps(
                    store.resolve_source(args.source_id, args.revision_id),
                    sort_keys=True,
                )
            )
        elif args.command == "revoke-source":
            app.revoke_source_revision(
                args.source_id, args.revision_id, actor_id=args.actor
            )
            print(
                json.dumps(
                    {
                        "source_id": args.source_id,
                        "revision_id": args.revision_id,
                        "state": "revoked",
                    }
                )
            )
        elif args.command == "register-policy":
            result = app.register_policy_revision(
                _json(args.document), actor_id=args.actor
            )
            print(
                json.dumps(
                    {
                        "policy_id": result.policy_id,
                        "policy_version": result.policy_version,
                        "digest": result.digest,
                    }
                )
            )
        elif args.command == "resolve-policy":
            print(
                json.dumps(
                    store.resolve_policy(args.policy_id, args.version),
                    sort_keys=True,
                )
            )
        elif args.command == "revoke-policy":
            app.revoke_policy_revision(
                args.policy_id, args.version, actor_id=args.actor
            )
            print(
                json.dumps(
                    {
                        "policy_id": args.policy_id,
                        "version": args.version,
                        "state": "revoked",
                    }
                )
            )
        elif args.command == "register-playbook":
            digest = app.register_playbook_revision(args.path, actor_id=args.actor)
            print(json.dumps({"playbook_digest": digest}))
        elif args.command == "register-build":
            result = app.register_verified_build(
                args.path,
                runtime_image_digest=args.runtime_image_digest,
                actor_id=args.actor,
            )
            print(
                json.dumps(
                    {
                        key: value
                        for key, value in result.items()
                        if key != "manifest" and key != "artifact_path"
                    }
                )
            )
        elif args.command == "create-candidate":
            request = _json(args.document)
            contract = request["payload_contract"]
            result = app.create_release_candidate(
                release_id=request["release_id"],
                source=SourceRevisionRef(**request["source"]),
                policy=PolicyRevisionRef(**request["policy"]),
                build_id=request["build_id"],
                runtime_image_digest=request["runtime_image_digest"],
                contract=ContractReference(
                    contract["contract_id"], contract["version"], contract["digest"]
                ),
                playbook_digest=request["playbook_digest"],
                actor_id=args.actor,
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "approve":
            print(
                json.dumps(
                    app.approve_release(args.release_id, actor_id=args.actor),
                    sort_keys=True,
                )
            )
        elif args.command == "revoke":
            app.revoke_release(args.release_id, actor_id=args.actor)
            print(json.dumps({"release_id": args.release_id, "state": "revoked"}))
        elif args.command == "resolve-release":
            print(
                json.dumps(
                    app.resolve_eligible_release(args.release_id),
                    default=str,
                    sort_keys=True,
                )
            )
        elif args.command == "inspect-release":
            print(
                json.dumps(
                    app.inspect_release(args.release_id),
                    default=str,
                    sort_keys=True,
                )
            )
        elif args.command == "publish-authority":
            envelope, _authority = app.publish_authority_snapshot()
            envelope_doc = json.loads(envelope)
            snapshot = envelope_doc["snapshot"]
            print(
                json.dumps(
                    {
                        "authority_id": snapshot["authority_id"],
                        "generation": snapshot["generation"],
                        "snapshot_digest": snapshot["snapshot_digest"],
                        "envelope_bytes": len(envelope),
                    }
                )
            )
        elif args.command == "inspect":
            print(
                json.dumps(
                    {
                        "source_revisions": store.list_sources(),
                        "policies": store.list_policies(),
                        "releases": store.list_releases(),
                    },
                    sort_keys=True,
                )
            )
        return 0
    except (
        LifecycleError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        PsycopgError,
    ) as error:
        print(
            f"acquisition lifecycle command failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
