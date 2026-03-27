from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from context_graph.ingestion.deterministic_planner import DeterministicMutationPlanner
import context_graph.ingestion.deterministic_planner as planner_mod
from context_graph.ingestion.diffing import diff_snapshots
from context_graph.ingestion.git.git_metadata import CommitInfo, GitMetadata
from context_graph.ingestion.snapshot_parsing import build_snapshot_from_graph_export
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot, NodeChange, SnapshotDiff


def _snapshot(revision_id: str, *, function_name: str = "run", include_class: bool = True) -> GraphSnapshot:
    nodes = [
        CanonicalNode("project:repo-1", "repo-1", "Project", "repo", properties={"root_path": "/tmp/repo"}),
        CanonicalNode("folder:repo-1:src", "repo-1", "Folder", "src", qualified_name="repo-1.src", file_path="src"),
        CanonicalNode("file:repo-1:src/main.py:main.py", "repo-1", "File", "main.py", file_path="src/main.py"),
        CanonicalNode(
            "function:repo-1:pkg.module.run",
            "repo-1",
            "Function",
            function_name,
            qualified_name="pkg.module.run",
            file_path="src/main.py",
            properties={"arity": 1},
        ),
    ]
    if include_class:
        nodes.append(
            CanonicalNode(
                "class:repo-1:pkg.module.Runner",
                "repo-1",
                "Class",
                "Runner",
                qualified_name="pkg.module.Runner",
                file_path="src/main.py",
            )
        )

    edges = [
        CanonicalEdge(
            "edge:repo-1:CONTAINS:file:repo-1:src/main.py:main.py:function:repo-1:pkg.module.run",
            "repo-1",
            "CONTAINS",
            "file:repo-1:src/main.py:main.py",
            "function:repo-1:pkg.module.run",
        )
    ]
    return GraphSnapshot(
        repo_id="repo-1",
        revision_id=revision_id,
        root_path="/tmp/repo",
        generated_at="2026-03-27T12:00:00+00:00",
        nodes=tuple(nodes),
        edges=tuple(edges),
        metadata={"source": "test"},
    )


def test_build_snapshot_from_graph_export_creates_stable_snapshot() -> None:
    snapshot = build_snapshot_from_graph_export(
        {
            "project": "Context Graph",
            "root_path": "/tmp/repo",
            "node_count": 3,
            "edge_count": 2,
            "nodes": [
                {"temp_id": 1, "label": "Project", "name": "Context Graph", "properties_json": '{"lang":"py"}'},
                {"temp_id": 2, "label": "File", "name": "main.py", "file_path": "./src/main.py", "properties_json": '{"size":10}'},
                {
                    "temp_id": 3,
                    "label": "Function",
                    "name": "run",
                    "qualified_name": "pkg.module.run",
                    "file_path": "./src/main.py",
                    "start_line": 3,
                    "end_line": 9,
                    "properties_json": '["unexpected"]',
                },
            ],
            "edges": [
                {"source_temp_id": 2, "target_temp_id": 3, "type": "CONTAINS", "properties_json": '{"weight":1}'},
                {"source_temp_id": 404, "target_temp_id": 3, "type": "BROKEN", "properties_json": "{}"},
            ],
        },
        revision_id="rev-1",
    )

    assert snapshot.repo_id == "context-graph"
    assert snapshot.metadata["export_node_count"] == 3
    assert any(node.kind == "Project" and node.properties["root_path"] == "/tmp/repo" for node in snapshot.nodes)
    function_node = next(node for node in snapshot.nodes if node.kind == "Function")
    assert function_node.properties == {"value": ["unexpected"]}
    assert len(snapshot.edges) == 1

    custom_repo_snapshot = build_snapshot_from_graph_export(
        {
            "project": "Context Graph",
            "root_path": "/tmp/repo",
            "nodes": [
                {"temp_id": 1, "label": "Project", "name": "Context Graph", "properties_json": None},
                {"temp_id": 2, "label": "File", "name": "bad.py", "file_path": "bad.py", "properties_json": "{oops"},
            ],
            "edges": [],
        },
        repo_id="custom-repo",
        revision_id="rev-2",
    )
    assert custom_repo_snapshot.repo_id == "custom-repo"
    bad_node = next(node for node in custom_repo_snapshot.nodes if node.kind == "File")
    assert bad_node.properties == {"raw_properties_json": "{oops"}


def test_diff_snapshots_and_deterministic_planner_capture_changes() -> None:
    previous = _snapshot("rev-1")
    current = _snapshot("rev-2", function_name="execute", include_class=False)
    diff = diff_snapshots(previous, current)

    assert list(diff.changed_nodes) == ["function:repo-1:pkg.module.run"]
    assert list(diff.removed_nodes) == ["class:repo-1:pkg.module.Runner"]
    assert diff.added_edges == {}

    planner = DeterministicMutationPlanner()
    initial = planner.plan_snapshot_diff(previous_snapshot=None, snapshot=previous, diff=diff_snapshots(None, previous))
    planned = planner.plan_snapshot_diff(previous_snapshot=initial.snapshot, snapshot=current, diff=diff)

    assert planned.state.active_node_versions["function:repo-1:pkg.module.run"].endswith("@rev-2")
    assert planned.state.closed_node_versions["function:repo-1:pkg.module.run"].endswith("@rev-1")
    assert planned.state.closed_node_versions["class:repo-1:pkg.module.Runner"].endswith("@rev-1")
    assert any(record.record_type == "project" for record in planned.plan.upsert_nodes)
    assert any(record.record_type == "has_version" for record in planned.plan.upsert_edges)
    assert any(record.record_type == "code_node" for record in planned.plan.upsert_search_documents)

    commit = CommitInfo(
        sha="abc1234",
        parent_shas=("def5678",),
        branch="main",
        author_name="Kirill",
        author_email="kirill@example.test",
        authored_at="2026-03-27T12:00:00+00:00",
        committed_at="2026-03-27T12:05:00+00:00",
        subject="Update function",
        body="Body",
        changed_files=("src/main.py",),
    )
    provenance = planner.plan_commit_provenance(
        snapshot=planned.snapshot,
        diff=diff,
        commit=commit,
        planning_state=planned.state,
    )

    edge_types = {edge.record_type for edge in provenance.upsert_edges}
    assert "has_commit" in edge_types
    assert "precedes" in edge_types
    assert "touched" in edge_types
    assert "introduced_version" in edge_types
    assert "invalidated_version" in edge_types

    merged = planner.merge_plans(initial.plan, planned.plan)
    assert merged.operation_count() >= planned.plan.operation_count()


def test_git_metadata_parses_git_outputs_and_edge_cases() -> None:
    metadata = GitMetadata(worktree_base_dir="/tmp/context-graph")

    outputs = {
        ("rev-parse", "--show-toplevel"): subprocess.CompletedProcess([], 0, stdout="/repo\n", stderr=""),
        ("rev-parse", "HEAD"): subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr=""),
        ("symbolic-ref", "--short", "-q", "HEAD"): subprocess.CompletedProcess([], 0, stdout="main\n", stderr=""),
        (
            "show",
            "-s",
            "--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%cI%x00%s%x00%b",
            "HEAD",
        ): subprocess.CompletedProcess(
            [],
            0,
            stdout="abc123\x00def456 ghi789\x00Kirill\x00kirill@example.test\x002026-03-27T12:00:00+00:00\x002026-03-27T12:05:00+00:00\x00Subject\x00Body\n",
            stderr="",
        ),
        ("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "abc123"): subprocess.CompletedProcess(
            [], 0, stdout="src/main.py\nREADME.md\n", stderr=""
        ),
        ("diff", "--name-only", "old", "new"): subprocess.CompletedProcess([], 0, stdout="src/main.py\n", stderr=""),
        ("rev-list", "--reverse", "old..new"): subprocess.CompletedProcess([], 0, stdout="a\nb\n", stderr=""),
        ("rev-list", "--reverse", "same..same"): subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ("rev-list", "--reverse", "--first-parent", "old..new"): subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ("rev-list", "--reverse", "--first-parent", "same..same"): subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ("rev-list", "--first-parent", "b"): subprocess.CompletedProcess([], 0, stdout="b\na\n", stderr=""),
        ("rev-list", "--max-count", "2", "--reverse", "--first-parent", "HEAD"): subprocess.CompletedProcess(
            [], 0, stdout="a\nb\n", stderr=""
        ),
        ("rev-list", "--max-count", "2", "--reverse", "HEAD"): subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        ),
        ("merge-base", "--is-ancestor", "zero", "new"): subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ("merge-base", "--is-ancestor", "old", "new"): subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        ("remote",): subprocess.CompletedProcess([], 0, stdout="origin\nupstream\n", stderr=""),
        ("remote", "get-url", "origin"): subprocess.CompletedProcess([], 0, stdout="git@example.test:origin.git\n", stderr=""),
        ("remote", "get-url", "upstream"): subprocess.CompletedProcess([], 0, stdout="git@example.test:upstream.git\n", stderr=""),
    }

    def fake_run_git(repo_path: str, args: list[str], *, check: bool = True, env=None):
        key = tuple(args)
        if key in outputs:
            return outputs[key]
        raise AssertionError(f"unexpected git call: {args!r}")

    with patch.object(metadata, "_run_git", side_effect=fake_run_git):
        assert metadata.resolve_repo_root("/repo") == "/repo"
        assert metadata.get_head_sha("/repo") == "abc123"
        assert metadata.get_head_branch("/repo") == "main"
        assert metadata.resolve_revision("/repo", "HEAD") == "abc123"
        commit = metadata.get_commit_info("/repo")
        assert commit.parent_shas == ("def456", "ghi789")
        assert commit.changed_files == ("src/main.py", "README.md")
        assert metadata.list_changed_files("/repo", "old", "new") == ("src/main.py",)
        assert metadata.list_commits_between("/repo", "old", "new") == ("a", "b")
        assert metadata.list_first_parent_commits_between("/repo", "old", "new") == ("new",)
        assert metadata.list_commit_window("/repo", "HEAD", max_count=2) == ("a", "b")
        assert metadata.list_commit_window("/repo", "HEAD", max_count=2, first_parent=False) == ("HEAD",)
        assert metadata.list_commits_between("/repo", "same", "same") == ()
        assert metadata.list_first_parent_commits_between("/repo", "same", "same") == ()
        assert metadata.is_ancestor("/repo", "zero", "new") is True
        assert metadata.is_ancestor("/repo", "old", "new") is False
        assert metadata.is_first_parent_ancestor("/repo", "a", "b") is True
        assert metadata.is_first_parent_ancestor("/repo", "b", "b") is True
        assert metadata.has_new_commit("/repo", None) is True
        assert metadata.has_new_commit("/repo", "old") is True
        assert metadata.has_new_commit("/repo", "abc123") is False
        assert metadata.list_remotes("/repo") == {
            "origin": "git@example.test:origin.git",
            "upstream": "git@example.test:upstream.git",
        }
        assert metadata._auth_config_args("secret")[0] == "-c"

    with pytest.raises(ValueError, match="max_count must be >= 1"):
        metadata.list_commit_window("/repo", "HEAD", max_count=0)

    bad_show = subprocess.CompletedProcess([], 0, stdout="too-short", stderr="")
    with patch.object(metadata, "_run_git", return_value=bad_show):
        with pytest.raises(RuntimeError, match="unexpected git show output"):
            metadata.get_commit_info("/repo")

    error = subprocess.CalledProcessError(1, ["git", "status"], stderr="bad news\n")
    with patch("subprocess.run", side_effect=error):
        with pytest.raises(RuntimeError, match="git status failed"):
            metadata._run_git("/repo", ["status"])

    merge_fail = subprocess.CompletedProcess([], 2, stdout="", stderr="merge failed")
    with patch.object(metadata, "_run_git", return_value=merge_fail):
        with pytest.raises(RuntimeError, match="merge-base --is-ancestor failed"):
            metadata.is_ancestor("/repo", "a", "b")

    called: list[tuple[str, tuple[str, ...]]] = []

    def record_run_git(repo_path: str, args: list[str], *, check: bool = True, env=None):
        called.append((repo_path, tuple(args)))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch.object(metadata, "_run_git", side_effect=record_run_git):
        metadata.clone_repo("https://example.test/repo.git", "/tmp/repo", branch="main", auth_token="secret")
        metadata.fetch_remote("/repo", remote="origin", auth_token="secret")
        metadata.checkout_branch("/repo", "main")
        metadata.hard_reset("/repo", "HEAD")

    assert any("clone" in args for _, args in called)
    assert any("fetch" in args for _, args in called)
    assert any("checkout" in args for _, args in called)
    assert any("reset" in args for _, args in called)

    with patch("shutil.rmtree") as rmtree:
        metadata.remove_repo("/tmp/repo")
    rmtree.assert_called_once_with("/tmp/repo", ignore_errors=True)

    worktree_calls: list[tuple[str, tuple[str, ...], bool]] = []

    def worktree_run(repo_path: str, args: list[str], *, check: bool = True, env=None):
        worktree_calls.append((repo_path, tuple(args), check))
        if tuple(args) == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(args, 0, stdout="/repo\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch.object(metadata, "_run_git", side_effect=worktree_run), patch("tempfile.mkdtemp", return_value="/tmp/wt"), patch("shutil.rmtree") as rmtree:
        with metadata.temporary_worktree("/repo", "abc123") as worktree:
            assert worktree == "/tmp/wt/checkout"
    assert any(args[:3] == ("worktree", "add", "--detach") for _, args, _ in worktree_calls)
    assert any(args[:3] == ("worktree", "remove", "--force") for _, args, _ in worktree_calls)
    rmtree.assert_called_with(Path("/tmp/wt"), ignore_errors=True)

    metadata_no_base = GitMetadata()
    worktree_calls = []
    with patch.object(metadata_no_base, "_run_git", side_effect=worktree_run), patch("tempfile.mkdtemp", return_value="/tmp/wt2"), patch("shutil.rmtree") as rmtree:
        with metadata_no_base.temporary_worktree("/repo", "abc123") as worktree:
            assert worktree == "/tmp/wt2/checkout"
    rmtree.assert_called_with(Path("/tmp/wt2"), ignore_errors=True)
    assert metadata._auth_config_args(None) == []


def test_planner_private_helpers_cover_remaining_branches() -> None:
    project = CanonicalNode("project:repo", "repo", "Project", "repo")
    file_node = CanonicalNode("file:repo:src/main.py:main.py", "repo", "File", "main.py", file_path="src/main.py")
    func_old = CanonicalNode("function:repo:pkg.run", "repo", "Function", "run", qualified_name="pkg.run", file_path="src/main.py")
    func_new = CanonicalNode("function:repo:pkg.run", "repo", "Function", "execute", qualified_name="pkg.run", file_path="src/main.py")
    edge_old = CanonicalEdge("edge:repo:CALLS:a:b", "repo", "CALLS", "a", "b")
    edge_new = CanonicalEdge("edge:repo:CALLS:a:b", "repo", "CALLS", "a", "c")
    snapshot = GraphSnapshot("repo", "rev-2", "/tmp/repo", "2026-03-27T12:00:00+00:00", (project, file_node, func_new), (edge_new,), {})
    previous = GraphSnapshot(
        "repo",
        "rev-1",
        "/tmp/repo",
        "2026-03-27T11:00:00+00:00",
        (project, file_node, func_old),
        (edge_old,),
        {
            "deterministic_mutation_state": {
                "active_node_versions": {"function:repo:pkg.run": "function:repo:pkg.run@rev-1"},
                "active_edge_versions": {"edge:repo:CALLS:a:b": "edge:repo:CALLS:a:b@rev-1"},
            }
        },
    )
    diff = diff_snapshots(previous, snapshot)
    planner = DeterministicMutationPlanner()
    plan = planner.plan_snapshot_diff(previous_snapshot=previous, snapshot=snapshot, diff=diff)
    assert any(edge.properties.get("valid_to_revision") == "rev-2" for edge in plan.plan.upsert_edges)

    snapshot_with_project_add = GraphSnapshot(
        "repo",
        "rev-3",
        "/tmp/repo",
        "2026-03-27T13:00:00+00:00",
        (project, file_node, func_new),
        (edge_new,),
        {},
    )
    previous_with_missing_versions = GraphSnapshot(
        "repo",
        "rev-2",
        "/tmp/repo",
        "2026-03-27T12:30:00+00:00",
        (project, file_node, func_old),
        (edge_old,),
        {"deterministic_mutation_state": {"active_node_versions": {}, "active_edge_versions": {}}},
    )
    diff_missing_versions = diff_snapshots(previous_with_missing_versions, snapshot_with_project_add)
    sparse_plan = planner.plan_snapshot_diff(
        previous_snapshot=previous_with_missing_versions,
        snapshot=snapshot_with_project_add,
        diff=diff_missing_versions,
    )
    assert sparse_plan.state.closed_node_versions == {}
    assert sparse_plan.state.closed_edge_versions == {}

    commit = CommitInfo(
        sha="abc",
        parent_shas=(),
        branch="main",
        author_name="A",
        author_email="a@example.test",
        authored_at="2026-03-27T12:00:00+00:00",
        committed_at="2026-03-27T12:00:01+00:00",
        subject="Subject",
        body="Body",
        changed_files=("src/main.py",),
    )

    initial_diff = diff_snapshots(None, GraphSnapshot("repo", "rev-1", "/tmp/repo", "t", (project, file_node), (), {}))
    assert planner._introduced_version_rows(
        snapshot=GraphSnapshot("repo", "rev-1", "/tmp/repo", "t", (project, file_node), (), {}),
        diff=initial_diff,
        commit=commit,
        active_node_versions={"file:repo:src/main.py:main.py@skip": "x", "file:repo:src/main.py:main.py": "file:repo:src/main.py:main.py@rev-1"},
    )
    assert planner._introduced_version_rows(
        snapshot=snapshot,
        diff=SnapshotDiff(
            repo_id="repo",
            previous_revision_id="rev-1",
            new_revision_id="rev-2",
            added_nodes={"project:repo": project},
            changed_nodes={},
            removed_nodes={},
            added_edges={},
            changed_edges={},
            removed_edges={},
        ),
        commit=commit,
        active_node_versions={},
    ) == []
    added_rows = planner._introduced_version_rows(
        snapshot=snapshot,
        diff=SnapshotDiff(
            repo_id="repo",
            previous_revision_id="rev-1",
            new_revision_id="rev-2",
            added_nodes={"file:repo:src/main.py:main.py": file_node},
            changed_nodes={},
            removed_nodes={},
            added_edges={},
            changed_edges={},
            removed_edges={},
        ),
        commit=commit,
        active_node_versions={"file:repo:src/main.py:main.py": "file:repo:src/main.py:main.py@rev-2"},
    )
    assert added_rows == [
        {
            "repo_id": "repo",
            "commit_sha": "abc",
            "node_id": "file:repo:src/main.py:main.py",
            "change_kind": "added",
            "version_id": "file:repo:src/main.py:main.py@rev-2",
        }
    ]
    assert planner._introduced_version_rows(
        snapshot=snapshot,
        diff=SnapshotDiff(
            repo_id="repo",
            previous_revision_id="rev-1",
            new_revision_id="rev-2",
            added_nodes={},
            changed_nodes={diff.changed_nodes["function:repo:pkg.run"].new.node_id: diff.changed_nodes["function:repo:pkg.run"]},
            removed_nodes={},
            added_edges={},
            changed_edges={},
            removed_edges={},
        ),
        commit=commit,
        active_node_versions={},
    ) == []
    project_only_rows = planner._touched_node_rows(
        snapshot=snapshot,
        diff=SnapshotDiff(
            repo_id="repo",
            previous_revision_id="rev-1",
            new_revision_id="rev-2",
            added_nodes={},
            changed_nodes={"project:repo": NodeChange(old=project, new=project)},
            removed_nodes={"project:repo": project},
            added_edges={},
            changed_edges={},
            removed_edges={},
        ),
        commit=commit,
    )
    assert len(project_only_rows) == 1
    assert project_only_rows[0]["change_kind"] == "changed"
    assert planner._invalidated_version_rows(diff=initial_diff, commit=commit, closed_node_versions={}) == []
    assert planner._invalidated_version_rows(
        diff=SnapshotDiff(
            repo_id="repo",
            previous_revision_id="rev-1",
            new_revision_id="rev-2",
            added_nodes={},
            changed_nodes={diff.changed_nodes["function:repo:pkg.run"].old.node_id: diff.changed_nodes["function:repo:pkg.run"]},
            removed_nodes={"project:repo": project},
            added_edges={},
            changed_edges={},
            removed_edges={},
        ),
        commit=commit,
        closed_node_versions={},
    ) == []
    assert planner._touched_node_rows(snapshot=snapshot, diff=diff, commit=commit)

    with pytest.raises(ValueError, match="snapshot must contain a Project node"):
        planner_mod._require_project_node(GraphSnapshot("repo", "r", "/tmp", "t", (file_node,), (), {}))

    state = planner_mod._state_from_snapshot(previous)
    assert state.active_edge_versions["edge:repo:CALLS:a:b"] == "edge:repo:CALLS:a:b@rev-1"
    snapshot_with_state = planner_mod._snapshot_with_state(snapshot, state)
    assert "deterministic_mutation_state" in snapshot_with_state.metadata
    assert planner_mod._normalized_rel_path("./src/main.py") == "src/main.py"
    assert planner_mod._slug("CALLS-EDGE") == "calls_edge"
    assert planner_mod._normalized_text(None) == ""
    assert planner_mod._search_text(" A ", None, "B") == "A B"
    assert planner_mod._commit_record_id("repo", "abc") == "commit:repo:abc"
    assert planner_mod._version_record_id("node", "rev") == "node@rev"
    assert planner_mod._edge_version_record_id("edge", "rev") == "edge@rev"
    assert planner_mod._sorted_nodes([func_new, file_node])[0].node_id == "file:repo:src/main.py:main.py"
    assert planner_mod._sorted_edges([edge_new])[0].edge_id == edge_new.edge_id
    assert planner_mod._sorted_node_changes([diff.changed_nodes["function:repo:pkg.run"]])[0].new.name == "execute"
    assert planner_mod._sorted_edge_changes([diff.changed_edges["edge:repo:CALLS:a:b"]])[0].new.target_id == "c"
