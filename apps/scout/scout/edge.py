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


def validate_request(request: dict) -> None:
    """Validate the public request projection before the application is called."""
    validate("ScoutRequest", request)
