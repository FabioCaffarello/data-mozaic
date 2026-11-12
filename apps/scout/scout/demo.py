"""Fixture-only composition for the offline Scout demonstration."""

import json
from datetime import UTC, datetime, timedelta

from .application import ScoutApplication, projection
from .domain import PolicyDecisionRef, ScoutScope, SourceRef
from .edge import ROOT, validate
from .fakes import (
    FakeEvidenceCustody,
    FakePolicy,
    FakeProposalHandoff,
    FakeSessionRepository,
    FakeSourceLookup,
)
from .outbox_relay import OutboxRelay


def main():
    request = json.loads(
        (ROOT / "contracts/scout/v1/fixtures/valid/ScoutRequest.json").read_text()
    )
    validate("ScoutRequest", request)
    source = SourceRef(**request["source"])
    raw = request["requested_scope"]
    scope = ScoutScope(
        tuple(raw["origins"]),
        tuple(raw["path_prefixes"]),
        tuple(raw["methods"]),
        raw["max_requests"],
        raw["max_duration_seconds"],
    )
    now = datetime.now(UTC)
    policy = FakePolicy(
        PolicyDecisionRef(
            "policy-fixture",
            request["requested_by"],
            source,
            True,
            now + timedelta(hours=1),
            scope,
        )
    )
    repository = FakeSessionRepository()
    receiver = FakeProposalHandoff()
    app = ScoutApplication(
        FakeSourceLookup({source: scope}), policy, repository, FakeEvidenceCustody()
    )
    fixture = (ROOT / "apps/scout/fixtures/page.txt").read_bytes()
    session = app.run(request, request["requested_by"], fixture, now)
    OutboxRelay(repository, receiver, jitter=lambda: 0).run_once(now)
    session = app.run(request, request["requested_by"], fixture, now)
    payload = projection(session.proposal)
    validate("PlaybookProposal", payload)
    print(
        json.dumps(
            {
                "state": session.state,
                "handoff": session.handoff_result,
                "proposal": payload,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
