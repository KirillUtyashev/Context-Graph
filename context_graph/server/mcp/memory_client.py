from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel

from context_graph.episode_processor import EpisodeProcessor, EpisodeWorkItem
from context_graph.graph_read import GraphReadRepository
from context_graph.graph_sinks import GraphitiNeo4jExecutor, GraphitiNeptuneExecutor, Neo4jGraphSink, NeptuneGraphSink
from context_graph.memory.models.edges import EntityEdge
from context_graph.memory.models.nodes import EpisodeType, EpisodicNode
from context_graph.memory.search.search import search as run_search
from context_graph.memory.search.search_config import SearchConfig, SearchResults
from context_graph.memory.search.search_config_recipes import (
    EDGE_HYBRID_SEARCH_NODE_DISTANCE,
    EDGE_HYBRID_SEARCH_RRF,
)
from context_graph.memory.search.search_filters import SearchFilters
from context_graph.memory.types import GraphitiClients
from context_graph.metadata import InMemoryMetadataStore, MetadataStore
from context_graph.runtime.embedding.client import EMBEDDING_DIM, EmbedderClient
from context_graph.runtime.llm import LLMClient
from context_graph.runtime.llm.config import LLMConfig
from context_graph.runtime.rerank import NoOpCrossEncoder
from context_graph.runtime.tracing import create_tracer
from context_graph.storage.graph.driver import GraphDriver


class DisabledLLMClient(LLMClient):
    def __init__(self) -> None:
        super().__init__(config=LLMConfig())

    async def _generate_response(self, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError("LLM client is not configured")


class DisabledEmbedder(EmbedderClient):
    async def create(self, input_data):  # type: ignore[override]
        return [0.0] * EMBEDDING_DIM

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return [[0.0] * EMBEDDING_DIM for _ in input_data_list]


class ContextGraphMemoryClient:
    """Small runtime client that exposes the Graphiti MCP surface over context_graph."""

    def __init__(
        self,
        *,
        driver: GraphDriver,
        llm_client: LLMClient | None = None,
        embedder: EmbedderClient | None = None,
        metadata_store: MetadataStore | None = None,
        entity_types: dict[str, type[BaseModel]] | None = None,
    ) -> None:
        self.driver = driver
        self.llm_client = llm_client
        self.embedder = embedder
        self.entity_types = entity_types
        self.metadata_store = metadata_store or InMemoryMetadataStore()
        self.metadata_store.bootstrap()
        self.cross_encoder = NoOpCrossEncoder()
        self.clients = GraphitiClients(
            driver=driver,
            llm_client=llm_client or DisabledLLMClient(),
            embedder=embedder or DisabledEmbedder(),
            cross_encoder=self.cross_encoder,
            tracer=create_tracer(),
        )
        if driver.provider.value == "neptune":
            graph_sink = NeptuneGraphSink(
                driver=driver,
                executor=GraphitiNeptuneExecutor(driver=driver, build_indices=False),
            )
        else:
            graph_sink = Neo4jGraphSink(
                driver=driver,
                executor=GraphitiNeo4jExecutor(driver=driver, build_indices=False),
            )
        self.read_repository = GraphReadRepository(
            metadata_store=self.metadata_store,
            graph_sink=graph_sink,
        )
        self.processor = EpisodeProcessor(
            graph_sink=graph_sink,
            read_repository=self.read_repository,
            llm_client=llm_client,
            entity_types=entity_types,
        )

    async def build_indices_and_constraints(self) -> None:
        await self.driver.build_indices_and_constraints()

    async def search(
        self,
        *,
        query: str,
        center_node_uuid: str | None = None,
        group_ids: list[str] | None = None,
        num_results: int = 10,
        search_filter: SearchFilters | None = None,
    ) -> list[EntityEdge]:
        base = (
            EDGE_HYBRID_SEARCH_NODE_DISTANCE
            if center_node_uuid is not None
            else EDGE_HYBRID_SEARCH_RRF
        )
        config = deepcopy(base)
        config.limit = num_results
        results = await run_search(
            self.clients,
            query,
            group_ids,
            config,
            search_filter if search_filter is not None else SearchFilters(),
            center_node_uuid=center_node_uuid,
            driver=self.driver,
        )
        return results.edges

    async def search_(
        self,
        *,
        query: str,
        config: SearchConfig,
        group_ids: list[str] | None = None,
        center_node_uuid: str | None = None,
        bfs_origin_node_uuids: list[str] | None = None,
        search_filter: SearchFilters | None = None,
    ) -> SearchResults:
        return await run_search(
            self.clients,
            query,
            group_ids,
            deepcopy(config),
            search_filter if search_filter is not None else SearchFilters(),
            center_node_uuid=center_node_uuid,
            bfs_origin_node_uuids=bfs_origin_node_uuids,
            driver=self.driver,
        )

    async def add_episode(
        self,
        *,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: datetime,
        source: EpisodeType = EpisodeType.message,
        group_id: str | None = None,
        uuid: str | None = None,
        update_communities: bool = False,
        entity_types: dict[str, type[BaseModel]] | None = None,
        excluded_entity_types: list[str] | None = None,
        previous_episode_uuids: list[str] | None = None,
        edge_types: dict[str, type[BaseModel]] | None = None,
        edge_type_map: dict[tuple[str, str], list[str]] | None = None,
        custom_extraction_instructions: str | None = None,
        saga: str | None = None,
        saga_previous_episode_uuid: str | None = None,
    ) -> dict[str, object]:
        del update_communities, excluded_entity_types, previous_episode_uuids, saga, saga_previous_episode_uuid
        effective_group_id = group_id or self.driver.default_group_id or "default"
        processor = EpisodeProcessor(
            graph_sink=self.processor.graph_sink,
            read_repository=self.read_repository,
            llm_client=self.llm_client,
            entity_types=entity_types or self.entity_types,
            edge_types=edge_types,
            edge_type_map=edge_type_map,
            custom_extraction_instructions=custom_extraction_instructions,
        )
        episode = EpisodicNode(
            uuid=uuid or str(uuid4()),
            name=name,
            group_id=effective_group_id,
            source=source,
            source_description=source_description,
            content=episode_body,
            valid_at=reference_time,
        )
        batch = await asyncio.to_thread(
            processor.apply_batch,
            [EpisodeWorkItem(episode=episode)],
            execute=True,
        )
        result = batch.results[0] if batch.results else None
        return {
            "episode": episode,
            "facts": list(result.facts) if result else [],
            "mentions": list(result.mentions) if result else [],
            "statements_executed": batch.statements_executed,
        }
