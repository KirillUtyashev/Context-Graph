"""Neo4j mutation implementation for typed backend writes."""

from __future__ import annotations

import re
from collections import defaultdict

from context_graph.storage.backend.mutations import (
    EdgeRecord,
    MutationApplyResult,
    MutationOperations,
    MutationPlan,
    NodeRecord,
    SearchDocumentRecord,
)
from context_graph.storage.backend.schema import (
    EDGE_SEARCH_DOCUMENT_LABEL,
    GRAPH_RECORD_ID_PROPERTY,
    NODE_SEARCH_DOCUMENT_LABEL,
)
from context_graph.storage.backend.types import RecordKind

_REL_TYPE_RE = re.compile(r"[^A-Z0-9_]")
_NODE_LABEL_RE = re.compile(r"[^A-Za-z0-9_]")


class Neo4jMutationOperations(MutationOperations):  # pragma: no cover
    """Apply typed node, edge, and search-document upserts to Neo4j."""

    def __init__(self, driver) -> None:
        self._driver = driver

    async def apply(self, plan: MutationPlan) -> MutationApplyResult:  # pragma: no cover
        node_count = 0
        edge_count = 0
        search_doc_count = 0

        for labels, batch in _group_nodes(plan.upsert_nodes).items():
            if not batch:
                continue
            await self._driver.execute_query(
                _node_upsert_query(labels),
                rows=[record.normalized_properties() for record in batch],
            )
            node_count += len(batch)

        for relationship_type, batch in _group_edges(plan.upsert_edges).items():
            if not batch:
                continue
            await self._driver.execute_query(
                _edge_upsert_query(relationship_type),
                rows=[
                    {
                        "source_record_id": record.source_record_id,
                        "target_record_id": record.target_record_id,
                        "properties": record.normalized_properties(),
                    }
                    for record in batch
                ],
            )
            edge_count += len(batch)

        for record_kind, batch in _group_search_documents(plan.upsert_search_documents).items():
            if not batch:
                continue
            await self._driver.execute_query(
                _search_document_upsert_query(record_kind),
                rows=[record.normalized_properties() for record in batch],
            )
            search_doc_count += len(batch)

        return MutationApplyResult(
            nodes_upserted=node_count,
            edges_upserted=edge_count,
            search_documents_upserted=search_doc_count,
        )


def _group_nodes(records: tuple[NodeRecord, ...]) -> dict[tuple[str, ...], list[NodeRecord]]:  # pragma: no cover
    grouped: dict[tuple[str, ...], list[NodeRecord]] = defaultdict(list)
    for record in records:
        labels = tuple(_sanitize_node_label(label) for label in record.labels)
        grouped[labels].append(record)
    return grouped


def _group_edges(records: tuple[EdgeRecord, ...]) -> dict[str, list[EdgeRecord]]:  # pragma: no cover
    grouped: dict[str, list[EdgeRecord]] = defaultdict(list)
    for record in records:
        grouped[_sanitize_relationship_type(record.relationship_type)].append(record)
    return grouped


def _group_search_documents(  # pragma: no cover
    records: tuple[SearchDocumentRecord, ...],
) -> dict[RecordKind, list[SearchDocumentRecord]]:
    grouped: dict[RecordKind, list[SearchDocumentRecord]] = defaultdict(list)
    for record in records:
        grouped[record.record_kind].append(record)
    return grouped


def _node_upsert_query(labels: tuple[str, ...]) -> str:  # pragma: no cover
    label_sql = "".join(f":{label}" for label in labels)
    return (
        "UNWIND $rows AS row "
        f"MERGE (n{label_sql} {{{GRAPH_RECORD_ID_PROPERTY}: row.record_id}}) "
        "SET n += row"
    )


def _edge_upsert_query(relationship_type: str) -> str:  # pragma: no cover
    return (
        "UNWIND $rows AS row "
        f"MATCH (source:BackendRecord {{{GRAPH_RECORD_ID_PROPERTY}: row.source_record_id}}) "
        f"MATCH (target:BackendRecord {{{GRAPH_RECORD_ID_PROPERTY}: row.target_record_id}}) "
        f"MERGE (source)-[rel:{relationship_type} {{{GRAPH_RECORD_ID_PROPERTY}: row.properties.record_id}}]->(target) "
        "SET rel += row.properties"
    )


def _search_document_upsert_query(record_kind: RecordKind) -> str:  # pragma: no cover
    label = (
        NODE_SEARCH_DOCUMENT_LABEL
        if record_kind is RecordKind.NODE
        else EDGE_SEARCH_DOCUMENT_LABEL
    )
    return (
        "UNWIND $rows AS row "
        f"MERGE (doc:{label} {{{GRAPH_RECORD_ID_PROPERTY}: row.record_id}}) "
        "SET doc += row"
    )


def _sanitize_relationship_type(value: str) -> str:  # pragma: no cover
    normalized = _REL_TYPE_RE.sub("_", value.upper()).strip("_")
    if not normalized:
        raise ValueError(f"invalid relationship type: {value!r}")
    if normalized[0].isdigit():
        return f"R_{normalized}"
    return normalized


def _sanitize_node_label(value: str) -> str:  # pragma: no cover
    normalized = _NODE_LABEL_RE.sub("_", value).strip("_")
    if not normalized:
        raise ValueError(f"invalid node label: {value!r}")
    if normalized[0].isdigit():
        return f"N_{normalized}"
    return normalized
