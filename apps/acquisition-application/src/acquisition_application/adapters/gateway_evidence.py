"""Trusted operator adapter for Gateway's owner-local release contract."""

from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..result_bundle import FixtureImportError, _strict_json


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class GatewayEvidenceReleaseHTTPClient:
    """Uses the distinct release credential; never intended for workers."""

    def __init__(self, base_url: str, release_token: str) -> None:
        if not base_url.startswith(("http://", "https://")) or not release_token:
            raise ValueError(
                "Gateway release endpoint and operator credential are required"
            )
        self._base_url = base_url.rstrip("/")
        self._token = release_token

    def release(
        self,
        *,
        tenant_id: str,
        run_id: str,
        attempt_id: str,
        generation: int,
        fetch_id: str,
        digest: str,
    ) -> dict[str, object]:
        request = Request(
            self._base_url + "/internal/v1/evidence/" + quote(fetch_id, safe=""),
            method="DELETE",
            headers={
                "Authorization": "Bearer " + self._token,
                "X-DM-Tenant-ID": tenant_id,
                "X-DM-Run-ID": run_id,
                "X-DM-Attempt-ID": attempt_id,
                "X-DM-Generation": str(generation),
                "X-DM-Body-SHA256": digest,
            },
        )
        try:
            with build_opener(_RejectRedirects).open(request, timeout=15) as response:
                payload = response.read(4097)
                if len(payload) > 4096 or response.read(1):
                    raise FixtureImportError(
                        "Gateway release response exceeds its bound"
                    )
        except HTTPError as error:
            detail = error.read(1024).decode("utf-8", errors="replace")
            raise FixtureImportError(
                f"Gateway rejected evidence release ({error.code}): {detail}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise FixtureImportError(
                "Gateway evidence release outcome is unresolved"
            ) from error
        value = _strict_json(payload, "Gateway release response")
        if (
            not isinstance(value, dict)
            or value.get("fetch_id") != fetch_id
            or value.get("body_state") != "released"
            or value.get("body_digest") != digest
        ):
            raise FixtureImportError(
                "Gateway release response differs from exact evidence identity"
            )
        return value


class GatewayEvidenceMetadataHTTPClient:
    """Resolve exact historical FetchEvidence metadata for operator inspection."""

    def __init__(self, base_url: str, importer_token: str) -> None:
        if not base_url.startswith(("http://", "https://")) or not importer_token:
            raise ValueError(
                "Gateway metadata endpoint and importer credential are required"
            )
        self._base_url = base_url.rstrip("/")
        self._token = importer_token

    def get(
        self,
        *,
        tenant_id: str,
        run_id: str,
        attempt_id: str,
        generation: int,
        fetch_id: str,
    ) -> dict[str, object]:
        request = Request(
            self._base_url
            + "/internal/v1/evidence/"
            + quote(fetch_id, safe="")
            + "/metadata",
            headers={
                "Authorization": "Bearer " + self._token,
                "X-DM-Tenant-ID": tenant_id,
                "X-DM-Run-ID": run_id,
                "X-DM-Attempt-ID": attempt_id,
                "X-DM-Generation": str(generation),
            },
        )
        try:
            with build_opener(_RejectRedirects).open(request, timeout=5) as response:
                payload = response.read(8193)
                if len(payload) > 8192 or response.read(1):
                    raise FixtureImportError("Gateway metadata exceeds its bound")
        except HTTPError as error:
            detail = error.read(1024).decode("utf-8", errors="replace")
            raise FixtureImportError(
                f"Gateway rejected evidence metadata lookup ({error.code}): {detail}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise FixtureImportError(
                "Gateway evidence metadata is unavailable"
            ) from error
        value = _strict_json(payload, "Gateway evidence metadata")
        if (
            not isinstance(value, dict)
            or value.get("fetch_id") != fetch_id
            or value.get("tenant_id") != tenant_id
            or value.get("run_id") != run_id
            or value.get("attempt_id") != attempt_id
            or value.get("generation") != generation
        ):
            raise FixtureImportError(
                "Gateway metadata differs from exact evidence identity"
            )
        return value
