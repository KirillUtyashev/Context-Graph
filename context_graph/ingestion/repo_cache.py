from __future__ import annotations

from pathlib import Path
import shutil

from context_graph.ingestion.git.git_metadata import GitMetadata
from context_graph.ingestion.git.github_client import GitHubRepoRef
from .models import ManagedRepo
from ..models import utc_now_iso
from context_graph.backend import RepoSourceState


class RepoCacheManager:
    """Manages cached hosted clones and per-request workspace directories."""

    def __init__(
        self,
        *,
        base_dir: str = "/mnt/context-graph",
        git_metadata: GitMetadata | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.git_metadata = git_metadata or GitMetadata()
        self.repos_root = self.base_dir / "repos"
        self.work_root = self.base_dir / "work"

    def manage_local_repo(
        self,
        repo_path: str,
        *,
        repo_id: str,
        request_id: str,
    ) -> ManagedRepo:
        """Register an existing local checkout for one indexing request."""

        request_work_root = self._request_work_root(repo_id, request_id)
        request_work_root.mkdir(parents=True, exist_ok=True)
        return ManagedRepo(
            repo_id=repo_id,
            repo_path=self.git_metadata.resolve_repo_root(repo_path),
            request_id=request_id,
            work_root=str(request_work_root),
            repo_ref=None,
            source_state=None,
        )

    def ensure_managed_repo(
        self,
        repo_ref: GitHubRepoRef,
        *,
        repo_id: str,
        request_id: str,
        auth_token: str | None,
        existing_state: RepoSourceState | None = None,
    ) -> ManagedRepo:
        """Ensure a GitHub repo is cloned/fetched and ready for deterministic indexing."""

        clone_path = Path(existing_state.clone_path) if existing_state else self._clone_path(repo_ref)
        self.repos_root.mkdir(parents=True, exist_ok=True)
        request_work_root = self._request_work_root(repo_id, request_id)
        request_work_root.mkdir(parents=True, exist_ok=True)

        self._ensure_clone(repo_ref, clone_path, auth_token=auth_token)
        default_branch = repo_ref.default_branch or existing_state.default_branch if existing_state else None
        if default_branch:
            self.prepare_default_branch(str(clone_path), default_branch)
        source_state = RepoSourceState(
            repo_id=repo_id,
            provider="github",
            canonical_repo=repo_ref.canonical_repo,
            clone_url=repo_ref.clone_url,
            default_branch=default_branch,
            clone_path=str(clone_path),
            last_fetch_at=utc_now_iso(),
            last_fetched_head_sha=self.git_metadata.get_head_sha(str(clone_path)),
            metadata={
                "html_url": repo_ref.html_url,
                "source": repo_ref.source,
            },
        )
        return ManagedRepo(
            repo_id=repo_id,
            repo_path=str(clone_path),
            request_id=request_id,
            work_root=str(request_work_root),
            repo_ref=repo_ref,
            source_state=source_state,
        )

    def prepare_default_branch(self, repo_path: str, branch: str) -> None:
        """Move a cached clone to the remote default branch HEAD."""

        self.git_metadata.checkout_branch(repo_path, branch)
        self.git_metadata.hard_reset(repo_path, f"origin/{branch}")

    def resolve_ref_sha(self, repo_path: str, ref: str) -> str:
        """Resolve a user-provided ref across the common local and remote forms."""

        candidates = (
            ref,
            f"origin/{ref}",
            f"refs/remotes/origin/{ref}",
            f"refs/tags/{ref}",
        )
        for candidate in candidates:
            try:
                return self.git_metadata.resolve_revision(repo_path, candidate)
            except RuntimeError:
                continue
        raise RuntimeError(f"unable to resolve git ref {ref!r}")

    def cleanup_request_workspace(self, repo_id: str, request_id: str) -> None:
        """Best-effort cleanup of the temporary worktree root for one request."""

        work_root = self._request_work_root(repo_id, request_id)
        shutil.rmtree(work_root, ignore_errors=True)

    def _ensure_clone(
        self,
        repo_ref: GitHubRepoRef,
        clone_path: Path,
        *,
        auth_token: str | None,
    ) -> None:
        """Keep the on-disk hosted clone usable, even if it needs recloning."""

        if not clone_path.exists():
            self.git_metadata.clone_repo(
                repo_ref.clone_url,
                str(clone_path),
                branch=repo_ref.default_branch,
                auth_token=auth_token,
            )
            return

        if not self._looks_like_git_repo(clone_path):
            self.git_metadata.remove_repo(str(clone_path))
            self.git_metadata.clone_repo(
                repo_ref.clone_url,
                str(clone_path),
                branch=repo_ref.default_branch,
                auth_token=auth_token,
            )
            return

        try:
            self.git_metadata.fetch_remote(
                str(clone_path),
                auth_token=auth_token,
            )
        except RuntimeError:
            self.git_metadata.remove_repo(str(clone_path))
            self.git_metadata.clone_repo(
                repo_ref.clone_url,
                str(clone_path),
                branch=repo_ref.default_branch,
                auth_token=auth_token,
            )

    def _looks_like_git_repo(self, clone_path: Path) -> bool:
        try:
            resolved = Path(self.git_metadata.resolve_repo_root(str(clone_path)))
        except RuntimeError:
            return False
        return resolved == clone_path.resolve()

    def _clone_path(self, repo_ref: GitHubRepoRef) -> Path:
        return self.repos_root / repo_ref.owner / repo_ref.name

    def _request_work_root(self, repo_id: str, request_id: str) -> Path:
        return self.work_root / repo_id / request_id
