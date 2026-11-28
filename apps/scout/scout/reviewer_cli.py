"""Dedicated OS-user promotion command; never exposed through the agent MCP."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

from acquisition_lifecycle.cli import _application as lifecycle_application
from collector_engineering.playbook_v2 import validate_playbook_v2

from .adapters.local_custody import LocalFileEvidenceCustody
from .adapters.postgres.repository import PostgresScoutSessionRepository
from .application import ScoutApplication
from .domain import AuthorizationError
from .proposal_workflow import ProposalDiagnostic, ProposalWorkflow
from .reviewer_identity import (
    DEFAULT_REVIEWER_POLICY,
    AuthenticatedReviewerAccess,
    AuthenticatedReviewerPromotion,
    ReviewerPromotionAdapter,
    UnixReviewerIdentityProvider,
)
from .tool_host.composition import _LocalSourceLookup
from .tools.policy import LocalAuthorizationStore


def _protected_file(name: str) -> Path:
    raw = os.environ.get(name, "")
    if not raw:
        raise AuthorizationError(f"managed reviewer setting {name} is required")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise AuthorizationError(f"managed reviewer setting {name} is invalid")
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise AuthorizationError(f"managed reviewer setting {name} is not protected")
    return path


def _build_adapter() -> ReviewerPromotionAdapter:
    identity = UnixReviewerIdentityProvider(DEFAULT_REVIEWER_POLICY).current()
    database_url = os.environ.get("SCOUT_DATABASE_URL", "")
    evidence_root = Path(os.environ.get("SCOUT_EVIDENCE_ROOT", ""))
    if not database_url or not evidence_root.is_absolute():
        raise AuthorizationError("managed reviewer data access is unavailable")
    source_policy = LocalAuthorizationStore.from_file(
        _protected_file("SCOUT_REVIEW_SOURCE_REGISTRY")
    )
    scout = ScoutApplication(
        sources=_LocalSourceLookup(source_policy),
        policy=None,
        repository=PostgresScoutSessionRepository(database_url),
        custody=LocalFileEvidenceCustody(evidence_root),
        proposal_review_access=AuthenticatedReviewerAccess(identity),
    )

    class CandidateValidator:
        def validate(self, candidate):
            result = validate_playbook_v2(candidate)
            return tuple(
                ProposalDiagnostic(item.code, item.path, item.message)
                for item in result.diagnostics
            )

    lifecycle, _store = lifecycle_application(require_signer=False)

    class PlaybookRegistration:
        def register(self, candidate, *, actor_id):
            return lifecycle.register_playbook_document(candidate, actor_id=actor_id)

    workflow = ProposalWorkflow(
        scout,
        CandidateValidator(),
        AuthenticatedReviewerPromotion(identity),
        PlaybookRegistration(),
    )
    return ReviewerPromotionAdapter(UnixReviewerIdentityProvider(), workflow)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scout-review")
    commands = parser.add_subparsers(dest="command", required=True)
    promote = commands.add_parser("promote")
    promote.add_argument("--session-id", required=True)
    promote.add_argument("--proposal-id", required=True)
    promote.add_argument("--proposal-revision", type=int, required=True)
    promote.add_argument("--candidate-sha256", required=True)
    promote.add_argument(
        "--confirm-candidate-sha256",
        required=True,
        help="repeat the exact digest from the reviewed handoff",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "promote":
        return 2
    if (
        args.proposal_revision < 1
        or not args.proposal_id
        or len(args.candidate_sha256) != 64
        or any(char not in "0123456789abcdef" for char in args.candidate_sha256)
        or args.candidate_sha256 != args.confirm_candidate_sha256
    ):
        raise SystemExit(
            "promotion confirmation must exactly match the candidate digest"
        )
    try:
        result = _build_adapter().promote(
            args.session_id,
            args.proposal_id,
            args.proposal_revision,
            args.candidate_sha256,
            now=datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 - command boundary must fail closed without leaking configuration
        print(
            "promotion failed or was denied; inspect protected operator logs",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
