"""JSON Schema boundary; never imported by the domain."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[3]


def validate(name: str, payload: dict):
    schema = json.loads(
        (ROOT / "contracts" / "scout" / "v1" / f"{name}.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def execute(app, request: dict, principal: str, fixture: bytes, now):
    """Public request boundary; fixture bytes must be controlled and sanitised."""
    validate("ScoutRequest", request)
    return app.run(request, principal, fixture, now)
