"""Hybrid provenance search services and context search entrypoints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from context_graph.models.profiles import (
    SEMANTIC_CLAIM_PROFILE_KIND,
    SEMANTIC_OBSERVATION_PROFILE_KIND,
    SEMANTIC_RELATION_PROFILE_KIND,
)
from context_graph.backend import BackendDriver, GraphRecordRef, RecordKind

from ..schemas.provenance import SearchContextResponse
from .models import (
    HYBRID_CHANNEL_WEIGHTS,
    HYBRID_RRF_K,
    IMPLEMENTED_STATUS,
    LEXICAL_CHANNEL,
    SEARCH_CONTEXT_SOURCES,
    SEMANTIC_CLAIM_CHANNEL,
    SEMANTIC_OBSERVATION_CHANNEL,
    SEMANTIC_RELATION_CHANNEL,
    HybridContextEntry,
    RelationContextEntry,
    SemanticSearchFn,
    _HybridContextAggregate,
    _LexicalNodeCandidate,
)
from .shared import (
    _coerce_bool,
    _fallback_record_from_profile_hit,
    _first_text,
    _invoke_semantic_search_async,
    _matched_lexical_summary,
    _normalize_sources,
    _normalize_text,
    _prefer_record,
    _record_identity,
    _record_is_active,
    _record_observation_id,
    _record_qualified_name,
    _record_summary,
    _record_title,
    _record_types_for_sources,
    _record_file_path,
    _record_inactive_at,
    _record_observed_at,
    _relation_observed_at_from_record,
    _relation_record_inactive_at,
    _relation_record_is_active,
    _relation_text_from_record,
    _relation_type_from_record,
    _run_async,
    _search_records_async,
    _source_for_record_type,
    _truncate,
)


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
