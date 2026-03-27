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
