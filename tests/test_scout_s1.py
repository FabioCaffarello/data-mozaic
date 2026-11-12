"""Behavioral tests of the offline BC14 workflow and its boundaries."""

import ast
import copy
import hashlib
import json
import sys
import threading
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/scout"))

from scout.__main__ import PolicyDecisionRef, ScoutScope
from scout.application import ScoutApplication, projection
from scout.domain import (
    AuthorizationError,
    ConflictError,
    IntegrityError,
    ScoutObjective,
    ScoutSession,
    SourceRef,
    State,
    TransitionError,
)
from scout.edge import validate
from scout.fakes import (
    FakeEvidenceCustody,
    FakePolicy,
    FakeProposalHandoff,
    FakeSessionRepository,
    FakeSourceLookup,
)
from scout.outbox_relay import OutboxRelay
from scout.relay_worker import run_worker


class OfflineScoutTests(unittest.TestCase):
    def setUp(self):
        self.request = json.loads(
            (ROOT / "contracts/scout/v1/fixtures/valid/ScoutRequest.json").read_text()
        )
        self.fixture = (ROOT / "apps/scout/fixtures/page.txt").read_bytes()
        self.now = datetime(2026, 11, 12, tzinfo=UTC)
        self.source = SourceRef(**self.request["source"])
        self.scope = ScoutScope(
            ("https://example.org",), ("/catalog/",), ("GET",), 10, 60
        )
        self.decision = PolicyDecisionRef(
            "policy-1",
            "analyst-1",
            self.source,
            True,
            self.now + timedelta(hours=1),
            self.scope,
        )
        self.sources = FakeSourceLookup({self.source: self.scope})
        self.policy = FakePolicy(self.decision)
        self.repo, self.custody, self.handoff = (
            FakeSessionRepository(),
            FakeEvidenceCustody(),
            FakeProposalHandoff(),
        )
        self.app = ScoutApplication(self.sources, self.policy, self.repo, self.custody)

    def run_scout(self):
        validate("ScoutRequest", self.request)
        session = self.app.run(self.request, "analyst-1", self.fixture, self.now)
        if session.handoff_result == "delivery_pending":
            relay = OutboxRelay(self.repo, self.handoff, jitter=lambda: 0)
            status = relay.run_once(self.now)
            if status != "delivered":
                raise OSError(f"test relay result: {status}")
            session = self.app.run(self.request, "analyst-1", self.fixture, self.now)
        return session

    def test_complete_contract_digest_and_replay(self):
        session = self.run_scout()
        self.assertEqual(session.state, State.COMPLETED)
        self.assertEqual(session.handoff_result, "received_by_bc15")
        validate("PlaybookProposal", projection(session.proposal))
        artifact = session.manifests[0].artifacts[0]
        self.assertEqual(artifact.sha256, hashlib.sha256(self.fixture).hexdigest())
        self.assertTrue(self.custody.verify(artifact))
        self.assertEqual(self.run_scout(), session)
        self.request["objective"] = "Different"
        with self.assertRaises(ConflictError):
            self.run_scout()

    def test_application_returns_with_persisted_proposal_without_receiver_call(self):
        session = self.app.run(self.request, "analyst-1", self.fixture, self.now)
        self.assertEqual(session.state, State.EXPLORING)
        self.assertEqual(session.handoff_result, "delivery_pending")
        self.assertEqual(len(self.handoff.deliveries), 0)
        self.assertEqual(len(self.repo.outbox), 1)

    def test_independent_worker_finishes_inflight_message_before_graceful_stop(self):
        pending = self.app.run(self.request, "analyst-1", self.fixture, self.now)
        self.assertEqual(pending.handoff_result, "delivery_pending")
        stop = threading.Event()

        class StopAfterDelivery:
            def deliver_message(inner, message_id, payload):
                result = self.handoff.deliver_message(message_id, payload)
                stop.set()
                return result

        worker = OutboxRelay(self.repo, StopAfterDelivery(), jitter=lambda: 0)
        self.assertEqual(run_worker(worker, stop, idle_poll_seconds=0.01), 1)
        completed = self.app.run(self.request, "analyst-1", self.fixture, self.now)
        self.assertEqual(completed.state, State.COMPLETED)
        self.assertEqual(completed.handoff_result, "received_by_bc15")

    def test_one_shot_worker_exits_when_outbox_is_empty(self):
        self.assertEqual(
            run_worker(
                OutboxRelay(self.repo, self.handoff, jitter=lambda: 0),
                threading.Event(),
                max_messages=1,
            ),
            0,
        )

    def test_replay_requires_current_authorization(self):
        self.run_scout()
        self.policy.decision = replace(self.decision, revoked=True)
        with self.assertRaises(AuthorizationError):
            self.run_scout()

    def test_replay_authorization_gate_precedes_receipt_payload_load(self):
        self.run_scout()
        self.policy.available = False
        with (
            patch.object(
                self.repo,
                "find_intent",
                side_effect=AssertionError(
                    "receipt payload loaded before authorization"
                ),
            ),
            self.assertRaises(AuthorizationError),
        ):
            self.run_scout()

    def test_replay_scope_reduction_is_rejected_without_widening(self):
        completed = self.run_scout()
        original_scope = completed.authorization.scope
        reduced = replace(
            self.decision,
            scope=replace(self.scope, max_requests=1, max_duration_seconds=10),
        )
        self.policy.decision = reduced
        with self.assertRaises(AuthorizationError):
            self.run_scout()
        stored = self.repo.find_intent(
            "analyst-1",
            self.request["idempotency_key"],
            next(iter(self.repo.intents.values()))[0],
        )
        self.assertEqual(stored.authorization.scope, original_scope)

    def test_returned_session_is_detached_from_persisted_state(self):
        result = self.run_scout()
        object.__setattr__(result, "handoff_result", "approved")
        object.__setattr__(result, "findings", ())
        persisted = self.repo.find_intent(
            "analyst-1",
            self.request["idempotency_key"],
            next(iter(self.repo.intents.values()))[0],
        )
        self.assertEqual(persisted.handoff_result, "received_by_bc15")
        self.assertTrue(persisted.findings)

    def test_source_principal_and_policy_fail_closed(self):
        for decision in (
            replace(self.decision, allowed=False),
            replace(self.decision, revoked=True),
            replace(self.decision, expires_at=self.now),
            None,
        ):
            with self.subTest(decision=decision):
                self.policy.decision = decision
                with self.assertRaises(AuthorizationError):
                    self.run_scout()
        self.policy.decision = self.decision
        self.sources.sources = {}
        with self.assertRaises(IntegrityError):
            self.run_scout()
        self.sources.sources = {self.source: self.scope}
        self.request["source"]["revision"] = "different"
        with self.assertRaises(IntegrityError):
            self.run_scout()
        with self.assertRaises(IntegrityError):
            self.app.run(self.request, "other", self.fixture, self.now)

    def test_handoff_failure_retry(self):
        self.handoff.fail = True
        with self.assertRaises(OSError):
            self.run_scout()
        saved = next(iter(self.repo.sessions.values()))
        self.assertEqual(saved.state, State.EXPLORING)
        self.assertEqual(saved.handoff_result, "delivery_pending")
        self.handoff.fail = False
        self.assertEqual(self.run_scout().state, State.COMPLETED)
        self.assertEqual(len(self.handoff.deliveries), 1)

    def test_lost_ack_is_recovered_by_idempotent_handoff(self):
        self.handoff.lose_ack = True
        with self.assertRaises(OSError):
            self.run_scout()
        self.assertEqual(len(self.handoff.deliveries), 1)
        result = self.run_scout()
        self.assertEqual(result.state, State.COMPLETED)
        self.assertEqual(len(self.handoff.deliveries), 1)

    def test_custody_failures_before_and_after_write_are_retryable(self):
        self.custody.fail_before_store = True
        with self.assertRaises(OSError):
            self.run_scout()
        self.assertFalse(self.custody.objects)
        self.custody.fail_before_store = False
        self.custody.fail_after_store = True
        with self.assertRaises(OSError):
            self.run_scout()
        self.assertEqual(len(self.custody.objects), 1)
        self.custody.fail_after_store = False
        self.assertEqual(self.run_scout().state, State.COMPLETED)
        self.assertEqual(len(self.custody.objects), 1)
        intent = next(iter(self.repo.custody_intents.values()))
        self.assertEqual(intent[2], "verified")

    def test_application_custody_reconciles_unknown_write_by_identity(self):
        self.custody.fail_after_store = True
        with self.assertRaises(OSError):
            self.run_scout()
        artifact_id = "artifact-" + self.request["request_id"]
        self.custody.fail_after_store = False
        self.assertEqual(self.app.reconcile_custody_intent(artifact_id), "confirmed")
        self.assertEqual(self.repo.custody_intents[artifact_id][2], "verified")

    def test_concurrent_compare_and_swap_has_single_winner(self):
        self.run_scout()
        current = next(iter(self.repo.sessions.values()))
        contenders = [copy.deepcopy(current), copy.deepcopy(current)]
        barrier = threading.Barrier(2)
        outcomes = []

        def save(candidate):
            barrier.wait()
            try:
                self.repo.save(candidate, candidate.version)
                outcomes.append("saved")
            except ConflictError:
                outcomes.append("conflict")

        workers = [threading.Thread(target=save, args=(item,)) for item in contenders]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        self.assertEqual(
            next(iter(self.repo.sessions.values())).version, current.version + 1
        )

    def test_concurrency_and_immutable_terminal(self):
        self.run_scout()
        session = next(iter(self.repo.sessions.values()))
        stale = copy.deepcopy(session)
        self.repo.save(session, session.version)
        with self.assertRaises(ConflictError):
            self.repo.save(stale, stale.version)
        with self.assertRaises(TransitionError):
            session.cancel()
        with self.assertRaises(TransitionError):
            session.handoff_result = "approved"
        with self.assertRaises(IntegrityError):
            session.source = SourceRef("other", "revision")

    def test_pause_scope_budget_and_policy(self):
        session = ScoutSession(
            "s", self.source, ScoutObjective("objective"), self.scope, "analyst-1"
        )
        narrow = replace(
            self.scope,
            path_prefixes=("/catalog/detail/",),
            max_requests=2,
            max_duration_seconds=4,
        )
        session.authorize(narrow, self.decision, self.now)
        self.assertEqual(session.authorization.scope, narrow)
        session.start(self.decision, self.now)
        from scout.domain import SourceObservation

        session.observe(
            SourceObservation("o", "s", self.source, "observation", "a"),
            self.decision,
            self.now,
            2,
        )
        session.pause()
        session.authorize(self.scope, self.decision, self.now)
        self.assertEqual(
            (session.remaining_requests, session.remaining_seconds), (1, 2)
        )
        self.assertEqual(session.authorization.scope, narrow)
        session.start(self.decision, self.now)
        with self.assertRaises(AuthorizationError):
            session.observe(
                SourceObservation("o2", "s", self.source, "x", "a"),
                replace(self.decision, revoked=True),
                self.now,
            )
        with self.assertRaises(IntegrityError):
            session.complete(self.decision, self.now)
        session.cancel()
        with self.assertRaises(TransitionError):
            session.start(self.decision, self.now)

    def test_evidence_and_cross_references(self):
        self.handoff.fail = True
        with self.assertRaises(OSError):
            self.run_scout()
        session = copy.deepcopy(next(iter(self.repo.sessions.values())))
        manifest = session.manifests[0]
        with self.assertRaises(FrozenInstanceError):
            manifest.sealed_at = "changed"
        with self.assertRaises(IntegrityError):
            session.seal(
                replace(manifest, manifest_id="other"), False, self.decision, self.now
            )
        with self.assertRaises(IntegrityError):
            session.seal(
                replace(manifest, manifest_id="other", source=SourceRef("x", "y")),
                True,
                self.decision,
                self.now,
            )
        finding = session.findings[0]
        with self.assertRaises(IntegrityError):
            session.add_finding(
                replace(finding, finding_id="new", manifest_ids=()),
                self.decision,
                self.now,
            )
        with self.assertRaises(IntegrityError):
            session.add_finding(
                replace(finding, finding_id="new", epistemic_status="verified"),
                self.decision,
                self.now,
            )
        session.proposal = None
        with self.assertRaises(IntegrityError):
            session.propose(
                replace(
                    next(
                        iter(self.handoff.deliveries.values()),
                        self.repo.sessions[session.session_id].proposal,
                    ),
                    finding_ids=("foreign",),
                ),
                self.decision,
                self.now,
            )
        self.custody.objects[manifest.artifacts[0].artifact_id] = (
            manifest.artifacts[0],
            b"tampered",
        )
        self.assertFalse(self.custody.verify(manifest.artifacts[0]))

    def test_retry_rechecks_policy_and_evidence(self):
        self.handoff.fail = True
        with self.assertRaises(OSError):
            self.run_scout()
        self.handoff.fail = False
        self.policy.decision = replace(self.decision, revoked=True)
        with self.assertRaises(AuthorizationError):
            self.run_scout()
        self.policy.decision = self.decision
        artifact_id = next(iter(self.custody.objects))
        artifact, _ = self.custody.objects[artifact_id]
        self.custody.objects[artifact_id] = (artifact, b"corrupted")
        with self.assertRaises(IntegrityError):
            self.run_scout()
        self.assertEqual(next(iter(self.repo.sessions.values())).state, State.EXPLORING)
        self.assertFalse(self.handoff.deliveries)

    def test_offline_path_never_opens_socket(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self.run_scout().state, State.COMPLETED)

    def test_domain_import_boundaries(self):
        tree = ast.parse((ROOT / "apps/scout/scout/domain.py").read_text())
        imports = [
            n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        ]
        imports += [
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        ]
        self.assertEqual(set(imports) - {"dataclasses", "datetime", "enum"}, set())


if __name__ == "__main__":
    unittest.main()
