"""Build, validate and replay draft CapturePlaybooks without network access."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

_SCHEMAS = files("collector_engineering").joinpath("schemas")
SCHEMA = json.loads(_SCHEMAS.joinpath("CapturePlaybook.schema.json").read_text())
PROPOSAL_SCHEMA = json.loads(
    _SCHEMAS.joinpath("PlaybookProposal.schema.json").read_text()
)
TRANSCRIPT_SCHEMA = json.loads(
    _SCHEMAS.joinpath("ScoutSessionTranscript.schema.json").read_text()
)


class PlaybookError(ValueError):
    """Input cannot safely produce or validate a playbook draft."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping_without_duplicate_keys(loader: _UniqueKeyLoader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise PlaybookError("YAML mapping keys must be strings")
        if key in mapping:
            raise PlaybookError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _mapping_without_duplicate_keys,
)


def _parse_yaml(text: str) -> dict:
    if len(text.encode("utf-8")) > 1_000_000:
        raise PlaybookError("playbook exceeds the 1 MB draft limit")
    try:
        if any(
            isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
            for token in yaml.scan(text)
        ):
            raise PlaybookError("YAML anchors and aliases are not supported")
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, UnicodeError) as error:
        raise PlaybookError(f"invalid YAML: {error}") from error
    if not isinstance(value, dict):
        raise PlaybookError("playbook root must be a mapping")
    return value


def _normalized_origin(value: str) -> str:
    if not isinstance(value, str):
        raise PlaybookError("source origin must be text")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except (TypeError, ValueError) as error:
        raise PlaybookError("invalid source origin") from error
    if (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or any(char in value for char in "\\\r\n\t")
    ):
        raise PlaybookError("source origin must be a bare HTTPS origin")
    ascii_host = _ascii_hostname(host)
    return urlunsplit(("https", ascii_host, "", "", ""))


def _ascii_hostname(host: str) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PlaybookError(
            "source origin must use a registered hostname, not an IP literal"
        )
    if host.endswith(".") or "%" in host:
        raise PlaybookError("source hostname has a non-canonical form")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise PlaybookError("invalid internationalized host") from error
    labels = ascii_host.split(".")
    if (
        len(ascii_host) > 253
        or len(labels) < 2
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in labels
        )
    ):
        raise PlaybookError("source hostname is not a canonical registered hostname")
    return ascii_host


def _within_path(path: str, prefix: str) -> bool:
    normalized = prefix.rstrip("/") or "/"
    return normalized == "/" or path == normalized or path.startswith(normalized + "/")


def _scoped_url(value: str, origin: str, prefixes: list[str]) -> str:
    if not isinstance(value, str):
        raise PlaybookError("URL must be text")
    try:
        parts = urlsplit(value)
        port = parts.port
        host = parts.hostname
    except (TypeError, ValueError) as error:
        raise PlaybookError(f"invalid URL: {value}") from error
    if (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.query
        or parts.fragment
        or any(char in value for char in "\\\r\n\t")
        or _unsafe_path(parts.path)
    ):
        raise PlaybookError("URL is ambiguous or outside supported HTTPS rules")
    canonical_host = _ascii_hostname(host)
    normalized_path = unicodedata.normalize("NFC", parts.path or "/")
    normalized = urlunsplit(("https", canonical_host, normalized_path, "", ""))
    candidate_origin = urlunsplit(("https", canonical_host, "", "", ""))
    if candidate_origin != origin:
        raise PlaybookError("URL origin differs from the approved source origin")
    if not any(_within_path(normalized_path, prefix) for prefix in prefixes):
        raise PlaybookError("URL path is outside the approved source prefixes")
    return normalized


def _unsafe_path(path: str) -> bool:
    segments = path.split("/")
    return (
        "\\" in path
        or "%" in path
        or "//" in path
        or any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in path)
        or any(segment in {".", ".."} for segment in segments)
    )


def _semantic_errors(playbook: dict) -> list[str]:
    errors: list[str] = []
    source = playbook.get("source", {})
    try:
        origin = _normalized_origin(source.get("origin", ""))
    except PlaybookError as error:
        return [str(error)]
    prefixes = source.get("path_prefixes", [])
    if any(
        not isinstance(item, str)
        or not item.startswith("/")
        or _unsafe_path(item)
        or item != unicodedata.normalize("NFC", item)
        for item in prefixes
    ):
        errors.append("path prefixes must be absolute URL paths")
    step_ids: set[str] = set()
    seen_fetch = False
    seen_discovery = False
    action_caps = set()
    for step in playbook.get("steps", []):
        step_id = step.get("id")
        action = step.get("action")
        if step_id in step_ids:
            errors.append(f"duplicate step id: {step_id}")
        if action == "http.fetch":
            try:
                _scoped_url(step.get("url", ""), origin, prefixes)
            except (PlaybookError, UnicodeError) as error:
                errors.append(f"step {step_id}: {error}")
            if seen_fetch:
                errors.append("draft v1 supports exactly one declared starting fetch")
            seen_fetch = True
            action_caps.add("http.fetch")
        elif action == "links.discover":
            if not seen_fetch or step.get("from_step") not in step_ids:
                errors.append(f"step {step_id}: links must follow an earlier fetch")
            seen_discovery = True
            action_caps.add("links.discover")
        elif action == "pagination.follow":
            if not seen_discovery or step.get("from_step") not in step_ids:
                errors.append(f"step {step_id}: pagination must follow link discovery")
            if step.get("max_pages", 0) > playbook.get("budgets", {}).get(
                "max_pages", 0
            ):
                errors.append(
                    f"step {step_id}: pagination exceeds approved page budget"
                )
            action_caps.add("pagination.bounded")
        elif action == "output.raw":
            if step.get("from_step") not in step_ids:
                errors.append(
                    f"step {step_id}: raw output must reference an earlier step"
                )
            action_caps.add("output.raw")
        step_ids.add(step_id)
    if not seen_fetch:
        errors.append("draft must declare one HTTP fetch step")
    budgets = playbook.get("budgets", {})
    paginated_pages = max(
        (
            step.get("max_pages", 0)
            for step in playbook.get("steps", [])
            if step.get("action") == "pagination.follow"
        ),
        default=0,
    )
    minimum_requests = 1 + paginated_pages
    if budgets.get("max_requests", 0) < minimum_requests:
        errors.append("request budget cannot satisfy declared steps")
    if set(playbook.get("capabilities", [])) != action_caps:
        errors.append("capabilities must exactly match the declared actions")
    findings = playbook.get("findings", [])
    if not findings:
        errors.append("draft must retain at least one evidence-backed finding")
    if any(not finding.get("evidence_refs") for finding in findings):
        errors.append("every finding must retain at least one evidence reference")
    return errors


def validate_playbook(value: dict) -> list[str]:
    validator = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
    errors = [error.message for error in validator.iter_errors(value)]
    if not errors:
        errors.extend(_semantic_errors(value))
    return sorted(set(errors))


def load_playbook(path: Path) -> dict:
    return _parse_yaml(path.read_text(encoding="utf-8"))


def validate_file(path: Path) -> list[str]:
    return validate_playbook(load_playbook(path))


def build_draft(transcript: dict) -> dict:
    """Project a host/tool transcript into a separate BC15 draft artifact."""
    if not isinstance(transcript, dict):
        raise PlaybookError("transcript root must be a JSON object")
    try:
        Draft202012Validator(
            TRANSCRIPT_SCHEMA, format_checker=FormatChecker()
        ).validate(transcript)
    except ValidationError as error:
        raise PlaybookError(
            "input does not satisfy Scout transcript draft v1"
        ) from error
    approval = transcript.get("approved_scope", {})
    proposal = transcript.get("proposal", {})
    if not isinstance(approval, dict) or not isinstance(proposal, dict):
        raise PlaybookError("approved scope and proposal must be JSON objects")
    objective = transcript.get("objective")
    pages = transcript.get("pages", [])
    if not isinstance(pages, list):
        raise PlaybookError("transcript pages must be a list")
    if any(not isinstance(page, dict) for page in pages):
        raise PlaybookError("transcript page entries must be JSON objects")
    transcript_findings = transcript.get("findings", [])
    if not isinstance(transcript_findings, list) or any(
        not isinstance(finding, dict) for finding in transcript_findings
    ):
        raise PlaybookError("transcript findings must be a list of JSON objects")
    origin = _normalized_origin(approval.get("origin", ""))
    prefixes = approval.get("path_prefixes", [])
    if not isinstance(prefixes, list) or not prefixes:
        raise PlaybookError("approved path prefixes are required")
    if any(not isinstance(prefix, str) for prefix in prefixes):
        raise PlaybookError("approved path prefixes must be strings")
    prefixes = [unicodedata.normalize("NFC", prefix) for prefix in prefixes]
    approval_record = transcript.get("approval_record", {})
    if not isinstance(approval_record, dict) or not approval_record.get("approval_id"):
        raise PlaybookError("transcript lacks an explicit host approval receipt")
    if (
        approval_record.get("source_id") != approval.get("source_id")
        or approval_record.get("revision") != approval.get("revision")
        or approval_record.get("origin") != approval.get("origin")
        or approval_record.get("path_prefixes") != approval.get("path_prefixes")
        or approval_record.get("budgets") != approval.get("budgets")
    ):
        raise PlaybookError("approval receipt differs from the approved scope snapshot")
    if not objective or not pages or not proposal.get("proposal_id"):
        raise PlaybookError("transcript needs objective, pages, and a BC14 proposal")
    proposal_errors = list(Draft202012Validator(PROPOSAL_SCHEMA).iter_errors(proposal))
    if proposal_errors:
        raise PlaybookError("BC14 proposal is invalid under Scout v1 contract")
    if proposal.get("source") != {
        "source_id": approval.get("source_id"),
        "revision": approval.get("revision"),
    }:
        raise PlaybookError("proposal source does not match approved source revision")
    finding_map = {item.get("finding_id"): item for item in transcript_findings}
    if len(finding_map) != len(transcript_findings):
        raise PlaybookError("transcript contains duplicate finding IDs")
    proposal_findings = set(proposal.get("finding_ids", []))
    evidence_ids = set(proposal.get("evidence_manifest_ids", []))
    evidence_by_artifact = {
        item.get("artifact_id"): item
        for item in transcript.get("evidence", [])
        if isinstance(item, dict)
    }
    if len(evidence_by_artifact) != len(transcript.get("evidence", [])):
        raise PlaybookError("transcript has duplicate or malformed evidence metadata")
    evidence_manifests = {
        item.get("manifest_id") for item in evidence_by_artifact.values()
    }
    if not evidence_ids <= evidence_manifests:
        raise PlaybookError("proposal references missing transcript evidence metadata")
    findings = []
    for finding_id in sorted(proposal_findings):
        finding = finding_map.get(finding_id)
        if finding is None:
            raise PlaybookError(f"proposal references missing finding {finding_id}")
        evidence_refs = set(finding.get("evidence_refs", []))
        if not evidence_refs or not evidence_refs <= evidence_ids:
            raise PlaybookError(f"finding {finding_id} has unsupported evidence refs")
        classification = finding.get("classification")
        if classification not in {
            "observed",
            "hypothesized",
            "independently_verified",
        }:
            raise PlaybookError(f"finding {finding_id} has invalid classification")
        findings.append(
            {
                "finding_id": finding_id,
                "classification": classification,
                "evidence_refs": sorted(evidence_refs),
            }
        )
    first_url = _scoped_url(pages[0].get("url", ""), origin, prefixes)
    page_urls = set()
    for page in pages:
        _scoped_url(page.get("url", ""), origin, prefixes)
        metadata = evidence_by_artifact.get(page.get("artifact_id"))
        if (
            page.get("evidence_ref") not in evidence_ids
            or metadata is None
            or metadata.get("manifest_id") != page.get("evidence_ref")
            or metadata.get("sha256") != page.get("sha256")
            or metadata.get("size_bytes") != page.get("size_bytes")
        ):
            raise PlaybookError("page evidence reference is absent from the proposal")
        if page["url"] in page_urls:
            raise PlaybookError("transcript contains a duplicate page URL")
        page_urls.add(page["url"])
    budgets = approval.get("budgets", {})
    max_requests = budgets.get("max_requests", 0)
    max_pages = budgets.get("max_pages", 0)
    max_response_bytes = budgets.get("max_response_bytes", 0)
    if not all(
        isinstance(value, int) and value > 0
        for value in (max_requests, max_pages, max_response_bytes)
    ):
        raise PlaybookError("approved request, page, and byte budgets are required")
    steps = [
        {"id": "fetch_start", "action": "http.fetch", "url": first_url},
    ]
    capabilities = ["http.fetch", "output.raw"]
    has_discovered_links = any(page.get("discovered_links") for page in pages)
    if has_discovered_links:
        steps.append(
            {
                "id": "discover_links",
                "action": "links.discover",
                "from_step": "fetch_start",
                "max_links": min(100, max(1, max_requests - 1)),
            }
        )
        capabilities.append("links.discover")
    if len(pages) > 1:
        if not has_discovered_links:
            raise PlaybookError("multiple captured pages lack observed link discovery")
        steps.append(
            {
                "id": "follow_pages",
                "action": "pagination.follow",
                "from_step": "discover_links",
                "max_pages": min(max_pages, len(pages) - 1),
            }
        )
        capabilities.append("pagination.bounded")
    steps.append(
        {
            "id": "raw_output",
            "action": "output.raw",
            "from_step": "follow_pages" if len(pages) > 1 else "fetch_start",
        }
    )
    draft = {
        "dsl_version": "capture-playbook-draft/v1",
        "playbook_id": f"draft-{proposal['proposal_id']}",
        "status": "draft",
        "source": {
            "source_id": approval["source_id"],
            "revision": approval["revision"],
            "origin": origin,
            "path_prefixes": prefixes,
        },
        "objective": objective,
        "proposal_ref": proposal["proposal_id"],
        "findings": findings,
        "capabilities": capabilities,
        "budgets": {
            "max_requests": max_requests,
            "max_pages": max_pages,
            "max_response_bytes": max_response_bytes,
        },
        "steps": steps,
    }
    errors = validate_playbook(draft)
    if errors:
        raise PlaybookError("generated draft is invalid: " + "; ".join(errors))
    return draft


def dump_yaml(value: dict) -> str:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href and href not in self.links:
            self.links.append(href)


def replay(playbook: dict, fixture_root: Path) -> dict:
    """Replay declared steps using captured bodies only; this function has no transport."""
    errors = validate_playbook(playbook)
    if errors:
        raise PlaybookError("cannot replay invalid playbook: " + "; ".join(errors))
    try:
        index = json.loads((fixture_root / "index.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PlaybookError(f"cannot read fixture index: {error}") from error
    if not isinstance(index, dict) or not isinstance(index.get("pages"), list):
        raise PlaybookError("fixture index must contain a pages list")
    origin = playbook["source"]["origin"]
    prefixes = playbook["source"]["path_prefixes"]
    fixture_pages = {}
    for item in index.get("pages", []):
        if not isinstance(item, dict) or not isinstance(item.get("body_file"), str):
            raise PlaybookError("fixture entries must include a body file path")
        url = _scoped_url(item.get("url", ""), origin, prefixes)
        body_path = (fixture_root / item.get("body_file", "")).resolve()
        if fixture_root.resolve() not in body_path.parents:
            raise PlaybookError("fixture body path escapes fixture root")
        try:
            body = body_path.read_bytes()
        except OSError as error:
            raise PlaybookError(f"fixture body unavailable for {url}") from error
        if hashlib.sha256(body).hexdigest() != item.get("sha256"):
            raise PlaybookError(f"fixture body digest mismatch for {url}")
        if url in fixture_pages:
            raise PlaybookError(f"duplicate fixture URL: {url}")
        fixture_pages[url] = {**item, "body": body}
    page_budget = playbook["budgets"]["max_pages"]
    request_budget = playbook["budgets"]["max_requests"]
    byte_budget = playbook["budgets"]["max_response_bytes"]
    discovered_queue = []
    visited: set[str] = set()
    results = []
    failures = []

    def discover_from(url: str) -> list[str]:
        page = fixture_pages.get(url)
        if not page:
            return []
        parser = _LinkParser()
        parser.feed(page["body"].decode("utf-8", errors="replace"))
        found = []
        for href in parser.links:
            resolved = urljoin(url, href)
            try:
                candidate = _scoped_url(resolved, origin, prefixes)
            except (PlaybookError, UnicodeError):
                continue
            if candidate not in visited and candidate not in found:
                found.append(candidate)
        return found

    def fetch_fixture(url: str, step_id: str) -> bool:
        if url in visited:
            return True
        if len(visited) >= request_budget or len(visited) >= page_budget:
            failures.append(
                {
                    "step_id": step_id,
                    "url": url,
                    "reason": "approved page/request budget exhausted",
                }
            )
            return False
        page = fixture_pages.get(url)
        if page is None:
            failures.append(
                {
                    "step_id": step_id,
                    "url": url,
                    "reason": "no captured fixture for discovered page",
                }
            )
            return False
        body = page["body"]
        if len(body) > byte_budget:
            failures.append(
                {
                    "step_id": step_id,
                    "url": url,
                    "reason": "captured body exceeds byte budget",
                }
            )
            return False
        if not 200 <= page.get("status", 0) < 300:
            failures.append(
                {
                    "step_id": step_id,
                    "url": url,
                    "reason": "captured response status is not successful",
                }
            )
            return False
        evidence_refs = {
            evidence
            for finding in playbook["findings"]
            for evidence in finding["evidence_refs"]
        }
        if page.get("evidence_ref") not in evidence_refs:
            failures.append(
                {
                    "step_id": step_id,
                    "url": url,
                    "reason": "fixture evidence is not referenced by a finding",
                }
            )
            return False
        visited.add(url)
        results.append(
            {
                "url": url,
                "status": page["status"],
                "evidence_ref": page["evidence_ref"],
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
        return True

    for step in playbook["steps"]:
        action = step["action"]
        if action == "http.fetch":
            fetch_fixture(step["url"], step["id"])
        elif action == "pagination.follow":
            queue = list(discovered_queue[: step["max_pages"]])
            followed = 0
            while queue and followed < step["max_pages"]:
                url = queue.pop(0)
                if not fetch_fixture(url, step["id"]):
                    continue
                followed += 1
                for candidate in discover_from(url):
                    if candidate not in queue and candidate not in discovered_queue:
                        queue.append(candidate)
                        discovered_queue.append(candidate)
        elif action == "links.discover":
            discovered = []
            for url in list(visited):
                discovered.extend(
                    candidate
                    for candidate in discover_from(url)
                    if candidate not in discovered
                )
            discovered_queue = discovered[: step["max_links"]]
    return {
        "playbook_id": playbook["playbook_id"],
        "state": "failed" if failures else "passed",
        "replayed_pages": results,
        "failures": failures,
    }
