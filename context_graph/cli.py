from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from .commit_indexer import CommitIndexer
from .episode_processor import EpisodeProcessor, EpisodeWorkItem
from .episodes import EpisodeRecord
from .graph_read import GraphReadRepository
from .graphiti_adapter import GraphitiAdapter
from .github_client import GitHubApiClient
from .graph_sinks import NeptuneGraphSink, Neo4jGraphSink
from .hosted_indexer import HostedIndexRequest, HostedIndexer
from .llm import load_episode_llm_settings, make_episode_llm_client
from .metadata import MetadataStore, PostgresMetadataStore, SQLiteMetadataStore
from .repo_cache import RepoCacheManager
from .snapshot_builder import CodebaseMemorySnapshotProducer


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
        help="Maximum number of Cypher statements to print",
    )
    latest_commit_parser.add_argument(
        "--row-preview-limit",
        type=int,
        default=3,
        help="Maximum number of batched parameter rows to print per statement preview",
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
    _add_llm_args(latest_commit_parser)

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
        help="Maximum number of Cypher statements to print",
    )
    commit_parser.add_argument(
        "--row-preview-limit",
        type=int,
        default=3,
        help="Maximum number of batched parameter rows to print per statement preview",
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
    _add_llm_args(commit_parser)

    hosted_parser = subparsers.add_parser(
        "hosted-index",
        help="Clone/fetch a GitHub repository if needed, index commits, and attach GitHub provenance",
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
        help="Maximum number of Cypher statements to print",
    )
    hosted_parser.add_argument(
        "--row-preview-limit",
        type=int,
        default=3,
        help="Maximum number of batched parameter rows to print per statement preview",
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
    _add_llm_args(hosted_parser)

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
    _add_llm_args(episode_parser)

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


def _make_episode_processor(
    args,
    *,
    metadata: MetadataStore | None = None,
    graph_sink=None,
) -> EpisodeProcessor:
    metadata = metadata or _make_metadata_store(args)
    graph_sink = graph_sink or _make_sink(args)
    llm_settings = load_episode_llm_settings(args)
    llm_client = make_episode_llm_client(llm_settings)
    adapter = GraphitiAdapter(llm_client=llm_client)
    return EpisodeProcessor(
        graph_sink=graph_sink,
        read_repository=GraphReadRepository(metadata_store=metadata, graph_sink=graph_sink),
        adapter=adapter,
    )


def _make_sink(args):
    if args.graph_backend == "neptune":
        return NeptuneGraphSink(
            host=args.neptune_host or os.getenv("CONTEXT_GRAPH_NEPTUNE_HOST"),
            port=args.neptune_port,
        )

    return Neo4jGraphSink(
        uri=args.neo4j_uri or os.getenv("NEO4J_URI"),
        username=args.neo4j_user or os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER"),
        password=args.neo4j_password or os.getenv("NEO4J_PASSWORD"),
        database=args.neo4j_database or os.getenv("NEO4J_DATABASE"),
    )


def _make_metadata_store(args) -> MetadataStore:
    if args.metadata_backend == "postgres":
        dsn = args.metadata_dsn or os.getenv("CONTEXT_GRAPH_METADATA_DSN")
        if not dsn:
            raise RuntimeError(
                "PostgreSQL metadata backend requires --metadata-dsn or CONTEXT_GRAPH_METADATA_DSN"
            )
        return PostgresMetadataStore.from_dsn(dsn)

    return SQLiteMetadataStore(args.metadata_db)


def _make_github_api(args) -> GitHubApiClient | None:
    token_env = getattr(args, "github_token_env", None)
    if not token_env:
        return None
    token = os.getenv(token_env)
    if not token:
        return None
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
    from .models import utc_now_iso

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


def _statements_preview(statements, *, print_limit: int, row_preview_limit: int) -> list[dict[str, object]]:
    return [
        {
            "query": statement.query,
            "parameters": _preview_parameters(
                dict(statement.parameters),
                row_limit=row_preview_limit,
            ),
        }
        for statement in statements[:print_limit]
    ]


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
        "episode_count": result.episode_count,
        "pull_request_numbers": list(result.pull_request_numbers),
        "issue_numbers": list(result.issue_numbers),
        "statements_emitted": len(result.statements),
        "statements_executed": result.statements_executed,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command in {"apply-latest-commit", "apply-commit"}:
        metadata = _make_metadata_store(args)
        graph_sink = _make_sink(args)
        if args.execute:
            graph_sink.validate_execute_config()
        episode_processor = _make_episode_processor(
            args,
            metadata=metadata,
            graph_sink=graph_sink,
        )
        indexer = CommitIndexer(
            metadata_store=metadata,
            graph_sink=graph_sink,
            snapshot_producer=_producer(),
            episode_processor=episode_processor,
        )
        if args.command == "apply-latest-commit":
            result = indexer.apply_latest_commit(
                args.repo_path,
                repo_id=args.repo_id,
                limit=args.limit,
                execute=args.execute,
            )
        else:
            result = indexer.apply_commit(
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
            "statements_emitted": len(result.statements),
            "statements_executed": result.apply_result.statements_executed if result.apply_result else 0,
            "statements_preview": _statements_preview(
                result.statements,
                print_limit=args.print_limit,
                row_preview_limit=args.row_preview_limit,
            ),
            "repo_index_state": _repo_index_state_summary(metadata.load_repo_index_state(result.repo_id)),
        }
        return _emit_json(payload)

    if args.command == "hosted-index":
        metadata = _make_metadata_store(args)
        graph_sink = _make_sink(args)
        if args.execute:
            graph_sink.validate_execute_config()
        episode_processor = _make_episode_processor(
            args,
            metadata=metadata,
            graph_sink=graph_sink,
        )
        hosted_indexer = HostedIndexer(
            metadata_store=metadata,
            graph_sink=graph_sink,
            snapshot_producer=_producer(),
            github_api=_make_github_api(args),
            repo_cache=RepoCacheManager(base_dir=args.repo_cache_dir),
            episode_processor=episode_processor,
        )
        result = hosted_indexer.index(
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
                "statements_emitted": len(result.commit_result.statements),
                "statements_executed": result.commit_result.apply_result.statements_executed if result.commit_result.apply_result else 0,
            },
            "github": _github_sync_summary(result.github_result),
            "github_error": result.github_error,
            "statements_emitted": len(result.statements),
            "statements_executed": result.statements_executed,
            "statements_preview": _statements_preview(
                result.statements,
                print_limit=args.print_limit,
                row_preview_limit=args.row_preview_limit,
            ),
            "repo_index_state": _repo_index_state_summary(metadata.load_repo_index_state(result.repo_id)),
        }
        return _emit_json(payload)

    if args.command == "ingest-episode":
        processor = _make_episode_processor(args)
        graph_sink = processor.graph_sink
        if args.execute:
            graph_sink.validate_execute_config()
        text = _load_episode_text(args)
        batch = processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=EpisodeRecord(
                        episode_id=f"{args.source_type}:{args.source_id}",
                        repo_id=args.repo_id,
                        source_type=args.source_type,
                        source_id=args.source_id,
                        author=args.author,
                        created_at=_now_iso(),
                        updated_at=_now_iso(),
                        text=text,
                        metadata={"thread_id": args.thread_id} if args.thread_id else {},
                        parent_node_id=args.parent_node_id,
                    )
                )
            ],
            execute=args.execute,
        )
        payload = {
            "repo_id": args.repo_id,
            "episodes": [
                {
                    "episode_id": result.episode.episode_id,
                    "source_type": result.episode.source_type,
                    "mentions": [mention.target_node_id for mention in result.mentions],
                    "facts": [
                        {
                            "fact_id": fact.fact_id,
                            "source_node_id": fact.source_node_id,
                            "target_node_id": fact.target_node_id,
                            "relation_type": fact.relation_type,
                            "fact": fact.fact,
                        }
                        for fact in result.facts
                    ],
                }
                for result in batch.results
            ],
            "statements_emitted": len(batch.statements),
            "statements_executed": batch.statements_executed,
            "statements_preview": _statements_preview(
                batch.statements,
                print_limit=args.print_limit,
                row_preview_limit=args.row_preview_limit,
            ),
        }
        return _emit_json(payload)

    if args.command == "reset-context":
        metadata = _make_metadata_store(args)
        graph_sink = _make_sink(args)
        statements = graph_sink.plan_reset_repo(args.repo_id)
        zero_counts = {
            "repo_state": 0,
            "current_nodes": 0,
            "current_edges": 0,
            "context_nodes": 0,
            "profiles": 0,
            "index_runs": 0,
        }
        if args.execute:
            graph_sink.validate_execute_config()
            statements_executed = graph_sink.execute(statements)
            metadata_deleted = metadata.reset_repo_context(args.repo_id)
        else:
            statements_executed = 0
            metadata_deleted = zero_counts

        payload = {
            "repo_id": args.repo_id,
            "execute": bool(args.execute),
            "metadata_deleted": metadata_deleted,
            "graph_statements_emitted": len(statements),
            "graph_statements_executed": statements_executed,
            "graph_statements_preview": [
                {
                    "query": statement.query,
                    "parameters": dict(statement.parameters),
                }
                for statement in statements[: args.print_limit]
            ],
        }
        return _emit_json(payload)

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
