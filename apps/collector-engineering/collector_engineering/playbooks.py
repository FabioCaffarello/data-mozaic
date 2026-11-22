"""Build, validate and replay draft CapturePlaybooks without network access."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import unicodedata
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .syntax import PlaybookError, UniqueKeyLoader

_SCHEMAS = files("collector_engineering").joinpath("schemas")
SCHEMA = json.loads(_SCHEMAS.joinpath("CapturePlaybook.schema.json").read_text())
PROPOSAL_SCHEMA = json.loads(
    _SCHEMAS.joinpath("PlaybookProposal.schema.json").read_text()
)
TRANSCRIPT_SCHEMAS = {
    "scout-transcript-draft/v1": json.loads(
        _SCHEMAS.joinpath("ScoutSessionTranscript.v1.schema.json").read_text()
    ),
    "scout-transcript-draft/v2": json.loads(
        _SCHEMAS.joinpath("ScoutSessionTranscript.v2.schema.json").read_text()
    ),
}


try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - platform-specific fail-closed path
    _fcntl = None


_UniqueKeyLoader = UniqueKeyLoader


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
    actions = []
    seen_fetch = False
    seen_discovery = False
    action_caps = set()
    for step in playbook.get("steps", []):
        step_id = step.get("id")
        action = step.get("action")
        actions.append(action)
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
    ordered_steps = playbook.get("steps", [])
    for index, step in enumerate(ordered_steps):
        action = step.get("action")
        if action == "links.discover" and (
            index == 0
            or ordered_steps[index - 1].get("action") != "http.fetch"
            or step.get("from_step") != ordered_steps[index - 1].get("id")
        ):
            errors.append(f"step {step.get('id')}: link discovery is unreachable")
        if action == "pagination.follow" and (
            index == 0
            or ordered_steps[index - 1].get("action") != "links.discover"
            or step.get("from_step") != ordered_steps[index - 1].get("id")
        ):
            errors.append(f"step {step.get('id')}: pagination is unreachable")
        if action == "output.raw" and (
            index != len(ordered_steps) - 1
            or index == 0
            or step.get("from_step") != ordered_steps[index - 1].get("id")
        ):
            errors.append(f"step {step.get('id')}: raw output is unreachable")
    if not seen_fetch:
        errors.append("draft must declare one HTTP fetch step")
    expected_actions = ["http.fetch"]
    if any(action == "pagination.follow" for action in actions):
        expected_actions.extend(["links.discover", "pagination.follow"])
    expected_actions.append("output.raw")
    if actions != expected_actions:
        errors.append(
            "draft operations must be reachable fetch, optional discovery/follow, then raw output"
        )
    if len(actions) != len(set(actions)):
        errors.append("draft v1 permits each operation at most once")
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
    transcript_version = transcript.get("transcript_schema")
    transcript_schema = TRANSCRIPT_SCHEMAS.get(transcript_version)
    if transcript_schema is None:
        raise PlaybookError("unsupported Scout transcript draft version")
    try:
        Draft202012Validator(
            transcript_schema, format_checker=FormatChecker()
        ).validate(transcript)
    except ValidationError as error:
        raise PlaybookError(
            f"input does not satisfy Scout transcript {transcript_version}"
        ) from error
    if transcript_version == "scout-transcript-draft/v1":
        raise PlaybookError(
            "Scout transcript draft v1 has no explicit starting URL or exploration order; re-export with BC14 transcript draft v2"
        )
    if transcript.get("navigation", {}).get("sequence_complete") is not True:
        raise PlaybookError("incomplete transcript cannot produce an acquisition draft")
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
    navigation = transcript["navigation"]
    ordered_pages = sorted(pages, key=lambda page: page["sequence"])
    if (
        [page["sequence"] for page in ordered_pages]
        != list(range(1, navigation["observation_count"] + 1))
        or len({page["observation_id"] for page in ordered_pages}) != len(ordered_pages)
        or not ordered_pages
        or ordered_pages[0]["requested_url"] != navigation["start_requested_url"]
        or ordered_pages[0]["url"] != navigation["start_effective_url"]
    ):
        raise PlaybookError(
            "transcript starting observation or sequence is inconsistent"
        )
    known_observations = {page["observation_id"] for page in ordered_pages}
    for page in ordered_pages:
        parents = page["discovered_from_observation_ids"]
        expected_parents = {
            parent["observation_id"]
            for parent in ordered_pages
            if parent["sequence"] < page["sequence"]
            and page["requested_url"] in parent["discovered_links"]
        }
        if (
            len(parents) != len(set(parents))
            or not set(parents) <= known_observations
            or set(parents) != expected_parents
        ):
            raise PlaybookError("transcript evidence has invalid discovery relations")
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
    supported_proposal_capabilities = {
        "http-read",
        "http.fetch",
        "links.discover",
        "pagination.bounded",
        "output.raw",
    }
    unsupported = sorted(
        set(proposal.get("required_capabilities", [])) - supported_proposal_capabilities
    )
    if unsupported:
        raise PlaybookError(
            "proposal requires unsupported draft/v1 capabilities: "
            + ", ".join(unsupported)
        )
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
    first_url = _scoped_url(navigation.get("start_requested_url", ""), origin, prefixes)
    start_page = next(
        (
            page
            for page in pages
            if page.get("sequence") == 1
            and page.get("requested_url") == first_url
            and page.get("url") == navigation.get("start_effective_url")
        ),
        None,
    )
    if start_page is None:
        raise PlaybookError(
            "transcript does not contain its explicit starting observation"
        )
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
    reachable = {start_page["url"]}
    while True:
        before = len(reachable)
        for page in ordered_pages:
            if page["url"] not in reachable:
                continue
            for candidate in ordered_pages:
                if candidate["requested_url"] in page["discovered_links"]:
                    reachable.add(candidate["url"])
        if len(reachable) == before:
            break
    if any(page["url"] not in reachable for page in ordered_pages):
        raise PlaybookError(
            "observed pages are not reachable from the starting page through evidenced links"
        )
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
    required_follow_pages = len(ordered_pages) - 1
    if required_follow_pages:
        steps.append(
            {
                "id": "discover_links",
                "action": "links.discover",
                "from_step": "fetch_start",
                "max_links": min(100, max(1, max_requests - 1)),
            }
        )
        capabilities.append("links.discover")
    if required_follow_pages:
        steps.append(
            {
                "id": "follow_pages",
                "action": "pagination.follow",
                "from_step": "discover_links",
                "max_pages": required_follow_pages,
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


def _require_descriptor_output_support() -> None:
    required_flags = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
    required_functions = (os.open, os.mkdir, os.link, os.unlink, os.stat)
    if (
        _fcntl is None
        or any(not hasattr(os, name) for name in required_flags)
        or any(function not in os.supports_dir_fd for function in required_functions)
        or os.link not in os.supports_follow_symlinks
    ):
        raise PlaybookError(
            "secure raw output requires POSIX dir_fd, O_NOFOLLOW, O_DIRECTORY, "
            "hard-link publication, and flock support"
        )


def _check_private_directory(descriptor: int, label: str) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or metadata.st_mode & 0o700 != 0o700
    ):
        raise PlaybookError(f"{label} must be an owner-only directory")
    return metadata


def _check_output_parent_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    shared_sticky = bool(metadata.st_mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or (metadata.st_mode & 0o022 and not shared_sticky)
    ):
        raise PlaybookError(
            "raw output ancestors must be root/user-owned and not group/world writable"
        )


def _open_child_directory(parent: int, name: str, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        os.fsync(parent)
        return os.open(name, flags, dir_fd=parent)


def _open_private_output_root(path: Path) -> tuple[int, int, str]:
    if ".." in path.parts:
        raise PlaybookError("raw output root cannot contain parent traversal")
    absolute = path if path.is_absolute() else Path.cwd() / path
    # macOS exposes the system temporary roots through root-owned /tmp and /var
    # symlink aliases. Normalize only those OS aliases; never resolve an
    # application-selected path component that could be replaced by its caller.
    if sys.platform == "darwin" and len(absolute.parts) > 1:
        if absolute.parts[1] == "tmp":
            absolute = Path("/private/tmp", *absolute.parts[2:])
        elif absolute.parts[1] == "var":
            absolute = Path("/private/var", *absolute.parts[2:])
    components = absolute.parts[1:]
    if not components:
        raise PlaybookError("filesystem root cannot be used as raw output root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/", flags)
    root_parent = None
    try:
        _check_output_parent_directory(descriptor)
        for index, component in enumerate(components):
            if index == len(components) - 1:
                root_parent = os.dup(descriptor)
            child = _open_child_directory(descriptor, component, create=True)
            try:
                if index == len(components) - 1:
                    _check_private_directory(child, "raw output root")
                else:
                    _check_output_parent_directory(child)
                descriptor_to_close = descriptor
                descriptor = child
                child = -1
                os.close(descriptor_to_close)
                if index == len(components) - 1:
                    return root_parent, descriptor, component
            finally:
                if child >= 0:
                    os.close(child)
    except BaseException:
        os.close(descriptor)
        if root_parent is not None:
            os.close(root_parent)
        raise
    raise PlaybookError("raw output root path could not be opened")


def _read_private_file_at(
    directory: int, name: str, *, expected_size: int | None = None
) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=directory)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
            or (expected_size is not None and metadata.st_size != expected_size)
        ):
            raise PlaybookError("raw output file has unsafe type, mode, or size")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if expected_size is not None and total > expected_size:
                raise PlaybookError("raw output grew while it was being verified")
        content = b"".join(chunks)
        if expected_size is not None and len(content) != expected_size:
            raise PlaybookError("raw output changed while it was being verified")
        return content
    finally:
        os.close(descriptor)


class _PrivateRawOutput:
    """Descriptor-relative, private and idempotent BC15 replay publication."""

    def __init__(self, output_root: Path):
        self.output_root = output_root
        self.root_parent_fd: int | None = None
        self.root_fd: int | None = None
        self.root_name: str | None = None
        self.raw_fd: int | None = None
        self.lock_fd: int | None = None

    def __enter__(self):
        _require_descriptor_output_support()
        try:
            self.root_parent_fd, self.root_fd, self.root_name = (
                _open_private_output_root(self.output_root)
            )
            self.lock_fd = self._open_lock()
            _fcntl.flock(self.lock_fd, _fcntl.LOCK_EX)
            self._verify_root_name()
            self.raw_fd = _open_child_directory(self.root_fd, "raw", create=True)
            _check_private_directory(self.raw_fd, "raw output directory")
            self._verify_raw_name()
            self._remove_stale_pending_files()
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, PlaybookError):
                raise
            if isinstance(error, (OSError, ValueError)):
                raise PlaybookError(
                    "cannot establish a secure raw output directory"
                ) from error
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self) -> None:
        first_error = None
        if self.raw_fd is not None:
            descriptor = self.raw_fd
            self.raw_fd = None
            try:
                os.close(descriptor)
            except OSError as error:  # pragma: no cover - close failure is platform I/O
                first_error = error
        if self.lock_fd is not None:
            descriptor = self.lock_fd
            self.lock_fd = None
            try:
                if _fcntl is not None:
                    _fcntl.flock(descriptor, _fcntl.LOCK_UN)
            except OSError as error:  # pragma: no cover - close failure is platform I/O
                first_error = first_error or error
            try:
                os.close(descriptor)
            except OSError as error:  # pragma: no cover - close failure is platform I/O
                first_error = first_error or error
        if self.root_fd is not None:
            descriptor = self.root_fd
            self.root_fd = None
            try:
                os.close(descriptor)
            except OSError as error:  # pragma: no cover - close failure is platform I/O
                first_error = first_error or error
        if self.root_parent_fd is not None:
            descriptor = self.root_parent_fd
            self.root_parent_fd = None
            try:
                os.close(descriptor)
            except OSError as error:  # pragma: no cover - close failure is platform I/O
                first_error = first_error or error
        if first_error is not None:
            raise PlaybookError(
                "could not close secure raw output descriptors"
            ) from first_error

    def _open_lock(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(".replay.lock", flags, 0o600, dir_fd=self.root_fd)
        except FileExistsError:
            descriptor = os.open(
                ".replay.lock",
                os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self.root_fd,
            )
        try:
            metadata = os.fstat(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_mode & 0o600 != 0o600
            or metadata.st_nlink != 1
        ):
            os.close(descriptor)
            raise PlaybookError("raw output lock has unsafe ownership, mode, or type")
        return descriptor

    def _verify_root_name(self) -> None:
        metadata = os.stat(
            self.root_name, dir_fd=self.root_parent_fd, follow_symlinks=False
        )
        opened = os.fstat(self.root_fd)
        if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise PlaybookError("raw output root changed during replay")
        _check_private_directory(self.root_fd, "raw output root")

    def _verify_raw_name(self) -> None:
        self._verify_root_name()
        metadata = os.stat("raw", dir_fd=self.root_fd, follow_symlinks=False)
        opened = os.fstat(self.raw_fd)
        if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise PlaybookError("raw output directory changed during replay")

    def _remove_stale_pending_files(self) -> None:
        for name in os.listdir(self.raw_fd):
            if not name.startswith(".pending-"):
                continue
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self.raw_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077
                    or metadata.st_nlink != 1
                ):
                    raise PlaybookError("stale raw output temporary is unsafe")
            finally:
                os.close(descriptor)
            os.unlink(name, dir_fd=self.raw_fd)
            os.fsync(self.raw_fd)

    def _publish_bytes(self, name: str, content: bytes) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", name):
            raise PlaybookError("raw output filename is invalid")
        try:
            existing = _read_private_file_at(
                self.raw_fd, name, expected_size=len(content)
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing != content
                or hashlib.sha256(existing).digest() != hashlib.sha256(content).digest()
            ):
                raise PlaybookError("existing raw output differs from replay bytes")
            return name

        temporary = ".pending-" + secrets.token_hex(16)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=self.raw_fd)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while publishing raw output")
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            descriptor = -1
            try:
                os.unlink(temporary, dir_fd=self.raw_fd)
                os.fsync(self.raw_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        try:
            verified = _read_private_file_at(
                self.raw_fd, temporary, expected_size=len(content)
            )
            if (
                verified != content
                or hashlib.sha256(verified).digest() != hashlib.sha256(content).digest()
            ):
                raise PlaybookError("staged raw output failed integrity verification")
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=self.raw_fd,
                    dst_dir_fd=self.raw_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing = _read_private_file_at(
                    self.raw_fd, name, expected_size=len(content)
                )
                if existing != content:
                    raise PlaybookError(
                        "concurrent raw output differs from replay bytes"
                    )
            os.fsync(self.raw_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.raw_fd)
                os.fsync(self.raw_fd)
            except FileNotFoundError:
                pass
        final = _read_private_file_at(self.raw_fd, name, expected_size=len(content))
        if (
            final != content
            or hashlib.sha256(final).digest() != hashlib.sha256(content).digest()
        ):
            raise PlaybookError("published raw output failed integrity verification")
        return name

    def publish_body(self, name: str, body: bytes) -> None:
        self._publish_bytes(name, body)

    def publish_manifest(self, content: bytes) -> str:
        self._verify_raw_name()
        name = self._publish_bytes("index.json", content)
        self._verify_raw_name()
        return name

    def read_file(self, name: str, *, expected_size: int | None = None) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", name):
            raise PlaybookError("raw output filename is invalid")
        self._verify_raw_name()
        return _read_private_file_at(self.raw_fd, name, expected_size=expected_size)


def replay(playbook: dict, fixture_root: Path, output_root: Path) -> dict:
    """Replay for trusted in-process callers only; not an agent isolation boundary.

    Untrusted callers must use the protected Scout MCP executor, which owns the
    output volume. The path-based API cannot protect against a same-UID process
    that can rename directories in its filesystem namespace.
    """
    """Replay declared steps against captured bodies and publish raw outputs offline."""
    errors = validate_playbook(playbook)
    if errors:
        raise PlaybookError("cannot replay invalid playbook: " + "; ".join(errors))
    try:
        index = json.loads((fixture_root / "index.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PlaybookError(f"cannot read fixture index: {error}") from error
    if (
        not isinstance(index, dict)
        or index.get("complete") is not True
        or not isinstance(index.get("pages"), list)
    ):
        raise PlaybookError("fixture index must be a complete published pages manifest")
    origin = playbook["source"]["origin"]
    prefixes = playbook["source"]["path_prefixes"]
    fixture_pages = {}
    fixture_aliases = {}
    for item in index.get("pages", []):
        if not isinstance(item, dict) or not isinstance(item.get("body_file"), str):
            raise PlaybookError("fixture entries must include a body file path")
        url = _scoped_url(
            item.get("effective_url", item.get("url", "")), origin, prefixes
        )
        requested_url = _scoped_url(item.get("requested_url", url), origin, prefixes)
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
        fixture_pages[url] = {
            **item,
            "url": url,
            "requested_url": requested_url,
            "body": body,
        }
        for alias in {url, requested_url}:
            if alias in fixture_aliases:
                raise PlaybookError("fixture URLs alias the same replay target")
            fixture_aliases[alias] = url
    page_budget = playbook["budgets"]["max_pages"]
    request_budget = playbook["budgets"]["max_requests"]
    byte_budget = playbook["budgets"]["max_response_bytes"]
    discovered_queue = []
    visited: set[str] = set()
    results = []
    failures = []
    raw_bodies = {}
    requests_used = 0

    def observed_links(url: str) -> list[str]:
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
            if candidate not in found:
                found.append(candidate)
        return found

    def discover_from(url: str) -> list[str]:
        return [
            candidate for candidate in observed_links(url) if candidate not in visited
        ]

    def fetch_fixture(url: str, step_id: str) -> bool:
        nonlocal requests_used
        effective_url = fixture_aliases.get(url, url)
        if effective_url in visited:
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
        page = fixture_pages.get(effective_url)
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
        request_count = page.get("request_count", 1)
        if not isinstance(request_count, int) or request_count < 1:
            failures.append(
                {
                    "step_id": step_id,
                    "url": url,
                    "reason": "invalid captured request count",
                }
            )
            return False
        if requests_used + request_count > request_budget:
            failures.append(
                {
                    "step_id": step_id,
                    "url": url,
                    "reason": "approved request budget exhausted",
                }
            )
            return False
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
        visited.add(effective_url)
        requests_used += request_count
        results.append(
            {
                "requested_url": page["requested_url"],
                "url": effective_url,
                "sequence": page.get("sequence", len(results) + 1),
                "status": page["status"],
                "evidence_ref": page["evidence_ref"],
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "request_count": request_count,
                "discovered_links": observed_links(effective_url),
            }
        )
        raw_bodies[effective_url] = body
        return True

    raw_outputs = []
    raw_manifest = None
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
            for url in (result["url"] for result in results):
                discovered.extend(
                    candidate
                    for candidate in discover_from(url)
                    if candidate not in discovered
                )
            discovered_queue = discovered[: step["max_links"]]
        elif action == "output.raw":
            try:
                with _PrivateRawOutput(output_root) as publisher:
                    for sequence, result in enumerate(results, start=1):
                        body = raw_bodies[result["url"]]
                        filename = f"{sequence:04d}-{result['sha256']}.body"
                        publisher.publish_body(filename, body)
                        raw_outputs.append(
                            {
                                "url": result["url"],
                                "evidence_ref": result["evidence_ref"],
                                "body_file": str(Path("raw") / filename),
                                "bytes": len(body),
                                "sha256": result["sha256"],
                            }
                        )
                    manifest = {
                        "complete": True,
                        "playbook_id": playbook["playbook_id"],
                        "outputs": raw_outputs,
                    }
                    manifest_bytes = (
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    manifest_name = publisher.publish_manifest(manifest_bytes)
                    raw_manifest = {
                        "complete": True,
                        "path": str(Path("raw") / manifest_name),
                        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    }
            except (OSError, ValueError) as error:
                if isinstance(error, PlaybookError):
                    raise
                raise PlaybookError("raw output publication failed securely") from error
    replay_pages = [
        {key: value for key, value in result.items() if key != "_body"}
        for result in results
    ]
    return {
        "playbook_id": playbook["playbook_id"],
        "state": "failed" if failures else "passed",
        "replayed_pages": replay_pages,
        "requests_used": requests_used,
        "raw_outputs": raw_outputs,
        "raw_manifest": raw_manifest,
        "failures": failures,
    }


def accept_acquisition(
    playbook: dict,
    transcript: dict,
    fixture_root: Path,
    output_root: Path,
    expectations: dict,
) -> dict:
    """Compare replay with an oracle; trusted callers only, not agent isolation."""
    errors = validate_playbook(playbook)
    structural = {"state": "passed" if not errors else "failed", "errors": errors}
    if errors:
        return {
            "structural_validation": structural,
            "replay_execution": {"state": "not_run"},
            "acquisition_acceptance": {"state": "rejected", "failures": errors},
        }
    try:
        report = replay(playbook, fixture_root, output_root)
    except (OSError, PlaybookError) as error:
        return {
            "structural_validation": structural,
            "replay_execution": {"state": "failed", "error": str(error)},
            "acquisition_acceptance": {
                "state": "rejected",
                "failures": ["offline replay could not complete"],
            },
        }
    replay_state = report["state"]
    failures = []
    if expectations.get("expectation_schema") != "capture-acquisition-expectations/v1":
        failures.append("unsupported independent expectation schema")
    if transcript.get("transcript_schema") != "scout-transcript-draft/v2":
        failures.append("acquisition acceptance requires transcript draft v2")
    if not transcript.get("navigation", {}).get("sequence_complete"):
        failures.append("transcript exploration sequence is incomplete")
    expected_source = expectations.get("source", {})
    if (
        playbook.get("source", {}).get("source_id"),
        playbook.get("source", {}).get("revision"),
    ) != (expected_source.get("source_id"), expected_source.get("revision")):
        failures.append("draft source/revision differs from independent expectations")
    start_expectation = expectations.get("start", {})
    fetch_steps = [step for step in playbook["steps"] if step["action"] == "http.fetch"]
    actual_start = fetch_steps[0]["url"] if fetch_steps else None
    if actual_start != start_expectation.get("requested_url"):
        failures.append(
            "draft does not start at the independently expected requested URL"
        )
    navigation = transcript.get("navigation", {})
    if navigation.get("start_effective_url") != start_expectation.get("effective_url"):
        failures.append(
            "transcript effective start URL differs from independent expectations"
        )
    pages = {page.get("url"): page for page in report["replayed_pages"]}
    transcript_pages = {page.get("url"): page for page in transcript.get("pages", [])}
    raw_outputs = {item.get("url"): item for item in report.get("raw_outputs", [])}
    raw_manifest_ref = report.get("raw_manifest")
    if (
        not isinstance(raw_manifest_ref, dict)
        or raw_manifest_ref.get("complete") is not True
        or raw_manifest_ref.get("path") != "raw/index.json"
    ):
        failures.append("replay did not publish a complete raw output manifest")
    else:
        try:
            with _PrivateRawOutput(output_root) as publisher:
                manifest_bytes = publisher.read_file("index.json")
            if hashlib.sha256(manifest_bytes).hexdigest() != raw_manifest_ref.get(
                "sha256"
            ):
                failures.append("raw output manifest digest differs from replay")
            manifest = json.loads(manifest_bytes)
            if (
                manifest.get("complete") is not True
                or manifest.get("playbook_id") != playbook.get("playbook_id")
                or manifest.get("outputs") != report.get("raw_outputs")
            ):
                failures.append("raw output manifest is incomplete or inconsistent")
        except (OSError, PlaybookError, json.JSONDecodeError):
            failures.append("raw output manifest is unavailable or invalid")
    required_pages = expectations.get("required_pages")
    if not isinstance(required_pages, list) or not required_pages:
        failures.append("independent expectations must require at least one page")
        required_pages = []
    if any(not isinstance(expected, dict) for expected in required_pages):
        failures.append("independent required pages must be objects")
        required_pages = [
            expected for expected in required_pages if isinstance(expected, dict)
        ]
    required_links = expectations.get("required_links", [])
    if not isinstance(required_links, list):
        failures.append("independent required links must be a list")
        required_links = []
    max_requests = expectations.get("max_requests")
    max_pages = expectations.get("max_pages")
    if (
        not isinstance(max_requests, int)
        or isinstance(max_requests, bool)
        or max_requests < 1
    ):
        failures.append("independent request limit must be a positive integer")
        max_requests = -1
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        failures.append("independent page limit must be a positive integer")
        max_pages = -1
    for expected in required_pages:
        url = expected.get("effective_url")
        replayed = pages.get(url)
        if replayed is None:
            failures.append(f"required page was not replayed: {url}")
            continue
        transcript_page = transcript_pages.get(url)
        if transcript_page is None:
            failures.append(f"required page is absent from transcript evidence: {url}")
        for field in ("sha256",):
            if replayed.get(field) != expected.get(field):
                failures.append(f"required page {field} differs for {url}")
        if transcript_page is not None and replayed.get(
            "evidence_ref"
        ) != transcript_page.get("evidence_ref"):
            failures.append(
                f"replay evidence identity differs from transcript for {url}"
            )
        if (
            expected.get("evidence_ref") is not None
            and replayed.get("evidence_ref") != expected["evidence_ref"]
        ):
            failures.append(f"required page evidence identity differs for {url}")
        if replayed.get("requested_url") != expected.get("requested_url", url):
            failures.append(f"required page requested URL differs for {url}")
        if replayed.get("bytes") != expected.get("size_bytes"):
            failures.append(f"required page size differs for {url}")
        raw = raw_outputs.get(url)
        if raw is None:
            failures.append(f"raw output was not produced for {url}")
            continue
        raw_name = raw.get("body_file", "")
        if not raw_name.startswith("raw/") or raw_name.count("/") != 1:
            failures.append(f"raw output path is invalid for {url}")
            continue
        try:
            with _PrivateRawOutput(output_root) as publisher:
                body = publisher.read_file(raw_name.removeprefix("raw/"))
        except (OSError, PlaybookError):
            failures.append(f"raw output is unavailable for {url}")
            continue
        if hashlib.sha256(body).hexdigest() != expected.get("sha256"):
            failures.append(f"raw output digest differs for {url}")
        if len(body) != expected.get("size_bytes"):
            failures.append(f"raw output size differs for {url}")
        for marker in expected.get("contains", []):
            if not isinstance(marker, str) or marker.encode("utf-8") not in body:
                failures.append(f"raw output lacks an expected marker for {url}")
    for edge in required_links:
        if not isinstance(edge, dict):
            failures.append("independent required links must be objects")
            continue
        parent = transcript_pages.get(edge.get("from_effective_url"))
        if parent is None or edge.get("to_requested_url") not in parent.get(
            "discovered_links", []
        ):
            failures.append(
                "required navigation edge is not supported by transcript evidence"
            )
        replay_parent = pages.get(edge.get("from_effective_url"))
        if replay_parent is None or edge.get(
            "to_requested_url"
        ) not in replay_parent.get("discovered_links", []):
            failures.append(
                "required navigation edge is absent from the replayed raw body"
            )
    if max_requests > 0 and report["requests_used"] > max_requests:
        failures.append("replay exceeded independent request limit")
    if max_pages > 0 and len(report["replayed_pages"]) > max_pages:
        failures.append("replay exceeded independent page limit")
    if replay_state != "passed":
        failures.append("replay execution reported technical failures")
    return {
        "structural_validation": structural,
        "replay_execution": report,
        "acquisition_acceptance": {
            "state": "accepted" if not failures else "rejected",
            "failures": failures,
            "checked_required_pages": len(required_pages),
            "checked_required_links": len(required_links),
            "records_extracted": False,
            "raw_output_only": True,
        },
    }
