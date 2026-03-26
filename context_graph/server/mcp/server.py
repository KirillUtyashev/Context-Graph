#!/usr/bin/env python3
"""Context Graph MCP server for memory ingestion and search."""

import argparse
import asyncio
from datetime import datetime
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from context_graph.memory.models.edges import EntityEdge
from context_graph.memory.models.nodes import EpisodeType, EpisodicNode
from context_graph.memory.search.search_filters import SearchFilters
from context_graph.memory.extraction.graph_data_operations import clear_data
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.responses import JSONResponse

from context_graph.server.mcp.config import GraphitiConfig, ServerConfig
from context_graph.server.mcp.response_types import (
    EpisodeSearchResponse,
    ErrorResponse,
    FactSearchResponse,
    NodeResult,
    NodeSearchResponse,
    ProfileResult,
    SearchMemoryResponse,
    StatusResponse,
    SuccessResponse,
)
from context_graph.embeddings import EmbeddingProvider
from context_graph.runtime.providers.factories import DatabaseDriverFactory, EmbedderFactory, LLMClientFactory
from context_graph.server.mcp.memory_client import ContextGraphMemoryClient
from context_graph.server.mcp.queue import QueueService
from context_graph.server.mcp.formatting import format_fact_result

# Load .env file from mcp_server directory
mcp_server_dir = Path(__file__).parent.parent
env_file = mcp_server_dir / '.env'
if env_file.exists():
    load_dotenv(env_file)
else:
    # Try current working directory as fallback
    load_dotenv()


# Semaphore limit for concurrent Graphiti operations.
#
# This controls how many episodes can be processed simultaneously. Each episode
# processing involves multiple LLM calls (entity extraction, deduplication, etc.),
# so the actual number of concurrent LLM requests will be higher.
#
# TUNING GUIDELINES:
#
# LLM Provider Rate Limits (requests per minute):
# - OpenAI Tier 1 (free):     3 RPM   -> SEMAPHORE_LIMIT=1-2
# - OpenAI Tier 2:            60 RPM   -> SEMAPHORE_LIMIT=5-8
# - OpenAI Tier 3:           500 RPM   -> SEMAPHORE_LIMIT=10-15
# - OpenAI Tier 4:         5,000 RPM   -> SEMAPHORE_LIMIT=20-50
# - Anthropic (default):     50 RPM   -> SEMAPHORE_LIMIT=5-8
# - Anthropic (high tier): 1,000 RPM   -> SEMAPHORE_LIMIT=15-30
# - Azure OpenAI (varies):  Consult your quota -> adjust accordingly
#
# SYMPTOMS:
# - Too high: 429 rate limit errors, increased costs from parallel processing
# - Too low: Slow throughput, underutilized API quota
#
# MONITORING:
# - Watch logs for rate limit errors (429)
# - Monitor episode processing times
# - Check LLM provider dashboard for actual request rates
#
# DEFAULT: 10 (suitable for OpenAI Tier 3, mid-tier Anthropic)
SEMAPHORE_LIMIT = int(os.getenv('SEMAPHORE_LIMIT', 10))


# Configure structured logging with timestamps
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    stream=sys.stderr,
)

# Configure specific loggers
logging.getLogger('uvicorn').setLevel(logging.INFO)
logging.getLogger('uvicorn.access').setLevel(logging.WARNING)  # Reduce access log noise
logging.getLogger('mcp.server.streamable_http_manager').setLevel(
    logging.WARNING
)  # Reduce MCP noise


# Patch uvicorn's logging config to use our format
def configure_uvicorn_logging():
    """Configure uvicorn loggers to match our format after they're created."""
    for logger_name in ['uvicorn', 'uvicorn.error', 'uvicorn.access']:
        uvicorn_logger = logging.getLogger(logger_name)
        # Remove existing handlers and add our own with proper formatting
        uvicorn_logger.handlers.clear()
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        uvicorn_logger.addHandler(handler)
        uvicorn_logger.propagate = False


logger = logging.getLogger(__name__)

# Create global config instance - will be properly initialized later
config: GraphitiConfig

# MCP server instructions
GRAPHITI_MCP_INSTRUCTIONS = """
Graphiti is a memory service for AI agents built on a knowledge graph. Graphiti performs well
with dynamic data such as user interactions, changing enterprise data, and external information.

Graphiti transforms information into a richly connected knowledge network, allowing you to 
capture relationships between concepts, entities, and information. The system organizes data as episodes 
(content snippets), nodes (entities), and facts (relationships between entities), creating a dynamic, 
queryable memory store that evolves with new information. Graphiti supports multiple data formats, including 
structured JSON data, enabling seamless integration with existing data pipelines and systems.

Facts contain temporal metadata, allowing you to track the time of creation and whether a fact is invalid 
(superseded by new information).

Key capabilities:
1. Add episodes (text, messages, or JSON) to the knowledge graph with the add_memory tool
2. Search for nodes (entities) in the graph using natural language queries with search_nodes
3. Find relevant facts (relationships between entities) with search_facts
4. Retrieve specific entity edges or episodes by UUID
5. Manage the knowledge graph with tools like delete_episode, delete_entity_edge, and clear_graph

The server connects to a database for persistent storage and uses language models for certain operations. 
Each piece of information is organized by group_id, allowing you to maintain separate knowledge domains.

When adding information, provide descriptive names and detailed content to improve search quality. 
When searching, use specific queries and consider filtering by group_id for more relevant results.

For optimal performance, ensure the database is properly configured and accessible, and valid 
API keys are provided for any language model operations.
"""

# MCP server instance
mcp = FastMCP(
    'Graphiti Agent Memory',
    instructions=GRAPHITI_MCP_INSTRUCTIONS,
)

# Global services
graphiti_service: Optional['GraphitiService'] = None
queue_service: QueueService | None = None
aurora_ingestion_service: Optional['AuroraIngestionService'] = None

# Global client for backward compatibility
graphiti_client: ContextGraphMemoryClient | None = None
semaphore: asyncio.Semaphore


class AuroraIngestionService:
    """Drives context_graph EpisodeProcessor to write episodic ingestion into Aurora + Neptune.

    Can be configured for cloud (AWS Neptune + Aurora) or local (Neo4j + SQLite/Postgres).
    
    Reads provider settings from the global GraphitiConfig:
      - LLM: config.llm
      - Embedder: config.embedder
    """

    def __init__(self, config: GraphitiConfig) -> None:
        self.config = config
        self._processor: Any = None  # context_graph.EpisodeProcessor
        self._metadata: Any = None   # context_graph.PostgresMetadataStore
        self._embed_provider: Any = None   # context_graph.embeddings.EmbeddingProvider
        self._profile_vector_store: Any = None  # context_graph.profile_vectors.AuroraProfileVectorStore
        self._profile_indexer: Any = None  # context_graph.profile_vectors.ProfileVectorIndexer
        self._profile_search_service: Any = None  # context_graph.profile_vectors.ProfileSearchService
        self._ready: bool = False
        self._init_error: str | None = None

    def _build_dsn(self) -> str | None:
        # Priority 1: Explicit DSN or DATABASE_URL (for local development)
        dsn = os.getenv('POSTGRES_DSN') or os.getenv('DATABASE_URL')
        if dsn:
            return dsn

        # Priority 2: AWS Aurora with Secrets Manager (production)
        endpoint = os.getenv('AURORA_ENDPOINT', '').strip()
        if not endpoint:
            # Fallback to local SQLite if requested
            sqlite_path = os.getenv('METADATA_SQLITE_PATH')
            if sqlite_path:
                return f'sqlite:///{sqlite_path}'
            return None

        db_name = os.getenv('AURORA_DB_NAME', 'context_graph')
        username = os.getenv('AURORA_USERNAME', 'postgres')
        secret_arn = os.getenv('AURORA_PASSWORD_SECRET_ARN', '').strip()
        region = os.getenv('AWS_DEFAULT_REGION') or os.getenv('AWS_REGION', 'ca-central-1')

        if not secret_arn:
            logger.warning('AURORA_PASSWORD_SECRET_ARN not set and no local DSN found — Aurora ingestion disabled')
            return None

        try:
            import boto3
            sm = boto3.client('secretsmanager', region_name=region)
            password = sm.get_secret_value(SecretId=secret_arn)['SecretString']
            if password.startswith('{'):
                import json as _json
                pw_obj = _json.loads(password)
                password = pw_obj.get('password') or pw_obj.get('master_password') or list(pw_obj.values())[0]
            import urllib.parse
            return (
                f'postgresql://{username}:{urllib.parse.quote_plus(password)}'
                f'@{endpoint}:5432/{db_name}'
            )
        except Exception as exc:
            logger.error(f'Failed to fetch Aurora password from Secrets Manager: {exc}')
            return None

    def initialize(self) -> bool:
        """Initialize synchronously (called from asyncio.to_thread at first use)."""
        if self._ready:
            return True
        if self._init_error:
            return False
        try:
            metadata_dsn = self._build_dsn()
            if metadata_dsn is None:
                self._init_error = 'Metadata storage (Postgres/SQLite) could not be configured'
                return False

            from context_graph.metadata import PostgresMetadataStore, SQLiteMetadataStore
            from context_graph.graph_sinks import BaseCypherGraphSink, CypherStatement
            from context_graph.graph_read import GraphReadRepository
            from context_graph.episode_processor import EpisodeProcessor
            from typing import Iterable as _Iterable

            # Determine metadata store type
            if metadata_dsn.startswith('sqlite:///'):
                sqlite_path = metadata_dsn.replace('sqlite:///', '')
                metadata = SQLiteMetadataStore(sqlite_path)
            else:
                metadata = PostgresMetadataStore.from_dsn(metadata_dsn)
            
            metadata.bootstrap()
            self._metadata = metadata

            class _NullGraphSink(BaseCypherGraphSink):
                """Collects planned Cypher statements without executing them.

                The context_graph EpisodeProcessor plans Neptune Cypher writes. For this
                integration, Neptune writes are already handled by Graphiti's own path;
                here we only need the Aurora-side metadata and fact extraction, so we
                discard the actual Cypher execution.
                """
                def execute(self, statements: '_Iterable[CypherStatement]') -> int:
                    return sum(1 for _ in statements)  # count but don't execute

            graph_sink = _NullGraphSink()

            # Create LLM client using MCP factory (supports OpenAI, Claude, Bedrock, etc.)
            llm_client = LLMClientFactory.create(self.config.llm)

            # Set up profile vector store and search service
            try:
                from context_graph.profile_vectors import (
                    AuroraProfileVectorStore,
                    InMemoryProfileVectorStore,
                    ProfileVectorIndexer,
                    ProfileSearchService,
                )
                from context_graph.query_parser import QueryParser

                # Create Embedder using MCP factory (supports OpenAI, Bedrock, etc.)
                # This ensures we use the same embedding model for both backends.
                raw_embedder = EmbedderFactory.create(self.config.embedder)
                self._embed_provider = EmbeddingProvider(raw_embedder)
                
                # Choose vector store based on metadata type
                if isinstance(metadata, PostgresMetadataStore):
                    self._profile_vector_store = AuroraProfileVectorStore.from_metadata_store(
                        metadata, 
                        embedding_dim=self.config.embedder.dimensions
                    )
                else:
                    logger.warning('Using In-Memory vector store (profiles will not persist across restarts)')
                    self._profile_vector_store = InMemoryProfileVectorStore()
                
                self._profile_vector_store.bootstrap()
                self._profile_indexer = ProfileVectorIndexer(
                    vector_store=self._profile_vector_store,
                    embedding_provider=self._embed_provider,
                )
                self._profile_search_service = ProfileSearchService(
                    metadata_store=metadata,
                    vector_store=self._profile_vector_store,
                    embedding_provider=self._embed_provider,
                    query_parser=QueryParser(llm_client=llm_client),
                )
                logger.info(f'Profile vector store ({type(self._profile_vector_store).__name__}) initialised')
            except Exception as vec_exc:
                logger.warning(f'Profile vector store init failed: {vec_exc}')
                self._profile_indexer = None
                self._profile_search_service = None

            self._processor = EpisodeProcessor(
                graph_sink=graph_sink,
                read_repository=GraphReadRepository(
                    metadata_store=metadata,
                    graph_sink=graph_sink,
                ),
                llm_client=llm_client,
                profile_indexer=self._profile_indexer,
            )
            self._ready = True
            logger.info('AuroraIngestionService initialised successfully')
            return True
        except Exception as exc:
            self._init_error = str(exc)
            logger.error(f'AuroraIngestionService init failed: {exc}')
            return False

    def search_profiles_sync(
        self,
        *,
        query: str,
        repo_id: str,
        limit: int = 10,
        active_only: bool = True,
    ) -> list[dict]:
        """Search Aurora semantic fact profiles synchronously (for asyncio.to_thread)."""
        if not self.initialize():
            logger.warning(
                'Profile search skipped for %s because AuroraIngestionService failed to initialize: %s',
                repo_id,
                self._init_error,
            )
            return []
        if self._profile_search_service is None:
            logger.warning(
                'Profile search skipped for %s because profile search service is unavailable',
                repo_id,
            )
            return []
        try:
            active_profile_count = len(self._metadata.load_profiles(repo_id, active_only=active_only))
            logger.info(
                'Profile search starting for %s with %d active metadata profiles',
                repo_id,
                active_profile_count,
            )

            # For local testing with In-Memory vector store, always resync from the
            # metadata DB before searching. Hosted indexing often runs in a separate
            # process from the MCP server, so a one-time per-repo cache can leave the
            # in-memory vector index permanently stale after new profiles are written.
            from context_graph.profile_vectors import InMemoryProfileVectorStore
            if isinstance(self._profile_vector_store, InMemoryProfileVectorStore) and self._profile_indexer:
                try:
                    logger.info(
                        'Auto-syncing profiles for %s from metadata store into in-memory index...',
                        repo_id,
                    )
                    count = self._profile_indexer.sync_repo(
                        repo_id=repo_id,
                        metadata_store=self._metadata,
                    )
                    logger.info('Synced %s semantic profile keys for %s', count, repo_id)
                except Exception as sync_exc:
                    logger.warning(f'Auto-sync failed for {repo_id}: {sync_exc}')

            result = self._profile_search_service.search(
                repo_id=repo_id,
                query=query,
                limit=limit,
                active_only=active_only,
            )
            logger.info(
                'Profile search for %s produced %d hits and %d profiles',
                repo_id,
                len(result.hits),
                len(result.profiles),
            )
            hit_scores = {h.profile_id: h.score for h in result.hits}
            return [
                {
                    'profile_id': p.profile_id,
                    'relation_type': p.relation_type,
                    'value_text': p.value_text,
                    'source_node_id': p.source_node_id,
                    'target_node_id': p.target_node_id,
                    'is_active': bool(p.is_active),
                    'score': hit_scores.get(p.profile_id, 0.0),
                    'metadata': p.metadata if isinstance(p.metadata, dict) else {},
                }
                for p in result.profiles
            ]
        except Exception as exc:
            logger.error(f'AuroraIngestionService.search_profiles_sync error: {exc}')
            return []

    def ingest_episode_sync(
        self,
        *,
        repo_id: str,
        source_type: str,
        source_id: str,
        text: str,
        author: str | None = None,
        thread_id: str | None = None,
    ) -> dict:
        """Run the EpisodeProcessor synchronously (intended for asyncio.to_thread)."""
        if not self.initialize():
            return {'ok': False, 'error': self._init_error}
        try:
            from context_graph.episode_processor import EpisodeWorkItem
            from context_graph.episodes import make_episode

            now = datetime.utcnow().isoformat() + 'Z'
            batch = self._processor.apply_batch(
                [
                    EpisodeWorkItem(
                        episode=make_episode(
                            episode_id=f'{source_type}:{source_id}',
                            repo_id=repo_id,
                            source_type=source_type,
                            source_id=source_id,
                            author=author,
                            created_at=now,
                            updated_at=now,
                            text=text,
                            metadata={'thread_id': thread_id} if thread_id else {},
                        )
                    )
                ],
                execute=True,
            )
            result = batch.results[0] if batch.results else None
            return {
                'ok': True,
                'statements_executed': batch.statements_executed,
                'facts': [
                    {
                        'fact_id': f.uuid,
                        'relation_type': f.name,
                        'fact': f.fact,
                    }
                    for f in (result.facts if result else [])
                ],
                'mentions': [m.target_node_id for m in (result.mentions if result else [])],
            }
        except Exception as exc:
            logger.error(f'AuroraIngestionService.ingest_episode_sync error: {exc}')
            return {'ok': False, 'error': str(exc)}


class GraphitiService:
    """Context Graph memory service using the unified configuration system."""

    def __init__(self, config: GraphitiConfig, semaphore_limit: int = 10):
        self.config = config
        self.semaphore_limit = semaphore_limit
        self.semaphore = asyncio.Semaphore(semaphore_limit)
        self.client: ContextGraphMemoryClient | None = None
        self.entity_types = None

    async def initialize(self) -> None:
        """Initialize the internal memory client with factory-created components."""
        try:
            llm_client = None
            embedder_client = None

            try:
                llm_client = LLMClientFactory.create(self.config.llm)
            except Exception as e:
                logger.warning(f'Failed to create LLM client: {e}')

            try:
                embedder_client = EmbedderFactory.create(self.config.embedder)
            except Exception as e:
                logger.warning(f'Failed to create embedder client: {e}')

            db_config = DatabaseDriverFactory.create_config(self.config.database)

            custom_types = None
            if self.config.graphiti.entity_types:
                custom_types = {}
                for entity_type in self.config.graphiti.entity_types:
                    entity_model = type(
                        entity_type.name,
                        (BaseModel,),
                        {
                            '__doc__': entity_type.description,
                        },
                    )
                    custom_types[entity_type.name] = entity_model

            self.entity_types = custom_types

            try:
                if self.config.database.provider.lower() == 'neptune':
                    from context_graph.storage.graph.neptune_driver import NeptuneDriver

                    neptune_driver = NeptuneDriver(
                        host=db_config['host'],
                        aoss_host=db_config.get('aoss_host'),
                    )

                    self.client = ContextGraphMemoryClient(
                        driver=neptune_driver,
                        llm_client=llm_client,
                        embedder=embedder_client,
                        entity_types=custom_types,
                    )
                else:
                    from context_graph.storage.graph.neo4j_driver import Neo4jDriver

                    neo4j_driver = Neo4jDriver(
                        uri=db_config['uri'],
                        user=db_config['user'],
                        password=db_config['password'],
                        database=db_config.get('database') or 'neo4j',
                    )
                    self.client = ContextGraphMemoryClient(
                        driver=neo4j_driver,
                        llm_client=llm_client,
                        embedder=embedder_client,
                        entity_types=custom_types,
                    )
            except Exception as db_error:
                error_msg = str(db_error).lower()
                if 'connection refused' in error_msg or 'could not connect' in error_msg:
                    db_provider = self.config.database.provider
                    if db_provider.lower() == 'neo4j':
                        raise RuntimeError(
                            f'\n{"=" * 70}\n'
                            f'Database Connection Error: Neo4j is not running\n'
                            f'{"=" * 70}\n\n'
                            f'Neo4j at {db_config.get("uri", "unknown")} is not accessible.\n\n'
                            f'To start Neo4j:\n'
                            f'  - Using Docker Compose: cd mcp_server && docker compose -f docker/docker-compose-neo4j.yml up\n'
                            f'  - Or install Neo4j Desktop from: https://neo4j.com/download/\n'
                            f'  - Or run Neo4j manually: docker run -p 7474:7474 -p 7687:7687 neo4j:latest\n\n'
                            f'{"=" * 70}\n'
                        ) from db_error
                    else:
                        raise RuntimeError(
                            f'\n{"=" * 70}\n'
                            f'Database Connection Error: {db_provider} is not running\n'
                            f'{"=" * 70}\n\n'
                            f'{db_provider} at {db_config.get("uri", "unknown")} is not accessible.\n\n'
                            f'Please ensure {db_provider} is running and accessible.\n\n'
                            f'{"=" * 70}\n'
                        ) from db_error
                raise

            await self.client.build_indices_and_constraints()

            logger.info('Successfully initialized memory client')

            if llm_client:
                logger.info(
                    f'Using LLM provider: {self.config.llm.provider} / {self.config.llm.model}'
                )
            else:
                logger.info('No LLM client configured - entity extraction will be limited')

            if embedder_client:
                logger.info(f'Using Embedder provider: {self.config.embedder.provider}')
            else:
                logger.info('No Embedder client configured - search will be limited')

            if self.entity_types:
                entity_type_names = list(self.entity_types.keys())
                logger.info(f'Using custom entity types: {", ".join(entity_type_names)}')
            else:
                logger.info('Using default entity types')

            logger.info(f'Using database: {self.config.database.provider}')
            logger.info(f'Using group_id: {self.config.graphiti.group_id}')

        except Exception as e:
            logger.error(f'Failed to initialize memory client: {e}')
            raise

    async def get_client(self) -> ContextGraphMemoryClient:
        """Get the memory client, initializing if necessary."""
        if self.client is None:
            await self.initialize()
        if self.client is None:
            raise RuntimeError('Failed to initialize memory client')
        return self.client


@mcp.tool()
async def add_memory(
    name: str,
    episode_body: str,
    group_id: str | None = None,
    source: str = 'text',
    source_description: str = '',
    uuid: str | None = None,
    repo_id: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    author: str | None = None,
    thread_id: str | None = None,
) -> SuccessResponse | ErrorResponse:
    """Add an episode to memory. This is the primary way to add information to the graph.

    This function returns immediately and processes the episode addition in the background.
    Episodes for the same group_id are processed sequentially to avoid race conditions.

    The episode is written to two backends:
    1. Neptune (Graphiti knowledge graph — entity/fact extraction).
    2. Aurora PostgreSQL (context_graph metadata store — episodic records and semantic
       relation profiles for LightRAG-style retrieval). Aurora ingestion requires
       repo_id, source_type, and source_id.

    Args:
        name (str): Name of the episode
        episode_body (str): The content of the episode to persist to memory. When source='json', this must be a
                           properly escaped JSON string, not a raw Python dictionary. The JSON data will be
                           automatically processed to extract entities and relationships.
        group_id (str, optional): A unique ID for this graph. If not provided, uses the default group_id from CLI
                                 or a generated one.
        source (str, optional): Source type, must be one of:
                               - 'text': For plain text content (default)
                               - 'json': For structured data
                               - 'message': For conversation-style content
        source_description (str, optional): Description of the source
        uuid (str, optional): Optional UUID for the episode
        repo_id (str, optional): Repository/project identifier for Aurora ingestion (e.g. 'context-graph').
                                 Required to enable Aurora write path.
        source_type (str, optional): Episode source type for Aurora (e.g. 'agent_transcript', 'message').
                                     Defaults to the value of `source` if not set.
        source_id (str, optional): Stable identifier for this episode within the source
                                   (e.g. 'transcript-001'). Defaults to `name` if not set.
        author (str, optional): Author login for Aurora metadata.
        thread_id (str, optional): Logical thread identifier for grouping related episodes in Aurora.

    Examples:
        # Adding an agent transcript to both Neptune and Aurora
        add_memory(
            name="Implement auth PR discussion",
            episode_body="Agent: I'll implement JWT auth...",
            source="message",
            repo_id="my-project",
            source_type="agent_transcript",
            source_id="transcript-2026-03-22-001",
            author="kai",
        )
    """
    global graphiti_service, queue_service, aurora_ingestion_service

    if graphiti_service is None or queue_service is None:
        return ErrorResponse(error='Services not initialized')

    try:
        # Use the provided group_id or fall back to the default from config
        effective_group_id = group_id or config.graphiti.group_id

        # Try to parse the source as an EpisodeType enum, with fallback to text
        episode_type = EpisodeType.text  # Default
        if source:
            try:
                episode_type = EpisodeType[source.lower()]
            except (KeyError, AttributeError):
                # If the source doesn't match any enum value, use text as default
                logger.warning(f"Unknown source type '{source}', using 'text' as default")
                episode_type = EpisodeType.text

        # Submit to queue service for async Neptune processing
        await queue_service.add_episode(
            group_id=effective_group_id,
            name=name,
            content=episode_body,
            source_description=source_description,
            episode_type=episode_type,
            entity_types=graphiti_service.entity_types,
            uuid=uuid or None,  # Ensure None is passed if uuid is None
        )

        # Also ingest into Aurora via context_graph EpisodeProcessor in the background.
        # The MCP tool should return quickly after queueing work instead of blocking
        # on the full profile extraction/indexing path.
        aurora_result: dict | None = None
        if aurora_ingestion_service is not None and repo_id:
            effective_source_type = source_type or source or 'text'
            effective_source_id = source_id or name
            
            async def _run_aurora_ingest() -> None:
                try:
                    result = await asyncio.to_thread(
                        aurora_ingestion_service.ingest_episode_sync,
                        repo_id=repo_id,
                        source_type=effective_source_type,
                        source_id=effective_source_id,
                        text=episode_body,
                        author=author,
                        thread_id=thread_id,
                    )
                    if result.get('ok'):
                        logger.info(
                            "Aurora ingestion completed for repo_id=%s source_id=%s with %s statements and %s facts",
                            repo_id,
                            effective_source_id,
                            result.get('statements_executed', 0),
                            len(result.get('facts', [])),
                        )
                    else:
                        logger.warning(
                            "Aurora ingestion skipped for repo_id=%s source_id=%s: %s",
                            repo_id,
                            effective_source_id,
                            result.get('error', 'unknown error'),
                        )
                except Exception as aurora_exc:
                    logger.error(
                        'Aurora ingestion error (non-fatal) for repo_id=%s source_id=%s: %s',
                        repo_id,
                        effective_source_id,
                        aurora_exc,
                    )

            asyncio.create_task(_run_aurora_ingest())
            aurora_result = {'ok': True, 'queued': True}

        msg = f"Episode '{name}' queued for processing in group '{effective_group_id}'"
        if aurora_result is not None:
            if aurora_result.get('queued'):
                msg += '; Aurora: queued for background ingestion'
            elif aurora_result.get('ok'):
                facts_count = len(aurora_result.get('facts', []))
                msg += f'; Aurora: {aurora_result["statements_executed"]} graph writes, {facts_count} facts extracted'
            else:
                msg += f'; Aurora: skipped ({aurora_result.get("error", "unknown error")})'
        elif repo_id is None:
            msg += '; Aurora: skipped (provide repo_id to enable)'

        return SuccessResponse(message=msg)
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error queuing episode: {error_msg}')
        return ErrorResponse(error=f'Error queuing episode: {error_msg}')


@mcp.tool()
async def search_nodes(
    query: str,
    group_ids: list[str] | None = None,
    max_nodes: int = 10,
    entity_types: list[str] | None = None,
) -> NodeSearchResponse | ErrorResponse:
    """Search for nodes in the graph memory.

    Args:
        query: The search query
        group_ids: Optional list of group IDs to filter results
        max_nodes: Maximum number of nodes to return (default: 10)
        entity_types: Optional list of entity type names to filter by
    """
    global graphiti_service

    if graphiti_service is None:
        return ErrorResponse(error='Graphiti service not initialized')

    try:
        client = await graphiti_service.get_client()

        # Use the provided group_ids or fall back to the default from config if none provided
        effective_group_ids = (
            group_ids
            if group_ids is not None
            else [config.graphiti.group_id]
            if config.graphiti.group_id
            else []
        )

        # Create search filters
        search_filters = SearchFilters(
            node_labels=entity_types,
        )

        # Use the search_ method with node search config
        from context_graph.memory.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF

        results = await client.search_(
            query=query,
            config=NODE_HYBRID_SEARCH_RRF,
            group_ids=effective_group_ids,
            search_filter=search_filters,
        )

        # Extract nodes from results
        nodes = results.nodes[:max_nodes] if results.nodes else []

        if not nodes:
            return NodeSearchResponse(message='No relevant nodes found', nodes=[])

        # Format the results
        node_results = []
        for node in nodes:
            # Get attributes and ensure no embeddings are included
            attrs = node.attributes if hasattr(node, 'attributes') else {}
            # Remove any embedding keys that might be in attributes
            attrs = {k: v for k, v in attrs.items() if 'embedding' not in k.lower()}

            node_results.append(
                NodeResult(
                    uuid=node.uuid,
                    name=node.name,
                    labels=node.labels if node.labels else [],
                    created_at=node.created_at.isoformat() if node.created_at else None,
                    summary=node.summary,
                    group_id=node.group_id,
                    attributes=attrs,
                )
            )

        return NodeSearchResponse(message='Nodes retrieved successfully', nodes=node_results)
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error searching nodes: {error_msg}')
        return ErrorResponse(error=f'Error searching nodes: {error_msg}')


@mcp.tool()
async def search_memory_facts(
    query: str,
    group_ids: list[str] | None = None,
    max_facts: int = 10,
    center_node_uuid: str | None = None,
) -> FactSearchResponse | ErrorResponse:
    """Search the graph memory for relevant facts.

    Args:
        query: The search query
        group_ids: Optional list of group IDs to filter results
        max_facts: Maximum number of facts to return (default: 10)
        center_node_uuid: Optional UUID of a node to center the search around
    """
    global graphiti_service

    if graphiti_service is None:
        return ErrorResponse(error='Graphiti service not initialized')

    try:
        # Validate max_facts parameter
        if max_facts <= 0:
            return ErrorResponse(error='max_facts must be a positive integer')

        client = await graphiti_service.get_client()

        # Use the provided group_ids or fall back to the default from config if none provided
        effective_group_ids = (
            group_ids
            if group_ids is not None
            else [config.graphiti.group_id]
            if config.graphiti.group_id
            else []
        )

        relevant_edges = await client.search(
            group_ids=effective_group_ids,
            query=query,
            num_results=max_facts,
            center_node_uuid=center_node_uuid,
        )

        if not relevant_edges:
            return FactSearchResponse(message='No relevant facts found', facts=[])

        facts = [format_fact_result(edge) for edge in relevant_edges]
        return FactSearchResponse(message='Facts retrieved successfully', facts=facts)
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error searching facts: {error_msg}')
        return ErrorResponse(error=f'Error searching facts: {error_msg}')


@mcp.tool()
async def search_memory(
    query: str,
    group_ids: list[str] | None = None,
    repo_id: str | None = None,
    max_facts: int = 10,
    max_profiles: int = 5,
) -> SearchMemoryResponse | ErrorResponse:
    """Search memory for context relevant to a query.

    Combines Neptune (Graphiti structured entity facts) with Aurora PostgreSQL
    (LightRAG-style semantic relation profiles) when repo_id is provided.

    Args:
        query: Natural language search query
        group_ids: Optional list of Graphiti group IDs to filter Neptune results
        repo_id: Repository identifier for Aurora profile search (enables Aurora path)
        max_facts: Maximum number of Neptune facts to return (default: 10)
        max_profiles: Maximum number of Aurora semantic profiles to return (default: 5)
    """
    global graphiti_service, aurora_ingestion_service

    if graphiti_service is None:
        return ErrorResponse(error='Graphiti service not initialized')

    try:
        if max_facts < 0:
            return ErrorResponse(error='max_facts must be a non-negative integer')
        if max_profiles < 0:
            return ErrorResponse(error='max_profiles must be a non-negative integer')

        client = await graphiti_service.get_client()

        effective_group_ids = (
            group_ids
            if group_ids is not None
            else [config.graphiti.group_id]
            if config.graphiti.group_id
            else []
        )

        # Neptune facts (skipped when max_facts=0)
        facts = []
        if max_facts > 0:
            relevant_edges = await client.search(
                group_ids=effective_group_ids,
                query=query,
                num_results=max_facts,
            )
            facts = [format_fact_result(edge) for edge in (relevant_edges or [])]

        # Aurora profiles (when repo_id provided)
        profiles: list[ProfileResult] = []
        if repo_id and aurora_ingestion_service is not None and max_profiles > 0:
            raw_profiles = await asyncio.to_thread(
                aurora_ingestion_service.search_profiles_sync,
                query=query,
                repo_id=repo_id,
                limit=max_profiles,
            )
            profiles = [ProfileResult(**p) for p in raw_profiles]

        n_facts = len(facts)
        n_profiles = len(profiles)
        if n_facts == 0 and n_profiles == 0:
            return SearchMemoryResponse(
                message=f"No results found for: '{query}'",
                facts=[],
                profiles=[],
            )

        parts = []
        if n_facts:
            parts.append(f'{n_facts} fact{"s" if n_facts != 1 else ""}')
        if n_profiles:
            parts.append(f'{n_profiles} profile{"s" if n_profiles != 1 else ""}')
        msg = f"Found {' and '.join(parts)} for: '{query}'"
        if repo_id and aurora_ingestion_service is None:
            msg += ' (Aurora unavailable — Neptune only)'
        elif repo_id is None:
            msg += ' (Neptune only — provide repo_id to include Aurora profiles)'

        return SearchMemoryResponse(message=msg, facts=facts, profiles=profiles)
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error in search_memory: {error_msg}')
        return ErrorResponse(error=f'Error in search_memory: {error_msg}')


@mcp.tool()
async def delete_entity_edge(uuid: str) -> SuccessResponse | ErrorResponse:
    """Delete an entity edge from the graph memory.

    Args:
        uuid: UUID of the entity edge to delete
    """
    global graphiti_service

    if graphiti_service is None:
        return ErrorResponse(error='Graphiti service not initialized')

    try:
        client = await graphiti_service.get_client()

        # Get the entity edge by UUID
        entity_edge = await EntityEdge.get_by_uuid(client.driver, uuid)
        # Delete the edge using its delete method
        await entity_edge.delete(client.driver)
        return SuccessResponse(message=f'Entity edge with UUID {uuid} deleted successfully')
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error deleting entity edge: {error_msg}')
        return ErrorResponse(error=f'Error deleting entity edge: {error_msg}')


@mcp.tool()
async def delete_episode(uuid: str) -> SuccessResponse | ErrorResponse:
    """Delete an episode from the graph memory.

    Args:
        uuid: UUID of the episode to delete
    """
    global graphiti_service

    if graphiti_service is None:
        return ErrorResponse(error='Graphiti service not initialized')

    try:
        client = await graphiti_service.get_client()

        # Get the episodic node by UUID
        episodic_node = await EpisodicNode.get_by_uuid(client.driver, uuid)
        # Delete the node using its delete method
        await episodic_node.delete(client.driver)
        return SuccessResponse(message=f'Episode with UUID {uuid} deleted successfully')
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error deleting episode: {error_msg}')
        return ErrorResponse(error=f'Error deleting episode: {error_msg}')


@mcp.tool()
async def get_entity_edge(uuid: str) -> dict[str, Any] | ErrorResponse:
    """Get an entity edge from the graph memory by its UUID.

    Args:
        uuid: UUID of the entity edge to retrieve
    """
    global graphiti_service

    if graphiti_service is None:
        return ErrorResponse(error='Graphiti service not initialized')

    try:
        client = await graphiti_service.get_client()

        # Get the entity edge directly using the EntityEdge class method
        entity_edge = await EntityEdge.get_by_uuid(client.driver, uuid)

        # Use the format_fact_result function to serialize the edge
        # Return the Python dict directly - MCP will handle serialization
        return format_fact_result(entity_edge)
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error getting entity edge: {error_msg}')
        return ErrorResponse(error=f'Error getting entity edge: {error_msg}')


@mcp.tool()
async def get_episodes(
    group_ids: list[str] | None = None,
    max_episodes: int = 10,
) -> EpisodeSearchResponse | ErrorResponse:
    """Get episodes from the graph memory.

    Args:
        group_ids: Optional list of group IDs to filter results
        max_episodes: Maximum number of episodes to return (default: 10)
    """
    global graphiti_service

    if graphiti_service is None:
        return ErrorResponse(error='Graphiti service not initialized')

    try:
        client = await graphiti_service.get_client()

        # Use the provided group_ids or fall back to the default from config if none provided
        effective_group_ids = (
            group_ids
            if group_ids is not None
            else [config.graphiti.group_id]
            if config.graphiti.group_id
            else []
        )

        # Get episodes from the driver directly
        from context_graph.memory.models.nodes import EpisodicNode

        if effective_group_ids:
            episodes = await EpisodicNode.get_by_group_ids(
                client.driver, effective_group_ids, limit=max_episodes
            )
        else:
            # If no group IDs, we need to use a different approach
            # For now, return empty list when no group IDs specified
            episodes = []

        if not episodes:
            return EpisodeSearchResponse(message='No episodes found', episodes=[])

        # Format the results
        episode_results = []
        for episode in episodes:
            episode_dict = {
                'uuid': episode.uuid,
                'name': episode.name,
                'content': episode.content,
                'created_at': episode.created_at.isoformat() if episode.created_at else None,
                'source': episode.source.value
                if hasattr(episode.source, 'value')
                else str(episode.source),
                'source_description': episode.source_description,
                'group_id': episode.group_id,
            }
            episode_results.append(episode_dict)

        return EpisodeSearchResponse(
            message='Episodes retrieved successfully', episodes=episode_results
        )
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error getting episodes: {error_msg}')
        return ErrorResponse(error=f'Error getting episodes: {error_msg}')


@mcp.tool()
async def clear_graph(group_ids: list[str] | None = None) -> SuccessResponse | ErrorResponse:
    """Clear all data from the graph for specified group IDs.

    Args:
        group_ids: Optional list of group IDs to clear. If not provided, clears the default group.
    """
    global graphiti_service

    if graphiti_service is None:
        return ErrorResponse(error='Graphiti service not initialized')

    try:
        client = await graphiti_service.get_client()

        # Use the provided group_ids or fall back to the default from config if none provided
        effective_group_ids = (
            group_ids or [config.graphiti.group_id] if config.graphiti.group_id else []
        )

        if not effective_group_ids:
            return ErrorResponse(error='No group IDs specified for clearing')

        # Clear data for the specified group IDs
        await clear_data(client.driver, group_ids=effective_group_ids)

        return SuccessResponse(
            message=f'Graph data cleared successfully for group IDs: {", ".join(effective_group_ids)}'
        )
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error clearing graph: {error_msg}')
        return ErrorResponse(error=f'Error clearing graph: {error_msg}')


@mcp.tool()
async def get_status() -> StatusResponse:
    """Get the status of the Graphiti MCP server and database connection."""
    global graphiti_service

    if graphiti_service is None:
        return StatusResponse(status='error', message='Graphiti service not initialized')

    try:
        client = await graphiti_service.get_client()

        # Test database connection with a simple query.
        # NeptuneDriver.execute_query returns a (list, None, None) tuple directly;
        # Neo4j/FalkorDB return async-iterable results via session.run().
        # Using execute_query is safe for both because all drivers implement it.
        await client.driver.execute_query('MATCH (n) RETURN count(n) AS count LIMIT 1')

        # Use the provider from the service's config, not the global
        provider_name = graphiti_service.config.database.provider
        return StatusResponse(
            status='ok',
            message=f'Graphiti MCP server is running and connected to {provider_name} database',
        )
    except Exception as e:
        error_msg = str(e)
        logger.error(f'Error checking database connection: {error_msg}')
        return StatusResponse(
            status='error',
            message=f'Graphiti MCP server is running but database connection failed: {error_msg}',
        )


@mcp.custom_route('/health', methods=['GET'])
async def health_check(request) -> JSONResponse:
    """Health check endpoint for Docker and load balancers."""
    return JSONResponse({'status': 'healthy', 'service': 'graphiti-mcp'})


async def initialize_server() -> ServerConfig:
    """Parse CLI arguments and initialize the Graphiti server configuration."""
    global config, graphiti_service, queue_service, graphiti_client, semaphore

    parser = argparse.ArgumentParser(
        description='Run the Graphiti MCP server with YAML configuration support'
    )

    # Configuration file argument
    # Default is None — the server reads CONFIG_PATH env var (set by systemd unit).
    # Pass --config <path> to override explicitly.
    parser.add_argument(
        '--config',
        type=Path,
        default=None,
        help='Path to YAML configuration file (default: $CONFIG_PATH env var, or config/config.yaml)',
    )

    # Transport arguments
    parser.add_argument(
        '--transport',
        choices=['sse', 'stdio', 'http'],
        help='Transport to use: http (recommended, default), stdio (standard I/O), or sse (deprecated)',
    )
    parser.add_argument(
        '--host',
        help='Host to bind the MCP server to',
    )
    parser.add_argument(
        '--port',
        type=int,
        help='Port to bind the MCP server to',
    )

    # Provider selection arguments
    parser.add_argument(
        '--llm-provider',
        choices=['openai', 'bedrock'],
        help='LLM provider to use',
    )
    parser.add_argument(
        '--embedder-provider',
        choices=['openai', 'bedrock', 'bedrock_titan'],
        help='Embedder provider to use',
    )
    parser.add_argument(
        '--database-provider',
        choices=['neo4j', 'neptune'],
        help='Database provider to use',
    )

    # LLM configuration arguments
    parser.add_argument('--model', help='Model name to use with the LLM client')
    parser.add_argument('--small-model', help='Small model name to use with the LLM client')
    parser.add_argument(
        '--temperature', type=float, help='Temperature setting for the LLM (0.0-2.0)'
    )

    # Embedder configuration arguments
    parser.add_argument('--embedder-model', help='Model name to use with the embedder')

    # Graphiti-specific arguments
    parser.add_argument(
        '--group-id',
        help='Namespace for the graph. If not provided, uses config file or generates random UUID.',
    )
    parser.add_argument(
        '--user-id',
        help='User ID for tracking operations',
    )
    parser.add_argument(
        '--destroy-graph',
        action='store_true',
        help='Destroy all Graphiti graphs on startup',
    )

    args = parser.parse_args()

    # Set config path in environment for the settings to pick up
    if args.config:
        os.environ['CONFIG_PATH'] = str(args.config)

    # Load configuration with environment variables and YAML
    config = GraphitiConfig()

    # Apply CLI overrides
    config.apply_cli_overrides(args)

    # Also apply legacy CLI args for backward compatibility
    if hasattr(args, 'destroy_graph'):
        config.destroy_graph = args.destroy_graph

    # Log configuration details
    logger.info('Using configuration:')
    logger.info(f'  - LLM: {config.llm.provider} / {config.llm.model}')
    logger.info(f'  - Embedder: {config.embedder.provider} / {config.embedder.model}')
    logger.info(f'  - Database: {config.database.provider}')
    logger.info(f'  - Group ID: {config.graphiti.group_id}')
    logger.info(f'  - Transport: {config.server.transport}')

    logger.info('  - Runtime: context_graph internal memory modules')

    # Handle graph destruction if requested
    if hasattr(config, 'destroy_graph') and config.destroy_graph:
        logger.warning('Destroying all memory graphs as requested...')
        temp_service = GraphitiService(config, SEMAPHORE_LIMIT)
        await temp_service.initialize()
        client = await temp_service.get_client()
        await clear_data(client.driver)
        logger.info('All graphs destroyed')

    # Initialize services
    graphiti_service = GraphitiService(config, SEMAPHORE_LIMIT)
    queue_service = QueueService()
    await graphiti_service.initialize()

    # Set global client for backward compatibility
    graphiti_client = await graphiti_service.get_client()
    semaphore = graphiti_service.semaphore

    # Initialize queue service with the client
    await queue_service.initialize(graphiti_client)

    # Initialize Aurora ingestion service (optional — only active when AURORA_ENDPOINT or DATABASE_URL is set)
    global aurora_ingestion_service
    has_aurora_config = (
        os.getenv('AURORA_ENDPOINT', '').strip() 
        or os.getenv('DATABASE_URL', '').strip()
        or os.getenv('POSTGRES_DSN', '').strip()
        or os.getenv('METADATA_SQLITE_PATH', '').strip()
    )
    if has_aurora_config:
        aurora_ingestion_service = AuroraIngestionService(config)
        logger.info('AuroraIngestionService created (will initialise on first use)')
    else:
        aurora_ingestion_service = None
        logger.info('AURORA_ENDPOINT/DATABASE_URL not set — Aurora ingestion path disabled')

    # Set MCP server settings
    if config.server.host:
        mcp.settings.host = config.server.host
        # When binding to 0.0.0.0 (all interfaces), disable the localhost-only DNS
        # rebinding protection that FastMCP enables by default.  Access is already
        # restricted by the EC2 security group, so this is safe.
        if config.server.host == '0.0.0.0':
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            )
    if config.server.port:
        mcp.settings.port = config.server.port

    # Return MCP configuration for transport
    return config.server


async def run_mcp_server():
    """Run the MCP server in the current event loop."""
    # Initialize the server
    mcp_config = await initialize_server()

    # Run the server with configured transport
    logger.info(f'Starting MCP server with transport: {mcp_config.transport}')
    if mcp_config.transport == 'stdio':
        await mcp.run_stdio_async()
    elif mcp_config.transport == 'sse':
        logger.info(
            f'Running MCP server with SSE transport on {mcp.settings.host}:{mcp.settings.port}'
        )
        logger.info(f'Access the server at: http://{mcp.settings.host}:{mcp.settings.port}/sse')
        await mcp.run_sse_async()
    elif mcp_config.transport == 'http':
        # Use localhost for display if binding to 0.0.0.0
        display_host = 'localhost' if mcp.settings.host == '0.0.0.0' else mcp.settings.host
        logger.info(
            f'Running MCP server with streamable HTTP transport on {mcp.settings.host}:{mcp.settings.port}'
        )
        logger.info('=' * 60)
        logger.info('MCP Server Access Information:')
        logger.info(f'  Base URL: http://{display_host}:{mcp.settings.port}/')
        logger.info(f'  MCP Endpoint: http://{display_host}:{mcp.settings.port}/mcp/')
        logger.info('  Transport: HTTP (streamable)')

        # Show FalkorDB Browser UI access if enabled
        if os.environ.get('BROWSER', '1') == '1':
            logger.info(f'  FalkorDB Browser UI: http://{display_host}:3000/')

        logger.info('=' * 60)
        logger.info('For MCP clients, connect to the /mcp/ endpoint above')

        # Configure uvicorn logging to match our format
        configure_uvicorn_logging()

        await mcp.run_streamable_http_async()
    else:
        raise ValueError(
            f'Unsupported transport: {mcp_config.transport}. Use "sse", "stdio", or "http"'
        )


def main():
    """Main function to run the Graphiti MCP server."""
    try:
        # Run everything in a single event loop
        asyncio.run(run_mcp_server())
    except KeyboardInterrupt:
        logger.info('Server shutting down...')
    except Exception as e:
        logger.error(f'Error initializing Graphiti MCP server: {str(e)}')
        raise


if __name__ == '__main__':
    main()
