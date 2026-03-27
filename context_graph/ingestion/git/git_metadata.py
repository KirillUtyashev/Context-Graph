from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterator, Mapping


@dataclass(frozen=True, slots=True)
class CommitInfo:
    sha: str
    parent_shas: tuple[str, ...]
    branch: str | None
    author_name: str
    author_email: str
    authored_at: str
    committed_at: str
    subject: str
    body: str
    changed_files: tuple[str, ...]


class GitMetadata:
    def __init__(self, *, worktree_base_dir: str | None = None) -> None:
        self.worktree_base_dir = worktree_base_dir

    def _run_git(
        self,
        repo_path: str,
        args: list[str],
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=check,
                env=dict(env) if env is not None else None,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise RuntimeError(f"git {' '.join(args)} failed for {repo_path}: {detail}") from exc

    def resolve_repo_root(self, repo_path: str) -> str:
        completed = self._run_git(repo_path, ["rev-parse", "--show-toplevel"])
        return str(Path(completed.stdout.strip()).resolve())

    def get_head_sha(self, repo_path: str) -> str:
        completed = self._run_git(repo_path, ["rev-parse", "HEAD"])
        return completed.stdout.strip()

    def get_head_branch(self, repo_path: str) -> str | None:
        completed = self._run_git(repo_path, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
        branch = completed.stdout.strip()
        return branch or None

    def get_commit_info(
        self,
        repo_path: str,
        sha: str = "HEAD",
        *,
        branch_hint: str | None = None,
    ) -> CommitInfo:
        completed = self._run_git(
            repo_path,
            ["show", "-s", "--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%cI%x00%s%x00%b", sha],
        )
        parts = completed.stdout.rstrip("\n").split("\x00")
        if len(parts) != 8:
            raise RuntimeError(f"unexpected git show output for {sha!r} in {repo_path}")

        commit_sha, parents, author_name, author_email, authored_at, committed_at, subject, body = parts
        return CommitInfo(
            sha=commit_sha,
            parent_shas=tuple(item for item in parents.split() if item),
            branch=branch_hint if branch_hint is not None else self.get_head_branch(repo_path),
            author_name=author_name,
            author_email=author_email,
            authored_at=authored_at,
            committed_at=committed_at,
            subject=subject,
            body=body.strip(),
            changed_files=self._list_commit_files(repo_path, commit_sha),
        )

    def _list_commit_files(self, repo_path: str, sha: str) -> tuple[str, ...]:
        completed = self._run_git(
            repo_path,
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha],
        )
        return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())

    def list_changed_files(self, repo_path: str, old_sha: str, new_sha: str) -> tuple[str, ...]:
        completed = self._run_git(repo_path, ["diff", "--name-only", old_sha, new_sha])
        return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())

    def resolve_revision(self, repo_path: str, revision: str) -> str:
        completed = self._run_git(repo_path, ["rev-parse", revision])
        return completed.stdout.strip()

    def list_commits_between(self, repo_path: str, old_sha: str, new_sha: str) -> tuple[str, ...]:
        completed = self._run_git(repo_path, ["rev-list", "--reverse", f"{old_sha}..{new_sha}"])
        commits = tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())
        return commits or (() if old_sha == new_sha else (new_sha,))

    def list_first_parent_commits_between(
        self,
        repo_path: str,
        old_sha: str,
        new_sha: str,
    ) -> tuple[str, ...]:
        completed = self._run_git(
            repo_path,
            ["rev-list", "--reverse", "--first-parent", f"{old_sha}..{new_sha}"],
        )
        commits = tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())
        return commits or (() if old_sha == new_sha else (new_sha,))

    def list_commit_window(
        self,
        repo_path: str,
        end_sha: str,
        *,
        max_count: int,
        first_parent: bool = True,
    ) -> tuple[str, ...]:
        """Return up to ``max_count`` commits ending at ``end_sha``.

        The returned sequence is ordered from oldest to newest so callers can
        replay the window safely through deterministic indexing. We use
        ``--first-parent`` by default because hosted replay wants a short,
        interpretable linear history ending at the requested commit rather than
        every side-branch ancestor reachable from a merge commit.
        """

        if max_count < 1:
            raise ValueError("max_count must be >= 1")
        args = ["rev-list", "--max-count", str(max_count), "--reverse"]
        if first_parent:
            args.append("--first-parent")
        args.append(end_sha)
        completed = self._run_git(repo_path, args)
        commits = tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())
        return commits or (end_sha,)

    def is_ancestor(self, repo_path: str, ancestor_sha: str, descendant_sha: str) -> bool:
        completed = self._run_git(
            repo_path,
            ["merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
            check=False,
        )
        if completed.returncode == 0:
            return True
        if completed.returncode == 1:
            return False
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise RuntimeError(
            f"git merge-base --is-ancestor failed for {repo_path}: {detail}"
        )

    def is_first_parent_ancestor(self, repo_path: str, ancestor_sha: str, descendant_sha: str) -> bool:
        if ancestor_sha == descendant_sha:
            return True
        completed = self._run_git(
            repo_path,
            ["rev-list", "--first-parent", descendant_sha],
        )
        return ancestor_sha in {
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        }

    def has_new_commit(self, repo_path: str, last_indexed_sha: str | None) -> bool:
        if last_indexed_sha is None:
            return True
        return self.get_head_sha(repo_path) != last_indexed_sha

    def list_remotes(self, repo_path: str) -> dict[str, str]:
        completed = self._run_git(repo_path, ["remote"])
        remotes = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return {
            remote: self.get_remote_url(repo_path, remote)
            for remote in remotes
        }

    def get_remote_url(self, repo_path: str, remote: str) -> str:
        completed = self._run_git(repo_path, ["remote", "get-url", remote])
        return completed.stdout.strip()

    def clone_repo(
        self,
        clone_url: str,
        destination_path: str,
        *,
        branch: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        destination = Path(destination_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        args = [
            *self._auth_config_args(auth_token),
            "clone",
        ]
        if branch:
            args.extend(["--branch", branch])
        args.extend([clone_url, str(destination)])
        self._run_git(str(destination.parent), args)

    def fetch_remote(
        self,
        repo_path: str,
        *,
        remote: str = "origin",
        auth_token: str | None = None,
    ) -> None:
        self._run_git(
            repo_path,
            [
                *self._auth_config_args(auth_token),
                "fetch",
                "--prune",
                "--tags",
                remote,
                f"+refs/heads/*:refs/remotes/{remote}/*",
            ],
        )

    def checkout_branch(self, repo_path: str, branch: str, *, remote: str = "origin") -> None:
        self._run_git(
            repo_path,
            [
                "checkout",
                "--force",
                "-B",
                branch,
                f"{remote}/{branch}",
            ],
        )

    def hard_reset(self, repo_path: str, target_rev: str) -> None:
        self._run_git(repo_path, ["reset", "--hard", target_rev])

    def remove_repo(self, repo_path: str) -> None:
        shutil.rmtree(repo_path, ignore_errors=True)

    @contextmanager
    def temporary_worktree(self, repo_path: str, sha: str) -> Iterator[str]:
        repo_root = self.resolve_repo_root(repo_path)
        if self.worktree_base_dir:
            base_dir = Path(self.worktree_base_dir)
            base_dir.mkdir(parents=True, exist_ok=True)
            temp_root = Path(tempfile.mkdtemp(prefix="context-graph-worktree-", dir=base_dir))
        else:
            temp_root = Path(tempfile.mkdtemp(prefix="context-graph-worktree-"))
        worktree_path = temp_root / "checkout"
        self._run_git(repo_root, ["worktree", "add", "--detach", str(worktree_path), sha])
        try:
            yield str(worktree_path)
        finally:
            try:
                self._run_git(repo_root, ["worktree", "remove", "--force", str(worktree_path)], check=False)
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)

    def _auth_config_args(self, auth_token: str | None) -> list[str]:
        if not auth_token:
            return []
        encoded = base64.b64encode(f"x-access-token:{auth_token}".encode("utf-8")).decode("ascii")
        return ["-c", f"http.extraheader=AUTHORIZATION: basic {encoded}"]
