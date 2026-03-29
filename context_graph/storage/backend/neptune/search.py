"""Neptune lexical search implementation backed by AOSS."""

from __future__ import annotations

from typing import Any

from context_graph.storage.backend.errors import (
    BackendFeatureNotImplementedError,
    UnsupportedCapabilityError,
)
from context_graph.storage.backend.schema import (
    GRAPH_GROUP_ID_PROPERTY,
    GRAPH_RECORD_ID_PROPERTY,
    GRAPH_RECORD_TYPE_PROPERTY,
    GRAPH_REPO_ID_PROPERTY,
    NEPTUNE_EDGE_AOSS_INDEX,
    NEPTUNE_NODE_AOSS_INDEX,
)
from context_graph.storage.backend.search import SearchOperations, validate_extra_filters
from context_graph.storage.backend.types import (
    GraphRecordRef,
    RecordKind,
    SearchHit,
    SearchMode,
    SearchQuery,
)


class NeptuneSearchOperations(SearchOperations):
    """Execute lexical search by querying AOSS then hydrating from Neptune."""

    def __init__(self, driver) -> None:
        self._driver = driver

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        """Run lexical search against the AOSS sidecar indices."""

        if query.mode is not SearchMode.LEXICAL:
            raise BackendFeatureNotImplementedError(
                f"{query.mode.value} search is not implemented for Neptune in V1"
            )
        if self._driver.aoss_client is None:
            raise UnsupportedCapabilityError(
                "Neptune lexical search requires an AOSS host in the backend config"
            )

        validate_extra_filters(query.filters)
        kinds = query.record_kinds or (RecordKind.NODE, RecordKind.EDGE)

        hits: list[SearchHit] = []
        for kind in kinds:
            index_name = NEPTUNE_NODE_AOSS_INDEX if kind is RecordKind.NODE else NEPTUNE_EDGE_AOSS_INDEX
            response = await self._driver._run_aoss_query(index_name=index_name, query=query)
            hits.extend(_aoss_response_to_hits(kind=kind, response=response))

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: query.limit]

    async def fetch(self, refs: list[GraphRecordRef]) -> list[dict[str, Any]]:
        """Hydrate raw graph records from Neptune using their `record_id`."""

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


def build_aoss_query(search_query: SearchQuery) -> dict[str, Any]:
    """Translate a shared lexical query into an AOSS bool query."""

    filters: list[dict[str, Any]] = []
    _append_terms_filter(filters, GRAPH_RECORD_TYPE_PROPERTY, search_query.record_types)
    _append_terms_filter(filters, GRAPH_REPO_ID_PROPERTY, search_query.repo_ids)
    _append_terms_filter(filters, GRAPH_GROUP_ID_PROPERTY, search_query.group_ids)

    for key, value in search_query.filters.items():
        if isinstance(value, (list, tuple, set)):
            filters.append({"terms": {key: list(value)}})
        else:
            filters.append({"term": {key: value}})

    query_body: dict[str, Any] = {
        "size": search_query.limit,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": search_query.text,
                            "fields": ["search_text"],
                        }
                    }
                ]
            }
        },
    }
    if filters:
        query_body["query"]["bool"]["filter"] = filters
    return query_body


def _append_terms_filter(
    filters: list[dict[str, Any]],
    field: str,
    values: tuple[str, ...] | None,
) -> None:
    if values:
        filters.append({"terms": {field: list(values)}})


def _aoss_response_to_hits(*, kind: RecordKind, response: dict[str, Any]) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for raw_hit in response.get("hits", {}).get("hits", []):
        source = raw_hit.get("_source", {})
        ref = GraphRecordRef(
            record_kind=kind,
            record_id=source["record_id"],
            record_type=source.get("record_type") or "",
            repo_id=source.get("repo_id"),
            group_id=source.get("group_id"),
        )
        score = float(raw_hit.get("_score", 0.0))
        hits.append(
            SearchHit(
                ref=ref,
                score=score,
                lexical_score=score,
                metadata={"provider": "neptune", "index": raw_hit.get("_index")},
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
MATCH (n)
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
