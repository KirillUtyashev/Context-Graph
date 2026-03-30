from __future__ import annotations

from dataclasses import replace
import re
import uuid
from typing import Any

from context_graph.ingestion.git.git_metadata import GitMetadata
from context_graph.ingestion.git.github_client import GitHubApiClient, GitHubRepoRef, GitHubRepoResolver
from context_graph.ingestion.git.github_indexer import GitHubIndexer, GitHubSyncResult
from context_graph.profile_vectors import ProfileVectorIndexer
from context_graph.backend import BackendDriver, merge_mutation_plans, MutationPlan

from .commit_indexing import CommitIndexingService, default_repo_id
from .deterministic_planner import DeterministicMutationPlanner
from .models import DeterministicHostedIndexResult, HostedIndexRequest
from .repo_cache import RepoCacheManager
from .snapshot_builder import CodebaseMemorySnapshotProducer
from context_graph.backend import MetadataStore, RepoSourceState
from ..models import utc_now_iso


class HostedIndexingService:
    """Resolves, materializes, and indexes repositories for hosted requests."""

    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        backend_driver: BackendDriver | None,
        snapshot_producer: CodebaseMemorySnapshotProducer | None = None,
        git_metadata: GitMetadata | None = None,
        github_api: GitHubApiClient | None = None,
        repo_cache: RepoCacheManager | None = None,
        repo_resolver: GitHubRepoResolver | None = None,
        planner: DeterministicMutationPlanner | None = None,
        llm_client: Any | None = None,
        profile_indexer: ProfileVectorIndexer | None = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.backend_driver = backend_driver
        self.snapshot_producer = snapshot_producer or CodebaseMemorySnapshotProducer()
        self.git_metadata = git_metadata or GitMetadata()
        self.github_api = github_api or GitHubApiClient(token=None)
        self.repo_cache = repo_cache or RepoCacheManager(git_metadata=self.git_metadata)
        self.planner = planner or DeterministicMutationPlanner()
        self.llm_client = llm_client
        self.profile_indexer = profile_indexer
        self.repo_resolver = repo_resolver or GitHubRepoResolver(
            git_metadata=self.git_metadata,
            github_api=self.github_api,
        )
        if getattr(self.repo_resolver, "github_api", None) is None:
            self.repo_resolver.github_api = self.github_api

    def index(self, request: HostedIndexRequest) -> DeterministicHostedIndexResult:
        """Resolve a repo source, materialize a checkout, and run deterministic indexing."""

        if not request.repo_path and not request.github_url and not request.github_repo:
            raise RuntimeError(
                "hosted indexing requires a GitHub URL, GitHub owner/repo slug, or a local repo path"
            )

        require_github = request.repo_path is None or bool(request.github_url or request.github_repo)

        repo_ref = self.repo_resolver.resolve(
            github_url=request.github_url,
            github_repo=request.github_repo,
            repo_path=request.repo_path,
            require_github=require_github,
        )
        resolved_repo_id = request.repo_id or default_repo_id(
            repo_ref.canonical_repo if repo_ref else request.repo_path or "repo"
        )
        request_id = uuid.uuid4().hex
        existing_source = self.metadata_store.load_repo_source_state(resolved_repo_id)
        source_state = existing_source

        with self.metadata_store.repo_lock(repo_lock_key(repo_ref, resolved_repo_id)):
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
                commit_service = CommitIndexingService(
                    metadata_store=self.metadata_store,
                    backend_driver=self.backend_driver,
                    snapshot_producer=self.snapshot_producer,
                    git_metadata=repo_git,
                    planner=self.planner,
                    llm_client=self.llm_client,
                    profile_indexer=self.profile_indexer,
                )
                branch_hint = branch_hint_for_request(
                    request=request,
                    repo_ref=repo_ref,
                    source_state=source_state,
                )
                commit_result = self._index_commits(
                    request=request,
                    repo_path=managed_repo.repo_path,
                    repo_id=resolved_repo_id,
                    branch_hint=branch_hint,
                    commit_service=commit_service,
                )
                github_result, source_state = self._sync_github_structural_provenance(
                    request=request,
                    repo_id=resolved_repo_id,
                    repo_ref=repo_ref,
                    commit_result=commit_result,
                    source_state=source_state,
                )
                if source_state is not None:
                    self.metadata_store.save_repo_source_state(source_state)
                combined_operations = self.planner.merge_plans(
                    commit_result.operations,
                    github_result.operations if github_result is not None else MutationPlan(),
                )
                observation_operations = merge_mutation_plans(
                    getattr(commit_result, "observation_operations", MutationPlan()),
                    (
                        github_result.observation_operations
                        if github_result is not None
                        else MutationPlan()
                    ),
                )
                evidence_operations = merge_mutation_plans(
                    getattr(commit_result, "evidence_operations", MutationPlan()),
                    (
                        github_result.evidence_operations
                        if github_result is not None
                        else MutationPlan()
                    ),
                )
                return DeterministicHostedIndexResult(
                    request_id=request_id,
                    repo_id=resolved_repo_id,
                    repo_path=managed_repo.repo_path,
                    repo_ref=repo_ref,
                    repo_source_state=source_state,
                    commit_result=commit_result,
                    github_sync_result=github_result,
                    operations=combined_operations,
                    operations_applied=(
                        (commit_result.apply_result.operations_applied if commit_result.apply_result else 0)
                        + (github_result.operations_applied if github_result is not None else 0)
                    ),
                    observation_operations=observation_operations,
                    evidence_operations=evidence_operations,
                    claim_candidates_extracted=(
                        getattr(commit_result, "claim_candidates_extracted", 0)
                        + (github_result.claim_candidates_extracted if github_result is not None else 0)
                    ),
                    claim_candidates_validated=(
                        getattr(commit_result, "claim_candidates_validated", 0)
                        + (github_result.claim_candidates_validated if github_result is not None else 0)
                    ),
                    claim_candidates_rejected=(
                        getattr(commit_result, "claim_candidates_rejected", 0)
                        + (github_result.claim_candidates_rejected if github_result is not None else 0)
                    ),
                    claim_rejection_reasons=_merge_reason_counts(
                        getattr(commit_result, "claim_rejection_reasons", {}),
                        github_result.claim_rejection_reasons if github_result is not None else {},
                    ),
                    evidence_invalidated=(
                        getattr(commit_result, "evidence_invalidated", 0)
                        + (github_result.evidence_invalidated if github_result is not None else 0)
                    ),
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
        commit_service: CommitIndexingService,
    ):
        if request.commit_sha:
            return commit_service.apply_commit_with_history(
                repo_path,
                commit_sha=request.commit_sha,
                repo_id=repo_id,
                branch_hint=branch_hint,
                limit=request.limit,
                execute=request.execute,
                history_window=5,
            )
        if request.ref:
            resolved_commit = self.repo_cache.resolve_ref_sha(repo_path, request.ref)
            return commit_service.apply_commit_with_history(
                repo_path,
                commit_sha=resolved_commit,
                repo_id=repo_id,
                branch_hint=branch_hint,
                limit=request.limit,
                execute=request.execute,
                history_window=5,
            )
        latest_commit = commit_service.git_metadata.get_commit_info(
            repo_path,
            branch_hint=branch_hint,
        )
        return commit_service.apply_commit_with_history(
            repo_path,
            commit_sha=latest_commit.sha,
            repo_id=repo_id,
            branch_hint=branch_hint,
            limit=request.limit,
            execute=request.execute,
            history_window=5,
        )

    def _sync_github_structural_provenance(
        self,
        *,
        request: HostedIndexRequest,
        repo_id: str,
        repo_ref: GitHubRepoRef | None,
        commit_result,
        source_state: RepoSourceState | None,
    ) -> tuple[GitHubSyncResult | None, RepoSourceState | None]:
        """Sync GitHub PR/issue structure after deterministic commit indexing.

        The deterministic pass is the source of truth for code + commit records.
        GitHub sync is an additive follow-up step. If it fails, we keep the
        deterministic result and record the sync failure in metadata.
        """

        if self.github_api is None or repo_ref is None or not commit_result.applied_commit_shas:
            return None, source_state

        github_indexer = GitHubIndexer(
            github_api=self.github_api,
            metadata_store=self.metadata_store,
            backend_driver=self.backend_driver,
            llm_client=self.llm_client,
            profile_indexer=self.profile_indexer,
        )
        sync_started_at = utc_now_iso()
        try:
            result = github_indexer.sync_for_commits(
                repo_id=repo_id,
                repo_ref=repo_ref,
                commit_shas=commit_result.applied_commit_shas,
                snapshot=commit_result.snapshot,
                execute=request.execute,
            )
        except Exception as exc:
            if source_state is None:
                raise
            failed_state = replace(
                source_state,
                last_github_sync_at=sync_started_at,
                last_github_sync_status="failed",
                last_github_sync_error=str(exc),
            )
            return GitHubSyncResult(
                repo_ref=repo_ref,
                skipped=False,
                skip_reason=None,
                error=str(exc),
                operations=MutationPlan(),
                operations_applied=0,
                entity_counts={},
                pull_request_numbers=(),
                issue_numbers=(),
                evidence_operations=MutationPlan(),
            ), failed_state

        if result.skipped or source_state is None:
            return result, source_state

        updated_state = replace(
            source_state,
            last_github_sync_at=sync_started_at,
            last_github_sync_status="applied",
            last_github_sync_error=None,
        )
        return result, updated_state


def repo_lock_key(repo_ref: GitHubRepoRef | None, repo_id: str) -> str:
    """Choose a stable metadata lock key for a hosted repository request."""

    return repo_ref.canonical_repo if repo_ref is not None else repo_id


def branch_hint_for_request(
    *,
    request: HostedIndexRequest,
    repo_ref: GitHubRepoRef | None,
    source_state: RepoSourceState | None,
) -> str | None:
    """Infer the branch we should read commit metadata from."""

    if request.ref and not request.ref.startswith("refs/tags/") and not looks_like_commit(request.ref):
        return request.ref
    if source_state and source_state.default_branch:
        return source_state.default_branch
    if repo_ref and repo_ref.default_branch:
        return repo_ref.default_branch
    return None


def looks_like_commit(value: str) -> bool:
    """Best-effort check for a SHA-like git revision string."""

    return bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", value))


def _merge_reason_counts(*reason_maps: dict[str, int]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason_map in reason_maps:
        for key, value in reason_map.items():
            counts[key] = counts.get(key, 0) + value
    return counts
