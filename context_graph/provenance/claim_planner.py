"""Scaffolding for future canonical claim promotion."""

from __future__ import annotations

from dataclasses import dataclass

from context_graph.storage.backend import MutationPlan

from .canonicalization import canonical_claim_key
from .validation import ClaimValidationResult, ValidatedClaimBundle


@dataclass(frozen=True, slots=True)
class ClaimPromotionCandidate:
    claim_text: str
    claim_kind: str
    canonical_key: str


class ClaimMutationPlanner:
    """Prepare validated claims for a future canonical claim layer."""

    def build_promotion_candidates(
        self,
        validation: ClaimValidationResult,
    ) -> tuple[ClaimPromotionCandidate, ...]:
        return tuple(
            ClaimPromotionCandidate(
                claim_text=claim.claim_text,
                claim_kind=claim.claim_kind,
                canonical_key=canonical_claim_key(
                    claim_kind=claim.claim_kind,
                    claim_text=claim.claim_text,
                ),
            )
            for claim in validation.validated_claims
        )

    def plan_validated_claims(
        self,
        validation: ClaimValidationResult,
    ) -> MutationPlan:
        raise NotImplementedError(
            "Canonical Claim insertion is deferred in Claim-Validated Provenance Pipeline V1."
        )


class ClaimPromotionService:
    """Boundary for future validated-claim promotion into canonical claims."""

    def __init__(self, *, planner: ClaimMutationPlanner | None = None) -> None:
        self.planner = planner or ClaimMutationPlanner()

    def prepare_candidates(
        self,
        validation: ClaimValidationResult,
    ) -> tuple[ClaimPromotionCandidate, ...]:
        return self.planner.build_promotion_candidates(validation)
