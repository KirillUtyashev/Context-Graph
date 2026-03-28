"""Mutation planning for grounded evidence records."""

from __future__ import annotations

from dataclasses import dataclass
import json

from context_graph.models import GraphSnapshot
from context_graph.storage.backend import (
    EdgeRecord,
    MutationPlan,
    NodeRecord,
    RecordKind,
    SearchDocumentRecord,
    merge_mutation_plans,
)

from .canonicalization import canonical_claim_key
from .grounding import resolve_about_record_ids
from .hint_enrichment import collect_grounding_hints
from .models import (
    EvidenceCandidate,
    EvidenceExtractionResponse,
    EvidenceRecord,
    Observation,
    PersistedEvidenceRecord,
    evidence_record_id,
)
from .validation import ClaimValidationResult, ValidatedClaimBundle


@dataclass(frozen=True, slots=True)
class PreparedEvidenceRecord:
    """One evidence record plus the grounded ABOUT targets it should emit."""

    evidence: EvidenceRecord
    about_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceFactState:
    """Top-level fact state mirrored onto evidence records and search docs."""

    fact_key: str | None = None
    claim_text: str | None = None
    claim_kind: str | None = None
    claim_confidence: str | None = None
    claim_derivation_kind: str | None = None
    observation_id: str | None = None
    observed_at: str | None = None
    is_current: bool = True
    invalidated_at: str | None = None
    invalidated_by_fact_key: str | None = None
    invalidated_by_observation_id: str | None = None


class EvidenceMutationPlanner:
    """Translate evidence extraction outputs into backend mutation records."""

    def plan_evidence_records(  # pragma: no cover
        self,
        *,
        observation: Observation,
        extraction: EvidenceExtractionResponse,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        return merge_mutation_plans(
            *(
                self.plan_prepared_records(
                    observation=observation,
                    prepared_records=(
                        self.prepare_evidence_record(
                            observation=observation,
                            candidate=candidate,
                            snapshot=snapshot,
                        ),
                    ),
                    fact_state=EvidenceFactState(
                        observation_id=observation.observation_id,
                        observed_at=observation.observed_at,
                    ),
                )
                for candidate in extraction.evidence
            )
        )

    def plan_validated_claims(
        self,
        *,
        observation: Observation,
        validation: ClaimValidationResult,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        return merge_mutation_plans(
            *(
                self.plan_validated_claim_bundle(
                    observation=observation,
                    bundle=bundle,
                    snapshot=snapshot,
                )
                for bundle in validation.validated_claims
            )
        )

    def plan_validated_claim_bundle(  # pragma: no cover
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        fact_key = canonical_claim_key(
            claim_kind=bundle.claim_kind,
            claim_text=bundle.claim_text,
        )
        return self.plan_prepared_records(
            observation=observation,
            prepared_records=self.prepare_validated_claim_bundle(
                observation=observation,
                bundle=bundle,
                snapshot=snapshot,
            ),
            fact_state=EvidenceFactState(
                fact_key=fact_key,
                claim_text=bundle.claim_text,
                claim_kind=bundle.claim_kind,
                claim_confidence=bundle.confidence,
                claim_derivation_kind=bundle.derivation_kind,
                observation_id=observation.observation_id,
                observed_at=observation.observed_at,
            ),
        )

    def prepare_evidence_record(  # pragma: no cover
        self,
        *,
        observation: Observation,
        candidate: EvidenceCandidate,
        snapshot: GraphSnapshot | None = None,
    ) -> PreparedEvidenceRecord:
        evidence = EvidenceRecord(
            evidence_id=evidence_record_id(
                repo_id=observation.repo_id,
                observation_id=observation.observation_id,
                evidence_kind=candidate.evidence_kind,
                quote_text=candidate.quote_text,
                span_start=candidate.span_start,
                span_end=candidate.span_end,
            ),
            repo_id=observation.repo_id,
            evidence_kind=candidate.evidence_kind,
            quote_text=candidate.quote_text,
            summary_text=candidate.summary_text,
            span_start=candidate.span_start,
            span_end=candidate.span_end,
            support_kind=candidate.support_kind,
            observation_id=observation.observation_id,
            observed_at=observation.observed_at,
            metadata=candidate.metadata.model_dump(),
        )
        about_record_ids = resolve_about_record_ids(
            snapshot,
            collect_grounding_hints(
                observation=observation,
                evidence=candidate,
            ),
        )
        return PreparedEvidenceRecord(
            evidence=evidence,
            about_record_ids=about_record_ids,
        )

    def prepare_validated_claim_bundle(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        snapshot: GraphSnapshot | None = None,
    ) -> tuple[PreparedEvidenceRecord, ...]:
        prepared: list[PreparedEvidenceRecord] = []
        for candidate in bundle.supporting_evidence:
            evidence = EvidenceRecord(
                evidence_id=evidence_record_id(
                    repo_id=observation.repo_id,
                    observation_id=observation.observation_id,
                    evidence_kind=candidate.evidence_kind,
                    quote_text=candidate.quote_text,
                    span_start=candidate.span_start,
                    span_end=candidate.span_end,
                    claim_text=bundle.claim_text,
                ),
                repo_id=observation.repo_id,
                evidence_kind=candidate.evidence_kind,
                quote_text=candidate.quote_text,
                summary_text=candidate.summary_text,
                span_start=candidate.span_start,
                span_end=candidate.span_end,
                support_kind=candidate.support_kind,
                fact_key=canonical_claim_key(
                    claim_kind=bundle.claim_kind,
                    claim_text=bundle.claim_text,
                ),
                claim_text=bundle.claim_text,
                claim_kind=bundle.claim_kind,
                claim_confidence=bundle.confidence,
                claim_derivation_kind=bundle.derivation_kind,
                observation_id=observation.observation_id,
                observed_at=observation.observed_at,
                metadata={
                    **candidate.metadata.model_dump(),
                    "claim_text": bundle.claim_text,
                    "claim_kind": bundle.claim_kind,
                    "claim_confidence": bundle.confidence,
                    "claim_derivation_kind": bundle.derivation_kind,
                },
            )
            prepared.append(
                PreparedEvidenceRecord(
                    evidence=evidence,
                    about_record_ids=resolve_about_record_ids(
                        snapshot,
                        collect_grounding_hints(
                            observation=observation,
                            claim=bundle,
                            evidence=candidate,
                        ),
                    ),
                )
            )
        return tuple(prepared)

    def plan_prepared_records(
        self,
        *,
        observation: Observation,
        prepared_records: tuple[PreparedEvidenceRecord, ...] | list[PreparedEvidenceRecord],
        fact_state: EvidenceFactState,
        supersedes_record_ids: tuple[str, ...] | list[str] = (),
        superseded_by_record_ids: tuple[str, ...] | list[str] = (),
    ) -> MutationPlan:
        return merge_mutation_plans(
            *(
                self._plan_prepared_record(
                    observation=observation,
                    prepared=prepared,
                    fact_state=fact_state,
                    supersedes_record_ids=tuple(supersedes_record_ids),
                    superseded_by_record_ids=tuple(superseded_by_record_ids),
                )
                for prepared in prepared_records
            )
        )

    def plan_existing_record_state_update(
        self,
        *,
        record: PersistedEvidenceRecord,
        invalidated_at: str | None,
        invalidated_by_fact_key: str,
        invalidated_by_observation_id: str,
    ) -> MutationPlan:
        evidence = EvidenceRecord(
            evidence_id=record.evidence.evidence_id,
            repo_id=record.evidence.repo_id,
            evidence_kind=record.evidence.evidence_kind,
            quote_text=record.evidence.quote_text,
            summary_text=record.evidence.summary_text,
            span_start=record.evidence.span_start,
            span_end=record.evidence.span_end,
            support_kind=record.evidence.support_kind,
            fact_key=record.evidence.fact_key,
            claim_text=record.evidence.claim_text,
            claim_kind=record.evidence.claim_kind,
            claim_confidence=record.evidence.claim_confidence,
            claim_derivation_kind=record.evidence.claim_derivation_kind,
            observation_id=record.evidence.observation_id,
            observed_at=record.evidence.observed_at,
            is_current=False,
            invalidated_at=invalidated_at,
            invalidated_by_fact_key=invalidated_by_fact_key,
            invalidated_by_observation_id=invalidated_by_observation_id,
            metadata=record.evidence.metadata,
        )
        return MutationPlan(
            upsert_nodes=(self._node_record(evidence),),
            upsert_search_documents=(self._search_document(evidence, observation_kind=None),),
        )

    def _plan_prepared_record(
        self,
        *,
        observation: Observation,
        prepared: PreparedEvidenceRecord,
        fact_state: EvidenceFactState,
        supersedes_record_ids: tuple[str, ...] = (),
        superseded_by_record_ids: tuple[str, ...] = (),
    ) -> MutationPlan:
        evidence = EvidenceRecord(
            evidence_id=prepared.evidence.evidence_id,
            repo_id=prepared.evidence.repo_id,
            evidence_kind=prepared.evidence.evidence_kind,
            quote_text=prepared.evidence.quote_text,
            summary_text=prepared.evidence.summary_text,
            span_start=prepared.evidence.span_start,
            span_end=prepared.evidence.span_end,
            support_kind=prepared.evidence.support_kind,
            fact_key=fact_state.fact_key or prepared.evidence.fact_key,
            claim_text=fact_state.claim_text or prepared.evidence.claim_text,
            claim_kind=fact_state.claim_kind or prepared.evidence.claim_kind,
            claim_confidence=fact_state.claim_confidence or prepared.evidence.claim_confidence,
            claim_derivation_kind=fact_state.claim_derivation_kind or prepared.evidence.claim_derivation_kind,
            observation_id=fact_state.observation_id or prepared.evidence.observation_id,
            observed_at=fact_state.observed_at or prepared.evidence.observed_at,
            is_current=fact_state.is_current,
            invalidated_at=fact_state.invalidated_at,
            invalidated_by_fact_key=fact_state.invalidated_by_fact_key,
            invalidated_by_observation_id=fact_state.invalidated_by_observation_id,
            metadata=prepared.evidence.metadata,
        )
        derived_from_edge = EdgeRecord(
            record_id=f"derived_from:{evidence.evidence_id}:{observation.observation_id}",
            record_type="derived_from",
            repo_id=evidence.repo_id,
            group_id=evidence.repo_id,
            relationship_type="DERIVED_FROM",
            source_record_id=evidence.evidence_id,
            target_record_id=observation.observation_id,
            properties={"observation_kind": observation.observation_kind.value},
        )
        about_edges = tuple(
            EdgeRecord(
                record_id=f"about:{evidence.evidence_id}:{target_record_id}",
                record_type="about",
                repo_id=evidence.repo_id,
                group_id=evidence.repo_id,
                relationship_type="ABOUT",
                source_record_id=evidence.evidence_id,
                target_record_id=target_record_id,
            )
            for target_record_id in prepared.about_record_ids
        )
        supersedes_edges = tuple(
            EdgeRecord(
                record_id=f"supersedes:{evidence.evidence_id}:{target_record_id}",
                record_type="supersedes",
                repo_id=evidence.repo_id,
                group_id=evidence.repo_id,
                relationship_type="SUPERSEDES",
                source_record_id=evidence.evidence_id,
                target_record_id=target_record_id,
                properties={
                    "fact_key": evidence.fact_key,
                    "claim_kind": evidence.claim_kind,
                },
            )
            for target_record_id in supersedes_record_ids
        )
        superseded_by_edges = tuple(
            EdgeRecord(
                record_id=f"supersedes:{source_record_id}:{evidence.evidence_id}",
                record_type="supersedes",
                repo_id=evidence.repo_id,
                group_id=evidence.repo_id,
                relationship_type="SUPERSEDES",
                source_record_id=source_record_id,
                target_record_id=evidence.evidence_id,
                properties={
                    "fact_key": evidence.fact_key,
                    "claim_kind": evidence.claim_kind,
                },
            )
            for source_record_id in superseded_by_record_ids
        )
        return MutationPlan(
            upsert_nodes=(self._node_record(evidence),),
            upsert_edges=(
                derived_from_edge,
                *about_edges,
                *supersedes_edges,
                *superseded_by_edges,
            ),
            upsert_search_documents=(self._search_document(evidence, observation_kind=observation.observation_kind.value),),
        )

    def _node_record(self, evidence: EvidenceRecord) -> NodeRecord:
        properties = _compact_properties(
            {
                "evidence_id": evidence.evidence_id,
                "evidence_kind": evidence.evidence_kind,
                "quote_text": evidence.quote_text,
                "summary_text": evidence.summary_text,
                "span_start": evidence.span_start,
                "span_end": evidence.span_end,
                "support_kind": evidence.support_kind,
                "fact_key": evidence.fact_key,
                "claim_text": evidence.claim_text,
                "claim_kind": evidence.claim_kind,
                "claim_confidence": evidence.claim_confidence,
                "claim_derivation_kind": evidence.claim_derivation_kind,
                "observation_id": evidence.observation_id,
                "observed_at": evidence.observed_at,
                "is_current": evidence.is_current,
                "invalidated_at": evidence.invalidated_at,
                "invalidated_by_fact_key": evidence.invalidated_by_fact_key,
                "invalidated_by_observation_id": evidence.invalidated_by_observation_id,
                "metadata_json": json.dumps(evidence.metadata, sort_keys=True, separators=(",", ":")),
            }
        )
        return NodeRecord(
            record_id=evidence.evidence_id,
            record_type="evidence_record",
            repo_id=evidence.repo_id,
            group_id=evidence.repo_id,
            labels=("BackendRecord", "EvidenceRecord"),
            properties=properties,
        )

    def _search_document(self, evidence: EvidenceRecord, *, observation_kind: str | None) -> SearchDocumentRecord:
        properties = _compact_properties(
            {
                "support_kind": evidence.support_kind,
                "source_observation_kind": observation_kind,
                "fact_key": evidence.fact_key,
                "claim_text": evidence.claim_text,
                "claim_kind": evidence.claim_kind,
                "claim_confidence": evidence.claim_confidence,
                "claim_derivation_kind": evidence.claim_derivation_kind,
                "observation_id": evidence.observation_id,
                "observed_at": evidence.observed_at,
                "is_current": evidence.is_current,
                "invalidated_at": evidence.invalidated_at,
                "invalidated_by_fact_key": evidence.invalidated_by_fact_key,
                "invalidated_by_observation_id": evidence.invalidated_by_observation_id,
            }
        )
        return SearchDocumentRecord(
            record_kind=RecordKind.NODE,
            record_id=evidence.evidence_id,
            record_type="evidence_record",
            repo_id=evidence.repo_id,
            group_id=evidence.repo_id,
            search_text=_evidence_search_text(evidence),
            properties=properties,
        )


def _evidence_search_text(evidence: EvidenceRecord) -> str:
    return " ".join(
        part
        for part in (
            evidence.evidence_kind.strip(),
            evidence.quote_text.strip(),
            evidence.summary_text.strip(),
            (evidence.claim_text or "").strip(),
        )
        if part
    )


def _compact_properties(properties: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in properties.items()
        if value is not None
    }
