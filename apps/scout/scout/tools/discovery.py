"""Policy-mediated HTTP discovery; callers cannot provide identity or budgets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..domain import AuthorizationError, SourceRef
from .ports import (
    AuthorizationPort,
    BudgetLedgerPort,
    ClockPort,
    EvidenceReference,
    EvidenceStorePort,
    ToolContextPort,
    TracePort,
)
from .transport import HTTPTransportPort
from .url_policy import NormalizedURL, TargetRejected, normalize_url


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveryResult:
    evidence: EvidenceReference
    status: int
    content_type: str
    text: str
    title: str | None
    links: tuple[str, ...]
    truncated: bool


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._hidden_depth = 0
        self._title_depth = 0

    def handle_starttag(self, tag, attrs):
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1
        if normalized == "title":
            self._title_depth += 1
        if normalized == "a" and self._hidden_depth == 0:
            href = dict(attrs).get("href")
            if href and len(self.links) < 500:
                self.links.append(href)

    def handle_endtag(self, tag):
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)
        if normalized == "title":
            self._title_depth = max(0, self._title_depth - 1)

    def handle_data(self, data):
        if self._hidden_depth:
            return
        cleaned = " ".join(data.split())
        if cleaned:
            if self._title_depth:
                self.title_parts.append(cleaned)
            self.text_parts.append(cleaned)


class HTTPDiscoveryTool:
    """Fetch one URL through an approved source, scope, and per-run budget."""

    def __init__(
        self,
        context: ToolContextPort,
        authorization: AuthorizationPort,
        budget: BudgetLedgerPort,
        evidence: EvidenceStorePort,
        trace: TracePort,
        clock: ClockPort,
        transport: HTTPTransportPort,
    ):
        self.context = context
        self.authorization = authorization
        self.budget = budget
        self.evidence = evidence
        self.trace = trace
        self.clock = clock
        self.transport = transport

    async def fetch(self, source_id: str, revision: str, url: str) -> DiscoveryResult:
        caller = self.context.current()
        if not caller.principal or not caller.session_id or not caller.call_id:
            raise AuthorizationError(
                "tool host did not provide an authenticated context"
            )
        source = SourceRef(source_id, revision)
        scope = self.authorization.authorize(caller.principal, source, self.clock.now())
        if scope.principal != caller.principal or scope.source != source:
            raise AuthorizationError("authorization scope identity mismatch")
        current = normalize_url(url)
        self._authorize_target(caller.session_id, current, scope, self.clock.now())
        max_hops = min(scope.max_requests, 10)

        for hop in range(max_hops):
            now = self.clock.now()
            current_caller = self.context.current()
            if current_caller != caller:
                raise AuthorizationError(
                    "authenticated tool context changed during request"
                )
            current_authorization = self.authorization.authorize(
                caller.principal, source, now
            )
            scope = scope.intersect(current_authorization)
            self._authorize_target(caller.session_id, current, scope, now)
            budget_state = self.budget.consume(
                caller.session_id,
                caller.call_id,
                hop,
                scope,
            )
            scope = budget_state.scope
            if (
                budget_state.started_at.tzinfo is None
                or not 1 <= budget_state.consumed_requests <= scope.max_requests
            ):
                raise AuthorizationError("invalid or exhausted navigation budget")
            elapsed = (now - budget_state.started_at).total_seconds()
            if elapsed < 0:
                raise AuthorizationError(
                    "navigation budget start time is in the future"
                )
            remaining = min(
                scope.max_duration_seconds - elapsed,
                scope.expires_at.timestamp() - now.timestamp(),
            )
            if remaining <= 0:
                raise AuthorizationError("navigation time budget expired")
            self.trace.append(
                caller.session_id,
                "http.request.authorized",
                _trace_target(current) | {"method": "GET", "hop": hop},
                now,
            )
            try:
                response = await self.transport.get(
                    current.url, scope.max_response_bytes, remaining
                )
            except Exception as error:
                self.trace.append(
                    caller.session_id,
                    "http.request.failed",
                    _trace_target(current) | {"failure_type": type(error).__name__},
                    self.clock.now(),
                )
                raise DiscoveryError("HTTP request failed") from error
            if len(response.body) > scope.max_response_bytes:
                raise DiscoveryError("transport exceeded approved response byte limit")
            if response.headers.get("content-encoding", "identity").lower() not in (
                "",
                "identity",
            ):
                raise DiscoveryError("compressed responses are not accepted")
            after_response = self.clock.now()
            refreshed = self.authorization.authorize(
                caller.principal, source, after_response
            )
            scope = scope.intersect(refreshed)
            self._authorize_target(caller.session_id, current, scope, after_response)
            if (
                after_response - budget_state.started_at
            ).total_seconds() > scope.max_duration_seconds:
                raise AuthorizationError(
                    "navigation time budget expired during response"
                )
            if response.status in {301, 302, 303, 307, 308}:
                redirect_evidence = self._store_evidence(
                    caller.session_id,
                    source,
                    current,
                    response,
                    response.headers.get("content-type", "application/octet-stream")
                    .split(";", 1)[0]
                    .lower(),
                )
                self.trace.append(
                    caller.session_id,
                    "http.redirect.observed",
                    _trace_target(current)
                    | {
                        "status": response.status,
                        "evidence_id": redirect_evidence.evidence_id,
                        "size_bytes": redirect_evidence.size_bytes,
                    },
                    after_response,
                )
                location = response.headers.get("location")
                if not location:
                    raise DiscoveryError("redirect response has no location")
                next_target = normalize_url(urljoin(current.url, location))
                self._authorize_target(
                    caller.session_id, next_target, scope, after_response
                )
                self.trace.append(
                    caller.session_id,
                    "http.redirect.authorized",
                    _trace_target(next_target) | {"hop": hop + 1},
                    after_response,
                )
                current = next_target
                continue
            return self._record_result(
                caller.session_id, source, current, response, scope
            )
        raise AuthorizationError("redirect count exceeds the approved request budget")

    def _authorize_target(self, session_id, target, scope, now):
        try:
            scope.authorize_url(target, now)
        except AuthorizationError:
            self.trace.append(
                session_id,
                "http.target.denied",
                _trace_target(target) | {"decision": "denied"},
                now,
            )
            raise

    def _record_result(
        self, session_id, source, target, response, scope
    ) -> DiscoveryResult:
        now = self.clock.now()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"text/html", "text/plain"}:
            raise DiscoveryError("only HTML and plain text responses are accepted")
        evidence = self._store_evidence(
            session_id, source, target, response, content_type
        )
        current_scope = scope.intersect(self._scope_for(session_id, source))
        text, title, links = _extract_content(
            response.body, content_type, target, current_scope, now
        )
        truncated = len(text) > 16_000
        text = text[:16_000]
        self.trace.append(
            session_id,
            "http.response.recorded",
            {
                "url_sha256": _url_digest(target),
                "status": response.status,
                "content_type": content_type,
                "size_bytes": evidence.size_bytes,
                "evidence_id": evidence.evidence_id,
            },
            now,
        )
        return DiscoveryResult(
            evidence, response.status, content_type, text, title, links, truncated
        )

    def _store_evidence(self, session_id, source, target, response, content_type):
        evidence = self.evidence.store(
            session_id,
            source,
            target.url,
            response.status,
            content_type,
            response.body,
            self.clock.now(),
        )
        if (
            evidence.source != source
            or evidence.size_bytes != len(response.body)
            or evidence.sha256 != hashlib.sha256(response.body).hexdigest()
        ):
            raise DiscoveryError(
                "evidence store returned inconsistent identity or digest"
            )
        return evidence

    def _scope_for(self, session_id: str, source: SourceRef):
        # Reauthorize link disclosure against current policy; links never trigger requests.
        caller = self.context.current()
        if caller.session_id != session_id:
            raise AuthorizationError("tool context changed during request")
        return self.authorization.authorize(caller.principal, source, self.clock.now())


def _extract_content(body: bytes, content_type: str, target: NormalizedURL, scope, now):
    decoded = body.decode("utf-8", errors="replace")
    if content_type == "text/plain":
        return " ".join(decoded.split()), None, ()
    parser = _PageParser()
    parser.feed(decoded)
    title = " ".join(parser.title_parts).strip() or None
    text = " ".join(parser.text_parts)
    links = []
    for raw_link in parser.links:
        try:
            candidate = normalize_url(urljoin(target.url, raw_link))
            scope.authorize_url(candidate, now)
        except (TargetRejected, AuthorizationError, ValueError):
            continue
        if candidate.url not in links:
            links.append(candidate.url)
    return text, title, tuple(links[:100])


def _url_digest(target: NormalizedURL) -> str:
    return hashlib.sha256(target.url.encode("utf-8")).hexdigest()


def _trace_target(target: NormalizedURL) -> dict:
    return {
        "origin": target.origin,
        "path_sha256": hashlib.sha256(target.path.encode("utf-8")).hexdigest(),
        "url_sha256": _url_digest(target),
    }
