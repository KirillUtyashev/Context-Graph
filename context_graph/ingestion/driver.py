from __future__ import annotations

from context_graph.ingestion.git.git_metadata import GitMetadata
from context_graph.ingestion.git.github_client import GitHubApiClient, GitHubRepoResolver
from context_graph.storage.backend import BackendDriver

from .commit_indexing import CommitIndexingService
from .deterministic_planner import DeterministicMutationPlanner
from .hosted_indexing import (
    HostedIndexingService,
    branch_hint_for_request as _branch_hint,
    repo_lock_key as _repo_lock_key,
)
from .models import CommitIndexResult, DeterministicHostedIndexResult, HostedIndexRequest
from .repo_cache import RepoCacheManager
from .snapshot_builder import CodebaseMemorySnapshotProducer
from ..metadata import MetadataStore


class IngestionDriver:
    """Public entrypoint for the deterministic ingestion block.

    This class intentionally stays thin. It wires dependencies together and
    delegates the real orchestration to focused services for commit indexing and
    hosted repo resolution/materialization.
    """

    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        backend_driver: BackendDriver | None,
        snapshot_producer: CodebaseMemorySnapshotProducer | None = None,
        git_metadata: GitMetadata | None = None,
        github_api: GitHubApiClient | None = None,
        repo_cache: RepoCacheManager | None = None,
        llm_client=None,
    ) -> None:
        self.metadata_store = metadata_store
        self.backend_driver = backend_driver
        self.snapshot_producer = snapshot_producer or CodebaseMemorySnapshotProducer()
        self.git_metadata = git_metadata or GitMetadata()
        self.github_api = github_api
        self.repo_cache = repo_cache or RepoCacheManager(git_metadata=self.git_metadata)
        self.llm_client = llm_client
        self.planner = DeterministicMutationPlanner()
        self.repo_resolver = GitHubRepoResolver(
            git_metadata=self.git_metadata,
            github_api=github_api,
        )

    def apply_latest_commit(
        self,
        repo_path: str,
        *,
        repo_id: str | None = None,
        branch_hint: str | None = None,
        limit: int = 1000000,
        execute: bool = False,
    ) -> CommitIndexResult:
        """Index HEAD for a checkout, replaying any missing descendant commits."""

        return self._make_commit_indexing_service().apply_latest_commit(
            repo_path,
            repo_id=repo_id,
            branch_hint=branch_hint,
            limit=limit,
            execute=execute,
        )

    def apply_commit(
        self,
        repo_path: str,
        *,
        commit_sha: str,
        repo_id: str | None = None,
        branch_hint: str | None = None,
        limit: int = 1000000,
        execute: bool = False,
    ) -> CommitIndexResult:
        """Index one explicit commit from a local checkout."""

        return self._make_commit_indexing_service().apply_commit(
            repo_path,
            commit_sha=commit_sha,
            repo_id=repo_id,
            branch_hint=branch_hint,
            limit=limit,
            execute=execute,
        )

    def index_hosted(self, request: HostedIndexRequest) -> DeterministicHostedIndexResult:
        """Resolve a hosted request and run deterministic indexing only."""

        return self._make_hosted_indexing_service().index(request)

    def _make_commit_indexing_service(self) -> CommitIndexingService:
        return CommitIndexingService(
            metadata_store=self.metadata_store,
            backend_driver=self.backend_driver,
            snapshot_producer=self.snapshot_producer,
            git_metadata=self.git_metadata,
            planner=self.planner,
            llm_client=self.llm_client,
        )

    def _make_hosted_indexing_service(self) -> HostedIndexingService:
        return HostedIndexingService(
            metadata_store=self.metadata_store,
            backend_driver=self.backend_driver,
            snapshot_producer=self.snapshot_producer,
            git_metadata=self.git_metadata,
            github_api=self.github_api,
            repo_cache=self.repo_cache,
            repo_resolver=self.repo_resolver,
            planner=self.planner,
        )
