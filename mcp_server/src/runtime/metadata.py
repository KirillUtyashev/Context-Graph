"""Metadata snapshot runtime for deterministic code and profile retrieval."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from context_graph.backend import (
    AuroraProfileVectorStore,
    InMemoryProfileVectorStore,
    MetadataStore,
    PostgresMetadataStore,
    SQLiteMetadataStore,
)

from ..config.settings import MCPServerConfig

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from context_graph.backend.profile_vector_services import (
        ProfileSearchService,
        ProfileVectorIndexer,
        SemanticProvenanceSearchService,
    )


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
    def profile_indexer(self) -> ProfileVectorIndexer | None:
        return self._profile_indexer

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
            from context_graph.backend.profile_vector_services import (
                ProfileSearchService,
                ProfileVectorIndexer,
                SemanticProvenanceSearchService,
            )
            from context_graph.query.parser import QueryParser
            from context_graph.runtime.embeddings import EmbeddingProvider
            from context_graph.runtime.providers.factories import (
                EmbedderFactory,
                LLMClientFactory,
            )

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

        self._rollback_aborted_profile_search_connections()
        try:
            if self._profile_vector_store is not None:
                self._profile_vector_store.bootstrap()

            should_sync_vectors = False
            if self._profile_indexer is not None:
                if isinstance(self._profile_vector_store, InMemoryProfileVectorStore):
                    should_sync_vectors = True
                elif bool(getattr(self._profile_vector_store, "needs_resync", False)):
                    should_sync_vectors = True

            if should_sync_vectors and self._profile_indexer:
                try:
                    self._profile_indexer.sync_repo(
                        repo_id=repo_id,
                        metadata_store=self._metadata,
                    )
                    mark_resynced = getattr(self._profile_vector_store, "mark_resynced", None)
                    if callable(mark_resynced):
                        mark_resynced()
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
            profiles_by_id = {profile.profile_id: profile for profile in result.profiles}
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
                for hit in result.hits
                if (profile := profiles_by_id.get(hit.profile_id)) is not None
            ]
        except Exception as exc:
            self._rollback_profile_search_connections()
            logger.error("MetadataSnapshotService.search_profiles_sync error: %s", exc)
            return []

    def _rollback_profile_search_connections(self) -> None:
        seen: set[int] = set()
        for connection in (
            getattr(self._metadata, "connection", None),
            getattr(self._profile_vector_store, "connection", None),
        ):
            if connection is None:
                continue
            identity = id(connection)
            if identity in seen:
                continue
            seen.add(identity)
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                try:
                    rollback()
                except Exception:
                    continue

    def _rollback_aborted_profile_search_connections(self) -> None:
        seen: set[int] = set()
        for connection in (
            getattr(self._metadata, "connection", None),
            getattr(self._profile_vector_store, "connection", None),
        ):
            if connection is None:
                continue
            identity = id(connection)
            if identity in seen:
                continue
            seen.add(identity)
            if connection_transaction_in_error(connection):
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    try:
                        rollback()
                    except Exception:
                        continue


def connection_transaction_in_error(connection: Any) -> bool:
    """Return whether a psycopg connection currently has an aborted transaction."""

    info = getattr(connection, "info", None)
    status = getattr(info, "transaction_status", None)
    if status is not None:
        status_name = getattr(status, "name", None)
        if isinstance(status_name, str) and status_name.upper() == "INERROR":
            return True
        if str(status).upper().endswith("INERROR"):
            return True

    getter = getattr(connection, "get_transaction_status", None)
    if callable(getter):
        try:
            return int(getter()) == 3
        except Exception:
            return False
    return False
