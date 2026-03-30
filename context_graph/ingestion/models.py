from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.ingestion.git.github_client import GitHubRepoRef

from context_graph.backend import MutationPlan

from ..models import GraphSnapshot, SnapshotApplyResult
from context_graph.backend import RepoSourceState

if TYPE_CHECKING:
    from context_graph.ingestion.git.github_indexer import GitHubSyncResult


@dataclass(frozen=True, slots=True)
class ManagedRepo:
    """A repository checkout plus per-request workspace metadata."""

    repo_id: str
    repo_path: str
    request_id: str
    work_root: str
    repo_ref: GitHubRepoRef | None
    source_state: RepoSourceState | None


@dataclass(frozen=True, slots=True)
class CommitIndexResult:
    """The deterministic result of indexing one commit or a replay window."""

    repo_id: str
    repo_path: str
    commit: CommitInfo
    previous_commit_sha: str | None
    branch: str | None
    skipped: bool
    skip_reason: str | None
    snapshot: GraphSnapshot | None
    apply_result: SnapshotApplyResult | None
    operations: MutationPlan
    applied_commit_shas: tuple[str, ...]
    observation_operations: MutationPlan = field(default_factory=MutationPlan)
    evidence_operations: MutationPlan = field(default_factory=MutationPlan)
    claim_candidates_extracted: int = 0
    claim_candidates_validated: int = 0
    claim_candidates_rejected: int = 0
    claim_rejection_reasons: dict[str, int] = field(default_factory=dict)
    evidence_invalidated: int = 0


@dataclass(frozen=True, slots=True)
class HostedIndexRequest:
    """Input for deterministic hosted indexing of a local or remote repository."""

    repo_path: str | None = None
    github_url: str | None = None
    github_repo: str | None = None
    repo_id: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    limit: int = 1000000
    execute: bool = False


@dataclass(frozen=True, slots=True)
class DeterministicHostedIndexResult:
    """The full deterministic result of a hosted indexing request."""

    request_id: str
    repo_id: str
    repo_path: str
    repo_ref: GitHubRepoRef | None
    repo_source_state: RepoSourceState | None
    commit_result: CommitIndexResult
    github_sync_result: GitHubSyncResult | None
    operations: MutationPlan
    operations_applied: int
    observation_operations: MutationPlan = field(default_factory=MutationPlan)
    evidence_operations: MutationPlan = field(default_factory=MutationPlan)
    claim_candidates_extracted: int = 0
    claim_candidates_validated: int = 0
    claim_candidates_rejected: int = 0
    claim_rejection_reasons: dict[str, int] = field(default_factory=dict)
    evidence_invalidated: int = 0
