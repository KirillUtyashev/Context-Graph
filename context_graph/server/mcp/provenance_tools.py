"""Helpers for provenance-oriented MCP tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from context_graph.profiles import (
    SEMANTIC_CLAIM_PROFILE_KIND,
    SEMANTIC_OBSERVATION_PROFILE_KIND,
)
from context_graph.provenance import (
    ClaimCandidate,
    ClaimResolutionService,
    EvidenceCandidate,
    ResearchArtifactSynthesisService,
)
from context_graph.server.mcp import code_graph as code_graph_queries
from context_graph.server.mcp.response_types import (
    CodeContextResponse,
    CodeContextSnippet,
    CodeGraphNodeRef,
    NodeNeighborhoodResponse,
    RecordAgentObservationResponse,
    ResolveClaimResponse,
    SearchContextResponse,
    SynthesizeArtifactResponse,
    TimelineResponse,
)
from context_graph.server.mcp.queue import ObservationWriteResult
from context_graph.storage.backend import (
    BackendDriver,
    GraphRecordRef,
    RecordKind,
    SearchMode,
    SearchQuery,
)

IMPLEMENTED_STATUS = "implemented"
SCAFFOLD_STATUS = "scaffold"

SEARCH_CONTEXT_SOURCES = ("claims", "observations", "artifacts", "code")
SOURCE_RECORD_TYPES: dict[str, tuple[str, ...]] = {
    "claims": ("claim",),
    "observations": ("observation", "evidence_record"),
    "artifacts": (
        "github_pull_request",
        "github_issue",
        "github_pull_request_review",
        "github_pull_request_review_comment",
        "github_issue_comment",
        "github_git_hub_timeline_event",
    ),
    "code": ("code_node", "project", "commit"),
}

NEIGHBORHOOD_CAPABILITY = "context_graph.backend.traversal.neighborhood"
NEIGHBORHOOD_TODO = "TODO(context_graph.backend.traversal.neighborhood)"
TIMELINE_CAPABILITY = "context_graph.backend.timeline"
TIMELINE_TODO = "TODO(context_graph.backend.timeline)"
OBSERVATION_WRITE_CAPABILITY = "context_graph.backend.ingestion.observation_write"
OBSERVATION_WRITE_TODO = "TODO(context_graph.backend.ingestion.observation_write)"
ARTIFACT_PERSISTENCE_TODO = "TODO(context_graph.backend.artifacts.persistence)"

LEXICAL_CHANNEL = "lexical"
SEMANTIC_CLAIM_CHANNEL = "semantic_claim"
SEMANTIC_OBSERVATION_CHANNEL = "semantic_observation"
HYBRID_RRF_K = 60
HYBRID_CHANNEL_WEIGHTS = {
    LEXICAL_CHANNEL: 1.0,
    SEMANTIC_CLAIM_CHANNEL: 0.9,
    SEMANTIC_OBSERVATION_CHANNEL: 0.75,
}

SemanticSearchFn = Callable[..., list[dict[str, Any]]]

ALLOWED_CLAIM_SUBKINDS = {
    "bug_or_regression",
    "fix_or_mitigation",
    "invariant_or_constraint",
    "desired_behavior_or_requirement",
    "reviewer_objection_or_requested_change",
    "approval_or_acceptance",
    "workaround_or_temporary_mitigation",
    "test_result_or_validation",
    "rollout_migration_or_dependency",
    "ownership_or_impact_scope",
}
ALLOWED_ASSERTION_STATUSES = {
    "reported",
    "proposed",
    "requested",
    "observed",
    "confirmed",
    "rejected",
}
ALLOWED_CLAIM_FRAMES = {
    "author_statement",
    "reviewer_feedback",
    "user_requirement",
    "reported_observation",
    "observed_result",
}
ALLOWED_RESOLUTION_TYPES = {"same_as", "refines", "contradicts", "supersedes", "unrelated"}


@dataclass(frozen=True, slots=True)
class HybridContextEntry:
    """One fused provenance retrieval result."""

    record: dict[str, Any]
    score: float
    lexical_score: float | None
    semantic_score: float | None
    retrieval_channels: tuple[str, ...]
    matched_profile_key_text: str | None = None
    matched_profile_key_origin: str | None = None
    semantic_profile_kind: str | None = None


@dataclass(slots=True)
class _HybridContextAggregate:
    record: dict[str, Any]
    score: float = 0.0
    lexical_score: float | None = None
    semantic_score: float | None = None
    retrieval_channels: list[str] = field(default_factory=list)
    matched_profile_key_text: str | None = None
    matched_profile_key_origin: str | None = None
    semantic_profile_kind: str | None = None


class HybridContextSearchService:
    """Fuse backend lexical search with semantic claim and observation profiles."""

    def __init__(
        self,
        *,
        driver: BackendDriver,
        semantic_search: SemanticSearchFn | None = None,
    ) -> None:
        self.driver = driver
        self.semantic_search = semantic_search

    def search(
        self,
        *,
        repo_id: str,
        query: str,
        group_id: str | None,
        sources: Sequence[str],
        limit: int,
    ) -> tuple[HybridContextEntry, ...]:
        normalized_sources = _normalize_sources(list(sources))
        overfetch_limit = max(limit * 4, limit)
        lexical_records = _search_records(
            driver=self.driver,
            repo_id=repo_id,
            query=query,
            group_id=group_id,
            record_types=_record_types_for_sources(normalized_sources),
            limit=overfetch_limit,
        )
        lexical_records = self._surface_lexical_records(
            repo_id=repo_id,
            group_id=group_id,
            records=lexical_records,
        )

        semantic_claim_hits = self._search_semantic_profiles(
            repo_id=repo_id,
            query=query,
            limit=overfetch_limit,
            profile_kinds=(SEMANTIC_CLAIM_PROFILE_KIND,),
            llm_enabled=True,
            query_texts=None,
        ) if "claims" in normalized_sources else ()
        semantic_observation_hits = self._search_semantic_profiles(
            repo_id=repo_id,
            query=query,
            limit=overfetch_limit,
            profile_kinds=(SEMANTIC_OBSERVATION_PROFILE_KIND,),
            llm_enabled=False,
            query_texts=(query,),
        ) if "observations" in normalized_sources else ()

        aggregates: dict[tuple[str, str], _HybridContextAggregate] = {}

        for rank, (raw_score, record) in enumerate(lexical_records, start=1):
            self._apply_channel_contribution(
                aggregates=aggregates,
                identity=_record_identity(record),
                record=record,
                channel=LEXICAL_CHANNEL,
                raw_score=raw_score,
                rank=rank,
            )

        for channel, hits in (
            (SEMANTIC_CLAIM_CHANNEL, semantic_claim_hits),
            (SEMANTIC_OBSERVATION_CHANNEL, semantic_observation_hits),
        ):
            for rank, hit in enumerate(hits, start=1):
                record = self._semantic_profile_record(
                    repo_id=repo_id,
                    group_id=group_id,
                    profile_hit=hit,
                )
                self._apply_channel_contribution(
                    aggregates=aggregates,
                    identity=_record_identity(record),
                    record=record,
                    channel=channel,
                    raw_score=float(hit.get("score") or 0.0),
                    rank=rank,
                    matched_profile_key_text=_normalize_text(hit.get("matched_key_text")),
                    matched_profile_key_origin=_normalize_text(hit.get("matched_key_origin")),
                    semantic_profile_kind=_normalize_text(hit.get("profile_kind")),
                )

        ordered = sorted(
            aggregates.values(),
            key=lambda entry: (
                -entry.score,
                _normalize_text(entry.record.get("record_id")),
            ),
        )
        return tuple(
            HybridContextEntry(
                record=entry.record,
                score=entry.score,
                lexical_score=entry.lexical_score,
                semantic_score=entry.semantic_score,
                retrieval_channels=tuple(entry.retrieval_channels),
                matched_profile_key_text=entry.matched_profile_key_text,
                matched_profile_key_origin=entry.matched_profile_key_origin,
                semantic_profile_kind=entry.semantic_profile_kind,
            )
            for entry in ordered[:limit]
        )

    def _apply_channel_contribution(
        self,
        *,
        aggregates: dict[tuple[str, str], _HybridContextAggregate],
        identity: tuple[str, str],
        record: dict[str, Any],
        channel: str,
        raw_score: float,
        rank: int,
        matched_profile_key_text: str | None = None,
        matched_profile_key_origin: str | None = None,
        semantic_profile_kind: str | None = None,
    ) -> None:
        entry = aggregates.get(identity)
        if entry is None:
            entry = _HybridContextAggregate(record=record)
            aggregates[identity] = entry
        elif _prefer_record(candidate=record, current=entry.record):
            entry.record = record

        entry.score += HYBRID_CHANNEL_WEIGHTS[channel] / (HYBRID_RRF_K + rank)
        if channel == LEXICAL_CHANNEL:
            entry.lexical_score = raw_score if entry.lexical_score is None else max(entry.lexical_score, raw_score)
        else:
            previous_semantic_score = entry.semantic_score
            entry.semantic_score = raw_score if entry.semantic_score is None else max(entry.semantic_score, raw_score)
            if matched_profile_key_text and (
                entry.matched_profile_key_text is None
                or previous_semantic_score is None
                or raw_score >= previous_semantic_score
            ):
                entry.matched_profile_key_text = matched_profile_key_text
                entry.matched_profile_key_origin = matched_profile_key_origin
                entry.semantic_profile_kind = semantic_profile_kind
        if channel not in entry.retrieval_channels:
            entry.retrieval_channels.append(channel)

    def _search_semantic_profiles(
        self,
        *,
        repo_id: str,
        query: str,
        limit: int,
        profile_kinds: tuple[str, ...],
        llm_enabled: bool,
        query_texts: tuple[str, ...] | None,
    ) -> tuple[dict[str, Any], ...]:
        if self.semantic_search is None:
            return ()
        try:
            hits = self.semantic_search(
                query=query,
                repo_id=repo_id,
                limit=limit,
                active_only=True,
                profile_kinds=profile_kinds,
                llm_enabled=llm_enabled,
                query_texts=query_texts,
            )
        except Exception:
            return ()
        return tuple(hit for hit in hits if isinstance(hit, Mapping))

    def _surface_lexical_records(
        self,
        *,
        repo_id: str,
        group_id: str | None,
        records: Sequence[tuple[float, dict[str, Any]]],
    ) -> tuple[tuple[float, dict[str, Any]], ...]:
        observation_refs: list[GraphRecordRef] = []
        seen_observation_ids: set[str] = set()
        for _, record in records:
            if _normalize_text(record.get("record_type")) != "evidence_record":
                continue
            observation_id = _record_observation_id(record)
            if not observation_id or observation_id in seen_observation_ids:
                continue
            seen_observation_ids.add(observation_id)
            observation_refs.append(
                GraphRecordRef(
                    record_kind=RecordKind.NODE,
                    record_id=observation_id,
                    record_type="observation",
                    repo_id=repo_id,
                    group_id=group_id,
                )
            )
        observation_records = self._fetch_records(observation_refs)
        surfaced: list[tuple[float, dict[str, Any]]] = []
        for raw_score, record in records:
            observation_id = _record_observation_id(record)
            surfaced_record = observation_records.get(observation_id, record) if observation_id else record
            surfaced.append((raw_score, surfaced_record))
        return tuple(surfaced)

    def _semantic_profile_record(
        self,
        *,
        repo_id: str,
        group_id: str | None,
        profile_hit: Mapping[str, Any],
    ) -> dict[str, Any]:
        profile_kind = _normalize_text(profile_hit.get("profile_kind"))
        target_id = _normalize_text(profile_hit.get("profile_target_id"))
        if profile_kind == SEMANTIC_CLAIM_PROFILE_KIND:
            record_type = "claim"
        elif profile_kind == SEMANTIC_OBSERVATION_PROFILE_KIND:
            record_type = "observation"
        else:
            record_type = _normalize_text(profile_hit.get("profile_target_kind")).lower()
        if target_id:
            records = self._fetch_records(
                [
                    GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id=target_id,
                        record_type=record_type or "claim",
                        repo_id=repo_id,
                        group_id=group_id,
                    )
                ]
            )
            if target_id in records:
                return records[target_id]
        return _fallback_record_from_profile_hit(profile_hit)

    def _fetch_records(self, refs: Sequence[GraphRecordRef]) -> dict[str, dict[str, Any]]:
        if not refs:
            return {}
        try:
            records = _run_async(self.driver.search.fetch(list(refs)))
        except Exception:
            return {}
        return {
            _normalize_text(record.get("record_id")): dict(record)
            for record in records
            if _normalize_text(record.get("record_id"))
        }


class NeighborhoodQueryService:
    """Scaffolded neighborhood query boundary."""

    def get_node_neighborhood(
        self,
        *,
        repo_id: str | None,
        node_id: str | None,
        concept: str | None,
    ) -> NodeNeighborhoodResponse:
        return NodeNeighborhoodResponse(
            status=SCAFFOLD_STATUS,
            message=(
                "get_node_neighborhood is scaffolded until backend traversal support is implemented."
            ),
            capability=NEIGHBORHOOD_CAPABILITY,
            todo=NEIGHBORHOOD_TODO,
            repo_id=repo_id,
            center_record_id=node_id,
            concept=concept,
            nodes=[],
            edges=[],
        )


class TimelineQueryService:
    """Scaffolded timeline query boundary."""

    def get_timeline(
        self,
        *,
        repo_id: str | None,
        target_kind: str,
        target_id: str,
    ) -> TimelineResponse:
        return TimelineResponse(
            status=SCAFFOLD_STATUS,
            message="get_timeline is scaffolded until temporal backend queries are implemented.",
            capability=TIMELINE_CAPABILITY,
            todo=TIMELINE_TODO,
            repo_id=repo_id,
            target_kind=target_kind,
            target_id=target_id,
            events=[],
        )


def search_context(
    *,
    driver: BackendDriver,
    repo_id: str,
    query: str,
    group_id: str | None,
    sources: list[str],
    limit: int,
    semantic_search: SemanticSearchFn | None = None,
) -> SearchContextResponse:
    normalized_sources = _normalize_sources(sources)
    if not _record_types_for_sources(normalized_sources):
        return SearchContextResponse(
            status=IMPLEMENTED_STATUS,
            message="No indexed record families are mapped to the requested sources.",
            repo_id=repo_id,
            query=query,
            group_id=group_id,
            requested_sources=normalized_sources,
            hits=[],
        )

    entries = HybridContextSearchService(
        driver=driver,
        semantic_search=semantic_search,
    ).search(
        repo_id=repo_id,
        query=query,
        group_id=group_id,
        sources=normalized_sources,
        limit=limit,
    )
    hits = [
        {
            "record_id": _normalize_text(entry.record.get("record_id")),
            "record_type": _normalize_text(entry.record.get("record_type")),
            "title": _record_title(entry.record),
            "summary": _record_summary(entry.record),
            "score": entry.score,
            "lexical_score": entry.lexical_score,
            "semantic_score": entry.semantic_score,
            "observed_at": _record_observed_at(entry.record),
            "source": _source_for_record_type(_normalize_text(entry.record.get("record_type"))),
            "file_path": _record_file_path(entry.record),
            "retrieval_channels": list(entry.retrieval_channels),
            "matched_profile_key_text": entry.matched_profile_key_text,
            "matched_profile_key_origin": entry.matched_profile_key_origin,
            "semantic_profile_kind": entry.semantic_profile_kind,
        }
        for entry in entries
    ]
    if semantic_search is None:
        message = (
            "Completed lexical backend retrieval for the requested context sources. "
            "Semantic profile search is unavailable for this runtime."
        )
    else:
        message = (
            "Completed hybrid provenance retrieval by fusing backend lexical search "
            "with semantic claim and observation profiles."
        )
    return SearchContextResponse(
        status=IMPLEMENTED_STATUS,
        message=message,
        repo_id=repo_id,
        query=query,
        group_id=group_id,
        requested_sources=normalized_sources,
        hits=hits,
    )


def get_node_neighborhood(
    *,
    service: NeighborhoodQueryService,
    repo_id: str | None,
    node_id: str | None,
    concept: str | None,
) -> NodeNeighborhoodResponse:
    return service.get_node_neighborhood(
        repo_id=repo_id,
        node_id=node_id,
        concept=concept,
    )


def get_timeline(
    *,
    service: TimelineQueryService,
    repo_id: str | None,
    target_kind: str,
    target_id: str,
) -> TimelineResponse:
    return service.get_timeline(
        repo_id=repo_id,
        target_kind=target_kind,
        target_id=target_id,
    )


def resolve_claim(
    *,
    driver: BackendDriver,
    claim_resolution_service: ClaimResolutionService | None,
    repo_id: str,
    group_id: str | None,
    claim_text: str,
    claim_kind: str | None,
    claim_subkind: str | None,
    candidate_limit: int,
    semantic_search: SemanticSearchFn | None = None,
) -> ResolveClaimResponse:
    if claim_resolution_service is None or claim_resolution_service.llm_client is None:
        raise RuntimeError("resolve_claim requires a configured llm provider")

    candidate_entries = HybridContextSearchService(
        driver=driver,
        semantic_search=semantic_search,
    ).search(
        repo_id=repo_id,
        query=claim_text,
        group_id=group_id,
        sources=["claims"],
        limit=candidate_limit,
    )
    candidates: list[dict[str, Any]] = []
    candidate_text_by_id: dict[str, str] = {}
    for entry in candidate_entries:
        record = entry.record
        properties = dict(record.get("properties") or {})
        candidate_claim_id = str(properties.get("claim_id") or record["record_id"])
        candidate_claim_text = _normalize_text(properties.get("claim_text"))
        if not candidate_claim_text:
            continue
        candidate = {
            "candidate_claim_id": candidate_claim_id,
            "claim_id": candidate_claim_id,
            "claim_text": candidate_claim_text,
            "claim_kind": _normalize_text(properties.get("claim_kind")),
            "claim_subkind": _safe_claim_subkind(properties.get("claim_subkind")),
        }
        candidates.append(candidate)
        candidate_text_by_id[candidate_claim_id] = candidate_claim_text

    if not candidates:
        return ResolveClaimResponse(
            status=IMPLEMENTED_STATUS,
            message="No nearby indexed claims were found for resolution.",
            repo_id=repo_id,
            candidate_claim_text=claim_text,
            claim_kind=claim_kind,
            claim_subkind=claim_subkind,
            matches=[],
        )

    resolution_response = claim_resolution_service.resolve(
        repo_id=repo_id,
        new_claim={
            "claim_text": claim_text,
            "claim_kind": claim_kind,
            "claim_subkind": claim_subkind,
        },
        candidates=candidates,
    )
    matches = []
    for relation in resolution_response.relations:
        normalized_relation = (
            relation.resolution_type
            if relation.resolution_type in ALLOWED_RESOLUTION_TYPES
            else "unrelated"
        )
        matches.append(
            {
                "candidate_claim_id": relation.candidate_claim_id,
                "candidate_claim_text": candidate_text_by_id.get(relation.candidate_claim_id),
                "relation": normalized_relation,
                "confidence": relation.confidence,
                "rationale": relation.rationale,
            }
        )

    return ResolveClaimResponse(
        status=IMPLEMENTED_STATUS,
        message="Resolved the candidate claim against hybrid-retrieved claim records.",
        repo_id=repo_id,
        candidate_claim_text=claim_text,
        claim_kind=claim_kind,
        claim_subkind=claim_subkind,
        matches=matches,
    )


def synthesize_artifact(
    *,
    driver: BackendDriver,
    artifact_synthesis_service: ResearchArtifactSynthesisService | None,
    snapshot: Any | None,
    repo_id: str,
    group_id: str | None,
    subject_kind: str,
    subject_id: str,
    artifact_kind: str,
    include_code_context: bool,
) -> SynthesizeArtifactResponse:
    if artifact_synthesis_service is None or artifact_synthesis_service.llm_client is None:
        raise RuntimeError("synthesize_artifact requires a configured llm provider")

    code_context_payload: dict[str, Any] = {}
    if include_code_context and snapshot is not None:
        try:
            code_context = get_code_context(
                snapshot=snapshot,
                repo_id=repo_id,
                target=subject_id,
                target_kind=subject_kind,
                include_neighbors=True,
                include_references=True,
                max_related_symbols=5,
            )
            code_context_payload = {
                "primary_symbol": code_context.get("primary_symbol"),
                "related_symbols": code_context.get("related_symbols"),
                "snippets": code_context.get("snippets"),
            }
        except Exception:
            code_context_payload = {}

    claim_records = _search_records(
        driver=driver,
        repo_id=repo_id,
        query=subject_id,
        group_id=group_id,
        record_types=("claim",),
        limit=5,
    )
    evidence_records = _search_records(
        driver=driver,
        repo_id=repo_id,
        query=subject_id,
        group_id=group_id,
        record_types=("evidence_record", "observation"),
        limit=5,
    )

    claims = [_claim_candidate_from_record(record) for _, record in claim_records]
    evidence = [_evidence_candidate_from_record(record) for _, record in evidence_records]
    claims = [candidate for candidate in claims if candidate is not None]
    evidence = [candidate for candidate in evidence if candidate is not None]

    synthesis = artifact_synthesis_service.synthesize(
        repo_id=repo_id,
        repo_region={
            "repo_id": repo_id,
            "subject_kind": subject_kind,
            "subject_id": subject_id,
        },
        code_context=code_context_payload,
        evidence=evidence,
        claims=claims,
    )
    if not synthesis.artifacts:
        return SynthesizeArtifactResponse(
            status=IMPLEMENTED_STATUS,
            message=(
                "Artifact synthesis completed without emitting a grounded artifact. "
                f"Persistence remains deferred via {ARTIFACT_PERSISTENCE_TODO}."
            ),
            repo_id=repo_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            artifact_kind=artifact_kind,
            artifact_id=None,
            title=None,
            summary=None,
            sections=[],
            supporting_claim_ids=[
                _record_claim_id(record)
                for _, record in claim_records
                if _record_claim_id(record) is not None
            ],
            supporting_observation_ids=[
                _record_observation_id(record)
                for _, record in evidence_records
                if _record_observation_id(record) is not None
            ],
        )

    artifact = synthesis.artifacts[0]
    sections = [
        {
            "section_id": f"key_point_{index + 1}",
            "title": f"Key Point {index + 1}",
            "summary": key_point,
        }
        for index, key_point in enumerate(artifact.key_points)
    ]
    if not sections and artifact.summary:
        sections.append(
            {
                "section_id": "summary",
                "title": "Summary",
                "summary": artifact.summary,
            }
        )
    return SynthesizeArtifactResponse(
        status=IMPLEMENTED_STATUS,
        message=(
            "Synthesized a read-only research artifact from indexed code and provenance context. "
            f"Persistence remains deferred via {ARTIFACT_PERSISTENCE_TODO}."
        ),
        repo_id=repo_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        artifact_kind=artifact.artifact_kind or artifact_kind,
        artifact_id=None,
        title=artifact.title,
        summary=artifact.summary,
        sections=sections,
        supporting_claim_ids=[
            _record_claim_id(record)
            for _, record in claim_records
            if _record_claim_id(record) is not None
        ],
        supporting_observation_ids=[
            _record_observation_id(record)
            for _, record in evidence_records
            if _record_observation_id(record) is not None
        ],
    )


def get_code_context(
    *,
    snapshot: Any,
    repo_id: str,
    target: str,
    target_kind: str | None,
    include_neighbors: bool,
    include_references: bool,
    max_related_symbols: int,
) -> CodeContextResponse:
    primary = _resolve_primary_node(snapshot, target=target, target_kind=target_kind)
    related_nodes = _related_nodes(
        snapshot,
        primary=primary,
        include_references=include_references,
        limit=max_related_symbols,
    )
    snippets = _context_snippets(
        snapshot,
        primary=primary,
        related_nodes=related_nodes,
        include_neighbors=include_neighbors,
    )
    return CodeContextResponse(
        status=IMPLEMENTED_STATUS,
        message="Built a deterministic code-context slice from the indexed snapshot.",
        repo_id=repo_id,
        target=target,
        target_kind=target_kind,
        primary_symbol=_node_ref(primary),
        related_symbols=[_node_ref(node) for node in related_nodes],
        snippets=snippets,
    )


def record_agent_observation(
    *,
    result: ObservationWriteResult,
    task_id: str | None,
) -> RecordAgentObservationResponse:
    response: RecordAgentObservationResponse = {
        "status": IMPLEMENTED_STATUS if result.persisted else SCAFFOLD_STATUS,
        "message": result.message,
        "capability": result.capability,
        "repo_id": result.repo_id,
        "observation_id": result.observation_id,
        "task_id": task_id,
        "accepted": result.accepted,
        "persisted": result.persisted,
        "queue_position": result.queue_position,
    }
    if result.todo:
        response["todo"] = result.todo
    return response


def _search_records(
    *,
    driver: BackendDriver,
    repo_id: str,
    query: str,
    group_id: str | None,
    record_types: tuple[str, ...],
    limit: int,
    filters: Mapping[str, object] | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    search_hits = _run_async(
        driver.search.search(
            SearchQuery(
                mode=SearchMode.LEXICAL,
                text=query,
                record_kinds=(RecordKind.NODE,),
                record_types=record_types,
                repo_ids=(repo_id,),
                group_ids=(group_id,) if group_id else None,
                limit=limit,
                filters=dict(filters or {}),
            )
        )
    )
    if not search_hits:
        return []

    records = _run_async(driver.search.fetch([hit.ref for hit in search_hits]))
    records_by_id = {
        str(record.get("record_id")): dict(record)
        for record in records
    }
    ordered: list[tuple[float, dict[str, Any]]] = []
    for hit in search_hits:
        record = records_by_id.get(hit.ref.record_id)
        if record is not None:
            ordered.append((hit.score, record))
    return ordered


def _normalize_sources(sources: list[str]) -> list[str]:
    normalized = []
    seen: set[str] = set()
    for source in sources:
        normalized_source = _normalize_text(source).lower()
        if not normalized_source:
            continue
        if normalized_source not in SOURCE_RECORD_TYPES:
            raise ValueError(
                f"unsupported source '{source}'. Supported sources are: {', '.join(SEARCH_CONTEXT_SOURCES)}"
            )
        if normalized_source not in seen:
            seen.add(normalized_source)
            normalized.append(normalized_source)
    return normalized or list(SEARCH_CONTEXT_SOURCES)


def _record_types_for_sources(sources: list[str]) -> tuple[str, ...]:
    ordered_types: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for record_type in SOURCE_RECORD_TYPES[source]:
            if record_type not in seen:
                seen.add(record_type)
                ordered_types.append(record_type)
    return tuple(ordered_types)


def _source_for_record_type(record_type: str) -> str:
    if record_type == "evidence_record":
        return "observations"
    for source, record_types in SOURCE_RECORD_TYPES.items():
        if record_type in record_types:
            return source
    return "unknown"


def _record_title(record: dict[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _first_text(
        properties.get("title"),
        properties.get("claim_text"),
        properties.get("summary_text"),
        properties.get("subject"),
        properties.get("name"),
        properties.get("sha"),
        record.get("record_id"),
    )


def _record_summary(record: dict[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _truncate(
        _first_text(
            properties.get("summary_text"),
            properties.get("text"),
            properties.get("quote_text"),
            properties.get("claim_text"),
            properties.get("subject"),
        ),
        limit=240,
    )


def _record_file_path(record: dict[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _normalize_text(properties.get("file_path"))


def _record_observed_at(record: dict[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _first_text(
        properties.get("observed_at"),
        properties.get("latest_observed_at"),
        properties.get("first_observed_at"),
        properties.get("committed_at"),
        properties.get("authored_at"),
        properties.get("updated_at"),
        properties.get("created_at"),
    )


def _record_claim_id(record: dict[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _normalize_text(properties.get("claim_id")) or _normalize_text(record.get("record_id"))


def _record_observation_id(record: dict[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _normalize_text(properties.get("observation_id")) or _normalize_text(record.get("record_id"))


def _record_identity(record: dict[str, Any]) -> tuple[str, str]:
    record_type = _normalize_text(record.get("record_type"))
    if record_type == "claim":
        return ("claim", _record_claim_id(record) or _normalize_text(record.get("record_id")))
    if record_type in {"observation", "evidence_record"}:
        return ("observation", _record_observation_id(record) or _normalize_text(record.get("record_id")))
    return (record_type or "unknown", _normalize_text(record.get("record_id")))


def _prefer_record(*, candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    candidate_type = _normalize_text(candidate.get("record_type"))
    current_type = _normalize_text(current.get("record_type"))
    preferred_types = {"claim", "observation"}
    return candidate_type in preferred_types and current_type not in preferred_types


def _fallback_record_from_profile_hit(profile_hit: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(profile_hit.get("metadata") or {})
    profile_kind = _normalize_text(profile_hit.get("profile_kind"))
    target_id = _normalize_text(profile_hit.get("profile_target_id"))
    if profile_kind == SEMANTIC_CLAIM_PROFILE_KIND:
        record_type = "claim"
        properties = {
            "claim_id": metadata.get("claim_id") or target_id,
            "claim_text": metadata.get("claim_text") or profile_hit.get("value_text"),
            "claim_kind": metadata.get("claim_kind"),
            "claim_subkind": metadata.get("claim_subkind"),
            "latest_observed_at": metadata.get("valid_to") or metadata.get("valid_from"),
            "context_kind": metadata.get("context_kind"),
            "context_id": metadata.get("context_id"),
        }
    else:
        record_type = "observation"
        properties = {
            "observation_id": metadata.get("observation_id") or target_id,
            "title": None,
            "text": profile_hit.get("value_text"),
            "observed_at": metadata.get("observed_at"),
            "source_kind": metadata.get("source_kind"),
            "source_id": metadata.get("source_id"),
            "context_kind": metadata.get("context_kind"),
            "context_id": metadata.get("context_id"),
            "thread_kind": metadata.get("thread_kind"),
            "thread_id": metadata.get("thread_id"),
        }
    return {
        "record_id": target_id,
        "record_type": record_type,
        "properties": properties,
    }


def _claim_candidate_from_record(record: dict[str, Any]) -> ClaimCandidate | None:
    properties = dict(record.get("properties") or {})
    claim_text = _normalize_text(properties.get("claim_text"))
    claim_kind = _normalize_text(properties.get("claim_kind"))
    if not claim_text or not claim_kind:
        return None
    return ClaimCandidate(
        claim_text=claim_text,
        claim_kind=claim_kind,
        claim_subkind=_safe_claim_subkind(properties.get("claim_subkind")),
        assertion_status=_safe_assertion_status(properties.get("assertion_status")),
        claim_frame=_safe_claim_frame(properties.get("claim_frame")),
        about_hints=[],
        supported_evidence_ids=[],
        derivation_kind="retrieved_context",
    )


def _evidence_candidate_from_record(record: dict[str, Any]) -> EvidenceCandidate | None:
    properties = dict(record.get("properties") or {})
    quote_text = _first_text(properties.get("quote_text"), properties.get("text"))
    support_kind = _normalize_text(properties.get("support_kind")) or "provides_context"
    if not quote_text:
        return None
    return EvidenceCandidate(
        evidence_kind=_normalize_text(properties.get("evidence_kind")) or record["record_type"],
        quote_text=quote_text,
        summary_text=_normalize_text(properties.get("summary_text")),
        span_start=_int_or_none(properties.get("span_start")),
        span_end=_int_or_none(properties.get("span_end")),
        about_hints=[],
        support_kind=support_kind,
    )


def _resolve_primary_node(snapshot: Any, *, target: str, target_kind: str | None) -> Any:
    kind_map = {
        "function": "Function",
        "class": "Class",
        "module": "Module",
        "file": "File",
        "symbol": None,
    }
    expected_kind = kind_map.get((target_kind or "").lower())
    for node in snapshot.nodes:
        if expected_kind and node.kind != expected_kind:
            continue
        if node.qualified_name == target or node.node_id == target:
            return node
        if node.file_path == target and (expected_kind in {None, "File"}):
            return node
        if node.name == target:
            return node

    search_results = code_graph_queries.search_code_graph(
        snapshot,
        query=target,
        node_kinds=[expected_kind] if expected_kind else None,
        limit=1,
        offset=0,
    )
    if not search_results["matches"]:
        raise code_graph_queries.CodeGraphQueryError(
            f"No indexed code context found for '{target}' in repo '{snapshot.repo_id}'"
        )
    node_id = search_results["matches"][0]["node_id"]
    return snapshot.node_map()[node_id]


def _related_nodes(
    snapshot: Any,
    *,
    primary: Any,
    include_references: bool,
    limit: int,
) -> list[Any]:
    node_map = snapshot.node_map()
    ordered: list[Any] = []
    seen: set[str] = {primary.node_id}

    def add_node(node: Any) -> None:
        if node.node_id in seen:
            return
        seen.add(node.node_id)
        ordered.append(node)

    if primary.file_path:
        same_file_nodes = [
            node
            for node in snapshot.nodes
            if node.file_path == primary.file_path and node.node_id != primary.node_id
        ]
        same_file_nodes.sort(
            key=lambda node: (
                node.start_line or 0,
                (node.qualified_name or node.name).lower(),
                node.node_id,
            )
        )
        for node in same_file_nodes:
            add_node(node)

    if include_references and primary.qualified_name and primary.kind != "File":
        trace = code_graph_queries.trace_call_path(
            snapshot,
            qualified_name=primary.qualified_name,
            direction="both",
            depth=1,
        )
        for step in trace["steps"]:
            for node_id in (step["source_node_id"], step["target_node_id"]):
                if node_id == primary.node_id:
                    continue
                node = node_map.get(node_id)
                if node is not None:
                    add_node(node)

    return ordered[:limit]


def _context_snippets(
    snapshot: Any,
    *,
    primary: Any,
    related_nodes: list[Any],
    include_neighbors: bool,
) -> list[CodeContextSnippet]:
    snippets: list[CodeContextSnippet] = []
    seen: set[tuple[str | None, str]] = set()

    def add_symbol_snippet(node: Any) -> None:
        if not node.qualified_name:
            return
        key = (node.qualified_name, node.file_path or "")
        if key in seen:
            return
        seen.add(key)
        snippet = code_graph_queries.get_code_snippet(
            snapshot,
            qualified_name=node.qualified_name,
            include_neighbors=include_neighbors,
        )
        snippets.append(
            CodeContextSnippet(
                file_path=snippet["file_path"],
                qualified_name=snippet["node"]["qualified_name"],
                start_line=snippet["start_line"],
                end_line=snippet["end_line"],
                snippet=snippet["snippet"],
            )
        )

    if primary.kind == "File":
        file_path = primary.file_path or primary.name
        absolute_path = (Path(snapshot.root_path) / file_path).resolve()
        if absolute_path.exists():
            file_text = absolute_path.read_text(encoding="utf-8", errors="replace")
            snippets.append(
                CodeContextSnippet(
                    file_path=file_path,
                    qualified_name=primary.qualified_name,
                    start_line=1,
                    end_line=len(file_text.splitlines()),
                    snippet=file_text,
                )
            )
        for node in related_nodes:
            add_symbol_snippet(node)
        return snippets

    add_symbol_snippet(primary)
    for node in related_nodes:
        add_symbol_snippet(node)
    return snippets


def _node_ref(node: Any) -> CodeGraphNodeRef:
    return CodeGraphNodeRef(
        node_id=node.node_id,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        file_path=node.file_path,
        start_line=node.start_line,
        end_line=node.end_line,
    )


def _safe_claim_subkind(value: object) -> str | None:
    normalized = _normalize_text(value)
    if normalized in ALLOWED_CLAIM_SUBKINDS:
        return normalized
    return None


def _safe_assertion_status(value: object) -> str | None:
    normalized = _normalize_text(value)
    if normalized in ALLOWED_ASSERTION_STATUSES:
        return normalized
    return None


def _safe_claim_frame(value: object) -> str | None:
    normalized = _normalize_text(value)
    if normalized in ALLOWED_CLAIM_FRAMES:
        return normalized
    return None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _first_text(*values: object) -> str | None:
    for value in values:
        normalized = _normalize_text(value)
        if normalized:
            return normalized
    return None


def _normalize_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split()).strip()
    return normalized or None


def _truncate(value: str | None, *, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _run_async(coro: Any) -> Any:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    future = asyncio.ensure_future(coro)
    raise RuntimeError(
        "provenance_tools._run_async was called while an event loop was already running; "
        "callers should await backend operations before invoking synchronous helpers"
    )
