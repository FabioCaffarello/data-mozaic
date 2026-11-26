"""Bounded local registry for immutable, repository-installed JSON Schemas.

The registry manifest and schema files are installed with the application
release. This is not an administrative network API or a mutable database.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_MAX_REGISTRY_BYTES = 256_000
_MAX_SCHEMA_BYTES = 64_000


class ContractResolutionError(ValueError):
    """Exact contract resolution, schema or payload validation failed."""


@dataclass(frozen=True, slots=True)
class ContractReference:
    contract_id: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or not _IDENTIFIER.fullmatch(
            self.contract_id
        ):
            raise ContractResolutionError("contract identity is invalid")
        if not isinstance(self.version, str) or not _VERSION.fullmatch(self.version):
            raise ContractResolutionError("contract version is invalid")
        if not isinstance(self.digest, str) or not _DIGEST.fullmatch(self.digest):
            raise ContractResolutionError("contract digest is invalid")


@dataclass(frozen=True, slots=True)
class ResolvedJSONSchema:
    reference: ContractReference
    dialect: str
    _schema_bytes: bytes

    def validate(self, payload: Any) -> None:
        try:
            schema = json.loads(self._schema_bytes.decode("utf-8", errors="strict"))
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(
                payload
            )
        except ValidationError:
            # Avoid returning rejected source payload values in diagnostics.
            raise ContractResolutionError(
                "payload is invalid for its contract"
            ) from None


class ImmutableContractRegistry:
    """Resolve exact revisions from a code-reviewed local installation.

    Only self-contained Draft 2020-12 schemas are supported. Any ``$ref`` is
    rejected in this initial capability, so validation cannot make network
    requests or change when a referenced document changes.
    """

    def __init__(self, contracts_root: Path) -> None:
        self._root = contracts_root.resolve(strict=True)
        if not self._root.is_dir() or contracts_root.is_symlink():
            raise ContractResolutionError("contract installation root is invalid")
        manifest_path = self._root / "registry.json"
        raw = self._read_regular(manifest_path, _MAX_REGISTRY_BYTES)
        document = self._strict_json(raw, "contract registry")
        if (
            not isinstance(document, dict)
            or set(document) != {"contract_version", "revisions"}
            or document.get("contract_version") != "payload-contract-registry/v1"
            or not isinstance(document.get("revisions"), list)
        ):
            raise ContractResolutionError("contract registry structure is invalid")
        revisions: dict[tuple[str, str], tuple[ContractReference, str, str]] = {}
        for item in document["revisions"]:
            if not isinstance(item, dict) or set(item) != {
                "contract_id",
                "version",
                "digest",
                "schema_path",
                "dialect",
            }:
                raise ContractResolutionError("contract registry entry is invalid")
            reference = ContractReference(
                item["contract_id"], item["version"], item["digest"]
            )
            path = item["schema_path"]
            dialect = item["dialect"]
            if (
                not isinstance(path, str)
                or Path(path).name != path
                or path in {"", ".", ".."}
                or dialect != _DIALECT
            ):
                raise ContractResolutionError(
                    "contract schema path or dialect is unsupported"
                )
            key = (reference.contract_id, reference.version)
            if key in revisions:
                raise ContractResolutionError(
                    "contract identity and version have conflicting registrations"
                )
            revisions[key] = (reference, path, dialect)
        self._revisions = revisions

    def resolve(self, reference: ContractReference) -> ResolvedJSONSchema:
        entry = self._revisions.get((reference.contract_id, reference.version))
        if entry is None:
            raise ContractResolutionError("contract revision is unresolved")
        registered, relative_path, dialect = entry
        if registered != reference:
            raise ContractResolutionError("contract digest does not match revision")
        path = self._root / relative_path
        raw = self._read_regular(path, _MAX_SCHEMA_BYTES)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != registered.digest:
            raise ContractResolutionError("installed contract content digest differs")
        document = self._strict_json(raw, "contract schema")
        if not isinstance(document, dict) or document.get("$schema") != dialect:
            raise ContractResolutionError("contract schema dialect is invalid")
        if self._contains_ref(document):
            raise ContractResolutionError("schema references are unsupported")
        try:
            Draft202012Validator.check_schema(document)
        except Exception as error:
            raise ContractResolutionError("contract schema is invalid") from error
        return ResolvedJSONSchema(registered, dialect, raw)

    def installed_reference(self, contract_id: str, version: str) -> ContractReference:
        """Return an installed identity for trusted release composition only.

        This lookup identifies installed content; it does not approve or bind a
        release. Consumers must still obtain authorization from their owner.
        """
        entry = self._revisions.get((contract_id, version))
        if entry is None:
            raise ContractResolutionError("contract revision is unresolved")
        return entry[0]

    @staticmethod
    def _contains_ref(value: Any) -> bool:
        if isinstance(value, dict):
            # Draft 2020-12 defines static and dynamic URI references. Reject
            # both before the validator can attempt any reference resolution.
            return any(key in value for key in ("$ref", "$dynamicRef")) or any(
                ImmutableContractRegistry._contains_ref(item) for item in value.values()
            )
        if isinstance(value, list):
            return any(ImmutableContractRegistry._contains_ref(item) for item in value)
        return False

    @staticmethod
    def _strict_json(raw: bytes, label: str) -> Any:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ContractResolutionError(f"{label} contains duplicate keys")
                result[key] = value
            return result

        try:
            return json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ContractResolutionError(f"{label} contains a non-finite number")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractResolutionError(f"{label} is not valid UTF-8 JSON") from error

    @staticmethod
    def _read_regular(path: Path, maximum: int) -> bytes:
        try:
            if path.is_symlink() or not path.is_file():
                raise ContractResolutionError("contract file is not a regular file")
            with path.open("rb") as stream:
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise ContractResolutionError("contract file exceeds size limit")
            return raw
        except OSError as error:
            raise ContractResolutionError("contract file cannot be read") from error
