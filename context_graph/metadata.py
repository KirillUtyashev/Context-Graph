"""Compatibility re-exports for metadata storage.

The canonical metadata implementations now live under
`context_graph.storage.backend.metadata`. This module keeps the historical import
path stable for the rest of the codebase while the migration proceeds.
"""

from context_graph.storage.backend.metadata import (
    AuroraMetadataStore,
    InMemoryMetadataConfig,
    InMemoryMetadataStore,
    MetadataStore,
    PostgresMetadataConfig,
    PostgresMetadataStore,
    RepoIndexState,
    RepoSourceState,
    SQLiteMetadataConfig,
    SQLiteMetadataStore,
    create_metadata_store,
)

__all__ = [
    "AuroraMetadataStore",
    "InMemoryMetadataConfig",
    "InMemoryMetadataStore",
    "MetadataStore",
    "PostgresMetadataConfig",
    "PostgresMetadataStore",
    "RepoIndexState",
    "RepoSourceState",
    "SQLiteMetadataConfig",
    "SQLiteMetadataStore",
    "create_metadata_store",
]
