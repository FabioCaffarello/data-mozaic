import hashlib
import unittest
from datetime import UTC, datetime, timedelta

from scout.domain import AuthorizationError, SourceRef
from scout.tools.discovery import HTTPDiscoveryTool
from scout.tools.policy import (
    ApprovedScope,
    EffectiveScope,
    LocalAuthorizationStore,
    OriginGrant,
    RegisteredSource,
    effective_scope,
)
from scout.tools.ports import BudgetState, EvidenceReference, TrustedToolContext
from scout.tools.transport import AiohttpSquidTransport, HTTPResponse
from scout.tools.url_policy import TargetRejected, normalize_origin, normalize_url

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
SOURCE = SourceRef("owned-source", "revision-1")


def limits(max_requests=5):
    return {
        "max_requests": max_requests,
        "max_duration_seconds": 30,
        "max_response_bytes": 8192,
    }


class URLPolicyTests(unittest.TestCase):
    def test_normalizes_https_origin_host_and_path(self):
        target = normalize_url("HTTPS://BÜCHER.example:443/caf%C3%A9")
        self.assertEqual(target.origin, "https://xn--bcher-kva.example")
        self.assertEqual(target.path, "/caf%C3%A9")
        self.assertEqual(
            normalize_origin("https://Example.org/"), "https://example.org"
        )

    def test_rejects_ambiguous_or_unsafe_targets(self):
        for url in (
            "http://example.org/",
            "https://user@example.org/",
            "https://example.org:444/",
            "https://example.org/a%2fb",
            "https://example.org/../private",
            "https://127.0.0.1/",
            "https://example.org/?access_token=x",
            "https://example.org/%zz",
        ):
            with self.subTest(url=url), self.assertRaises(TargetRejected):
                normalize_url(url)


class HTTPTransportConfigurationTests(unittest.TestCase):
    def test_transport_requires_the_named_local_egress_proxy(self):
        self.assertEqual(
            "http://squid:3128", AiohttpSquidTransport("http://squid:3128").proxy_url
        )
        for proxy in (
            "https://squid:3128",
            "http://squid:8080",
            "http://other-proxy:3128",
            "http://user:secret@squid:3128",
        ):
            with self.subTest(proxy=proxy), self.assertRaises(ValueError):
                AiohttpSquidTransport(proxy)

    def test_path_prefix_matches_segments(self):
        grant = OriginGrant("https://example.org", ("/foo",))
        policy = EffectiveScope(
            "analyst", SOURCE, (grant,), NOW + timedelta(minutes=5), 3, 20, 8192
        )
        policy.authorize_url(normalize_url("https://example.org/foo/bar"), NOW)
        with self.assertRaises(AuthorizationError):
            policy.authorize_url(normalize_url("https://example.org/foobar"), NOW)

    def test_domain_scope_intersection_is_segment_aware(self):
        from scout.domain import IntegrityError, ScoutScope

        requested = ScoutScope(("https://example.org",), ("/foo",), ("GET",), 4, 20)
        policy = ScoutScope(("https://example.org",), ("/foobar",), ("GET",), 2, 10)
        with self.assertRaises(IntegrityError):
            requested.intersect(policy)


class LocalAuthorizationTests(unittest.TestCase):
    def test_intersection_preserves_origin_path_pairs(self):
        registered = RegisteredSource(
            SOURCE,
            (
                OriginGrant("https://one.example", ("/public",)),
                OriginGrant("https://two.example", ("/private",)),
            ),
            8,
            60,
            8192,
        )
        approval = ApprovedScope(
            "analyst",
            SOURCE,
            (
                OriginGrant("https://one.example", ("/private",)),
                OriginGrant("https://two.example", ("/public",)),
            ),
            NOW + timedelta(minutes=5),
            3,
            20,
            4096,
        )
        with self.assertRaises(AuthorizationError):
            effective_scope(registered, approval)

    def test_source_revision_principal_and_expiry_are_required(self):
        store = LocalAuthorizationStore(
            (
                RegisteredSource(
                    SOURCE,
                    (OriginGrant("https://example.org", ("/",)),),
                    4,
                    20,
                    8192,
                ),
            ),
            (
                ApprovedScope(
                    "analyst",
                    SOURCE,
                    (OriginGrant("https://example.org", ("/docs",)),),
                    NOW + timedelta(minutes=5),
                    2,
                    10,
                    4096,
                ),
            ),
        )
        self.assertEqual(store.authorize("analyst", SOURCE, NOW).max_requests, 2)
        with self.assertRaises(AuthorizationError):
            store.authorize("other", SOURCE, NOW)
        with self.assertRaises(AuthorizationError):
            store.authorize("analyst", SourceRef("owned-source", "revision-2"), NOW)
        with self.assertRaises(AuthorizationError):
            store.authorize("analyst", SOURCE, NOW + timedelta(minutes=5))


class ToolFakes:
    class Context:
        def current(self):
            return TrustedToolContext("analyst", "session-1", "call-1")

    class Clock:
        def now(self):
            return NOW

    class Budget:
        def __init__(self):
            self.used = 0
            self.calls = set()
            self.scopes = {}

        def consume(self, session_id, call_id, hop, scope):
            key = (session_id, call_id, hop)
            if key in self.calls:
                raise AuthorizationError("request replay")
            effective = self.scopes.get(session_id, scope).intersect(scope)
            self.scopes[session_id] = effective
            self.calls.add(key)
            self.used += 1
            if self.used > effective.max_requests:
                raise AuthorizationError("budget exhausted")
            return BudgetState(NOW, self.used, effective)

    class Evidence:
        def __init__(self):
            self.rows = []

        def store(self, session_id, source, url, status, content_type, body, now):
            row = EvidenceReference(
                f"evidence-{len(self.rows) + 1}",
                hashlib.sha256(body).hexdigest(),
                len(body),
                source,
            )
            self.rows.append((url, status, content_type, body, row))
            return row

    class Trace:
        def __init__(self):
            self.events = []

        def append(self, session_id, event_type, fields, now):
            self.events.append((event_type, fields))

    class Transport:
        def __init__(self, responses):
            self.responses = list(responses)
            self.urls = []

        async def get(self, url, max_bytes, timeout_seconds):
            self.urls.append(url)
            if not self.responses:
                raise AssertionError("unexpected HTTP request")
            return self.responses.pop(0)


class HTTPDiscoveryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = RegisteredSource(
            SOURCE,
            (OriginGrant("https://example.org", ("/foo",)),),
            4,
            30,
            8192,
        )
        self.approval = ApprovedScope(
            "analyst",
            SOURCE,
            (OriginGrant("https://example.org", ("/foo",)),),
            NOW + timedelta(minutes=5),
            4,
            30,
            8192,
        )
        self.auth = LocalAuthorizationStore((self.source,), (self.approval,))
        self.budget = ToolFakes.Budget()
        self.evidence = ToolFakes.Evidence()
        self.trace = ToolFakes.Trace()

    async def test_redirects_are_reauthorized_and_evidenced(self):
        body = (
            b"<html><title>Index</title><script>secret()</script>"
            b"<a href='/foo/item'>item</a><a href='/foobar/escape'>escape</a></html>"
        )
        transport = ToolFakes.Transport(
            [
                HTTPResponse(302, {"location": "/foo/next"}, b""),
                HTTPResponse(200, {"content-type": "text/html; charset=utf-8"}, body),
            ]
        )
        tool = HTTPDiscoveryTool(
            ToolFakes.Context(),
            self.auth,
            self.budget,
            self.evidence,
            self.trace,
            ToolFakes.Clock(),
            transport,
        )
        result = await tool.fetch(
            "owned-source", "revision-1", "https://example.org/foo"
        )
        self.assertEqual(
            transport.urls,
            [
                "https://example.org/foo",
                "https://example.org/foo/next",
            ],
        )
        self.assertEqual(self.budget.used, 2)
        self.assertEqual(len(self.evidence.rows), 2)
        self.assertEqual(result.evidence, self.evidence.rows[1][4])
        self.assertEqual(result.title, "Index")
        self.assertNotIn("secret()", result.text)
        self.assertEqual(result.links, ("https://example.org/foo/item",))
        self.assertIn(
            "http.redirect.authorized", [event[0] for event in self.trace.events]
        )

    async def test_out_of_scope_initial_url_never_reaches_transport(self):
        transport = ToolFakes.Transport([])
        tool = HTTPDiscoveryTool(
            ToolFakes.Context(),
            self.auth,
            self.budget,
            self.evidence,
            self.trace,
            ToolFakes.Clock(),
            transport,
        )
        with self.assertRaises(AuthorizationError):
            await tool.fetch("owned-source", "revision-1", "https://example.org/foobar")
        self.assertEqual(transport.urls, [])

    async def test_out_of_scope_redirect_is_denied_before_next_request(self):
        transport = ToolFakes.Transport(
            [HTTPResponse(302, {"location": "/foobar/escape"}, b"")]
        )
        tool = HTTPDiscoveryTool(
            ToolFakes.Context(),
            self.auth,
            self.budget,
            self.evidence,
            self.trace,
            ToolFakes.Clock(),
            transport,
        )
        with self.assertRaises(AuthorizationError):
            await tool.fetch("owned-source", "revision-1", "https://example.org/foo")
        self.assertEqual(len(transport.urls), 1)
        self.assertEqual(len(self.evidence.rows), 1)
        self.assertEqual(self.evidence.rows[0][1], 302)
        self.assertIn("http.target.denied", [event[0] for event in self.trace.events])


if __name__ == "__main__":
    unittest.main()
