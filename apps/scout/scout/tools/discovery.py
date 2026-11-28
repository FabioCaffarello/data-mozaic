"""Policy-mediated HTTP discovery; callers cannot provide identity or budgets."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

import aiohttp

from ..discovery_content import SUPPORTED_DISCOVERY_CONTENT_TYPES
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


class DiscoveryNetworkError(DiscoveryError):
    pass


class DiscoveryPolicyError(DiscoveryError):
    pass


class DiscoveryStructureError(DiscoveryError):
    pass


class DiscoveryCustodyError(DiscoveryError):
    pass


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:password|passwd|secret|access[_-]?token|api[_-]?key)\b"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s,;\"'<>}\]]+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]+=*")


def _redact_page_text(value: str) -> str:
    value = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
    return _BEARER_VALUE.sub(r"\1[REDACTED]", value)


@dataclass(frozen=True)
class DiscoveryResult:
    evidence: EvidenceReference
    status: int
    content_type: str
    text: str
    title: str | None
    links: tuple[str, ...]
    truncated: bool
    body: bytes
    elapsed_seconds: int
    request_count: int
    final_url: str
    completed_at: datetime
    extraction_version: str


TEXT_EXTRACTION_VERSION = "scout-text-v1"
JSON_TEXT_EXTRACTION_VERSION = "scout-json-text-v1"
OPAQUE_EXTRACTION_VERSION = "scout-opaque-v1"


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
        *,
        preserve_evidence: bool = True,
    ):
        self.context = context
        self.authorization = authorization
        self.budget = budget
        self.evidence = evidence
        self.trace = trace
        self.clock = clock
        self.transport = transport
        self.preserve_evidence = preserve_evidence

    async def fetch(self, source_id: str, revision: str, url: str) -> DiscoveryResult:
        started_at = self.clock.now()
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
            # PostgreSQL commits the durable start timestamp after the host's
            # preflight clock read. A small positive database lead is zero
            # elapsed time; the ledger itself remains authoritative for expiry.
            elapsed = max(0.0, elapsed)
            remaining = min(
                scope.max_duration_seconds - elapsed,
                scope.expires_at.timestamp() - now.timestamp(),
            )
            if remaining <= 0:
                raise AuthorizationError("navigation time budget expired")
            self.trace.append(
                caller.session_id,
                "http.request.authorized",
                _trace_target(current)
                | {
                    "method": "GET",
                    "hop": hop,
                    "policy_revision_sha256": scope.policy_revision_sha256,
                },
                now,
            )
            try:
                response = await self.transport.get(
                    current.url, scope.max_response_bytes, remaining
                )
            except aiohttp.ClientHttpProxyError as error:
                if error.status in {403, 407}:
                    raise DiscoveryPolicyError(
                        "egress broker denied destination"
                    ) from error
                raise DiscoveryNetworkError("egress broker request failed") from error
            except TargetRejected as error:
                raise DiscoveryStructureError(str(error)) from error
            except Exception as error:
                self.trace.append(
                    caller.session_id,
                    "http.request.failed",
                    _trace_target(current) | {"failure_type": type(error).__name__},
                    self.clock.now(),
                )
                raise DiscoveryNetworkError("HTTP request failed") from error
            if len(response.body) > scope.max_response_bytes:
                raise DiscoveryStructureError(
                    "transport exceeded approved response byte limit"
                )
            if response.headers.get("content-encoding", "identity").lower() not in (
                "",
                "identity",
            ):
                raise DiscoveryStructureError("compressed responses are not accepted")
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
                redirect_evidence = (
                    self._store_evidence(
                        caller.session_id,
                        source,
                        current,
                        response,
                        response.headers.get("content-type", "application/octet-stream")
                        .split(";", 1)[0]
                        .lower(),
                    )
                    if self.preserve_evidence
                    else None
                )
                self.trace.append(
                    caller.session_id,
                    "http.redirect.observed",
                    _trace_target(current)
                    | {
                        "status": response.status,
                        "evidence_id": redirect_evidence.evidence_id
                        if redirect_evidence
                        else None,
                        "size_bytes": redirect_evidence.size_bytes
                        if redirect_evidence
                        else len(response.body),
                    },
                    after_response,
                )
                location = response.headers.get("location")
                if not location:
                    raise DiscoveryStructureError("redirect response has no location")
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
                caller.session_id,
                source,
                current,
                response,
                scope,
                started_at,
                hop + 1,
            )
        raise AuthorizationError("redirect count exceeds the approved request budget")

    @staticmethod
    def normalize_target(url: str):
        return normalize_url(url)

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
        self, session_id, source, target, response, scope, started_at, request_count
    ) -> DiscoveryResult:
        now = self.clock.now()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in SUPPORTED_DISCOVERY_CONTENT_TYPES:
            raise DiscoveryStructureError(
                "response media type is not supported for governed discovery"
            )
        evidence = (
            self._store_evidence(session_id, source, target, response, content_type)
            if self.preserve_evidence
            else EvidenceReference(
                "",
                hashlib.sha256(response.body).hexdigest(),
                len(response.body),
                source,
            )
        )
        current_scope = scope.intersect(self._scope_for(session_id, source))
        if content_type == "application/pdf":
            # Discovery records only the declaration and custody reference.
            # PDF bytes stay opaque; no parser or text extractor is invoked.
            text, title, links, truncated = "", None, (), False
            extraction_version = OPAQUE_EXTRACTION_VERSION
        else:
            raw_text, title, links = _extract_content(
                response.body, content_type, target, current_scope, now
            )
            text, truncated = _bounded_redacted_text(raw_text)
            extraction_version = (
                JSON_TEXT_EXTRACTION_VERSION
                if content_type == "application/json"
                else TEXT_EXTRACTION_VERSION
            )
        if title:
            title = _redact_page_text(title)[:512]
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
            evidence,
            response.status,
            content_type,
            text,
            title,
            links,
            truncated,
            response.body,
            max(0, int((now - started_at).total_seconds())),
            request_count,
            target.url,
            now,
            extraction_version,
        )

    @staticmethod
    def reconstruct_text(
        body: bytes, content_type: str, extraction_version: str
    ) -> tuple[str, bool]:
        return reconstruct_text_projection(body, content_type, extraction_version)

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
            raise DiscoveryCustodyError(
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
    if content_type in {"text/plain", "application/json"}:
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


def reconstruct_text_projection(
    body: bytes, content_type: str, extraction_version: str
) -> tuple[str, bool]:
    """Rebuild the stable, redacted text response from BC06 bytes."""
    if extraction_version == OPAQUE_EXTRACTION_VERSION:
        if content_type != "application/pdf":
            raise DiscoveryStructureError("opaque replay media type changed")
        return "", False
    if extraction_version not in {
        TEXT_EXTRACTION_VERSION,
        JSON_TEXT_EXTRACTION_VERSION,
    }:
        raise DiscoveryStructureError("unsupported HTTP text extraction version")
    decoded = body.decode("utf-8", errors="replace")
    if content_type in {"text/plain", "application/json"}:
        text = " ".join(decoded.split())
    elif content_type == "text/html":
        parser = _PageParser()
        parser.feed(decoded)
        text = " ".join(parser.text_parts)
    else:
        raise DiscoveryStructureError("unsupported content type for text replay")
    return _bounded_redacted_text(text)


def _bounded_redacted_text(text: str) -> tuple[str, bool]:
    truncated = len(text) > 16_000
    return _redact_page_text(text[:16_000]), truncated


def _url_digest(target: NormalizedURL) -> str:
    return hashlib.sha256(target.url.encode("utf-8")).hexdigest()


def _trace_target(target: NormalizedURL) -> dict:
    return {
        "origin": target.origin,
        "path_sha256": hashlib.sha256(target.path.encode("utf-8")).hexdigest(),
        "url_sha256": _url_digest(target),
    }
