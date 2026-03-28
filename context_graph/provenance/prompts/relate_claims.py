"""Prompt builders for bounded claim-comparison decisions."""

from __future__ import annotations

from typing import Any

from context_graph.memory.prompts.models import Message
from context_graph.memory.prompts.prompt_helpers import to_prompt_json

from .base import ProvenancePromptMetadata

RELATE_CLAIMS_COMPARE = ProvenancePromptMetadata(
    name="provenance.relate_claims.compare_claims",
    version="v1",
)
RESOLVE_NEW_CLAIM_AGAINST_CANDIDATES = ProvenancePromptMetadata(
    name="provenance.relate_claims.resolve_new_claim_against_candidates",
    version="v1",
)


def compare_claims(context: dict[str, Any]) -> list[Message]:
    claims = context["claims"]
    user_prompt = (
        "Claims to compare:\n"
        f"{to_prompt_json(claims, indent=2)}\n\n"
        "Compare these claims and decide whether each pair contradicts, supersedes, supports, or is unrelated. "
        "Base the decision only on the provided claim content."
    )
    return [
        Message(
            role="system",
            content=(
                "You compare small sets of claims and classify their relationship. "
                "Be conservative: choose unrelated unless the relationship is clearly supported."
            ),
        ),
        Message(role="user", content=user_prompt),
    ]


def resolve_new_claim_against_candidates(context: dict[str, Any]) -> list[Message]:
    new_claim = context["new_claim"]
    candidates = context["candidates"]
    user_prompt = (
        "New claim:\n"
        f"{to_prompt_json(new_claim, indent=2)}\n\n"
        "Candidate existing claims:\n"
        f"{to_prompt_json(candidates, indent=2)}\n\n"
        "For each candidate claim, classify the relationship from the new claim to the existing candidate. "
        "Only use one of: same_as, refines, contradicts, explains, supersedes, mitigates, depends_on, supports, unrelated. "
        "Return only high-confidence non-unrelated relations when clearly justified; otherwise choose unrelated."
    )
    return [
        Message(
            role="system",
            content=(
                "You resolve one new claim against a bounded set of existing claim candidates. "
                "Be conservative and avoid merges unless the claims clearly express the same scoped proposition."
            ),
        ),
        Message(role="user", content=user_prompt),
    ]
