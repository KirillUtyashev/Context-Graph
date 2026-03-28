from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_graph.ingestion.commit_indexing import default_repo_id
from context_graph.ingestion.git.github_client import (
    GitHubRepoRef,
    GitHubRepoResolver,
    collect_unique_numbers,
    is_github_remote,
    parse_github_repo_input,
)
from context_graph.ingestion.models import HostedIndexRequest
from context_graph.ingestion.repo_cache import RepoCacheManager
from context_graph.ingestion.snapshot_builder import CodebaseMemorySnapshotProducer
from context_graph.ingestion.temporal import TemporalGraphWriter
from context_graph.metadata import InMemoryMetadataStore
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot


def make_snapshot(
    revision_id: str,
    *,
    repo_id: str = "demo",
    root_path: str = "/tmp/demo",
    include_helper: bool = False,
) -> GraphSnapshot:
    project = CanonicalNode(
        node_id=f"project:{repo_id}",
        repo_id=repo_id,
        kind="Project",
        name=repo_id,
        qualified_name=repo_id,
    )
    app_file = CanonicalNode(
        node_id=f"file:{repo_id}:{repo_id}.app.__file__",
        repo_id=repo_id,
        kind="File",
        name="app.py",
        qualified_name=f"{repo_id}.app.__file__",
        file_path="app.py",
    )
    main = CanonicalNode(
        node_id=f"symbol:{repo_id}:{repo_id}.app.main",
        repo_id=repo_id,
        kind="Function",
        name="main",
        qualified_name=f"{repo_id}.app.main",
        file_path="app.py",
        start_line=1,
        end_line=4,
        properties={"version": revision_id},
    )

    nodes = [project, app_file, main]
    edges = [
        CanonicalEdge(
            edge_id=f"edge:{repo_id}:contains",
            repo_id=repo_id,
            edge_type="CONTAINS_FILE",
            source_id=project.node_id,
            target_id=app_file.node_id,
        ),
        CanonicalEdge(
            edge_id=f"edge:{repo_id}:defines-main",
            repo_id=repo_id,
            edge_type="DEFINES",
            source_id=app_file.node_id,
            target_id=main.node_id,
        ),
    ]

    if include_helper:
        helper = CanonicalNode(
            node_id=f"symbol:{repo_id}:{repo_id}.app.helper",
            repo_id=repo_id,
            kind="Function",
            name="helper",
            qualified_name=f"{repo_id}.app.helper",
            file_path="app.py",
            start_line=6,
            end_line=7,
        )
        nodes.append(helper)
        edges.append(
            CanonicalEdge(
                edge_id=f"edge:{repo_id}:calls-helper",
                repo_id=repo_id,
                edge_type="CALLS",
                source_id=main.node_id,
                target_id=helper.node_id,
                properties={"callee_name": "helper"},
            )
        )

    return GraphSnapshot(
        repo_id=repo_id,
        revision_id=revision_id,
        root_path=root_path,
        generated_at="2026-03-27T00:00:00+00:00",
        nodes=tuple(nodes),
        edges=tuple(edges),
        metadata={"source": "unit-test"},
    )


class FakeGitMetadata:
    def __init__(self) -> None:
        self.clone_calls: list[tuple[str, str, str | None, str | None]] = []
        self.fetch_calls: list[tuple[str, str | None]] = []
        self.checkout_calls: list[tuple[str, str]] = []
        self.reset_calls: list[tuple[str, str]] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self.remove_calls: list[str] = []
        self.remotes = {
            "origin": "https://github.com/openai/context-graph.git",
            "upstream": "git@github.com:OpenAI/Context-Graph.git",
        }

    def resolve_repo_root(self, repo_path: str) -> str:
        return str(Path(repo_path).resolve())

    def clone_repo(
        self,
        clone_url: str,
        clone_path: str,
        *,
        branch: str | None,
        auth_token: str | None,
    ) -> None:
        self.clone_calls.append((clone_url, clone_path, branch, auth_token))
        Path(clone_path).mkdir(parents=True, exist_ok=True)

    def fetch_remote(self, repo_path: str, *, auth_token: str | None) -> None:
        self.fetch_calls.append((repo_path, auth_token))

    def checkout_branch(self, repo_path: str, branch: str) -> None:
        self.checkout_calls.append((repo_path, branch))

    def hard_reset(self, repo_path: str, revision: str) -> None:
        self.reset_calls.append((repo_path, revision))

    def get_head_sha(self, repo_path: str) -> str:
        return "deadbeef"

    def resolve_revision(self, repo_path: str, ref: str) -> str:
        self.resolve_calls.append((repo_path, ref))
        if ref == "origin/main":
            return "abc123"
        raise RuntimeError("missing ref")

    def remove_repo(self, repo_path: str) -> None:
        self.remove_calls.append(repo_path)

    def list_remotes(self, repo_path: str) -> dict[str, str]:
        return dict(self.remotes)


class FakeGitHubApi:
    def get_repository(self, owner: str, repo: str) -> dict[str, object]:
        return {
            "owner": {"login": owner.capitalize()},
            "name": repo.upper(),
            "clone_url": f"https://example.test/{owner}/{repo}.git",
            "html_url": f"https://example.test/{owner}/{repo}",
            "default_branch": "main",
        }


class ContextGraphCurrentRepoTests(unittest.TestCase):
    def test_github_input_helpers_cover_slug_url_and_ssh_forms(self) -> None:
        self.assertEqual(parse_github_repo_input("openai/context-graph"), ("openai", "context-graph"))
        self.assertEqual(
            parse_github_repo_input("https://github.com/openai/context-graph.git"),
            ("openai", "context-graph"),
        )
        self.assertEqual(
            parse_github_repo_input("git@github.com:openai/context-graph.git"),
            ("openai", "context-graph"),
        )
        self.assertTrue(is_github_remote("https://github.com/openai/context-graph"))
        self.assertFalse(is_github_remote("https://gitlab.com/openai/context-graph"))
        self.assertEqual(
            collect_unique_numbers(
                [
                    {"number": 7},
                    {"number": 3},
                    {"number": 7},
                    {"number": "9"},
                ]
            ),
            (3, 7),
        )

    def test_github_repo_resolver_prefers_upstream_and_hydrates_repo_metadata(self) -> None:
        resolver = GitHubRepoResolver(
            git_metadata=FakeGitMetadata(),
            github_api=FakeGitHubApi(),
        )

        resolved = resolver.resolve(repo_path="/tmp/repo")

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.source, "git_remote_upstream")
        self.assertEqual(resolved.canonical_repo, "Openai/CONTEXT-GRAPH")
        self.assertEqual(resolved.default_branch, "main")
        self.assertEqual(resolved.clone_url, "https://example.test/OpenAI/Context-Graph.git")

    def test_default_repo_id_and_hosted_request_defaults_match_current_ingestion_shape(self) -> None:
        self.assertEqual(default_repo_id("/tmp/My Project"), "my-project")
        self.assertEqual(default_repo_id("OpenAI/Context.Graph"), "context.graph")

        request = HostedIndexRequest(github_repo="openai/context-graph", execute=True)
        self.assertEqual(request.github_repo, "openai/context-graph")
        self.assertTrue(request.execute)
        self.assertEqual(request.limit, 1000000)

    def test_repo_cache_manager_registers_local_repos_and_resolves_refs(self) -> None:
        git_metadata = FakeGitMetadata()
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir) / "repo"
            repo_path.mkdir()
            cache = RepoCacheManager(base_dir=tmp_dir, git_metadata=git_metadata)

            managed = cache.manage_local_repo(
                str(repo_path),
                repo_id="demo",
                request_id="req-1",
            )

            self.assertEqual(managed.repo_id, "demo")
            self.assertEqual(managed.repo_path, str(repo_path.resolve()))
            self.assertTrue(Path(managed.work_root).exists())
            self.assertEqual(cache.resolve_ref_sha(str(repo_path), "main"), "abc123")
            self.assertEqual(
                git_metadata.resolve_calls,
                [
                    (str(repo_path), "main"),
                    (str(repo_path), "origin/main"),
                ],
            )

    def test_repo_cache_manager_clones_hosted_repo_and_records_source_state(self) -> None:
        git_metadata = FakeGitMetadata()
        repo_ref = GitHubRepoRef(
            owner="openai",
            name="context-graph",
            canonical_repo="openai/context-graph",
            clone_url="https://github.com/openai/context-graph.git",
            html_url="https://github.com/openai/context-graph",
            default_branch="main",
            source="github_repo",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = RepoCacheManager(base_dir=tmp_dir, git_metadata=git_metadata)

            managed = cache.ensure_managed_repo(
                repo_ref,
                repo_id="demo",
                request_id="req-2",
                auth_token="secret-token",
            )

            self.assertEqual(len(git_metadata.clone_calls), 1)
            self.assertEqual(git_metadata.checkout_calls, [])
            self.assertEqual(git_metadata.reset_calls, [])
            self.assertIsNotNone(managed.source_state)
            assert managed.source_state is not None
            self.assertEqual(managed.source_state.canonical_repo, "openai/context-graph")
            self.assertEqual(managed.source_state.last_fetched_head_sha, "deadbeef")
            self.assertIsNone(managed.source_state.default_branch)

            cache.cleanup_request_workspace("demo", "req-2")
            self.assertFalse(Path(managed.work_root).exists())

    def test_snapshot_producer_builds_snapshot_from_export_payload(self) -> None:
        payload = {
            "root_path": "/tmp/demo",
            "project": "Context Graph",
            "node_count": 2,
            "edge_count": 1,
            "nodes": [
                {
                    "temp_id": 1,
                    "label": "Project",
                    "name": "Context Graph",
                    "qualified_name": "context-graph",
                    "file_path": "",
                    "start_line": None,
                    "end_line": None,
                    "properties_json": "{}",
                },
                {
                    "temp_id": 2,
                    "label": "File",
                    "name": "app.py",
                    "qualified_name": "context_graph.app.__file__",
                    "file_path": "app.py",
                    "start_line": None,
                    "end_line": None,
                    "properties_json": "{\"lang\": \"python\"}",
                },
            ],
            "edges": [
                {
                    "source_temp_id": 1,
                    "target_temp_id": 2,
                    "type": "CONTAINS_FILE",
                    "properties_json": "{}",
                }
            ],
        }
        producer = CodebaseMemorySnapshotProducer(root=Path("/tmp/project-root"))
        with patch.object(producer, "export_graph", return_value=payload):
            snapshot = producer.build_snapshot("/tmp/demo", repo_id="demo", revision_id="rev-1")

        self.assertEqual(snapshot.repo_id, "demo")
        self.assertEqual(snapshot.revision_id, "rev-1")
        self.assertEqual(snapshot.root_path, "/tmp/demo")
        self.assertEqual(len(snapshot.nodes), 2)
        self.assertEqual(len(snapshot.edges), 1)
        self.assertEqual(snapshot.metadata["project_name"], "Context Graph")
        file_node = next(node for node in snapshot.nodes if node.kind == "File")
        self.assertEqual(file_node.properties["lang"], "python")

    def test_snapshot_producer_ensures_binary_before_exporting(self) -> None:
        producer = CodebaseMemorySnapshotProducer(root=Path("/tmp/project-root"))
        with patch("pathlib.Path.exists", return_value=False), patch(
            "context_graph.ingestion.snapshot_builder.subprocess.run"
        ) as run_mock:
            producer.ensure_binary()

        run_mock.assert_called_once_with(
            ["make", "repo-graph-export"],
            cwd=producer.vendor_dir,
            check=True,
        )

    def test_temporal_graph_writer_plans_and_finalizes_snapshot_metadata(self) -> None:
        metadata = InMemoryMetadataStore()
        writer = TemporalGraphWriter(metadata_store=metadata, backend_driver=None)

        first = make_snapshot("r1")
        plan = writer.plan_snapshot(first)
        result = writer.apply_planned_snapshot(
            first,
            plan.diff,
            plan.plan,
            execute=False,
        )

        self.assertEqual(result.snapshot.revision_id, "r1")
        self.assertEqual(result.operations_applied, 0)
        self.assertEqual(metadata.load_latest_snapshot("demo"), first)
        state = metadata.load_repo_index_state("demo")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.latest_revision_id, "r1")
        self.assertEqual(metadata._runs[-1]["status"], "applied")

        second = make_snapshot("r2", include_helper=True)
        second_plan = writer.plan_snapshot(second)
        self.assertIn("symbol:demo:demo.app.helper", second_plan.diff.added_nodes)

    def test_snapshot_and_edge_helpers_round_trip_through_json_friendly_shapes(self) -> None:
        snapshot = make_snapshot("r3", include_helper=True)

        snapshot_dict = snapshot.to_dict()
        restored = GraphSnapshot.from_dict(snapshot_dict)

        self.assertEqual(restored.repo_id, snapshot.repo_id)
        self.assertEqual(restored.revision_id, snapshot.revision_id)
        self.assertEqual(len(restored.nodes), len(snapshot.nodes))
        self.assertEqual(len(restored.edges), len(snapshot.edges))
        self.assertEqual(json.loads(snapshot.nodes[2].properties_json())["version"], "r3")


if __name__ == "__main__":
    unittest.main()
