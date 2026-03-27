#!/usr/bin/env python3
"""Context Graph MCP server for memory ingestion and search."""

import argparse
import asyncio
from datetime import datetime
import fnmatch
import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from context_graph.server.mcp.config import GraphitiConfig, ServerConfig
from context_graph.server.mcp import code_graph as code_graph_queries
from context_graph.server.mcp.response_types import (
    CodeGraphSchemaResponse,
    CodeSnippetResponse,
    ErrorResponse,
    ProfileResult,
    SearchCodeResponse,
    SearchCodeGraphResponse,
    SearchMemoryResponse,
    SuccessResponse,
    TraceCallPathResponse,
)
from context_graph.embeddings import EmbeddingProvider
from context_graph.models import GraphSnapshot

if TYPE_CHECKING:
    from context_graph.server.mcp.memory_client import ContextGraphMemoryClient
    from context_graph.server.mcp.queue import QueueService

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
2. Search memory for relevant context with the search_memory tool
3. Search indexed repository files with search_code
4. Inspect structural code-graph schema with get_code_graph_schema
5. Search structural code nodes with search_code_graph
6. Trace function call relationships with trace_call_path
7. Read indexed symbol source with get_code_snippet

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
queue_service: Any | None = None
aurora_ingestion_service: Optional['AuroraIngestionService'] = None

# Global client for backward compatibility
graphiti_client: Any | None = None
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
        self._metadata_init_error: str | None = None

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
            if not self.initialize_metadata_store():
                self._init_error = (
                    self._metadata_init_error
                    or 'Metadata storage (Postgres/SQLite) could not be configured'
                )
                return False

            from context_graph.legacy_graph_backend import BaseCypherGraphSink, CypherStatement
            from context_graph.graph_read import GraphReadRepository
            from context_graph.episode_processor import EpisodeProcessor
            from typing import Iterable as _Iterable

            metadata = self._metadata
            if metadata is None:
                self._init_error = 'Metadata storage initialized without a metadata handle'
                return False

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
            from context_graph.runtime.providers.factories import EmbedderFactory, LLMClientFactory

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

    def initialize_metadata_store(self) -> bool:
        """Initialize only the metadata store needed by deterministic MCP tools."""

        if self._metadata is not None:
            return True
        if self._metadata_init_error:
            return False

        try:
            metadata_dsn = self._build_dsn()
            if metadata_dsn is None:
                self._metadata_init_error = 'Metadata storage (Postgres/SQLite) could not be configured'
                return False

            from context_graph.metadata import PostgresMetadataStore, SQLiteMetadataStore

            if metadata_dsn.startswith('sqlite:///'):
                sqlite_path = metadata_dsn.replace('sqlite:///', '')
                metadata = SQLiteMetadataStore(sqlite_path)
            else:
                metadata = PostgresMetadataStore.from_dsn(metadata_dsn)

            metadata.bootstrap()
            self._metadata = metadata
            return True
        except Exception as exc:
            self._metadata_init_error = str(exc)
            logger.error(f'AuroraIngestionService metadata init failed: {exc}')
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
        self.client: Any | None = None
        self.entity_types = None

    async def initialize(self) -> None:
        """Initialize the internal memory client with factory-created components."""
        try:
            from pydantic import BaseModel

            from context_graph.runtime.providers.factories import (
                DatabaseDriverFactory,
                EmbedderFactory,
                LLMClientFactory,
            )
            from context_graph.server.mcp.memory_client import ContextGraphMemoryClient

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
                    from context_graph.storage.graph import NeptuneDriver

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
                    from context_graph.storage.graph import Neo4jDriver

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

    async def get_client(self) -> Any:
        """Get the memory client, initializing if necessary."""
        if self.client is None:
            await self.initialize()
        if self.client is None:
            raise RuntimeError('Failed to initialize memory client')
        return self.client


def _normalize_repo_file_path(root_path: str, file_path: str | None) -> Path | None:
    if not file_path:
        return None
    return (Path(root_path) / file_path).resolve()


async def _get_metadata_snapshot(repo_id: str) -> GraphSnapshot | None:
    global aurora_ingestion_service

    if aurora_ingestion_service is None:
        raise code_graph_queries.CodeGraphQueryError(
            f'No indexed snapshot found for repo_id={repo_id}'
        )

    initializer = getattr(
        aurora_ingestion_service,
        'initialize_metadata_store',
        aurora_ingestion_service.initialize,
    )
    ready = await asyncio.to_thread(initializer)
    if not ready or aurora_ingestion_service._metadata is None:
        metadata_error = getattr(aurora_ingestion_service, '_metadata_init_error', None)
        if metadata_error:
            raise code_graph_queries.CodeGraphQueryError(
                f'Metadata store is unavailable: {metadata_error}'
            )
        raise code_graph_queries.CodeGraphQueryError(
            f'No indexed snapshot found for repo_id={repo_id}'
        )

    return await asyncio.to_thread(
        code_graph_queries.load_latest_snapshot,
        aurora_ingestion_service._metadata,
        repo_id,
    )


async def _ensure_graphiti_runtime_initialized() -> None:
    """Lazily initialize legacy memory runtime pieces only when they are needed."""

    global graphiti_service, queue_service, graphiti_client, semaphore

    if graphiti_service is None:
        raise RuntimeError('Graphiti service not configured')
    if queue_service is None:
        from context_graph.server.mcp.queue import QueueService

        queue_service = QueueService()
    if graphiti_client is not None:
        return

    await graphiti_service.initialize()
    graphiti_client = await graphiti_service.get_client()
    semaphore = graphiti_service.semaphore
    await queue_service.initialize(graphiti_client)


def _iter_file_matches(
    *,
    snapshot: GraphSnapshot,
    query: str,
    file_pattern: str | None,
    limit: int,
    regex: bool,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    matcher = re.compile(query) if regex else None
    seen_files: set[str] = set()
    matches: list[dict[str, Any]] = []

    for node in snapshot.nodes:
        file_path = node.file_path
        if not file_path or file_path in seen_files:
            continue
        seen_files.add(file_path)

        if file_pattern and not fnmatch.fnmatch(file_path, file_pattern):
            continue

        absolute_path = _normalize_repo_file_path(snapshot.root_path, file_path)
        if absolute_path is None or not absolute_path.exists():
            continue

        try:
            lines = absolute_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue

        for index, line in enumerate(lines, start=1):
            is_match = bool(matcher.search(line)) if matcher else query.lower() in line.lower()
            if not is_match:
                continue
            matches.append(
                {
                    'file_path': file_path,
                    'line_number': index,
                    'line_text': line.strip(),
                }
            )
            if len(matches) >= limit:
                return matches

    return matches


@mcp.tool()
async def search_code(
    repo_id: str,
    query: str,
    file_pattern: str | None = None,
    limit: int = 20,
    regex: bool = False,
) -> SearchCodeResponse | ErrorResponse:
    """Search indexed repository files by text, replacing the old graph-first search flow."""

    try:
        if limit <= 0:
            return ErrorResponse(error='limit must be a positive integer')

        snapshot = await _get_metadata_snapshot(repo_id)

        matches = await asyncio.to_thread(
            _iter_file_matches,
            snapshot=snapshot,
            query=query,
            file_pattern=file_pattern,
            limit=limit,
            regex=regex,
        )
        if not matches:
            return SearchCodeResponse(
                message=f"No code matches found for '{query}' in repo '{repo_id}'",
                matches=[],
            )

        return SearchCodeResponse(
            message=f"Found {len(matches)} code match{'es' if len(matches) != 1 else ''} for '{query}' in repo '{repo_id}'",
            matches=matches,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except re.error as exc:
        return ErrorResponse(error=f'Invalid regex: {exc}')
    except Exception as exc:
        logger.error(f'Error in search_code: {exc}')
        return ErrorResponse(error=f'Error in search_code: {exc}')


@mcp.tool()
async def get_code_graph_schema(
    repo_id: str,
) -> CodeGraphSchemaResponse | ErrorResponse:
    """Return structural node kinds, edge types, and relationship patterns for the latest snapshot."""

    try:
        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(code_graph_queries.get_code_graph_schema, snapshot)
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error(f'Error in get_code_graph_schema: {exc}')
        return ErrorResponse(error=f'Error in get_code_graph_schema: {exc}')


@mcp.tool()
async def search_code_graph(
    repo_id: str,
    query: str | None = None,
    node_kinds: list[str] | None = None,
    file_pattern: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> SearchCodeGraphResponse | ErrorResponse:
    """Search structural code nodes in the latest indexed snapshot."""

    try:
        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(
            code_graph_queries.search_code_graph,
            snapshot,
            query=query,
            node_kinds=node_kinds,
            file_pattern=file_pattern,
            limit=limit,
            offset=offset,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error(f'Error in search_code_graph: {exc}')
        return ErrorResponse(error=f'Error in search_code_graph: {exc}')


@mcp.tool()
async def trace_call_path(
    repo_id: str,
    qualified_name: str,
    direction: str = 'both',
    depth: int = 3,
) -> TraceCallPathResponse | ErrorResponse:
    """Trace bounded BFS call relationships from one exact qualified name."""

    try:
        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(
            code_graph_queries.trace_call_path,
            snapshot,
            qualified_name=qualified_name,
            direction=direction,
            depth=depth,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error(f'Error in trace_call_path: {exc}')
        return ErrorResponse(error=f'Error in trace_call_path: {exc}')


@mcp.tool()
async def get_code_snippet(
    repo_id: str,
    qualified_name: str,
    include_neighbors: bool = False,
) -> CodeSnippetResponse | ErrorResponse:
    """Return source code for one exact qualified name in the latest snapshot."""

    try:
        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(
            code_graph_queries.get_code_snippet,
            snapshot,
            qualified_name=qualified_name,
            include_neighbors=include_neighbors,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error(f'Error in get_code_snippet: {exc}')
        return ErrorResponse(error=f'Error in get_code_snippet: {exc}')


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
        from context_graph.memory.extraction.graph_data_operations import clear_data

        await clear_data(client.driver)
        logger.info('All graphs destroyed')

    # Initialize services
    graphiti_service = GraphitiService(config, SEMAPHORE_LIMIT)
    from context_graph.server.mcp.queue import QueueService

    queue_service = QueueService()
    graphiti_client = None
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

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
