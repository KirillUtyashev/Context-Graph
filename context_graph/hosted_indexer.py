from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import uuid

from .commit_indexer import CommitIndexResult, CommitIndexer
from .episode_processor import EpisodeProcessor
from .git_metadata import GitMetadata
from .github_client import GitHubApiClient, GitHubRepoRef, GitHubRepoResolver
from .github_indexer import GitHubIndexer, GitHubSyncResult
from .graph_read import GraphReadRepository
from .graph_sinks import BaseCypherGraphSink, CypherStatement
from .metadata import MetadataStore, RepoSourceState
from .repo_cache import RepoCacheManager
from .snapshot_builder import CodebaseMemorySnapshotProducer


@dataclass(frozen=True, slots=True)
class HostedIndexRequest:
    repo_path: str | None = None
    github_url: str | None = None
    github_repo: str | None = None
    repo_id: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    limit: int = 1000000
    execute: bool = False


@dataclass(frozen=True, slots=True)
class HostedIndexResult:
    request_id: str
    repo_id: str
    repo_path: str
    repo_ref: GitHubRepoRef | None
    repo_source_state: RepoSourceState | None
    commit_result: CommitIndexResult
    github_result: GitHubSyncResult | None
    github_error: str | None
    statements: tuple[CypherStatement, ...]
    statements_executed: int


class HostedIndexer:
    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        graph_sink: BaseCypherGraphSink,
        snapshot_producer: CodebaseMemorySnapshotProducer | None = None,
        git_metadata: GitMetadata | None = None,
        github_api: GitHubApiClient | None = None,
        repo_cache: RepoCacheManager | None = None,
        episode_processor: EpisodeProcessor | None = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.graph_sink = graph_sink
        self.snapshot_producer = snapshot_producer or CodebaseMemorySnapshotProducer()
        self.git_metadata = git_metadata or GitMetadata()
        self.github_api = github_api
        self.repo_cache = repo_cache or RepoCacheManager(git_metadata=self.git_metadata)
        self.repo_resolver = GitHubRepoResolver(
            git_metadata=self.git_metadata,
            github_api=github_api,
        )
        self.episode_processor = episode_processor or EpisodeProcessor(
            graph_sink=graph_sink,
            read_repository=GraphReadRepository(metadata_store=metadata_store, graph_sink=graph_sink),
        )
        self.github_indexer = (
            GitHubIndexer(
                github_api=github_api,
                graph_sink=graph_sink,
                episode_processor=self.episode_processor,
            )
            if github_api is not None
            else None
        )

    def index(self, request: HostedIndexRequest) -> HostedIndexResult:
        if not request.repo_path and not request.github_url and not request.github_repo:
            raise RuntimeError("hosted indexing requires a GitHub URL, GitHub owner/repo slug, or a local repo path")

        require_github = request.repo_path is None or bool(request.github_url or request.github_repo)
        if require_github and self.github_api is None:
            raise RuntimeError("hosted GitHub indexing requires a GitHub API token")

        repo_ref = self.repo_resolver.resolve(
            github_url=request.github_url,
            github_repo=request.github_repo,
            repo_path=request.repo_path,
            require_github=require_github,
        )
        resolved_repo_id = request.repo_id or _default_repo_id(
            repo_ref.canonical_repo if repo_ref else request.repo_path or "repo"
        )
        request_id = uuid.uuid4().hex
        existing_source = self.metadata_store.load_repo_source_state(resolved_repo_id)
        source_state = existing_source

        with self.metadata_store.repo_lock(_repo_lock_key(repo_ref, resolved_repo_id)):
            if request.repo_path:
                managed_repo = self.repo_cache.manage_local_repo(
                    request.repo_path,
                    repo_id=resolved_repo_id,
                    request_id=request_id,
                )
            else:
                if repo_ref is None:
                    raise RuntimeError("unable to resolve a canonical GitHub repository for hosted indexing")
                managed_repo = self.repo_cache.ensure_managed_repo(
                    repo_ref,
                    repo_id=resolved_repo_id,
                    request_id=request_id,
                    auth_token=self.github_api.token if self.github_api else None,
                    existing_state=existing_source,
                )
                source_state = managed_repo.source_state
                self.metadata_store.save_repo_source_state(source_state)

            try:
                repo_git = GitMetadata(worktree_base_dir=managed_repo.work_root)
                commit_indexer = CommitIndexer(
                    metadata_store=self.metadata_store,
                    graph_sink=self.graph_sink,
                    snapshot_producer=self.snapshot_producer,
                    git_metadata=repo_git,
                    episode_processor=self.episode_processor,
                )
                branch_hint = _branch_hint(request=request, repo_ref=repo_ref, source_state=source_state)
                commit_result = self._index_commits(
                    request=request,
                    repo_path=managed_repo.repo_path,
                    repo_id=resolved_repo_id,
                    branch_hint=branch_hint,
                    git_metadata=repo_git,
                    commit_indexer=commit_indexer,
                    repo_ref=repo_ref,
                )

                github_result = None
                github_error = None
                if repo_ref and self.github_indexer is not None:
                    try:
                        github_result = self.github_indexer.sync_for_commits(
                            repo_id=resolved_repo_id,
                            repo_ref=repo_ref,
                            commit_shas=commit_result.applied_commit_shas,
                            execute=request.execute,
                        )
                        if source_state is not None:
                            source_state = RepoSourceState(
                                repo_id=source_state.repo_id,
                                provider=source_state.provider,
                                canonical_repo=source_state.canonical_repo,
                                clone_url=source_state.clone_url,
                                default_branch=source_state.default_branch,
                                clone_path=source_state.clone_path,
                                last_fetch_at=source_state.last_fetch_at,
                                last_fetched_head_sha=source_state.last_fetched_head_sha,
                                last_github_sync_at=_now_iso(),
                                last_github_sync_status="applied",
                                last_github_sync_error=None,
                                metadata=source_state.metadata,
                            )
                            self.metadata_store.save_repo_source_state(source_state)
                    except Exception as exc:
                        github_error = str(exc)
                        if source_state is not None:
                            source_state = RepoSourceState(
                                repo_id=source_state.repo_id,
                                provider=source_state.provider,
                                canonical_repo=source_state.canonical_repo,
                                clone_url=source_state.clone_url,
                                default_branch=source_state.default_branch,
                                clone_path=source_state.clone_path,
                                last_fetch_at=source_state.last_fetch_at,
                                last_fetched_head_sha=source_state.last_fetched_head_sha,
                                last_github_sync_at=_now_iso(),
                                last_github_sync_status="failed",
                                last_github_sync_error=github_error,
                                metadata=source_state.metadata,
                            )
                            self.metadata_store.save_repo_source_state(source_state)

                statements = tuple(commit_result.statements) + tuple(
                    github_result.statements if github_result is not None else ()
                )
                statements_executed = (
                    (commit_result.apply_result.statements_executed if commit_result.apply_result else 0)
                    + (github_result.statements_executed if github_result is not None else 0)
                )
                return HostedIndexResult(
                    request_id=request_id,
                    repo_id=resolved_repo_id,
                    repo_path=managed_repo.repo_path,
                    repo_ref=repo_ref,
                    repo_source_state=source_state,
                    commit_result=commit_result,
                    github_result=github_result,
                    github_error=github_error,
                    statements=statements,
                    statements_executed=statements_executed,
                )
            finally:
                self.repo_cache.cleanup_request_workspace(resolved_repo_id, request_id)

    def _index_commits(
        self,
        *,
        request: HostedIndexRequest,
        repo_path: str,
        repo_id: str,
        branch_hint: str | None,
        git_metadata: GitMetadata,
        commit_indexer: CommitIndexer,
        repo_ref: GitHubRepoRef | None,
    ) -> CommitIndexResult:
        if request.commit_sha:
            return commit_indexer.apply_commit(
                repo_path,
                commit_sha=request.commit_sha,
                repo_id=repo_id,
                branch_hint=branch_hint,
                limit=request.limit,
                execute=request.execute,
            )
        if request.ref:
            resolved_commit = self.repo_cache.resolve_ref_sha(repo_path, request.ref)
            return commit_indexer.apply_commit(
                repo_path,
                commit_sha=resolved_commit,
                repo_id=repo_id,
                branch_hint=branch_hint,
                limit=request.limit,
                execute=request.execute,
            )
        if repo_ref and repo_ref.default_branch and not request.repo_path:
            self.repo_cache.prepare_default_branch(repo_path, repo_ref.default_branch)
        return commit_indexer.apply_latest_commit(
            repo_path,
            repo_id=repo_id,
            branch_hint=branch_hint,
            limit=request.limit,
            execute=request.execute,
        )


def _default_repo_id(value: str) -> str:
    raw_name = Path(value).name or value
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_name).strip("-_.").lower()
    return slug or "repo"


def _repo_lock_key(repo_ref: GitHubRepoRef | None, repo_id: str) -> str:
    return repo_ref.canonical_repo if repo_ref is not None else repo_id


def _branch_hint(
    *,
    request: HostedIndexRequest,
    repo_ref: GitHubRepoRef | None,
    source_state: RepoSourceState | None,
) -> str | None:
    if request.ref and not request.ref.startswith("refs/tags/") and not _looks_like_commit(request.ref):
        return request.ref
    if source_state and source_state.default_branch:
        return source_state.default_branch
    if repo_ref and repo_ref.default_branch:
        return repo_ref.default_branch
    return None


def _now_iso() -> str:
    from .models import utc_now_iso

    return utc_now_iso()


def _looks_like_commit(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", value))
