"""Helpers for provenance-oriented MCP tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from context_graph.profiles import (
    SEMANTIC_CLAIM_PROFILE_KIND,
    SEMANTIC_OBSERVATION_PROFILE_KIND,
    SEMANTIC_RELATION_PROFILE_KIND,
)
from context_graph.server.mcp import code_graph as code_graph_queries
from context_graph.server.mcp.response_types import (
    CodeContextResponse,
    CodeContextSnippet,
    CodeGraphNodeRef,
    NeighborhoodEdge,
    NeighborhoodNode,
    NodeNeighborhoodResponse,
    SearchContextResponse,
    TimelineEvent,
    TimelineResponse,
)
from context_graph.storage.backend import (
    BackendDriver,
    GraphRecordRef,
    RecordKind,
    SearchMode,
    SearchQuery,
    TraversalQuery,
)

IMPLEMENTED_STATUS = "implemented"

SEARCH_CONTEXT_SOURCES = ("claims", "observations", "relations", "code")
SOURCE_RECORD_TYPES: dict[str, tuple[str, ...]] = {
    "claims": ("claim",),
    "observations": ("observation", "evidence_record"),
    "relations": ("claim_relation",),
    "code": ("code_node", "project", "commit"),
}
PROVENANCE_TRAVERSAL_LIMIT = 200

LEXICAL_CHANNEL = "lexical"
SEMANTIC_CLAIM_CHANNEL = "semantic_claim"
SEMANTIC_OBSERVATION_CHANNEL = "semantic_observation"
SEMANTIC_RELATION_CHANNEL = "semantic_relation"
HYBRID_RRF_K = 60
HYBRID_CHANNEL_WEIGHTS = {
    LEXICAL_CHANNEL: 1.0,
    SEMANTIC_CLAIM_CHANNEL: 0.9,
    SEMANTIC_OBSERVATION_CHANNEL: 0.75,
    SEMANTIC_RELATION_CHANNEL: 0.75,
}

SemanticSearchFn = Callable[..., list[dict[str, Any]]]


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
    matched_lexical_summary: str | None = None


@dataclass(frozen=True, slots=True)
class RelationContextEntry:
    """One relation-profile retrieval result."""

    record_id: str
    relation_type: str | None
    score: float
    is_active: bool
    lexical_score: float | None = None
    semantic_score: float | None = None
    retrieval_channels: tuple[str, ...] = ()
    value_text: str = ""
    matched_profile_key_text: str | None = None
    matched_profile_key_origin: str | None = None
    semantic_profile_kind: str | None = None
    source_record_id: str | None = None
    source_record_type: str | None = None
    target_record_id: str | None = None
    target_record_type: str | None = None
    observed_at: str | None = None
    inactive_at: str | None = None


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
    matched_lexical_summary: str | None = None


@dataclass(slots=True)
class _LexicalNodeCandidate:
    record: dict[str, Any]
    raw_score: float
    matched_lexical_summary: str | None = None


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
        active_only: bool = True,
    ) -> tuple[HybridContextEntry, ...]:
        return _run_async(
            self.search_async(
                repo_id=repo_id,
                query=query,
                group_id=group_id,
                sources=sources,
                limit=limit,
                active_only=active_only,
            )
        )

    async def search_async(
        self,
        *,
        repo_id: str,
        query: str,
        group_id: str | None,
        sources: Sequence[str],
        limit: int,
        active_only: bool = True,
    ) -> tuple[HybridContextEntry, ...]:
        normalized_sources = _normalize_sources(list(sources))
        overfetch_limit = max(limit * 4, limit)
        lexical_records = await _search_records_async(
            driver=self.driver,
            repo_id=repo_id,
            query=query,
            group_id=group_id,
            record_kinds=(RecordKind.NODE,),
            record_types=_record_types_for_sources(normalized_sources),
            limit=overfetch_limit,
        )
        lexical_records = await self._surface_lexical_records_async(
            repo_id=repo_id,
            group_id=group_id,
            records=lexical_records,
        )

        semantic_claim_hits = (
            await _invoke_semantic_search_async(
                semantic_search=self.semantic_search,
                query=query,
                repo_id=repo_id,
                limit=overfetch_limit,
                active_only=active_only,
                profile_kinds=(SEMANTIC_CLAIM_PROFILE_KIND,),
                llm_enabled=True,
                query_texts=None,
            )
            if "claims" in normalized_sources
            else ()
        )
        semantic_observation_hits = (
            await _invoke_semantic_search_async(
                semantic_search=self.semantic_search,
                query=query,
                repo_id=repo_id,
                limit=overfetch_limit,
                active_only=active_only,
                profile_kinds=(SEMANTIC_OBSERVATION_PROFILE_KIND,),
                llm_enabled=False,
                query_texts=(query,),
            )
            if "observations" in normalized_sources
            else ()
        )

        aggregates: dict[tuple[str, str], _HybridContextAggregate] = {}

        for rank, lexical_hit in enumerate(lexical_records, start=1):
            self._apply_channel_contribution(
                aggregates=aggregates,
                identity=_record_identity(lexical_hit.record),
                record=lexical_hit.record,
                channel=LEXICAL_CHANNEL,
                raw_score=lexical_hit.raw_score,
                rank=rank,
                matched_lexical_summary=lexical_hit.matched_lexical_summary,
            )

        for channel, hits in (
            (SEMANTIC_CLAIM_CHANNEL, semantic_claim_hits),
            (SEMANTIC_OBSERVATION_CHANNEL, semantic_observation_hits),
        ):
            for rank, hit in enumerate(hits, start=1):
                record = await self._semantic_profile_record_async(
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
                matched_lexical_summary=entry.matched_lexical_summary,
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
        matched_lexical_summary: str | None = None,
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
            if matched_lexical_summary and entry.matched_lexical_summary is None:
                entry.matched_lexical_summary = matched_lexical_summary
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

    def _surface_lexical_records(
        self,
        *,
        repo_id: str,
        group_id: str | None,
        records: Sequence[tuple[float, dict[str, Any]]],
    ) -> tuple[tuple[float, dict[str, Any]], ...]:
        return _run_async(
            self._surface_lexical_records_async(
                repo_id=repo_id,
                group_id=group_id,
                records=records,
            )
        )

    async def _surface_lexical_records_async(
        self,
        *,
        repo_id: str,
        group_id: str | None,
        records: Sequence[tuple[float, dict[str, Any]]],
    ) -> tuple[_LexicalNodeCandidate, ...]:
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
        observation_records = await self._fetch_records_async(observation_refs)
        surfaced: list[_LexicalNodeCandidate] = []
        surfaced_by_identity: dict[tuple[str, str], _LexicalNodeCandidate] = {}
        for raw_score, record in records:
            record_type = _normalize_text(record.get("record_type"))
            matched_lexical_summary: str | None = None
            if record_type == "evidence_record":
                observation_id = _record_observation_id(record)
                surfaced_record = observation_records.get(observation_id, record) if observation_id else record
                matched_lexical_summary = _matched_lexical_summary(record)
            else:
                surfaced_record = record
            identity = _record_identity(surfaced_record)
            existing = surfaced_by_identity.get(identity)
            if existing is None:
                candidate = _LexicalNodeCandidate(
                    record=surfaced_record,
                    raw_score=raw_score,
                    matched_lexical_summary=matched_lexical_summary,
                )
                surfaced_by_identity[identity] = candidate
                surfaced.append(candidate)
                continue
            if _prefer_record(candidate=surfaced_record, current=existing.record):
                existing.record = surfaced_record
            if existing.matched_lexical_summary is None and matched_lexical_summary:
                existing.matched_lexical_summary = matched_lexical_summary
        return tuple(surfaced)

    def _semantic_profile_record(
        self,
        *,
        repo_id: str,
        group_id: str | None,
        profile_hit: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _run_async(
            self._semantic_profile_record_async(
                repo_id=repo_id,
                group_id=group_id,
                profile_hit=profile_hit,
            )
        )

    async def _semantic_profile_record_async(
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
            record_type = (_normalize_text(profile_hit.get("profile_target_kind")) or "").lower()
        if target_id:
            records = await self._fetch_records_async(
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
        return _run_async(self._fetch_records_async(refs))

    async def _fetch_records_async(self, refs: Sequence[GraphRecordRef]) -> dict[str, dict[str, Any]]:
        if not refs:
            return {}
        try:
            records = await self.driver.search.fetch(list(refs))
        except Exception:
            return {}
        return {
            _normalize_text(record.get("record_id")): dict(record)
            for record in records
            if _normalize_text(record.get("record_id"))
        }


class RelationContextSearchService:
    """Search relation edges through lexical and semantic channels."""

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
        group_id: str | None,
        query: str,
        limit: int,
        active_only: bool,
    ) -> tuple[RelationContextEntry, ...]:
        return _run_async(
            self.search_async(
                repo_id=repo_id,
                group_id=group_id,
                query=query,
                limit=limit,
                active_only=active_only,
            )
        )

    async def search_async(
        self,
        *,
        repo_id: str,
        group_id: str | None,
        query: str,
        limit: int,
        active_only: bool,
    ) -> tuple[RelationContextEntry, ...]:
        overfetch_limit = max(limit * 4, limit)
        lexical_records = await _search_records_async(
            driver=self.driver,
            repo_id=repo_id,
            query=query,
            group_id=group_id,
            record_kinds=(RecordKind.EDGE,),
            record_types=("claim_relation",),
            limit=overfetch_limit,
            filters={"is_active": True} if active_only else None,
        )
        aggregates: dict[str, dict[str, Any]] = {}
        for rank, (raw_score, record) in enumerate(lexical_records, start=1):
            record_id = _normalize_text(record.get("record_id"))
            if not record_id:
                continue
            aggregate = aggregates.setdefault(
                record_id,
                {
                    "record_id": record_id,
                    "relation_type": _relation_type_from_record(record),
                    "value_text": _relation_text_from_record(record),
                    "score": 0.0,
                    "is_active": _relation_record_is_active(record),
                    "lexical_score": None,
                    "semantic_score": None,
                    "retrieval_channels": [],
                    "matched_profile_key_text": None,
                    "matched_profile_key_origin": None,
                    "semantic_profile_kind": None,
                    "source_record_id": _normalize_text(record.get("source_record_id")),
                    "source_record_type": "claim",
                    "target_record_id": _normalize_text(record.get("target_record_id")),
                    "target_record_type": "claim",
                    "observed_at": _relation_observed_at_from_record(record),
                    "inactive_at": _relation_record_inactive_at(record),
                },
            )
            aggregate["score"] += HYBRID_CHANNEL_WEIGHTS[LEXICAL_CHANNEL] / (HYBRID_RRF_K + rank)
            aggregate["lexical_score"] = (
                raw_score
                if aggregate["lexical_score"] is None
                else max(aggregate["lexical_score"], raw_score)
            )
            if LEXICAL_CHANNEL not in aggregate["retrieval_channels"]:
                aggregate["retrieval_channels"].append(LEXICAL_CHANNEL)

        if self.semantic_search is None and not lexical_records:
            return ()
        hits = []
        if self.semantic_search is not None:
            hits = await _invoke_semantic_search_async(
                semantic_search=self.semantic_search,
                query=query,
                repo_id=repo_id,
                limit=overfetch_limit,
                active_only=active_only,
                profile_kinds=(SEMANTIC_RELATION_PROFILE_KIND,),
                llm_enabled=False,
                query_texts=(query,),
            )
        for rank, hit in enumerate(hits, start=1):
            if not isinstance(hit, Mapping):
                continue
            record_id = _normalize_text(hit.get("profile_target_id"))
            if not record_id:
                continue
            aggregate = aggregates.setdefault(
                record_id,
                {
                    "record_id": record_id,
                    "relation_type": _normalize_text(hit.get("relation_type")),
                    "value_text": _normalize_text(hit.get("value_text")) or "",
                    "score": 0.0,
                    "is_active": _coerce_bool(hit.get("is_active"), default=True),
                    "lexical_score": None,
                    "semantic_score": None,
                    "retrieval_channels": [],
                    "matched_profile_key_text": None,
                    "matched_profile_key_origin": None,
                    "semantic_profile_kind": _normalize_text(hit.get("profile_kind")),
                    "source_record_id": _normalize_text(hit.get("source_node_id")),
                    "source_record_type": _normalize_text(dict(hit.get("metadata") or {}).get("source_record_type")),
                    "target_record_id": _normalize_text(hit.get("target_node_id")),
                    "target_record_type": _normalize_text(dict(hit.get("metadata") or {}).get("target_record_type")),
                    "observed_at": _first_text(
                        dict(hit.get("metadata") or {}).get("observed_at"),
                        dict(hit.get("metadata") or {}).get("updated_at"),
                    ),
                    "inactive_at": _first_text(
                        dict(hit.get("metadata") or {}).get("deactivated_at"),
                        dict(hit.get("metadata") or {}).get("valid_to"),
                    )
                    if not _coerce_bool(hit.get("is_active"), default=True)
                    else None,
                },
            )
            aggregate["score"] += HYBRID_CHANNEL_WEIGHTS[SEMANTIC_RELATION_CHANNEL] / (HYBRID_RRF_K + rank)
            aggregate["semantic_score"] = (
                float(hit.get("score") or 0.0)
                if aggregate["semantic_score"] is None
                else max(aggregate["semantic_score"], float(hit.get("score") or 0.0))
            )
            aggregate["matched_profile_key_text"] = _normalize_text(hit.get("matched_key_text"))
            aggregate["matched_profile_key_origin"] = _normalize_text(hit.get("matched_key_origin"))
            aggregate["semantic_profile_kind"] = _normalize_text(hit.get("profile_kind"))
            aggregate["relation_type"] = aggregate["relation_type"] or _normalize_text(hit.get("relation_type"))
            aggregate["value_text"] = aggregate["value_text"] or (_normalize_text(hit.get("value_text")) or "")
            aggregate["source_record_id"] = aggregate["source_record_id"] or _normalize_text(hit.get("source_node_id"))
            aggregate["target_record_id"] = aggregate["target_record_id"] or _normalize_text(hit.get("target_node_id"))
            aggregate["source_record_type"] = aggregate["source_record_type"] or _normalize_text(dict(hit.get("metadata") or {}).get("source_record_type"))
            aggregate["target_record_type"] = aggregate["target_record_type"] or _normalize_text(dict(hit.get("metadata") or {}).get("target_record_type"))
            aggregate["observed_at"] = aggregate["observed_at"] or _first_text(
                dict(hit.get("metadata") or {}).get("observed_at"),
                dict(hit.get("metadata") or {}).get("updated_at"),
            )
            aggregate["inactive_at"] = aggregate["inactive_at"] or (
                _first_text(
                    dict(hit.get("metadata") or {}).get("deactivated_at"),
                    dict(hit.get("metadata") or {}).get("valid_to"),
                )
                if not _coerce_bool(hit.get("is_active"), default=True)
                else None
            )
            if SEMANTIC_RELATION_CHANNEL not in aggregate["retrieval_channels"]:
                aggregate["retrieval_channels"].append(SEMANTIC_RELATION_CHANNEL)
        ordered = sorted(
            (
                RelationContextEntry(
                    record_id=aggregate["record_id"],
                    relation_type=aggregate["relation_type"],
                    score=aggregate["score"],
                    is_active=aggregate["is_active"],
                    lexical_score=aggregate["lexical_score"],
                    semantic_score=aggregate["semantic_score"],
                    retrieval_channels=tuple(aggregate["retrieval_channels"]),
                    value_text=aggregate["value_text"],
                    matched_profile_key_text=aggregate["matched_profile_key_text"],
                    matched_profile_key_origin=aggregate["matched_profile_key_origin"],
                    semantic_profile_kind=aggregate["semantic_profile_kind"],
                    source_record_id=aggregate["source_record_id"],
                    source_record_type=aggregate["source_record_type"],
                    target_record_id=aggregate["target_record_id"],
                    target_record_type=aggregate["target_record_type"],
                    observed_at=aggregate["observed_at"],
                    inactive_at=aggregate["inactive_at"],
                )
                for aggregate in aggregates.values()
            ),
            key=lambda entry: (
                not entry.is_active,
                -entry.score,
                entry.record_id,
            ),
        )
        return tuple(ordered[:limit])


class NeighborhoodQueryService:
    """Exact-node neighborhood expansion boundary."""

    def __init__(
        self,
        *,
        driver: BackendDriver | None = None,
        snapshot: Any | None = None,
    ) -> None:
        self.driver = driver
        self.snapshot = snapshot

    def get_node_neighborhood(
        self,
        *,
        repo_id: str | None,
        node_id: str | None,
        hops: int = 2,
        edge_types: Sequence[str] | None = None,
    ) -> NodeNeighborhoodResponse:
        return _run_async(
            self.get_node_neighborhood_async(
                repo_id=repo_id,
                node_id=node_id,
                hops=hops,
                edge_types=edge_types,
            )
        )

    async def get_node_neighborhood_async(
        self,
        *,
        repo_id: str | None,
        node_id: str | None,
        hops: int = 2,
        edge_types: Sequence[str] | None = None,
    ) -> NodeNeighborhoodResponse:
        if not node_id:
            raise ValueError("get_node_neighborhood requires an exact node_id")
        if hops <= 0:
            raise ValueError("hops must be a positive integer")
        if self.driver is None:
            raise RuntimeError("backend driver is required for neighborhood queries")

        if self._is_snapshot_code_node(node_id):
            if self.snapshot is None:
                raise ValueError(
                    "repo_id with an indexed snapshot is required for file/project/symbol neighborhood queries"
                )
            return self._get_snapshot_neighborhood(
                repo_id=repo_id or self.snapshot.repo_id,
                node_id=node_id,
                hops=hops,
                edge_types=edge_types,
            )

        center_record, node_records, edge_records = await _expand_backend_neighborhood_async(
            driver=self.driver,
            node_id=node_id,
            repo_id=repo_id,
            hops=hops,
            edge_types=edge_types,
        )
        nodes = tuple(
            sorted(
                (
                    _neighborhood_node_payload(record)
                    for record in (center_record, *node_records)
                ),
                key=lambda item: (item["record_id"] != node_id, item["record_type"], item["record_id"]),
            )
        )
        edges = tuple(
            sorted(
                (_neighborhood_edge_payload(record) for record in edge_records),
                key=lambda item: (
                    item["edge_type"],
                    item["source_record_id"],
                    item["target_record_id"],
                ),
            )
        )
        return NodeNeighborhoodResponse(
            status=IMPLEMENTED_STATUS,
            message="Expanded the exact node neighborhood from the indexed graph.",
            repo_id=repo_id,
            center_record_id=node_id,
            nodes=list(nodes),
            edges=list(edges),
        )

    def _is_snapshot_code_node(self, node_id: str) -> bool:
        return node_id.startswith(("project:", "file:", "symbol:"))

    def _get_snapshot_neighborhood(
        self,
        *,
        repo_id: str,
        node_id: str,
        hops: int,
        edge_types: Sequence[str] | None,
    ) -> NodeNeighborhoodResponse:
        if self.snapshot is None:
            raise ValueError("snapshot is required for deterministic code neighborhood queries")

        node_map = self.snapshot.node_map()
        center = node_map.get(node_id)
        if center is None:
            raise ValueError(f"No indexed code node found for node_id={node_id}")

        normalized_edge_types = {
            _normalize_text(edge_type).upper()
            for edge_type in edge_types or ()
            if _normalize_text(edge_type)
        }
        adjacency: dict[str, list[Any]] = {}
        for edge in self.snapshot.edges:
            if normalized_edge_types and edge.edge_type.upper() not in normalized_edge_types:
                continue
            adjacency.setdefault(edge.source_id, []).append(edge)
            adjacency.setdefault(edge.target_id, []).append(edge)

        distances: dict[str, int] = {center.node_id: 0}
        queue: list[str] = [center.node_id]
        collected_edges: dict[str, Any] = {}
        while queue:
            current = queue.pop(0)
            current_distance = distances[current]
            if current_distance >= hops:
                continue
            for edge in adjacency.get(current, ()):
                collected_edges[edge.edge_id] = edge
                neighbor_id = edge.target_id if edge.source_id == current else edge.source_id
                if neighbor_id not in distances:
                    distances[neighbor_id] = current_distance + 1
                    queue.append(neighbor_id)

        nodes = sorted(
            (
                NeighborhoodNode(
                    record_id=node.node_id,
                    record_type=node.kind,
                    display_name=node.qualified_name or node.name,
                    summary=node.file_path or None,
                    observed_at=None,
                    qualified_name=node.qualified_name,
                )
                for node_id_key, distance in distances.items()
                if (node := node_map.get(node_id_key)) is not None and distance <= hops
            ),
            key=lambda item: (item["record_id"] != center.node_id, item["record_type"], item["record_id"]),
        )
        edges = sorted(
            (
                NeighborhoodEdge(
                    edge_type=edge.edge_type,
                    source_record_id=edge.source_id,
                    target_record_id=edge.target_id,
                    observed_at=None,
                )
                for edge in collected_edges.values()
            ),
            key=lambda item: (item["edge_type"], item["source_record_id"], item["target_record_id"]),
        )
        return NodeNeighborhoodResponse(
            status=IMPLEMENTED_STATUS,
            message="Expanded the exact code-node neighborhood from the indexed snapshot.",
            repo_id=repo_id,
            center_record_id=center.node_id,
            nodes=list(nodes),
            edges=list(edges),
        )


class TimelineQueryService:
    """Historical provenance timeline assembly boundary."""

    def __init__(
        self,
        *,
        driver: BackendDriver | None = None,
        snapshot: Any | None = None,
    ) -> None:
        self.driver = driver
        self.snapshot = snapshot

    def get_timeline(
        self,
        *,
        repo_id: str | None,
        target_id: str,
        limit: int = 50,
    ) -> TimelineResponse:
        return _run_async(
            self.get_timeline_async(
                repo_id=repo_id,
                target_id=target_id,
                limit=limit,
            )
        )

    async def get_timeline_async(
        self,
        *,
        repo_id: str | None,
        target_id: str,
        limit: int = 50,
    ) -> TimelineResponse:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        if self.driver is None:
            raise RuntimeError("backend driver is required for timeline queries")
        center_record, node_records, edge_records = await _expand_backend_neighborhood_async(
            driver=self.driver,
            node_id=target_id,
            repo_id=repo_id,
            hops=2,
            edge_types=None,
        )
        events = _timeline_events_from_records(
            center_record=center_record,
            node_records=node_records,
            edge_records=edge_records,
            limit=limit,
        )
        return TimelineResponse(
            status=IMPLEMENTED_STATUS,
            message="Built a historical provenance timeline around the exact target.",
            repo_id=repo_id,
            target_id=target_id,
            events=events,
        )


def search_context(
    *,
    driver: BackendDriver,
    repo_id: str,
    query: str,
    group_id: str | None,
    sources: list[str],
    limit: int,
    include_historical: bool = False,
    semantic_search: SemanticSearchFn | None = None,
) -> SearchContextResponse:
    return _run_async(
        search_context_async(
            driver=driver,
            repo_id=repo_id,
            query=query,
            group_id=group_id,
            sources=sources,
            limit=limit,
            include_historical=include_historical,
            semantic_search=semantic_search,
        )
    )


async def search_context_async(
    *,
    driver: BackendDriver,
    repo_id: str,
    query: str,
    group_id: str | None,
    sources: list[str],
    limit: int,
    include_historical: bool = False,
    semantic_search: SemanticSearchFn | None = None,
) -> SearchContextResponse:
    normalized_sources = _normalize_sources(sources)
    node_sources = [source for source in normalized_sources if source != "relations"]
    relation_requested = "relations" in normalized_sources
    if not _record_types_for_sources(node_sources) and not relation_requested:
        return SearchContextResponse(
            status=IMPLEMENTED_STATUS,
            message="No indexed record families are mapped to the requested sources.",
            repo_id=repo_id,
            query=query,
            hits=[],
        )

    node_entries: tuple[HybridContextEntry, ...] = ()
    if _record_types_for_sources(node_sources):
        node_entries = await HybridContextSearchService(
            driver=driver,
            semantic_search=semantic_search,
        ).search_async(
            repo_id=repo_id,
            query=query,
            group_id=group_id,
            sources=node_sources,
            limit=limit,
            active_only=not include_historical,
        )
        if include_historical:
            node_entries = tuple(
                sorted(
                    node_entries,
                    key=lambda entry: (
                        not _record_is_active(entry.record),
                        -entry.score,
                        _normalize_text(entry.record.get("record_id")),
                    ),
                )
            )

    relation_entries: tuple[RelationContextEntry, ...] = ()
    if relation_requested:
        relation_entries = await RelationContextSearchService(
            driver=driver,
            semantic_search=semantic_search,
        ).search_async(
            repo_id=repo_id,
            group_id=group_id,
            query=query,
            limit=limit,
            active_only=not include_historical,
        )

    hits = [
        _node_hit_payload(entry)
        for entry in node_entries
    ]
    hits.extend(_relation_hit_payload(entry) for entry in relation_entries)
    hits = hits[:limit]
    if semantic_search is None:
        message = (
            "Completed lexical backend retrieval for the requested context sources. "
            "Semantic node and relation profile search is unavailable for this runtime."
        )
    elif include_historical:
        message = (
            "Completed provenance retrieval with separate node and relation search pipelines, "
            "including inactive semantic profiles for historical inspection."
        )
    else:
        message = (
            "Completed provenance retrieval with separate node and relation search pipelines."
        )
    return SearchContextResponse(
        status=IMPLEMENTED_STATUS,
        message=message,
        repo_id=repo_id,
        query=query,
        hits=hits,
    )


def get_node_neighborhood(
    *,
    service: NeighborhoodQueryService,
    repo_id: str | None,
    node_id: str | None,
    hops: int,
    edge_types: Sequence[str] | None,
) -> NodeNeighborhoodResponse:
    return service.get_node_neighborhood(
        repo_id=repo_id,
        node_id=node_id,
        hops=hops,
        edge_types=edge_types,
    )


async def get_node_neighborhood_async(
    *,
    service: NeighborhoodQueryService,
    repo_id: str | None,
    node_id: str | None,
    hops: int,
    edge_types: Sequence[str] | None,
) -> NodeNeighborhoodResponse:
    return await service.get_node_neighborhood_async(
        repo_id=repo_id,
        node_id=node_id,
        hops=hops,
        edge_types=edge_types,
    )


def get_timeline(
    *,
    service: TimelineQueryService,
    repo_id: str | None,
    target_id: str,
    limit: int,
) -> TimelineResponse:
    return service.get_timeline(
        repo_id=repo_id,
        target_id=target_id,
        limit=limit,
    )


async def get_timeline_async(
    *,
    service: TimelineQueryService,
    repo_id: str | None,
    target_id: str,
    limit: int,
) -> TimelineResponse:
    return await service.get_timeline_async(
        repo_id=repo_id,
        target_id=target_id,
        limit=limit,
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

def _node_hit_payload(entry: HybridContextEntry) -> dict[str, Any]:
    record_type = _normalize_text(entry.record.get("record_type"))
    payload: dict[str, Any] = {
        "hit_kind": "node",
        "record_id": _normalize_text(entry.record.get("record_id")),
        "record_type": record_type,
        "title": _record_title(entry.record),
        "summary": _truncate(entry.matched_lexical_summary, limit=240)
        if entry.matched_lexical_summary
        else _record_summary(entry.record),
        "is_active": _record_is_active(entry.record),
        "observed_at": _record_observed_at(entry.record),
        "inactive_at": _record_inactive_at(entry.record),
    }
    file_path = _record_file_path(entry.record)
    if file_path:
        payload["file_path"] = file_path
    qualified_name = _record_qualified_name(entry.record)
    if qualified_name:
        payload["qualified_name"] = qualified_name
    return payload


def _relation_type_from_record(record: Mapping[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _first_text(
        properties.get("resolution_type"),
        record.get("relationship_type"),
        record.get("record_type"),
    )


def _relation_text_from_record(record: Mapping[str, Any]) -> str:
    properties = dict(record.get("properties") or {})
    return _first_text(
        properties.get("resolution_rationale"),
        properties.get("summary_text"),
        properties.get("text"),
        "",
    ) or ""


def _relation_observed_at_from_record(record: Mapping[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _first_text(
        properties.get("observed_at"),
        properties.get("updated_at"),
        properties.get("created_at"),
    )


def _relation_record_is_active(record: Mapping[str, Any]) -> bool:
    properties = dict(record.get("properties") or {})
    return _coerce_bool(properties.get("is_active"), default=True)


def _relation_record_inactive_at(record: Mapping[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    if _relation_record_is_active(record):
        return None
    return _first_text(
        properties.get("deactivated_at"),
        properties.get("valid_to"),
    )


def _relation_hit_payload(entry: RelationContextEntry) -> dict[str, Any]:
    return {
        "hit_kind": "relation",
        "record_id": entry.record_id,
        "summary": _truncate(entry.value_text, limit=240),
        "is_active": bool(entry.is_active),
        "observed_at": entry.observed_at,
        "inactive_at": entry.inactive_at,
        "relation_type": entry.relation_type,
        "source_record_id": entry.source_record_id,
        "source_record_type": entry.source_record_type,
        "target_record_id": entry.target_record_id,
        "target_record_type": entry.target_record_type,
    }


def _search_records(
    *,
    driver: BackendDriver,
    repo_id: str,
    query: str,
    group_id: str | None,
    record_kinds: tuple[RecordKind, ...],
    record_types: tuple[str, ...],
    limit: int,
    filters: Mapping[str, object] | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    return _run_async(
        _search_records_async(
            driver=driver,
            repo_id=repo_id,
            query=query,
            group_id=group_id,
            record_kinds=record_kinds,
            record_types=record_types,
            limit=limit,
            filters=filters,
        )
    )


async def _search_records_async(
    *,
    driver: BackendDriver,
    repo_id: str,
    query: str,
    group_id: str | None,
    record_kinds: tuple[RecordKind, ...],
    record_types: tuple[str, ...],
    limit: int,
    filters: Mapping[str, object] | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    search_hits = await driver.search.search(
        SearchQuery(
            mode=SearchMode.LEXICAL,
            text=query,
            record_kinds=record_kinds,
            record_types=record_types,
            repo_ids=(repo_id,),
            group_ids=(group_id,) if group_id else None,
            limit=limit,
            filters=dict(filters or {}),
        )
    )
    if not search_hits:
        return []

    records = await driver.search.fetch([hit.ref for hit in search_hits])
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


async def _fetch_exact_node_record_async(
    *,
    driver: BackendDriver,
    node_id: str,
    repo_id: str | None,
) -> dict[str, Any]:
    records = await driver.search.fetch(
        [
            GraphRecordRef(
                record_kind=RecordKind.NODE,
                record_id=node_id,
                record_type="",
                repo_id=repo_id,
            )
        ]
    )
    if not records:
        raise ValueError(f"No indexed node found for node_id={node_id}")
    return dict(records[0])


async def _expand_backend_neighborhood_async(
    *,
    driver: BackendDriver,
    node_id: str,
    repo_id: str | None,
    hops: int,
    edge_types: Sequence[str] | None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    center_record = await _fetch_exact_node_record_async(driver=driver, node_id=node_id, repo_id=repo_id)
    effective_repo_id = repo_id or _normalize_text(center_record.get("repo_id")) or None
    effective_group_id = _normalize_text(center_record.get("group_id")) or None
    traversal_hits = await driver.traversal.expand(
        TraversalQuery(
            origin_refs=(
                GraphRecordRef(
                    record_kind=RecordKind.NODE,
                    record_id=node_id,
                    record_type=_normalize_text(center_record.get("record_type")) or "",
                    repo_id=effective_repo_id,
                    group_id=effective_group_id,
                ),
            ),
            max_depth=max(1, int(hops)),
            edge_types=tuple(
                normalized
                for normalized in (
                    _normalize_text(edge_type)
                    for edge_type in (edge_types or ())
                )
                if normalized
            )
            or None,
            direction="both",
            repo_ids=(effective_repo_id,) if effective_repo_id else None,
            group_ids=(effective_group_id,) if effective_group_id else None,
            limit=PROVENANCE_TRAVERSAL_LIMIT,
        )
    )
    node_refs: list[GraphRecordRef] = []
    edge_refs: list[GraphRecordRef] = []
    seen_node_ids: set[str] = set()
    seen_edge_ids: set[str] = set()
    for hit in traversal_hits:
        if hit.ref.record_kind is RecordKind.NODE:
            if hit.ref.record_id == node_id or hit.ref.record_id in seen_node_ids:
                continue
            seen_node_ids.add(hit.ref.record_id)
            node_refs.append(hit.ref)
            continue
        if hit.ref.record_kind is RecordKind.EDGE and hit.ref.record_id not in seen_edge_ids:
            seen_edge_ids.add(hit.ref.record_id)
            edge_refs.append(hit.ref)

    node_records = tuple(await driver.search.fetch(node_refs)) if node_refs else ()
    edge_records = tuple(await driver.search.fetch(edge_refs)) if edge_refs else ()
    return center_record, tuple(dict(record) for record in node_records), tuple(
        dict(record) for record in edge_records
    )


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


def _record_is_active(record: Mapping[str, Any]) -> bool:
    properties = dict(record.get("properties") or {})
    if "is_current" in properties:
        return _coerce_bool(properties.get("is_current"), default=True)
    if "is_active" in properties:
        return _coerce_bool(properties.get("is_active"), default=True)
    return True


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


def _matched_lexical_summary(record: Mapping[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _truncate(
        _first_text(
            properties.get("quote_text"),
            properties.get("summary_text"),
            properties.get("text"),
        ),
        limit=240,
    )


def _record_file_path(record: dict[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    return _normalize_text(properties.get("file_path"))


def _record_qualified_name(record: Mapping[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    qualified_name = _normalize_text(properties.get("qualified_name"))
    if qualified_name:
        return qualified_name
    record_type = _normalize_text(record.get("record_type"))
    if record_type in {"code_node", "project", "file", "symbol"}:
        return _normalize_text(properties.get("title"))
    return None


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


def _record_inactive_at(record: Mapping[str, Any]) -> str | None:
    properties = dict(record.get("properties") or {})
    if _record_is_active(record):
        return None
    return _first_text(
        properties.get("valid_to"),
        properties.get("deactivated_at"),
        properties.get("invalidated_at"),
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
            "is_current": metadata.get("is_current", True),
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
            "is_current": True,
        }
    return {
        "record_id": target_id,
        "record_type": record_type,
        "properties": properties,
    }
def _fetch_exact_node_record(
    *,
    driver: BackendDriver,
    node_id: str,
    repo_id: str | None,
) -> dict[str, Any]:
    return _run_async(
        _fetch_exact_node_record_async(
            driver=driver,
            node_id=node_id,
            repo_id=repo_id,
        )
    )

def _neighborhood_node_payload(record: Mapping[str, Any]) -> NeighborhoodNode:
    payload = NeighborhoodNode(
        record_id=_normalize_text(record.get("record_id")) or "",
        record_type=_normalize_text(record.get("record_type")) or "unknown",
        display_name=_record_title(dict(record)),
        summary=_record_summary(dict(record)),
        observed_at=_record_observed_at(dict(record)),
    )
    qualified_name = _record_qualified_name(record)
    if qualified_name:
        payload["qualified_name"] = qualified_name
    return payload


def _neighborhood_edge_payload(record: Mapping[str, Any]) -> NeighborhoodEdge:
    return NeighborhoodEdge(
        edge_type=_relation_type_from_record(record) or _normalize_text(record.get("record_type")) or "related_to",
        source_record_id=_normalize_text(record.get("source_record_id")) or "",
        target_record_id=_normalize_text(record.get("target_record_id")) or "",
        observed_at=_relation_observed_at_from_record(record),
    )


def _timeline_events_from_records(
    *,
    center_record: Mapping[str, Any],
    node_records: Sequence[Mapping[str, Any]],
    edge_records: Sequence[Mapping[str, Any]],
    limit: int,
) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    seen_event_ids: set[str] = set()

    def add_event(event: TimelineEvent) -> None:
        if event["event_id"] in seen_event_ids:
            return
        seen_event_ids.add(event["event_id"])
        events.append(event)

    for record in (center_record, *node_records):
        record_type = _normalize_text(record.get("record_type"))
        if record_type != "claim":
            continue
        record_id = _normalize_text(record.get("record_id"))
        observed_at = _record_observed_at(dict(record))
        add_event(
            TimelineEvent(
                event_id=f"{record_id}:observed",
                event_type=record_type or "record",
                title=_record_title(dict(record)),
                summary=_record_summary(dict(record)),
                observed_at=observed_at,
                record_id=record_id,
            )
        )
        properties = dict(record.get("properties") or {})
        inactive_at = _first_text(properties.get("deactivated_at"), properties.get("valid_to"))
        if inactive_at and not _record_is_active(record):
            add_event(
                TimelineEvent(
                    event_id=f"{record_id}:inactive",
                    event_type=f"{record_type}_inactive",
                    title=_record_title(dict(record)),
                    summary="Marked inactive in provenance history.",
                    observed_at=inactive_at,
                    record_id=record_id,
                )
            )

    for record in edge_records:
        if _normalize_text(record.get("record_type")) != "claim_relation":
            continue
        record_id = _normalize_text(record.get("record_id"))
        relation_type = _relation_type_from_record(record) or "relation"
        observed_at = _relation_observed_at_from_record(record)
        summary = _relation_text_from_record(record) or (
            f"{relation_type} between "
            f"{_normalize_text(record.get('source_record_id'))} and {_normalize_text(record.get('target_record_id'))}."
        )
        add_event(
            TimelineEvent(
                event_id=f"{record_id}:observed",
                event_type=f"relation:{relation_type}",
                title=relation_type,
                summary=summary,
                observed_at=observed_at,
                record_id=record_id,
            )
        )
        properties = dict(record.get("properties") or {})
        inactive_at = _first_text(properties.get("deactivated_at"), properties.get("valid_to"))
        if inactive_at and not _relation_record_is_active(record):
            add_event(
                TimelineEvent(
                    event_id=f"{record_id}:inactive",
                    event_type="relation:inactive",
                    title=relation_type,
                    summary="Relation became inactive in provenance history.",
                    observed_at=inactive_at,
                    record_id=record_id,
                )
            )

    events.sort(
        key=lambda event: (
            event["observed_at"] is None,
            event["observed_at"] or "",
            event["event_id"],
        )
    )
    return events[:limit]


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = _normalize_text(value).lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    return default


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


async def _invoke_semantic_search_async(
    *,
    semantic_search: SemanticSearchFn | None,
    query: str,
    repo_id: str,
    limit: int,
    active_only: bool,
    profile_kinds: tuple[str, ...],
    llm_enabled: bool,
    query_texts: tuple[str, ...] | None,
) -> tuple[dict[str, Any], ...]:
    if semantic_search is None:
        return ()
    try:
        hits = await asyncio.to_thread(
            semantic_search,
            query=query,
            repo_id=repo_id,
            limit=limit,
            active_only=active_only,
            profile_kinds=profile_kinds,
            llm_enabled=llm_enabled,
            query_texts=query_texts,
        )
    except Exception:
        return ()
    return tuple(hit for hit in hits if isinstance(hit, Mapping))


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    raise RuntimeError(
        "provenance_tools._run_async was called while an event loop was already running; "
        "callers should await backend operations before invoking synchronous helpers"
    )
