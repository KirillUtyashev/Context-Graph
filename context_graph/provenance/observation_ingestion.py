"""Generic observation-first ingestion orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from context_graph.models import GraphSnapshot
from context_graph.profiles import ProfileRecord, semantic_observation_profile_from_observation
from context_graph.storage.backend import BackendDriver, BackendSyncBridge, MutationPlan, merge_mutation_plans

from .planner import ObservationMutationPlanner, PlannedObservation
from .prompt_context import build_deterministic_code_context
from .schema_registry import DEFAULT_PROVENANCE_SCHEMA_REGISTRY, ObservationSourcePolicy, ProvenanceSchemaRegistry
from .services import ClaimEvidenceExtractionService
from .validation import ClaimBundleValidator
from .invalidation import EvidenceInvalidationPlanner


@dataclass(frozen=True, slots=True)
class ObservationIngestionItem:
    planned_observation: PlannedObservation
    code_context: Mapping[str, Any] | None = None
    source_context: Mapping[str, Any] | None = None
    source_policy: ObservationSourcePolicy | None = None


@dataclass(frozen=True, slots=True)
class ObservationIngestionResult:
    observation_plan: MutationPlan = field(default_factory=MutationPlan)
    evidence_plan: MutationPlan = field(default_factory=MutationPlan)
    plan: MutationPlan = field(default_factory=MutationPlan)
    profiles: tuple[ProfileRecord, ...] = ()
    claim_candidates_extracted: int = 0
    claim_candidates_validated: int = 0
    claim_candidates_rejected: int = 0
    claim_rejection_reasons: dict[str, int] = field(default_factory=dict)
    evidence_invalidated: int = 0


@dataclass(frozen=True, slots=True)
class ObservationApplyResult:
    result: ObservationIngestionResult
    operations_applied: int


class ObservationIngestionService:
    """Shared provenance orchestration for all observation sources."""

    def __init__(
        self,
        *,
        metadata_store=None,
        backend_driver: BackendDriver | None = None,
        observation_planner: ObservationMutationPlanner | None = None,
        claim_extraction_service: ClaimEvidenceExtractionService | None = None,
        claim_validator: ClaimBundleValidator | None = None,
        evidence_invalidation_planner: EvidenceInvalidationPlanner | None = None,
        profile_indexer: Any | None = None,
        registry: ProvenanceSchemaRegistry | None = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.backend_driver = backend_driver
        self.observation_planner = observation_planner or ObservationMutationPlanner()
        self.claim_extraction_service = claim_extraction_service
        self.claim_validator = claim_validator or ClaimBundleValidator(registry=registry)
        self.evidence_invalidation_planner = evidence_invalidation_planner
        self.profile_indexer = profile_indexer
        self.registry = registry or DEFAULT_PROVENANCE_SCHEMA_REGISTRY

    def plan_ingestion(
        self,
        *,
        items: Sequence[ObservationIngestionItem],
        snapshot: GraphSnapshot | None = None,
    ) -> ObservationIngestionResult:
        if not items:
            return ObservationIngestionResult()

        observation_plan = self.observation_planner.plan_observations(
            *(item.planned_observation for item in items),
            snapshot=snapshot,
        )
        profiles: list[ProfileRecord] = [
            semantic_observation_profile_from_observation(item.planned_observation.observation)
            for item in items
        ]
        if (
            self.claim_extraction_service is None
            or self.evidence_invalidation_planner is None
        ):
            deduped_profiles = _dedupe_profiles(profiles)
            return ObservationIngestionResult(
                observation_plan=observation_plan,
                evidence_plan=MutationPlan(),
                plan=observation_plan,
                profiles=deduped_profiles,
            )

        extracted_count = 0
        validated_count = 0
        rejected_count = 0
        invalidated_count = 0
        rejection_reasons: dict[str, int] = {}
        evidence_plans: list[MutationPlan] = []
        claim_session = self.evidence_invalidation_planner.begin_claim_session(
            repo_id=items[0].planned_observation.observation.repo_id
        )

        for item in items:
            observation = item.planned_observation.observation
            policy = item.source_policy or self.registry.observation_policy(
                observation.observation_kind,
                source_kind=observation.source_kind,
            )
            if policy is not None and policy.extraction_mode == "observation_only":
                continue
            try:
                extraction = self.claim_extraction_service.extract(
                    observation,
                    code_context=dict(
                        item.code_context
                        or build_deterministic_code_context(
                            observation,
                            snapshot=snapshot,
                        )
                    ),
                    source_context=dict(item.source_context or {}),
                )
            except Exception:
                continue
            validation = self.claim_validator.validate(
                observation=observation,
                extraction=extraction,
                snapshot=snapshot,
            )
            extracted_count += validation.extracted_count
            validated_count += validation.validated_count
            rejected_count += validation.rejected_count
            rejection_reasons = _merge_reason_counts(
                rejection_reasons,
                validation.rejection_reason_counts,
            )
            invalidation_result = self.evidence_invalidation_planner.plan_validated_claims(
                observation=observation,
                validation=validation,
                snapshot=snapshot,
                session=claim_session,
            )
            evidence_plans.append(invalidation_result.plan)
            invalidated_count += invalidation_result.invalidated_count
            profiles.extend(invalidation_result.profiles)

        evidence_plan = merge_mutation_plans(*evidence_plans)
        deduped_profiles = _dedupe_profiles(profiles)
        return ObservationIngestionResult(
            observation_plan=observation_plan,
            evidence_plan=evidence_plan,
            plan=merge_mutation_plans(observation_plan, evidence_plan),
            profiles=deduped_profiles,
            claim_candidates_extracted=extracted_count,
            claim_candidates_validated=validated_count,
            claim_candidates_rejected=rejected_count,
            claim_rejection_reasons=rejection_reasons,
            evidence_invalidated=invalidated_count,
        )

    def apply_ingestion(
        self,
        *,
        items: Sequence[ObservationIngestionItem],
        snapshot: GraphSnapshot | None = None,
        execute: bool,
    ) -> ObservationApplyResult:
        result = self.plan_ingestion(items=items, snapshot=snapshot)
        operations_applied = 0
        if execute:
            if self.backend_driver is None:
                raise RuntimeError("observation ingestion execution requires a configured backend driver")
            bridge = BackendSyncBridge(self.backend_driver)
            operations_applied = bridge.apply(result.plan).operations_applied
            self.persist_profiles(result.profiles)
        return ObservationApplyResult(result=result, operations_applied=operations_applied)

    def persist_profiles(self, profiles: Sequence[ProfileRecord]) -> None:
        deduped = _dedupe_profiles(profiles)
        if not deduped or self.metadata_store is None:
            return
        self.metadata_store.upsert_profiles(deduped)
        if self.profile_indexer is not None:
            self.profile_indexer.apply_profile_updates(profiles=deduped)


def _dedupe_profiles(profiles: Sequence[ProfileRecord]) -> tuple[ProfileRecord, ...]:
    deduped: dict[str, ProfileRecord] = {}
    for profile in profiles:
        deduped[profile.profile_id] = profile
    return tuple(deduped[profile_id] for profile_id in sorted(deduped))


def _merge_reason_counts(
    left: Mapping[str, int],
    right: Mapping[str, int],
) -> dict[str, int]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = merged.get(key, 0) + int(value)
    return merged
