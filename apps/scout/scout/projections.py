"""Stable wire projections owned by the Scout application boundary."""

from dataclasses import asdict

from .domain import PlaybookProposal


def projection(proposal: PlaybookProposal) -> dict:
    result = {
        "contract_version": "2.0.0"
        if proposal.candidate_specification is not None
        else "1.0.0",
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
    if proposal.candidate_specification is not None:
        result.update(
            proposal_revision=proposal.proposal_revision,
            candidate_specification=proposal.candidate_specification,
            candidate_sha256=proposal.candidate_sha256,
        )
    return result
