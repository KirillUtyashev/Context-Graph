"""Neptune traversal implementation for the additive backend."""

from __future__ import annotations

from typing import Any

from context_graph.storage.backend.traversal import TraversalOperations
from context_graph.storage.backend.types import GraphRecordRef, RecordKind, TraversalHit, TraversalQuery


class NeptuneTraversalOperations(TraversalOperations):
    """Provider-neutral graph expansion backed by Neptune OpenCypher."""

    def __init__(self, driver) -> None:
        self._driver = driver

    async def expand(self, query: TraversalQuery) -> list[TraversalHit]:
        params = _traversal_params(query)
        pattern = _path_pattern(query.max_depth, query.direction)
        node_rows = await self._driver.execute_query(_node_query(pattern), **params)
        edge_rows = await self._driver.execute_query(_edge_query(pattern), **params)
        return _rows_to_hits(node_rows=node_rows, edge_rows=edge_rows, limit=query.limit)


def _path_pattern(max_depth: int, direction: str) -> str:
    if direction == "outbound":
        return f"(origin)-[rels*1..{max_depth}]->(neighbor:BackendRecord)"
    if direction == "inbound":
        return f"(origin)<-[rels*1..{max_depth}]-(neighbor:BackendRecord)"
    return f"(origin)-[rels*1..{max_depth}]-(neighbor:BackendRecord)"


def _traversal_params(query: TraversalQuery) -> dict[str, Any]:
    origin_ids = _dedupe_text(ref.record_id for ref in query.origin_refs)
    repo_ids = query.repo_ids or tuple(
        repo_id for repo_id in _dedupe_text(ref.repo_id for ref in query.origin_refs) if repo_id
    )
    group_ids = query.group_ids or tuple(
        group_id for group_id in _dedupe_text(ref.group_id for ref in query.origin_refs) if group_id
    )
    edge_types = tuple(
        normalized
        for normalized in (
            _normalize_text(edge_type).lower()
            for edge_type in (query.edge_types or ())
        )
        if normalized
    )
    return {
        "origin_ids": list(origin_ids),
        "repo_ids": list(repo_ids),
        "group_ids": list(group_ids),
        "edge_types": list(edge_types),
        "repo_filter_disabled": not repo_ids,
        "group_filter_disabled": not group_ids,
        "edge_type_filter_disabled": not edge_types,
    }


def _node_query(pattern: str) -> str:
    return f"""
UNWIND $origin_ids AS origin_id
MATCH (origin:BackendRecord)
WHERE origin.record_id = origin_id
  AND ($repo_filter_disabled OR coalesce(origin.repo_id, '') IN $repo_ids)
  AND ($group_filter_disabled OR coalesce(origin.group_id, '') IN $group_ids)
MATCH path={pattern}
WHERE ($edge_type_filter_disabled OR all(rel IN rels WHERE toLower(type(rel)) IN $edge_types OR toLower(coalesce(rel.record_type, '')) IN $edge_types))
  AND ($repo_filter_disabled OR all(node IN nodes(path) WHERE coalesce(node.repo_id, '') IN $repo_ids))
  AND ($group_filter_disabled OR all(node IN nodes(path) WHERE coalesce(node.group_id, '') IN $group_ids))
  AND ($repo_filter_disabled OR all(rel IN rels WHERE coalesce(rel.repo_id, '') IN $repo_ids))
  AND ($group_filter_disabled OR all(rel IN rels WHERE coalesce(rel.group_id, '') IN $group_ids))
WITH neighbor, min(length(path)) AS distance
WHERE neighbor.record_id IS NOT NULL
  AND NOT neighbor.record_id IN $origin_ids
RETURN
  "{RecordKind.NODE.value}" AS record_kind,
  neighbor.record_id AS record_id,
  coalesce(neighbor.record_type, '') AS record_type,
  neighbor.repo_id AS repo_id,
  neighbor.group_id AS group_id,
  distance AS distance,
  NULL AS relationship_type,
  NULL AS source_record_id,
  NULL AS target_record_id
ORDER BY distance ASC, record_id ASC
""".strip()


def _edge_query(pattern: str) -> str:
    return f"""
UNWIND $origin_ids AS origin_id
MATCH (origin:BackendRecord)
WHERE origin.record_id = origin_id
  AND ($repo_filter_disabled OR coalesce(origin.repo_id, '') IN $repo_ids)
  AND ($group_filter_disabled OR coalesce(origin.group_id, '') IN $group_ids)
MATCH path={pattern}
WHERE ($edge_type_filter_disabled OR all(rel IN rels WHERE toLower(type(rel)) IN $edge_types OR toLower(coalesce(rel.record_type, '')) IN $edge_types))
  AND ($repo_filter_disabled OR all(node IN nodes(path) WHERE coalesce(node.repo_id, '') IN $repo_ids))
  AND ($group_filter_disabled OR all(node IN nodes(path) WHERE coalesce(node.group_id, '') IN $group_ids))
  AND ($repo_filter_disabled OR all(rel IN rels WHERE coalesce(rel.repo_id, '') IN $repo_ids))
  AND ($group_filter_disabled OR all(rel IN rels WHERE coalesce(rel.group_id, '') IN $group_ids))
UNWIND rels AS rel
WITH rel, min(length(path)) AS distance
MATCH (source:BackendRecord)-[rel]->(target:BackendRecord)
WITH rel, source, target, min(distance) AS distance
WHERE rel.record_id IS NOT NULL
RETURN
  "{RecordKind.EDGE.value}" AS record_kind,
  rel.record_id AS record_id,
  coalesce(rel.record_type, type(rel)) AS record_type,
  rel.repo_id AS repo_id,
  rel.group_id AS group_id,
  distance AS distance,
  type(rel) AS relationship_type,
  source.record_id AS source_record_id,
  target.record_id AS target_record_id
ORDER BY distance ASC, record_id ASC
""".strip()


def _rows_to_hits(
    *,
    node_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    limit: int,
) -> list[TraversalHit]:
    hits = [
        *_coerce_hits(node_rows),
        *_coerce_hits(edge_rows),
    ]
    hits.sort(
        key=lambda hit: (
            hit.distance,
            hit.ref.record_kind is RecordKind.EDGE,
            hit.ref.record_id,
        )
    )
    return hits[:limit]


def _coerce_hits(rows: list[dict[str, Any]]) -> list[TraversalHit]:
    hits: list[TraversalHit] = []
    for row in rows:
        record_id = _normalize_text(row.get("record_id"))
        record_kind_value = _normalize_text(row.get("record_kind")).lower()
        if not record_id or record_kind_value not in {RecordKind.NODE.value, RecordKind.EDGE.value}:
            continue
        record_kind = RecordKind(record_kind_value)
        record_type = _normalize_text(row.get("record_type")) or (
            _normalize_text(row.get("relationship_type")) if record_kind is RecordKind.EDGE else ""
        )
        metadata = {
            "record_kind": record_kind.value,
            "record_type": record_type,
            "relationship_type": _normalize_text(row.get("relationship_type")),
            "source_record_id": _normalize_text(row.get("source_record_id")),
            "target_record_id": _normalize_text(row.get("target_record_id")),
        }
        hits.append(
            TraversalHit(
                ref=GraphRecordRef(
                    record_kind=record_kind,
                    record_id=record_id,
                    record_type=record_type,
                    repo_id=_normalize_text(row.get("repo_id")) or None,
                    group_id=_normalize_text(row.get("group_id")) or None,
                ),
                distance=max(1, int(row.get("distance") or 1)),
                path_metadata=metadata,
            )
        )
    return hits


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _dedupe_text(values) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)
