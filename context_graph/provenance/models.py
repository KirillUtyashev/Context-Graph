"""Domain models for the observation-first provenance ontology."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field


class ObservationKind(str, Enum):
    """Documented built-in observation kinds supported by the provenance layer."""

    GIT_COMMIT_MESSAGE = "git_commit_message"
    GITHUB_PR_BODY = "github_pr_body"
    GITHUB_ISSUE_BODY = "github_issue_body"
    GITHUB_ISSUE_CLOSED = "github_issue_closed"
    GITHUB_PR_REVIEW = "github_pr_review"
    GITHUB_PR_REVIEW_COMMENT = "github_pr_review_comment"
    GITHUB_ISSUE_COMMENT = "github_issue_comment"
    MARKDOWN_DOCUMENT = "markdown_document"
    AGENT_NOTE = "agent_note"
    AGENT_TRACE = "agent_trace"
    STACK_TRACE = "stack_trace"


ClaimSubkind = Literal[
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
]
AssertionStatus = Literal["reported", "proposed", "requested", "observed", "confirmed", "rejected"]
ClaimFrame = Literal[
    "author_statement",
    "reviewer_feedback",
    "user_requirement",
    "reported_observation",
    "observed_result",
]
SupportSource = Literal["observation_text", "observation_title"]
SupportRole = Literal[
    "states_change",
    "states_rationale",
    "states_constraint",
    "reports_observation",
    "requests_change",
    "confirms_result",
    "rejects_prior_explanation",
    "provides_context",
]
ClaimResolutionType = Literal[
    "same_as",
    "refines",
    "contradicts",
    "explains",
    "supersedes",
    "mitigates",
    "depends_on",
    "supports",
    "unrelated",
]
ResolutionConfidence = Literal["low", "medium", "high"]
ThreadKind = Literal["pull_request", "issue", "commit"]


def observation_kind_value(value: str | ObservationKind) -> str:
    if isinstance(value, ObservationKind):
        return value.value
    return str(value).strip()


@dataclass(frozen=True, slots=True)
class ObservationSourceRef:
    """Stable reference to the structural record an observation came from."""

    source_kind: str
    source_id: str
    source_record_type: str
    repo_id: str
    display_name: str | None = None
    ensure_source_node: bool = False
    source_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObservationLink:
    """Deterministic relationship emitted from an observation."""

    relation_type: str
    target_record_id: str
    target_record_type: str
    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Observation:
    """Canonical raw provenance/event record."""

    observation_id: str
    repo_id: str
    observation_kind: str
    source_kind: str
    source_id: str
    observed_at: str | None
    author: str | None
    title: str | None
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    ingested_at: str | None = None
    context_kind: str | None = None
    context_id: str | None = None
    thread_kind: ThreadKind | None = None
    thread_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_kind", observation_kind_value(self.observation_kind))
        if self.context_kind is None and self.thread_kind is not None:
            object.__setattr__(self, "context_kind", str(self.thread_kind))
        if self.context_id is None and self.thread_id is not None:
            object.__setattr__(self, "context_id", str(self.thread_id))


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Canonical bounded evidence derived from one raw observation."""

    evidence_id: str
    repo_id: str
    evidence_kind: str
    quote_text: str
    summary_text: str | None
    span_start: int | None
    span_end: int | None
    support_kind: str
    support_source: str | None = None
    fact_key: str | None = None
    claim_text: str | None = None
    claim_kind: str | None = None
    claim_subkind: str | None = None
    assertion_status: str | None = None
    claim_frame: str | None = None
    claim_confidence: str | None = None
    claim_derivation_kind: str | None = None
    observation_id: str | None = None
    observed_at: str | None = None
    is_current: bool = True
    invalidated_at: str | None = None
    invalidated_by_fact_key: str | None = None
    invalidated_by_observation_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PersistedEvidenceRecord:
    """One evidence record hydrated from the backend with grounded targets."""

    evidence: EvidenceRecord
    about_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PersistedEvidenceFact:
    """One current fact candidate aggregated from persisted evidence records."""

    fact_key: str
    claim_text: str
    claim_kind: str
    observation_id: str | None
    observed_at: str | None
    about_record_ids: tuple[str, ...]
    evidence_records: tuple[PersistedEvidenceRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """Canonical persisted claim node."""

    claim_id: str
    repo_id: str
    canonical_key: str
    claim_text: str
    claim_kind: str
    claim_subkind: str
    first_observed_at: str | None
    latest_observed_at: str | None
    is_current: bool = True
    ingested_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    invalidated_at: str | None = None
    invalidated_by_claim_id: str | None = None
    observation_id: str | None = None
    source_kind: str | None = None
    source_id: str | None = None
    context_kind: str | None = None
    context_id: str | None = None
    thread_kind: ThreadKind | None = None
    thread_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.context_kind is None and self.thread_kind is not None:
            object.__setattr__(self, "context_kind", str(self.thread_kind))
        if self.context_id is None and self.thread_id is not None:
            object.__setattr__(self, "context_id", str(self.thread_id))


@dataclass(frozen=True, slots=True)
class PersistedClaimRecord:
    """One persisted claim node hydrated from the backend."""

    claim: ClaimRecord
    about_record_ids: tuple[str, ...] = ()
    evidence_record_ids: tuple[str, ...] = ()
    legacy_evidence_records: tuple[PersistedEvidenceRecord, ...] = ()


def git_commit_message_observation_id(repo_id: str, commit_sha: str) -> str:
    return f"observation:{ObservationKind.GIT_COMMIT_MESSAGE.value}:{repo_id}:{commit_sha}"


def github_pr_body_observation_id(repo_id: str, pr_number: int) -> str:
    return f"observation:{ObservationKind.GITHUB_PR_BODY.value}:{repo_id}:{pr_number}"


def github_issue_body_observation_id(repo_id: str, issue_number: int) -> str:
    return f"observation:{ObservationKind.GITHUB_ISSUE_BODY.value}:{repo_id}:{issue_number}"


def github_issue_closed_observation_id(repo_id: str, issue_number: int) -> str:
    return f"observation:{ObservationKind.GITHUB_ISSUE_CLOSED.value}:{repo_id}:{issue_number}"


def github_pr_review_observation_id(repo_id: str, review_id: str | int) -> str:
    return f"observation:{ObservationKind.GITHUB_PR_REVIEW.value}:{repo_id}:{review_id}"


def github_pr_review_comment_observation_id(repo_id: str, comment_id: str | int) -> str:
    return f"observation:{ObservationKind.GITHUB_PR_REVIEW_COMMENT.value}:{repo_id}:{comment_id}"


def github_issue_comment_observation_id(repo_id: str, comment_id: str | int) -> str:
    return f"observation:{ObservationKind.GITHUB_ISSUE_COMMENT.value}:{repo_id}:{comment_id}"


def markdown_document_observation_id(
    repo_id: str,
    revision_id: str,
    relative_path: str,
) -> str:
    import hashlib

    digest = hashlib.sha1(
        "|".join((repo_id, revision_id, relative_path)).encode("utf-8")
    ).hexdigest()
    return f"observation:{ObservationKind.MARKDOWN_DOCUMENT.value}:{repo_id}:{digest}"


def general_observation_id(
    *,
    repo_id: str,
    observation_kind: str | ObservationKind,
    source_id: str | None = None,
    context_id: str | None = None,
    observed_at: str | None = None,
    title: str | None = None,
    text: str | None = None,
) -> str:
    import hashlib

    digest = hashlib.sha1(
        "|".join(
            (
                repo_id,
                observation_kind_value(observation_kind),
                source_id or "",
                context_id or "",
                observed_at or "",
                title or "",
                text or "",
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"observation:{observation_kind_value(observation_kind)}:{repo_id}:{digest}"


def evidence_record_id(
    *,
    repo_id: str,
    observation_id: str,
    evidence_kind: str,
    quote_text: str,
    span_start: int | None,
    span_end: int | None,
    claim_text: str | None = None,
) -> str:
    import hashlib

    digest = hashlib.sha1(
        "|".join(
            [
                repo_id,
                observation_id,
                evidence_kind,
                quote_text,
                str(span_start),
                str(span_end),
                claim_text or "",
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"evidence:{repo_id}:{digest}"


def claim_record_id(
    *,
    repo_id: str,
    canonical_key: str,
    about_record_ids: tuple[str, ...] | list[str] = (),
) -> str:
    import hashlib

    digest = hashlib.sha1(
        "|".join(
            [
                repo_id,
                canonical_key,
                ",".join(sorted(dict.fromkeys(about_record_ids))),
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"claim:{repo_id}:{digest}"


class ProvenanceBaseModel(BaseModel):
    """Base model for structured provenance extraction outputs."""

    model_config = ConfigDict(extra="forbid")


class ExtractionMetadata(ProvenanceBaseModel):
    """Closed metadata envelope for structured extraction responses.

    OpenAI structured output requires object schemas to explicitly disallow
    arbitrary keys. We keep the response metadata closed in v1 and rely on
    typed top-level fields for the values we actually use.
    """


class SupportingEvidenceCandidate(ProvenanceBaseModel):
    evidence_kind: str
    quote_text: str
    summary_text: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    about_hints: list[str] = Field(default_factory=list)
    support_kind: str
    support_source: SupportSource = "observation_text"
    support_role: SupportRole = "provides_context"
    support_rationale: str | None = None
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)


class ClaimWithEvidenceCandidate(ProvenanceBaseModel):
    claim_text: str
    claim_subkind: ClaimSubkind | None = None
    assertion_status: AssertionStatus | None = None
    claim_frame: ClaimFrame | None = None
    about_hints: list[str] = Field(default_factory=list)
    confidence: str
    derivation_kind: str
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)
    supporting_evidence: list[SupportingEvidenceCandidate] = Field(default_factory=list)


class ClaimEvidenceExtractionResponse(ProvenanceBaseModel):
    claims: list[ClaimWithEvidenceCandidate] = Field(default_factory=list)


class EvidenceCandidate(SupportingEvidenceCandidate):
    """Compatibility alias for older derivation helpers that still consume evidence sets."""


class ClaimCandidate(ProvenanceBaseModel):
    claim_text: str
    claim_kind: str
    claim_subkind: ClaimSubkind | None = None
    assertion_status: AssertionStatus | None = None
    claim_frame: ClaimFrame | None = None
    about_hints: list[str] = Field(default_factory=list)
    supported_evidence_ids: list[str] = Field(default_factory=list)
    derivation_kind: str
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)


class EvidenceDedupCandidate(ProvenanceBaseModel):
    observation_id: str
    evidence_kind: str
    quote_text: str
    summary_text: str
    span_start: int | None = None
    span_end: int | None = None
    support_kind: str
    about_record_ids: list[str] = Field(default_factory=list)
    claim_kind: str | None = None
    claim_text: str | None = None
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)


class ClaimDedupCandidate(ProvenanceBaseModel):
    observation_id: str
    claim_text: str
    claim_kind: str
    evidence_ids: list[str] = Field(default_factory=list)
    about_record_ids: list[str] = Field(default_factory=list)
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)


class EvidenceDedupGroup(ProvenanceBaseModel):
    canonical_key: str
    candidates: list[EvidenceDedupCandidate] = Field(default_factory=list)


class ClaimCanonicalizationGroup(ProvenanceBaseModel):
    canonical_key: str
    candidates: list[ClaimDedupCandidate] = Field(default_factory=list)


class EvidenceDedupResult(ProvenanceBaseModel):
    groups: list[EvidenceDedupGroup] = Field(default_factory=list)


class ClaimCanonicalizationResult(ProvenanceBaseModel):
    groups: list[ClaimCanonicalizationGroup] = Field(default_factory=list)


class ClaimRelationCandidate(ProvenanceBaseModel):
    relation_type: Literal["contradicts", "supersedes", "supports", "unrelated"]
    relation_rationale: str
    confidence_note: str | None = None
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)


class ClaimResolutionCandidate(ProvenanceBaseModel):
    candidate_claim_id: str
    resolution_type: ClaimResolutionType
    confidence: ResolutionConfidence
    rationale: str
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)


class ResearchArtifactCandidate(ProvenanceBaseModel):
    artifact_kind: str
    title: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    about_refs: list[str] = Field(default_factory=list)
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)


class EvidenceExtractionResponse(ProvenanceBaseModel):
    """Compatibility wrapper for legacy evidence-only extraction flows."""

    evidence: list[EvidenceCandidate] = Field(default_factory=list)


class ClaimDerivationResponse(ProvenanceBaseModel):
    claims: list[ClaimCandidate] = Field(default_factory=list)


class ClaimRelationResponse(ProvenanceBaseModel):
    relations: list[ClaimRelationCandidate] = Field(default_factory=list)


class ClaimResolutionResponse(ProvenanceBaseModel):
    relations: list[ClaimResolutionCandidate] = Field(default_factory=list)


class ResearchArtifactSynthesisResponse(ProvenanceBaseModel):
    artifacts: list[ResearchArtifactCandidate] = Field(default_factory=list)


class FactConflictResolutionResponse(ProvenanceBaseModel):
    contradicted_fact_ids: list[str] = Field(default_factory=list)
