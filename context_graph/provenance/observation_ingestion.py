"""Generic observation-first ingestion orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from context_graph.models import GraphSnapshot
from context_graph.profiles import (
    ProfileDeactivation,
    ProfileRecord,
    SEMANTIC_RELATION_PROFILE_KIND,
    semantic_observation_profile_from_observation,
)
from context_graph.storage.backend import (
    BackendDriver,
    BackendSyncBridge,
    EdgeRecord,
    MutationPlan,
    RecordKind,
    SearchDocumentRecord,
    merge_mutation_plans,
)

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
    profile_deactivations: tuple[ProfileDeactivation, ...] = ()
    invalidated_claim_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
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
        claim_ids: list[str] = []
        rejection_reasons: dict[str, int] = {}
        evidence_plans: list[MutationPlan] = []
        invalidated_claim_ids: list[str] = []
        invalidated_claim_observed_at: dict[str, str] = {}
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
            for claim_id in getattr(invalidation_result, "claim_ids", ()):
                if claim_id and claim_id not in claim_ids:
                    claim_ids.append(claim_id)
            for claim_id in getattr(invalidation_result, "invalidated_claim_ids", ()):
                if claim_id not in invalidated_claim_observed_at:
                    invalidated_claim_ids.append(claim_id)
                invalidated_claim_observed_at[claim_id] = (
                    observation.observed_at
                    or invalidated_claim_observed_at.get(claim_id)
                    or ""
                )
            profiles.extend(invalidation_result.profiles)

        evidence_plan = merge_mutation_plans(*evidence_plans)
        deduped_profiles = _dedupe_profiles(profiles)
        relation_history_plan, profile_deactivations = self._build_relation_profile_deactivations(
            repo_id=items[0].planned_observation.observation.repo_id,
            invalidated_claim_ids=tuple(invalidated_claim_ids),
            observed_at_by_claim_id=invalidated_claim_observed_at,
        )
        return ObservationIngestionResult(
            observation_plan=observation_plan,
            evidence_plan=evidence_plan,
            plan=merge_mutation_plans(observation_plan, evidence_plan, relation_history_plan),
            profiles=deduped_profiles,
            profile_deactivations=profile_deactivations,
            invalidated_claim_ids=tuple(invalidated_claim_ids),
            claim_ids=tuple(claim_ids),
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
            self.persist_profiles(result.profiles, deactivations=result.profile_deactivations)
        return ObservationApplyResult(result=result, operations_applied=operations_applied)

    def persist_profiles(
        self,
        profiles: Sequence[ProfileRecord],
        *,
        deactivations: Sequence[ProfileDeactivation] = (),
    ) -> None:
        deduped = _dedupe_profiles(profiles)
        deduped_deactivations = _dedupe_profile_deactivations(deactivations)
        if (not deduped and not deduped_deactivations) or self.metadata_store is None:
            return
        if deduped_deactivations:
            self.metadata_store.deactivate_profiles(deduped_deactivations)
        if deduped:
            self.metadata_store.upsert_profiles(deduped)
        if self.profile_indexer is not None:
            self.profile_indexer.apply_profile_updates(
                profiles=deduped,
                deactivations=deduped_deactivations,
            )

    def _build_relation_profile_deactivations(
        self,
        *,
        repo_id: str,
        invalidated_claim_ids: Sequence[str],
        observed_at_by_claim_id: Mapping[str, str],
    ) -> tuple[MutationPlan, tuple[ProfileDeactivation, ...]]:
        if self.metadata_store is None or not invalidated_claim_ids:
            return MutationPlan(), ()
        invalidated_set = {claim_id for claim_id in invalidated_claim_ids if claim_id}
        if not invalidated_set:
            return MutationPlan(), ()
        active_profiles = self.metadata_store.load_profiles(repo_id, active_only=True)
        deactivations: list[ProfileDeactivation] = []
        relation_updates: list[EdgeRecord] = []
        relation_documents: list[SearchDocumentRecord] = []
        for profile in active_profiles:
            if profile.profile_kind != SEMANTIC_RELATION_PROFILE_KIND:
                continue
            if profile.source_node_id not in invalidated_set and profile.target_node_id not in invalidated_set:
                continue
            observed_at = (
                observed_at_by_claim_id.get(profile.source_node_id or "")
                or observed_at_by_claim_id.get(profile.target_node_id or "")
                or profile.updated_at
            )
            if not observed_at:
                continue
            deactivations.append(
                ProfileDeactivation(
                    repo_id=repo_id,
                    profile_kind=profile.profile_kind,
                    relation_type=profile.relation_type,
                    source_node_id=profile.source_node_id,
                    target_node_id=profile.target_node_id,
                    observed_at=observed_at,
                )
            )
            edge_type = _relation_edge_type(profile.relation_type)
            if edge_type and profile.source_node_id and profile.target_node_id:
                relation_updates.append(
                    EdgeRecord(
                        record_id=profile.profile_target_id,
                        record_type="claim_relation",
                        repo_id=repo_id,
                        group_id=repo_id,
                        relationship_type=edge_type,
                        source_record_id=profile.source_node_id,
                        target_record_id=profile.target_node_id,
                        properties={
                            "record_id": profile.profile_target_id,
                            "record_type": "claim_relation",
                            "repo_id": repo_id,
                            "group_id": repo_id,
                            "resolution_type": profile.relation_type,
                            "resolution_rationale": profile.value_text,
                            "resolution_confidence": profile.metadata.get("resolution_confidence"),
                            "observed_at": observed_at,
                            "deactivated_at": observed_at,
                            "valid_to": observed_at,
                            "is_active": False,
                        },
                    )
                )
                relation_documents.append(
                    SearchDocumentRecord(
                        record_kind=RecordKind.EDGE,
                        record_id=profile.profile_target_id,
                        record_type="claim_relation",
                        repo_id=repo_id,
                        group_id=repo_id,
                        search_text=" ".join(
                            part
                            for part in (
                                profile.value_text.strip(),
                                str(profile.relation_type or "").strip(),
                            )
                            if part
                        ),
                        properties={
                            "relation_type": profile.relation_type,
                            "resolution_confidence": profile.metadata.get("resolution_confidence"),
                            "resolution_rationale": profile.value_text,
                            "source_record_id": profile.source_node_id,
                            "target_record_id": profile.target_node_id,
                            "observed_at": observed_at,
                            "deactivated_at": observed_at,
                            "valid_to": observed_at,
                            "is_active": False,
                        },
                    )
                )
        return (
            MutationPlan(
                upsert_edges=tuple(relation_updates),
                upsert_search_documents=tuple(relation_documents),
            ),
            _dedupe_profile_deactivations(deactivations),
        )


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


def _dedupe_profile_deactivations(
    deactivations: Sequence[ProfileDeactivation],
) -> tuple[ProfileDeactivation, ...]:
    deduped: dict[tuple[str, str, str | None, str | None, str | None], ProfileDeactivation] = {}
    for deactivation in deactivations:
        key = (
            deactivation.repo_id,
            deactivation.profile_kind,
            deactivation.relation_type,
            deactivation.source_node_id,
            deactivation.target_node_id,
        )
        deduped[key] = deactivation
    return tuple(deduped.values())


def _relation_edge_type(relation_type: str | None) -> str | None:
    normalized = " ".join(str(relation_type or "").strip().split())
    if not normalized:
        return None
    return normalized.upper().replace(" ", "_")
