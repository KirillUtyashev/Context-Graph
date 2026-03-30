"""Factory helpers for backend metadata stores."""

from __future__ import annotations

from .config import InMemoryMetadataConfig, PostgresMetadataConfig, SQLiteMetadataConfig
from .store import InMemoryMetadataStore, MetadataStore, PostgresMetadataStore, SQLiteMetadataStore


def create_metadata_store(
    config: InMemoryMetadataConfig | SQLiteMetadataConfig | PostgresMetadataConfig,
) -> MetadataStore:
    """Create a metadata store from the provided backend config."""

    if isinstance(config, InMemoryMetadataConfig):
        return InMemoryMetadataStore()
    if isinstance(config, SQLiteMetadataConfig):
        return SQLiteMetadataStore(config.path)
    if isinstance(config, PostgresMetadataConfig):
        return PostgresMetadataStore.from_dsn(config.dsn)
    raise TypeError(f"unsupported metadata config: {type(config)!r}")
