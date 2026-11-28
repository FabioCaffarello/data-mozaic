"""Machine-readable authoring surface derived from BC15 capability declarations.

This is an inspection projection, not an authorization grant. A returned
capability is executable only when the selected backend validator accepts the
compiled IR and the release lifecycle authorizes the resulting build.
"""

from __future__ import annotations

import json
from pathlib import Path

from .backend_scrapy_live import (
    PARAMETERIZED_QUERY_MAX_KEYS,
    RAW_V4_CONSTRAINTS,
    RAW_V4_OPERATORS,
)
from .backend_scrapy_live import PROFILE as LIVE_PROFILE

_PACKAGE = Path(__file__).resolve().parent


def _collect_values(
    node: object, property_name: str, operators: set, capabilities: set
) -> None:
    if isinstance(node, dict):
        value = node.get(property_name)
        if isinstance(value, dict) and isinstance(value.get("const"), str):
            operators.add(value["const"])
        if isinstance(value, dict) and isinstance(value.get("enum"), list):
            capabilities.update(item for item in value["enum"] if isinstance(item, str))
        for child in node.values():
            _collect_values(child, property_name, operators, capabilities)
    elif isinstance(node, list):
        for child in node:
            _collect_values(child, property_name, operators, capabilities)


def capability_catalog() -> dict:
    profiles = []
    for path in sorted((_PACKAGE / "profiles").glob("*.json")):
        declaration = json.loads(path.read_text(encoding="utf-8"))
        profiles.append(
            {
                "id": declaration.get("runtime_profile", declaration["backend_id"]),
                "backend_id": declaration["backend_id"],
                "backend_version": declaration["backend_version"],
                "execution_mode": declaration.get("execution_mode"),
                "capabilities": sorted(declaration["capabilities"]),
                "unsupported_capabilities": sorted(
                    declaration.get("unsupported_capabilities", [])
                ),
            }
        )

    raw_schema_path = _PACKAGE / "schemas/CapturePlaybook.v4.schema.json"
    raw_schema = json.loads(raw_schema_path.read_text(encoding="utf-8"))
    playbook_versions = []
    for path in sorted((_PACKAGE / "schemas").glob("CapturePlaybook*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        version = schema.get("properties", {}).get("dsl_version", {}).get("const")
        if not version:
            continue
        operators = set()
        capabilities = set()
        _collect_values(schema, "op", operators, capabilities)
        _collect_values(schema, "action", operators, capabilities)
        capability_items = (
            schema.get("properties", {}).get("capabilities", {}).get("items", {})
        )
        capabilities.update(
            item for item in capability_items.get("enum", []) if isinstance(item, str)
        )
        playbook_versions.append(
            {
                "version": version,
                "operators": sorted(operators),
                "capabilities": sorted(capabilities),
                "requires_input_contract": version == "capture-playbook-draft/v3",
                "requires_payload_contract": version
                in {
                    "capture-playbook-draft/v2",
                    "capture-playbook-draft/v3",
                },
            }
        )
    profile_bounds = raw_schema["$defs"]["artifact_output_contract"]["properties"]
    raw_limit = profile_bounds["max_bytes"]["maximum"]
    response_limit = raw_schema["$defs"]["budgets"]["properties"]["max_response_bytes"][
        "maximum"
    ]
    media_item = profile_bounds["media_types"]["items"]
    raw_profile = {
        "playbook_version": "capture-playbook-draft/v4",
        "runtime_profile": LIVE_PROFILE["runtime_profile"],
        "output_modality": "artifact",
        "max_artifact_bytes": raw_limit,
        "max_response_bytes": response_limit,
        "media_type_pattern": media_item.get("pattern"),
        **RAW_V4_CONSTRAINTS,
        "supported_operations": sorted(RAW_V4_OPERATORS),
    }
    return {
        "catalog_version": "bc15-authoring-capabilities/v1",
        "notice": "Capabilities describe compiler support; they do not grant source authorization or release approval.",
        "backends": profiles,
        "playbook_versions": playbook_versions,
        "parameterized_profile": {
            "playbook_version": "capture-playbook-draft/v3",
            "max_scalar_query_bindings": PARAMETERIZED_QUERY_MAX_KEYS,
            "supports_pagination": False,
        },
        "raw_artifact_profile": raw_profile,
    }
