"""Claim-centric mutation planning for validated provenance claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import inspect
import json
import re
from typing import Any

from context_graph.models import GraphSnapshot
from context_graph.storage.backend import (
    BackendDriver,
    EdgeRecord,
    MutationPlan,
    NodeRecord,
    RecordKind,
    SearchDocumentRecord,
    merge_mutation_plans,
    run_backend_coroutine_sync,
)

from .canonicalization import canonical_claim_key, canonicalize_claim_text
from .debug import emit_provenance_debug
from .evidence_planner import EvidenceFactState, EvidenceMutationPlanner
from .models import (
    ClaimRecord,
    ClaimResolutionResponse,
    Observation,
    PersistedClaimRecord,
    PersistedEvidenceRecord,
    claim_record_id,
)
from .services import ClaimResolutionService
from .services import FactConflictResolutionService
from .validation import ClaimValidationResult, ValidatedClaimBundle

_CURRENT_CLAIM_QUERY = """
MATCH (c:BackendRecord)
WHERE c.record_type = "claim"
  AND c.repo_id = $repo_id
  AND c.claim_kind = $claim_kind
  AND coalesce(c.is_current, true) = true
OPTIONAL MATCH (c)-[:ABOUT]->(about:BackendRecord)
OPTIONAL MATCH (c)<-[:SUPPORTS_CLAIM]-(e:BackendRecord)
RETURN
    c.record_id AS claim_id,
    properties(c) AS claim_properties,
    collect(DISTINCT about.record_id) AS about_record_ids,
    collect(DISTINCT e.record_id) AS evidence_record_ids
"""

_CURRENT_EVIDENCE_QUERY = """
MATCH (e:BackendRecord)-[:ABOUT]->(matched_about:BackendRecord)
WHERE e.record_type = "evidence_record"
  AND e.repo_id = $repo_id
  AND matched_about.record_id IN $about_record_ids
  AND coalesce(e.is_current, true) = true
WITH DISTINCT e
OPTIONAL MATCH (e)-[:ABOUT]->(about:BackendRecord)
OPTIONAL MATCH (e)-[:DERIVED_FROM]->(observation:BackendRecord)
RETURN
    e.record_id AS evidence_id,
    properties(e) AS evidence_properties,
    collect(DISTINCT about.record_id) AS about_record_ids,
    collect(DISTINCT observation.record_id) AS observation_record_ids,
    collect(DISTINCT properties(observation)) AS observation_properties
"""

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_INVALIDATING_RELATION_TYPES = frozenset({"contradicts", "supersedes"})
_RELATION_EDGE_TYPES = {
    "refines": "REFINES",
    "contradicts": "CONTRADICTS",
    "explains": "EXPLAINS",
    "supersedes": "SUPERSEDES",
    "mitigates": "MITIGATES",
    "depends_on": "DEPENDS_ON",
    "supports": "SUPPORTS",
}


@dataclass(frozen=True, slots=True)
class ClaimPromotionCandidate:
    claim_text: str
    claim_kind: str
    canonical_key: str


@dataclass(frozen=True, slots=True)
class ClaimMutationPlanResult:
    plan: MutationPlan
    invalidated_count: int = 0


@dataclass(frozen=True, slots=True)
class CandidateClaimState:
    claim: ClaimRecord
    about_record_ids: tuple[str, ...] = ()
    evidence_record_ids: tuple[str, ...] = ()
    legacy_evidence_records: tuple[PersistedEvidenceRecord, ...] = ()
    backfill_required: bool = False


class CurrentClaimReader:
    """Load current claim nodes, with legacy evidence fallback during migration."""

    def __init__(self, *, backend_driver: BackendDriver | None) -> None:
        self.backend_driver = backend_driver

    def load_current_claims(
        self,
        *,
        repo_id: str,
        claim_kind: str,
        about_record_ids: Sequence[str],
        source_id: str | None,
        limit: int = 50,
    ) -> tuple[CandidateClaimState, ...]:
        if self.backend_driver is None:
            return ()
        claim_candidates = self._load_claim_nodes(
            repo_id=repo_id,
            claim_kind=claim_kind,
            about_record_ids=about_record_ids,
            source_id=source_id,
        )
        legacy_candidates = self._load_legacy_evidence_claims(
            repo_id=repo_id,
            claim_kind=claim_kind,
            about_record_ids=about_record_ids,
        )
        merged: dict[str, CandidateClaimState] = {
            candidate.claim.claim_id: candidate
            for candidate in claim_candidates
        }
        for candidate in legacy_candidates:
            merged.setdefault(candidate.claim.claim_id, candidate)
        ranked = sorted(merged.values(), key=_candidate_sort_key, reverse=True)
        return tuple(ranked[:limit])

    def _load_claim_nodes(
        self,
        *,
        repo_id: str,
        claim_kind: str,
        about_record_ids: Sequence[str],
        source_id: str | None,
    ) -> tuple[CandidateClaimState, ...]:
        query_coro = self.backend_driver.execute_query(
            _CURRENT_CLAIM_QUERY,
            repo_id=repo_id,
            claim_kind=claim_kind,
        )
        try:
            rows = run_backend_coroutine_sync(query_coro)
        except Exception:
            _close_unstarted_coroutine(query_coro)
            return ()
        _close_unstarted_coroutine(query_coro)

        ordered_abouts = tuple(dict.fromkeys(about_record_ids))
        candidates: list[CandidateClaimState] = []
        for row in rows:
            claim = _parse_persisted_claim_record(row)
            if claim is None:
                continue
            if not _claim_candidate_in_scope(
                claim,
                about_record_ids=ordered_abouts,
                source_id=source_id,
            ):
                continue
            candidates.append(claim)
        return tuple(candidates)

    def _load_legacy_evidence_claims(
        self,
        *,
        repo_id: str,
        claim_kind: str,
        about_record_ids: Sequence[str],
    ) -> tuple[CandidateClaimState, ...]:
        ordered_abouts = tuple(dict.fromkeys(about_record_ids))
        if not ordered_abouts:
            return ()
        query_coro = self.backend_driver.execute_query(
            _CURRENT_EVIDENCE_QUERY,
            repo_id=repo_id,
            about_record_ids=list(ordered_abouts),
        )
        try:
            rows = run_backend_coroutine_sync(query_coro)
        except Exception:
            _close_unstarted_coroutine(query_coro)
            return ()
        _close_unstarted_coroutine(query_coro)

        grouped: dict[str, list[PersistedEvidenceRecord]] = {}
        for row in rows:
            record = _parse_persisted_evidence_record(row)
            if record is None:
                continue
            if record.evidence.claim_kind != claim_kind:
                continue
            if not any(record_id in set(ordered_abouts) for record_id in record.about_record_ids):
                continue
            fact_key = (
                record.evidence.fact_key
                or canonical_claim_key(
                    claim_kind=record.evidence.claim_kind or "",
                    claim_text=record.evidence.claim_text or "",
                    about_record_ids=record.about_record_ids,
                )
            )
            grouped.setdefault(fact_key, []).append(record)

        candidates: list[CandidateClaimState] = []
        for records in grouped.values():
            representative = sorted(records, key=_legacy_evidence_sort_key, reverse=True)[0]
            claim_text = representative.evidence.claim_text
            claim_kind_value = representative.evidence.claim_kind
            if not claim_text or not claim_kind_value:
                continue
            candidate_about_ids = tuple(
                dict.fromkeys(
                    about_record_id
                    for record in records
                    for about_record_id in record.about_record_ids
                )
            )
            canonical_key = canonical_claim_key(
                claim_kind=claim_kind_value,
                claim_text=claim_text,
                about_record_ids=candidate_about_ids,
            )
            claim_id = claim_record_id(
                repo_id=repo_id,
                canonical_key=canonical_key,
                about_record_ids=candidate_about_ids,
            )
            claim = ClaimRecord(
                claim_id=claim_id,
                repo_id=repo_id,
                canonical_key=canonical_key,
                claim_text=claim_text,
                claim_kind=claim_kind_value,
                claim_subkind=representative.evidence.claim_subkind or "ownership_or_impact_scope",
                first_observed_at=representative.evidence.observed_at,
                latest_observed_at=representative.evidence.observed_at,
                is_current=representative.evidence.is_current,
                invalidated_at=representative.evidence.invalidated_at,
                observation_id=representative.evidence.observation_id,
                metadata={"legacy_backfill": True},
            )
            candidates.append(
                CandidateClaimState(
                    claim=claim,
                    about_record_ids=candidate_about_ids,
                    evidence_record_ids=tuple(record.evidence.evidence_id for record in records),
                    legacy_evidence_records=tuple(records),
                    backfill_required=True,
                )
            )
        return tuple(candidates)


class ClaimMutationPlanner:
    """Persist validated claims as first-class records with bounded reconciliation."""

    def __init__(
        self,
        *,
        backend_driver: BackendDriver | None,
        evidence_planner: EvidenceMutationPlanner | None = None,
        current_claim_reader: CurrentClaimReader | None = None,
        claim_resolution_service: ClaimResolutionService | None = None,
        fact_conflict_resolution_service: FactConflictResolutionService | None = None,
    ) -> None:
        self.backend_driver = backend_driver
        self.evidence_planner = evidence_planner or EvidenceMutationPlanner()
        self.current_claim_reader = current_claim_reader or CurrentClaimReader(
            backend_driver=backend_driver
        )
        self.claim_resolution_service = claim_resolution_service
        self.fact_conflict_resolution_service = fact_conflict_resolution_service

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
                    about_record_ids=claim.resolved_about_record_ids,
                ),
            )
            for claim in validation.validated_claims
        )

    def plan_validated_claims(
        self,
        *,
        observation: Observation,
        validation: ClaimValidationResult,
        snapshot: GraphSnapshot | None = None,
    ) -> ClaimMutationPlanResult:
        plans: list[MutationPlan] = []
        invalidated_count = 0
        for bundle in validation.validated_claims:
            result = self.plan_validated_claim_bundle(
                observation=observation,
                bundle=bundle,
                snapshot=snapshot,
            )
            plans.append(result.plan)
            invalidated_count += result.invalidated_count
        return ClaimMutationPlanResult(
            plan=merge_mutation_plans(*plans),
            invalidated_count=invalidated_count,
        )

    def plan_validated_claim_bundle(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        snapshot: GraphSnapshot | None = None,
    ) -> ClaimMutationPlanResult:
        prepared_records = self.evidence_planner.prepare_validated_claim_bundle(
            observation=observation,
            bundle=bundle,
            snapshot=snapshot,
        )
        if not prepared_records:
            return ClaimMutationPlanResult(plan=MutationPlan(), invalidated_count=0)

        about_record_ids = tuple(
            dict.fromkeys(
                record_id
                for prepared in prepared_records
                for record_id in prepared.about_record_ids
            )
        )
        canonical_key = canonical_claim_key(
            claim_kind=bundle.claim_kind,
            claim_text=bundle.claim_text,
            about_record_ids=about_record_ids,
        )
        new_claim = self._build_claim_record(
            observation=observation,
            bundle=bundle,
            about_record_ids=about_record_ids,
            canonical_key=canonical_key,
        )
        if bundle.assertion_status == "rejected":
            rejected_claim = ClaimRecord(
                claim_id=new_claim.claim_id,
                repo_id=new_claim.repo_id,
                canonical_key=new_claim.canonical_key,
                claim_text=new_claim.claim_text,
                claim_kind=new_claim.claim_kind,
                claim_subkind=new_claim.claim_subkind,
                first_observed_at=new_claim.first_observed_at,
                latest_observed_at=new_claim.latest_observed_at,
                is_current=False,
                observation_id=new_claim.observation_id,
                source_kind=new_claim.source_kind,
                source_id=new_claim.source_id,
                metadata=new_claim.metadata,
            )
            plan = merge_mutation_plans(
                self._plan_claim_record(
                    claim=rejected_claim,
                    about_record_ids=about_record_ids,
                ),
                self.evidence_planner.plan_prepared_records(
                    observation=observation,
                    prepared_records=prepared_records,
                    fact_state=self._build_fact_state(
                        observation=observation,
                        bundle=bundle,
                        claim=rejected_claim,
                    ),
                    claim_record_id=rejected_claim.claim_id,
                ),
            )
            return ClaimMutationPlanResult(plan=plan, invalidated_count=0)

        candidates = self.current_claim_reader.load_current_claims(
            repo_id=observation.repo_id,
            claim_kind=bundle.claim_kind,
            about_record_ids=about_record_ids,
            source_id=observation.source_id,
        )
        exact_match = next(
            (candidate for candidate in candidates if candidate.claim.canonical_key == canonical_key),
            None,
        )
        if exact_match is not None:
            updated_exact = self._merge_claim_observation(exact_match.claim, observation=observation)
            plan = merge_mutation_plans(
                self._plan_backfill_candidate(exact_match),
                self._plan_claim_record(
                    claim=updated_exact,
                    about_record_ids=exact_match.about_record_ids or about_record_ids,
                ),
                self.evidence_planner.plan_prepared_records(
                    observation=observation,
                    prepared_records=prepared_records,
                    fact_state=self._build_fact_state(
                        observation=observation,
                        bundle=bundle,
                        claim=updated_exact,
                    ),
                    claim_record_id=exact_match.claim.claim_id,
                ),
            )
            return ClaimMutationPlanResult(plan=plan, invalidated_count=0)

        ranked_candidates = _rank_candidates(
            new_claim=new_claim,
            new_about_record_ids=about_record_ids,
            candidates=candidates,
        )[:10]
        accepted_relations = self._resolve_candidate_relations(
            observation=observation,
            new_claim=new_claim,
            about_record_ids=about_record_ids,
            candidates=ranked_candidates,
        )
        same_as_candidates = [item for item in accepted_relations if item["resolution_type"] == "same_as"]
        if len(same_as_candidates) == 1:
            matched = next(
                candidate
                for candidate in ranked_candidates
                if candidate.claim.claim_id == same_as_candidates[0]["candidate_claim_id"]
            )
            updated_match = self._merge_claim_observation(matched.claim, observation=observation)
            plan = merge_mutation_plans(
                self._plan_backfill_candidate(matched),
                self._plan_claim_record(
                    claim=updated_match,
                    about_record_ids=matched.about_record_ids or about_record_ids,
                ),
                self.evidence_planner.plan_prepared_records(
                    observation=observation,
                    prepared_records=prepared_records,
                    fact_state=self._build_fact_state(
                        observation=observation,
                        bundle=bundle,
                        claim=updated_match,
                    ),
                    claim_record_id=matched.claim.claim_id,
                ),
            )
            return ClaimMutationPlanResult(plan=plan, invalidated_count=0)
        if len(same_as_candidates) > 1:
            accepted_relations = ()

        relation_candidates = tuple(
            relation
            for relation in accepted_relations
            if relation["resolution_type"] != "same_as"
        )
        relation_states = {
            candidate.claim.claim_id: candidate
            for candidate in ranked_candidates
        }
        invalidated_claim_ids: tuple[str, ...] = ()
        claim_to_write = new_claim
        invalidating_relations = [
            relation
            for relation in relation_candidates
            if relation["resolution_type"] in _INVALIDATING_RELATION_TYPES
        ]
        if invalidating_relations:
            newest_existing = max(
                (
                    relation_states[relation["candidate_claim_id"]]
                    for relation in invalidating_relations
                    if relation["candidate_claim_id"] in relation_states
                ),
                key=lambda candidate: _parse_datetime(candidate.claim.latest_observed_at) or datetime.min,
            )
            if _is_strictly_newer(newest_existing.claim.latest_observed_at, observation.observed_at):
                claim_to_write = ClaimRecord(
                    claim_id=new_claim.claim_id,
                    repo_id=new_claim.repo_id,
                    canonical_key=new_claim.canonical_key,
                    claim_text=new_claim.claim_text,
                    claim_kind=new_claim.claim_kind,
                    claim_subkind=new_claim.claim_subkind,
                    first_observed_at=new_claim.first_observed_at,
                    latest_observed_at=new_claim.latest_observed_at,
                    is_current=False,
                    invalidated_at=newest_existing.claim.latest_observed_at,
                    invalidated_by_claim_id=newest_existing.claim.claim_id,
                    observation_id=new_claim.observation_id,
                    source_kind=new_claim.source_kind,
                    source_id=new_claim.source_id,
                    metadata=new_claim.metadata,
                )
            else:
                invalidated_claim_ids = tuple(
                    dict.fromkeys(
                        relation["candidate_claim_id"]
                        for relation in invalidating_relations
                    )
                )
        plan = merge_mutation_plans(
            self._plan_claim_record(
                claim=claim_to_write,
                about_record_ids=about_record_ids,
            ),
            self.evidence_planner.plan_prepared_records(
                observation=observation,
                prepared_records=prepared_records,
                fact_state=self._build_fact_state(
                    observation=observation,
                    bundle=bundle,
                    claim=claim_to_write,
                ),
                claim_record_id=claim_to_write.claim_id,
            ),
            *(
                self._plan_backfill_candidate(relation_states[claim_id])
                for claim_id in {
                    relation["candidate_claim_id"]
                    for relation in relation_candidates
                    if relation["candidate_claim_id"] in relation_states
                }
            ),
            *(
                self._plan_claim_relation(
                    repo_id=observation.repo_id,
                    source_claim_id=claim_to_write.claim_id,
                    target_claim_id=relation["candidate_claim_id"],
                    relation_type=relation["resolution_type"],
                    rationale=relation["rationale"],
                    confidence=relation["confidence"],
                )
                for relation in relation_candidates
            ),
            *(
                self._plan_claim_state_update(
                    relation_states[claim_id].claim,
                    invalidated_at=observation.observed_at,
                    invalidated_by_claim_id=claim_to_write.claim_id,
                )
                for claim_id in invalidated_claim_ids
                if claim_id in relation_states
            ),
        )
        return ClaimMutationPlanResult(
            plan=plan,
            invalidated_count=len(invalidated_claim_ids),
        )

    def _merge_claim_observation(
        self,
        claim: ClaimRecord,
        *,
        observation: Observation,
    ) -> ClaimRecord:
        first_observed_at = claim.first_observed_at
        latest_observed_at = claim.latest_observed_at
        observation_time = _parse_datetime(observation.observed_at)
        first_time = _parse_datetime(first_observed_at)
        latest_time = _parse_datetime(latest_observed_at)
        if observation_time is not None:
            if first_time is None or observation_time < first_time:
                first_observed_at = observation.observed_at
            if latest_time is None or observation_time > latest_time:
                latest_observed_at = observation.observed_at
        return ClaimRecord(
            claim_id=claim.claim_id,
            repo_id=claim.repo_id,
            canonical_key=claim.canonical_key,
            claim_text=claim.claim_text,
            claim_kind=claim.claim_kind,
            claim_subkind=claim.claim_subkind,
            first_observed_at=first_observed_at,
            latest_observed_at=latest_observed_at,
            is_current=claim.is_current,
            invalidated_at=claim.invalidated_at,
            invalidated_by_claim_id=claim.invalidated_by_claim_id,
            observation_id=claim.observation_id,
            source_kind=claim.source_kind,
            source_id=claim.source_id,
            metadata=claim.metadata,
        )

    def _build_claim_record(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        about_record_ids: Sequence[str],
        canonical_key: str,
    ) -> ClaimRecord:
        return ClaimRecord(
            claim_id=claim_record_id(
                repo_id=observation.repo_id,
                canonical_key=canonical_key,
                about_record_ids=tuple(about_record_ids),
            ),
            repo_id=observation.repo_id,
            canonical_key=canonical_key,
            claim_text=bundle.claim_text,
            claim_kind=bundle.claim_kind,
            claim_subkind=bundle.claim_subkind,
            first_observed_at=observation.observed_at,
            latest_observed_at=observation.observed_at,
            is_current=True,
            observation_id=observation.observation_id,
            source_kind=observation.source_kind,
            source_id=observation.source_id,
            metadata={
                **bundle.metadata,
                "assertion_status": bundle.assertion_status,
                "claim_frame": bundle.claim_frame,
            },
        )

    def _build_fact_state(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        claim: ClaimRecord,
    ) -> EvidenceFactState:
        _ = bundle, claim
        return EvidenceFactState(
            observation_id=observation.observation_id,
            observed_at=observation.observed_at,
        )

    def _plan_claim_record(
        self,
        *,
        claim: ClaimRecord,
        about_record_ids: Sequence[str],
    ) -> MutationPlan:
        node = NodeRecord(
            record_id=claim.claim_id,
            record_type="claim",
            repo_id=claim.repo_id,
            group_id=claim.repo_id,
            labels=("BackendRecord", "Claim"),
            properties=_compact_properties(
                {
                    "repo_id": claim.repo_id,
                    "claim_id": claim.claim_id,
                    "canonical_key": claim.canonical_key,
                    "claim_text": claim.claim_text,
                    "claim_kind": claim.claim_kind,
                    "claim_subkind": claim.claim_subkind,
                    "source_kind": claim.source_kind,
                    "source_id": claim.source_id,
                    "observation_id": claim.observation_id,
                    "first_observed_at": claim.first_observed_at,
                    "latest_observed_at": claim.latest_observed_at,
                    "is_current": claim.is_current,
                    "invalidated_at": claim.invalidated_at,
                    "invalidated_by_claim_id": claim.invalidated_by_claim_id,
                    "metadata_json": json.dumps(claim.metadata, sort_keys=True, separators=(",", ":")),
                }
            ),
        )
        about_edges = tuple(
            EdgeRecord(
                record_id=f"claim_about:{claim.claim_id}:{target_record_id}",
                record_type="claim_about",
                repo_id=claim.repo_id,
                group_id=claim.repo_id,
                relationship_type="ABOUT",
                source_record_id=claim.claim_id,
                target_record_id=target_record_id,
            )
            for target_record_id in dict.fromkeys(about_record_ids)
        )
        derived_from_edge = (
            EdgeRecord(
                record_id=f"claim_derived_from:{claim.claim_id}:{claim.observation_id}",
                record_type="claim_derived_from",
                repo_id=claim.repo_id,
                group_id=claim.repo_id,
                relationship_type="DERIVED_FROM",
                source_record_id=claim.claim_id,
                target_record_id=claim.observation_id,
                properties={},
            )
            if claim.observation_id is not None
            else None
        )
        search_document = SearchDocumentRecord(
            record_kind=RecordKind.NODE,
            record_id=claim.claim_id,
            record_type="claim",
            repo_id=claim.repo_id,
            group_id=claim.repo_id,
            search_text=" ".join(
                part
                for part in (
                    claim.claim_text.strip(),
                    claim.claim_kind.strip(),
                    claim.claim_subkind.strip(),
                )
                if part
            ),
            properties=_compact_properties(
                {
                    "canonical_key": claim.canonical_key,
                    "claim_kind": claim.claim_kind,
                    "claim_subkind": claim.claim_subkind,
                    "source_kind": claim.source_kind,
                    "source_id": claim.source_id,
                    "observation_id": claim.observation_id,
                    "first_observed_at": claim.first_observed_at,
                    "latest_observed_at": claim.latest_observed_at,
                    "is_current": claim.is_current,
                }
            ),
        )
        return MutationPlan(
            upsert_nodes=(node,),
            upsert_edges=(
                *about_edges,
                *((derived_from_edge,) if derived_from_edge is not None else ()),
            ),
            upsert_search_documents=(search_document,),
        )

    def _plan_claim_relation(
        self,
        *,
        repo_id: str,
        source_claim_id: str,
        target_claim_id: str,
        relation_type: str,
        rationale: str,
        confidence: str,
    ) -> MutationPlan:
        edge_type = _RELATION_EDGE_TYPES.get(relation_type)
        if edge_type is None:
            return MutationPlan()
        edge = EdgeRecord(
            record_id=f"claim_relation:{source_claim_id}:{target_claim_id}:{edge_type.lower()}",
            record_type="claim_relation",
            repo_id=repo_id,
            group_id=repo_id,
            relationship_type=edge_type,
            source_record_id=source_claim_id,
            target_record_id=target_claim_id,
            properties=_compact_properties(
                {
                    "resolution_type": relation_type,
                    "resolution_confidence": confidence,
                    "resolution_rationale": rationale,
                }
            ),
        )
        return MutationPlan(upsert_edges=(edge,))

    def _plan_claim_state_update(
        self,
        claim: ClaimRecord,
        *,
        invalidated_at: str | None,
        invalidated_by_claim_id: str,
    ) -> MutationPlan:
        updated = ClaimRecord(
            claim_id=claim.claim_id,
            repo_id=claim.repo_id,
            canonical_key=claim.canonical_key,
            claim_text=claim.claim_text,
            claim_kind=claim.claim_kind,
            claim_subkind=claim.claim_subkind,
            first_observed_at=claim.first_observed_at,
            latest_observed_at=claim.latest_observed_at,
            is_current=False,
            invalidated_at=invalidated_at,
            invalidated_by_claim_id=invalidated_by_claim_id,
            observation_id=claim.observation_id,
            source_kind=claim.source_kind,
            source_id=claim.source_id,
            metadata=claim.metadata,
        )
        return self._plan_claim_record(claim=updated, about_record_ids=())

    def _plan_backfill_candidate(self, candidate: CandidateClaimState) -> MutationPlan:
        if not candidate.backfill_required:
            return MutationPlan()
        support_edges = tuple(
            EdgeRecord(
                record_id=f"supports_claim:{record.evidence.evidence_id}:{candidate.claim.claim_id}",
                record_type="supports_claim",
                repo_id=candidate.claim.repo_id,
                group_id=candidate.claim.repo_id,
                relationship_type="SUPPORTS_CLAIM",
                source_record_id=record.evidence.evidence_id,
                target_record_id=candidate.claim.claim_id,
                properties=_compact_properties(
                    {
                        "support_kind": record.evidence.support_kind,
                        "support_source": record.evidence.support_source,
                        "support_role": _legacy_support_role(record),
                        "support_rationale": _legacy_support_rationale(record),
                    }
                ),
            )
            for record in candidate.legacy_evidence_records
        )
        return merge_mutation_plans(
            self._plan_claim_record(
                claim=candidate.claim,
                about_record_ids=candidate.about_record_ids,
            ),
            MutationPlan(upsert_edges=support_edges),
        )

    def _resolve_candidate_relations(
        self,
        *,
        observation: Observation,
        new_claim: ClaimRecord,
        about_record_ids: Sequence[str],
        candidates: Sequence[CandidateClaimState],
    ) -> tuple[dict[str, str], ...]:
        if not candidates:
            return ()
        candidate_payloads = []
        for candidate in candidates:
            candidate_payloads.append(
                {
                    "candidate_claim_id": candidate.claim.claim_id,
                    "claim_text": candidate.claim.claim_text,
                    "claim_kind": candidate.claim.claim_kind,
                    "claim_subkind": candidate.claim.claim_subkind,
                    "about_record_ids": list(candidate.about_record_ids),
                    "latest_observed_at": candidate.claim.latest_observed_at,
                }
            )
        if self.claim_resolution_service is not None:
            try:
                resolution = self.claim_resolution_service.resolve(
                    repo_id=observation.repo_id,
                    new_claim={
                        "claim_text": new_claim.claim_text,
                        "claim_kind": new_claim.claim_kind,
                        "claim_subkind": new_claim.claim_subkind,
                        "about_record_ids": list(dict.fromkeys(about_record_ids)),
                    },
                    candidates=candidate_payloads,
                )
            except Exception:
                resolution = None
            if resolution is not None:
                accepted = tuple(
                    {
                        "candidate_claim_id": relation.candidate_claim_id,
                        "resolution_type": relation.resolution_type,
                        "confidence": relation.confidence,
                        "rationale": relation.rationale,
                    }
                    for relation in resolution.relations
                    if relation.confidence == "high" and relation.resolution_type != "unrelated"
                )
                emit_provenance_debug(
                    "claim_resolution",
                    {
                        "repo_id": observation.repo_id,
                        "observation_id": observation.observation_id,
                        "new_claim_id": new_claim.claim_id,
                        "candidate_count": len(candidates),
                        "accepted_relations": list(accepted),
                    },
                )
                if accepted:
                    return accepted
        return self._resolve_legacy_conflicts(
            observation=observation,
            new_claim=new_claim,
            about_record_ids=about_record_ids,
            candidates=candidates,
        )

    def _resolve_legacy_conflicts(
        self,
        *,
        observation: Observation,
        new_claim: ClaimRecord,
        about_record_ids: Sequence[str],
        candidates: Sequence[CandidateClaimState],
    ) -> tuple[dict[str, str], ...]:
        if self.fact_conflict_resolution_service is None or not candidates:
            return ()
        current_facts = [
            {
                "fact_id": _candidate_fact_identifier(candidate),
                "claim_text": candidate.claim.claim_text,
                "claim_kind": candidate.claim.claim_kind,
                "observed_at": candidate.claim.latest_observed_at,
                "about_record_ids": list(candidate.about_record_ids),
            }
            for candidate in candidates
        ]
        try:
            resolution = self.fact_conflict_resolution_service.resolve(
                repo_id=observation.repo_id,
                new_fact={
                    "claim_text": new_claim.claim_text,
                    "claim_kind": new_claim.claim_kind,
                    "about_record_ids": list(dict.fromkeys(about_record_ids)),
                },
                current_facts=current_facts,
            )
        except Exception:
            return ()
        candidate_by_fact_id = {
            _candidate_fact_identifier(candidate): candidate
            for candidate in candidates
        }
        accepted = tuple(
            {
                "candidate_claim_id": candidate_by_fact_id[fact_id].claim.claim_id,
                "resolution_type": "contradicts",
                "confidence": "high",
                "rationale": "Compatibility conflict resolution marked this claim as contradicted.",
            }
            for fact_id in resolution.contradicted_fact_ids
            if fact_id in candidate_by_fact_id
        )
        emit_provenance_debug(
            "claim_resolution_compat",
            {
                "repo_id": observation.repo_id,
                "observation_id": observation.observation_id,
                "new_claim_id": new_claim.claim_id,
                "candidate_count": len(candidates),
                "accepted_relations": list(accepted),
            },
        )
        return accepted


class ClaimPromotionService:
    """Boundary for validated-claim promotion into canonical claims."""

    def __init__(self, *, planner: ClaimMutationPlanner | None = None) -> None:
        self.planner = planner or ClaimMutationPlanner(backend_driver=None)

    def prepare_candidates(
        self,
        validation: ClaimValidationResult,
    ) -> tuple[ClaimPromotionCandidate, ...]:
        return self.planner.build_promotion_candidates(validation)


def _parse_persisted_claim_record(row: Mapping[str, Any]) -> CandidateClaimState | None:
    properties = dict(row.get("claim_properties") or {})
    metadata = _parse_json_object(properties.get("metadata_json"))
    claim_text = _string_or_none(properties.get("claim_text"))
    claim_kind = _string_or_none(properties.get("claim_kind"))
    claim_subkind = _string_or_none(properties.get("claim_subkind"))
    canonical_key = _string_or_none(properties.get("canonical_key"))
    claim_id = _string_or_none(properties.get("claim_id")) or _string_or_none(row.get("claim_id"))
    repo_id = _string_or_none(properties.get("repo_id"))
    if not all((claim_text, claim_kind, claim_subkind, canonical_key, claim_id, repo_id)):
        return None
    claim = ClaimRecord(
        claim_id=claim_id,
        repo_id=repo_id,
        canonical_key=canonical_key,
        claim_text=claim_text,
        claim_kind=claim_kind,
        claim_subkind=claim_subkind,
        first_observed_at=_string_or_none(properties.get("first_observed_at")),
        latest_observed_at=_string_or_none(properties.get("latest_observed_at")),
        is_current=_coerce_bool(properties.get("is_current"), default=True),
        invalidated_at=_string_or_none(properties.get("invalidated_at")),
        invalidated_by_claim_id=_string_or_none(properties.get("invalidated_by_claim_id")),
        observation_id=_string_or_none(properties.get("observation_id")),
        source_kind=_string_or_none(properties.get("source_kind")),
        source_id=_string_or_none(properties.get("source_id")),
        metadata=metadata,
    )
    about_record_ids = tuple(
        dict.fromkeys(
            str(record_id)
            for record_id in row.get("about_record_ids", ())
            if record_id is not None
        )
    )
    evidence_record_ids = tuple(
        dict.fromkeys(
            str(record_id)
            for record_id in row.get("evidence_record_ids", ())
            if record_id is not None
        )
    )
    return CandidateClaimState(
        claim=claim,
        about_record_ids=about_record_ids,
        evidence_record_ids=evidence_record_ids,
    )


def _parse_persisted_evidence_record(row: Mapping[str, Any]) -> PersistedEvidenceRecord | None:
    from .invalidation import _build_evidence_record  # local import avoids cycle at module load

    properties = dict(row.get("evidence_properties") or {})
    metadata = _parse_json_object(properties.get("metadata_json"))
    observation_properties = [
        dict(item)
        for item in row.get("observation_properties", ())
        if isinstance(item, Mapping)
    ]
    observation = observation_properties[0] if observation_properties else {}
    observation_ids = tuple(
        str(value)
        for value in row.get("observation_record_ids", ())
        if value is not None
    )
    claim_text = _string_or_none(properties.get("claim_text")) or _string_or_none(metadata.get("claim_text"))
    claim_kind = _string_or_none(properties.get("claim_kind")) or _string_or_none(metadata.get("claim_kind"))
    if not claim_text or not claim_kind:
        return None
    evidence = PersistedEvidenceRecord(
        evidence=_build_evidence_record(
            evidence_id=_string_or_none(properties.get("evidence_id")) or _string_or_none(row.get("evidence_id")) or "",
            repo_id=_string_or_none(properties.get("repo_id")) or "",
            evidence_kind=_string_or_none(properties.get("evidence_kind")) or "",
            quote_text=_string_or_none(properties.get("quote_text")) or "",
            summary_text=_string_or_none(properties.get("summary_text")) or "",
            span_start=_int_or_none(properties.get("span_start")),
            span_end=_int_or_none(properties.get("span_end")),
            support_kind=_string_or_none(properties.get("support_kind")) or "",
            support_source=_string_or_none(properties.get("support_source"))
            or _string_or_none(metadata.get("support_source")),
            fact_key=_string_or_none(properties.get("fact_key")) or canonical_claim_key(
                claim_kind=claim_kind,
                claim_text=claim_text,
            ),
            claim_text=claim_text,
            claim_kind=claim_kind,
            claim_subkind=_string_or_none(properties.get("claim_subkind"))
            or _string_or_none(metadata.get("claim_subkind")),
            assertion_status=_string_or_none(properties.get("assertion_status"))
            or _string_or_none(metadata.get("assertion_status")),
            claim_frame=_string_or_none(properties.get("claim_frame"))
            or _string_or_none(metadata.get("claim_frame")),
            claim_confidence=_string_or_none(properties.get("claim_confidence"))
            or _string_or_none(metadata.get("claim_confidence")),
            claim_derivation_kind=_string_or_none(properties.get("claim_derivation_kind"))
            or _string_or_none(metadata.get("claim_derivation_kind")),
            observation_id=_string_or_none(properties.get("observation_id"))
            or (observation_ids[0] if observation_ids else None)
            or _string_or_none(observation.get("observation_id"))
            or _string_or_none(observation.get("record_id")),
            observed_at=_string_or_none(properties.get("observed_at"))
            or _string_or_none(observation.get("observed_at")),
            is_current=_coerce_bool(properties.get("is_current"), default=True),
            invalidated_at=_string_or_none(properties.get("invalidated_at")),
            invalidated_by_fact_key=_string_or_none(properties.get("invalidated_by_fact_key")),
            invalidated_by_observation_id=_string_or_none(properties.get("invalidated_by_observation_id")),
            metadata=metadata,
        ),
        about_record_ids=tuple(
            dict.fromkeys(
                str(record_id)
                for record_id in row.get("about_record_ids", ())
                if record_id is not None
            )
        ),
    )
    return evidence


def _claim_candidate_in_scope(
    candidate: CandidateClaimState,
    *,
    about_record_ids: Sequence[str],
    source_id: str | None,
) -> bool:
    ordered_abouts = tuple(dict.fromkeys(about_record_ids))
    if ordered_abouts:
        return any(record_id in set(ordered_abouts) for record_id in candidate.about_record_ids)
    if source_id is None:
        return False
    return candidate.claim.source_id == source_id


def _rank_candidates(
    *,
    new_claim: ClaimRecord,
    new_about_record_ids: Sequence[str],
    candidates: Sequence[CandidateClaimState],
) -> list[CandidateClaimState]:
    return sorted(
        candidates,
        key=lambda candidate: (
            _about_overlap_score(new_about_record_ids, candidate),
            _lexical_overlap_score(new_claim.claim_text, candidate.claim.claim_text),
            _parse_datetime(candidate.claim.latest_observed_at) or datetime.min,
            candidate.claim.claim_id,
        ),
        reverse=True,
    )


def _about_overlap_score(
    new_about_record_ids: Sequence[str],
    candidate: CandidateClaimState,
) -> int:
    if not new_about_record_ids:
        return 0
    return len(set(new_about_record_ids) & set(candidate.about_record_ids))


def _lexical_overlap_score(left: str, right: str) -> int:
    left_tokens = set(_TOKEN_RE.findall(canonicalize_claim_text(left)))
    right_tokens = set(_TOKEN_RE.findall(canonicalize_claim_text(right)))
    return len(left_tokens & right_tokens)


def _candidate_sort_key(candidate: CandidateClaimState) -> tuple[bool, datetime, str]:
    observed_at = _parse_datetime(candidate.claim.latest_observed_at)
    return (
        observed_at is not None,
        observed_at or datetime.min,
        candidate.claim.claim_id,
    )


def _legacy_evidence_sort_key(record: PersistedEvidenceRecord) -> tuple[bool, datetime, str]:
    observed_at = _parse_datetime(record.evidence.observed_at)
    return (
        observed_at is not None,
        observed_at or datetime.min,
        record.evidence.evidence_id,
    )


def _legacy_support_role(record: PersistedEvidenceRecord) -> str:
    kind = record.evidence.evidence_kind
    if kind in {"change_summary", "workaround"}:
        return "states_change"
    if kind in {"state_detail", "scope_detail"}:
        return "provides_context"
    if kind in {"bug_signal", "test_expectation"}:
        return "reports_observation"
    if kind == "requirement_detail":
        return "requests_change"
    return "provides_context"


def _legacy_support_rationale(record: PersistedEvidenceRecord) -> str:
    summary = " ".join(str(record.evidence.summary_text or "").split()).strip()
    if summary:
        return summary
    return "Legacy evidence attached during claim backfill."


def _candidate_fact_identifier(candidate: CandidateClaimState) -> str:
    for record in candidate.legacy_evidence_records:
        if record.evidence.fact_key:
            return record.evidence.fact_key
    return _compat_fact_key(candidate.claim)


def _compat_fact_key(claim: ClaimRecord) -> str:
    return canonical_claim_key(
        claim_kind=claim.claim_kind,
        claim_text=claim.claim_text,
    )


def _is_strictly_newer(existing_observed_at: str | None, new_observed_at: str | None) -> bool:
    existing = _parse_datetime(existing_observed_at)
    new = _parse_datetime(new_observed_at)
    if existing is None or new is None:
        return False
    return existing > new


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _parse_json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _coerce_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return bool(value)


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _compact_properties(properties: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in properties.items()
        if value is not None
    }


def _close_unstarted_coroutine(value: object) -> None:
    if not inspect.iscoroutine(value):
        return
    if value.cr_frame is not None:
        return
    try:
        value.close()
    except RuntimeError:
        return
