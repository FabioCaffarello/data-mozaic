"""Draft live-execution binding checks consumed by BC15.

This module validates owner-issued public documents; it does not issue policy,
source, or release approvals. Production authorization remains an owner service
integration and this code deliberately has no network or credential access.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from mozaic_bot_build import canonical_json

from .syntax import PlaybookError


@dataclass(frozen=True)
class LiveAdmission:
    source_id: str
    source_revision_id: str
    policy_id: str
    policy_version: str
    release_id: str
    build_id: str
    build_manifest_digest: str
    payload_contract_id: str | None
    payload_contract_version: str | None
    payload_contract_digest: str | None


_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)


def _origin(value: str, *, allow_test_http: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise PlaybookError("origin is malformed") from error
    if (
        parsed.scheme not in ({"https", "http"} if allow_test_http else {"https"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise PlaybookError("origin must be a bare authorized HTTP origin")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise PlaybookError("origin hostname is malformed") from error
        if host.endswith(".") or not host:
            raise PlaybookError("origin hostname is ambiguous")
    else:
        host = address.compressed
        if address.version == 6:
            host = f"[{host}]"
    scheme = parsed.scheme.lower()
    if scheme == "http":
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname.lower() == "localhost"
        if not allow_test_http or not local:
            raise PlaybookError("unencrypted HTTP is restricted to test loopback")
    default_port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port and port != default_port else "")


def _canonical_path(path: str) -> str:
    if not path.startswith("/") or _BAD_PERCENT.search(path):
        raise PlaybookError("request path is malformed")
    if _ENCODED_SEPARATOR.search(path):
        raise PlaybookError("encoded path separators are not allowed")
    decoded = path
    for _ in range(8):
        try:
            next_value = unquote(decoded, errors="strict")
        except UnicodeError as error:
            raise PlaybookError("request path encoding is invalid") from error
        if next_value == decoded:
            break
        decoded = next_value
        if "\\" in decoded or any(ord(char) < 0x20 for char in decoded):
            raise PlaybookError("request path contains forbidden characters")
        if _ENCODED_SEPARATOR.search(decoded):
            raise PlaybookError("nested encoded path separators are not allowed")
    else:
        raise PlaybookError("request path encoding is too deeply nested")
    if (
        "%" in decoded
        or "\\" in decoded
        or any(ord(char) < 0x20 for char in decoded)
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise PlaybookError("request path is ambiguous or traverses scope")
    return decoded


def _path_allowed(path: str, prefixes: list[str]) -> bool:
    decoded = _canonical_path(path)
    return any(_within_path(decoded, _canonical_path(prefix)) for prefix in prefixes)


def _within_path(path: str, prefix: str) -> bool:
    boundary = prefix.rstrip("/")
    return prefix == "/" or path == boundary or path.startswith(boundary + "/")


def _document_digest(document: dict[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in document.items() if key != field}
    return "sha256:" + hashlib.sha256(canonical_json(unsigned)).hexdigest()


def validate_live_admission(
    *,
    source_revision: dict[str, Any],
    policy: dict[str, Any],
    release: dict[str, Any],
    build_manifest: dict[str, Any],
    requested_urls: tuple[str, ...],
) -> LiveAdmission:
    """Check immutable bindings; owner services must establish authenticity.

    The caller must obtain the three owner documents from trusted registries and
    verify the build bytes before calling this function. This is a binding
    consistency check, not a signature or authorization decision.
    """
    if source_revision.get("status") != "eligible":
        raise PlaybookError("source revision is not eligible")
    if policy.get("status") != "eligible":
        raise PlaybookError("acquisition policy is not eligible")
    if release.get("status") != "approved":
        raise PlaybookError("BotRelease is not approved")
    contract = release.get("payload_contract")
    artifact_profile = release.get("contract_version") == "bot-release/v4"
    if release.get("contract_version") not in {
        "bot-release/v2",
        "bot-release/v3",
        "bot-release/v4",
    } or (
        not artifact_profile
        and (
            not isinstance(contract, dict)
            or set(contract) != {"contract_id", "version", "digest"}
            or not isinstance(contract.get("contract_id"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", contract["contract_id"])
            or not isinstance(contract.get("version"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,63}", contract["version"])
            or not isinstance(contract.get("digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", contract["digest"])
        )
    ):
        raise PlaybookError("BotRelease payload contract binding is invalid")
    if artifact_profile:
        output_contract = release.get("output_contract")
        if (
            not isinstance(output_contract, dict)
            or output_contract != build_manifest.get("output_contract")
            or build_manifest.get("manifest_version") != "bot-build-manifest/v4"
            or release.get("payload_contract") is not None
        ):
            raise PlaybookError("BotRelease raw artifact binding differs from BotBuild")
    if release.get("contract_version") in {"bot-release/v3", "bot-release/v4"}:
        input_contract = release.get("input_contract")
        input_request = release.get("input_request")
        manifest_request = build_manifest.get("live_request")
        valid_request = (
            isinstance(input_request, dict)
            and input_request == manifest_request
            and input_request.get("method") == "GET"
            and (
                (artifact_profile and set(input_request) == {"method", "url"})
                or (
                    not artifact_profile
                    and set(input_request) == {"method", "url", "input_query"}
                    and isinstance(input_request.get("input_query"), dict)
                    and set(input_request["input_query"]) == {"parameter", "query_name"}
                )
            )
        )
        if (
            not isinstance(input_contract, dict)
            or set(input_contract) != {"contract_id", "version", "digest"}
            or not isinstance(input_contract.get("contract_id"), str)
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9._:-]{0,127}", input_contract["contract_id"]
            )
            or not isinstance(input_contract.get("version"), str)
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9._:-]{0,63}", input_contract["version"]
            )
            or not isinstance(input_contract.get("digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", input_contract["digest"])
        ):
            raise PlaybookError("BotRelease input contract binding is invalid")
        if not valid_request:
            raise PlaybookError("BotRelease input request differs from BotBuild")
    for document, field, label in (
        (source_revision, "configuration_digest", "source revision"),
        (policy, "policy_digest", "acquisition policy"),
        (release, "release_digest", "BotRelease"),
    ):
        if document.get(field) != _document_digest(document, field):
            raise PlaybookError(f"{label} digest does not match its immutable fields")
    source_id = source_revision.get("source_id")
    revision_id = source_revision.get("revision_id")
    if (policy.get("source_id"), policy.get("source_revision_id")) != (
        source_id,
        revision_id,
    ):
        raise PlaybookError("policy is not bound to the supplied source revision")
    if (release.get("source_id"), release.get("source_revision_id")) != (
        source_id,
        revision_id,
    ):
        raise PlaybookError("release is not bound to the supplied source revision")
    if (release.get("policy_id"), release.get("policy_version")) != (
        policy.get("policy_id"),
        policy.get("policy_version"),
    ):
        raise PlaybookError("release is not bound to the supplied policy version")
    if (
        release.get("backend_id") != "scrapy-live-http"
        or release.get("runtime_profile_id") != "scrapy-live-http-v1"
    ):
        raise PlaybookError("release runtime profile is not the live HTTP profile")
    if release.get("build_id") != build_manifest.get("build_id"):
        raise PlaybookError("release build identity differs from verified BotBuild")
    expected_build_digest = (
        "sha256:" + hashlib.sha256(canonical_json(build_manifest)).hexdigest()
    )
    if release.get("build_manifest_digest") != expected_build_digest:
        raise PlaybookError(
            "release digest differs from the supplied BotBuild manifest"
        )
    if source_revision.get("scope", {}).get("methods") is None:
        raise PlaybookError("source revision has no acquisition scope")
    if not set(policy.get("methods", ())) <= set(
        source_revision["scope"].get("methods", ())
    ):
        raise PlaybookError("policy methods exceed source revision scope")
    test_policy = policy.get("dns_policy") == "test-fixture-only"
    source_origins = {
        _origin(value, allow_test_http=test_policy)
        for value in source_revision["scope"].get("origins", ())
    }
    policy_origins = {
        _origin(value, allow_test_http=test_policy)
        for value in policy.get("origins", ())
    }
    if not policy_origins or not policy_origins <= source_origins:
        raise PlaybookError("policy origins exceed source revision scope")
    source_prefixes = [
        _canonical_path(path)
        for path in source_revision["scope"].get("path_prefixes", ())
    ]
    policy_prefixes = [
        _canonical_path(path) for path in policy.get("path_prefixes", ())
    ]
    if not policy_prefixes or any(
        not any(_within_path(path, allowed) for allowed in source_prefixes)
        for path in policy_prefixes
    ):
        raise PlaybookError("policy paths exceed source revision scope")
    if test_policy:
        if policy.get("require_tls") is not False:
            raise PlaybookError("test-fixture policy must explicitly disable TLS")
    elif policy.get("dns_policy") != "public-addresses-only" or not policy.get(
        "require_tls"
    ):
        raise PlaybookError("live admission requires public DNS and TLS policy")
    if not requested_urls:
        raise PlaybookError("live admission requires at least one compiled request URL")
    for requested_url in requested_urls:
        try:
            parsed = urlsplit(requested_url)
            origin = _origin(
                f"{parsed.scheme}://{parsed.netloc}", allow_test_http=test_policy
            )
            if (
                parsed.fragment
                or "\\" in requested_url
                or "\r" in requested_url
                or "\n" in requested_url
            ):
                raise PlaybookError("compiled request URL is ambiguous")
            if _BAD_PERCENT.search(parsed.query):
                raise PlaybookError("compiled request query encoding is invalid")
            query_pairs = parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=True
            )
            if any("%" in key or "%" in value for key, value in query_pairs):
                raise PlaybookError("nested query encoding is not allowed")
        except ValueError as error:
            raise PlaybookError("compiled request URL is malformed") from error
        query = dict(query_pairs)
        allowed_query = policy.get("query_parameter_values", {})
        if (
            origin not in policy_origins
            or not _path_allowed(parsed.path or "/", policy_prefixes)
            or len(query_pairs) != len(query)
            or any(
                key not in allowed_query or value not in allowed_query[key]
                for key, value in query_pairs
            )
        ):
            raise PlaybookError(
                "compiled request origin/path/query is outside approved policy"
            )
    source_query = source_revision["scope"].get("query_parameter_values", {})
    policy_query = policy.get("query_parameter_values", {})
    if any(
        key not in source_query or not set(values) <= set(source_query[key])
        for key, values in policy_query.items()
    ):
        raise PlaybookError("policy query values exceed source revision scope")
    return LiveAdmission(
        source_id=source_id,
        source_revision_id=revision_id,
        policy_id=policy["policy_id"],
        policy_version=policy["policy_version"],
        release_id=release["release_id"],
        build_id=release["build_id"],
        build_manifest_digest=expected_build_digest,
        payload_contract_id=None if artifact_profile else contract["contract_id"],
        payload_contract_version=None if artifact_profile else contract["version"],
        payload_contract_digest=None if artifact_profile else contract["digest"],
    )
