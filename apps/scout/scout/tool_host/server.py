"""Thin MCP STDIO adapter; identity and ports come from trusted composition."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool

from ..application import ScoutApplication
from ..domain import SourceRef
from ..edge import validate_transcript

_LOG = logging.getLogger(__name__)


def create_server(application: ScoutApplication, principal: str) -> Server:
    """Create a tool server bound to launcher-owned local identity and adapters."""
    if not principal:
        raise ValueError("local development principal is required")
    server = Server("data-mozaic-scout")

    def now() -> datetime:
        return datetime.now(UTC)

    def command_identity(tool_name: str, arguments: dict) -> tuple[str, str, str]:
        canonical = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        fingerprint = hashlib.sha256(canonical).hexdigest()
        slot = {"principal": principal, "tool": tool_name}
        if tool_name == "scout_start":
            slot["arguments"] = arguments
        else:
            slot["session_id"] = arguments["session_id"]
            slot["expected_version"] = arguments["expected_version"]
        slot_digest = hashlib.sha256(
            json.dumps(slot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        key = f"mcp:{tool_name}:{slot_digest}"
        return "cmd-" + slot_digest, key, fingerprint

    def failure(error: Exception) -> dict:
        from ..domain import AuthorizationError, ConflictError, IntegrityError

        if isinstance(error, AuthorizationError):
            code = "authorization_denied"
        elif isinstance(error, ConflictError):
            code = "version_or_idempotency_conflict"
        elif isinstance(error, IntegrityError):
            code = "invalid_command"
        else:
            code = "operation_failed"
        _LOG.warning("scout.tool.failed code=%s type=%s", code, type(error).__name__)
        return {"ok": False, "error": {"code": code}}

    tools = [
        Tool(
            name="scout_preflight",
            description="Resolve a registered source revision and approved limits.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "revision": {"type": "string"},
                },
                "required": ["source_id", "revision"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scout_start",
            description="Start an authorized Scout session with host-bound identity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "revision": {"type": "string"},
                    "objective": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "required": ["source_id", "revision", "objective"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scout_record_finding",
            description="Append a finding referencing sealed manifests in this session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "expected_version": {"type": "integer", "minimum": 0},
                    "statement": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "epistemic_status": {"enum": ["observation", "hypothesis"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "manifest_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                        "uniqueItems": True,
                    },
                    "verification_note": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2000,
                    },
                },
                "required": [
                    "session_id",
                    "expected_version",
                    "statement",
                    "epistemic_status",
                    "confidence",
                    "manifest_ids",
                    "verification_note",
                ],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scout_explore_fixture",
            description="Explore one host-registered hermetic fixture; this tool has no network transport.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "expected_version": {"type": "integer", "minimum": 0},
                    "fixture_id": {
                        "type": "string",
                        "enum": ["catalog-home", "catalog-page-2"],
                    },
                },
                "required": ["session_id", "expected_version", "fixture_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scout_propose",
            description="Submit a BC14 proposal through the durable BC15 outbox.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "expected_version": {"type": "integer", "minimum": 0},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "finding_ids": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "minItems": 1,
                        "maxItems": 50,
                        "uniqueItems": True,
                    },
                    "manifest_ids": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "minItems": 1,
                        "maxItems": 20,
                        "uniqueItems": True,
                    },
                    "assumptions": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 1000},
                        "maxItems": 20,
                    },
                    "risks": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 1000},
                        "maxItems": 20,
                    },
                    "required_capabilities": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "maxItems": 10,
                        "uniqueItems": True,
                    },
                },
                "required": [
                    "session_id",
                    "expected_version",
                    "summary",
                    "finding_ids",
                    "manifest_ids",
                    "assumptions",
                    "risks",
                    "required_capabilities",
                ],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scout_status",
            description="Return an authorized redacted status; a session ID grants no access.",
            inputSchema={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scout_export_transcript",
            description="Export a reauthorized, metadata-only BC14 transcript and SHA-256.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1, "maxLength": 128}
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        ),
    ]
    by_name = {tool.name: tool for tool in tools}

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tools

    @server.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict) -> dict:
        try:
            if (
                len(
                    json.dumps(
                        arguments, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                )
                > 32_000
            ):
                raise ValueError("tool arguments exceed the 32 KB limit")
            if name == "scout_preflight":
                source = SourceRef(arguments["source_id"], arguments["revision"])
                registered = application.sources.resolve(source)
                decision = application.policy.decide(principal, source, now())
                effective = registered.intersect(decision.scope)
                return {
                    "ok": True,
                    "source": {
                        "source_id": source.source_id,
                        "revision": source.revision,
                    },
                    "origins": list(effective.origins),
                    "path_prefixes": list(effective.path_prefixes),
                    "methods": list(effective.methods),
                    "max_requests": effective.max_requests,
                    "max_duration_seconds": effective.max_duration_seconds,
                    "approval_expires_at": decision.expires_at.isoformat(),
                }
            if name == "scout_start":
                command_id, _, _ = command_identity(name, arguments)
                session = application.start_incremental(
                    source=SourceRef(arguments["source_id"], arguments["revision"]),
                    objective=arguments["objective"],
                    principal=principal,
                    command_id=command_id,
                    now=now(),
                )
                return {
                    "ok": True,
                    "session_id": session.session_id,
                    "version": session.version,
                    "state": session.state.value,
                }
            if name == "scout_record_finding":
                command_id, key, fingerprint = command_identity(name, arguments)
                session = application.record_finding_incremental(
                    session_id=arguments["session_id"],
                    principal=principal,
                    command_id=command_id,
                    idempotency_key=key,
                    fingerprint=fingerprint,
                    expected_version=arguments["expected_version"],
                    statement=arguments["statement"],
                    epistemic_status=arguments["epistemic_status"],
                    confidence=arguments["confidence"],
                    manifest_ids=tuple(arguments["manifest_ids"]),
                    verification_note=arguments["verification_note"],
                    now=now(),
                )
                return {
                    "ok": True,
                    "version": session.version,
                    "finding_ids": [item.finding_id for item in session.findings],
                }
            if name == "scout_explore_fixture":
                command_id, key, fingerprint = command_identity(name, arguments)
                session = application.explore_fixture_incremental(
                    session_id=arguments["session_id"],
                    principal=principal,
                    command_id=command_id,
                    idempotency_key=key,
                    fingerprint=fingerprint,
                    expected_version=arguments["expected_version"],
                    fixture_id=arguments["fixture_id"],
                    now=now(),
                )
                observation = session.observations[-1]
                return {
                    "ok": True,
                    "version": session.version,
                    "observation_id": observation.observation_id,
                    "manifest_id": session.manifests[-1].manifest_id,
                    "title": observation.title,
                    "status": observation.status,
                    "content_type": observation.content_type,
                    "links": list(observation.links),
                    "body_included": False,
                }
            if name == "scout_propose":
                command_id, key, fingerprint = command_identity(name, arguments)
                session = application.submit_proposal_incremental(
                    session_id=arguments["session_id"],
                    principal=principal,
                    expected_version=arguments["expected_version"],
                    command_id=command_id,
                    idempotency_key=key,
                    fingerprint=fingerprint,
                    summary=arguments["summary"],
                    finding_ids=tuple(arguments["finding_ids"]),
                    manifest_ids=tuple(arguments["manifest_ids"]),
                    assumptions=tuple(arguments["assumptions"]),
                    risks=tuple(arguments["risks"]),
                    required_capabilities=tuple(arguments["required_capabilities"]),
                    now=now(),
                )
                return {
                    "ok": True,
                    "proposal_id": session.proposal.proposal_id,
                    "version": session.version,
                    "handoff": "delivery_pending",
                }
            if name == "scout_status":
                return {
                    "ok": True,
                    **application.status_incremental(
                        arguments["session_id"], principal, now()
                    ),
                }
            if name == "scout_export_transcript":
                transcript = application.export_transcript(
                    arguments["session_id"], principal, now()
                )
                validate_transcript(transcript)
                rendered = (
                    json.dumps(
                        transcript,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                encoded = rendered.encode("utf-8")
                if len(encoded) > 256_000:
                    raise ValueError("transcript exceeds MCP output limit")
                return {
                    "ok": True,
                    "transcript_json": rendered,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            return {"ok": False, "error": {"code": "unknown_tool"}}
        except Exception as error:  # noqa: BLE001 - adapter returns typed tool failures
            return failure(error)

    # The SDK validates schema before invoking call_tool. The explicit name guard
    # also makes unknown tools a typed denial if a client bypasses tools/list.
    del by_name
    return server


async def run_stdio(server: Server) -> None:
    """Run the official SDK STDIO transport; stdout remains protocol-only."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def serve(server: Server) -> None:
    anyio.run(run_stdio, server)
