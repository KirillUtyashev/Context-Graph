"""Validation gates for claim-conditioned provenance extraction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping

from context_graph.code_identity import SnapshotCodeResolver
from context_graph.models import GraphSnapshot

from .models import ClaimEvidenceExtractionResponse, ClaimWithEvidenceCandidate, Observation, SupportingEvidenceCandidate
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
            rejection = self._validate_structural(observation=observation, candidate=candidate)
            if rejection is None:
                rejection = self._validate_semantic(observation=observation, candidate=candidate)
            if rejection is not None:
                rejected.append(rejection)
                continue
            resolved_about_nodes = resolver.resolve_about_hints(candidate.about_hints) if resolver is not None else ()
            resolved_about_record_ids = (
                tuple(f"{node.node_id}@{snapshot.revision_id}" for node in resolved_about_nodes)
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
                    claim_kind=candidate.claim_kind,
                    about_hints=tuple(candidate.about_hints),
                    resolved_about_record_ids=resolved_about_record_ids,
                    unresolved_about_hints=unresolved_about_hints,
                    confidence=candidate.confidence,
                    derivation_kind=candidate.derivation_kind,
                    metadata=candidate.metadata.model_dump(),
                    supporting_evidence=tuple(candidate.supporting_evidence),
                )
            )
        return ClaimValidationResult(
            validated_claims=tuple(validated),
            rejected_claims=tuple(rejected),
            extracted_count=len(extraction.claims),
        )

    def _validate_structural(  # pragma: no cover
        self,
        *,
        observation: Observation,
        candidate: ClaimWithEvidenceCandidate,
    ) -> RejectedClaimBundle | None:
        claim_text = " ".join(candidate.claim_text.split()).strip()
        if not claim_text:
            return _rejected(candidate, "empty_claim_text", "claim_text must not be empty")
        if candidate.claim_kind not in self.registry.claim_kinds:
            return _rejected(candidate, "unsupported_claim_kind", f"unsupported claim_kind {candidate.claim_kind!r}")
        if candidate.confidence not in self.registry.confidence_levels:
            return _rejected(candidate, "unsupported_confidence", f"unsupported confidence {candidate.confidence!r}")
        if not candidate.supporting_evidence:
            return _rejected(candidate, "missing_supporting_evidence", "claim must include supporting evidence")
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
            if not self._span_is_valid(observation.text, evidence):
                return _rejected(candidate, "invalid_evidence_span", "supporting evidence span is invalid")
        return None

    def _validate_semantic(  # pragma: no cover
        self,
        *,
        observation: Observation,
        candidate: ClaimWithEvidenceCandidate,
    ) -> RejectedClaimBundle | None:
        if not self.registry.claim_kind_allowed(candidate.claim_kind, observation.source_kind):
            return _rejected(
                candidate,
                "unsupported_source_kind",
                f"claim_kind {candidate.claim_kind!r} is not allowed for source_kind {observation.source_kind!r}",
            )
        normalized_text = " ".join(candidate.claim_text.lower().split()).strip()
        if len(normalized_text) < 12 or len(normalized_text.split()) < 2:
            return _rejected(candidate, "claim_too_vague", "claim text is too short or vague")
        if any(marker in normalized_text for marker in _VAGUE_MARKERS):
            return _rejected(candidate, "speculative_claim", "claim text appears speculative")
        if candidate.confidence == "low":
            return _rejected(candidate, "insufficient_confidence", "low-confidence claims are not stored in v1")
        return None

    def _span_is_valid(self, observation_text: str, evidence: SupportingEvidenceCandidate) -> bool:  # pragma: no cover
        if evidence.span_start is None and evidence.span_end is None:
            return True
        if evidence.span_start is None or evidence.span_end is None:
            return False
        if evidence.span_start < 0 or evidence.span_end <= evidence.span_start:
            return False
        if evidence.span_end > len(observation_text):
            return False
        span_text = observation_text[evidence.span_start:evidence.span_end]
        normalized_quote = " ".join(evidence.quote_text.split()).strip()
        normalized_span = " ".join(span_text.split()).strip()
        if not normalized_quote or not normalized_span:
            return False
        return normalized_quote in normalized_span or normalized_span in normalized_quote


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
