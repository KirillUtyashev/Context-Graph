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


DEFAULT_PROVENANCE_SCHEMA_REGISTRY = ProvenanceSchemaRegistry()
