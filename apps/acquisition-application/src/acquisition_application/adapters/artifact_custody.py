"""BC06 public HTTP contract adapter used to place verified bytes in custody."""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import stat
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
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
        body: bytes | Path,
        representation: str,
        retention_policy_ref: str,
        evidence_ref: str,
    ) -> ArtifactRef:
        body_size, body_digest = self._body_identity(body)
        if (
            body_size > _MAX_UPLOAD_BYTES
            or body_size != artifact.size_bytes
            or body_digest != artifact.digest
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

    def _request(self, path: str, *, method: str, tenant_id: str, body: bytes | Path):
        if isinstance(body, Path):
            if method != "PUT":
                raise FixtureImportError("BC06 file transfer method is invalid")
            return self._upload_file(path, tenant_id=tenant_id, body=body)
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

    def _body_identity(self, body: bytes | Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        if isinstance(body, bytes):
            digest.update(body)
            return len(body), "sha256:" + digest.hexdigest()
        try:
            info = body.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size < 0:
                raise FixtureImportError("BC06 upload source is not a regular file")
            size = 0
            with body.open("rb") as source:
                while chunk := source.read(64 << 10):
                    size += len(chunk)
                    if size > _MAX_UPLOAD_BYTES:
                        raise FixtureImportError("BC06 upload exceeds its bound")
                    digest.update(chunk)
            if size != info.st_size:
                raise FixtureImportError("BC06 upload source changed while hashing")
            return size, "sha256:" + digest.hexdigest()
        except OSError as error:
            raise FixtureImportError("BC06 upload source is unavailable") from error

    def _upload_file(
        self, path: str, *, tenant_id: str, body: Path
    ) -> dict[str, object]:
        parsed = urlsplit(self._base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise FixtureImportError("BC06 client endpoint is invalid")
        connection_type = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        kwargs = {"timeout": 30}
        if parsed.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        connection = connection_type(parsed.hostname, parsed.port, **kwargs)
        request_path = (parsed.path.rstrip("/") + path) or "/"
        try:
            size, _ = self._body_identity(body)
            connection.putrequest("PUT", request_path)
            connection.putheader("Authorization", "Bearer " + self._token)
            connection.putheader("X-DM-Tenant-ID", tenant_id)
            connection.putheader("Content-Length", str(size))
            connection.putheader("Connection", "close")
            connection.endheaders()
            with body.open("rb") as source:
                while chunk := source.read(64 << 10):
                    connection.send(chunk)
            response = connection.getresponse()
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_RESPONSE_BYTES or response.read(1):
                raise FixtureImportError("BC06 response exceeds its bound")
            status = response.status
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            ssl.SSLError,
        ) as error:
            raise FixtureImportError("BC06 custody upload failed") from error
        finally:
            connection.close()
        try:
            result = _strict_json(payload, "BC06 response")
        except FixtureImportError as error:
            raise FixtureImportError(
                f"BC06 response was not valid JSON (HTTP {status}, {payload[:200]!r})"
            ) from error
        if not isinstance(result, dict):
            raise FixtureImportError("BC06 response must be an object")
        if status < 200 or status >= 300:
            code = result.get("code", "unknown")
            raise FixtureImportError(
                f"BC06 custody upload was rejected ({status}, {code})"
            )
        return result
