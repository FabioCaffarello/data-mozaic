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


def validate_transcript(transcript: dict) -> None:
    """Validate the versioned draft transcript DTO and embedded BC14 proposal."""
    if not isinstance(transcript, dict):
        raise TypeError("transcript must be a JSON object")
    versions = {
        "scout-transcript-draft/v1": "v1",
        "scout-transcript-draft/v2": "v2",
    }
    version = versions.get(transcript.get("transcript_schema"))
    if version is None:
        raise ValueError("unsupported Scout transcript draft version")
    schema = json.loads(
        (
            ROOT
            / f"contracts/scout/transcript/draft/{version}/ScoutSessionTranscript.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(transcript)
    validate("PlaybookProposal", transcript["proposal"])
    session = transcript["session"]
    proposal = transcript["proposal"]
    approval = transcript["approved_scope"]
    approval_record = transcript["approval_record"]
    if (
        session["source"]
        != {"source_id": approval["source_id"], "revision": approval["revision"]}
        or session["objective"] != transcript["objective"]
        or proposal["session_id"] != session["session_id"]
        or proposal["source"] != session["source"]
        or any(
            approval_record[field] != approval[field]
            for field in ("source_id", "revision", "origin", "path_prefixes", "budgets")
        )
    ):
        raise ValueError("transcript source, session or approval references mismatch")
    evidence_by_id = {item["artifact_id"]: item for item in transcript["evidence"]}
    if len(evidence_by_id) != len(transcript["evidence"]):
        raise ValueError("transcript contains duplicate artifact identities")
    manifest_ids = {item["manifest_id"] for item in transcript["evidence"]}
    if not set(proposal["evidence_manifest_ids"]) <= manifest_ids:
        raise ValueError("proposal references absent transcript manifests")
    finding_ids = {item["finding_id"] for item in transcript["findings"]}
    if len(finding_ids) != len(transcript["findings"]):
        raise ValueError("transcript contains duplicate finding identities")
    if not set(proposal["finding_ids"]) <= finding_ids:
        raise ValueError("proposal references absent transcript findings")
    for finding in transcript["findings"]:
        if not set(finding["evidence_refs"]) <= manifest_ids:
            raise ValueError("finding references absent transcript manifests")
    page_artifacts = set()
    for page in transcript["pages"]:
        evidence = evidence_by_id.get(page["artifact_id"])
        if (
            evidence is None
            or evidence["manifest_id"] != page["evidence_ref"]
            or evidence["sha256"] != page["sha256"]
            or evidence["size_bytes"] != page["size_bytes"]
            or page["evidence_ref"] not in proposal["evidence_manifest_ids"]
        ):
            raise ValueError("transcript page evidence reference mismatch")
        page_artifacts.add(page["artifact_id"])
    if page_artifacts != set(evidence_by_id):
        raise ValueError("transcript evidence is not linked to captured pages")
    if version == "v2":
        pages = transcript["pages"]
        navigation = transcript["navigation"]
        ordered = sorted(pages, key=lambda page: page["sequence"])
        sequences = [page["sequence"] for page in ordered]
        complete = sequences == list(range(1, navigation["observation_count"] + 1))
        if (
            len(sequences) != len(set(sequences))
            or sequences != sorted(sequences)
            or not sequences
            or navigation["observation_count"] < sequences[-1]
            or navigation["sequence_complete"] != complete
        ):
            raise ValueError(
                "transcript navigation sequence is incomplete or inconsistent"
            )
        observation_ids = {page["observation_id"] for page in ordered}
        if len(observation_ids) != len(ordered):
            raise ValueError("transcript contains duplicate observation identities")
        if complete and (
            ordered[0]["requested_url"] != navigation["start_requested_url"]
            or ordered[0]["url"] != navigation["start_effective_url"]
        ):
            raise ValueError(
                "transcript navigation start differs from its first observation"
            )
        for page in ordered:
            parents = page["discovered_from_observation_ids"]
            if len(parents) != len(set(parents)) or (
                complete and not set(parents) <= observation_ids
            ):
                raise ValueError("transcript discovery relation is invalid")
            if not complete:
                continue
            expected_parents = {
                parent["observation_id"]
                for parent in ordered
                if page["requested_url"] in parent["discovered_links"]
                and parent["sequence"] < page["sequence"]
            }
            if set(parents) != expected_parents:
                raise ValueError(
                    "transcript discovery relation does not match evidence"
                )
