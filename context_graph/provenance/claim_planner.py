"""Claim-centric mutation planning for validated provenance claims."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import inspect
import json
from typing import Any

from context_graph.models import GraphSnapshot
from context_graph.profiles import (
    ProfileDeactivation,
    ProfileRecord,
    RelationProfileSpec,
    semantic_claim_profile_from_claim,
    semantic_relation_profile_from_spec,
)
from context_graph.storage.backend import (
    BackendDriver,
    EdgeRecord,
    MutationPlan,
    NodeRecord,
    RecordKind,
    SearchMode,
    SearchQuery,
    SearchDocumentRecord,
    merge_mutation_plans,
    run_backend_coroutine_sync,
)

from .canonicalization import canonical_claim_key
from .claim_retrieval import ClaimSemanticRetrievalService, build_claim_retrieval_features
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
from .services import ClaimProfileKeyGenerationService, ClaimResolutionService
from .services import FactConflictResolutionService
from .validation import ClaimValidationResult, ValidatedClaimBundle

_CURRENT_CLAIM_QUERY = """
MATCH (c:BackendRecord)
WHERE c.record_type = "claim"
  AND c.repo_id = $repo_id
  AND coalesce(c.is_current, true) = true
OPTIONAL MATCH (c)-[:ABOUT]->(about:BackendRecord)
OPTIONAL MATCH (c)<-[:SUPPORTS_CLAIM]-(e:BackendRecord)
RETURN
    c.record_id AS claim_id,
    properties(c) AS claim_properties,
    collect(DISTINCT about.record_id) AS about_record_ids,
    collect(DISTINCT e.record_id) AS evidence_record_ids,
    collect(DISTINCT e.quote_text) AS support_texts
"""

_CURRENT_EVIDENCE_QUERY = """
MATCH (e:BackendRecord)
WHERE e.record_type = "evidence_record"
  AND e.repo_id = $repo_id
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


def _claim_relation_record_id(*, source_claim_id: str, target_claim_id: str, edge_type: str) -> str:
    return f"claim_relation:{source_claim_id}:{target_claim_id}:{edge_type.lower()}"


@dataclass(frozen=True, slots=True)
class ClaimPromotionCandidate:
    claim_text: str
    claim_kind: str
    canonical_key: str


@dataclass(frozen=True, slots=True)
class ClaimMutationPlanResult:
    plan: MutationPlan
    invalidated_count: int = 0
    invalidated_claim_ids: tuple[str, ...] = ()
    profiles: tuple[ProfileRecord, ...] = ()
    profile_deactivations: tuple[ProfileDeactivation, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateClaimState:
    claim: ClaimRecord
    about_record_ids: tuple[str, ...] = ()
    evidence_record_ids: tuple[str, ...] = ()
    support_texts: tuple[str, ...] = ()
    legacy_evidence_records: tuple[PersistedEvidenceRecord, ...] = ()
    backfill_required: bool = False
    origin: str = "persisted"


@dataclass(frozen=True, slots=True)
class CandidateRetrievalScore:
    candidate: CandidateClaimState
    same_thread: bool
    about_overlap: int
    lexical_overlap: int
    code_anchor_overlap: int
    numeric_anchor_overlap: int
    kind_compatibility: int
    retrieval_sources: tuple[str, ...]
    total_score: float


class CurrentClaimReader:
    """Load current claim nodes, with legacy evidence fallback during migration."""

    def __init__(self, *, backend_driver: BackendDriver | None) -> None:
        self.backend_driver = backend_driver

    def load_current_claims(
        self,
        *,
        repo_id: str,
        limit: int = 200,
    ) -> tuple[CandidateClaimState, ...]:
        if self.backend_driver is None:
            return ()
        claim_candidates = self._load_claim_nodes(
            repo_id=repo_id,
        )
        legacy_candidates = self._load_legacy_evidence_claims(
            repo_id=repo_id,
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
    ) -> tuple[CandidateClaimState, ...]:
        query_coro = self.backend_driver.execute_query(
            _CURRENT_CLAIM_QUERY,
            repo_id=repo_id,
        )
        try:
            rows = run_backend_coroutine_sync(query_coro)
        except Exception:
            _close_unstarted_coroutine(query_coro)
            return ()
        _close_unstarted_coroutine(query_coro)

        candidates: list[CandidateClaimState] = []
        for row in rows:
            claim = _parse_persisted_claim_record(row)
            if claim is None:
                continue
            candidates.append(claim)
        return tuple(candidates)

    def _load_legacy_evidence_claims(
        self,
        *,
        repo_id: str,
    ) -> tuple[CandidateClaimState, ...]:
        query_coro = self.backend_driver.execute_query(
            _CURRENT_EVIDENCE_QUERY,
            repo_id=repo_id,
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
                valid_from=representative.evidence.observed_at,
                valid_to=representative.evidence.invalidated_at,
                invalidated_at=representative.evidence.invalidated_at,
                observation_id=representative.evidence.observation_id,
                context_kind=None,
                context_id=None,
                metadata={"legacy_backfill": True},
            )
            candidates.append(
                CandidateClaimState(
                    claim=claim,
                    about_record_ids=candidate_about_ids,
                    evidence_record_ids=tuple(record.evidence.evidence_id for record in records),
                    support_texts=tuple(
                        dict.fromkeys(
                            record.evidence.quote_text
                            for record in records
                            if record.evidence.quote_text
                        )
                    ),
                    legacy_evidence_records=tuple(records),
                    backfill_required=True,
                )
            )
        return tuple(candidates)


class ClaimPlanningSession:
    """Mutable in-memory view of current claims during one planning run."""

    def __init__(
        self,
        *,
        planner: ClaimMutationPlanner,
        repo_id: str,
        seeded_candidates: Sequence[CandidateClaimState] = (),
        seed_loader: Callable[[], Sequence[CandidateClaimState]] | None = None,
    ) -> None:
        self.planner = planner
        self.repo_id = repo_id
        self._current_by_claim_id: dict[str, CandidateClaimState] = {}
        self._claim_ids_by_canonical_key: dict[str, set[str]] = {}
        self._seed_loader = seed_loader
        self._seeded = seed_loader is None
        for candidate in seeded_candidates:
            self.record_current_candidate(candidate)

    def _ensure_seeded(self) -> None:
        if self._seeded:
            return
        self._seeded = True
        if self._seed_loader is None:
            return
        for candidate in self._seed_loader():
            if candidate.claim.claim_id in self._current_by_claim_id:
                continue
            self.record_current_candidate(candidate)

    def current_candidates(self) -> tuple[CandidateClaimState, ...]:
        self._ensure_seeded()
        return tuple(
            sorted(
                self._current_by_claim_id.values(),
                key=_candidate_sort_key,
                reverse=True,
            )
        )

    def find_exact_match(self, canonical_key: str) -> CandidateClaimState | None:
        self._ensure_seeded()
        claim_ids = self._claim_ids_by_canonical_key.get(canonical_key, set())
        matches = [
            self._current_by_claim_id[claim_id]
            for claim_id in claim_ids
            if claim_id in self._current_by_claim_id
        ]
        if not matches:
            return None
        return sorted(matches, key=_candidate_sort_key, reverse=True)[0]

    def record_current_candidate(self, candidate: CandidateClaimState) -> None:
        previous = self._current_by_claim_id.get(candidate.claim.claim_id)
        if previous is not None:
            previous_ids = self._claim_ids_by_canonical_key.get(previous.claim.canonical_key)
            if previous_ids is not None:
                previous_ids.discard(previous.claim.claim_id)
                if not previous_ids:
                    self._claim_ids_by_canonical_key.pop(previous.claim.canonical_key, None)
        self._current_by_claim_id[candidate.claim.claim_id] = candidate
        self._claim_ids_by_canonical_key.setdefault(candidate.claim.canonical_key, set()).add(
            candidate.claim.claim_id
        )

    def discard_current_candidate(self, claim_id: str) -> None:
        candidate = self._current_by_claim_id.pop(claim_id, None)
        if candidate is None:
            return
        claim_ids = self._claim_ids_by_canonical_key.get(candidate.claim.canonical_key)
        if claim_ids is None:
            return
        claim_ids.discard(claim_id)
        if not claim_ids:
            self._claim_ids_by_canonical_key.pop(candidate.claim.canonical_key, None)

    def mark_backfilled(self, claim_ids: Sequence[str]) -> None:
        for claim_id in claim_ids:
            candidate = self._current_by_claim_id.get(claim_id)
            if candidate is None or not candidate.backfill_required:
                continue
            self._current_by_claim_id[claim_id] = replace(candidate, backfill_required=False)

    def update_matched_candidate(
        self,
        matched: CandidateClaimState,
        *,
        claim: ClaimRecord,
        about_record_ids: Sequence[str],
        support_texts: Sequence[str],
        backfill_consumed: bool = False,
    ) -> None:
        resolved_about_ids = tuple(matched.about_record_ids or tuple(dict.fromkeys(about_record_ids)))
        merged_support_texts = _merge_support_texts(matched.support_texts, support_texts)
        updated = replace(
            matched,
            claim=claim,
            about_record_ids=resolved_about_ids,
            support_texts=merged_support_texts,
            backfill_required=False if backfill_consumed else matched.backfill_required,
        )
        self.record_current_candidate(updated)

    def add_pending_claim(
        self,
        *,
        claim: ClaimRecord,
        about_record_ids: Sequence[str],
        support_texts: Sequence[str],
    ) -> None:
        self.record_current_candidate(
            CandidateClaimState(
                claim=claim,
                about_record_ids=tuple(dict.fromkeys(about_record_ids)),
                support_texts=tuple(dict.fromkeys(support_texts)),
                origin="pending_batch",
            )
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
        profiles: list[ProfileRecord] = []
        for bundle in validation.validated_claims:
            result = self.planner._plan_validated_claim_bundle_in_session(
                session=self,
                observation=observation,
                bundle=bundle,
                snapshot=snapshot,
            )
            plans.append(result.plan)
            invalidated_count += result.invalidated_count
            profiles.extend(result.profiles)
        return ClaimMutationPlanResult(
            plan=merge_mutation_plans(*plans),
            invalidated_count=invalidated_count,
            profiles=tuple(profiles),
        )

    def plan_validated_claim_bundle(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        snapshot: GraphSnapshot | None = None,
    ) -> ClaimMutationPlanResult:
        return self.planner._plan_validated_claim_bundle_in_session(
            session=self,
            observation=observation,
            bundle=bundle,
            snapshot=snapshot,
        )


class ClaimResolutionCandidateRetriever:
    """Expand planner claim candidates through lexical and semantic retrieval."""

    def __init__(
        self,
        *,
        backend_driver: BackendDriver | None,
        semantic_claim_search_service: ClaimSemanticRetrievalService | None = None,
    ) -> None:
        self.backend_driver = backend_driver
        self.semantic_claim_search_service = semantic_claim_search_service

    def retrieve(
        self,
        *,
        candidates: Sequence[CandidateClaimState],
        new_claim: ClaimRecord,
        observation: Observation,
        about_record_ids: Sequence[str],
        support_texts: Sequence[str],
        limit: int = 10,
    ) -> tuple[CandidateRetrievalScore, ...]:
        current_by_claim_id = {
            candidate.claim.claim_id: candidate
            for candidate in candidates
            if candidate.claim.is_current
        }
        if not current_by_claim_id:
            return ()

        lexical_ranks: dict[str, int] = {}
        lexical_scores: dict[str, float] = {}
        if self.backend_driver is not None and hasattr(self.backend_driver, "search"):
            search_coro = self.backend_driver.search.search(
                SearchQuery(
                    mode=SearchMode.LEXICAL,
                    text=new_claim.claim_text,
                    record_kinds=(RecordKind.NODE,),
                    record_types=("claim",),
                    repo_ids=(new_claim.repo_id,),
                    limit=max(limit * 4, limit),
                    filters={"is_current": True},
                )
            )
            try:
                lexical_hits = run_backend_coroutine_sync(search_coro)
            except Exception:
                _close_unstarted_coroutine(search_coro)
                lexical_hits = []
            else:
                _close_unstarted_coroutine(search_coro)
            for rank, hit in enumerate(lexical_hits, start=1):
                claim_id = hit.ref.record_id
                if claim_id not in current_by_claim_id or claim_id in lexical_ranks:
                    continue
                lexical_ranks[claim_id] = rank
                lexical_scores[claim_id] = float(hit.score)

        semantic_ranks: dict[str, int] = {}
        semantic_scores: dict[str, float] = {}
        if self.semantic_claim_search_service is not None:
            try:
                semantic_result = self.semantic_claim_search_service.search(
                    repo_id=new_claim.repo_id,
                    query=new_claim.claim_text,
                    limit=max(limit * 4, limit),
                    active_only=True,
                    llm_enabled=True,
                )
            except Exception:
                semantic_result = None
            if semantic_result is not None:
                for rank, hit in enumerate(semantic_result.hits, start=1):
                    if hit.claim_id not in current_by_claim_id or hit.claim_id in semantic_ranks:
                        continue
                    semantic_ranks[hit.claim_id] = rank
                    semantic_scores[hit.claim_id] = float(hit.score)

        if not lexical_ranks and not semantic_ranks:
            return ()

        new_features = build_claim_retrieval_features(
            claim_text=new_claim.claim_text,
            support_texts=support_texts,
            fallback_texts=((observation.title,) if observation.title else ()),
            about_record_ids=about_record_ids,
        )
        selected: list[CandidateRetrievalScore] = []
        for claim_id, candidate in current_by_claim_id.items():
            lexical_rank = lexical_ranks.get(claim_id)
            semantic_rank = semantic_ranks.get(claim_id)
            if lexical_rank is None and semantic_rank is None:
                continue
            base_score = _score_candidate(
                candidate=candidate,
                new_claim=new_claim,
                observation=observation,
                new_about_record_ids=about_record_ids,
                new_features=new_features,
            )
            if not _candidate_is_viable_from_search(
                base_score,
                lexical_hit=lexical_rank is not None,
                semantic_hit=semantic_rank is not None,
            ):
                continue
            selected.append(
                _score_candidate_from_search(
                    base_score,
                    lexical_rank=lexical_rank,
                    lexical_score=lexical_scores.get(claim_id),
                    semantic_rank=semantic_rank,
                    semantic_score=semantic_scores.get(claim_id),
                )
            )
        return tuple(
            sorted(
                selected,
                key=_candidate_retrieval_sort_key,
                reverse=True,
            )
        )


class ClaimMutationPlanner:
    """Persist validated claims as first-class records with bounded reconciliation."""

    def __init__(
        self,
        *,
        backend_driver: BackendDriver | None,
        evidence_planner: EvidenceMutationPlanner | None = None,
        current_claim_reader: CurrentClaimReader | None = None,
        claim_candidate_retriever: ClaimResolutionCandidateRetriever | None = None,
        claim_profile_key_generation_service: ClaimProfileKeyGenerationService | None = None,
        claim_resolution_service: ClaimResolutionService | None = None,
        fact_conflict_resolution_service: FactConflictResolutionService | None = None,
    ) -> None:
        self.backend_driver = backend_driver
        self.evidence_planner = evidence_planner or EvidenceMutationPlanner()
        self.current_claim_reader = current_claim_reader or CurrentClaimReader(
            backend_driver=backend_driver
        )
        self.claim_candidate_retriever = claim_candidate_retriever or ClaimResolutionCandidateRetriever(
            backend_driver=backend_driver,
        )
        self.claim_profile_key_generation_service = claim_profile_key_generation_service
        self.claim_resolution_service = claim_resolution_service
        self.fact_conflict_resolution_service = fact_conflict_resolution_service

    def begin_session(self, *, repo_id: str) -> ClaimPlanningSession:
        return ClaimPlanningSession(
            planner=self,
            repo_id=repo_id,
            seed_loader=lambda: self.current_claim_reader.load_current_claims(repo_id=repo_id),
        )

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
        session: ClaimPlanningSession | None = None,
    ) -> ClaimMutationPlanResult:
        claim_session = session or self.begin_session(repo_id=observation.repo_id)
        return claim_session.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=snapshot,
        )

    def plan_validated_claim_bundle(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        snapshot: GraphSnapshot | None = None,
        session: ClaimPlanningSession | None = None,
    ) -> ClaimMutationPlanResult:
        claim_session = session or self.begin_session(repo_id=observation.repo_id)
        return claim_session.plan_validated_claim_bundle(
            observation=observation,
            bundle=bundle,
            snapshot=snapshot,
        )

    def _plan_validated_claim_bundle_in_session(
        self,
        *,
        session: ClaimPlanningSession,
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

        new_support_texts = tuple(
            dict.fromkeys(
                prepared.evidence.quote_text
                for prepared in prepared_records
                if prepared.evidence.quote_text
            )
        )

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
                ingested_at=new_claim.ingested_at,
                valid_from=new_claim.valid_from,
                valid_to=new_claim.valid_from,
                invalidated_at=new_claim.valid_from,
                observation_id=new_claim.observation_id,
                source_kind=new_claim.source_kind,
                source_id=new_claim.source_id,
                context_kind=new_claim.context_kind,
                context_id=new_claim.context_id,
                thread_kind=new_claim.thread_kind,
                thread_id=new_claim.thread_id,
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
            return ClaimMutationPlanResult(
                plan=plan,
                invalidated_count=0,
                profiles=(
                    self._build_claim_profile(
                        claim=rejected_claim,
                        about_record_ids=about_record_ids,
                        support_texts=new_support_texts,
                    ),
                ),
            )

        candidates = session.current_candidates()
        exact_match = session.find_exact_match(canonical_key)
        hybrid_candidates = self.claim_candidate_retriever.retrieve(
            candidates=candidates,
            new_claim=new_claim,
            observation=observation,
            about_record_ids=about_record_ids,
            support_texts=new_support_texts,
        )
        thread_candidates = _select_candidate_pass(
            candidates=candidates,
            new_claim=new_claim,
            observation=observation,
            about_record_ids=about_record_ids,
            support_texts=new_support_texts,
            thread_scoped=True,
        )
        global_candidates = _select_candidate_pass(
            candidates=candidates,
            new_claim=new_claim,
            observation=observation,
            about_record_ids=about_record_ids,
            support_texts=new_support_texts,
            thread_scoped=False,
            excluded_claim_ids={score.candidate.claim.claim_id for score in thread_candidates},
        )
        if exact_match is not None:
            updated_exact = self._merge_claim_observation(exact_match.claim, observation=observation)
            resolved_about_record_ids = exact_match.about_record_ids or about_record_ids
            merged_support_texts = _merge_support_texts(exact_match.support_texts, new_support_texts)
            plan = merge_mutation_plans(
                self._plan_backfill_candidate(exact_match),
                self._plan_claim_record(
                    claim=updated_exact,
                    about_record_ids=resolved_about_record_ids,
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
            session.update_matched_candidate(
                exact_match,
                    claim=updated_exact,
                    about_record_ids=resolved_about_record_ids,
                    support_texts=new_support_texts,
                    backfill_consumed=exact_match.backfill_required,
                )
            return ClaimMutationPlanResult(
                plan=plan,
                invalidated_count=0,
                profiles=(
                    self._build_claim_profile(
                        claim=updated_exact,
                        about_record_ids=resolved_about_record_ids,
                        support_texts=merged_support_texts,
                    ),
                ),
            )

        thread_hybrid_candidates = tuple(score for score in hybrid_candidates if score.same_thread)
        global_hybrid_candidates = tuple(score for score in hybrid_candidates if not score.same_thread)
        accepted_relations, ranked_candidates = self._resolve_candidate_pass(
            observation=observation,
            new_claim=new_claim,
            about_record_ids=about_record_ids,
            support_texts=new_support_texts,
            candidates=_merge_candidate_scores(
                thread_hybrid_candidates,
                thread_candidates,
            ),
            scope_label="same_context",
        )
        same_as_candidates = [item for item in accepted_relations if item["resolution_type"] == "same_as"]
        if not accepted_relations or len(same_as_candidates) > 1:
            accepted_relations, ranked_candidates = self._resolve_candidate_pass(
                observation=observation,
                new_claim=new_claim,
                about_record_ids=about_record_ids,
                support_texts=new_support_texts,
                candidates=_merge_candidate_scores(
                    global_hybrid_candidates,
                    global_candidates,
                ),
                scope_label="global",
            )
            same_as_candidates = [
                item for item in accepted_relations if item["resolution_type"] == "same_as"
            ]
        if len(same_as_candidates) == 1:
            matched = next(
                candidate
                for candidate in ranked_candidates
                if candidate.claim.claim_id == same_as_candidates[0]["candidate_claim_id"]
            )
            updated_match = self._merge_claim_observation(matched.claim, observation=observation)
            resolved_about_record_ids = matched.about_record_ids or about_record_ids
            merged_support_texts = _merge_support_texts(matched.support_texts, new_support_texts)
            plan = merge_mutation_plans(
                self._plan_backfill_candidate(matched),
                self._plan_claim_record(
                    claim=updated_match,
                    about_record_ids=resolved_about_record_ids,
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
            session.update_matched_candidate(
                matched,
                    claim=updated_match,
                    about_record_ids=resolved_about_record_ids,
                    support_texts=new_support_texts,
                    backfill_consumed=matched.backfill_required,
                )
            return ClaimMutationPlanResult(
                plan=plan,
                invalidated_count=0,
                profiles=(
                    self._build_claim_profile(
                        claim=updated_match,
                        about_record_ids=resolved_about_record_ids,
                        support_texts=merged_support_texts,
                    ),
                ),
            )
        if len(same_as_candidates) > 1:
            accepted_relations = ()

        relation_candidates = tuple(
            relation
            for relation in accepted_relations
            if relation["resolution_type"] != "same_as"
        )[:10]
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
                    ingested_at=new_claim.ingested_at,
                    valid_from=new_claim.valid_from,
                    valid_to=newest_existing.claim.latest_observed_at,
                    invalidated_at=newest_existing.claim.latest_observed_at,
                    invalidated_by_claim_id=newest_existing.claim.claim_id,
                    observation_id=new_claim.observation_id,
                    source_kind=new_claim.source_kind,
                    source_id=new_claim.source_id,
                    context_kind=new_claim.context_kind,
                    context_id=new_claim.context_id,
                    thread_kind=new_claim.thread_kind,
                    thread_id=new_claim.thread_id,
                    metadata=new_claim.metadata,
                )
            else:
                invalidated_claim_ids = tuple(
                    dict.fromkeys(
                        relation["candidate_claim_id"]
                        for relation in invalidating_relations
                    )
                )
        invalidated_claims = tuple(
            (
                claim_id,
                self._build_invalidated_claim_record(
                    relation_states[claim_id].claim,
                    invalidated_at=observation.observed_at,
                    invalidated_by_claim_id=claim_to_write.claim_id,
                ),
            )
            for claim_id in invalidated_claim_ids
            if claim_id in relation_states
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
                    observed_at=observation.observed_at or claim_to_write.latest_observed_at or "",
                    is_active=(
                        claim_to_write.is_current
                        and relation["candidate_claim_id"] not in invalidated_claim_ids
                        and (
                            relation_states[relation["candidate_claim_id"]].claim.is_current
                            if relation["candidate_claim_id"] in relation_states
                            else True
                        )
                    ),
                )
                for relation in relation_candidates
            ),
            *(
                self._plan_claim_state_update(
                    invalidated_claim,
                    about_record_ids=relation_states[claim_id].about_record_ids,
                )
                for claim_id, invalidated_claim in invalidated_claims
            ),
        )
        session.mark_backfilled(
            [
                claim_id
                for claim_id in {
                    relation["candidate_claim_id"]
                    for relation in relation_candidates
                    if relation["candidate_claim_id"] in relation_states
                }
            ]
        )
        for claim_id in invalidated_claim_ids:
            session.discard_current_candidate(claim_id)
        if claim_to_write.is_current:
            session.add_pending_claim(
                claim=claim_to_write,
                about_record_ids=about_record_ids,
                support_texts=new_support_texts,
            )
        else:
            session.discard_current_candidate(claim_to_write.claim_id)
        relation_profiles = tuple(
            profile
            for relation in relation_candidates
            for profile in (
                self._build_claim_relation_profile(
                    repo_id=observation.repo_id,
                    source_claim_id=claim_to_write.claim_id,
                    target_claim_id=relation["candidate_claim_id"],
                    relation_type=relation["resolution_type"],
                    rationale=relation["rationale"],
                    confidence=relation["confidence"],
                    is_active=(
                        claim_to_write.is_current
                        and relation["candidate_claim_id"] not in invalidated_claim_ids
                        and (
                            relation_states[relation["candidate_claim_id"]].claim.is_current
                            if relation["candidate_claim_id"] in relation_states
                            else True
                        )
                    ),
                    updated_at=observation.observed_at or claim_to_write.latest_observed_at or "",
                ),
            )
            if profile is not None
        )
        return ClaimMutationPlanResult(
            plan=plan,
            invalidated_count=len(invalidated_claim_ids),
            invalidated_claim_ids=invalidated_claim_ids,
            profiles=(
                self._build_claim_profile(
                    claim=claim_to_write,
                    about_record_ids=about_record_ids,
                    support_texts=new_support_texts,
                ),
                *(
                    self._build_claim_profile(
                        claim=invalidated_claim,
                        about_record_ids=relation_states[claim_id].about_record_ids,
                        support_texts=relation_states[claim_id].support_texts,
                    )
                    for claim_id, invalidated_claim in invalidated_claims
                ),
                *relation_profiles,
            ),
        )

    def _merge_claim_observation(
        self,
        claim: ClaimRecord,
        *,
        observation: Observation,
    ) -> ClaimRecord:
        first_observed_at = claim.first_observed_at
        latest_observed_at = claim.latest_observed_at
        valid_from = claim.valid_from or claim.first_observed_at
        observation_time = _parse_datetime(observation.observed_at)
        first_time = _parse_datetime(first_observed_at)
        latest_time = _parse_datetime(latest_observed_at)
        valid_from_time = _parse_datetime(valid_from)
        if observation_time is not None:
            if first_time is None or observation_time < first_time:
                first_observed_at = observation.observed_at
            if valid_from_time is None or observation_time < valid_from_time:
                valid_from = observation.observed_at
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
            ingested_at=claim.ingested_at,
            valid_from=valid_from,
            valid_to=claim.valid_to,
            invalidated_at=claim.invalidated_at,
            invalidated_by_claim_id=claim.invalidated_by_claim_id,
            observation_id=claim.observation_id,
            source_kind=claim.source_kind,
            source_id=claim.source_id,
            context_kind=claim.context_kind,
            context_id=claim.context_id,
            thread_kind=claim.thread_kind,
            thread_id=claim.thread_id,
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
            ingested_at=observation.ingested_at,
            valid_from=observation.observed_at,
            observation_id=observation.observation_id,
            source_kind=observation.source_kind,
            source_id=observation.source_id,
            context_kind=observation.context_kind,
            context_id=observation.context_id,
            thread_kind=observation.thread_kind,
            thread_id=observation.thread_id,
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
                    "context_kind": claim.context_kind,
                    "context_id": claim.context_id,
                    "first_observed_at": claim.first_observed_at,
                    "latest_observed_at": claim.latest_observed_at,
                    "ingested_at": claim.ingested_at,
                    "valid_from": claim.valid_from,
                    "valid_to": claim.valid_to,
                    "thread_kind": claim.thread_kind,
                    "thread_id": claim.thread_id,
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
                    "context_kind": claim.context_kind,
                    "context_id": claim.context_id,
                    "first_observed_at": claim.first_observed_at,
                    "latest_observed_at": claim.latest_observed_at,
                    "ingested_at": claim.ingested_at,
                    "valid_from": claim.valid_from,
                    "valid_to": claim.valid_to,
                    "thread_kind": claim.thread_kind,
                    "thread_id": claim.thread_id,
                    "is_current": claim.is_current,
                }
            ),
        )
        return MutationPlan(
            upsert_nodes=(node,),
            upsert_edges=about_edges,
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
        observed_at: str,
        is_active: bool,
    ) -> MutationPlan:
        edge_type = _RELATION_EDGE_TYPES.get(relation_type)
        if edge_type is None:
            return MutationPlan()
        record_id = _claim_relation_record_id(
            source_claim_id=source_claim_id,
            target_claim_id=target_claim_id,
            edge_type=edge_type,
        )
        edge = EdgeRecord(
            record_id=record_id,
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
                    "observed_at": observed_at,
                    "is_active": is_active,
                }
            ),
        )
        search_document = SearchDocumentRecord(
            record_kind=RecordKind.EDGE,
            record_id=record_id,
            record_type="claim_relation",
            repo_id=repo_id,
            group_id=repo_id,
            search_text=" ".join(
                part
                for part in (
                    rationale.strip(),
                    relation_type.strip(),
                )
                if part
            ),
            properties=_compact_properties(
                {
                    "relation_type": relation_type,
                    "resolution_confidence": confidence,
                    "resolution_rationale": rationale,
                    "source_record_id": source_claim_id,
                    "target_record_id": target_claim_id,
                    "observed_at": observed_at,
                    "is_active": is_active,
                }
            ),
        )
        return MutationPlan(upsert_edges=(edge,), upsert_search_documents=(search_document,))

    def _plan_claim_state_update(
        self,
        claim: ClaimRecord,
        *,
        about_record_ids: Sequence[str],
    ) -> MutationPlan:
        return self._plan_claim_record(claim=claim, about_record_ids=about_record_ids)

    def _build_invalidated_claim_record(
        self,
        claim: ClaimRecord,
        *,
        invalidated_at: str | None,
        invalidated_by_claim_id: str,
    ) -> ClaimRecord:
        return ClaimRecord(
            claim_id=claim.claim_id,
            repo_id=claim.repo_id,
            canonical_key=claim.canonical_key,
            claim_text=claim.claim_text,
            claim_kind=claim.claim_kind,
            claim_subkind=claim.claim_subkind,
            first_observed_at=claim.first_observed_at,
            latest_observed_at=claim.latest_observed_at,
            is_current=False,
            ingested_at=claim.ingested_at,
            valid_from=claim.valid_from,
            valid_to=invalidated_at,
            invalidated_at=invalidated_at,
            invalidated_by_claim_id=invalidated_by_claim_id,
            observation_id=claim.observation_id,
            source_kind=claim.source_kind,
            source_id=claim.source_id,
            context_kind=claim.context_kind,
            context_id=claim.context_id,
            thread_kind=claim.thread_kind,
            thread_id=claim.thread_id,
            metadata=claim.metadata,
        )

    def _build_claim_profile(
        self,
        *,
        claim: ClaimRecord,
        about_record_ids: Sequence[str],
        support_texts: Sequence[str],
    ) -> ProfileRecord:
        generated_keys = (
            self.claim_profile_key_generation_service.generate(
                repo_id=claim.repo_id,
                claim_text=claim.claim_text,
                claim_subkind=claim.claim_subkind,
                support_texts=support_texts,
            )
            if self.claim_profile_key_generation_service is not None
            else ()
        )
        return semantic_claim_profile_from_claim(
            claim,
            about_record_ids=about_record_ids,
            support_texts=support_texts,
            generated_keys=generated_keys,
        )

    def _build_claim_relation_profile(
        self,
        *,
        repo_id: str,
        source_claim_id: str,
        target_claim_id: str,
        relation_type: str,
        rationale: str,
        confidence: str,
        is_active: bool,
        updated_at: str,
    ) -> ProfileRecord | None:
        edge_type = _RELATION_EDGE_TYPES.get(relation_type)
        if edge_type is None:
            return None
        return semantic_relation_profile_from_spec(
            RelationProfileSpec(
                repo_id=repo_id,
                profile_target_id=_claim_relation_record_id(
                    source_claim_id=source_claim_id,
                    target_claim_id=target_claim_id,
                    edge_type=edge_type,
                ),
                profile_target_kind="Relation",
                relation_type=relation_type,
                source_record_id=source_claim_id,
                source_record_type="claim",
                target_record_id=target_claim_id,
                target_record_type="claim",
                value_text=rationale,
                metadata={
                    "resolution_confidence": confidence,
                    "observed_at": updated_at,
                },
                is_active=is_active,
                updated_at=updated_at,
            )
        )

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
                claim=_claim_with_backfill_ingested_at(candidate.claim),
                about_record_ids=candidate.about_record_ids,
            ),
            MutationPlan(upsert_edges=support_edges),
        )

    def _resolve_candidate_pass(
        self,
        *,
        observation: Observation,
        new_claim: ClaimRecord,
        about_record_ids: Sequence[str],
        support_texts: Sequence[str],
        candidates: Sequence[CandidateRetrievalScore],
        scope_label: str,
    ) -> tuple[tuple[dict[str, str], ...], tuple[CandidateClaimState, ...]]:
        if not candidates:
            return (), ()
        ranked_scores = tuple(
            sorted(
                candidates,
                key=_candidate_retrieval_sort_key,
                reverse=True,
            )[:10]
        )
        ranked_candidates = tuple(score.candidate for score in ranked_scores)
        emit_provenance_debug(
            "claim_candidate_retrieval",
            {
                "repo_id": observation.repo_id,
                "observation_id": observation.observation_id,
                "new_claim_id": new_claim.claim_id,
                "scope_label": scope_label,
                "candidate_count": len(ranked_scores),
                "candidates": [
                    {
                        "candidate_claim_id": score.candidate.claim.claim_id,
                        "claim_text": score.candidate.claim.claim_text,
                        "claim_kind": score.candidate.claim.claim_kind,
                        "claim_subkind": score.candidate.claim.claim_subkind,
                        "candidate_origin": score.candidate.origin,
                        "same_thread": score.same_thread,
                        "about_overlap": score.about_overlap,
                        "lexical_overlap": score.lexical_overlap,
                        "code_anchor_overlap": score.code_anchor_overlap,
                        "numeric_anchor_overlap": score.numeric_anchor_overlap,
                        "kind_compatibility": score.kind_compatibility,
                        "retrieval_sources": list(score.retrieval_sources),
                        "total_score": score.total_score,
                    }
                    for score in ranked_scores
                ],
            },
        )
        accepted_relations = self._resolve_candidate_relations(
            observation=observation,
            new_claim=new_claim,
            about_record_ids=about_record_ids,
            candidates=ranked_candidates,
            scope_label=scope_label,
        )
        return accepted_relations, ranked_candidates

    def _resolve_candidate_relations(
        self,
        *,
        observation: Observation,
        new_claim: ClaimRecord,
        about_record_ids: Sequence[str],
        candidates: Sequence[CandidateClaimState],
        scope_label: str,
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
                candidate_ids = {candidate.claim.claim_id for candidate in candidates}
                accepted = tuple(
                    {
                        "candidate_claim_id": relation.candidate_claim_id,
                        "resolution_type": relation.resolution_type,
                        "confidence": relation.confidence,
                        "rationale": relation.rationale,
                    }
                    for relation in resolution.relations
                    if relation.candidate_claim_id in candidate_ids
                    and relation.confidence == "high"
                    and relation.resolution_type != "unrelated"
                )
                emit_provenance_debug(
                    "claim_resolution",
                    {
                        "repo_id": observation.repo_id,
                        "observation_id": observation.observation_id,
                        "new_claim_id": new_claim.claim_id,
                        "scope_label": scope_label,
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
            scope_label=scope_label,
        )

    def _resolve_legacy_conflicts(
        self,
        *,
        observation: Observation,
        new_claim: ClaimRecord,
        about_record_ids: Sequence[str],
        candidates: Sequence[CandidateClaimState],
        scope_label: str,
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
                "scope_label": scope_label,
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
        ingested_at=_string_or_none(properties.get("ingested_at")),
        valid_from=_string_or_none(properties.get("valid_from")),
        valid_to=_string_or_none(properties.get("valid_to")),
        invalidated_at=_string_or_none(properties.get("invalidated_at")),
        invalidated_by_claim_id=_string_or_none(properties.get("invalidated_by_claim_id")),
        observation_id=_string_or_none(properties.get("observation_id")),
        source_kind=_string_or_none(properties.get("source_kind")),
        source_id=_string_or_none(properties.get("source_id")),
        context_kind=_string_or_none(properties.get("context_kind"))
        or _string_or_none(properties.get("thread_kind"))
        or _derive_thread_kind(_string_or_none(properties.get("source_kind"))),
        context_id=_string_or_none(properties.get("context_id"))
        or _string_or_none(properties.get("thread_id"))
        or _derive_thread_id(
            source_kind=_string_or_none(properties.get("source_kind")),
            source_id=_string_or_none(properties.get("source_id")),
        ),
        thread_kind=_string_or_none(properties.get("thread_kind"))
        or _derive_thread_kind(_string_or_none(properties.get("source_kind"))),
        thread_id=_string_or_none(properties.get("thread_id"))
        or _derive_thread_id(
            source_kind=_string_or_none(properties.get("source_kind")),
            source_id=_string_or_none(properties.get("source_id")),
        ),
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
        support_texts=tuple(
            dict.fromkeys(
                str(text)
                for text in row.get("support_texts", ())
                if isinstance(text, str) and text.strip()
            )
        ),
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


def _select_candidate_pass(
    *,
    candidates: Sequence[CandidateClaimState],
    new_claim: ClaimRecord,
    observation: Observation,
    about_record_ids: Sequence[str],
    support_texts: Sequence[str],
    thread_scoped: bool,
    excluded_claim_ids: set[str] | None = None,
) -> tuple[CandidateRetrievalScore, ...]:
    excluded_claim_ids = excluded_claim_ids or set()
    new_features = build_claim_retrieval_features(
        claim_text=new_claim.claim_text,
        support_texts=support_texts,
        fallback_texts=((observation.title,) if observation.title else ()),
        about_record_ids=about_record_ids,
    )
    selected: list[CandidateRetrievalScore] = []
    for candidate in candidates:
        if candidate.claim.claim_id in excluded_claim_ids:
            continue
        score = _score_candidate(
            candidate=candidate,
            new_claim=new_claim,
            observation=observation,
            new_about_record_ids=about_record_ids,
            new_features=new_features,
        )
        if thread_scoped and not score.same_thread:
            continue
        if not thread_scoped and score.same_thread:
            continue
        if not _candidate_has_strong_signal(score):
            continue
        selected.append(score)
    return tuple(
        sorted(
            selected,
            key=_candidate_retrieval_sort_key,
            reverse=True,
        )
    )


def _same_thread(claim: ClaimRecord, *, observation: Observation) -> bool:
    claim_thread_kind = _claim_context_kind(claim)
    claim_thread_id = _claim_context_id(claim)
    observation_thread_kind = _observation_context_kind(observation)
    observation_thread_id = _observation_context_id(observation)
    return (
        bool(observation_thread_kind)
        and bool(observation_thread_id)
        and claim_thread_kind == observation_thread_kind
        and claim_thread_id == observation_thread_id
    )


def _score_candidate(
    *,
    new_claim: ClaimRecord,
    observation: Observation,
    new_about_record_ids: Sequence[str],
    new_features,
    candidate: CandidateClaimState,
) -> CandidateRetrievalScore:
    candidate_features = build_claim_retrieval_features(
        claim_text=candidate.claim.claim_text,
        support_texts=candidate.support_texts,
        about_record_ids=candidate.about_record_ids,
    )
    same_thread = _same_thread(candidate.claim, observation=observation)
    about_overlap = _about_overlap_score(new_about_record_ids, candidate)
    lexical_overlap = len(set(new_features.lexical_tokens) & set(candidate_features.lexical_tokens))
    code_anchor_overlap = len(set(new_features.code_anchors) & set(candidate_features.code_anchors))
    numeric_anchor_overlap = len(set(new_features.numeric_anchors) & set(candidate_features.numeric_anchors))
    kind_compatibility = _claim_kind_compatibility_score(new_claim.claim_kind, candidate.claim.claim_kind)
    retrieval_sources = tuple(
        source
        for source, is_active in (
            ("thread", same_thread),
            ("about", about_overlap > 0),
            ("lexical", lexical_overlap >= 3 or code_anchor_overlap > 0),
        )
        if is_active
    )
    total_score = (
        (120.0 if same_thread else 0.0)
        + (40.0 * about_overlap)
        + (18.0 * code_anchor_overlap)
        + (6.0 * numeric_anchor_overlap)
        + (4.0 * lexical_overlap)
        + (8.0 * kind_compatibility)
    )
    return CandidateRetrievalScore(
        candidate=candidate,
        same_thread=same_thread,
        about_overlap=about_overlap,
        lexical_overlap=lexical_overlap,
        code_anchor_overlap=code_anchor_overlap,
        numeric_anchor_overlap=numeric_anchor_overlap,
        kind_compatibility=kind_compatibility,
        retrieval_sources=retrieval_sources,
        total_score=total_score,
    )


def _candidate_has_strong_signal(score: CandidateRetrievalScore) -> bool:
    if score.about_overlap > 0:
        return True
    if score.code_anchor_overlap >= 2:
        return True
    if score.code_anchor_overlap >= 1 and score.lexical_overlap >= 2:
        return True
    if score.lexical_overlap >= 5:
        return True
    if score.same_thread and (score.code_anchor_overlap >= 1 or score.lexical_overlap >= 4):
        return True
    return False


def _candidate_is_viable_from_search(
    score: CandidateRetrievalScore,
    *,
    lexical_hit: bool,
    semantic_hit: bool,
) -> bool:
    if semantic_hit and (
        score.kind_compatibility > 0
        or score.about_overlap > 0
        or score.same_thread
        or score.lexical_overlap >= 2
    ):
        return True
    if lexical_hit and (
        _candidate_has_strong_signal(score)
        or score.about_overlap > 0
        or score.same_thread
        or (score.kind_compatibility > 0 and score.lexical_overlap >= 3)
    ):
        return True
    return False


def _score_candidate_from_search(
    score: CandidateRetrievalScore,
    *,
    lexical_rank: int | None,
    lexical_score: float | None,
    semantic_rank: int | None,
    semantic_score: float | None,
) -> CandidateRetrievalScore:
    retrieval_sources = list(score.retrieval_sources)
    total_score = score.total_score
    if lexical_rank is not None:
        retrieval_sources.append("lexical_search")
        total_score += 24.0 / lexical_rank
    if semantic_rank is not None:
        retrieval_sources.append("semantic_claim")
        total_score += 36.0 / semantic_rank
    _ = lexical_score, semantic_score
    return CandidateRetrievalScore(
        candidate=score.candidate,
        same_thread=score.same_thread,
        about_overlap=score.about_overlap,
        lexical_overlap=score.lexical_overlap,
        code_anchor_overlap=score.code_anchor_overlap,
        numeric_anchor_overlap=score.numeric_anchor_overlap,
        kind_compatibility=score.kind_compatibility,
        retrieval_sources=tuple(dict.fromkeys(retrieval_sources)),
        total_score=total_score,
    )


def _merge_candidate_scores(
    *score_groups: Sequence[CandidateRetrievalScore],
) -> tuple[CandidateRetrievalScore, ...]:
    merged: dict[str, CandidateRetrievalScore] = {}
    for scores in score_groups:
        for score in scores:
            claim_id = score.candidate.claim.claim_id
            current = merged.get(claim_id)
            if current is None:
                merged[claim_id] = score
                continue
            merged[claim_id] = CandidateRetrievalScore(
                candidate=current.candidate,
                same_thread=current.same_thread or score.same_thread,
                about_overlap=max(current.about_overlap, score.about_overlap),
                lexical_overlap=max(current.lexical_overlap, score.lexical_overlap),
                code_anchor_overlap=max(current.code_anchor_overlap, score.code_anchor_overlap),
                numeric_anchor_overlap=max(current.numeric_anchor_overlap, score.numeric_anchor_overlap),
                kind_compatibility=max(current.kind_compatibility, score.kind_compatibility),
                retrieval_sources=tuple(
                    dict.fromkeys((*current.retrieval_sources, *score.retrieval_sources))
                ),
                total_score=max(current.total_score, score.total_score),
            )
    return tuple(
        sorted(
            merged.values(),
            key=_candidate_retrieval_sort_key,
            reverse=True,
        )
    )


def _candidate_retrieval_sort_key(score: CandidateRetrievalScore) -> tuple[float, datetime, str]:
    return (
        score.total_score,
        _parse_datetime(score.candidate.claim.latest_observed_at) or datetime.min,
        score.candidate.claim.claim_id,
    )


def _about_overlap_score(
    new_about_record_ids: Sequence[str],
    candidate: CandidateClaimState,
) -> int:
    if not new_about_record_ids:
        return 0
    return len(set(new_about_record_ids) & set(candidate.about_record_ids))


def _claim_kind_compatibility_score(left: str, right: str) -> int:
    if left == right:
        return 2
    comparable_families = (
        frozenset({"change_claim", "state_claim", "scope_statement", "concern", "bug_report"}),
        frozenset({"review_feedback", "requirement", "concern"}),
        frozenset({"test_expectation", "state_claim", "change_claim"}),
    )
    if any(left in family and right in family for family in comparable_families):
        return 1
    return 0


def _merge_support_texts(
    existing_support_texts: Sequence[str],
    new_support_texts: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            text
            for text in (*existing_support_texts, *new_support_texts)
            if isinstance(text, str) and text.strip()
        )
    )


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


def _claim_with_backfill_ingested_at(claim: ClaimRecord) -> ClaimRecord:
    if claim.ingested_at is not None:
        return claim
    return ClaimRecord(
        claim_id=claim.claim_id,
        repo_id=claim.repo_id,
        canonical_key=claim.canonical_key,
        claim_text=claim.claim_text,
        claim_kind=claim.claim_kind,
        claim_subkind=claim.claim_subkind,
        first_observed_at=claim.first_observed_at,
        latest_observed_at=claim.latest_observed_at,
        is_current=claim.is_current,
        ingested_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        valid_from=claim.valid_from,
        valid_to=claim.valid_to,
        invalidated_at=claim.invalidated_at,
        invalidated_by_claim_id=claim.invalidated_by_claim_id,
        observation_id=claim.observation_id,
        source_kind=claim.source_kind,
        source_id=claim.source_id,
        context_kind=claim.context_kind,
        context_id=claim.context_id,
        thread_kind=claim.thread_kind,
        thread_id=claim.thread_id,
        metadata=claim.metadata,
    )


def _derive_thread_kind(source_kind: str | None) -> str | None:
    if source_kind in {"pull_request", "issue", "commit"}:
        return source_kind
    return None


def _derive_thread_id(*, source_kind: str | None, source_id: str | None) -> str | None:
    if not source_id:
        return None
    if source_kind == "pull_request" and source_id.startswith("github:pr:"):
        return source_id.rsplit(":", 1)[-1] or None
    if source_kind == "issue" and source_id.startswith("github:issue:"):
        return source_id.rsplit(":", 1)[-1] or None
    if source_kind == "commit" and source_id.startswith("commit:"):
        return source_id.rsplit(":", 1)[-1] or None
    return source_id


def _claim_context_kind(claim: ClaimRecord) -> str | None:
    return claim.context_kind or claim.thread_kind or _derive_thread_kind(claim.source_kind)


def _claim_context_id(claim: ClaimRecord) -> str | None:
    return claim.context_id or claim.thread_id or _derive_thread_id(
        source_kind=claim.source_kind,
        source_id=claim.source_id,
    )


def _observation_context_kind(observation: Observation) -> str | None:
    return observation.context_kind or observation.thread_kind


def _observation_context_id(observation: Observation) -> str | None:
    return observation.context_id or observation.thread_id


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
