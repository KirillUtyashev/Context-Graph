"""Shared metadata state models used by backend metadata stores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RepoIndexState:
    """Latest deterministic indexing state for one repository."""

    repo_id: str
    root_path: str
    latest_revision_id: str
    latest_indexed_at: str
    metadata: dict[str, Any]
    latest_commit_sha: str | None = None
    latest_branch: str | None = None
    last_commit_indexed_at: str | None = None


@dataclass(frozen=True, slots=True)
class RepoSourceState:
    """Last known hosted-source state for one repository."""

    repo_id: str
    provider: str
    canonical_repo: str
    clone_url: str
    default_branch: str | None
    clone_path: str
    last_fetch_at: str | None = None
    last_fetched_head_sha: str | None = None
    last_github_sync_at: str | None = None
    last_github_sync_status: str | None = None
    last_github_sync_error: str | None = None
    metadata: dict[str, Any] | None = None
