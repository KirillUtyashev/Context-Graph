"""Prompt builders for bounded fact contradiction decisions."""

from __future__ import annotations

from typing import Any

from context_graph.prompts.models import Message
from context_graph.prompts.prompt_helpers import to_prompt_json

from .base import ProvenancePromptMetadata

RESOLVE_FACT_CONFLICTS_COMPARE = ProvenancePromptMetadata(
    name="provenance.resolve_fact_conflicts.compare_against_current",
    version="v1",
)


def compare_against_current(context: dict[str, Any]) -> list[Message]:
    new_fact = context["new_fact"]
    current_facts = context["current_facts"]
    user_prompt = (
        "New fact:\n"
        f"{to_prompt_json(new_fact, indent=2)}\n\n"
        "Current fact candidates:\n"
        f"{to_prompt_json(current_facts, indent=2)}\n\n"
        "Return the candidate ids whose facts are contradicted by the new fact. "
        "Mark contradictions only when the two facts cannot both remain current for the same grounded code target. "
        "Do not mark duplicates or merely related facts as contradictions."
    )
    return [
        Message(
            role="system",
            content=(
                "You compare one new coding fact against a small set of current fact candidates. "
                "Be conservative: only mark direct contradictions or clear supersession updates."
            ),
        ),
        Message(role="user", content=user_prompt),
    ]
