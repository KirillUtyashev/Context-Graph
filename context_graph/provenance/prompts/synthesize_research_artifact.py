"""Prompt builders for bounded research artifact synthesis."""

from __future__ import annotations

from typing import Any

from context_graph.prompts.models import Message
from context_graph.prompts.prompt_helpers import to_prompt_json

from .base import ProvenancePromptMetadata

SYNTHESIZE_RESEARCH_ARTIFACT_FROM_CODE_AND_EVIDENCE = ProvenancePromptMetadata(
    name="provenance.synthesize_research_artifact.from_code_and_evidence",
    version="v1",
)


def from_code_and_evidence(context: dict[str, Any]) -> list[Message]:
    user_prompt = (
        "Repo region:\n"
        f"{to_prompt_json(context.get('repo_region'), indent=2)}\n\n"
        "Deterministic code context:\n"
        f"{to_prompt_json(context.get('code_context') or {}, indent=2)}\n\n"
        "Evidence context:\n"
        f"{to_prompt_json(context.get('evidence') or [], indent=2)}\n\n"
        "Claim context:\n"
        f"{to_prompt_json(context.get('claims') or [], indent=2)}\n\n"
        "Synthesize a bounded research artifact that stays grounded in the supplied code, evidence, and claims."
    )
    return [
        Message(
            role="system",
            content=(
                "You synthesize concise research artifacts grounded in deterministic code context and evidence-backed claims. "
                "Do not invent unsupported architecture or behavior."
            ),
        ),
        Message(role="user", content=user_prompt),
    ]
