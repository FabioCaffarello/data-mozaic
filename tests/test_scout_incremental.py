"""Incremental application use cases reuse the BC14 aggregate and repository."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scout.adapters.fixtures import FixtureExplorer
from scout.adapters.local_custody import LocalFileEvidenceCustody
from scout.application import ScoutApplication
from scout.domain import (
    ArtifactRef,
    AuthorizationError,
    ConflictError,
    EvidenceManifest,
    IntegrityError,
    PolicyDecisionRef,
    ScoutScope,
    SourceObservation,
    SourceRef,
)
from scout.fakes import (
    FakeEvidenceCustody,
    FakePolicy,
    FakeSessionRepository,
    FakeSourceLookup,
)


class ScoutIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, tzinfo=UTC)
        self.source = SourceRef("catalog", "revision-3")
        self.scope = ScoutScope(("https://catalog.test",), ("/",), ("GET",), 4, 60)
        self.decision = PolicyDecisionRef(
            "approval-1",
            "operator",
            self.source,
            True,
            self.now + timedelta(hours=1),
            self.scope,
        )
        self.repository = FakeSessionRepository()
        self.app = ScoutApplication(
            FakeSourceLookup({self.source: self.scope}),
            FakePolicy(self.decision),
            self.repository,
            FakeEvidenceCustody(),
        )

    def test_start_replay_status_and_reauthorization(self):
        session = self.app.start_incremental(
            source=self.source,
            objective="inspect catalog",
            principal="operator",
            command_id="command-1",
            now=self.now,
        )
        replay = self.app.start_incremental(
            source=self.source,
            objective="inspect catalog",
            principal="operator",
            command_id="command-1",
            now=self.now,
        )
        self.assertEqual(replay.session_id, session.session_id)
        self.assertEqual(replay.version, 0)
        self.assertEqual(
            self.app.status_incremental(session.session_id, "operator", self.now)[
                "state"
            ],
            "exploring",
        )
        with self.assertRaises(IntegrityError):
            self.app.status_incremental(session.session_id, "other", self.now)

    def test_finding_requires_session_manifest_and_expected_version(self):
        session = self.app.start_incremental(
            source=self.source,
            objective="inspect catalog",
            principal="operator",
            command_id="command-2",
            now=self.now,
        )
        artifact = ArtifactRef("artifact-1", session.session_id, self.source, "a" * 64)
        observation = SourceObservation(
            "observation-1",
            session.session_id,
            self.source,
            "captured page",
            artifact.artifact_id,
        )
        session.observe(observation, self.decision, self.now)
        manifest = EvidenceManifest(
            "manifest-1",
            session.session_id,
            self.source,
            self.now.isoformat().replace("+00:00", "Z"),
            (artifact,),
        )
        session.seal(manifest, True, self.decision, self.now)
        self.repository.save(session, 0)
        updated = self.app.record_finding_incremental(
            session_id=session.session_id,
            principal="operator",
            command_id="finding-1",
            idempotency_key="finding-key-1",
            fingerprint="finding-fingerprint-1",
            expected_version=1,
            statement="Catalog page captured",
            epistemic_status="observation",
            confidence=1.0,
            manifest_ids=(manifest.manifest_id,),
            verification_note="Directly visible in captured evidence",
            now=self.now,
        )
        self.assertEqual(updated.version, 2)
        self.assertEqual(len(updated.findings), 1)
        with self.assertRaises(ConflictError):
            self.app.record_finding_incremental(
                session_id=session.session_id,
                principal="operator",
                command_id="finding-2",
                idempotency_key="finding-key-2",
                fingerprint="finding-fingerprint-2",
                expected_version=1,
                statement="stale update",
                epistemic_status="observation",
                confidence=1.0,
                manifest_ids=(manifest.manifest_id,),
                verification_note="stale",
                now=self.now,
            )

    def test_independently_verified_is_not_accepted_without_domain_support(self):
        session = self.app.start_incremental(
            source=self.source,
            objective="inspect catalog",
            principal="operator",
            command_id="command-3",
            now=self.now,
        )
        with self.assertRaises(IntegrityError):
            self.app.record_finding_incremental(
                session_id=session.session_id,
                principal="operator",
                command_id="finding-3",
                idempotency_key="finding-key-3",
                fingerprint="finding-fingerprint-3",
                expected_version=0,
                statement="unverified claim",
                epistemic_status="independently_verified",
                confidence=1.0,
                manifest_ids=("missing",),
                verification_note="claimed",
                now=self.now,
            )

    def test_fixture_receipt_is_idempotent_concurrent_and_digest_verified(self):
        source = SourceRef("catalog-test", "revision-1")
        scope = ScoutScope(("https://catalog.test",), ("/",), ("GET",), 4, 60)
        decision = PolicyDecisionRef(
            "approval-fixture",
            "operator",
            source,
            True,
            self.now + timedelta(hours=1),
            scope,
        )
        repository = FakeSessionRepository()
        with tempfile.TemporaryDirectory() as temporary:
            custody = LocalFileEvidenceCustody(Path(temporary).resolve())
            app = ScoutApplication(
                FakeSourceLookup({source: scope}),
                FakePolicy(decision),
                repository,
                custody,
                FixtureExplorer(),
            )

            def start():
                return app.start_incremental(
                    source=source,
                    objective="test registered fixtures",
                    principal="operator",
                    command_id="session-fixture",
                    now=self.now,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                started = list(pool.map(lambda _: start(), range(2)))
            session = started[0]
            self.assertEqual(started[0].session_id, started[1].session_id)

            def explore():
                return app.explore_fixture_incremental(
                    session_id=session.session_id,
                    principal="operator",
                    command_id="fixture-command",
                    idempotency_key="fixture-key",
                    fingerprint="same-canonical-command",
                    expected_version=0,
                    fixture_id="catalog-home",
                    now=self.now,
                )

            with (
                self.assertLogs("scout.application") as captured,
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                outcomes = list(pool.map(lambda _: explore(), range(2)))
            self.assertNotIn("disclose local files", " ".join(captured.output))
            saved = repository.get_session(session.session_id)
            self.assertEqual(1, saved.version)
            self.assertEqual(1, len(saved.observations))
            self.assertEqual(1, len(saved.manifests))
            self.assertEqual(3, saved.remaining_requests)
            self.assertEqual(outcomes[0].session_id, outcomes[1].session_id)

            artifact = saved.manifests[0].artifacts[0]
            body_path = Path(temporary) / "objects" / artifact.sha256
            body_path.write_bytes(body_path.read_bytes() + b" tampered")
            self.assertFalse(custody.verify(artifact))

    def test_fixture_scope_widening_and_wrong_revision_have_no_mutation(self):
        source = SourceRef("catalog-test", "revision-1")
        narrow = ScoutScope(("https://catalog.test",), ("/private",), ("GET",), 4, 60)
        decision = PolicyDecisionRef(
            "approval-narrow",
            "operator",
            source,
            True,
            self.now + timedelta(hours=1),
            narrow,
        )
        repository = FakeSessionRepository()
        app = ScoutApplication(
            FakeSourceLookup({source: narrow}),
            FakePolicy(decision),
            repository,
            FakeEvidenceCustody(),
            FixtureExplorer(),
        )
        session = app.start_incremental(
            source=source,
            objective="fixture scope denial",
            principal="operator",
            command_id="session-narrow",
            now=self.now,
        )
        commands_before = dict(repository.commands)
        with self.assertRaises(AuthorizationError):
            app.explore_fixture_incremental(
                session_id=session.session_id,
                principal="operator",
                command_id="fixture-outside",
                idempotency_key="fixture-outside-key",
                fingerprint="outside",
                expected_version=0,
                fixture_id="catalog-home",
                now=self.now,
            )
        self.assertEqual(0, repository.get_session(session.session_id).version)
        self.assertEqual(
            0, len(repository.get_session(session.session_id).observations)
        )
        self.assertEqual(commands_before, repository.commands)
        with self.assertRaises(IntegrityError):
            app.start_incremental(
                source=SourceRef("catalog-test", "revision-2"),
                objective="wrong source revision",
                principal="operator",
                command_id="wrong-revision-session",
                now=self.now,
            )

    def test_start_denials_do_not_persist_sessions_or_receipts(self):
        for policy_decision, principal, source, error_type in (
            (
                self.decision,
                "different-principal",
                self.source,
                AuthorizationError,
            ),
            (
                replace(self.decision, revoked=True),
                "operator",
                self.source,
                AuthorizationError,
            ),
            (
                replace(self.decision, expires_at=self.now - timedelta(seconds=1)),
                "operator",
                self.source,
                AuthorizationError,
            ),
            (
                self.decision,
                "operator",
                SourceRef("catalog", "other-revision"),
                IntegrityError,
            ),
        ):
            repository = FakeSessionRepository()
            policy = FakePolicy(policy_decision)
            app = ScoutApplication(
                FakeSourceLookup({self.source: self.scope}),
                policy,
                repository,
                FakeEvidenceCustody(),
            )
            with (
                self.subTest(principal=principal, source=source),
                self.assertRaises(error_type),
            ):
                app.start_incremental(
                    source=source,
                    objective="must be denied",
                    principal=principal,
                    command_id="denied-start",
                    now=self.now,
                )
            self.assertEqual({}, repository.sessions)
            self.assertEqual({}, repository.commands)

    def test_retry_reconciles_custody_write_with_lost_ack(self):
        source = SourceRef("catalog-test", "revision-1")
        scope = ScoutScope(("https://catalog.test",), ("/",), ("GET",), 4, 60)
        decision = PolicyDecisionRef(
            "approval-reconcile",
            "operator",
            source,
            True,
            self.now + timedelta(hours=1),
            scope,
        )
        repository = FakeSessionRepository()
        custody = FakeEvidenceCustody(kind="http_response")
        custody.fail_after_store = True
        app = ScoutApplication(
            FakeSourceLookup({source: scope}),
            FakePolicy(decision),
            repository,
            custody,
            FixtureExplorer(),
        )
        session = app.start_incremental(
            source=source,
            objective="reconcile fixture custody",
            principal="operator",
            command_id="session-reconcile",
            now=self.now,
        )
        command = {
            "session_id": session.session_id,
            "principal": "operator",
            "command_id": "fixture-lost-ack",
            "idempotency_key": "fixture-lost-ack-key",
            "fingerprint": "fixture-lost-ack-fingerprint",
            "expected_version": 0,
            "fixture_id": "catalog-home",
            "now": self.now,
        }
        with self.assertRaises(OSError):
            app.explore_fixture_incremental(**command)
        custody.fail_after_store = False
        result = app.explore_fixture_incremental(**command)
        self.assertEqual(1, result.version)
        self.assertEqual(1, len(result.observations))
        self.assertEqual(
            "available",
            result.manifests[0].artifacts[0].custody_status,
        )

    def test_exhausted_request_budget_denies_before_new_command_or_custody(self):
        source = SourceRef("catalog-test", "revision-1")
        scope = ScoutScope(("https://catalog.test",), ("/",), ("GET",), 1, 60)
        decision = PolicyDecisionRef(
            "approval-one-request",
            "operator",
            source,
            True,
            self.now + timedelta(hours=1),
            scope,
        )
        repository = FakeSessionRepository()
        custody = FakeEvidenceCustody(kind="http_response")
        app = ScoutApplication(
            FakeSourceLookup({source: scope}),
            FakePolicy(decision),
            repository,
            custody,
            FixtureExplorer(),
        )
        session = app.start_incremental(
            source=source,
            objective="consume the single fixture request",
            principal="operator",
            command_id="session-one-request",
            now=self.now,
        )
        first = app.explore_fixture_incremental(
            session_id=session.session_id,
            principal="operator",
            command_id="first-fixture-command",
            idempotency_key="first-fixture-key",
            fingerprint="first-fixture-fingerprint",
            expected_version=0,
            fixture_id="catalog-home",
            now=self.now,
        )
        command_count = len(repository.commands)
        artifact_count = len(custody.objects)
        with self.assertRaises(IntegrityError):
            app.explore_fixture_incremental(
                session_id=session.session_id,
                principal="operator",
                command_id="second-fixture-command",
                idempotency_key="second-fixture-key",
                fingerprint="second-fixture-fingerprint",
                expected_version=first.version,
                fixture_id="catalog-page-2",
                now=self.now,
            )
        self.assertEqual(command_count, len(repository.commands))
        self.assertEqual(artifact_count, len(custody.objects))


if __name__ == "__main__":
    unittest.main()
