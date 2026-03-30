"""Prompt builders for joint claim and evidence extraction from observations."""

from __future__ import annotations

from typing import Any

from context_graph.prompts.models import Message
from context_graph.prompts.prompt_helpers import to_prompt_json
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
            "You extract grounded candidate claim instances and supporting evidence from one git commit message. "
            "Focus on code changes, fixes, mitigations, scope, constraints, and linked intent. "
            "Treat the message as an authored statement about the change, not as an excuse to infer unstated repo-wide truths."
        ),
        context=context,
    )


def from_github_pr_body(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract grounded candidate claim instances and supporting evidence from one GitHub pull request body. "
            "Focus on change descriptions, rationale, constraints, rollout notes, migration/dependency details, and claimed fixes. "
            "Treat the PR body as an authored statement, not ground truth."
        ),
        context=context,
    )


def from_github_issue_body(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract grounded candidate claim instances and supporting evidence from one GitHub issue body. "
            "Focus on reported bugs, reproductions, expected behavior, requirements, and impact scope. "
            "Treat the issue body as a report, not a confirmation that the reported condition is true."
        ),
        context=context,
    )


def from_github_pr_review(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract grounded candidate claim instances and supporting evidence from one GitHub pull request review. "
            "Focus on objections, requested changes, approvals, blocking concerns, invariant violations, and review rationale. "
            "Treat the review as reviewer feedback rather than an objective statement of repository truth."
        ),
        context=context,
    )


def from_github_pr_review_comment(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract grounded candidate claim instances and supporting evidence from one GitHub pull request review comment. "
            "Focus on line-level objections, requested changes, invariant violations, concerns, and suggested fixes."
        ),
        context=context,
    )


def from_github_issue_comment(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract grounded candidate claim instances and supporting evidence from one GitHub issue comment. "
            "Focus on confirmations, workarounds, scope narrowing, correction statements, additional reproductions, and follow-up rationale."
        ),
        context=context,
    )


def from_generic_observation(context: dict[str, Any]) -> list[Message]:
    return _build_messages(
        system_prompt=(
            "You extract grounded candidate claim instances and supporting evidence from one raw provenance observation. "
            "Only return claims explicitly supported by the provided observation."
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
        "Extract up to 5 candidate claim instances from this single observation.\n\n"
        "Rules:\n"
        "- Only extract claims explicitly supported by the observation.\n"
        "- Keep independent facts separate; do not merge multiple claims into one summary.\n"
        "- Do not infer repo-wide truths, hidden intent, or unstated causality.\n"
        "- If the observation contains no strong coding-relevant claim, return an empty list.\n"
        "- Coarse claim_kind is derived by the pipeline; focus on claim_subkind, assertion_status, claim_frame, and exact support.\n"
        "- Evidence may only come from observation text or title. Do not quote code_context or source_context.\n"
        "- Every claim must include 1-3 exact supporting evidence items.\n"
        "- For each support item, explain briefly how that quote supports this claim.\n"
        "- If the observation explicitly corrects or rejects an earlier explanation, emit the replacement claim normally.\n"
        "- Emit a rejected claim only when the rejected proposition is explicitly stated in the observation.\n"
        "- If the text says maybe, might, I think, or similar, do not emit the claim as confirmed or observed.\n\n"
        "Prefer claim instances of these subkinds:\n"
        "- bug_or_regression\n"
        "- fix_or_mitigation\n"
        "- invariant_or_constraint\n"
        "- desired_behavior_or_requirement\n"
        "- reviewer_objection_or_requested_change\n"
        "- approval_or_acceptance\n"
        "- workaround_or_temporary_mitigation\n"
        "- test_result_or_validation\n"
        "- rollout_migration_or_dependency\n"
        "- ownership_or_impact_scope\n\n"
        f"Allowed claim subkinds: {sorted(registry.claim_subkinds)}\n"
        f"Allowed assertion statuses: {sorted(registry.assertion_statuses)}\n"
        f"Allowed claim frames: {sorted(registry.claim_frames)}\n"
        f"Allowed evidence kinds: {sorted(registry.evidence_kinds)}\n"
        f"Allowed support kinds: {sorted(registry.support_kinds)}\n"
        f"Allowed support sources: {sorted(registry.support_sources)}\n"
        f"Allowed support roles: {sorted(registry.support_roles)}\n"
        f"Allowed confidence levels: {sorted(registry.confidence_levels)}\n\n"
        "For each claim:\n"
        "1. Write a short normalized claim text.\n"
        "2. Choose one claim_subkind.\n"
        "3. Choose one assertion_status.\n"
        "4. Choose one claim_frame.\n"
        "5. Provide 1-3 exact supporting evidence items.\n"
        "6. For each support item, choose one support_kind from the allowed support kinds.\n"
        "7. For each support item, set support_source to observation_text or observation_title.\n"
        "8. For each support item, choose one support_role from the allowed support roles and write a short support_rationale.\n"
        "9. Do not put support_role values into support_kind. For example, states_rationale and states_constraint are support_role values, not support_kind values.\n"
        "10. Use spans only for observation_text support; keep title support spans null.\n"
        "11. summary_text is optional legacy context and should not be used instead of support_rationale.\n"
        "12. Assign confidence based only on how directly the observation supports the claim."
    )
    return [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]
