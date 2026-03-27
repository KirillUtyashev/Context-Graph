"""Neo4j backend driver implementation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import ClientError

from context_graph.storage.backend.driver import BackendDriver, BackendSession
from context_graph.storage.backend.maintenance import MaintenanceOperations
from context_graph.storage.backend.mutations import MutationOperations
from context_graph.storage.backend.query_executor import Transaction
from context_graph.storage.backend.schema import (
    NEO4J_GRAPH_NODE_RECORD_ID_INDEX,
    NEO4J_EDGE_SEARCH_INDEX,
    NEO4J_NODE_SEARCH_INDEX,
)
from context_graph.storage.backend.types import BackendCapabilities, BackendProvider

from .config import Neo4jConfig
from .maintenance import Neo4jMaintenanceOperations
from .mutations import Neo4jMutationOperations
from .search import Neo4jSearchOperations
from .traversal import Neo4jTraversalOperations

logger = logging.getLogger(__name__)

_INDEX_CREATE_QUERIES = [
    # Dedicated search-document nodes let us keep the backend generic while
    # still using Neo4j's native full-text indexing.
    f"""CREATE FULLTEXT INDEX {NEO4J_NODE_SEARCH_INDEX} IF NOT EXISTS
FOR (doc:NodeSearchDocument) ON EACH [doc.search_text]""",
    f"""CREATE FULLTEXT INDEX {NEO4J_EDGE_SEARCH_INDEX} IF NOT EXISTS
FOR (doc:EdgeSearchDocument) ON EACH [doc.search_text]""",
    f"""CREATE RANGE INDEX {NEO4J_GRAPH_NODE_RECORD_ID_INDEX} IF NOT EXISTS
FOR (n:BackendRecord) ON (n.record_id)""",
]

_INDEX_DROP_QUERIES = [
    f"DROP INDEX {NEO4J_NODE_SEARCH_INDEX} IF EXISTS",
    f"DROP INDEX {NEO4J_EDGE_SEARCH_INDEX} IF EXISTS",
    f"DROP INDEX {NEO4J_GRAPH_NODE_RECORD_ID_INDEX} IF EXISTS",
]


class Neo4jBackendDriver(BackendDriver):
    """Provider-neutral backend driver backed by Neo4j."""

    provider = BackendProvider.NEO4J

    def __init__(self, config: Neo4jConfig):
        self.config = config
        self.client = AsyncGraphDatabase.driver(
            uri=config.uri,
            auth=(config.user or "", config.password or ""),
        )
        self.capabilities = BackendCapabilities(
            supports_transactions=True,
            supports_lexical_search=True,
            supports_vector_search=False,
            supports_hybrid_search=False,
            supports_graph_traversal=True,
            uses_external_text_index=False,
        )
        self._search = Neo4jSearchOperations(self)
        self._mutations = Neo4jMutationOperations(self)
        self._maintenance = Neo4jMaintenanceOperations(self)
        self._traversal = Neo4jTraversalOperations()

    @property
    def search(self) -> Neo4jSearchOperations:
        return self._search

    @property
    def mutations(self) -> MutationOperations:
        return self._mutations

    @property
    def maintenance(self) -> MaintenanceOperations:
        return self._maintenance

    @property
    def traversal(self) -> Neo4jTraversalOperations:
        return self._traversal

    async def execute_query(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Execute one query and normalize Neo4j records into plain dicts."""

        eager_result = await self.client.execute_query(
            query,
            parameters_=kwargs,
            database_=self.config.database,
        )
        return [record.data() for record in eager_result.records]

    def session(self) -> BackendSession:
        return Neo4jBackendSession(self.client.session(database=self.config.database))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """Use Neo4j's native transaction semantics."""

        async with self.client.session(database=self.config.database) as session:
            tx = await session.begin_transaction()
            try:
                yield _Neo4jTransaction(tx)
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise

    async def close(self) -> None:
        await self.client.close()

    async def build_indices(self, delete_existing: bool = False) -> None:
        """Build the full-text indices used by lexical search documents."""

        if delete_existing:
            for query in _INDEX_DROP_QUERIES:
                await self.execute_query(query)

        for query in _INDEX_CREATE_QUERIES:
            try:
                await self.execute_query(query)
            except ClientError as exc:
                # Neo4j can still raise equivalent-schema errors when two IF NOT
                # EXISTS statements race. That is safe to ignore here.
                if "EquivalentSchemaRuleAlreadyExists" not in str(exc):
                    raise
                logger.debug("Ignoring concurrent Neo4j index creation race for query: %s", query)

    async def health_check(self) -> None:
        """Verify that the configured Neo4j database is reachable."""

        await self.client.verify_connectivity()


class Neo4jBackendSession(BackendSession):
    """Adapter that exposes a Neo4j session through the shared backend API."""

    provider = BackendProvider.NEO4J

    def __init__(self, session) -> None:
        self._session = session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def run(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        cursor = await self._session.run(query, kwargs)
        return await cursor.data()

    async def close(self) -> None:
        await self._session.close()

    async def execute_write(self, func, *args, **kwargs) -> Any:
        return await self._session.execute_write(func, *args, **kwargs)


class _Neo4jTransaction(Transaction):
    """Adapter that exposes a Neo4j transaction via the shared contract."""

    def __init__(self, tx) -> None:
        self._tx = tx

    async def run(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        cursor = await self._tx.run(query, kwargs)
        return await cursor.data()
