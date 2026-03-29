"""Neptune backend driver implementation with optional AOSS lexical search."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from context_graph.storage.backend.driver import BackendDriver, BackendSession
from context_graph.storage.backend.errors import BackendConfigurationError
from context_graph.storage.backend.maintenance import MaintenanceOperations
from context_graph.storage.backend.mutations import MutationOperations
from context_graph.storage.backend.schema import NEPTUNE_EDGE_AOSS_INDEX, NEPTUNE_NODE_AOSS_INDEX
from context_graph.storage.backend.types import BackendCapabilities, BackendProvider, SearchQuery

from .config import NeptuneConfig
from .maintenance import NeptuneMaintenanceOperations
from .mutations import NeptuneMutationOperations
from .search import NeptuneSearchOperations, build_aoss_query
from .traversal import NeptuneTraversalOperations

logger = logging.getLogger(__name__)

try:
    import boto3
    from langchain_aws.graphs import NeptuneAnalyticsGraph, NeptuneGraph
    from opensearchpy import OpenSearch, Urllib3AWSV4SignerAuth, Urllib3HttpConnection
except ImportError:  # pragma: no cover - exercised through configuration tests
    boto3 = None
    NeptuneAnalyticsGraph = None
    NeptuneGraph = None
    OpenSearch = None
    Urllib3AWSV4SignerAuth = None
    Urllib3HttpConnection = None


_AOSS_INDEX_DEFINITIONS = {
    NEPTUNE_NODE_AOSS_INDEX: {
        "mappings": {
            "properties": {
                "record_id": {"type": "keyword"},
                "record_type": {"type": "keyword"},
                "repo_id": {"type": "keyword"},
                "group_id": {"type": "keyword"},
                "search_text": {"type": "text"},
            }
        }
    },
    NEPTUNE_EDGE_AOSS_INDEX: {
        "mappings": {
            "properties": {
                "record_id": {"type": "keyword"},
                "record_type": {"type": "keyword"},
                "repo_id": {"type": "keyword"},
                "group_id": {"type": "keyword"},
                "search_text": {"type": "text"},
            }
        }
    },
}


class NeptuneBackendDriver(BackendDriver):
    """Provider-neutral backend driver backed by Neptune and optional AOSS."""

    provider = BackendProvider.NEPTUNE

    def __init__(self, config: NeptuneConfig):
        if not config.host:
            raise BackendConfigurationError("Neptune host must not be empty")
        self.config = config
        self.client = self._create_neptune_client(config)
        self.aoss_client = self._create_aoss_client(config)
        self.capabilities = BackendCapabilities(
            supports_transactions=False,
            supports_lexical_search=self.aoss_client is not None,
            supports_vector_search=False,
            supports_hybrid_search=False,
            supports_graph_traversal=True,
            uses_external_text_index=True,
        )
        self._search = NeptuneSearchOperations(self)
        self._mutations = NeptuneMutationOperations(self)
        self._maintenance = NeptuneMaintenanceOperations(self)
        self._traversal = NeptuneTraversalOperations(self)

    @property
    def search(self) -> NeptuneSearchOperations:
        return self._search

    @property
    def mutations(self) -> MutationOperations:
        return self._mutations

    @property
    def maintenance(self) -> MaintenanceOperations:
        return self._maintenance

    @property
    def traversal(self) -> NeptuneTraversalOperations:
        return self._traversal

    async def execute_query(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Execute one OpenCypher query through Neptune's boto3 client."""

        params = {key: value for key, value in kwargs.items()}
        return await asyncio.to_thread(self._run_query, query, params)

    def session(self) -> BackendSession:
        return NeptuneBackendSession(self)

    async def close(self) -> None:
        close = getattr(self.client.client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    async def build_indices(self, delete_existing: bool = False) -> None:
        """Create or refresh the AOSS sidecar indices used for lexical search."""

        if self.aoss_client is None:
            logger.debug("Skipping AOSS bootstrap because no AOSS client is configured")
            return

        if delete_existing:
            for index_name in _AOSS_INDEX_DEFINITIONS:
                if self.aoss_client.indices.exists(index=index_name):
                    self.aoss_client.indices.delete(index=index_name)

        for index_name, body in _AOSS_INDEX_DEFINITIONS.items():
            if not self.aoss_client.indices.exists(index=index_name):
                self.aoss_client.indices.create(index=index_name, body=body)

    async def health_check(self) -> None:
        """Validate connectivity by executing a trivial OpenCypher query."""

        await self.execute_query("RETURN 1 AS ok")

    def _create_neptune_client(self, config: NeptuneConfig):
        if NeptuneGraph is None or NeptuneAnalyticsGraph is None:
            raise BackendConfigurationError(
                "Neptune dependencies are not installed for the additive backend"
            )

        if config.host.startswith("neptune-db://"):
            endpoint = config.host.replace("neptune-db://", "")
            return NeptuneGraph(endpoint, config.port)
        if config.host.startswith("neptune-graph://"):
            graph_id = config.host.replace("neptune-graph://", "")
            return NeptuneAnalyticsGraph(graph_id)
        raise BackendConfigurationError(
            "Neptune host must start with neptune-db:// or neptune-graph://"
        )

    def _create_aoss_client(self, config: NeptuneConfig):
        if not config.aoss_host:
            return None
        if (
            boto3 is None
            or OpenSearch is None
            or Urllib3AWSV4SignerAuth is None
            or Urllib3HttpConnection is None
        ):
            raise BackendConfigurationError(
                "AOSS dependencies are not installed for the additive backend"
            )

        session = boto3.Session()
        return OpenSearch(
            hosts=[{"host": config.aoss_host, "port": config.aoss_port}],
            http_auth=Urllib3AWSV4SignerAuth(session.get_credentials(), session.region_name, "aoss"),
            use_ssl=True,
            verify_certs=True,
            connection_class=Urllib3HttpConnection,
            pool_maxsize=20,
        )

    def _run_query(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        # Neptune's high-level wrapper ignores parameter dictionaries for openCypher
        # calls, so we pass JSON parameters to the underlying boto3 client directly.
        payload: dict[str, Any] = {"openCypherQuery": query}
        if params:
            payload["parameters"] = json.dumps(params)
        response = self.client.client.execute_open_cypher_query(**payload)
        return response["results"]

    def _run_aoss_query(self, *, index_name: str, query: SearchQuery) -> dict[str, Any]:
        if self.aoss_client is None:
            return {}
        body = build_aoss_query(query)
        # AOSS returns lightweight search-document hits; the real graph record is
        # hydrated later through `fetch()`.
        return self.aoss_client.search(index=index_name, body=body)


class NeptuneBackendSession(BackendSession):
    """Thin session wrapper for Neptune, which lacks native transactions here."""

    provider = BackendProvider.NEPTUNE

    def __init__(self, driver: NeptuneBackendDriver):
        self._driver = driver

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def run(self, query: str, **kwargs: Any) -> Any:
        return await self._driver.execute_query(query, **kwargs)

    async def close(self) -> None:
        return None

    async def execute_write(self, func, *args, **kwargs) -> Any:
        return await func(self, *args, **kwargs)
