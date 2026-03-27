#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import defaultdict
from pathlib import Path

from context_graph.cli import (
    _make_backend_driver,
    _make_github_api,
    _make_metadata_store,
)
from context_graph.ingestion import HostedIndexRequest, IngestionDriver, RepoCacheManager
from context_graph.ingestion.driver import _branch_hint, _repo_lock_key
from context_graph.ingestion.git.git_metadata import GitMetadata
from context_graph.ingestion.git.github_indexer import GitHubIndexer
from context_graph.models import utc_now_iso


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Debug GitHub structural provenance bundle generation.",
    )
    parser.add_argument("repo_path", nargs="?", help="Optional local repo path override")
    parser.add_argument("--github-url", help="GitHub repo URL")
    parser.add_argument("--github-repo", help="GitHub owner/repo slug")
    parser.add_argument("--repo-id", required=True, help="Stable repository identifier")
    parser.add_argument(
        "--repo-cache-dir",
        default=str(Path(".cache/context-graph").resolve()),
        help="Managed repo cache directory",
    )
    parser.add_argument("--ref", help="Git ref to inspect")
    parser.add_argument("--commit-sha", action="append", help="Specific commit SHA(s) to inspect")
    parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="Metadata backend",
    )
    parser.add_argument(
        "--metadata-db",
        default=":memory:",
        help="SQLite metadata DB path",
    )
    parser.add_argument("--metadata-dsn", help="PostgreSQL metadata DSN")
    parser.add_argument(
        "--graph-backend",
        choices=("neo4j", "neptune"),
        default="neo4j",
        help="Graph backend syntax to plan against",
    )
    parser.add_argument("--neo4j-uri", help="Neo4j Bolt URI")
    parser.add_argument("--neo4j-user", help="Neo4j username")
    parser.add_argument("--neo4j-password", help="Neo4j password")
    parser.add_argument("--neo4j-database", help="Neo4j database name")
    parser.add_argument("--neptune-host", help="Neptune host")
    parser.add_argument("--neptune-port", type=int, default=8182, help="Neptune port")
    parser.add_argument("--neptune-host-scheme", default="neptune-db", help="Neptune host scheme")
    parser.add_argument("--aoss-host", help="Neptune AOSS host")
    parser.add_argument(
        "--github-token-env",
        default="GITHUB_TOKEN",
        help="Environment variable name that stores the GitHub token",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    metadata = _make_metadata_store(args)
    github_api = _make_github_api(args)
    if github_api is None:
        raise RuntimeError(
            f"GitHub token env {args.github_token_env!r} is not set; cannot build live GitHub bundle"
        )

    driver = IngestionDriver(
        metadata_store=metadata,
        backend_driver=_make_backend_driver(args),
        github_api=github_api,
        repo_cache=RepoCacheManager(base_dir=args.repo_cache_dir),
    )
    github_indexer = GitHubIndexer(
        github_api=github_api,
        backend_driver=None,
    )

    request = HostedIndexRequest(
        repo_path=args.repo_path,
        github_url=args.github_url,
        github_repo=args.github_repo,
        repo_id=args.repo_id,
        ref=args.ref,
        commit_sha=args.commit_sha[0] if args.commit_sha else None,
    )

    repo_ref = driver.repo_resolver.resolve(
        github_url=args.github_url,
        github_repo=args.github_repo,
        repo_path=args.repo_path,
        require_github=(args.repo_path is None or bool(args.github_url or args.github_repo)),
    )
    if repo_ref is None and not args.repo_path:
        raise RuntimeError("unable to resolve repository")

    existing_source = metadata.load_repo_source_state(args.repo_id)
    source_state = existing_source
    request_id = uuid.uuid4().hex
    require_github = args.repo_path is None or bool(args.github_url or args.github_repo)

    with metadata.repo_lock(_repo_lock_key(repo_ref, args.repo_id)):
        if args.repo_path:
            managed_repo = driver.repo_cache.manage_local_repo(
                args.repo_path,
                repo_id=args.repo_id,
                request_id=request_id,
            )
        else:
            if repo_ref is None:
                raise RuntimeError("unable to resolve canonical GitHub repository")
            managed_repo = driver.repo_cache.ensure_managed_repo(
                repo_ref,
                repo_id=args.repo_id,
                request_id=request_id,
                auth_token=github_api.token,
                existing_state=existing_source,
            )
            source_state = managed_repo.source_state
        try:
            repo_git = GitMetadata(worktree_base_dir=managed_repo.work_root)
            branch_hint = _branch_hint(request=request, repo_ref=repo_ref, source_state=source_state)
            commit_shas = list(args.commit_sha or [])
            if args.ref and not commit_shas:
                commit_shas.append(driver.repo_cache.resolve_ref_sha(managed_repo.repo_path, args.ref))
            if not commit_shas:
                if repo_ref and repo_ref.default_branch and not args.repo_path:
                    driver.repo_cache.prepare_default_branch(managed_repo.repo_path, repo_ref.default_branch)
                commit_shas.append(
                    repo_git.get_commit_info(
                        managed_repo.repo_path,
                        branch_hint=branch_hint,
                    ).sha
                )

            bundle = github_indexer._build_bundle(
                repo_id=args.repo_id,
                repo_ref=repo_ref,
                commit_shas=tuple(commit_shas),
            )

            print(
                json.dumps(
                    {
                        "repo_id": args.repo_id,
                        "repo_path": managed_repo.repo_path,
                        "resolved_at": utc_now_iso(),
                        "commit_shas": commit_shas,
                        "bundle": {
                            "entity_count": len(bundle.entities),
                            "project_link_count": len(bundle.project_links),
                            "commit_link_count": len(bundle.commit_links),
                            "entity_link_count": len(bundle.entity_links),
                            "entity_kinds": _count_by(lambda item: item.kind, bundle.entities),
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        finally:
            driver.repo_cache.cleanup_request_workspace(args.repo_id, request_id)
    return 0


def _count_by(key_fn, values) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[str(key_fn(value))] += 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
