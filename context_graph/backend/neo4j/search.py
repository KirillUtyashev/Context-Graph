"""Neo4j lexical search implementation for the additive backend."""

from __future__ import annotations

from typing import Any

from context_graph.backend.errors import (
    BackendFeatureNotImplementedError,
)
from context_graph.backend.schema import (
    EDGE_SEARCH_DOCUMENT_LABEL,
    GRAPH_GROUP_ID_PROPERTY,
    GRAPH_RECORD_ID_PROPERTY,
    GRAPH_RECORD_TYPE_PROPERTY,
    GRAPH_REPO_ID_PROPERTY,
    NEO4J_EDGE_SEARCH_INDEX,
    NEO4J_NODE_SEARCH_INDEX,
    NODE_SEARCH_DOCUMENT_LABEL,
)
from context_graph.backend.search import SearchOperations, validate_extra_filters
from context_graph.backend.types import (
    GraphRecordRef,
    RecordKind,
    SearchHit,
    SearchMode,
    SearchQuery,
)


class Neo4jSearchOperations(SearchOperations):
    """Execute lexical search via dedicated Neo4j full-text search documents."""

    def __init__(self, driver) -> None:
        self._driver = driver

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        """Search Neo4j full-text indices for node or edge search documents."""

        if query.mode is not SearchMode.LEXICAL:
            raise BackendFeatureNotImplementedError(
                f"{query.mode.value} search is not implemented for Neo4j in V1"
            )

        validate_extra_filters(query.filters)
        kinds = query.record_kinds or (RecordKind.NODE, RecordKind.EDGE)

        hits: list[SearchHit] = []
        for kind in kinds:
            index_name = (
                NEO4J_NODE_SEARCH_INDEX if kind is RecordKind.NODE else NEO4J_EDGE_SEARCH_INDEX
            )
            label = (
                NODE_SEARCH_DOCUMENT_LABEL
                if kind is RecordKind.NODE
                else EDGE_SEARCH_DOCUMENT_LABEL
            )
            cypher, params = _build_lexical_search_query(
                index_name=index_name,
                label=label,
                search_query=query,
            )
            records = await self._driver.execute_query(cypher, **params)
            hits.extend(_records_to_hits(kind=kind, records=records))

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: query.limit]

    async def fetch(self, refs: list[GraphRecordRef]) -> list[dict[str, Any]]:
        """Hydrate raw Neo4j node and edge records by `record_id`."""

        if not refs:
            return []

        node_refs = [ref for ref in refs if ref.record_kind is RecordKind.NODE]
        edge_refs = [ref for ref in refs if ref.record_kind is RecordKind.EDGE]

        results: dict[tuple[RecordKind, str], dict[str, Any]] = {}

        if node_refs:
            node_records = await self._driver.execute_query(_NODE_FETCH_QUERY, refs=_ref_params(node_refs))
            for record in node_records:
                results[(RecordKind.NODE, record["record_id"])] = record

        if edge_refs:
            edge_records = await self._driver.execute_query(_EDGE_FETCH_QUERY, refs=_ref_params(edge_refs))
            for record in edge_records:
                results[(RecordKind.EDGE, record["record_id"])] = record

        ordered: list[dict[str, Any]] = []
        for ref in refs:
            record = results.get((ref.record_kind, ref.record_id))
            if record is not None:
                ordered.append(record)
        return ordered


def _build_lexical_search_query(*, index_name: str, label: str, search_query: SearchQuery) -> tuple[str, dict[str, Any]]:
    """Build the Neo4j full-text query for one search-document kind."""

    where_clauses = [f"doc:{label}"]
    params: dict[str, Any] = {
        "search_text": search_query.text,
        "search_limit": search_query.limit,
    }

    _append_list_filter(
        where_clauses,
        params,
        field=GRAPH_RECORD_TYPE_PROPERTY,
        param_name="record_types",
        values=search_query.record_types,
    )
    _append_list_filter(
        where_clauses,
        params,
        field=GRAPH_REPO_ID_PROPERTY,
        param_name="repo_ids",
        values=search_query.repo_ids,
    )
    _append_list_filter(
        where_clauses,
        params,
        field=GRAPH_GROUP_ID_PROPERTY,
        param_name="group_ids",
        values=search_query.group_ids,
    )
    _append_extra_filters(where_clauses, params, search_query.filters)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    cypher = f"""
CALL db.index.fulltext.queryNodes("{index_name}", $search_text, {{limit: $search_limit}})
YIELD node AS doc, score
{where_sql}
RETURN
    doc.record_id AS record_id,
    doc.record_type AS record_type,
    doc.repo_id AS repo_id,
    doc.group_id AS group_id,
    score
ORDER BY score DESC
LIMIT $search_limit
"""
    return cypher.strip(), params


def _append_list_filter(
    where_clauses: list[str],
    params: dict[str, Any],
    *,
    field: str,
    param_name: str,
    values: tuple[str, ...] | None,
) -> None:
    if not values:
        return
    where_clauses.append(f"doc.{field} IN ${param_name}")
    params[param_name] = list(values)


def _append_extra_filters(
    where_clauses: list[str],
    params: dict[str, Any],
    extra_filters: dict[str, object],
) -> None:
    for key, value in extra_filters.items():
        param_name = f"filter_{key}"
        if isinstance(value, (list, tuple, set)):
            where_clauses.append(f"doc.{key} IN ${param_name}")
            params[param_name] = list(value)
        else:
            where_clauses.append(f"doc.{key} = ${param_name}")
            params[param_name] = value


def _records_to_hits(*, kind: RecordKind, records: list[dict[str, Any]]) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for record in records:
        ref = GraphRecordRef(
            record_kind=kind,
            record_id=record["record_id"],
            record_type=record.get("record_type") or "",
            repo_id=record.get("repo_id"),
            group_id=record.get("group_id"),
        )
        score = float(record["score"])
        hits.append(
            SearchHit(
                ref=ref,
                score=score,
                lexical_score=score,
                metadata={
                    "repo_id": record.get("repo_id"),
                    "group_id": record.get("group_id"),
                    "provider": "neo4j",
                },
            )
        )
    return hits


def _ref_params(refs: list[GraphRecordRef]) -> list[dict[str, str | None]]:
    return [
        {
            "record_id": ref.record_id,
            "record_type": ref.record_type,
        }
        for ref in refs
    ]


_NODE_FETCH_QUERY = f"""
UNWIND $refs AS ref
MATCH (n:BackendRecord)
WHERE n.{GRAPH_RECORD_ID_PROPERTY} = ref.record_id
RETURN
    "{RecordKind.NODE.value}" AS record_kind,
    ref.record_id AS record_id,
    coalesce(n.{GRAPH_RECORD_TYPE_PROPERTY}, ref.record_type) AS record_type,
    n.{GRAPH_REPO_ID_PROPERTY} AS repo_id,
    n.{GRAPH_GROUP_ID_PROPERTY} AS group_id,
    labels(n) AS labels,
    properties(n) AS properties
"""

_EDGE_FETCH_QUERY = f"""
UNWIND $refs AS ref
MATCH (source)-[rel]->(target)
WHERE rel.{GRAPH_RECORD_ID_PROPERTY} = ref.record_id
RETURN
    "{RecordKind.EDGE.value}" AS record_kind,
    ref.record_id AS record_id,
    coalesce(rel.{GRAPH_RECORD_TYPE_PROPERTY}, ref.record_type, type(rel)) AS record_type,
    rel.{GRAPH_REPO_ID_PROPERTY} AS repo_id,
    rel.{GRAPH_GROUP_ID_PROPERTY} AS group_id,
    type(rel) AS relationship_type,
    source.{GRAPH_RECORD_ID_PROPERTY} AS source_record_id,
    target.{GRAPH_RECORD_ID_PROPERTY} AS target_record_id,
    properties(rel) AS properties
"""
