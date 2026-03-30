"""Mutation planning for grounded evidence records."""

from __future__ import annotations

from dataclasses import dataclass
import json
from os.path import basename

from context_graph.code_identity import SnapshotCodeResolver
from context_graph.models.graph import GraphSnapshot
from context_graph.backend import (
    EdgeRecord,
    MutationPlan,
    NodeRecord,
    RecordKind,
    SearchDocumentRecord,
    merge_mutation_plans,
)

from .grounding import resolve_about_record_ids
from .hint_enrichment import collect_grounding_hints, collect_observation_grounding_hints
from .models import (
    EvidenceCandidate,
    EvidenceExtractionResponse,
    EvidenceRecord,
    Observation,
    evidence_record_id,
)
from .validation import ClaimValidationResult, ValidatedClaimBundle


@dataclass(frozen=True, slots=True)
class PreparedEvidenceRecord:
    """One evidence record plus grounded targets available to claim planning."""

    evidence: EvidenceRecord
    about_record_ids: tuple[str, ...] = ()
    support_role: str = "provides_context"
    support_rationale: str = ""
    assertion_status: str | None = None
    claim_frame: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceFactState:
    """Top-level fact state mirrored onto evidence records and search docs."""

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


class EvidenceMutationPlanner:
    """Translate evidence extraction outputs into backend mutation records."""

    def plan_evidence_records(
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

    def plan_validated_claim_bundle(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        return self.plan_prepared_records(
            observation=observation,
            prepared_records=self.prepare_validated_claim_bundle(
                observation=observation,
                bundle=bundle,
                snapshot=snapshot,
            ),
            fact_state=EvidenceFactState(
                observation_id=observation.observation_id,
                observed_at=observation.observed_at,
            ),
        )

    def prepare_evidence_record(
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
            support_source=candidate.support_source,
            observation_id=observation.observation_id,
            observed_at=observation.observed_at,
            metadata={
                **candidate.metadata.model_dump(),
                "support_source": candidate.support_source,
            },
        )
        about_record_ids = self._resolve_prepared_about_record_ids(
            observation=observation,
            claim=None,
            evidence=candidate,
            snapshot=snapshot,
        )
        return PreparedEvidenceRecord(
            evidence=evidence,
            about_record_ids=about_record_ids,
            support_role=getattr(candidate, "support_role", "provides_context"),
            support_rationale=self._support_rationale(candidate),
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
                ),
                repo_id=observation.repo_id,
                evidence_kind=candidate.evidence_kind,
                quote_text=candidate.quote_text,
                summary_text=candidate.summary_text,
                span_start=candidate.span_start,
                span_end=candidate.span_end,
                support_kind=candidate.support_kind,
                support_source=candidate.support_source,
                observation_id=observation.observation_id,
                observed_at=observation.observed_at,
                metadata={
                    **candidate.metadata.model_dump(),
                    "support_source": candidate.support_source,
                },
            )
            prepared.append(
                PreparedEvidenceRecord(
                    evidence=evidence,
                    about_record_ids=self._resolve_prepared_about_record_ids(
                        observation=observation,
                        claim=bundle,
                        evidence=candidate,
                        snapshot=snapshot,
                    ),
                    support_role=candidate.support_role,
                    support_rationale=self._support_rationale(candidate),
                    assertion_status=bundle.assertion_status,
                    claim_frame=bundle.claim_frame,
                )
            )
        return tuple(prepared)

    def plan_prepared_records(
        self,
        *,
        observation: Observation,
        prepared_records: tuple[PreparedEvidenceRecord, ...] | list[PreparedEvidenceRecord],
        fact_state: EvidenceFactState,
        claim_record_id: str | None = None,
        supersedes_record_ids: tuple[str, ...] | list[str] = (),
        superseded_by_record_ids: tuple[str, ...] | list[str] = (),
    ) -> MutationPlan:
        return merge_mutation_plans(
            *(
                self._plan_prepared_record(
                    observation=observation,
                    prepared=prepared,
                    fact_state=fact_state,
                    claim_record_id=claim_record_id,
                    supersedes_record_ids=tuple(supersedes_record_ids),
                    superseded_by_record_ids=tuple(superseded_by_record_ids),
                )
                for prepared in prepared_records
            )
        )

    def _plan_prepared_record(
        self,
        *,
        observation: Observation,
        prepared: PreparedEvidenceRecord,
        fact_state: EvidenceFactState,
        claim_record_id: str | None,
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
            support_source=prepared.evidence.support_source,
            observation_id=fact_state.observation_id or prepared.evidence.observation_id,
            observed_at=fact_state.observed_at or prepared.evidence.observed_at,
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
            properties={"observation_kind": observation.observation_kind},
        )
        support_edge = (
            EdgeRecord(
                record_id=f"supports_claim:{evidence.evidence_id}:{claim_record_id}",
                record_type="supports_claim",
                repo_id=evidence.repo_id,
                group_id=evidence.repo_id,
                relationship_type="SUPPORTS_CLAIM",
                source_record_id=evidence.evidence_id,
                target_record_id=claim_record_id,
                properties=_compact_properties(
                    {
                        "support_kind": evidence.support_kind,
                        "support_source": evidence.support_source,
                        "support_role": prepared.support_role,
                        "support_rationale": prepared.support_rationale,
                        "assertion_status": prepared.assertion_status,
                        "claim_frame": prepared.claim_frame,
                    }
                ),
            )
            if claim_record_id is not None
            else None
        )
        return MutationPlan(
            upsert_nodes=(self._node_record(evidence),),
            upsert_edges=(
                derived_from_edge,
                *((support_edge,) if support_edge is not None else ()),
            ),
            upsert_search_documents=(self._search_document(evidence, observation_kind=observation.observation_kind),),
        )

    def _node_record(self, evidence: EvidenceRecord) -> NodeRecord:
        properties = _compact_properties(
            {
                "evidence_id": evidence.evidence_id,
                "repo_id": evidence.repo_id,
                "evidence_kind": evidence.evidence_kind,
                "quote_text": evidence.quote_text,
                "summary_text": evidence.summary_text,
                "span_start": evidence.span_start,
                "span_end": evidence.span_end,
                "support_kind": evidence.support_kind,
                "support_source": evidence.support_source,
                "observation_id": evidence.observation_id,
                "observed_at": evidence.observed_at,
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
                "support_source": evidence.support_source,
                "source_observation_kind": observation_kind,
                "observation_id": evidence.observation_id,
                "observed_at": evidence.observed_at,
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

    def _resolve_prepared_about_record_ids(
        self,
        *,
        observation: Observation,
        claim: ValidatedClaimBundle | None,
        evidence: EvidenceCandidate,
        snapshot: GraphSnapshot | None,
    ) -> tuple[str, ...]:
        about_record_ids = resolve_about_record_ids(
            snapshot,
            collect_grounding_hints(
                observation=observation,
                claim=claim,
                evidence=evidence,
            ),
        )
        if about_record_ids or snapshot is None:
            return about_record_ids
        return self._fallback_about_record_ids(
            observation=observation,
            claim=claim,
            evidence=evidence,
            snapshot=snapshot,
        )

    def _fallback_about_record_ids(
        self,
        *,
        observation: Observation,
        claim: ValidatedClaimBundle | None,
        evidence: EvidenceCandidate,
        snapshot: GraphSnapshot,
    ) -> tuple[str, ...]:
        resolver = SnapshotCodeResolver(snapshot)
        observation_candidates = resolver.resolve_about_hints(
            tuple(
                (hint.value, hint.hint_kind.value)
                for hint in collect_observation_grounding_hints(observation=observation)
            )
        )
        if not observation_candidates:
            return ()
        anchor_texts = tuple(
            text
            for text in (
                getattr(claim, "claim_text", None),
                evidence.quote_text,
            )
            if text
        )
        if not anchor_texts:
            return ()
        matched_record_ids = tuple(
            f"{node.node_id}@{snapshot.revision_id}"
            for node in observation_candidates
            if any(_text_mentions_node(anchor_text, node) for anchor_text in anchor_texts)
        )
        return tuple(dict.fromkeys(matched_record_ids))

    def _support_rationale(self, candidate: EvidenceCandidate) -> str:
        rationale = " ".join(str(getattr(candidate, "support_rationale", "") or "").split()).strip()
        if rationale:
            return rationale
        summary_text = " ".join(str(candidate.summary_text or "").split()).strip()
        if summary_text:
            return summary_text
        return "This evidence directly supports the claim."


def _evidence_search_text(evidence: EvidenceRecord) -> str:
    return " ".join(
        part
        for part in (
            evidence.evidence_kind.strip(),
            evidence.quote_text.strip(),
            (evidence.summary_text or "").strip(),
        )
        if part
    )


def _compact_properties(properties: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in properties.items()
        if value is not None
    }


def _text_mentions_node(text: str, node) -> bool:
    normalized_text = text.lower()
    candidate_tokens = {node.name.lower()}
    if node.file_path:
        candidate_tokens.add(basename(node.file_path).lower())
    return any(token and token in normalized_text for token in candidate_tokens)
