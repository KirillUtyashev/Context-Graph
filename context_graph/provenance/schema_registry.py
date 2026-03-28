"""Versioned schema registry for provenance extraction and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


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
    allowed_sources_by_claim_kind: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: {
            "change_claim": frozenset({"commit", "pull_request"}),
            "state_claim": frozenset({"commit", "pull_request", "issue"}),
            "bug_report": frozenset({"commit", "pull_request", "issue"}),
            "requirement": frozenset({"pull_request", "issue"}),
            "review_feedback": frozenset({"pull_request"}),
            "approval": frozenset({"pull_request"}),
            "concern": frozenset({"commit", "pull_request", "issue"}),
            "workaround": frozenset({"pull_request", "issue"}),
            "scope_statement": frozenset({"commit", "pull_request", "issue"}),
            "test_expectation": frozenset({"commit", "pull_request", "issue"}),
        }
    )
    prompt_names: Mapping[str, str] = field(
        default_factory=lambda: {
            "commit": "provenance.extract_claims_and_evidence.from_commit_message.v1",
            "pull_request": "provenance.extract_claims_and_evidence.from_github_pr_body.v1",
            "issue": "provenance.extract_claims_and_evidence.from_github_issue_body.v1",
        }
    )

    def claim_kind_allowed(self, claim_kind: str, source_kind: str) -> bool:
        return source_kind in self.allowed_sources_by_claim_kind.get(claim_kind, frozenset())

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
            "approval": "reviewer_objection_or_requested_change",
            "concern": "reviewer_objection_or_requested_change",
            "workaround": "fix_or_mitigation",
            "scope_statement": "rollout_migration_or_dependency",
            "test_expectation": "test_result_or_validation",
        }
        return mapping.get(claim_kind)


DEFAULT_PROVENANCE_SCHEMA_REGISTRY = ProvenanceSchemaRegistry()
