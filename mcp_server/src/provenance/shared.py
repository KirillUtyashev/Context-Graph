"""Shared helpers for provenance, neighborhood, and code-context queries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, Sequence

from context_graph.models.profiles import (
    SEMANTIC_CLAIM_PROFILE_KIND,
)
from context_graph.query import code_graph as code_graph_queries
from context_graph.backend import (
    BackendDriver,
    GraphRecordRef,
    RecordKind,
    SearchMode,
    SearchQuery,
    TraversalQuery,
)

from ..schemas.code import CodeGraphNodeRef
from ..schemas.provenance import (
    CodeContextSnippet,
    NeighborhoodEdge,
    NeighborhoodNode,
    TimelineEvent,
)
from .models import (
    PROVENANCE_TRAVERSAL_LIMIT,
    SEARCH_CONTEXT_SOURCES,
    SOURCE_RECORD_TYPES,
    SemanticSearchFn,
)


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
        normalized_source = (_normalize_text(source) or "").lower()
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
        "provenance.shared._run_async was called while an event loop was already running; "
        "callers should await backend operations before invoking synchronous helpers"
    )
