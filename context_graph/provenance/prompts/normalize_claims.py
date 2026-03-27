"""Prompt builders for claim normalization from evidence candidates."""

from __future__ import annotations

from typing import Any

from context_graph.memory.prompts.models import Message
from context_graph.memory.prompts.prompt_helpers import to_prompt_json

from .base import ProvenancePromptMetadata

NORMALIZE_CLAIMS_FROM_EVIDENCE_SET = ProvenancePromptMetadata(
    name="provenance.normalize_claims.from_evidence_set",
    version="v1",
)


def from_evidence_set(context: dict[str, Any]) -> list[Message]:
    evidence = context["evidence"]
    target_context = context.get("target_context") or {}
    topic = context.get("topic")
    user_prompt = (
        "Topic:\n"
        f"{to_prompt_json(topic, indent=2)}\n\n"
        "Evidence candidates:\n"
        f"{to_prompt_json(evidence, indent=2)}\n\n"
        "Optional deterministic target context:\n"
        f"{to_prompt_json(target_context, indent=2)}\n\n"
        "Normalize these evidence items into concise claims. "
        "Each claim should preserve the supported meaning of the evidence set without adding unsupported detail."
    )
    return [
        Message(
            role="system",
            content=(
                "You derive normalized claims from bounded evidence candidates. "
                "Claims must stay faithful to the evidence and should remain specific rather than speculative."
            ),
        ),
        Message(role="user", content=user_prompt),
    ]
