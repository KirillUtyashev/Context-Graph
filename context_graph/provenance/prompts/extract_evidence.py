"""Prompt builders for evidence extraction from deterministic observations."""

from __future__ import annotations

from typing import Any

from context_graph.memory.prompts.models import Message
from context_graph.memory.prompts.prompt_helpers import to_prompt_json

from .base import ProvenancePromptMetadata

EXTRACT_EVIDENCE_FROM_COMMIT_MESSAGE = ProvenancePromptMetadata(
    name="provenance.extract_evidence.from_commit_message",
    version="v1",
)
EXTRACT_EVIDENCE_FROM_GITHUB_PR_BODY = ProvenancePromptMetadata(
    name="provenance.extract_evidence.from_github_pr_body",
    version="v1",
)
EXTRACT_EVIDENCE_FROM_GITHUB_ISSUE_BODY = ProvenancePromptMetadata(
    name="provenance.extract_evidence.from_github_issue_body",
    version="v1",
)
EXTRACT_EVIDENCE_FROM_GITHUB_PR_REVIEW = ProvenancePromptMetadata(
    name="provenance.extract_evidence.from_github_pr_review",
    version="v1",
)
EXTRACT_EVIDENCE_FROM_GITHUB_PR_REVIEW_COMMENT = ProvenancePromptMetadata(
    name="provenance.extract_evidence.from_github_pr_review_comment",
    version="v1",
)
EXTRACT_EVIDENCE_FROM_GITHUB_ISSUE_COMMENT = ProvenancePromptMetadata(
    name="provenance.extract_evidence.from_github_issue_comment",
    version="v1",
)
EXTRACT_EVIDENCE_FROM_GENERIC_OBSERVATION = ProvenancePromptMetadata(
    name="provenance.extract_evidence.from_generic_observation",
    version="v1",
)


def from_commit_message(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract evidence candidates from one git commit message. "
            "Return only evidence directly grounded in the provided observation text. "
            "Do not invent code behavior, user intent, or architectural conclusions."
        ),
        context=context,
    )


def from_github_pr_body(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract evidence candidates from one GitHub pull request body. "
            "Focus on explicit change descriptions, constraints, rollout notes, and linked technical context."
        ),
        context=context,
    )


def from_github_issue_body(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract evidence candidates from one GitHub issue body. "
            "Focus on explicit bug reports, reproduction details, desired behavior, and scope statements."
        ),
        context=context,
    )


def from_github_pr_review(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract evidence candidates from one GitHub pull request review. "
            "Focus on explicit approval rationale, requested changes, blocking concerns, and technical feedback."
        ),
        context=context,
    )


def from_github_pr_review_comment(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract evidence candidates from one GitHub pull request review comment. "
            "Focus on explicit line-level feedback, concerns, and suggested changes."
        ),
        context=context,
    )


def from_github_issue_comment(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract evidence candidates from one GitHub issue comment. "
            "Focus on explicit reproductions, confirmations, scope clarifications, and workaround details."
        ),
        context=context,
    )


def from_generic_observation(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract evidence candidates from one raw provenance observation. "
            "Only return evidence that is explicitly supported by the quoted observation."
        ),
        context=context,
    )


def _build_messages(*, system_prompt: str, context: dict[str, Any]) -> list[Message]:
    observation = context["observation"]
    code_context = context.get("code_context") or {}
    source_context = context.get("source_context") or {}
    user_prompt = (
        "Observation:\n"
        f"{to_prompt_json(observation, indent=2)}\n\n"
        "Optional deterministic code context:\n"
        f"{to_prompt_json(code_context, indent=2)}\n\n"
        "Optional source context:\n"
        f"{to_prompt_json(source_context, indent=2)}\n\n"
        "Extract bounded evidence candidates. Each candidate should preserve an exact quote when possible, "
        "summarize the evidence in one sentence, and include any grounded about-hints you can justify."
    )
    return [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]
