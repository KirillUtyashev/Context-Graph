"""Tests for context_graph.ingestion.git.git_metadata to increase coverage."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from context_graph.ingestion.git.git_metadata import CommitInfo, GitMetadata


def _fake_run(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestGitMetadata:
    def test_run_git_raises_on_failure(self):
        gm = GitMetadata()
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(
            1, "git", stderr="fatal: not a repo"
        )):
            with pytest.raises(RuntimeError, match="git .* failed"):
                gm._run_git("/tmp/fake", ["status"])

    def test_resolve_repo_root(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="/home/user/repo\n")):
            root = gm.resolve_repo_root("/home/user/repo/sub")
        assert "repo" in root

    def test_get_head_sha(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="abc123\n")):
            sha = gm.get_head_sha("/tmp/repo")
        assert sha == "abc123"

    def test_get_head_branch_returns_branch(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="main\n")):
            branch = gm.get_head_branch("/tmp/repo")
        assert branch == "main"

    def test_get_head_branch_returns_none_for_detached(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="")):
            branch = gm.get_head_branch("/tmp/repo")
        assert branch is None

    def test_get_commit_info(self):
        gm = GitMetadata()
        show_output = "abc123\x00parent1 parent2\x00Dev\x00dev@example.com\x002026-01-01T00:00:00\x002026-01-01T00:00:05\x00Add feature\x00body text\n"
        diff_output = "file1.py\nfile2.py\n"
        with patch("subprocess.run", side_effect=[
            _fake_run(stdout=show_output),
            _fake_run(stdout=diff_output),
        ]):
            info = gm.get_commit_info("/tmp/repo", "abc123", branch_hint="main")
        assert info.sha == "abc123"
        assert info.parent_shas == ("parent1", "parent2")
        assert info.branch == "main"
        assert info.author_name == "Dev"
        assert info.changed_files == ("file1.py", "file2.py")

    def test_get_commit_info_bad_output(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="bad output")):
            with pytest.raises(RuntimeError, match="unexpected git show output"):
                gm.get_commit_info("/tmp/repo", "abc123")

    def test_get_commit_info_without_branch_hint(self):
        gm = GitMetadata()
        show_output = "abc123\x00parent1\x00Dev\x00dev@example.com\x002026-01-01\x002026-01-01\x00msg\x00\n"
        # Keyword args in CommitInfo() are evaluated left-to-right, so
        # get_head_branch (for branch=) runs before _list_commit_files (for changed_files=).
        with patch("subprocess.run", side_effect=[
            _fake_run(stdout=show_output),   # git show
            _fake_run(stdout="main\n"),       # get_head_branch
            _fake_run(stdout=""),             # diff-tree (_list_commit_files)
        ]):
            info = gm.get_commit_info("/tmp/repo", "abc123")
        assert info.branch == "main"

    def test_list_changed_files(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="a.py\nb.py\n")):
            files = gm.list_changed_files("/tmp/repo", "old", "new")
        assert files == ("a.py", "b.py")

    def test_resolve_revision(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="deadbeef\n")):
            sha = gm.resolve_revision("/tmp/repo", "HEAD~1")
        assert sha == "deadbeef"

    def test_list_commits_between(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="sha1\nsha2\n")):
            commits = gm.list_commits_between("/tmp/repo", "old", "new")
        assert commits == ("sha1", "sha2")

    def test_list_commits_between_empty_same_sha(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="")):
            commits = gm.list_commits_between("/tmp/repo", "same", "same")
        assert commits == ()

    def test_list_commits_between_empty_different_sha(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="")):
            commits = gm.list_commits_between("/tmp/repo", "old", "new")
        assert commits == ("new",)

    def test_list_first_parent_commits_between(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="sha1\n")):
            commits = gm.list_first_parent_commits_between("/tmp/repo", "old", "new")
        assert commits == ("sha1",)

    def test_list_commit_window(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="sha1\nsha2\n")):
            commits = gm.list_commit_window("/tmp/repo", "sha2", max_count=5)
        assert commits == ("sha1", "sha2")

    def test_list_commit_window_invalid_max_count(self):
        gm = GitMetadata()
        with pytest.raises(ValueError, match="max_count must be >= 1"):
            gm.list_commit_window("/tmp/repo", "sha", max_count=0)

    def test_list_commit_window_no_first_parent(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="sha1\n")) as mock_run:
            gm.list_commit_window("/tmp/repo", "sha1", max_count=3, first_parent=False)
        args = mock_run.call_args[0][0]
        assert "--first-parent" not in args

    def test_is_ancestor_true(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(returncode=0)):
            assert gm.is_ancestor("/tmp/repo", "old", "new") is True

    def test_is_ancestor_false(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(returncode=1)):
            assert gm.is_ancestor("/tmp/repo", "old", "new") is False

    def test_is_ancestor_error(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(returncode=128, stderr="fatal")):
            with pytest.raises(RuntimeError, match="git merge-base"):
                gm.is_ancestor("/tmp/repo", "old", "new")

    def test_is_first_parent_ancestor_same_sha(self):
        gm = GitMetadata()
        assert gm.is_first_parent_ancestor("/tmp/repo", "abc", "abc") is True

    def test_is_first_parent_ancestor_found(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="abc\ndef\n")):
            assert gm.is_first_parent_ancestor("/tmp/repo", "abc", "def") is True

    def test_is_first_parent_ancestor_not_found(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="xyz\n")):
            assert gm.is_first_parent_ancestor("/tmp/repo", "abc", "def") is False

    def test_has_new_commit_none_sha(self):
        gm = GitMetadata()
        assert gm.has_new_commit("/tmp/repo", None) is True

    def test_has_new_commit_same(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="abc\n")):
            assert gm.has_new_commit("/tmp/repo", "abc") is False

    def test_has_new_commit_different(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run(stdout="def\n")):
            assert gm.has_new_commit("/tmp/repo", "abc") is True

    def test_list_remotes(self):
        gm = GitMetadata()
        with patch("subprocess.run", side_effect=[
            _fake_run(stdout="origin\nupstream\n"),
            _fake_run(stdout="https://github.com/owner/repo.git\n"),
            _fake_run(stdout="https://github.com/other/repo.git\n"),
        ]):
            remotes = gm.list_remotes("/tmp/repo")
        assert remotes == {
            "origin": "https://github.com/owner/repo.git",
            "upstream": "https://github.com/other/repo.git",
        }

    def test_clone_repo(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run()) as mock_run:
            with tempfile.TemporaryDirectory() as tmpdir:
                dest = str(Path(tmpdir) / "cloned")
                gm.clone_repo("https://github.com/owner/repo.git", dest, branch="main")
        args = mock_run.call_args[0][0]
        assert "clone" in args
        assert "--branch" in args

    def test_clone_repo_with_auth(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run()) as mock_run:
            with tempfile.TemporaryDirectory() as tmpdir:
                dest = str(Path(tmpdir) / "cloned")
                gm.clone_repo("https://github.com/owner/repo.git", dest, auth_token="tok123")
        args = mock_run.call_args[0][0]
        assert "-c" in args

    def test_fetch_remote(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run()) as mock_run:
            gm.fetch_remote("/tmp/repo", remote="origin", auth_token="tok")
        args = mock_run.call_args[0][0]
        assert "fetch" in args

    def test_checkout_branch(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run()) as mock_run:
            gm.checkout_branch("/tmp/repo", "main")
        args = mock_run.call_args[0][0]
        assert "checkout" in args

    def test_hard_reset(self):
        gm = GitMetadata()
        with patch("subprocess.run", return_value=_fake_run()) as mock_run:
            gm.hard_reset("/tmp/repo", "abc123")
        args = mock_run.call_args[0][0]
        assert "reset" in args and "--hard" in args

    def test_remove_repo(self):
        gm = GitMetadata()
        with patch("shutil.rmtree") as mock_rm:
            gm.remove_repo("/tmp/fake_repo")
        mock_rm.assert_called_once()

    def test_temporary_worktree(self):
        gm = GitMetadata()
        with patch("subprocess.run", side_effect=[
            _fake_run(stdout="/tmp/repo\n"),  # resolve_repo_root
            _fake_run(),  # worktree add
            _fake_run(),  # worktree remove
        ]):
            with patch("tempfile.mkdtemp", return_value="/tmp/wt-temp"):
                with patch("shutil.rmtree"):
                    with gm.temporary_worktree("/tmp/repo", "abc123") as wt:
                        assert "checkout" in wt

    def test_temporary_worktree_with_base_dir(self):
        gm = GitMetadata(worktree_base_dir="/tmp/worktrees")
        with patch("subprocess.run", side_effect=[
            _fake_run(stdout="/tmp/repo\n"),
            _fake_run(),
            _fake_run(),
        ]):
            with patch("tempfile.mkdtemp", return_value="/tmp/worktrees/wt-temp"):
                with patch("shutil.rmtree"):
                    with patch.object(Path, "mkdir"):
                        with gm.temporary_worktree("/tmp/repo", "abc123") as wt:
                            assert "checkout" in wt

    def test_auth_config_args_empty(self):
        gm = GitMetadata()
        assert gm._auth_config_args(None) == []
        assert gm._auth_config_args("") == []

    def test_auth_config_args_with_token(self):
        gm = GitMetadata()
        args = gm._auth_config_args("mytoken")
        assert "-c" in args
        assert any("AUTHORIZATION" in a for a in args)
