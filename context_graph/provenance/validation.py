"""Validation gates for claim-conditioned provenance extraction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping

from context_graph.code_identity import SnapshotCodeResolver
from context_graph.models import GraphSnapshot

from .debug import emit_provenance_debug
from .grounding import resolve_about_record_ids
from .models import (
    ClaimEvidenceExtractionResponse,
    ClaimWithEvidenceCandidate,
    Observation,
    ObservationKind,
    SupportingEvidenceCandidate,
)
from .schema_registry import DEFAULT_PROVENANCE_SCHEMA_REGISTRY, ProvenanceSchemaRegistry

_VAGUE_MARKERS = (
    "maybe",
    "might",
    "probably",
    "possibly",
    "i think",
    "it seems",
    "appears to",
)


@dataclass(frozen=True, slots=True)
class ValidatedClaimBundle:
    claim_text: str
    claim_kind: str
    claim_subkind: str
    assertion_status: str
    claim_frame: str
    about_hints: tuple[str, ...]
    resolved_about_record_ids: tuple[str, ...]
    unresolved_about_hints: tuple[str, ...]
    confidence: str
    derivation_kind: str
    metadata: Mapping[str, object]
    supporting_evidence: tuple[SupportingEvidenceCandidate, ...]


@dataclass(frozen=True, slots=True)
class RejectedClaimBundle:
    claim_text: str | None
    claim_kind: str | None
    reason_code: str
    reason: str


@dataclass(frozen=True, slots=True)
class ClaimValidationResult:
    validated_claims: tuple[ValidatedClaimBundle, ...] = ()
    rejected_claims: tuple[RejectedClaimBundle, ...] = ()
    extracted_count: int = 0

    @property
    def validated_count(self) -> int:
        return len(self.validated_claims)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected_claims)

    @property
    def rejection_reason_counts(self) -> dict[str, int]:
        return dict(Counter(item.reason_code for item in self.rejected_claims))


class ClaimBundleValidator:
    """Validate claim-conditioned extraction output before graph insertion."""

    def __init__(
        self,
        *,
        registry: ProvenanceSchemaRegistry | None = None,
    ) -> None:
        self.registry = registry or DEFAULT_PROVENANCE_SCHEMA_REGISTRY

    def validate(
        self,
        *,
        observation: Observation,
        extraction: ClaimEvidenceExtractionResponse,
        snapshot: GraphSnapshot | None = None,
    ) -> ClaimValidationResult:
        validated: list[ValidatedClaimBundle] = []
        rejected: list[RejectedClaimBundle] = []
        resolver = SnapshotCodeResolver(snapshot) if snapshot is not None else None
        for candidate in extraction.claims:
            claim_kind, claim_subkind, assertion_status, claim_frame = self._resolve_claim_shape(
                observation=observation,
                candidate=candidate,
            )
            rejection = self._validate_structural(
                observation=observation,
                candidate=candidate,
                claim_kind=claim_kind,
                claim_subkind=claim_subkind,
                assertion_status=assertion_status,
                claim_frame=claim_frame,
            )
            if rejection is None:
                rejection = self._validate_semantic(
                    observation=observation,
                    candidate=candidate,
                    claim_kind=claim_kind,
                    assertion_status=assertion_status,
                )
            if rejection is not None:
                rejected.append(rejection)
                continue
            resolved_about_nodes = resolver.resolve_about_hints(candidate.about_hints) if resolver is not None else ()
            resolved_about_record_ids = (
                resolve_about_record_ids(snapshot, candidate.about_hints)
                if snapshot is not None
                else ()
            )
            unresolved_about_hints = tuple(
                hint
                for hint in candidate.about_hints
                if resolver is None or resolver.resolve_about_hint(hint) is None
            )
            validated.append(
                ValidatedClaimBundle(
                    claim_text=" ".join(candidate.claim_text.split()).strip(),
                    claim_kind=claim_kind,
                    claim_subkind=claim_subkind,
                    assertion_status=assertion_status,
                    claim_frame=claim_frame,
                    about_hints=tuple(candidate.about_hints),
                    resolved_about_record_ids=resolved_about_record_ids,
                    unresolved_about_hints=unresolved_about_hints,
                    confidence=candidate.confidence,
                    derivation_kind=candidate.derivation_kind,
                    metadata=candidate.metadata.model_dump(),
                    supporting_evidence=tuple(candidate.supporting_evidence),
                )
            )
        result = ClaimValidationResult(
            validated_claims=tuple(validated),
            rejected_claims=tuple(rejected),
            extracted_count=len(extraction.claims),
        )
        emit_provenance_debug(
            "claim_validation",
            {
                "observation_id": observation.observation_id,
                "observation_kind": observation.observation_kind.value,
                "source_kind": observation.source_kind,
                "repo_id": observation.repo_id,
                "extracted_count": result.extracted_count,
                "validated_count": result.validated_count,
                "rejected_count": result.rejected_count,
                "rejection_reason_counts": result.rejection_reason_counts,
                "validated_claims": [
                    {
                        "claim_text": claim.claim_text,
                        "claim_kind": claim.claim_kind,
                        "claim_subkind": claim.claim_subkind,
                        "assertion_status": claim.assertion_status,
                        "claim_frame": claim.claim_frame,
                        "about_hints": list(claim.about_hints),
                        "resolved_about_record_ids": list(claim.resolved_about_record_ids),
                        "unresolved_about_hints": list(claim.unresolved_about_hints),
                        "supporting_evidence_count": len(claim.supporting_evidence),
                    }
                    for claim in result.validated_claims
                ],
                "rejected_claims": [
                    {
                        "claim_text": claim.claim_text,
                        "claim_kind": claim.claim_kind,
                        "reason_code": claim.reason_code,
                        "reason": claim.reason,
                    }
                    for claim in result.rejected_claims
                ],
            },
        )
        return result

    def _validate_structural(
        self,
        *,
        observation: Observation,
        candidate: ClaimWithEvidenceCandidate,
        claim_kind: str | None,
        claim_subkind: str | None,
        assertion_status: str | None,
        claim_frame: str | None,
    ) -> RejectedClaimBundle | None:
        claim_text = " ".join(candidate.claim_text.split()).strip()
        if not claim_text:
            return _rejected(candidate, "empty_claim_text", "claim_text must not be empty")
        if claim_kind not in self.registry.claim_kinds:
            return _rejected(candidate, "unsupported_claim_kind", f"unsupported claim_kind {claim_kind!r}")
        if claim_subkind not in self.registry.claim_subkinds:
            return _rejected(
                candidate,
                "unsupported_claim_subkind",
                f"unsupported claim_subkind {claim_subkind!r}",
            )
        if assertion_status not in self.registry.assertion_statuses:
            return _rejected(
                candidate,
                "unsupported_assertion_status",
                f"unsupported assertion_status {assertion_status!r}",
            )
        if claim_frame not in self.registry.claim_frames:
            return _rejected(
                candidate,
                "unsupported_claim_frame",
                f"unsupported claim_frame {claim_frame!r}",
            )
        if candidate.confidence not in self.registry.confidence_levels:
            return _rejected(candidate, "unsupported_confidence", f"unsupported confidence {candidate.confidence!r}")
        if not candidate.supporting_evidence:
            return _rejected(candidate, "missing_supporting_evidence", "claim must include supporting evidence")
        if len(candidate.supporting_evidence) > 3:
            return _rejected(candidate, "too_many_supporting_evidence", "claim must include at most three support items")
        for evidence in candidate.supporting_evidence:
            if evidence.evidence_kind not in self.registry.evidence_kinds:
                return _rejected(
                    candidate,
                    "unsupported_evidence_kind",
                    f"unsupported evidence_kind {evidence.evidence_kind!r}",
                )
            if evidence.support_kind not in self.registry.support_kinds:
                return _rejected(
                    candidate,
                    "unsupported_support_kind",
                    f"unsupported support_kind {evidence.support_kind!r}",
                )
            if evidence.support_source not in self.registry.support_sources:
                return _rejected(
                    candidate,
                    "unsupported_support_source",
                    f"unsupported support_source {evidence.support_source!r}",
                )
            if evidence.support_role not in self.registry.support_roles:
                return _rejected(
                    candidate,
                    "unsupported_support_role",
                    f"unsupported support_role {evidence.support_role!r}",
                )
            if not self._support_rationale_is_valid(evidence):
                return _rejected(
                    candidate,
                    "missing_support_rationale",
                    "support_rationale must not be empty",
                )
            if not self._support_is_valid(observation=observation, evidence=evidence):
                return _rejected(candidate, "invalid_evidence_span", "supporting evidence span is invalid")
        return None

    def _validate_semantic(
        self,
        *,
        observation: Observation,
        candidate: ClaimWithEvidenceCandidate,
        claim_kind: str | None,
        assertion_status: str | None,
    ) -> RejectedClaimBundle | None:
        if not self.registry.claim_kind_allowed(claim_kind or "", observation.source_kind):
            return _rejected(
                candidate,
                "unsupported_source_kind",
                f"claim_kind {claim_kind!r} is not allowed for source_kind {observation.source_kind!r}",
            )
        normalized_text = " ".join(candidate.claim_text.lower().split()).strip()
        if len(normalized_text) < 12 or len(normalized_text.split()) < 2:
            return _rejected(candidate, "claim_too_vague", "claim text is too short or vague")
        if (
            assertion_status in {"confirmed", "observed"}
            and any(marker in normalized_text for marker in _VAGUE_MARKERS)
        ):
            return _rejected(candidate, "speculative_claim", "claim text appears speculative")
        if candidate.confidence == "low":
            return _rejected(candidate, "insufficient_confidence", "low-confidence claims are not stored in v1")
        return None

    def _support_is_valid(
        self,
        *,
        observation: Observation,
        evidence: SupportingEvidenceCandidate,
    ) -> bool:
        if evidence.support_source == "observation_title":
            if evidence.span_start is not None or evidence.span_end is not None:
                return False
            title = observation.title or ""
            return self._quote_matches(title, evidence.quote_text)
        return self._quote_matches(observation.text, evidence.quote_text)

    def _quote_matches(self, source_text: str, quote_text: str) -> bool:
        if not source_text:
            return False
        normalized_quote = " ".join(quote_text.split()).strip()
        normalized_span = " ".join(source_text.split()).strip()
        if not normalized_quote or not normalized_span:
            return False
        return normalized_quote in normalized_span or normalized_span in normalized_quote

    def _support_rationale_is_valid(self, evidence: SupportingEvidenceCandidate) -> bool:
        rationale = " ".join(str(evidence.support_rationale or evidence.summary_text or "").split()).strip()
        return bool(rationale)

    def _resolve_claim_shape(
        self,
        *,
        observation: Observation,
        candidate: ClaimWithEvidenceCandidate,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        claim_kind = candidate.claim_kind
        claim_subkind = candidate.claim_subkind
        if claim_subkind is not None:
            mapped_claim_kind = self.registry.claim_kind_for_subkind(claim_subkind, observation.source_kind)
            if claim_kind is not None and mapped_claim_kind is not None and claim_kind != mapped_claim_kind:
                return None, claim_subkind, candidate.assertion_status, candidate.claim_frame
            claim_kind = mapped_claim_kind
        elif claim_kind is not None:
            claim_subkind = self.registry.default_claim_subkind_for_claim_kind(claim_kind)

        assertion_status = candidate.assertion_status or self._default_assertion_status(
            observation=observation,
            claim_kind=claim_kind,
        )
        claim_frame = candidate.claim_frame or self._default_claim_frame(
            observation=observation,
            claim_kind=claim_kind,
        )
        return claim_kind, claim_subkind, assertion_status, claim_frame

    def _default_assertion_status(
        self,
        *,
        observation: Observation,
        claim_kind: str | None,
    ) -> str | None:
        if claim_kind == "approval":
            return "confirmed"
        if claim_kind in {"review_feedback", "requirement"}:
            return "requested"
        if observation.source_kind == "commit":
            return "reported"
        if claim_kind == "bug_report" and observation.source_kind == "issue":
            return "reported"
        return "reported"

    def _default_claim_frame(
        self,
        *,
        observation: Observation,
        claim_kind: str | None,
    ) -> str | None:
        if claim_kind in {"review_feedback", "approval", "concern"} or observation.observation_kind in {
            ObservationKind.GITHUB_PR_REVIEW,
            ObservationKind.GITHUB_PR_REVIEW_COMMENT,
        }:
            return "reviewer_feedback"
        if claim_kind == "requirement":
            return "user_requirement"
        if claim_kind == "bug_report" and observation.source_kind == "issue":
            return "reported_observation"
        return "author_statement"


def _rejected(
    candidate: ClaimWithEvidenceCandidate,
    code: str,
    reason: str,
) -> RejectedClaimBundle:
    return RejectedClaimBundle(
        claim_text=candidate.claim_text or None,
        claim_kind=candidate.claim_kind or None,
        reason_code=code,
        reason=reason,
    )
