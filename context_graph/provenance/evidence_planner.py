"""Mutation planning for grounded evidence records."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Iterable

from context_graph.models import CanonicalNode, GraphSnapshot
from context_graph.storage.backend import (
    EdgeRecord,
    MutationPlan,
    NodeRecord,
    RecordKind,
    SearchDocumentRecord,
    merge_mutation_plans,
)

from .models import EvidenceCandidate, EvidenceExtractionResponse, EvidenceRecord, Observation, evidence_record_id


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
                self.plan_evidence_record(
                    observation=observation,
                    candidate=candidate,
                    snapshot=snapshot,
                )
                for candidate in extraction.evidence
            )
        )

    def plan_evidence_record(
        self,
        *,
        observation: Observation,
        candidate: EvidenceCandidate,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
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
            metadata=dict(candidate.metadata),
        )
        node = NodeRecord(
            record_id=evidence.evidence_id,
            record_type="evidence_record",
            repo_id=evidence.repo_id,
            group_id=evidence.repo_id,
            labels=("BackendRecord", "EvidenceRecord"),
            properties={
                "evidence_id": evidence.evidence_id,
                "evidence_kind": evidence.evidence_kind,
                "quote_text": evidence.quote_text,
                "summary_text": evidence.summary_text,
                "span_start": evidence.span_start,
                "span_end": evidence.span_end,
                "support_kind": evidence.support_kind,
                "metadata_json": json.dumps(evidence.metadata, sort_keys=True, separators=(",", ":")),
            },
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
            for target_record_id in self._resolve_about_record_ids(snapshot=snapshot, hints=candidate.about_hints)
        )
        search_document = SearchDocumentRecord(
            record_kind=RecordKind.NODE,
            record_id=evidence.evidence_id,
            record_type="evidence_record",
            repo_id=evidence.repo_id,
            group_id=evidence.repo_id,
            search_text=_evidence_search_text(evidence),
            properties={
                "support_kind": evidence.support_kind,
                "source_observation_kind": observation.observation_kind.value,
            },
        )
        return MutationPlan(
            upsert_nodes=(node,),
            upsert_edges=(derived_from_edge, *about_edges),
            upsert_search_documents=(search_document,),
        )

    def _resolve_about_record_ids(
        self,
        *,
        snapshot: GraphSnapshot | None,
        hints: Iterable[str],
    ) -> tuple[str, ...]:
        if snapshot is None:
            return ()

        by_node_id = {node.node_id: node for node in snapshot.nodes if node.kind != "Project"}
        by_qualified_name = {
            node.qualified_name: node
            for node in snapshot.nodes
            if node.kind != "Project" and node.qualified_name
        }
        by_name: dict[str, list[CanonicalNode]] = defaultdict(list)
        for node in snapshot.nodes:
            if node.kind == "Project":
                continue
            by_name[node.name].append(node)

        resolved: list[str] = []
        seen: set[str] = set()
        for raw_hint in hints:
            hint = " ".join(str(raw_hint).split()).strip()
            if not hint:
                continue
            node = by_qualified_name.get(hint)
            if node is None:
                node = by_node_id.get(hint)
            if node is None:
                exact_name_matches = by_name.get(hint, ())
                if len(exact_name_matches) == 1:
                    node = exact_name_matches[0]
            if node is None:
                continue
            version_record_id = f"{node.node_id}@{snapshot.revision_id}"
            if version_record_id in seen:
                continue
            seen.add(version_record_id)
            resolved.append(version_record_id)
        return tuple(resolved)


def _evidence_search_text(evidence: EvidenceRecord) -> str:
    return " ".join(
        part
        for part in (
            evidence.evidence_kind.strip(),
            evidence.quote_text.strip(),
            evidence.summary_text.strip(),
        )
        if part
    )
