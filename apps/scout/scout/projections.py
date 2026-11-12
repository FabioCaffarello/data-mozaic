"""Stable wire projections owned by the Scout application boundary."""

from dataclasses import asdict

from .domain import PlaybookProposal


def projection(proposal: PlaybookProposal) -> dict:
    return {
        "contract_version": "1.0.0",
        "proposal_id": proposal.proposal_id,
        "session_id": proposal.session_id,
        "source": asdict(proposal.source),
        "summary": proposal.summary,
        "finding_ids": list(proposal.finding_ids),
        "evidence_manifest_ids": list(proposal.manifest_ids),
        "assumptions": list(proposal.assumptions),
        "risks": list(proposal.risks),
        "required_capabilities": list(proposal.required_capabilities),
    }
