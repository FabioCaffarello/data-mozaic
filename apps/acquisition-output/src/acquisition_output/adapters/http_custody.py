"""Draft BC06 HTTP adapter implementing BC05's custody verification port."""

from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from acquisition_output.domain import ArtifactRef, CustodyReceipt, SourceRevisionRef


class ArtifactCustodyHTTPError(RuntimeError):
    """BC06 did not confirm custody or could not be reached."""


class ArtifactCustodyHTTPAdapter:
    def __init__(self, base_url: str, bearer_token: str, *, timeout: float = 10.0):
        if not base_url.startswith(("http://", "https://")) or not bearer_token:
            raise ValueError("BC06 base URL and local/test bearer token are required")
        self._base = base_url.rstrip("/")
        self._token = bearer_token
        self._timeout = timeout

    def confirm(
        self, artifact: ArtifactRef, *, tenant_id: str, source: SourceRevisionRef
    ) -> CustodyReceipt:
        query = urlencode(
            {
                "digest": artifact.digest,
                "size_bytes": artifact.size_bytes,
                "source_id": source.source_id,
                "source_revision": source.revision,
            }
        )
        url = f"{self._base}/draft/v1/artifacts/{artifact.artifact_id}/verification?{query}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-DM-Tenant-ID": tenant_id,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                value = json.loads(response.read(64 * 1024))
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            raise ArtifactCustodyHTTPError(
                "BC06 custody verification failed"
            ) from error
        if not isinstance(value, Mapping) or value.get("status") != "confirmed":
            raise ArtifactCustodyHTTPError("BC06 did not confirm artifact custody")
        receipt = value.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ArtifactCustodyHTTPError("BC06 response has no custody receipt")
        ref = receipt.get("artifact")
        if not isinstance(ref, Mapping):
            raise ArtifactCustodyHTTPError("BC06 receipt has no artifact reference")
        try:
            result = CustodyReceipt(
                tenant_id=str(receipt["tenant_id"]),
                artifact_id=str(ref["artifact_id"]),
                digest=str(ref["digest"]),
                size_bytes=int(ref["size_bytes"]),
                receipt_id=str(receipt["receipt_id"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ArtifactCustodyHTTPError("BC06 receipt is malformed") from error
        if (
            result.tenant_id != tenant_id
            or result.artifact_id != artifact.artifact_id
            or result.digest != artifact.digest
            or result.size_bytes != artifact.size_bytes
        ):
            raise ArtifactCustodyHTTPError(
                "BC06 receipt does not match requested artifact"
            )
        return result
