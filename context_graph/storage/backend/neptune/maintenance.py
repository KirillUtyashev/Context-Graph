"""Neptune maintenance/reset operations for the additive backend."""

from __future__ import annotations

import asyncio

from context_graph.storage.backend.maintenance import (
    MaintenanceOperations,
    RepoResetResult,
    ScopeResetRequest,
    ScopeResetResult,
)
from context_graph.storage.backend.schema import (
    GRAPH_RECORD_TYPE_PROPERTY,
    GRAPH_REPO_ID_PROPERTY,
    NEPTUNE_EDGE_AOSS_INDEX,
    NEPTUNE_NODE_AOSS_INDEX,
)


class NeptuneMaintenanceOperations(MaintenanceOperations):
    """Delete repo-scoped graph records and AOSS search docs from Neptune."""

    def __init__(self, driver) -> None:
        self._driver = driver

    async def reset_repo(self, repo_id: str) -> RepoResetResult:
        node_count = await self._count_graph_nodes(repo_id=repo_id, record_types=())
        edge_count = await self._count_edges(repo_id=repo_id, record_types=())
        search_doc_count = await asyncio.to_thread(self._delete_search_documents, repo_id=repo_id, record_types=())

        await self._driver.execute_query(_EDGE_DELETE_QUERY(record_types=()), repo_id=repo_id, record_types=[])
        await self._driver.execute_query(_NODE_DELETE_QUERY(record_types=()), repo_id=repo_id, record_types=[])

        return RepoResetResult(
            repo_id=repo_id,
            nodes_deleted=node_count,
            edges_deleted=edge_count,
            search_documents_deleted=search_doc_count,
        )

    async def reset_scope(self, request: ScopeResetRequest) -> ScopeResetResult:
        search_types = (
            request.search_document_record_types
            if request.search_document_record_types
            else request.node_record_types
        )
        node_count = await self._count_graph_nodes(
            repo_id=request.repo_id,
            record_types=request.node_record_types,
        )
        edge_count = await self._count_edges(
            repo_id=request.repo_id,
            record_types=request.edge_record_types,
        )
        search_doc_count = 0
        if request.delete_search_documents:
            search_doc_count = await asyncio.to_thread(
                self._delete_search_documents,
                repo_id=request.repo_id,
                record_types=search_types,
            )

        await self._driver.execute_query(
            _EDGE_DELETE_QUERY(record_types=request.edge_record_types),
            repo_id=request.repo_id,
            record_types=list(request.edge_record_types),
        )
        await self._driver.execute_query(
            _NODE_DELETE_QUERY(record_types=request.node_record_types),
            repo_id=request.repo_id,
            record_types=list(request.node_record_types),
        )

        return ScopeResetResult(
            repo_id=request.repo_id,
            nodes_deleted=node_count,
            edges_deleted=edge_count,
            search_documents_deleted=search_doc_count,
        )

    async def _count_graph_nodes(self, *, repo_id: str, record_types: tuple[str, ...]) -> int:
        records = await self._driver.execute_query(
            _NODE_COUNT_QUERY(record_types=record_types),
            repo_id=repo_id,
            record_types=list(record_types),
        )
        return _count_from_records(records)

    async def _count_edges(self, *, repo_id: str, record_types: tuple[str, ...]) -> int:
        records = await self._driver.execute_query(
            _EDGE_COUNT_QUERY(record_types=record_types),
            repo_id=repo_id,
            record_types=list(record_types),
        )
        return _count_from_records(records)

    def _delete_search_documents(self, *, repo_id: str, record_types: tuple[str, ...]) -> int:
        if self._driver.aoss_client is None:
            return 0
        from opensearchpy.exceptions import NotFoundError as AossNotFoundError
        query = _build_aoss_delete_query(repo_id=repo_id, record_types=record_types)
        deleted = 0
        for index_name in (NEPTUNE_NODE_AOSS_INDEX, NEPTUNE_EDGE_AOSS_INDEX):
            try:
                response = self._driver.aoss_client.delete_by_query(index=index_name, body=query)
                deleted += int(response.get("deleted", 0))
            except AossNotFoundError:
                pass
        return deleted


def _count_from_records(records: list[dict[str, object]]) -> int:
    if not records:
        return 0
    raw = records[0].get("count", 0)
    return int(raw)


def _type_filter(record_types: tuple[str, ...]) -> str:
    if not record_types:
        return ""
    return f" AND record.record_type IN $record_types"


def _NODE_COUNT_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH (record) "
        f"WHERE record.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(record_types)} "
        "RETURN count(record) AS count"
    )


def _NODE_DELETE_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH (record) "
        f"WHERE record.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(record_types)} "
        "DETACH DELETE record"
    )


def _EDGE_COUNT_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH ()-[record]->() "
        f"WHERE record.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(record_types)} "
        "RETURN count(record) AS count"
    )


def _EDGE_DELETE_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH ()-[record]->() "
        f"WHERE record.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(record_types)} "
        "DELETE record"
    )


def _build_aoss_delete_query(*, repo_id: str, record_types: tuple[str, ...]) -> dict[str, object]:
    filters: list[dict[str, object]] = [{"term": {GRAPH_REPO_ID_PROPERTY: repo_id}}]
    if record_types:
        filters.append({"terms": {GRAPH_RECORD_TYPE_PROPERTY: list(record_types)}})
    return {"query": {"bool": {"filter": filters}}}
