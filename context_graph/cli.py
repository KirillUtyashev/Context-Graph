from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from .runtime.embeddings import load_embedding_settings, make_embedding_provider
from .ingestion import CodebaseMemorySnapshotProducer, HostedIndexRequest, IngestionDriver, RepoCacheManager
from context_graph.ingestion.git.github_client import GitHubApiClient
from .runtime.llm_config import load_episode_llm_settings, make_episode_llm_client
from .backend.profile_vector_services import (
    ProfileSearchService,
    ProfileVectorIndexer,
    SemanticProvenanceSearchService,
)
from .query.parser import QueryParser
from context_graph.backend import (
    AuroraProfileVectorStore,
    BackendSyncBridge,
    EdgeRecord,
    InMemoryProfileVectorStore,
    MetadataStore,
    MutationPlan,
    Neo4jConfig,
    NeptuneConfig,
    NodeRecord,
    PostgresMetadataStore,
    SearchDocumentRecord,
    SQLiteMetadataStore,
    create_driver,
)


_EMBEDDING_UNSET = object()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Context graph git-aware indexing and provenance tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    latest_commit_parser = subparsers.add_parser(
        "apply-latest-commit",
        help="Index the latest git commit for a repository checkout",
    )
    latest_commit_parser.add_argument("repo_path", help="Path to the local repository checkout")
    latest_commit_parser.add_argument("--repo-id", help="Stable repository identifier")
    latest_commit_parser.add_argument(
        "--limit",
        type=int,
        default=1000000,
        help="Reserved for exporter compatibility; currently unused",
    )
    latest_commit_parser.add_argument(
        "--graph-backend",
        choices=("neo4j", "neptune"),
        default="neo4j",
        help="Graph backend syntax to plan against",
    )
    latest_commit_parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the planned graph writes against the configured graph backend",
    )
    latest_commit_parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="Metadata backend used to persist the latest snapshot state",
    )
    latest_commit_parser.add_argument(
        "--metadata-db",
        default=":memory:",
        help="SQLite database path used to persist the latest snapshot metadata",
    )
    latest_commit_parser.add_argument(
        "--metadata-dsn",
        help="PostgreSQL DSN used when --metadata-backend=postgres",
    )
    latest_commit_parser.add_argument(
        "--print-limit",
        type=int,
        default=10,
        help="Maximum number of typed operations to preview per category",
    )
    latest_commit_parser.add_argument(
        "--row-preview-limit",
        type=int,
        default=3,
        help="Maximum number of properties to preview inside each operation payload",
    )
    latest_commit_parser.add_argument(
        "--neo4j-uri",
        help="Neo4j Bolt URI, e.g. bolt://localhost:7687",
    )
    latest_commit_parser.add_argument(
        "--neo4j-user",
        help="Neo4j username",
    )
    latest_commit_parser.add_argument(
        "--neo4j-password",
        help="Neo4j password",
    )
    latest_commit_parser.add_argument(
        "--neo4j-database",
        help="Neo4j database name",
    )
    latest_commit_parser.add_argument(
        "--neptune-host",
        help="Neptune host used for direct Bolt execution",
    )
    latest_commit_parser.add_argument(
        "--neptune-port",
        type=int,
        default=8182,
        help="Neptune Bolt port",
    )
    latest_commit_parser.add_argument(
        "--neptune-host-scheme",
        choices=("neptune-db", "neptune-graph"),
        default="neptune-db",
        help="Neptune host URI scheme",
    )
    latest_commit_parser.add_argument(
        "--aoss-host",
        help="OpenSearch Serverless host for Neptune BM25 search",
    )
    _add_llm_args(latest_commit_parser)
    _add_embed_args(latest_commit_parser)

    commit_parser = subparsers.add_parser(
        "apply-commit",
        help="Index an explicit git commit for a repository checkout",
    )
    commit_parser.add_argument("repo_path", help="Path to the local repository checkout")
    commit_parser.add_argument("--repo-id", help="Stable repository identifier")
    commit_parser.add_argument(
        "--commit",
        required=True,
        help="Git commit SHA (or any rev-parse compatible revision) to index",
    )
    commit_parser.add_argument(
        "--limit",
        type=int,
        default=1000000,
        help="Reserved for exporter compatibility; currently unused",
    )
    commit_parser.add_argument(
        "--graph-backend",
        choices=("neo4j", "neptune"),
        default="neo4j",
        help="Graph backend syntax to plan against",
    )
    commit_parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the planned graph writes against the configured graph backend",
    )
    commit_parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="Metadata backend used to persist the latest snapshot state",
    )
    commit_parser.add_argument(
        "--metadata-db",
        default=":memory:",
        help="SQLite database path used to persist the latest snapshot metadata",
    )
    commit_parser.add_argument(
        "--metadata-dsn",
        help="PostgreSQL DSN used when --metadata-backend=postgres",
    )
    commit_parser.add_argument(
        "--print-limit",
        type=int,
        default=10,
        help="Maximum number of typed operations to preview per category",
    )
    commit_parser.add_argument(
        "--row-preview-limit",
        type=int,
        default=3,
        help="Maximum number of properties to preview inside each operation payload",
    )
    commit_parser.add_argument(
        "--neo4j-uri",
        help="Neo4j Bolt URI, e.g. bolt://localhost:7687",
    )
    commit_parser.add_argument(
        "--neo4j-user",
        help="Neo4j username",
    )
    commit_parser.add_argument(
        "--neo4j-password",
        help="Neo4j password",
    )
    commit_parser.add_argument(
        "--neo4j-database",
        help="Neo4j database name",
    )
    commit_parser.add_argument(
        "--neptune-host",
        help="Neptune host used for direct Bolt execution",
    )
    commit_parser.add_argument(
        "--neptune-port",
        type=int,
        default=8182,
        help="Neptune Bolt port",
    )
    commit_parser.add_argument(
        "--neptune-host-scheme",
        choices=("neptune-db", "neptune-graph"),
        default="neptune-db",
        help="Neptune host URI scheme",
    )
    commit_parser.add_argument(
        "--aoss-host",
        help="OpenSearch Serverless host for Neptune BM25 search",
    )
    _add_llm_args(commit_parser)
    _add_embed_args(commit_parser)

    hosted_parser = subparsers.add_parser(
        "hosted-index",
        help="Clone/fetch a GitHub repository if needed and index it deterministically",
    )
    hosted_parser.add_argument(
        "repo_path",
        nargs="?",
        help="Optional local repository path override for development",
    )
    hosted_parser.add_argument("--github-url", help="Canonical GitHub repository URL")
    hosted_parser.add_argument("--github-repo", help="Canonical GitHub owner/repo slug")
    hosted_parser.add_argument("--repo-id", help="Stable repository identifier")
    hosted_parser.add_argument("--ref", help="Optional branch/tag/ref to resolve and index")
    hosted_parser.add_argument("--commit", help="Optional explicit commit SHA to index")
    hosted_parser.add_argument(
        "--repo-cache-dir",
        default="/mnt/context-graph",
        help="Base directory for persistent hosted clones and worktrees",
    )
    hosted_parser.add_argument(
        "--github-token-env",
        default="GITHUB_TOKEN",
        help="Environment variable that contains the GitHub token",
    )
    hosted_parser.add_argument(
        "--limit",
        type=int,
        default=1000000,
        help="Reserved for exporter compatibility; currently unused",
    )
    hosted_parser.add_argument(
        "--graph-backend",
        choices=("neo4j", "neptune"),
        default="neo4j",
        help="Graph backend syntax to plan against",
    )
    hosted_parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the planned graph writes against the configured graph backend",
    )
    hosted_parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="Metadata backend used to persist the latest snapshot state",
    )
    hosted_parser.add_argument(
        "--metadata-db",
        default=":memory:",
        help="SQLite database path used to persist the latest snapshot metadata",
    )
    hosted_parser.add_argument(
        "--metadata-dsn",
        help="PostgreSQL DSN used when --metadata-backend=postgres",
    )
    hosted_parser.add_argument(
        "--print-limit",
        type=int,
        default=10,
        help="Maximum number of typed operations to preview per category",
    )
    hosted_parser.add_argument(
        "--row-preview-limit",
        type=int,
        default=3,
        help="Maximum number of properties to preview inside each operation payload",
    )
    hosted_parser.add_argument(
        "--neo4j-uri",
        help="Neo4j Bolt URI, e.g. bolt://localhost:7687",
    )
    hosted_parser.add_argument(
        "--neo4j-user",
        help="Neo4j username",
    )
    hosted_parser.add_argument(
        "--neo4j-password",
        help="Neo4j password",
    )
    hosted_parser.add_argument(
        "--neo4j-database",
        help="Neo4j database name",
    )
    hosted_parser.add_argument(
        "--neptune-host",
        help="Neptune host used for direct Bolt execution",
    )
    hosted_parser.add_argument(
        "--neptune-port",
        type=int,
        default=8182,
        help="Neptune Bolt port",
    )
    hosted_parser.add_argument(
        "--neptune-host-scheme",
        choices=("neptune-db", "neptune-graph"),
        default="neptune-db",
        help="Neptune host URI scheme",
    )
    hosted_parser.add_argument(
        "--aoss-host",
        help="OpenSearch Serverless host for Neptune BM25 search",
    )
    _add_llm_args(hosted_parser)
    _add_embed_args(hosted_parser)

    episode_parser = subparsers.add_parser(
        "ingest-episode",
        help="Ingest a generic episode through the unified episode pipeline",
    )
    episode_parser.add_argument("--repo-id", required=True, help="Stable repository identifier")
    episode_parser.add_argument("--source-type", required=True, help="Episode source type")
    episode_parser.add_argument("--source-id", required=True, help="Stable source identifier")
    episode_parser.add_argument("--author", help="Optional episode author/login")
    episode_parser.add_argument("--parent-node-id", help="Optional target node_id for an ON edge")
    episode_parser.add_argument("--thread-id", help="Optional logical thread identifier")
    episode_parser.add_argument("--text", help="Inline episode text")
    episode_parser.add_argument("--text-file", help="Path to a UTF-8 text file containing the episode body")
    episode_parser.add_argument(
        "--graph-backend",
        choices=("neo4j", "neptune"),
        default="neo4j",
        help="Graph backend syntax to plan against",
    )
    episode_parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the planned graph writes against the configured graph backend",
    )
    episode_parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="Metadata backend used to resolve the latest snapshot state",
    )
    episode_parser.add_argument(
        "--metadata-db",
        default=":memory:",
        help="SQLite database path used for metadata",
    )
    episode_parser.add_argument(
        "--metadata-dsn",
        help="PostgreSQL DSN used when --metadata-backend=postgres",
    )
    episode_parser.add_argument(
        "--print-limit",
        type=int,
        default=10,
        help="Maximum number of Cypher statements to print",
    )
    episode_parser.add_argument(
        "--row-preview-limit",
        type=int,
        default=3,
        help="Maximum number of batched parameter rows to print per statement preview",
    )
    episode_parser.add_argument("--neo4j-uri", help="Neo4j Bolt URI, e.g. bolt://localhost:7687")
    episode_parser.add_argument("--neo4j-user", help="Neo4j username")
    episode_parser.add_argument("--neo4j-password", help="Neo4j password")
    episode_parser.add_argument("--neo4j-database", help="Neo4j database name")
    episode_parser.add_argument("--neptune-host", help="Neptune host used for direct Bolt execution")
    episode_parser.add_argument(
        "--neptune-port",
        type=int,
        default=8182,
        help="Neptune Bolt port",
    )
    episode_parser.add_argument(
        "--neptune-host-scheme",
        choices=("neptune-db", "neptune-graph"),
        default="neptune-db",
        help="Neptune host URI scheme",
    )
    episode_parser.add_argument(
        "--aoss-host",
        help="OpenSearch Serverless host for Neptune BM25 search",
    )
    _add_llm_args(episode_parser)
    _add_embed_args(episode_parser)

    parse_query_parser = subparsers.add_parser(
        "parse-query",
        help="Parse a retrieval query into anchors, high-level keywords, and intent tags",
    )
    parse_query_parser.add_argument("--query", required=True, help="Natural-language retrieval query")
    _add_llm_args(parse_query_parser)

    sync_vectors_parser = subparsers.add_parser(
        "sync-profile-vectors",
        help="Embed active profiles into the Aurora pgvector index",
    )
    sync_vectors_parser.add_argument("--repo-id", required=True, help="Stable repository identifier")
    sync_vectors_parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="postgres",
        help="Metadata backend used to load profiles",
    )
    sync_vectors_parser.add_argument("--metadata-db", default=":memory:")
    sync_vectors_parser.add_argument("--metadata-dsn", help="PostgreSQL DSN used when --metadata-backend=postgres")
    _add_embed_args(sync_vectors_parser)

    search_profiles_parser = subparsers.add_parser(
        "search-profiles",
        help="Parse a query and search semantic provenance profiles",
    )
    search_profiles_parser.add_argument("--repo-id", required=True, help="Stable repository identifier")
    search_profiles_parser.add_argument("--query", required=True, help="Natural-language retrieval query")
    search_profiles_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of profile hits to return",
    )
    search_profiles_parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include inactive historical profiles in the result set",
    )
    search_profiles_parser.add_argument(
        "--profile-kind",
        action="append",
        dest="profile_kinds",
        help=(
            "Restrict search to specific profile kind(s). Repeat to pass multiple kinds. "
            "When omitted, searches claim and observation semantic profiles only."
        ),
    )
    search_profiles_parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="postgres",
        help="Metadata backend used to load profiles",
    )
    search_profiles_parser.add_argument("--metadata-db", default=":memory:")
    search_profiles_parser.add_argument("--metadata-dsn", help="PostgreSQL DSN used when --metadata-backend=postgres")
    _add_llm_args(search_profiles_parser)
    _add_embed_args(search_profiles_parser)

    reset_parser = subparsers.add_parser(
        "reset-context",
        help="Delete persisted graph and metadata context for a repository id",
    )
    reset_parser.add_argument(
        "--repo-id",
        required=True,
        help="Stable repository identifier whose stored context should be removed",
    )
    reset_parser.add_argument(
        "--graph-backend",
        choices=("neo4j", "neptune"),
        default="neo4j",
        help="Graph backend syntax to plan against",
    )
    reset_parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the reset against the configured graph and metadata backends",
    )
    reset_parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="Metadata backend used to persist the latest snapshot state",
    )
    reset_parser.add_argument(
        "--metadata-db",
        default=":memory:",
        help="SQLite database path used to persist the latest snapshot metadata",
    )
    reset_parser.add_argument(
        "--metadata-dsn",
        help="PostgreSQL DSN used when --metadata-backend=postgres",
    )
    reset_parser.add_argument(
        "--print-limit",
        type=int,
        default=10,
        help="Maximum number of Cypher statements to print",
    )
    reset_parser.add_argument(
        "--neo4j-uri",
        help="Neo4j Bolt URI, e.g. bolt://localhost:7687",
    )
    reset_parser.add_argument(
        "--neo4j-user",
        help="Neo4j username",
    )
    reset_parser.add_argument(
        "--neo4j-password",
        help="Neo4j password",
    )
    reset_parser.add_argument(
        "--neo4j-database",
        help="Neo4j database name",
    )
    reset_parser.add_argument(
        "--neptune-host",
        help="Neptune host used for direct Bolt execution",
    )
    reset_parser.add_argument(
        "--neptune-port",
        type=int,
        default=8182,
        help="Neptune Bolt port",
    )
    reset_parser.add_argument(
        "--neptune-host-scheme",
        choices=("neptune-db", "neptune-graph"),
        default="neptune-db",
        help="Neptune host URI scheme",
    )
    reset_parser.add_argument(
        "--aoss-host",
        help="OpenSearch Serverless host for Neptune BM25 search",
    )
    return parser


def _producer() -> CodebaseMemorySnapshotProducer:
    root = Path(__file__).resolve().parent.parent
    return CodebaseMemorySnapshotProducer(root=root)


def _add_llm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm-provider",
        choices=("openai", "bedrock"),
        help="Enable Graphiti-style fact extraction with the selected provider",
    )
    parser.add_argument(
        "--llm-model",
        help="LLM model id for episode fact extraction",
    )
    parser.add_argument(
        "--llm-small-model",
        help="Optional smaller LLM model id for Graphiti prompts",
    )
    parser.add_argument(
        "--llm-base-url",
        help="Optional OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--llm-api-key",
        help="Optional API key override for OpenAI-compatible providers",
    )
    parser.add_argument(
        "--aws-region",
        help="AWS region for Bedrock episode extraction",
    )


def _add_embed_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--embed-provider",
        choices=("openai", "bedrock"),
        help="Enable graph and profile embeddings with the selected provider",
    )
    parser.add_argument(
        "--embed-model",
        help="Embedding model id",
    )
    parser.add_argument(
        "--embed-dimensions",
        type=int,
        help="Embedding dimension for graph and pgvector storage",
    )
    parser.add_argument(
        "--embed-base-url",
        help="Optional OpenAI-compatible embeddings base URL",
    )
    parser.add_argument(
        "--embed-api-key",
        help="Optional API key override for embeddings",
    )


def _make_embedding_provider(args):
    settings = load_embedding_settings(args)
    return make_embedding_provider(settings)


def _make_backend_driver(args):
    """Create the additive backend driver used by deterministic ingestion."""

    if args.graph_backend == "neptune":
        host = args.neptune_host or os.getenv("CONTEXT_GRAPH_NEPTUNE_HOST")
        if not host:
            return None
        if not host.startswith(("neptune-db://", "neptune-graph://")):
            host = f"{getattr(args, 'neptune_host_scheme', 'neptune-db')}://{host}"
        return create_driver(
            NeptuneConfig(
                host=host,
                port=args.neptune_port,
                aoss_host=getattr(args, "aoss_host", None) or os.getenv("CONTEXT_GRAPH_AOSS_HOST"),
            )
        )

    uri = args.neo4j_uri or os.getenv("NEO4J_URI")
    user = args.neo4j_user or os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER")
    password = args.neo4j_password or os.getenv("NEO4J_PASSWORD")
    if not uri or user is None or password is None:
        return None
    return create_driver(
        Neo4jConfig(
            uri=uri,
            user=user,
            password=password,
            database=args.neo4j_database or os.getenv("NEO4J_DATABASE") or "neo4j",
        )
    )


def _close_backend_driver(driver) -> None:
    if driver is None:
        return
    BackendSyncBridge(driver).close()


def _make_metadata_store(args) -> MetadataStore:
    if args.metadata_backend == "postgres":
        dsn = args.metadata_dsn or os.getenv("CONTEXT_GRAPH_METADATA_DSN")
        if not dsn:
            raise RuntimeError(
                "PostgreSQL metadata backend requires --metadata-dsn or CONTEXT_GRAPH_METADATA_DSN"
            )
        return PostgresMetadataStore.from_dsn(dsn)

    return SQLiteMetadataStore(args.metadata_db)


def _make_query_parser(args) -> QueryParser:
    llm_settings = load_episode_llm_settings(args)
    llm_client = make_episode_llm_client(llm_settings)
    return QueryParser(llm_client=llm_client)


def _make_profile_vector_store(args, *, metadata: MetadataStore):
    settings = load_embedding_settings(args)
    embedding_dim = settings.dimensions if settings is not None else 1024
    return _make_profile_vector_store_from_metadata(metadata, embedding_dim=embedding_dim)


def _make_profile_vector_store_from_metadata(metadata: MetadataStore, *, embedding_dim: int = 1024):
    if isinstance(metadata, PostgresMetadataStore):
        return AuroraProfileVectorStore.from_metadata_store(metadata, embedding_dim=embedding_dim)
    return InMemoryProfileVectorStore()


def _make_profile_indexer(args, *, metadata: MetadataStore, embedding_provider=None):
    embedding_settings = load_embedding_settings(args)
    embedding_provider = embedding_provider or make_embedding_provider(embedding_settings)
    if embedding_provider is None or embedding_settings is None:
        return None
    vector_store = _make_profile_vector_store_from_metadata(
        metadata,
        embedding_dim=embedding_settings.dimensions,
    )
    if vector_store is None:
        return None
    return ProfileVectorIndexer(
        vector_store=vector_store,
        embedding_provider=embedding_provider,
    )


def _make_github_api(args) -> GitHubApiClient | None:
    token_env = getattr(args, "github_token_env", None)
    token = os.getenv(token_env) if token_env else None
    return GitHubApiClient(token=token)


def _load_episode_text(args) -> str:
    if args.text and args.text_file:
        raise RuntimeError("provide either --text or --text-file, not both")
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    if args.text is not None:
        return args.text
    raise RuntimeError("ingest-episode requires --text or --text-file")


def _now_iso() -> str:
    from .models.graph import utc_now_iso

    return utc_now_iso()


def _snapshot_summary(snapshot) -> dict[str, object]:
    edge_counts: dict[str, int] = {}
    for edge in snapshot.edges:
        edge_counts[edge.edge_type] = edge_counts.get(edge.edge_type, 0) + 1
    return {
        "repo_id": snapshot.repo_id,
        "revision_id": snapshot.revision_id,
        "root_path": snapshot.root_path,
        "generated_at": snapshot.generated_at,
        "node_count": len(snapshot.nodes),
        "edge_count": len(snapshot.edges),
        "edge_type_counts": edge_counts,
        "metadata": dict(snapshot.metadata),
    }


def _preview_parameters(parameters: dict[str, object], *, row_limit: int) -> dict[str, object]:
    preview: dict[str, object] = {}
    for key, value in parameters.items():
        if isinstance(value, list) and len(value) > row_limit:
            preview[key] = {
                "count": len(value),
                "preview": value[:row_limit],
                "truncated": len(value) - row_limit,
            }
        else:
            preview[key] = value
    return preview


def _emit_json(payload: dict[str, object]) -> int:
    try:
        print(json.dumps(payload, indent=2, sort_keys=True))
    except BrokenPipeError:
        try:
            sys.stderr.close()
        except OSError:
            pass
        return 0
    return 0


def _diff_summary(diff) -> dict[str, object]:
    return {
        "previous_revision_id": diff.previous_revision_id,
        "new_revision_id": diff.new_revision_id,
        "added_nodes": len(diff.added_nodes),
        "changed_nodes": len(diff.changed_nodes),
        "removed_nodes": len(diff.removed_nodes),
        "added_edges": len(diff.added_edges),
        "changed_edges": len(diff.changed_edges),
        "removed_edges": len(diff.removed_edges),
    }


def _preview_node_record(record: NodeRecord, *, property_limit: int) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "record_type": record.record_type,
        "labels": list(record.labels),
        "properties": _preview_parameters(dict(record.normalized_properties()), row_limit=property_limit),
    }


def _preview_edge_record(record: EdgeRecord, *, property_limit: int) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "record_type": record.record_type,
        "relationship_type": record.relationship_type,
        "source_record_id": record.source_record_id,
        "target_record_id": record.target_record_id,
        "properties": _preview_parameters(dict(record.normalized_properties()), row_limit=property_limit),
    }


def _preview_search_document(record: SearchDocumentRecord, *, property_limit: int) -> dict[str, object]:
    return {
        "record_kind": record.record_kind.value,
        "record_id": record.record_id,
        "record_type": record.record_type,
        "search_text": record.search_text,
        "properties": _preview_parameters(dict(record.normalized_properties()), row_limit=property_limit),
    }


def _preview_operation_batch(records, *, print_limit: int, property_limit: int, preview_fn):
    materialized = list(records)
    return {
        "count": len(materialized),
        "preview": [preview_fn(record, property_limit=property_limit) for record in materialized[:print_limit]],
        "truncated": max(0, len(materialized) - print_limit),
    }


def _operations_preview(
    plan: MutationPlan,
    *,
    print_limit: int,
    row_preview_limit: int,
) -> dict[str, object]:
    return {
        "node_upserts": _preview_operation_batch(
            plan.upsert_nodes,
            print_limit=print_limit,
            property_limit=row_preview_limit,
            preview_fn=_preview_node_record,
        ),
        "edge_upserts": _preview_operation_batch(
            plan.upsert_edges,
            print_limit=print_limit,
            property_limit=row_preview_limit,
            preview_fn=_preview_edge_record,
        ),
        "search_document_upserts": _preview_operation_batch(
            plan.upsert_search_documents,
            print_limit=print_limit,
            property_limit=row_preview_limit,
            preview_fn=_preview_search_document,
        ),
    }


def _commit_summary(commit) -> dict[str, object]:
    return {
        "sha": commit.sha,
        "parent_shas": list(commit.parent_shas),
        "branch": commit.branch,
        "author_name": commit.author_name,
        "author_email": commit.author_email,
        "authored_at": commit.authored_at,
        "committed_at": commit.committed_at,
        "subject": commit.subject,
        "body": commit.body,
        "changed_files": list(commit.changed_files),
    }


def _repo_index_state_summary(state) -> dict[str, object] | None:
    if state is None:
        return None
    return {
        "repo_id": state.repo_id,
        "root_path": state.root_path,
        "latest_revision_id": state.latest_revision_id,
        "latest_indexed_at": state.latest_indexed_at,
        "latest_commit_sha": state.latest_commit_sha,
        "latest_branch": state.latest_branch,
        "last_commit_indexed_at": state.last_commit_indexed_at,
        "metadata": dict(state.metadata),
    }


def _repo_source_state_summary(state) -> dict[str, object] | None:
    if state is None:
        return None
    return {
        "repo_id": state.repo_id,
        "provider": state.provider,
        "canonical_repo": state.canonical_repo,
        "clone_url": state.clone_url,
        "default_branch": state.default_branch,
        "clone_path": state.clone_path,
        "last_fetch_at": state.last_fetch_at,
        "last_fetched_head_sha": state.last_fetched_head_sha,
        "last_github_sync_at": state.last_github_sync_at,
        "last_github_sync_status": state.last_github_sync_status,
        "last_github_sync_error": state.last_github_sync_error,
        "metadata": dict(state.metadata or {}),
    }


def _repo_ref_summary(repo_ref) -> dict[str, object] | None:
    if repo_ref is None:
        return None
    return {
        "owner": repo_ref.owner,
        "name": repo_ref.name,
        "canonical_repo": repo_ref.canonical_repo,
        "clone_url": repo_ref.clone_url,
        "html_url": repo_ref.html_url,
        "default_branch": repo_ref.default_branch,
        "source": repo_ref.source,
    }


def _github_sync_summary(result) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "repo": _repo_ref_summary(result.repo_ref),
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
        "error": result.error,
        "entity_counts": dict(result.entity_counts),
        "pull_request_numbers": list(result.pull_request_numbers),
        "issue_numbers": list(result.issue_numbers),
        "operations_emitted": result.operations.operation_count(),
        "observation_operations_emitted": _observation_operation_count(result),
        "evidence_operations_emitted": _evidence_operation_count(result),
        "claim_candidates_extracted": _claim_candidates_extracted(result),
        "claim_candidates_validated": _claim_candidates_validated(result),
        "claim_candidates_rejected": _claim_candidates_rejected(result),
        "claim_rejection_reasons": _claim_rejection_reasons(result),
        "evidence_invalidated": _evidence_invalidated_count(result),
        "operations_applied": result.operations_applied,
    }


def _observation_operation_count(result) -> int:
    observation_operations = getattr(result, "observation_operations", None)
    if observation_operations is None:
        return 0
    return observation_operations.operation_count()


def _evidence_operation_count(result) -> int:
    evidence_operations = getattr(result, "evidence_operations", None)
    if evidence_operations is None:
        return 0
    return evidence_operations.operation_count()


def _claim_candidates_extracted(result) -> int:
    return int(getattr(result, "claim_candidates_extracted", 0) or 0)


def _claim_candidates_validated(result) -> int:
    return int(getattr(result, "claim_candidates_validated", 0) or 0)


def _claim_candidates_rejected(result) -> int:
    return int(getattr(result, "claim_candidates_rejected", 0) or 0)


def _claim_rejection_reasons(result) -> dict[str, int]:
    return dict(getattr(result, "claim_rejection_reasons", {}) or {})


def _evidence_invalidated_count(result) -> int:
    return int(getattr(result, "evidence_invalidated", 0) or 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command in {"apply-latest-commit", "apply-commit"}:
        metadata = _make_metadata_store(args)
        backend_driver = _make_backend_driver(args) if args.execute else None
        if args.execute and backend_driver is None:
            raise RuntimeError("deterministic execution requires a configured backend connection")
        llm_settings = load_episode_llm_settings(args)
        llm_client = make_episode_llm_client(llm_settings)
        try:
            driver = IngestionDriver(
                metadata_store=metadata,
                backend_driver=backend_driver,
                snapshot_producer=_producer(),
                llm_client=llm_client,
                profile_indexer=_make_profile_indexer(args, metadata=metadata),
            )
            if args.command == "apply-latest-commit":
                result = driver.apply_latest_commit(
                    args.repo_path,
                    repo_id=args.repo_id,
                    limit=args.limit,
                    execute=args.execute,
                )
            else:
                result = driver.apply_commit(
                    args.repo_path,
                    commit_sha=args.commit,
                    repo_id=args.repo_id,
                    limit=args.limit,
                    execute=args.execute,
                )

            payload = {
                "repo_id": result.repo_id,
                "repo_path": result.repo_path,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
                "previous_commit_sha": result.previous_commit_sha,
                "applied_commit_shas": list(result.applied_commit_shas),
                "commit": _commit_summary(result.commit),
                "snapshot": _snapshot_summary(result.snapshot) if result.snapshot else None,
                "diff": _diff_summary(result.apply_result.diff) if result.apply_result else None,
                "operations_emitted": result.operations.operation_count(),
                "observation_operations_emitted": _observation_operation_count(result),
                "evidence_operations_emitted": _evidence_operation_count(result),
                "claim_candidates_extracted": _claim_candidates_extracted(result),
                "claim_candidates_validated": _claim_candidates_validated(result),
                "claim_candidates_rejected": _claim_candidates_rejected(result),
                "claim_rejection_reasons": _claim_rejection_reasons(result),
                "evidence_invalidated": _evidence_invalidated_count(result),
                "operations_applied": result.apply_result.operations_applied if result.apply_result else 0,
                "operations_preview": _operations_preview(
                    result.operations,
                    print_limit=args.print_limit,
                    row_preview_limit=args.row_preview_limit,
                ),
                "repo_index_state": _repo_index_state_summary(metadata.load_repo_index_state(result.repo_id)),
            }
            return _emit_json(payload)
        finally:
            _close_backend_driver(backend_driver)

    if args.command == "hosted-index":
        metadata = _make_metadata_store(args)
        backend_driver = _make_backend_driver(args) if args.execute else None
        if args.execute and backend_driver is None:
            raise RuntimeError("deterministic execution requires a configured backend connection")
        llm_settings = load_episode_llm_settings(args)
        llm_client = make_episode_llm_client(llm_settings)
        try:
            driver = IngestionDriver(
                metadata_store=metadata,
                backend_driver=backend_driver,
                snapshot_producer=_producer(),
                github_api=_make_github_api(args),
                repo_cache=RepoCacheManager(base_dir=args.repo_cache_dir),
                llm_client=llm_client,
                profile_indexer=_make_profile_indexer(args, metadata=metadata),
            )
            result = driver.index_hosted(
                HostedIndexRequest(
                    repo_path=args.repo_path,
                    github_url=args.github_url,
                    github_repo=args.github_repo,
                    repo_id=args.repo_id,
                    ref=args.ref,
                    commit_sha=args.commit,
                    limit=args.limit,
                    execute=args.execute,
                )
            )
            payload = {
                "request_id": result.request_id,
                "repo_id": result.repo_id,
                "repo_path": result.repo_path,
                "repo_ref": _repo_ref_summary(result.repo_ref),
                "repo_source_state": _repo_source_state_summary(result.repo_source_state),
                "commit": _commit_summary(result.commit_result.commit),
                "commit_index": {
                    "skipped": result.commit_result.skipped,
                    "skip_reason": result.commit_result.skip_reason,
                    "previous_commit_sha": result.commit_result.previous_commit_sha,
                    "applied_commit_shas": list(result.commit_result.applied_commit_shas),
                    "snapshot": _snapshot_summary(result.commit_result.snapshot) if result.commit_result.snapshot else None,
                    "diff": _diff_summary(result.commit_result.apply_result.diff) if result.commit_result.apply_result else None,
                    "operations_emitted": result.commit_result.operations.operation_count(),
                    "observation_operations_emitted": _observation_operation_count(result.commit_result),
                    "evidence_operations_emitted": _evidence_operation_count(result.commit_result),
                    "claim_candidates_extracted": _claim_candidates_extracted(result.commit_result),
                    "claim_candidates_validated": _claim_candidates_validated(result.commit_result),
                    "claim_candidates_rejected": _claim_candidates_rejected(result.commit_result),
                    "claim_rejection_reasons": _claim_rejection_reasons(result.commit_result),
                    "evidence_invalidated": _evidence_invalidated_count(result.commit_result),
                    "operations_applied": result.commit_result.apply_result.operations_applied if result.commit_result.apply_result else 0,
                },
                "github_sync": _github_sync_summary(result.github_sync_result),
                "operations_emitted": result.operations.operation_count(),
                "observation_operations_emitted": _observation_operation_count(result),
                "evidence_operations_emitted": _evidence_operation_count(result),
                "claim_candidates_extracted": _claim_candidates_extracted(result),
                "claim_candidates_validated": _claim_candidates_validated(result),
                "claim_candidates_rejected": _claim_candidates_rejected(result),
                "claim_rejection_reasons": _claim_rejection_reasons(result),
                "evidence_invalidated": _evidence_invalidated_count(result),
                "operations_applied": result.operations_applied,
                "operations_preview": _operations_preview(
                    result.operations,
                    print_limit=args.print_limit,
                    row_preview_limit=args.row_preview_limit,
                ),
                "repo_index_state": _repo_index_state_summary(metadata.load_repo_index_state(result.repo_id)),
            }
            return _emit_json(payload)
        finally:
            _close_backend_driver(backend_driver)

    if args.command == "reset-context":
        metadata = _make_metadata_store(args)
        backend_driver = _make_backend_driver(args) if args.execute else None
        zero_counts = {
            "repo_state": 0,
            "repo_source": 0,
            "current_nodes": 0,
            "current_edges": 0,
            "profiles": 0,
            "index_runs": 0,
        }
        try:
            if args.execute:
                if backend_driver is None:
                    raise RuntimeError("reset-context --execute requires a configured backend connection")
                graph_reset = BackendSyncBridge(backend_driver).reset_repo(args.repo_id)
                metadata_deleted = metadata.reset_repo_context(args.repo_id)
                vector_store = _make_profile_vector_store(args, metadata=metadata)
                vector_deleted = vector_store.reset_repo_context(args.repo_id) if vector_store else 0
                graph_reset_payload: dict[str, object] = {
                    "repo_id": graph_reset.repo_id,
                    "nodes_deleted": graph_reset.nodes_deleted,
                    "edges_deleted": graph_reset.edges_deleted,
                    "search_documents_deleted": graph_reset.search_documents_deleted,
                    "total_deleted": graph_reset.total_deleted,
                }
                graph_reset_preview = None
            else:
                metadata_deleted = zero_counts
                vector_deleted = 0
                graph_reset_payload = {
                    "repo_id": args.repo_id,
                    "nodes_deleted": 0,
                    "edges_deleted": 0,
                    "search_documents_deleted": 0,
                    "total_deleted": 0,
                }
                graph_reset_preview = {
                    "repo_id": args.repo_id,
                    "scope": "repo",
                    "owned_record_families": [
                        "deterministic code graph",
                        "commit provenance",
                        "github structural provenance",
                        "search documents",
                    ],
                }

            payload = {
                "repo_id": args.repo_id,
                "execute": bool(args.execute),
                "graph_reset": graph_reset_payload,
                "graph_reset_preview": graph_reset_preview,
                "metadata_deleted": metadata_deleted,
                "vector_deleted": vector_deleted,
            }
            return _emit_json(payload)
        finally:
            _close_backend_driver(backend_driver)

    if args.command == "parse-query":
        parser_service = _make_query_parser(args)
        result = parser_service.parse(args.query, llm_enabled=True)
        return _emit_json(
            {
                "query": args.query,
                "anchors": [
                    {"kind": anchor.kind, "value": anchor.value}
                    for anchor in result.anchors
                ],
                "high_level_keywords": list(result.high_level_keywords),
                "intent_tags": list(result.intent_tags),
            }
        )

    if args.command == "sync-profile-vectors":
        metadata = _make_metadata_store(args)
        profile_indexer = _make_profile_indexer(args, metadata=metadata)
        if profile_indexer is None:
            raise RuntimeError("sync-profile-vectors requires embedding_clients settings")
        indexed = profile_indexer.sync_repo(repo_id=args.repo_id, metadata_store=metadata)
        return _emit_json(
            {
                "repo_id": args.repo_id,
                "profile_key_records_indexed": indexed,
            }
        )

    if args.command == "search-profiles":
        metadata = _make_metadata_store(args)
        embedding_provider = _make_embedding_provider(args)
        if embedding_provider is None:
            raise RuntimeError("search-profiles requires embedding_clients settings")
        vector_store = _make_profile_vector_store(args, metadata=metadata)
        if vector_store is None:
            raise RuntimeError("unable to create a profile vector store")
        vector_store.bootstrap()
        if isinstance(vector_store, InMemoryProfileVectorStore):
            profile_indexer = ProfileVectorIndexer(
                vector_store=vector_store,
                embedding_provider=embedding_provider,
            )
            profile_indexer.sync_repo(repo_id=args.repo_id, metadata_store=metadata)
        llm_settings = load_episode_llm_settings(args)
        llm_client = make_episode_llm_client(llm_settings)
        profile_search = ProfileSearchService(
            metadata_store=metadata,
            vector_store=vector_store,
            embedding_provider=embedding_provider,
            query_parser=QueryParser(llm_client=llm_client),
        )
        requested_profile_kinds = tuple(dict.fromkeys(args.profile_kinds or ()))
        if requested_profile_kinds:
            result = profile_search.search(
                repo_id=args.repo_id,
                query=args.query,
                limit=args.limit,
                active_only=not args.include_inactive,
                profile_kinds=requested_profile_kinds,
            )
        else:
            fused_search = SemanticProvenanceSearchService(
                profile_search_service=profile_search,
            )
            result = fused_search.search(
                repo_id=args.repo_id,
                query=args.query,
                limit=args.limit,
                active_only=not args.include_inactive,
            )
        profiles_by_id = {profile.profile_id: profile for profile in result.profiles}
        return _emit_json(
            {
                "repo_id": args.repo_id,
                "query": args.query,
                "requested_profile_kinds": list(requested_profile_kinds),
                "anchors": [
                    {"kind": anchor.kind, "value": anchor.value}
                    for anchor in result.parse_result.anchors
                ],
                "high_level_keywords": list(result.parse_result.high_level_keywords),
                "intent_tags": list(result.parse_result.intent_tags),
                "profiles": [
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
                        "metadata": dict(profile.metadata),
                    }
                    for hit in result.hits
                    if (profile := profiles_by_id.get(hit.profile_id)) is not None
                ],
            }
        )

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
