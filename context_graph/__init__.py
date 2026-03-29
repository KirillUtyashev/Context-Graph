"""Top-level package exports for context-graph.

The package now uses lazy attribute loading so additive subsystems can be
imported without eagerly pulling in legacy modules and optional dependencies.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, tuple[str, str]] = {
    "AuroraMetadataStore": (".metadata", "AuroraMetadataStore"),
    "AuroraProfileVectorStore": (".profile_vectors", "AuroraProfileVectorStore"),
    "BaseCypherExecutor": (".legacy_graph_backend", "BaseCypherExecutor"),
    "CanonicalEdge": (".models", "CanonicalEdge"),
    "CanonicalNode": (".models", "CanonicalNode"),
    "CodebaseMemorySnapshotProducer": (".ingestion", "CodebaseMemorySnapshotProducer"),
    "CommitIndexResult": (".ingestion", "CommitIndexResult"),
    "CommitInfo": (".ingestion.git.git_metadata", "CommitInfo"),
    "CypherStatement": (".legacy_graph_backend", "CypherStatement"),
    "DeterministicHostedIndexResult": (".ingestion", "DeterministicHostedIndexResult"),
    "EdgeChange": (".models", "EdgeChange"),
    "EmbeddingProvider": (".embeddings", "EmbeddingProvider"),
    "EmbeddingSettings": (".embeddings", "EmbeddingSettings"),
    "EntityEdge": (".episodes", "EntityEdge"),
    "EntityNode": (".episodes", "EntityNode"),
    "EpisodeBatchResult": (".episode_processor", "EpisodeBatchResult"),
    "EpisodeProcessor": (".episode_processor", "EpisodeProcessor"),
    "EpisodeWorkItem": (".episode_processor", "EpisodeWorkItem"),
    "EpisodicNode": (".episodes", "EpisodicNode"),
    "GitHubApiClient": (".ingestion.git.github_client", "GitHubApiClient"),
    "GitHubIndexer": (".ingestion.git.github_indexer", "GitHubIndexer"),
    "GitHubRepoRef": (".ingestion.git.github_client", "GitHubRepoRef"),
    "GitHubRepoResolver": (".ingestion.git.github_client", "GitHubRepoResolver"),
    "GitHubSyncResult": (".ingestion.git.github_indexer", "GitHubSyncResult"),
    "GitMetadata": (".ingestion.git.git_metadata", "GitMetadata"),
    "GraphReadRepository": (".graph_read", "GraphReadRepository"),
    "GraphSnapshot": (".models", "GraphSnapshot"),
    "GraphitiNeo4jExecutor": (".legacy_graph_backend", "GraphitiNeo4jExecutor"),
    "GraphitiNeptuneExecutor": (".legacy_graph_backend", "GraphitiNeptuneExecutor"),
    "HostedIndexRequest": (".ingestion", "HostedIndexRequest"),
    "InMemoryMetadataStore": (".metadata", "InMemoryMetadataStore"),
    "InMemoryProfileVectorStore": (".profile_vectors", "InMemoryProfileVectorStore"),
    "IngestionDriver": (".ingestion", "IngestionDriver"),
    "MentionRecord": (".episodes", "MentionRecord"),
    "Neo4jGraphSink": (".legacy_graph_backend", "Neo4jGraphSink"),
    "NeptuneGraphSink": (".legacy_graph_backend", "NeptuneGraphSink"),
    "NodeChange": (".models", "NodeChange"),
    "ProfileDeactivation": (".profiles", "ProfileDeactivation"),
    "ProfileRecord": (".profiles", "ProfileRecord"),
    "ProfileSearchResult": (".profile_vectors", "ProfileSearchResult"),
    "ProfileSearchService": (".profile_vectors", "ProfileSearchService"),
    "ProfileVectorHit": (".profile_vectors", "ProfileVectorHit"),
    "ProfileVectorIndexer": (".profile_vectors", "ProfileVectorIndexer"),
    "QueryAnchor": (".query_parser", "QueryAnchor"),
    "QueryParseResult": (".query_parser", "QueryParseResult"),
    "QueryParser": (".query_parser", "QueryParser"),
    "RepoCacheManager": (".ingestion", "RepoCacheManager"),
    "RepoIndexState": (".metadata", "RepoIndexState"),
    "RepoSourceState": (".metadata", "RepoSourceState"),
    "SEMANTIC_CLAIM_PROFILE_KIND": (".profiles", "SEMANTIC_CLAIM_PROFILE_KIND"),
    "SEMANTIC_FACT_PROFILE_KIND": (".profiles", "SEMANTIC_FACT_PROFILE_KIND"),
    "SEMANTIC_OBSERVATION_PROFILE_KIND": (".profiles", "SEMANTIC_OBSERVATION_PROFILE_KIND"),
    "SemanticProvenanceSearchService": (".profile_vectors", "SemanticProvenanceSearchService"),
    "SQLiteMetadataStore": (".metadata", "SQLiteMetadataStore"),
    "SnapshotApplyResult": (".models", "SnapshotApplyResult"),
    "SnapshotDiff": (".models", "SnapshotDiff"),
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
