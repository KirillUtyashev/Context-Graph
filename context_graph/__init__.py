"""Top-level package exports for context-graph.

The package now uses lazy attribute loading so additive subsystems can be
imported without eagerly pulling in legacy modules and optional dependencies.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, tuple[str, str]] = {
    "AuroraMetadataStore": (".backend.metadata", "AuroraMetadataStore"),
    "AuroraProfileVectorStore": (".backend.profile_vectors", "AuroraProfileVectorStore"),
    "CanonicalEdge": (".models.graph", "CanonicalEdge"),
    "CanonicalNode": (".models.graph", "CanonicalNode"),
    "CodebaseMemorySnapshotProducer": (".ingestion", "CodebaseMemorySnapshotProducer"),
    "CommitIndexResult": (".ingestion", "CommitIndexResult"),
    "CommitInfo": (".ingestion.git.git_metadata", "CommitInfo"),
    "DeterministicHostedIndexResult": (".ingestion", "DeterministicHostedIndexResult"),
    "EdgeChange": (".models.graph", "EdgeChange"),
    "EmbeddingProvider": (".runtime.embeddings", "EmbeddingProvider"),
    "EmbeddingSettings": (".runtime.embeddings", "EmbeddingSettings"),
    "EntityNode": (".models.entity_nodes", "EntityNode"),
    "GitHubApiClient": (".ingestion.git.github_client", "GitHubApiClient"),
    "GitHubIndexer": (".ingestion.git.github_indexer", "GitHubIndexer"),
    "GitHubRepoRef": (".ingestion.git.github_client", "GitHubRepoRef"),
    "GitHubRepoResolver": (".ingestion.git.github_client", "GitHubRepoResolver"),
    "GitHubSyncResult": (".ingestion.git.github_indexer", "GitHubSyncResult"),
    "GitMetadata": (".ingestion.git.git_metadata", "GitMetadata"),
    "GraphSnapshot": (".models.graph", "GraphSnapshot"),
    "HostedIndexRequest": (".ingestion", "HostedIndexRequest"),
    "InMemoryMetadataStore": (".backend.metadata", "InMemoryMetadataStore"),
    "InMemoryProfileVectorStore": (".backend.profile_vectors", "InMemoryProfileVectorStore"),
    "IngestionDriver": (".ingestion", "IngestionDriver"),
    "NodeChange": (".models.graph", "NodeChange"),
    "ProfileDeactivation": (".models.profiles", "ProfileDeactivation"),
    "ProfileRecord": (".models.profiles", "ProfileRecord"),
    "ProfileSearchResult": (".backend.profile_vector_services", "ProfileSearchResult"),
    "ProfileSearchService": (".backend.profile_vector_services", "ProfileSearchService"),
    "ProfileVectorHit": (".backend.profile_vectors", "ProfileVectorHit"),
    "ProfileVectorIndexer": (".backend.profile_vector_services", "ProfileVectorIndexer"),
    "QueryAnchor": (".query.parser", "QueryAnchor"),
    "QueryParseResult": (".query.parser", "QueryParseResult"),
    "QueryParser": (".query.parser", "QueryParser"),
    "RepoCacheManager": (".ingestion", "RepoCacheManager"),
    "RepoIndexState": (".backend.metadata", "RepoIndexState"),
    "RepoSourceState": (".backend.metadata", "RepoSourceState"),
    "SEMANTIC_CLAIM_PROFILE_KIND": (".models.profiles", "SEMANTIC_CLAIM_PROFILE_KIND"),
    "SEMANTIC_FACT_PROFILE_KIND": (".models.profiles", "SEMANTIC_FACT_PROFILE_KIND"),
    "SEMANTIC_OBSERVATION_PROFILE_KIND": (".models.profiles", "SEMANTIC_OBSERVATION_PROFILE_KIND"),
    "SEMANTIC_RELATION_PROFILE_KIND": (".models.profiles", "SEMANTIC_RELATION_PROFILE_KIND"),
    "SemanticProvenanceSearchService": (".backend.profile_vector_services", "SemanticProvenanceSearchService"),
    "SQLiteMetadataStore": (".backend.metadata", "SQLiteMetadataStore"),
    "SnapshotApplyResult": (".models.graph", "SnapshotApplyResult"),
    "SnapshotDiff": (".models.graph", "SnapshotDiff"),
    "TemporalGraphWriter": (".ingestion", "TemporalGraphWriter"),
    "diff_snapshots": (".ingestion", "diff_snapshots"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    """Load top-level exports lazily to avoid eager optional imports."""

    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:  # pragma: no cover - standard module behavior
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(module_name, __name__)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
