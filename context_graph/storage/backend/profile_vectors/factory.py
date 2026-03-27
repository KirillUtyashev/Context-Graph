"""Factory helpers for profile-vector backend stores."""

from __future__ import annotations

from context_graph.storage.backend.metadata import MetadataStore, PostgresMetadataStore

from .config import InMemoryProfileVectorConfig, PgVectorProfileVectorConfig
from .store import (
    InMemoryProfileVectorStore,
    PgVectorProfileVectorStore,
    ProfileVectorStore,
)


def create_profile_vector_store(
    config: InMemoryProfileVectorConfig | PgVectorProfileVectorConfig,
    *,
    metadata_store: MetadataStore | None = None,
    connection=None,
) -> ProfileVectorStore:
    """Create a profile-vector store from config and optional backing resources."""

    if isinstance(config, InMemoryProfileVectorConfig):
        return InMemoryProfileVectorStore()
    if isinstance(config, PgVectorProfileVectorConfig):
        if connection is not None:
            return PgVectorProfileVectorStore(connection, embedding_dim=config.embedding_dim)
        if metadata_store is not None:
            return create_profile_vector_store_from_metadata(
                metadata_store,
                embedding_dim=config.embedding_dim,
            )
        raise RuntimeError(
            "PgVectorProfileVectorConfig requires either a metadata_store or direct connection"
        )
    raise TypeError(f"unsupported profile vector config: {type(config)!r}")


def create_profile_vector_store_from_metadata(
    metadata_store: MetadataStore,
    *,
    embedding_dim: int = 1024,
) -> PgVectorProfileVectorStore:
    """Reuse a PostgreSQL metadata store connection for pgvector profile storage."""

    if not isinstance(metadata_store, PostgresMetadataStore):
        raise RuntimeError("PgVectorProfileVectorStore requires a PostgreSQL-compatible metadata store")
    return PgVectorProfileVectorStore(metadata_store.connection, embedding_dim=embedding_dim)
