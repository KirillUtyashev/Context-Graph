"""Prompt builders for joint claim and evidence extraction from observations."""

from __future__ import annotations

from typing import Any

from context_graph.memory.prompts.models import Message
from context_graph.memory.prompts.prompt_helpers import to_prompt_json
from context_graph.provenance.schema_registry import DEFAULT_PROVENANCE_SCHEMA_REGISTRY

from .base import ProvenancePromptMetadata

EXTRACT_CLAIMS_AND_EVIDENCE_FROM_COMMIT_MESSAGE = ProvenancePromptMetadata(
    name="provenance.extract_claims_and_evidence.from_commit_message",
    version="v1",
)
EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_BODY = ProvenancePromptMetadata(
    name="provenance.extract_claims_and_evidence.from_github_pr_body",
    version="v1",
)
EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_ISSUE_BODY = ProvenancePromptMetadata(
    name="provenance.extract_claims_and_evidence.from_github_issue_body",
    version="v1",
)
EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_REVIEW = ProvenancePromptMetadata(
    name="provenance.extract_claims_and_evidence.from_github_pr_review",
    version="v1",
)
EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_REVIEW_COMMENT = ProvenancePromptMetadata(
    name="provenance.extract_claims_and_evidence.from_github_pr_review_comment",
    version="v1",
)
EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_ISSUE_COMMENT = ProvenancePromptMetadata(
    name="provenance.extract_claims_and_evidence.from_github_issue_comment",
    version="v1",
)
EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GENERIC_OBSERVATION = ProvenancePromptMetadata(
    name="provenance.extract_claims_and_evidence.from_generic_observation",
    version="v1",
)


def from_commit_message(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract candidate claims and their supporting evidence from one git commit message. "
            "Only return claims that are directly supported by the observation text."
        ),
        context=context,
    )


def from_github_pr_body(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract candidate claims and their supporting evidence from one GitHub pull request body. "
            "Focus on explicit change descriptions, constraints, rollout notes, and linked technical context."
        ),
        context=context,
    )


def from_github_issue_body(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract candidate claims and their supporting evidence from one GitHub issue body. "
            "Focus on explicit bug reports, reproduction details, desired behavior, and scope statements."
        ),
        context=context,
    )


def from_github_pr_review(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract candidate claims and their supporting evidence from one GitHub pull request review. "
            "Focus on explicit approval rationale, requested changes, blocking concerns, and technical feedback."
        ),
        context=context,
    )


def from_github_pr_review_comment(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract candidate claims and their supporting evidence from one GitHub pull request review comment. "
            "Focus on explicit line-level feedback, concerns, and suggested changes."
        ),
        context=context,
    )


def from_github_issue_comment(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract candidate claims and their supporting evidence from one GitHub issue comment. "
            "Focus on explicit reproductions, confirmations, scope clarifications, and workaround details."
        ),
        context=context,
    )


def from_generic_observation(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract candidate claims and their supporting evidence from one raw provenance observation. "
            "Only return claims that are explicitly supported by the provided source text."
        ),
        context=context,
    )


def _build_messages(*, system_prompt: str, context: dict[str, Any]) -> list[Message]:
    observation = context["observation"]
    code_context = context.get("code_context") or {}
    source_context = context.get("source_context") or {}
    registry = DEFAULT_PROVENANCE_SCHEMA_REGISTRY
    user_prompt = (
        "Observation:\n"
        f"{to_prompt_json(observation, indent=2)}\n\n"
        "Optional deterministic code context:\n"
        f"{to_prompt_json(code_context, indent=2)}\n\n"
        "Optional source context:\n"
        f"{to_prompt_json(source_context, indent=2)}\n\n"
        "Extract up to 5 candidate claims useful for future coding tasks. For each claim, extract the exact "
        "supporting evidence from the observation and keep it tightly grounded.\n\n"
        f"Allowed claim kinds: {sorted(registry.claim_kinds)}\n"
        f"Allowed evidence kinds: {sorted(registry.evidence_kinds)}\n"
        f"Allowed support kinds: {sorted(registry.support_kinds)}\n"
        f"Allowed confidence levels: {sorted(registry.confidence_levels)}\n\n"
        "Each claim must include supporting evidence spans and must not be speculative."
    )
    return [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]
