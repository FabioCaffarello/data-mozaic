"""Strict URL normalization and segment-aware target matching for HTTPS tools."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes, urlsplit, urlunsplit


class TargetRejected(ValueError):
    """A target is malformed or outside the registered HTTPS surface."""


_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_DOUBLE_ENCODED_PATH_CONTROL = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_SECRET_QUERY_KEY = re.compile(
    r"(?:^|[_-])(?:auth|credential|key|password|secret|session|token)(?:$|[_-])",
    re.IGNORECASE,
)
_PATH_SAFE = "/:@!$&'()*+,;=-._~"
_QUERY_SAFE = "/?:@!$&'()*+,;=-._~"


@dataclass(frozen=True)
class NormalizedURL:
    url: str
    origin: str
    path: str
    query: str
    host: str
    port: int


def _decode_component(value: str, *, path: bool) -> str:
    if _BAD_ESCAPE.search(value):
        raise TargetRejected("malformed percent escape")
    if path and _ENCODED_SEPARATOR.search(value):
        raise TargetRejected("encoded path separator is ambiguous")
    try:
        decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TargetRejected("URL component is not valid UTF-8") from error
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        raise TargetRejected("control characters are not allowed in URLs")
    return unicodedata.normalize("NFC", decoded)


def normalize_origin(value: str) -> str:
    target = normalize_url(value)
    if target.path != "/" or target.query:
        raise TargetRejected("registered origin must not include a path or query")
    return target.origin


def normalize_url(value: str) -> NormalizedURL:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise TargetRejected("URL is empty or too long")
    if "\\" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise TargetRejected("backslashes and control characters are not allowed")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise TargetRejected("invalid URL authority") from error
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise TargetRejected("only absolute HTTPS URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise TargetRejected("userinfo is not allowed in URLs")
    if parts.fragment:
        raise TargetRejected("fragments are not request targets")
    raw_host = parts.hostname
    if not raw_host:
        raise TargetRejected("URL has no host")
    if port not in (None, 443):
        raise TargetRejected("only the default HTTPS port is allowed")
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise TargetRejected("invalid internationalized hostname") from error
        labels = host.split(".")
        label_pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
        if (
            not host
            or len(host) > 253
            or any(not label_pattern.fullmatch(label) for label in labels)
        ):
            raise TargetRejected("invalid hostname")
        authority = host
    else:
        if "%" in raw_host:
            raise TargetRejected("scoped IP literals are not allowed")
        if not address.is_global:
            raise TargetRejected("non-public IP literals are prohibited")
        host = address.compressed
        authority = f"[{host}]" if address.version == 6 else host
    path = _decode_component(parts.path or "/", path=True)
    if _DOUBLE_ENCODED_PATH_CONTROL.search(path):
        raise TargetRejected("double-encoded path controls are ambiguous")
    if not path.startswith("/"):
        raise TargetRejected("URL path must be absolute")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise TargetRejected("dot segments are not allowed in URL paths")
    path = quote(path, safe=_PATH_SAFE)
    query = _decode_component(parts.query, path=False)
    for key_value in query.split("&"):
        key = key_value.split("=", 1)[0]
        if _SECRET_QUERY_KEY.search(key):
            raise TargetRejected("credential-like query parameters are not allowed")
    query = quote(query, safe=_QUERY_SAFE)
    origin = f"https://{authority}"
    url = urlunsplit(("https", authority, path, query, ""))
    return NormalizedURL(url, origin, path, query, host, 443)


def path_prefix_contains(prefix: str, path: str) -> bool:
    prefix = prefix.rstrip("/") or "/"
    if prefix == "/":
        return path.startswith("/")
    return path == prefix or path.startswith(prefix + "/")


def intersect_path_prefixes(left: str, right: str) -> str | None:
    if path_prefix_contains(left, right):
        return right
    if path_prefix_contains(right, left):
        return left
    return None
