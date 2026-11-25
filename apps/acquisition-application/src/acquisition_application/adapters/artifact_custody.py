"""BC06 public HTTP contract adapter used to place verified bytes in custody."""

from __future__ import annotations

import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from acquisition_output.domain import ArtifactRef, SourceRevisionRef

from ..result_bundle import FixtureImportError, _strict_json

_MAX_RESPONSE_BYTES = 64_000
_MAX_UPLOAD_BYTES = 2_000_000


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class ArtifactCustodyHTTPClient:
    """Consumer-side uploader; it owns no BC06 state or retry policy."""

    def __init__(self, base_url: str, bearer_token: str) -> None:
        if not base_url.startswith(("http://", "https://")) or not bearer_token:
            raise ValueError("BC06 client endpoint and credential are required")
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token

    def store_verified_entity(
        self,
        *,
        artifact: ArtifactRef,
        operation_id: str,
        tenant_id: str,
        source: SourceRevisionRef,
        body: bytes,
        representation: str,
        retention_policy_ref: str,
        evidence_ref: str,
    ) -> ArtifactRef:
        if (
            len(body) > _MAX_UPLOAD_BYTES
            or len(body) != artifact.size_bytes
            or "sha256:" + hashlib.sha256(body).hexdigest() != artifact.digest
        ):
            raise FixtureImportError("BC06 upload bytes differ from verified artifact")
        intent = {
            "contract_version": "artifact-custody.v1",
            "operation_id": operation_id,
            "tenant_id": tenant_id,
            "artifact_id": artifact.artifact_id,
            "source": {"source_id": source.source_id, "revision": source.revision},
            "artifact": {
                "artifact_id": artifact.artifact_id,
                "digest": artifact.digest,
                "size_bytes": artifact.size_bytes,
                "media_type": artifact.media_type,
            },
            "representation": "other_declared",
            "classification": "internal",
            "retention_policy_ref": retention_policy_ref,
            "metadata": {
                "declared_representation": representation,
                "evidence_ref": evidence_ref,
            },
        }
        reservation = self._request(
            "/draft/v1/artifact-intents",
            method="POST",
            tenant_id=tenant_id,
            body=json.dumps(intent, ensure_ascii=False, separators=(",", ":")).encode(),
        )
        if reservation.get("artifact_id") != artifact.artifact_id:
            raise FixtureImportError("BC06 reserved a different artifact identity")
        if not isinstance(reservation.get("receipt"), dict):
            self._request(
                f"/draft/v1/artifact-intents/{operation_id}/content",
                method="PUT",
                tenant_id=tenant_id,
                body=body,
            )
        query = urlencode(
            {
                "digest": artifact.digest,
                "size_bytes": artifact.size_bytes,
                "source_id": source.source_id,
                "source_revision": source.revision,
            }
        )
        verification = self._request(
            f"/draft/v1/artifacts/{artifact.artifact_id}/verification?{query}",
            method="GET",
            tenant_id=tenant_id,
            body=b"",
        )
        receipt = verification.get("receipt")
        if (
            verification.get("status") != "confirmed"
            or not isinstance(receipt, dict)
            or receipt.get("tenant_id") != tenant_id
            or receipt.get("artifact", {}).get("artifact_id") != artifact.artifact_id
            or receipt.get("artifact", {}).get("digest") != artifact.digest
            or receipt.get("artifact", {}).get("size_bytes") != artifact.size_bytes
            or (
                receipt.get("artifact", {}).get("media_type") is not None
                and receipt.get("artifact", {}).get("media_type") != artifact.media_type
            )
        ):
            raise FixtureImportError("BC06 did not confirm the exact artifact bytes")
        return artifact

    def _request(self, path: str, *, method: str, tenant_id: str, body: bytes):
        request = Request(
            self._base_url + path,
            data=body if method != "GET" else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self._token,
                "X-DM-Tenant-ID": tenant_id,
            },
        )
        try:
            with build_opener(_RejectRedirects).open(request, timeout=30) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(payload) > _MAX_RESPONSE_BYTES or response.read(1):
                    raise FixtureImportError("BC06 response exceeds its bound")
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise FixtureImportError("BC06 custody request failed") from error
        result = _strict_json(payload, "BC06 response")
        if not isinstance(result, dict):
            raise FixtureImportError("BC06 response must be an object")
        return result
