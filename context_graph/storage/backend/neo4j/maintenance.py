"""Neo4j maintenance/reset operations for the additive backend."""

from __future__ import annotations

from context_graph.storage.backend.maintenance import (
    MaintenanceOperations,
    RepoResetResult,
    ScopeResetRequest,
    ScopeResetResult,
)
from context_graph.storage.backend.schema import (
    EDGE_SEARCH_DOCUMENT_LABEL,
    GRAPH_RECORD_TYPE_PROPERTY,
    GRAPH_REPO_ID_PROPERTY,
    NODE_SEARCH_DOCUMENT_LABEL,
)


class Neo4jMaintenanceOperations(MaintenanceOperations):
    """Delete repo-scoped graph records and search documents from Neo4j."""

    def __init__(self, driver) -> None:
        self._driver = driver

    async def reset_repo(self, repo_id: str) -> RepoResetResult:
        node_count = await self._count_graph_nodes(repo_id=repo_id, record_types=())
        edge_count = await self._count_edges(repo_id=repo_id, record_types=())
        search_doc_count = await self._count_search_documents(repo_id=repo_id, record_types=())

        await self._delete_search_documents(repo_id=repo_id, record_types=())
        await self._delete_edges(repo_id=repo_id, record_types=())
        await self._delete_graph_nodes(repo_id=repo_id, record_types=())

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
            search_doc_count = await self._count_search_documents(
                repo_id=request.repo_id,
                record_types=search_types,
            )

        if request.delete_search_documents:
            await self._delete_search_documents(repo_id=request.repo_id, record_types=search_types)
        await self._delete_edges(repo_id=request.repo_id, record_types=request.edge_record_types)
        await self._delete_graph_nodes(repo_id=request.repo_id, record_types=request.node_record_types)

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

    async def _count_search_documents(self, *, repo_id: str, record_types: tuple[str, ...]) -> int:
        records = await self._driver.execute_query(
            _SEARCH_DOCUMENT_COUNT_QUERY(record_types=record_types),
            repo_id=repo_id,
            record_types=list(record_types),
        )
        return _count_from_records(records)

    async def _delete_graph_nodes(self, *, repo_id: str, record_types: tuple[str, ...]) -> None:
        await self._driver.execute_query(
            _NODE_DELETE_QUERY(record_types=record_types),
            repo_id=repo_id,
            record_types=list(record_types),
        )

    async def _delete_edges(self, *, repo_id: str, record_types: tuple[str, ...]) -> None:
        await self._driver.execute_query(
            _EDGE_DELETE_QUERY(record_types=record_types),
            repo_id=repo_id,
            record_types=list(record_types),
        )

    async def _delete_search_documents(self, *, repo_id: str, record_types: tuple[str, ...]) -> None:
        await self._driver.execute_query(
            _SEARCH_DOCUMENT_DELETE_QUERY(record_types=record_types),
            repo_id=repo_id,
            record_types=list(record_types),
        )


def _count_from_records(records: list[dict[str, object]]) -> int:
    if not records:
        return 0
    raw = records[0].get("count", 0)
    return int(raw)


def _type_filter(*, alias: str, record_types: tuple[str, ...]) -> str:
    if not record_types:
        return ""
    return f" AND {alias}.{GRAPH_RECORD_TYPE_PROPERTY} IN $record_types"


def _NODE_COUNT_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH (n:BackendRecord) "
        f"WHERE n.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(alias='n', record_types=record_types)} "
        "RETURN count(n) AS count"
    )


def _NODE_DELETE_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH (n:BackendRecord) "
        f"WHERE n.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(alias='n', record_types=record_types)} "
        "DETACH DELETE n"
    )


def _EDGE_COUNT_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH ()-[rel]->() "
        f"WHERE rel.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(alias='rel', record_types=record_types)} "
        "RETURN count(rel) AS count"
    )


def _EDGE_DELETE_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH ()-[rel]->() "
        f"WHERE rel.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(alias='rel', record_types=record_types)} "
        "DELETE rel"
    )


def _SEARCH_DOCUMENT_COUNT_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH (doc) "
        f"WHERE (doc:{NODE_SEARCH_DOCUMENT_LABEL} OR doc:{EDGE_SEARCH_DOCUMENT_LABEL}) "
        f"AND doc.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(alias='doc', record_types=record_types)} "
        "RETURN count(doc) AS count"
    )


def _SEARCH_DOCUMENT_DELETE_QUERY(*, record_types: tuple[str, ...]) -> str:
    return (
        "MATCH (doc) "
        f"WHERE (doc:{NODE_SEARCH_DOCUMENT_LABEL} OR doc:{EDGE_SEARCH_DOCUMENT_LABEL}) "
        f"AND doc.{GRAPH_REPO_ID_PROPERTY} = $repo_id"
        f"{_type_filter(alias='doc', record_types=record_types)} "
        "DETACH DELETE doc"
    )
