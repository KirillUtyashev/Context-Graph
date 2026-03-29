"""Backend-native runtime helpers for the MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from context_graph.embeddings import EmbeddingProvider
from context_graph.metadata import MetadataStore, PostgresMetadataStore, SQLiteMetadataStore
from context_graph.profile_vectors import (
    AuroraProfileVectorStore,
    InMemoryProfileVectorStore,
    ProfileSearchService,
    ProfileVectorIndexer,
    SemanticProvenanceSearchService,
)
from context_graph.provenance import ClaimResolutionService, ResearchArtifactSynthesisService
from context_graph.query_parser import QueryParser
from context_graph.runtime.providers.factories import (
    DatabaseDriverFactory,
    EmbedderFactory,
    LLMClientFactory,
)
from context_graph.server.mcp.config import MCPServerConfig
from context_graph.server.mcp.queue import ObservationWriteService, QueueService
from context_graph.storage.backend import BackendDriver, create_driver

logger = logging.getLogger(__name__)


class MetadataSnapshotService:
    """Load deterministic snapshots and optional profile search state."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._metadata: MetadataStore | None = None
        self._metadata_init_error: str | None = None
        self._profile_search_service: SemanticProvenanceSearchService | None = None
        self._base_profile_search_service: ProfileSearchService | None = None
        self._profile_search_init_error: str | None = None
        self._profile_vector_store: Any = None
        self._profile_indexer: ProfileVectorIndexer | None = None

    @property
    def metadata_store(self) -> MetadataStore | None:
        return self._metadata

    @property
    def metadata_init_error(self) -> str | None:
        return self._metadata_init_error

    def _build_metadata_dsn(self) -> str | None:
        dsn = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL")
        if dsn:
            return dsn

        endpoint = os.getenv("AURORA_ENDPOINT", "").strip()
        if not endpoint:
            sqlite_path = os.getenv("METADATA_SQLITE_PATH")
            if sqlite_path:
                return f"sqlite:///{sqlite_path}"
            return None

        db_name = os.getenv("AURORA_DB_NAME", "context_graph")
        username = os.getenv("AURORA_USERNAME", "postgres")
        secret_arn = os.getenv("AURORA_PASSWORD_SECRET_ARN", "").strip()
        region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION", "ca-central-1")
        if not secret_arn:
            logger.warning(
                "AURORA_PASSWORD_SECRET_ARN not set and no local metadata DSN found; metadata disabled"
            )
            return None

        try:
            import boto3
            import json as _json
            import urllib.parse

            sm = boto3.client("secretsmanager", region_name=region)
            password = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
            if password.startswith("{"):
                pw_obj = _json.loads(password)
                password = (
                    pw_obj.get("password")
                    or pw_obj.get("master_password")
                    or list(pw_obj.values())[0]
                )
            return (
                f"postgresql://{username}:{urllib.parse.quote_plus(password)}"
                f"@{endpoint}:5432/{db_name}"
            )
        except Exception as exc:
            logger.error("Failed to fetch Aurora password from Secrets Manager: %s", exc)
            return None

    def initialize_metadata_store(self) -> bool:
        """Initialize only the metadata store needed by snapshot-backed tools."""

        if self._metadata is not None:
            return True
        if self._metadata_init_error:
            return False

        try:
            metadata_dsn = self._build_metadata_dsn()
            if metadata_dsn is None:
                self._metadata_init_error = (
                    "Metadata storage (Postgres/SQLite) could not be configured"
                )
                return False

            if metadata_dsn.startswith("sqlite:///"):
                sqlite_path = metadata_dsn.replace("sqlite:///", "")
                metadata = SQLiteMetadataStore(sqlite_path)
            else:
                metadata = PostgresMetadataStore.from_dsn(metadata_dsn)

            metadata.bootstrap()
            self._metadata = metadata
            return True
        except Exception as exc:
            self._metadata_init_error = str(exc)
            logger.error("MetadataSnapshotService metadata init failed: %s", exc)
            return False

    def initialize_profile_search(self) -> bool:
        """Initialize optional semantic profile search services."""

        if self._profile_search_service is not None:
            return True
        if self._profile_search_init_error:
            return False
        if not self.initialize_metadata_store():
            self._profile_search_init_error = (
                self._metadata_init_error
                or "metadata store must be initialized before profile search"
            )
            return False
        if self._metadata is None:
            self._profile_search_init_error = "metadata store initialized without a handle"
            return False

        try:
            llm_client = LLMClientFactory.create(self.config.llm)
            raw_embedder = EmbedderFactory.create(self.config.embedder)
            embedding_provider = EmbeddingProvider(raw_embedder)

            if isinstance(self._metadata, PostgresMetadataStore):
                self._profile_vector_store = AuroraProfileVectorStore.from_metadata_store(
                    self._metadata,
                    embedding_dim=self.config.embedder.dimensions,
                )
            else:
                logger.warning(
                    "Using in-memory profile vector store; semantic profile search will not persist across restarts"
                )
                self._profile_vector_store = InMemoryProfileVectorStore()

            self._profile_vector_store.bootstrap()
            self._profile_indexer = ProfileVectorIndexer(
                vector_store=self._profile_vector_store,
                embedding_provider=embedding_provider,
            )
            base_profile_search_service = ProfileSearchService(
                metadata_store=self._metadata,
                vector_store=self._profile_vector_store,
                embedding_provider=embedding_provider,
                query_parser=QueryParser(llm_client=llm_client),
            )
            self._base_profile_search_service = base_profile_search_service
            self._profile_search_service = SemanticProvenanceSearchService(
                profile_search_service=base_profile_search_service,
            )
            return True
        except Exception as exc:
            self._profile_search_init_error = str(exc)
            logger.warning("MetadataSnapshotService profile search init failed: %s", exc)
            return False

    def search_profiles_sync(
        self,
        *,
        query: str,
        repo_id: str,
        limit: int = 10,
        active_only: bool = True,
        profile_kinds: tuple[str, ...] | None = None,
        llm_enabled: bool = True,
        query_texts: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Search persisted semantic profiles synchronously."""

        if not self.initialize_profile_search():
            logger.warning(
                "Profile search skipped for %s because profile search is unavailable: %s",
                repo_id,
                self._profile_search_init_error,
            )
            return []
        if self._metadata is None:
            return []

        try:
            if isinstance(self._profile_vector_store, InMemoryProfileVectorStore) and self._profile_indexer:
                try:
                    self._profile_indexer.sync_repo(
                        repo_id=repo_id,
                        metadata_store=self._metadata,
                    )
                except Exception as sync_exc:
                    logger.warning("Profile auto-sync failed for %s: %s", repo_id, sync_exc)

            if profile_kinds is None:
                if self._profile_search_service is None:
                    return []
                result = self._profile_search_service.search(
                    repo_id=repo_id,
                    query=query,
                    limit=limit,
                    active_only=active_only,
                    llm_enabled=llm_enabled,
                )
            else:
                if self._base_profile_search_service is None:
                    return []
                result = self._base_profile_search_service.search(
                    repo_id=repo_id,
                    query=query,
                    limit=limit,
                    active_only=active_only,
                    llm_enabled=llm_enabled,
                    profile_kinds=profile_kinds,
                    query_texts=query_texts,
                )
            return [
                {
                    "profile_id": profile.profile_id,
                    "profile_kind": profile.profile_kind,
                    "profile_target_kind": profile.profile_target_kind,
                    "profile_target_id": profile.profile_target_id,
                    "relation_type": profile.relation_type,
                    "value_text": profile.value_text,
                    "source_node_id": profile.source_node_id,
                    "target_node_id": profile.target_node_id,
                    "is_active": bool(profile.is_active),
                    "score": hit.score,
                    "matched_key_text": hit.key_text,
                    "matched_key_origin": hit.key_origin,
                    "metadata": profile.metadata if isinstance(profile.metadata, dict) else {},
                }
                for hit, profile in zip(result.hits, result.profiles)
            ]
        except Exception as exc:
            logger.error("MetadataSnapshotService.search_profiles_sync error: %s", exc)
            return []


class MCPBackendRuntime:
    """Lazy backend runtime used by semantic MCP tools."""

    def __init__(self, config: MCPServerConfig, *, semaphore_limit: int = 10) -> None:
        self.config = config
        self.semaphore = asyncio.Semaphore(semaphore_limit)
        self.queue_service = QueueService()
        self.observation_write_service = ObservationWriteService()
        self.driver: BackendDriver | None = None
        self.llm_client: Any | None = None
        self.embedder_client: Any | None = None
        self.claim_resolution_service: ClaimResolutionService | None = None
        self.research_artifact_synthesis_service: ResearchArtifactSynthesisService | None = None
        self._initialized = False
        self._init_error: str | None = None

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        if self._init_error:
            raise RuntimeError(self._init_error)

        try:
            try:
                self.llm_client = LLMClientFactory.create(self.config.llm)
            except Exception as exc:
                logger.warning("Backend runtime LLM client unavailable: %s", exc)
                self.llm_client = None

            try:
                self.embedder_client = EmbedderFactory.create(self.config.embedder)
            except Exception as exc:
                logger.warning("Backend runtime embedder unavailable: %s", exc)
                self.embedder_client = None

            backend_config = DatabaseDriverFactory.create_backend_config(self.config.database)
            self.driver = create_driver(backend_config)
            await self.driver.build_indices()
            self.claim_resolution_service = ClaimResolutionService(
                llm_client=self.llm_client,
            )
            self.research_artifact_synthesis_service = ResearchArtifactSynthesisService(
                llm_client=self.llm_client,
            )
            self._initialized = True
        except Exception as exc:
            self._init_error = str(exc)
            logger.error("Failed to initialize MCP backend runtime: %s", exc)
            raise

    async def get_driver(self) -> BackendDriver:
        await self.ensure_initialized()
        if self.driver is None:
            raise RuntimeError("backend driver failed to initialize")
        return self.driver
