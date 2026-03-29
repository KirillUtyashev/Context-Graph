"""Versioned schema registry for provenance extraction and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping

from .models import ObservationKind, observation_kind_value

ExtractionMode = Literal["observation_only", "generic_claims", "specialized_claims"]
PromptProfile = Literal[
    "generic_observation",
    "commit_message",
    "github_pr_body",
    "github_issue_body",
    "github_pr_review",
    "github_pr_review_comment",
    "github_issue_comment",
]

_COMMIT_ALLOWED_CLAIM_KINDS = frozenset(
    {
        "change_claim",
        "state_claim",
        "bug_report",
        "concern",
        "scope_statement",
        "test_expectation",
    }
)
_PULL_REQUEST_ALLOWED_CLAIM_KINDS = frozenset(
    {
        "change_claim",
        "state_claim",
        "bug_report",
        "requirement",
        "review_feedback",
        "approval",
        "concern",
        "workaround",
        "scope_statement",
        "test_expectation",
    }
)
_ISSUE_ALLOWED_CLAIM_KINDS = frozenset(
    {
        "state_claim",
        "bug_report",
        "requirement",
        "concern",
        "workaround",
        "scope_statement",
        "test_expectation",
    }
)


@dataclass(frozen=True, slots=True)
class ObservationSourcePolicy:
    observation_kind: str
    source_kind: str
    source_record_type: str
    default_context_kind: str | None
    extraction_mode: ExtractionMode
    prompt_profile: PromptProfile | None
    allowed_claim_kinds: frozenset[str] | None = None


def _builtin_observation_source_policies() -> dict[tuple[str, str], ObservationSourcePolicy]:
    return {
        (
            ObservationKind.GIT_COMMIT_MESSAGE.value,
            "commit",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.GIT_COMMIT_MESSAGE.value,
            source_kind="commit",
            source_record_type="commit",
            default_context_kind="commit",
            extraction_mode="specialized_claims",
            prompt_profile="commit_message",
            allowed_claim_kinds=_COMMIT_ALLOWED_CLAIM_KINDS,
        ),
        (
            ObservationKind.GITHUB_PR_BODY.value,
            "pull_request",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.GITHUB_PR_BODY.value,
            source_kind="pull_request",
            source_record_type="github_pull_request",
            default_context_kind="pull_request",
            extraction_mode="specialized_claims",
            prompt_profile="github_pr_body",
            allowed_claim_kinds=_PULL_REQUEST_ALLOWED_CLAIM_KINDS,
        ),
        (
            ObservationKind.GITHUB_ISSUE_BODY.value,
            "issue",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.GITHUB_ISSUE_BODY.value,
            source_kind="issue",
            source_record_type="github_issue",
            default_context_kind="issue",
            extraction_mode="specialized_claims",
            prompt_profile="github_issue_body",
            allowed_claim_kinds=_ISSUE_ALLOWED_CLAIM_KINDS,
        ),
        (
            ObservationKind.GITHUB_ISSUE_CLOSED.value,
            "issue",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.GITHUB_ISSUE_CLOSED.value,
            source_kind="issue",
            source_record_type="github_issue",
            default_context_kind="issue",
            extraction_mode="observation_only",
            prompt_profile=None,
            allowed_claim_kinds=_ISSUE_ALLOWED_CLAIM_KINDS,
        ),
        (
            ObservationKind.GITHUB_PR_REVIEW.value,
            "pull_request",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.GITHUB_PR_REVIEW.value,
            source_kind="pull_request",
            source_record_type="github_pull_request_review",
            default_context_kind="pull_request",
            extraction_mode="specialized_claims",
            prompt_profile="github_pr_review",
            allowed_claim_kinds=_PULL_REQUEST_ALLOWED_CLAIM_KINDS,
        ),
        (
            ObservationKind.GITHUB_PR_REVIEW_COMMENT.value,
            "pull_request",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.GITHUB_PR_REVIEW_COMMENT.value,
            source_kind="pull_request",
            source_record_type="github_pull_request_review_comment",
            default_context_kind="pull_request",
            extraction_mode="specialized_claims",
            prompt_profile="github_pr_review_comment",
            allowed_claim_kinds=_PULL_REQUEST_ALLOWED_CLAIM_KINDS,
        ),
        (
            ObservationKind.GITHUB_ISSUE_COMMENT.value,
            "issue",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.GITHUB_ISSUE_COMMENT.value,
            source_kind="issue",
            source_record_type="github_issue_comment",
            default_context_kind="issue",
            extraction_mode="specialized_claims",
            prompt_profile="github_issue_comment",
            allowed_claim_kinds=_PULL_REQUEST_ALLOWED_CLAIM_KINDS | _ISSUE_ALLOWED_CLAIM_KINDS,
        ),
        (
            ObservationKind.GITHUB_ISSUE_COMMENT.value,
            "pull_request",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.GITHUB_ISSUE_COMMENT.value,
            source_kind="pull_request",
            source_record_type="github_issue_comment",
            default_context_kind="pull_request",
            extraction_mode="specialized_claims",
            prompt_profile="github_issue_comment",
            allowed_claim_kinds=_PULL_REQUEST_ALLOWED_CLAIM_KINDS,
        ),
        (
            ObservationKind.MARKDOWN_DOCUMENT.value,
            "document",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.MARKDOWN_DOCUMENT.value,
            source_kind="document",
            source_record_type="code_node",
            default_context_kind="document",
            extraction_mode="generic_claims",
            prompt_profile="generic_observation",
            allowed_claim_kinds=None,
        ),
        (
            ObservationKind.AGENT_NOTE.value,
            "agent",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.AGENT_NOTE.value,
            source_kind="agent",
            source_record_type="observation_source",
            default_context_kind="agent_task",
            extraction_mode="generic_claims",
            prompt_profile="generic_observation",
            allowed_claim_kinds=None,
        ),
        (
            ObservationKind.AGENT_TRACE.value,
            "agent",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.AGENT_TRACE.value,
            source_kind="agent",
            source_record_type="observation_source",
            default_context_kind="agent_task",
            extraction_mode="observation_only",
            prompt_profile=None,
            allowed_claim_kinds=None,
        ),
        (
            ObservationKind.STACK_TRACE.value,
            "agent",
        ): ObservationSourcePolicy(
            observation_kind=ObservationKind.STACK_TRACE.value,
            source_kind="agent",
            source_record_type="observation_source",
            default_context_kind="agent_task",
            extraction_mode="observation_only",
            prompt_profile=None,
            allowed_claim_kinds=None,
        ),
    }


@dataclass(frozen=True, slots=True)
class ProvenanceSchemaRegistry:
    version: str = "v1"
    claim_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "change_claim",
                "state_claim",
                "bug_report",
                "requirement",
                "review_feedback",
                "approval",
                "concern",
                "workaround",
                "scope_statement",
                "test_expectation",
            }
        )
    )
    evidence_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "change_summary",
                "state_detail",
                "bug_signal",
                "requirement_detail",
                "review_feedback",
                "approval_signal",
                "concern",
                "workaround",
                "scope_detail",
                "test_expectation",
                "code_reference",
            }
        )
    )
    support_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "direct_statement",
                "quoted_feedback",
                "reproduction_detail",
                "approval_signal",
                "contextual_reference",
            }
        )
    )
    confidence_levels: frozenset[str] = field(
        default_factory=lambda: frozenset({"low", "medium", "high"})
    )
    claim_subkinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "bug_or_regression",
                "fix_or_mitigation",
                "invariant_or_constraint",
                "desired_behavior_or_requirement",
                "reviewer_objection_or_requested_change",
                "approval_or_acceptance",
                "workaround_or_temporary_mitigation",
                "test_result_or_validation",
                "rollout_migration_or_dependency",
                "ownership_or_impact_scope",
            }
        )
    )
    assertion_statuses: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"reported", "proposed", "requested", "observed", "confirmed", "rejected"}
        )
    )
    claim_frames: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "author_statement",
                "reviewer_feedback",
                "user_requirement",
                "reported_observation",
                "observed_result",
            }
        )
    )
    support_sources: frozenset[str] = field(
        default_factory=lambda: frozenset({"observation_text", "observation_title"})
    )
    support_roles: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "states_change",
                "states_rationale",
                "states_constraint",
                "reports_observation",
                "requests_change",
                "confirms_result",
                "rejects_prior_explanation",
                "provides_context",
            }
        )
    )
    claim_resolution_types: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "same_as",
                "refines",
                "contradicts",
                "explains",
                "supersedes",
                "mitigates",
                "depends_on",
                "supports",
                "unrelated",
            }
        )
    )
    observation_source_policies: Mapping[tuple[str, str], ObservationSourcePolicy] = field(
        default_factory=_builtin_observation_source_policies
    )

    def observation_policy(
        self,
        observation_kind: str | ObservationKind,
        *,
        source_kind: str | None = None,
    ) -> ObservationSourcePolicy | None:
        normalized_kind = observation_kind_value(observation_kind)
        if source_kind is not None:
            policy = self.observation_source_policies.get((normalized_kind, source_kind))
            if policy is not None:
                return policy
        matches = [
            policy
            for (kind, _), policy in self.observation_source_policies.items()
            if kind == normalized_kind
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def claim_kind_allowed(
        self,
        claim_kind: str,
        source_kind: str,
        *,
        observation_kind: str | ObservationKind | None = None,
    ) -> bool:
        if claim_kind not in self.claim_kinds:
            return False
        if observation_kind is None:
            return True
        policy = self.observation_policy(observation_kind, source_kind=source_kind)
        if policy is None or policy.allowed_claim_kinds is None:
            return True
        return claim_kind in policy.allowed_claim_kinds

    def claim_kind_for_subkind(self, claim_subkind: str, source_kind: str) -> str | None:
        if claim_subkind == "bug_or_regression":
            return "bug_report"
        if claim_subkind == "fix_or_mitigation":
            return "change_claim"
        if claim_subkind == "invariant_or_constraint":
            return "state_claim"
        if claim_subkind == "desired_behavior_or_requirement":
            return "requirement"
        if claim_subkind == "reviewer_objection_or_requested_change":
            return "review_feedback" if source_kind == "pull_request" else "concern"
        if claim_subkind == "approval_or_acceptance":
            return "approval"
        if claim_subkind == "workaround_or_temporary_mitigation":
            return "workaround"
        if claim_subkind == "test_result_or_validation":
            return "test_expectation"
        if claim_subkind == "rollout_migration_or_dependency":
            return "scope_statement"
        if claim_subkind == "ownership_or_impact_scope":
            return "scope_statement"
        return None

    def default_claim_subkind_for_claim_kind(self, claim_kind: str) -> str | None:
        mapping = {
            "bug_report": "bug_or_regression",
            "change_claim": "fix_or_mitigation",
            "state_claim": "invariant_or_constraint",
            "requirement": "desired_behavior_or_requirement",
            "review_feedback": "reviewer_objection_or_requested_change",
            "approval": "approval_or_acceptance",
            "concern": "reviewer_objection_or_requested_change",
            "workaround": "workaround_or_temporary_mitigation",
            "scope_statement": "rollout_migration_or_dependency",
            "test_expectation": "test_result_or_validation",
        }
        return mapping.get(claim_kind)


DEFAULT_PROVENANCE_SCHEMA_REGISTRY = ProvenanceSchemaRegistry()
