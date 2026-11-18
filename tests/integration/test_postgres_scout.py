"""PostgreSQL durability checks; run with SCOUT_TEST_DATABASE_URL set."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from psycopg import Error, connect
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "scout"))

from scout.adapters.fixtures import FixtureExplorer
from scout.adapters.local_custody import LocalFileEvidenceCustody
from scout.adapters.postgres.repository import (
    PostgresNavigationBudgetLedger,
    PostgresScoutSessionRepository,
)
from scout.application import ScoutApplication
from scout.domain import (
    ArtifactRef,
    AuthorizationError,
    ConflictError,
    IntegrityError,
    PlaybookProposal,
    ScoutObjective,
    ScoutScope,
    ScoutSession,
    SourceRef,
)
from scout.fakes import FakePolicy, FakeSourceLookup
from scout.outbox_relay import OutboxRelay
from scout.tools.policy import EffectiveScope, OriginGrant

DSN = os.environ.get("SCOUT_TEST_DATABASE_URL")


def build_test_receiver(dsn):
    """Durable BC15 receiver double used by the independent worker CLI test."""
    with connect(dsn) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_g1_receiver(proposal_id text PRIMARY KEY, receipt_id text NOT NULL)"
        )

    class Receiver:
        def deliver_message(self, message_id, payload):
            with connect(dsn) as conn:
                conn.execute(
                    "INSERT INTO test_g1_receiver VALUES (%s,%s) ON CONFLICT (proposal_id) DO NOTHING",
                    (payload["proposal_id"], "receipt-" + payload["proposal_id"]),
                )
            return {"status": "received", "message_id": message_id}

    return Receiver()


@unittest.skipUnless(DSN, "SCOUT_TEST_DATABASE_URL is not configured")
class PostgresScoutRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parsed_host = urlparse(DSN).hostname
        if parsed_host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("integration tests require a loopback-only test DSN")
        cls.test_database = "scout_test_" + uuid4().hex
        with connect(DSN, autocommit=True) as conn:
            conn.execute(
                SQL("CREATE DATABASE {}").format(Identifier(cls.test_database))
            )
        cls.test_dsn = make_conninfo(DSN, dbname=cls.test_database)
        cls.repo = PostgresScoutSessionRepository(cls.test_dsn)
        cls.repo.migrate()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "test_database"):
            with connect(DSN, autocommit=True) as conn:
                conn.execute(
                    SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        Identifier(cls.test_database)
                    )
                )

    def make_session(self):
        session_id = "test-" + str(uuid4())
        return ScoutSession(
            session_id,
            SourceRef("source-test", "rev-1"),
            ScoutObjective("test"),
            ScoutScope(("https://example.org",), ("/",), ("GET",), 2, 10),
            "tester",
        )

    def setUp(self):
        with connect(self.test_dsn) as conn:
            conn.execute(
                "TRUNCATE scout_reconciliation_events,scout_orphan_reviews,scout_custody_intents,scout_outbox,scout_command_receipts,scout_sessions CASCADE"
            )

    def test_receipt_survives_new_repository_instance(self):
        session = self.make_session()
        key, fingerprint = str(uuid4()), "a" * 64
        self.repo.create(session, key, fingerprint)
        restarted = PostgresScoutSessionRepository(self.test_dsn)
        restored = restarted.find_intent("tester", key, fingerprint)
        self.assertEqual(restored, session)
        with self.assertRaises(ConflictError):
            restarted.find_intent("tester", key, "b" * 64)

    def test_incremental_command_intent_cas_and_result_survive_restart(self):
        session = self.make_session()
        self.repo.create(
            session,
            "incremental-start",
            "d" * 64,
            command_kind="StartScout",
            receipt_status="completed",
        )
        command_key = "fixture-command-" + uuid4().hex
        command_id = "fixture-id-" + uuid4().hex
        fingerprint = "e" * 64
        self.repo.begin_command(
            "tester",
            session.session_id,
            command_key,
            command_id,
            "ExploreFixture",
            fingerprint,
            {"fixture_id": "catalog-home", "expected_version": 0},
        )
        restarted = PostgresScoutSessionRepository(self.test_dsn)
        self.assertTrue(restarted.has_incomplete_commands(session.session_id))
        loaded = restarted.get_session(session.session_id)
        restarted.complete_command(loaded, 0, "tester", command_key, fingerprint)
        another_restart = PostgresScoutSessionRepository(self.test_dsn)
        self.assertFalse(another_restart.has_incomplete_commands(session.session_id))
        result = another_restart.get_command_result("tester", command_key, fingerprint)
        self.assertEqual(1, result.version)
        self.assertEqual(
            "completed",
            another_restart.find_command_header("tester", command_key, fingerprint)[
                "status"
            ],
        )
        with self.assertRaises(ConflictError):
            another_restart.get_command_result("tester", command_key, "f" * 64)

    def test_custody_intent_can_be_reconciled_after_repository_restart(self):
        session = self.make_session()
        self.repo.create(session, "custody-start", "a" * 64)
        artifact_id = "artifact-" + "b" * 64
        self.repo.begin_custody_intent(artifact_id, session.session_id, "c" * 64, 42)
        restarted = PostgresScoutSessionRepository(self.test_dsn)
        intent = restarted.get_custody_intent(artifact_id)
        self.assertEqual("pending", intent["status"])
        self.assertEqual(42, intent["expected_size_bytes"])
        restarted.mark_custody_unknown(artifact_id)
        unresolved = PostgresScoutSessionRepository(self.test_dsn).get_custody_intent(
            artifact_id
        )
        self.assertEqual("unknown", unresolved["status"])

    def test_incremental_fixture_to_transcript_recovers_lost_custody_ack(self):
        source = SourceRef("catalog-test", "revision-1")
        scope = ScoutScope(("https://catalog.test",), ("/",), ("GET",), 4, 60)
        now = datetime.now(UTC)
        from scout.domain import PolicyDecisionRef

        decision = PolicyDecisionRef(
            "approval-integration",
            "tester",
            source,
            True,
            now + timedelta(minutes=5),
            scope,
        )
        with tempfile.TemporaryDirectory(prefix="scout-postgres-custody-") as root:
            custody_root = Path(root).resolve()
            local_custody = LocalFileEvidenceCustody(custody_root)

            class LostAcknowledgement:
                def lookup(self, artifact_id):
                    return local_custody.lookup(artifact_id)

                def store(self, session_id, source, content, artifact_id):
                    local_custody.store(session_id, source, content, artifact_id)
                    raise OSError("lost acknowledgement after durable local write")

                def verify(self, artifact):
                    return local_custody.verify(artifact)

            def application(repository, custody):
                return ScoutApplication(
                    FakeSourceLookup({source: scope}),
                    FakePolicy(decision),
                    repository,
                    custody,
                    FixtureExplorer(),
                )

            first_app = application(self.repo, LostAcknowledgement())
            session = first_app.start_incremental(
                source=source,
                objective="recover a durable fixture capture",
                principal="tester",
                command_id="session-integration-" + uuid4().hex,
                now=now,
            )
            fixture_command = {
                "session_id": session.session_id,
                "principal": "tester",
                "command_id": "capture-integration-" + uuid4().hex,
                "idempotency_key": "capture-key-" + uuid4().hex,
                "fingerprint": "a" * 64,
                "expected_version": 0,
                "fixture_id": "catalog-home",
                "now": now,
            }
            with self.assertRaises(OSError):
                first_app.explore_fixture_incremental(**fixture_command)

            restarted_repository = PostgresScoutSessionRepository(self.test_dsn)
            restarted_app = application(
                restarted_repository, LocalFileEvidenceCustody(custody_root)
            )
            captured = restarted_app.explore_fixture_incremental(**fixture_command)
            self.assertEqual(1, captured.version)
            manifest_id = captured.manifests[0].manifest_id
            finding = restarted_app.record_finding_incremental(
                session_id=session.session_id,
                principal="tester",
                command_id="finding-integration-" + uuid4().hex,
                idempotency_key="finding-key-" + uuid4().hex,
                fingerprint="b" * 64,
                expected_version=1,
                statement="The registered fixture page has a captured link.",
                epistemic_status="observation",
                confidence=1,
                manifest_ids=(manifest_id,),
                verification_note="Observed in the verified fixture bytes.",
                now=now,
            )
            proposed = restarted_app.submit_proposal_incremental(
                session_id=session.session_id,
                principal="tester",
                expected_version=2,
                command_id="proposal-integration-" + uuid4().hex,
                idempotency_key="proposal-key-" + uuid4().hex,
                fingerprint="c" * 64,
                summary="Draft a plan for the registered catalog fixture.",
                finding_ids=(finding.findings[0].finding_id,),
                manifest_ids=(manifest_id,),
                assumptions=(),
                risks=("Captured fixture is not a live source.",),
                required_capabilities=("http.fetch",),
                now=now,
            )
            self.assertEqual(3, proposed.version)
            exported = restarted_app.export_transcript(
                session.session_id, "tester", now
            )
            again = application(
                PostgresScoutSessionRepository(self.test_dsn),
                LocalFileEvidenceCustody(custody_root),
            ).export_transcript(session.session_id, "tester", now)
            self.assertEqual(exported, again)
            self.assertTrue(
                restarted_repository.handoff_status(
                    "handoff:" + proposed.proposal.proposal_id
                )
            )

    def test_migrations_are_checksummed_and_idempotent(self):
        self.repo.migrate()
        with connect(self.test_dsn) as conn:
            before = conn.execute(
                "SELECT migration_name,checksum FROM scout_schema_migrations ORDER BY migration_name"
            ).fetchall()
            marker = "migration-data-" + uuid4().hex
            conn.execute(
                "CREATE TABLE IF NOT EXISTS test_migration_marker(value text PRIMARY KEY)"
            )
            conn.execute("INSERT INTO test_migration_marker VALUES (%s)", (marker,))
        self.repo.migrate()
        with connect(self.test_dsn) as conn:
            after = conn.execute(
                "SELECT migration_name,checksum FROM scout_schema_migrations ORDER BY migration_name"
            ).fetchall()
            kept = conn.execute(
                "SELECT 1 FROM test_migration_marker WHERE value=%s", (marker,)
            ).fetchone()
        self.assertEqual(before, after)
        self.assertIsNotNone(kept)
        with connect(self.test_dsn) as conn:
            conn.execute(
                "UPDATE scout_schema_migrations SET checksum='tampered' WHERE migration_name='001-durable-state.sql'"
            )
        with self.assertRaises(IntegrityError):
            self.repo.migrate()
        with connect(self.test_dsn) as conn:
            conn.execute(
                "UPDATE scout_schema_migrations SET checksum=%s WHERE migration_name='001-durable-state.sql'",
                (next(row[1] for row in before if row[0] == "001-durable-state.sql"),),
            )

    def test_navigation_budget_is_atomic_durable_and_cannot_widen(self):
        session = self.make_session()
        self.repo.create(session, "budget-key", "c" * 64)
        source = session.source
        narrow = EffectiveScope(
            "tester",
            source,
            (OriginGrant("https://example.org", ("/docs",)),),
            datetime.now(UTC) + timedelta(minutes=5),
            2,
            30,
            4096,
        )
        ledger = PostgresNavigationBudgetLedger(self.test_dsn)
        with self.assertRaises(AuthorizationError):
            ledger.consume(
                session.session_id,
                "wrong-principal",
                0,
                EffectiveScope(
                    "impostor",
                    source,
                    narrow.grants,
                    narrow.expires_at,
                    narrow.max_requests,
                    narrow.max_duration_seconds,
                    narrow.max_response_bytes,
                ),
            )
        first = ledger.consume(session.session_id, "call-1", 0, narrow)
        self.assertEqual(first.consumed_requests, 1)

        widened = EffectiveScope(
            "tester",
            source,
            (OriginGrant("https://example.org", ("/",)),),
            narrow.expires_at + timedelta(hours=1),
            9,
            90,
            8192,
        )
        restarted = PostgresNavigationBudgetLedger(self.test_dsn)
        second = restarted.consume(session.session_id, "call-2", 0, widened)
        self.assertEqual(second.consumed_requests, 2)
        self.assertEqual(second.scope.grants[0].path_prefixes, ("/docs",))
        self.assertEqual(second.scope.max_requests, 2)
        with self.assertRaises(AuthorizationError):
            restarted.consume(session.session_id, "call-3", 0, widened)
        with self.assertRaises(AuthorizationError):
            restarted.consume(session.session_id, "call-1", 0, widened)

    def test_child_process_crash_before_and_after_commit(self):
        with connect(self.test_dsn) as conn:
            conn.execute("CREATE TABLE test_crash_marker(value text PRIMARY KEY)")
        script = """
import os, psycopg, sys
conn = psycopg.connect(os.environ['SCOUT_CHILD_DSN'])
conn.execute('BEGIN')
conn.execute('INSERT INTO test_crash_marker VALUES (%s)', (sys.argv[1],))
if sys.argv[2] == 'after':
    conn.commit()
os._exit(41)
"""
        env = dict(os.environ, SCOUT_CHILD_DSN=self.test_dsn)
        before, after = "before-" + uuid4().hex, "after-" + uuid4().hex
        for marker, boundary in ((before, "before"), (after, "after")):
            child = subprocess.run(
                [sys.executable, "-c", script, marker, boundary],
                env=env,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(child.returncode, 41)
        with connect(self.test_dsn) as conn:
            rows = conn.execute(
                "SELECT value FROM test_crash_marker WHERE value = ANY(%s)",
                ([before, after],),
            ).fetchall()
        self.assertEqual(rows, [(after,)])

    def test_two_connections_cas_only_one_wins(self):
        session = self.make_session()
        key = str(uuid4())
        self.repo.create(session, key, "c" * 64)
        contenders = [self.repo.find_intent("tester", key, "c" * 64) for _ in range(2)]
        barrier, outcomes = threading.Barrier(2), []

        def update(candidate):
            barrier.wait()
            try:
                self.repo.save(candidate, 0)
                outcomes.append("saved")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=update, args=(item,)) for item in contenders]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        self.assertCountEqual(outcomes, ["saved", "conflict"])

    def test_outbox_intent_survives_repository_restart(self):
        session = self.make_session()
        session.proposal = PlaybookProposal(
            "proposal-" + session.session_id,
            session.session_id,
            session.source,
            "test proposal",
            ("finding-1",),
            ("manifest-1",),
            (),
            (),
            ("read",),
        )
        key = str(uuid4())
        self.repo.create(session, key, "e" * 64)
        self.repo.save(session, 0)
        restarted = PostgresScoutSessionRepository(self.test_dsn)
        pending = restarted.pending_handoffs()
        event = next(
            item
            for item in pending
            if item["proposal_id"] == session.proposal.proposal_id
        )
        self.assertEqual(event["publication_state"], "pending")
        claim = restarted.claim_handoff("test-relay", 30, datetime.now(UTC))
        self.assertEqual(claim["event_id"], event["event_id"])
        self.assertTrue(
            restarted.finish_handoff(
                event["event_id"],
                "test-relay",
                claim["fencing_token"],
                status="sent",
                error_class=None,
                result="received_by_bc15",
                next_attempt_delay_seconds=0,
                receipt={"receipt_id": "r-1"},
            )
        )
        with connect(self.test_dsn) as conn:
            row = conn.execute(
                "SELECT publication_state,business_ack FROM scout_outbox WHERE event_id=%s",
                (event["event_id"],),
            ).fetchone()
        self.assertEqual(row[0], "sent")
        self.assertEqual(row[1], {"receipt_id": "r-1"})

    def test_outbox_concurrent_process_claim_and_stale_fence(self):
        session = self.make_session()
        session.proposal = PlaybookProposal(
            "proposal-" + session.session_id,
            session.session_id,
            session.source,
            "test proposal",
            ("finding-1",),
            ("manifest-1",),
            (),
            (),
            ("read",),
        )
        self.repo.create(session, str(uuid4()), "f" * 64)
        self.repo.save(session, 0)
        event_id = "handoff:" + session.proposal.proposal_id
        claim_script = r"""
import sys
from datetime import UTC, datetime
from scout.adapters.postgres.repository import PostgresScoutSessionRepository
claim = PostgresScoutSessionRepository(sys.argv[1]).claim_handoff(sys.argv[2], 60, datetime.now(UTC))
print(claim['event_id'] if claim else 'idle')
"""
        env = dict(os.environ, PYTHONPATH=str(ROOT / "apps" / "scout"))
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", claim_script, self.test_dsn, f"worker-{i}"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(2)
        ]
        outcomes = [worker.communicate(timeout=10) for worker in workers]
        self.assertEqual([out[0].strip() for out in outcomes].count(event_id), 1)
        self.assertEqual([out[0].strip() for out in outcomes].count("idle"), 1)

        with connect(self.test_dsn) as conn:
            conn.execute(
                "UPDATE scout_outbox SET publication_state='pending',lease_owner=NULL,lease_deadline=NULL,next_attempt_at=now() WHERE event_id=%s",
                (event_id,),
            )
        old = self.repo.claim_handoff("old-worker", 60, datetime.now(UTC))
        with connect(self.test_dsn) as conn:
            conn.execute(
                "UPDATE scout_outbox SET lease_deadline=now()-interval '1 second' WHERE event_id=%s",
                (event_id,),
            )
        fresh = self.repo.claim_handoff("new-worker", 60, datetime.now(UTC))
        self.assertGreater(fresh["fencing_token"], old["fencing_token"])
        self.assertFalse(
            self.repo.finish_handoff(
                event_id,
                "old-worker",
                old["fencing_token"],
                status="sent",
                error_class=None,
                result="stale",
                next_attempt_delay_seconds=0,
                receipt={"status": "received"},
            )
        )
        self.assertTrue(
            self.repo.finish_handoff(
                event_id,
                "new-worker",
                fresh["fencing_token"],
                status="sent",
                error_class=None,
                result="received_by_bc15",
                next_attempt_delay_seconds=0,
                receipt={"status": "received"},
            )
        )

    def test_relay_lost_ack_backoff_receiver_dedup_and_dead_letter(self):
        session = self.make_session()
        session.proposal = PlaybookProposal(
            "proposal-" + session.session_id,
            session.session_id,
            session.source,
            "test proposal",
            ("finding-1",),
            ("manifest-1",),
            (),
            (),
            ("read",),
        )
        self.repo.create(session, str(uuid4()), "a" * 64)
        self.repo.save(session, 0)
        event_id = "handoff:" + session.proposal.proposal_id
        with connect(self.test_dsn) as conn:
            conn.execute(
                "CREATE TABLE test_relay_receiver(message_id text PRIMARY KEY, receipt_id text NOT NULL)"
            )

        class Receiver:
            lose_ack = True

            def deliver_message(inner, message_id, payload):
                with connect(self.test_dsn) as conn:
                    conn.execute(
                        "INSERT INTO test_relay_receiver VALUES (%s,%s) ON CONFLICT (message_id) DO NOTHING",
                        (message_id, "receipt-" + message_id),
                    )
                if inner.lose_ack:
                    inner.lose_ack = False
                    raise OSError("lost acknowledgement")
                return {"message_id": message_id, "status": "received"}

        receiver = Receiver()
        relay = OutboxRelay(self.repo, receiver, owner="relay-test", jitter=lambda: 0)
        self.assertEqual(relay.run_once(datetime.now(UTC)), "retry_scheduled")
        with connect(self.test_dsn) as conn:
            state, attempt_count, next_attempt = conn.execute(
                "SELECT publication_state,attempt_count,next_attempt_at FROM scout_outbox WHERE event_id=%s",
                (event_id,),
            ).fetchone()
            conn.execute(
                "UPDATE scout_outbox SET next_attempt_at=now()-interval '1 second' WHERE event_id=%s",
                (event_id,),
            )
        self.assertEqual((state, attempt_count), ("retry_scheduled", 1))
        self.assertGreater(next_attempt, datetime.now(UTC))
        self.assertEqual(relay.run_once(datetime.now(UTC)), "delivered")
        with connect(self.test_dsn) as conn:
            count, state, ack = conn.execute(
                "SELECT (SELECT count(*) FROM test_relay_receiver WHERE message_id=%s),publication_state,business_ack FROM scout_outbox WHERE event_id=%s",
                (event_id, event_id),
            ).fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(state, "sent")
        self.assertEqual(ack["message_id"], event_id)

        dead_session = self.make_session()
        dead_session.proposal = PlaybookProposal(
            "proposal-" + dead_session.session_id,
            dead_session.session_id,
            dead_session.source,
            "dead letter proposal",
            ("finding-1",),
            ("manifest-1",),
            (),
            (),
            ("read",),
        )
        self.repo.create(dead_session, str(uuid4()), "b" * 64)
        self.repo.save(dead_session, 0)
        dead_relay = OutboxRelay(
            self.repo,
            type(
                "Rejecting",
                (),
                {
                    "deliver_message": lambda *_: (_ for _ in ()).throw(
                        ValueError("invalid payload")
                    )
                },
            )(),
            owner="dead-letter-test",
            max_attempts=3,
            jitter=lambda: 0,
        )
        self.assertEqual(dead_relay.run_once(datetime.now(UTC)), "dead_letter")
        with connect(self.test_dsn) as conn:
            status, classification = conn.execute(
                "SELECT publication_state,error_class FROM scout_outbox WHERE event_id=%s",
                ("handoff:" + dead_session.proposal.proposal_id,),
            ).fetchone()
        self.assertEqual((status, classification), ("dead_letter", "definitive"))

    def test_independent_worker_reclaims_after_process_crashes(self):
        with connect(self.test_dsn) as conn:
            conn.execute(
                "CREATE TABLE test_crash_receiver(message_id text PRIMARY KEY, receipt_id text NOT NULL)"
            )

        def create_event(summary):
            session = self.make_session()
            session.proposal = PlaybookProposal(
                "proposal-" + session.session_id,
                session.session_id,
                session.source,
                summary,
                ("finding-1",),
                ("manifest-1",),
                (),
                (),
                ("read",),
            )
            self.repo.create(
                session, str(uuid4()), hashlib.sha256(summary.encode()).hexdigest()
            )
            self.repo.save(session, 0)
            return "handoff:" + session.proposal.proposal_id

        script = r"""
import os, sys
from datetime import UTC, datetime
import psycopg
from scout.adapters.postgres.repository import PostgresScoutSessionRepository
from scout.outbox_relay import OutboxRelay
from scout.relay_worker import run_worker
dsn, mode = sys.argv[1:]
repo = PostgresScoutSessionRepository(dsn)
class Receiver:
 def deliver_message(self, message_id, payload):
  with psycopg.connect(dsn) as c: c.execute("INSERT INTO test_crash_receiver VALUES (%s,%s) ON CONFLICT (message_id) DO NOTHING", (message_id, "receipt-" + message_id))
  if mode == "crash-after-send": os._exit(78)
  return {"message_id": message_id, "status": "received"}
if mode == "crash-after-claim":
 repo.claim_handoff("crashing-worker", 60, datetime.now(UTC))
 os._exit(77)
relay = OutboxRelay(repo, Receiver(), owner="recovery-worker", jitter=lambda: 0)
print(run_worker(relay, __import__('threading').Event(), max_messages=1))
"""
        env = dict(os.environ, PYTHONPATH=str(ROOT / "apps" / "scout"))

        def child(mode, exit_code=0):
            return subprocess.run(
                [sys.executable, "-c", script, self.test_dsn, mode],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        before_send = create_event("crash before receiver send")
        crashed = child("crash-after-claim", 77)
        self.assertEqual(crashed.returncode, 77, crashed.stderr)
        with connect(self.test_dsn) as conn:
            state, attempts = conn.execute(
                "SELECT publication_state,attempt_count FROM scout_outbox WHERE event_id=%s",
                (before_send,),
            ).fetchone()
            self.assertEqual(
                conn.execute("SELECT count(*) FROM test_crash_receiver").fetchone()[0],
                0,
            )
            conn.execute(
                "UPDATE scout_outbox SET lease_deadline=now()-interval '1 second' WHERE event_id=%s",
                (before_send,),
            )
        self.assertEqual((state, attempts), ("leased", 1))
        resumed = child("resume")
        self.assertEqual(
            (resumed.returncode, resumed.stdout.strip()), (0, "1"), resumed.stderr
        )

        after_send = create_event("crash after receiver commit")
        crashed = child("crash-after-send", 78)
        self.assertEqual(crashed.returncode, 78, crashed.stderr)
        with connect(self.test_dsn) as conn:
            state, attempts, received = conn.execute(
                "SELECT o.publication_state,o.attempt_count,(SELECT count(*) FROM test_crash_receiver r WHERE r.message_id=o.event_id) FROM scout_outbox o WHERE event_id=%s",
                (after_send,),
            ).fetchone()
            conn.execute(
                "UPDATE scout_outbox SET lease_deadline=now()-interval '1 second' WHERE event_id=%s",
                (after_send,),
            )
        self.assertEqual((state, attempts, received), ("leased", 1, 1))
        resumed = child("resume")
        self.assertEqual(
            (resumed.returncode, resumed.stdout.strip()), (0, "1"), resumed.stderr
        )
        with connect(self.test_dsn) as conn:
            status, attempts, received = conn.execute(
                "SELECT o.publication_state,o.attempt_count,(SELECT count(*) FROM test_crash_receiver r WHERE r.message_id=o.event_id) FROM scout_outbox o WHERE event_id=%s",
                (after_send,),
            ).fetchone()
        self.assertEqual((status, attempts, received), ("sent", 2, 1))

    def test_child_restart_reconciles_bc15_lost_ack_without_duplicate(self):
        session = self.make_session()
        session.proposal = PlaybookProposal(
            "proposal-" + session.session_id,
            session.session_id,
            session.source,
            "test proposal",
            ("finding-1",),
            ("manifest-1",),
            (),
            (),
            ("read",),
        )
        self.repo.create(session, str(uuid4()), "9" * 64)
        self.repo.save(session, 0)
        event = next(
            item
            for item in self.repo.pending_handoffs()
            if item["proposal_id"] == session.proposal.proposal_id
        )
        with connect(self.test_dsn) as conn:
            # Test-only receiver-owned table: deliberately outside BC14 transactions.
            conn.execute(
                "CREATE TABLE test_bc15_receipts(proposal_id text PRIMARY KEY,receipt_id text NOT NULL)"
            )
        receiver_script = """
import os, psycopg, sys
with psycopg.connect(os.environ['SCOUT_CHILD_DSN']) as conn:
    conn.execute('INSERT INTO test_bc15_receipts VALUES (%s,%s) ON CONFLICT (proposal_id) DO NOTHING', (sys.argv[1], 'receipt-' + sys.argv[1]))
os._exit(42)
"""
        env = dict(os.environ, SCOUT_CHILD_DSN=self.test_dsn)
        for _ in range(2):
            child = subprocess.run(
                [sys.executable, "-c", receiver_script, event["proposal_id"]],
                env=env,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(child.returncode, 42)
        restarted = PostgresScoutSessionRepository(self.test_dsn)
        with connect(self.test_dsn) as conn:
            count, receipt_id = conn.execute(
                "SELECT count(*),min(receipt_id) FROM test_bc15_receipts WHERE proposal_id=%s",
                (event["proposal_id"],),
            ).fetchone()
        self.assertEqual(count, 1)
        claim = restarted.claim_handoff("restart-relay", 30, datetime.now(UTC))
        self.assertEqual(claim["event_id"], event["event_id"])
        self.assertTrue(
            restarted.finish_handoff(
                event["event_id"],
                "restart-relay",
                claim["fencing_token"],
                status="sent",
                error_class=None,
                result="received_by_bc15",
                next_attempt_delay_seconds=0,
                receipt={"receipt_id": receipt_id},
            )
        )

    def test_bc06_unknown_outcome_reconciles_stable_artifact_and_digest(self):
        session = self.make_session()
        self.repo.create(session, str(uuid4()), "8" * 64)
        artifact_id = "artifact-" + session.session_id
        content = b"test-only evidence payload"
        digest = hashlib.sha256(content).hexdigest()
        self.repo.begin_custody_intent(
            artifact_id, session.session_id, digest, len(content)
        )
        with connect(self.test_dsn) as conn:
            # Separate test-owned custody table models BC06's independent commit.
            conn.execute(
                "CREATE TABLE test_bc06_objects(artifact_id text PRIMARY KEY,sha256 text NOT NULL,content bytea NOT NULL)"
            )
        child_script = """
import os, psycopg, sys
with psycopg.connect(os.environ['SCOUT_CHILD_DSN']) as conn:
    conn.execute('INSERT INTO test_bc06_objects VALUES (%s,%s,%s) ON CONFLICT (artifact_id) DO NOTHING', (sys.argv[1],sys.argv[2],bytes.fromhex(sys.argv[3])))
os._exit(43)
"""
        env = dict(os.environ, SCOUT_CHILD_DSN=self.test_dsn)
        child = subprocess.run(
            [sys.executable, "-c", child_script, artifact_id, digest, content.hex()],
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(child.returncode, 43)
        self.repo.mark_custody_unknown(artifact_id)
        restarted = PostgresScoutSessionRepository(self.test_dsn)
        with connect(self.test_dsn) as conn:
            stored_digest, stored_bytes = conn.execute(
                "SELECT sha256,content FROM test_bc06_objects WHERE artifact_id=%s",
                (artifact_id,),
            ).fetchone()
        self.assertEqual(hashlib.sha256(stored_bytes).hexdigest(), digest)
        self.assertEqual(stored_digest, digest)
        restarted.confirm_custody_intent(
            artifact_id,
            digest,
            {"artifact_id": artifact_id, "sha256": stored_digest},
        )
        with connect(self.test_dsn) as conn:
            status = conn.execute(
                "SELECT status FROM scout_custody_intents WHERE artifact_id=%s",
                (artifact_id,),
            ).fetchone()[0]
            count = conn.execute(
                "SELECT count(*) FROM test_bc06_objects WHERE artifact_id=%s",
                (artifact_id,),
            ).fetchone()[0]
        self.assertEqual(status, "verified")
        self.assertEqual(count, 1)

    def test_application_custody_reconciler_and_bc06_owned_orphan_review(self):
        session = self.make_session()
        self.repo.create(session, str(uuid4()), "7" * 64)
        artifact_id = "artifact-reconcile-" + uuid4().hex
        content = b"reconciliation evidence"
        digest = hashlib.sha256(content).hexdigest()
        self.repo.begin_custody_intent(
            artifact_id, session.session_id, digest, len(content)
        )
        with connect(self.test_dsn) as conn:
            conn.execute(
                "CREATE TABLE test_bc06_lookup(lookup_id text PRIMARY KEY,artifact_id text,session_id text,source_id text,revision text,sha256 text,content bytea NOT NULL,size_bytes bigint NOT NULL,custody_status text NOT NULL)"
            )
            conn.execute(
                "INSERT INTO test_bc06_lookup VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    artifact_id,
                    artifact_id,
                    session.session_id,
                    session.source.source_id,
                    session.source.revision,
                    digest,
                    content,
                    len(content),
                    "available",
                ),
            )

        class CustodyLookup:
            def lookup(inner, requested_id):
                with connect(self.test_dsn) as conn:
                    row = conn.execute(
                        "SELECT artifact_id,session_id,source_id,revision,sha256,content,size_bytes,custody_status FROM test_bc06_lookup WHERE lookup_id=%s",
                        (requested_id,),
                    ).fetchone()
                return (
                    ArtifactRef(
                        row[0],
                        row[1],
                        SourceRef(row[2], row[3]),
                        row[4],
                        size_bytes=row[6],
                        custody_status=row[7],
                    )
                    if row
                    else None
                )

            def verify(inner, artifact):
                with connect(self.test_dsn) as conn:
                    stored = conn.execute(
                        "SELECT content FROM test_bc06_lookup WHERE lookup_id=%s",
                        (artifact.artifact_id,),
                    ).fetchone()
                return bool(
                    stored and hashlib.sha256(stored[0]).hexdigest() == artifact.sha256
                )

        from scout.application import ScoutApplication

        app = ScoutApplication(
            None,
            None,
            PostgresScoutSessionRepository(self.test_dsn),
            CustodyLookup(),
        )
        self.assertEqual(app.reconcile_custody_intent(artifact_id), "confirmed")
        self.assertEqual(
            app.reconcile_custody_intent("unknown-artifact"), "unknown_intent"
        )
        mismatches = {
            "identity": (
                "wrong-artifact-id",
                session.session_id,
                session.source,
                digest,
                content,
                len(content),
                "available",
            ),
            "session": (
                "artifact-session-mismatch",
                "other-session",
                session.source,
                digest,
                content,
                len(content),
                "available",
            ),
            "source": (
                "artifact-source-mismatch",
                session.session_id,
                SourceRef("other-source", "rev-2"),
                digest,
                content,
                len(content),
                "available",
            ),
            "digest": (
                "artifact-digest-mismatch",
                session.session_id,
                session.source,
                "0" * 64,
                content,
                len(content),
                "available",
            ),
            "integrity": (
                "artifact-bytes-mismatch",
                session.session_id,
                session.source,
                digest,
                b"tampered bytes",
                len(content),
                "available",
            ),
            "size": (
                "artifact-size-mismatch",
                session.session_id,
                session.source,
                digest,
                content,
                len(content) + 1,
                "available",
            ),
            "custody-status": (
                "artifact-status-mismatch",
                session.session_id,
                session.source,
                digest,
                content,
                len(content),
                "unknown",
            ),
        }
        mismatch_ids = []
        for kind, (
            reported_id,
            reported_session,
            reported_source,
            reported_digest,
            reported_bytes,
            reported_size,
            reported_status,
        ) in mismatches.items():
            lookup_id = f"artifact-{kind}-" + uuid4().hex
            mismatch_ids.append(lookup_id)
            intent_digest = hashlib.sha256(content).hexdigest()
            self.repo.begin_custody_intent(
                lookup_id, session.session_id, intent_digest, len(content)
            )
            if kind != "identity":
                reported_id = lookup_id
            with connect(self.test_dsn) as conn:
                conn.execute(
                    "INSERT INTO test_bc06_lookup VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        lookup_id,
                        reported_id,
                        reported_session,
                        reported_source.source_id,
                        reported_source.revision,
                        reported_digest,
                        reported_bytes,
                        reported_size,
                        reported_status,
                    ),
                )
            restarted_app = ScoutApplication(
                None,
                None,
                PostgresScoutSessionRepository(self.test_dsn),
                CustodyLookup(),
            )
            self.assertEqual(
                restarted_app.reconcile_custody_intent(lookup_id), "manual_review", kind
            )
        restarted_repository = PostgresScoutSessionRepository(self.test_dsn)
        for lookup_id in mismatch_ids:
            persisted = restarted_repository.get_custody_intent(lookup_id)
            self.assertEqual(persisted["status"], "manual_review")
            self.assertIsNone(persisted["artifact_ref"])
        with connect(self.test_dsn) as conn:
            audit_count = conn.execute(
                "SELECT count(*) FROM scout_reconciliation_events WHERE event_kind='digest_or_identity_discrepancy' AND owner='BC14'"
            ).fetchone()[0]
        self.assertGreaterEqual(audit_count, len(mismatches))
        orphan_id = "orphan-" + uuid4().hex
        app.record_bc06_orphan_candidate(
            orphan_id,
            "bc06-inventory-candidate",
            {
                "identity": "bc06-inventory-candidate",
                "sha256": digest,
                "custody_status": "present",
            },
        )
        with connect(self.test_dsn) as conn:
            intent_status = conn.execute(
                "SELECT status FROM scout_custody_intents WHERE artifact_id=%s",
                (artifact_id,),
            ).fetchone()[0]
            owner, status = conn.execute(
                "SELECT reported_by,status FROM scout_orphan_reviews WHERE review_id=%s",
                (orphan_id,),
            ).fetchone()
            event_owner = conn.execute(
                "SELECT owner FROM scout_reconciliation_events WHERE event_id=%s",
                ("orphan-review:" + orphan_id,),
            ).fetchone()[0]
        self.assertEqual(intent_status, "verified")
        self.assertEqual(
            (owner, status, event_owner), ("BC06", "manual_review", "BC06")
        )

    def test_failed_transaction_rolls_back_session_and_receipt(self):
        session = self.make_session()
        key = str(uuid4())
        with self.assertRaises(RuntimeError), connect(self.test_dsn) as conn:
            conn.execute(
                "INSERT INTO scout_sessions(session_id,version,document_version,payload) VALUES (%s,0,1,'{}')",
                (session.session_id,),
            )
            conn.execute(
                "INSERT INTO scout_command_receipts(principal,idempotency_key,command_id,command_kind,fingerprint,session_id,status,response_snapshot) VALUES ('tester',%s,%s,'RunScout',%s,%s,'in_progress','{}')",
                (
                    key,
                    "not-unique",
                    "d" * 64,
                    session.session_id,
                ),
            )
            raise RuntimeError("injected pre-commit failure")
        self.assertIsNone(self.repo.find_intent("tester", key, "d" * 64))

    def test_outbox_insert_failure_rolls_back_cas_and_receipt(self):
        session = self.make_session()
        session.proposal = PlaybookProposal(
            "proposal-" + session.session_id,
            session.session_id,
            session.source,
            "proposal",
            ("finding",),
            ("manifest",),
            (),
            (),
            ("read",),
        )
        key = str(uuid4())
        self.repo.create(session, key, "f" * 64)
        with connect(self.test_dsn) as conn:
            conn.execute("""CREATE FUNCTION reject_scout_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'injected outbox failure'; END; $$""")
            conn.execute(
                "CREATE TRIGGER reject_scout_outbox BEFORE INSERT ON scout_outbox FOR EACH ROW EXECUTE FUNCTION reject_scout_outbox()"
            )
        try:
            with self.assertRaises(Error):
                self.repo.save(session, 0)
        finally:
            with connect(self.test_dsn) as conn:
                conn.execute("DROP TRIGGER reject_scout_outbox ON scout_outbox")
                conn.execute("DROP FUNCTION reject_scout_outbox()")
        with connect(self.test_dsn) as conn:
            version = conn.execute(
                "SELECT version FROM scout_sessions WHERE session_id=%s",
                (session.session_id,),
            ).fetchone()[0]
            receipt = conn.execute(
                "SELECT status,response_snapshot->'session'->>'version' FROM scout_command_receipts WHERE idempotency_key=%s",
                (key,),
            ).fetchone()
            count = conn.execute(
                "SELECT count(*) FROM scout_outbox WHERE proposal_id=%s",
                (session.proposal.proposal_id,),
            ).fetchone()[0]
        self.assertEqual(version, 0)
        self.assertEqual(receipt, ("in_progress", "0"))
        self.assertEqual(count, 0)

    def test_application_crash_inside_create_rolls_back_then_fresh_process_retries_once(
        self,
    ):
        with connect(self.test_dsn) as conn:
            conn.execute(
                "CREATE TABLE test_precommit_effects(effect_id text PRIMARY KEY)"
            )
            conn.execute("CREATE TABLE test_precommit_calls(kind text NOT NULL)")
            conn.execute(
                "CREATE TABLE test_precommit_receiver(message_id text PRIMARY KEY)"
            )

        script = r"""
import hashlib, json, os, sys
from datetime import UTC, datetime, timedelta
import psycopg
from psycopg.types.json import Jsonb
from scout.adapters.postgres.repository import PostgresScoutSessionRepository, _session_document
from scout.application import ScoutApplication
from scout.domain import ArtifactRef, PolicyDecisionRef, ScoutScope, SourceRef

dsn, request_id, key, mode = sys.argv[1:]
now = datetime(2035, 1, 1, tzinfo=UTC)
source = SourceRef("source-precommit", "rev-1")
scope = ScoutScope(("https://example.org",), ("/",), ("GET",), 3, 30)
request = {"request_id": request_id, "idempotency_key": key, "source": {"source_id": source.source_id, "revision": source.revision}, "objective": "retry after uncommitted crash", "requested_by": "operator-1", "requested_scope": {"origins": ["https://example.org"], "path_prefixes": ["/"], "methods": ["GET"], "max_requests": 3, "max_duration_seconds": 30}}
class Sources:
 def resolve(self, requested):
  with psycopg.connect(dsn) as c: c.execute("INSERT INTO test_precommit_calls VALUES ('source')")
  return scope
class Policy:
 def decide(self, principal, requested, at):
  with psycopg.connect(dsn) as c: c.execute("INSERT INTO test_precommit_calls VALUES ('policy')")
  return PolicyDecisionRef("decision-1", principal, requested, True, now + timedelta(days=1), scope)
class Custody:
 def lookup(self, artifact_id): return None
 def store(self, session_id, requested, content, artifact_id):
  digest = hashlib.sha256(content).hexdigest()
  with psycopg.connect(dsn) as c: c.execute("INSERT INTO test_precommit_effects VALUES (%s) ON CONFLICT DO NOTHING", (artifact_id,))
  return ArtifactRef(artifact_id, session_id, requested, digest, size_bytes=len(content), custody_status="available")
 def verify(self, artifact): return True
class CrashRepository(PostgresScoutSessionRepository):
 def create(self, session, idem_key, fingerprint):
  document = _session_document(session)
  with psycopg.connect(dsn) as conn:
   conn.execute("INSERT INTO scout_sessions(session_id,version,document_version,payload) VALUES (%s,%s,1,%s)", (session.session_id, session.version, Jsonb(document)))
   conn.execute("INSERT INTO scout_command_receipts(principal,idempotency_key,command_id,command_kind,fingerprint,session_id,status,response_snapshot) VALUES (%s,%s,%s,'RunScout',%s,%s,'in_progress',%s)", (session.principal, idem_key, session.session_id, fingerprint, session.session_id, Jsonb(document)))
   os._exit(75)
repo = CrashRepository(dsn) if mode == "crash" else PostgresScoutSessionRepository(dsn)
app = ScoutApplication(Sources(), Policy(), repo, Custody())
session = app.run(request, "operator-1", b"precommit fixture", now)
print(json.dumps({"session_id": session.session_id, "state": session.state.value, "handoff_result": session.handoff_result}))
"""
        request_id, key = "precommit-" + uuid4().hex, "retry-" + uuid4().hex
        env = dict(os.environ, PYTHONPATH=str(ROOT / "apps" / "scout"))
        crash = subprocess.run(
            [sys.executable, "-c", script, self.test_dsn, request_id, key, "crash"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(crash.returncode, 75, crash.stderr)
        with connect(self.test_dsn) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM scout_sessions WHERE session_id=%s",
                    (request_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM scout_command_receipts WHERE idempotency_key=%s",
                    (key,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM scout_outbox WHERE aggregate_id=%s",
                    (request_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM test_precommit_effects").fetchone()[
                    0
                ],
                0,
            )
        retry = subprocess.run(
            [sys.executable, "-c", script, self.test_dsn, request_id, key, "retry"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        result = json.loads(retry.stdout.strip().splitlines()[-1])
        self.assertEqual(
            (result["session_id"], result["state"], result["handoff_result"]),
            (request_id, "exploring", "delivery_pending"),
        )
        with connect(self.test_dsn) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM scout_sessions WHERE session_id=%s",
                    (request_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM scout_command_receipts WHERE principal='operator-1' AND idempotency_key=%s",
                    (key,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM scout_outbox WHERE aggregate_id=%s",
                    (request_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM test_precommit_effects").fetchone()[
                    0
                ],
                1,
            )
            self.assertGreaterEqual(
                conn.execute(
                    "SELECT count(*) FROM test_precommit_calls WHERE kind='policy'"
                ).fetchone()[0],
                2,
            )

    def test_application_replay_after_fresh_process_rechecks_authorization(self):
        """The command returns pending, a separate process delivers, and replay finalizes."""
        with connect(self.test_dsn) as conn:
            conn.execute("CREATE TABLE test_g1_policy_calls(kind text NOT NULL)")
            conn.execute(
                "CREATE TABLE test_g1_custody(artifact_id text PRIMARY KEY, session_id text, source_id text, revision text, sha256 text, size_bytes bigint, custody_status text)"
            )
            conn.execute(
                "CREATE TABLE test_g1_receiver(proposal_id text PRIMARY KEY, receipt_id text NOT NULL)"
            )

        script = r"""
import hashlib, json, os, sys
from datetime import UTC, datetime, timedelta
import psycopg
from scout.adapters.postgres.repository import PostgresScoutSessionRepository
from scout.application import ScoutApplication
from scout.domain import ArtifactRef, AuthorizationError, ConflictError, PolicyDecisionRef, ScoutScope, SourceRef, SourceRevisionConflict
from scout.outbox_relay import OutboxRelay

dsn, command_id, key, mode = sys.argv[1:]
now = datetime(2035, 1, 1, tzinfo=UTC)
scope = ScoutScope(("https://example.org",), ("/catalog/",), ("GET",), 10, 60)
request = {"request_id": command_id, "idempotency_key": key, "source": {"source_id": "source-123", "revision": "rev-4"}, "objective": "replay integration", "requested_by": "analyst-1", "requested_scope": {"origins": ["https://example.org"], "path_prefixes": ["/catalog/"], "methods": ["GET"], "max_requests": 10, "max_duration_seconds": 60}}
source = SourceRef(**request["source"])
class Sources:
 def resolve(self, requested):
  assert requested == source
  with psycopg.connect(dsn) as c: c.execute("INSERT INTO test_g1_policy_calls VALUES ('source')")
  if mode == "source-unavailable": raise RuntimeError("fixed source revision unavailable")
  if mode == "source-change": return ScoutScope(("https://example.org",), ("/catalog/subpath/",), ("GET",), 10, 60)
  return scope
class Policy:
 def decide(self, principal, requested, at):
  assert principal == "analyst-1" and requested == source
  with psycopg.connect(dsn) as c: c.execute("INSERT INTO test_g1_policy_calls VALUES ('policy')")
  if mode in ("deny", "unavailable"): raise AuthorizationError("test replay authorization denied")
  if mode == "expired": return PolicyDecisionRef("policy-current", principal, requested, True, now, scope)
  if mode == "revoked": return PolicyDecisionRef("policy-current", principal, requested, True, now + timedelta(days=1), scope, revoked=True)
  if mode == "scope-downgrade": return PolicyDecisionRef("policy-current", principal, requested, True, now + timedelta(days=1), ScoutScope(("https://example.org",), ("/catalog/",), ("GET",), 1, 10))
  return PolicyDecisionRef("policy-current", principal, requested, True, now + timedelta(days=1), scope)
class Custody:
 def lookup(self, artifact_id):
  with psycopg.connect(dsn) as c: row = c.execute("SELECT session_id,source_id,revision,sha256 FROM test_g1_custody WHERE artifact_id=%s", (artifact_id,)).fetchone()
  if row is None: return None
  return ArtifactRef(artifact_id, row[0], SourceRef(row[1], row[2]), row[3], size_bytes=len(b"durable fresh-process evidence"), custody_status="available")
 def store(self, session_id, requested, content, artifact_id):
  digest = hashlib.sha256(content).hexdigest()
  with psycopg.connect(dsn) as c: c.execute("INSERT INTO test_g1_custody VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (artifact_id) DO NOTHING", (artifact_id, session_id, requested.source_id, requested.revision, digest, len(content), "available"))
  return ArtifactRef(artifact_id, session_id, requested, digest, size_bytes=len(content), custody_status="available")
 def verify(self, artifact):
  with psycopg.connect(dsn) as c: row = c.execute("SELECT session_id,source_id,revision,sha256 FROM test_g1_custody WHERE artifact_id=%s", (artifact.artifact_id,)).fetchone()
  return row == (artifact.session_id, artifact.source.source_id, artifact.source.revision, artifact.sha256)
class Receiver:
 def deliver_message(self, message_id, payload):
  with psycopg.connect(dsn) as c: c.execute("INSERT INTO test_g1_receiver VALUES (%s,%s) ON CONFLICT (proposal_id) DO NOTHING", (payload["proposal_id"], "receipt-" + payload["proposal_id"]))
  return {"status": "received", "message_id": message_id}
repo = PostgresScoutSessionRepository(dsn)
if mode == "worker":
 os.environ["SCOUT_DATABASE_URL"] = dsn
 os.environ["SCOUT_BC15_RECEIVER_FACTORY"] = "tests.integration.test_postgres_scout:build_test_receiver"
 from scout.relay_worker import main
 raise SystemExit(main(["--once"]))
app = ScoutApplication(Sources(), Policy(), repo, Custody())
fixture = b"durable fresh-process evidence"
principal = "other-principal" if mode == "principal-mismatch" else "analyst-1"
if mode == "fingerprint-mismatch": request["objective"] = "changed request fingerprint"
try:
 session = app.run(request, principal, fixture, now)
except SourceRevisionConflict:
 if mode == "source-change":
  print(json.dumps("SourceRevisionConflict"))
  raise SystemExit(0)
 raise
except ConflictError:
 if mode == "fingerprint-mismatch":
  print(json.dumps("ConflictError"))
  raise SystemExit(0)
 raise
if mode == "crash-after-commit": os._exit(74)
print(json.dumps({"session_id": session.session_id, "version": session.version, "state": session.state.value, "handoff_result": session.handoff_result}))
"""
        command_id, key = "g1-" + uuid4().hex, "idem-" + uuid4().hex
        env = dict(os.environ, PYTHONPATH=str(ROOT / "apps" / "scout"))

        def invoke(mode, expected_exit=0):
            child_env = dict(env)
            if mode == "worker":
                child_env["SCOUT_DATABASE_URL"] = self.test_dsn
                child_env["SCOUT_BC15_RECEIVER_FACTORY"] = (
                    "tests.integration.test_postgres_scout:build_test_receiver"
                )
            child = subprocess.run(
                [sys.executable, "-c", script, self.test_dsn, command_id, key, mode],
                env=child_env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(child.returncode, expected_exit, child.stderr)
            return (
                json.loads(child.stdout.strip().splitlines()[-1])
                if child.stdout.strip()
                else None
            )

        pending = invoke("replay")
        self.assertEqual(
            (pending["state"], pending["handoff_result"]),
            ("exploring", "delivery_pending"),
        )
        invoke("crash-after-commit", 74)
        with connect(self.test_dsn) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT publication_state FROM scout_outbox WHERE aggregate_id=%s",
                    (command_id,),
                ).fetchone()[0],
                "pending",
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM test_g1_receiver").fetchone()[0], 0
            )
        self.assertIsNone(invoke("worker"))
        with connect(self.test_dsn) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT publication_state FROM scout_outbox WHERE aggregate_id=%s",
                    (command_id,),
                ).fetchone()[0],
                "sent",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM scout_command_receipts WHERE idempotency_key=%s",
                    (key,),
                ).fetchone()[0],
                "in_progress",
            )
        finalized = invoke("replay")
        terminal_replay = invoke("terminal-replay")
        outputs = [finalized, terminal_replay]
        self.assertEqual(finalized, terminal_replay)
        self.assertEqual(
            (finalized["state"], finalized["handoff_result"]),
            ("completed", "received_by_bc15"),
        )
        self.assertEqual(invoke("source-change"), "SourceRevisionConflict")
        self.assertEqual(invoke("fingerprint-mismatch"), "ConflictError")
        for rejected_mode in (
            "deny",
            "unavailable",
            "expired",
            "revoked",
            "principal-mismatch",
            "source-unavailable",
            "scope-downgrade",
        ):
            with self.subTest(rejected_mode=rejected_mode):
                denied = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        self.test_dsn,
                        command_id,
                        key,
                        rejected_mode,
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertNotEqual(denied.returncode, 0)
                self.assertNotIn('"session_id"', denied.stdout)
        with connect(self.test_dsn) as conn:
            version, payload_version = conn.execute(
                "SELECT version,payload->'session'->>'version' FROM scout_sessions WHERE session_id=%s",
                (outputs[0]["session_id"],),
            ).fetchone()
            receipt = conn.execute(
                "SELECT status,session_id,response_snapshot->'session'->>'version',response_snapshot->'session'->>'session_id' FROM scout_command_receipts WHERE idempotency_key=%s",
                (key,),
            ).fetchone()
            outbox_count = conn.execute(
                "SELECT count(*) FROM scout_outbox WHERE aggregate_id=%s",
                (outputs[0]["session_id"],),
            ).fetchone()[0]
            custody_count = conn.execute(
                "SELECT count(*) FROM test_g1_custody"
            ).fetchone()[0]
            handoff_count = conn.execute(
                "SELECT count(*) FROM test_g1_receiver"
            ).fetchone()[0]
            source_checks, policy_checks = conn.execute(
                "SELECT count(*) FILTER (WHERE kind='source'), count(*) FILTER (WHERE kind='policy') FROM test_g1_policy_calls"
            ).fetchone()
        self.assertEqual(
            (version, payload_version),
            (outputs[0]["version"], str(outputs[0]["version"])),
        )
        self.assertEqual(
            receipt,
            (
                "completed",
                outputs[0]["session_id"],
                str(outputs[0]["version"]),
                outputs[0]["session_id"],
            ),
        )
        self.assertEqual((outbox_count, custody_count, handoff_count), (1, 1, 1))
        self.assertGreaterEqual(source_checks, 2)
        self.assertGreaterEqual(policy_checks, 2)


if __name__ == "__main__":
    unittest.main()
