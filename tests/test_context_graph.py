from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from context_graph.cli import main as cli_main
from context_graph.commit_indexer import CommitIndexer
from context_graph.diffing import diff_snapshots
from context_graph.episode_processor import EpisodeProcessor, EpisodeWorkItem
from context_graph.episodes import EpisodeRecord
from context_graph.git_metadata import GitMetadata
from context_graph.github_client import (
    GitHubApiClient,
    GitHubRepoRef,
    GitHubRepoResolver,
    collect_unique_numbers,
    parse_github_repo_input,
)
from context_graph.graph_read import GraphReadRepository
from context_graph.graphiti_adapter import GraphitiAdapter
from context_graph.graph_sinks import NeptuneGraphSink, Neo4jGraphSink
from context_graph.hosted_indexer import HostedIndexRequest, HostedIndexer
from context_graph.llm import EpisodeLLMSettings, load_episode_llm_settings
from context_graph.metadata import InMemoryMetadataStore, RepoIndexState, RepoSourceState, SQLiteMetadataStore
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.repo_cache import RepoCacheManager
from context_graph.snapshot_builder import build_snapshot_from_graph_export
from context_graph.temporal import TemporalGraphWriter


def make_snapshot(
    revision_id: str,
    *,
    repo_id: str = "demo",
    root_path: str = "/tmp/demo",
    include_extra_call: bool = False,
) -> GraphSnapshot:
    node_repo = CanonicalNode(f"project:{repo_id}", repo_id, "Project", repo_id, qualified_name=repo_id)
    node_file = CanonicalNode(
        f"file:{repo_id}:{repo_id}.app.__file__",
        repo_id,
        "File",
        "app.py",
        qualified_name=f"{repo_id}.app.__file__",
        file_path="app.py",
    )
    node_func = CanonicalNode(
        f"symbol:{repo_id}:{repo_id}.app.main",
        repo_id,
        "Function",
        "main",
        qualified_name=f"{repo_id}.app.main",
        file_path="app.py",
        start_line=1,
        end_line=5,
        properties={"version": revision_id},
    )
    node_helper = CanonicalNode(
        f"symbol:{repo_id}:{repo_id}.app.helper",
        repo_id,
        "Function",
        "helper",
        qualified_name=f"{repo_id}.app.helper",
        file_path="app.py",
        start_line=7,
        end_line=8,
    )
    edges = [
        CanonicalEdge(
            f"edge:{repo_id}:CONTAINS_FILE:project:{repo_id}:file:{repo_id}:{repo_id}.app.__file__",
            repo_id,
            "CONTAINS_FILE",
            node_repo.node_id,
            node_file.node_id,
        ),
        CanonicalEdge(
            f"edge:{repo_id}:DEFINES:file:{repo_id}:{repo_id}.app.__file__:symbol:{repo_id}:{repo_id}.app.main",
            repo_id,
            "DEFINES",
            node_file.node_id,
            node_func.node_id,
        ),
    ]
    if include_extra_call:
        edges.append(
            CanonicalEdge(
                f"edge:{repo_id}:CALLS:symbol:{repo_id}:{repo_id}.app.main:symbol:{repo_id}:{repo_id}.app.helper",
                repo_id,
                "CALLS",
                node_func.node_id,
                node_helper.node_id,
                properties={"callee_name": "helper"},
            )
        )
        nodes = (node_repo, node_file, node_func, node_helper)
    else:
        nodes = (node_repo, node_file, node_func)
    return GraphSnapshot(
        repo_id=repo_id,
        revision_id=revision_id,
        root_path=root_path,
        generated_at="2026-03-20T00:00:00+00:00",
        nodes=nodes,
        edges=tuple(edges),
        metadata={},
    )


def _git(repo_path: str, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def create_git_repo(root_dir: str) -> tuple[str, str, str]:
    repo_path = os.path.join(root_dir, "repo")
    os.mkdir(repo_path)
    _git(repo_path, "init")
    _git(repo_path, "config", "user.name", "Context Graph Test")
    _git(repo_path, "config", "user.email", "context-graph@example.com")

    Path(repo_path, "app.py").write_text(
        "def main():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _git(repo_path, "add", "app.py")
    _git(repo_path, "commit", "-m", "initial app")
    first_sha = _git(repo_path, "rev-parse", "HEAD")

    Path(repo_path, "app.py").write_text(
        "def helper():\n"
        "    return 1\n\n"
        "def main():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    _git(repo_path, "add", "app.py")
    _git(repo_path, "commit", "-m", "add helper call")
    second_sha = _git(repo_path, "rev-parse", "HEAD")

    return repo_path, first_sha, second_sha


def create_git_repo_with_deletions(root_dir: str) -> tuple[str, str, str, str]:
    repo_path, first_sha, second_sha = create_git_repo(root_dir)

    Path(repo_path, "legacy.py").write_text(
        "def legacy():\n"
        "    return 'legacy'\n",
        encoding="utf-8",
    )
    _git(repo_path, "add", "legacy.py")
    _git(repo_path, "commit", "--amend", "--no-edit")
    second_sha = _git(repo_path, "rev-parse", "HEAD")

    Path(repo_path, "app.py").write_text(
        "def main():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    Path(repo_path, "legacy.py").unlink()
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-m", "remove helper and legacy")
    third_sha = _git(repo_path, "rev-parse", "HEAD")

    return repo_path, first_sha, second_sha, third_sha


def create_git_repo_with_unchanged_file(root_dir: str) -> tuple[str, str, str]:
    repo_path = os.path.join(root_dir, "repo")
    os.mkdir(repo_path)
    _git(repo_path, "init")
    _git(repo_path, "config", "user.name", "Context Graph Test")
    _git(repo_path, "config", "user.email", "context-graph@example.com")

    Path(repo_path, "app.py").write_text(
        "def main():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    Path(repo_path, "config.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    _git(repo_path, "add", "app.py", "config.py")
    _git(repo_path, "commit", "-m", "initial files")
    first_sha = _git(repo_path, "rev-parse", "HEAD")

    Path(repo_path, "app.py").write_text(
        "def main():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    _git(repo_path, "add", "app.py")
    _git(repo_path, "commit", "-m", "change app only")
    second_sha = _git(repo_path, "rev-parse", "HEAD")

    return repo_path, first_sha, second_sha


def create_git_repo_with_branch_divergence(root_dir: str) -> tuple[str, str, str, str]:
    repo_path, first_sha, second_sha = create_git_repo(root_dir)
    _git(repo_path, "branch", "-M", "main")
    _git(repo_path, "checkout", "-b", "feature", first_sha)

    Path(repo_path, "app.py").write_text(
        "def main():\n"
        "    return 99\n",
        encoding="utf-8",
    )
    _git(repo_path, "add", "app.py")
    _git(repo_path, "commit", "-m", "feature branch change")
    feature_sha = _git(repo_path, "rev-parse", "HEAD")
    _git(repo_path, "checkout", "main")

    return repo_path, first_sha, second_sha, feature_sha


def create_git_remote_repo(root_dir: str) -> tuple[str, str, str, str]:
    repo_path, first_sha, second_sha = create_git_repo(root_dir)
    _git(repo_path, "branch", "-M", "main")
    bare_path = os.path.join(root_dir, "remote.git")
    subprocess.run(
        ["git", "clone", "--bare", repo_path, bare_path],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo_path, "remote", "add", "origin", bare_path)
    _git(repo_path, "push", "--set-upstream", "origin", "main")
    return repo_path, bare_path, first_sha, second_sha


def push_remote_commit(repo_path: str) -> str:
    Path(repo_path, "app.py").write_text(
        "def helper():\n"
        "    return 3\n\n"
        "def main():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    _git(repo_path, "add", "app.py")
    _git(repo_path, "commit", "-m", "update remote app")
    _git(repo_path, "push", "origin", "main")
    return _git(repo_path, "rev-parse", "HEAD")


def build_repo_snapshot_from_fs(
    repo_path: str,
    *,
    repo_id: str,
    revision_id: str,
) -> GraphSnapshot:
    app_source = Path(repo_path, "app.py").read_text(encoding="utf-8")
    has_helper = "def helper" in app_source
    has_legacy = Path(repo_path, "legacy.py").exists()
    has_config = Path(repo_path, "config.py").exists()

    project_node = CanonicalNode(
        f"project:{repo_id}",
        repo_id,
        "Project",
        repo_id,
        qualified_name=repo_id,
    )
    app_file = CanonicalNode(
        f"file:{repo_id}:{repo_id}.app.__file__",
        repo_id,
        "File",
        "app.py",
        qualified_name=f"{repo_id}.app.__file__",
        file_path="app.py",
    )
    main_function = CanonicalNode(
        f"symbol:{repo_id}:{repo_id}.app.main",
        repo_id,
        "Function",
        "main",
        qualified_name=f"{repo_id}.app.main",
        file_path="app.py",
        start_line=1,
        end_line=5,
        properties={"body_kind": "helper-call" if has_helper else "literal-return"},
    )

    nodes = [project_node, app_file, main_function]
    edges = [
        CanonicalEdge(
            f"edge:{repo_id}:CONTAINS_FILE:project:{repo_id}:file:{repo_id}:{repo_id}.app.__file__",
            repo_id,
            "CONTAINS_FILE",
            project_node.node_id,
            app_file.node_id,
        ),
        CanonicalEdge(
            f"edge:{repo_id}:DEFINES:file:{repo_id}:{repo_id}.app.__file__:symbol:{repo_id}:{repo_id}.app.main",
            repo_id,
            "DEFINES",
            app_file.node_id,
            main_function.node_id,
        ),
    ]

    if has_helper:
        helper_function = CanonicalNode(
            f"symbol:{repo_id}:{repo_id}.app.helper",
            repo_id,
            "Function",
            "helper",
            qualified_name=f"{repo_id}.app.helper",
            file_path="app.py",
            start_line=1,
            end_line=2,
        )
        nodes.append(helper_function)
        edges.extend(
            [
                CanonicalEdge(
                    f"edge:{repo_id}:DEFINES:file:{repo_id}:{repo_id}.app.__file__:symbol:{repo_id}:{repo_id}.app.helper",
                    repo_id,
                    "DEFINES",
                    app_file.node_id,
                    helper_function.node_id,
                ),
                CanonicalEdge(
                    f"edge:{repo_id}:CALLS:symbol:{repo_id}:{repo_id}.app.main:symbol:{repo_id}:{repo_id}.app.helper",
                    repo_id,
                    "CALLS",
                    main_function.node_id,
                    helper_function.node_id,
                    properties={"callee_name": "helper"},
                ),
            ]
        )

    if has_legacy:
        legacy_file = CanonicalNode(
            f"file:{repo_id}:{repo_id}.legacy.__file__",
            repo_id,
            "File",
            "legacy.py",
            qualified_name=f"{repo_id}.legacy.__file__",
            file_path="legacy.py",
        )
        legacy_function = CanonicalNode(
            f"symbol:{repo_id}:{repo_id}.legacy.legacy",
            repo_id,
            "Function",
            "legacy",
            qualified_name=f"{repo_id}.legacy.legacy",
            file_path="legacy.py",
            start_line=1,
            end_line=2,
        )
        nodes.extend([legacy_file, legacy_function])
        edges.extend(
            [
                CanonicalEdge(
                    f"edge:{repo_id}:CONTAINS_FILE:project:{repo_id}:file:{repo_id}:{repo_id}.legacy.__file__",
                    repo_id,
                    "CONTAINS_FILE",
                    project_node.node_id,
                    legacy_file.node_id,
                ),
                CanonicalEdge(
                    f"edge:{repo_id}:DEFINES:file:{repo_id}:{repo_id}.legacy.__file__:symbol:{repo_id}:{repo_id}.legacy.legacy",
                    repo_id,
                    "DEFINES",
                    legacy_file.node_id,
                    legacy_function.node_id,
                ),
            ]
        )

    if has_config:
        config_file = CanonicalNode(
            f"file:{repo_id}:{repo_id}.config.__file__",
            repo_id,
            "File",
            "config.py",
            qualified_name=f"{repo_id}.config.__file__",
            file_path="config.py",
        )
        nodes.append(config_file)
        edges.append(
            CanonicalEdge(
                f"edge:{repo_id}:CONTAINS_FILE:project:{repo_id}:file:{repo_id}:{repo_id}.config.__file__",
                repo_id,
                "CONTAINS_FILE",
                project_node.node_id,
                config_file.node_id,
            )
        )

    return GraphSnapshot(
        repo_id=repo_id,
        revision_id=revision_id,
        root_path=repo_path,
        generated_at="2026-03-20T00:00:00+00:00",
        nodes=tuple(nodes),
        edges=tuple(edges),
        metadata={},
    )


class FakeSnapshotProducer:
    def build_snapshot(
        self,
        repo_path: str,
        *,
        repo_id: str | None = None,
        revision_id: str,
        limit: int = 1000000,
    ) -> GraphSnapshot:
        _ = limit
        return build_repo_snapshot_from_fs(
            repo_path,
            repo_id=repo_id or "demo",
            revision_id=revision_id,
        )


class FakeGitHubApiClient:
    def __init__(
        self,
        *,
        repo_payload: dict[str, object] | None = None,
        commit_prs: dict[str, list[dict[str, object]]] | None = None,
        linked_issue_numbers: dict[int, tuple[int, ...]] | None = None,
        pull_requests: dict[int, dict[str, object]] | None = None,
        reviews: dict[int, list[dict[str, object]]] | None = None,
        review_comments: dict[int, list[dict[str, object]]] | None = None,
        issue_comments: dict[int, list[dict[str, object]]] | None = None,
        timelines: dict[int, list[dict[str, object]]] | None = None,
        issues: dict[int, dict[str, object]] | None = None,
        fail_on_commit_prs: bool = False,
    ) -> None:
        self.token = "fake-token"
        self.repo_payload = repo_payload or {}
        self.commit_prs = commit_prs or {}
        self.linked_issue_numbers = linked_issue_numbers or {}
        self.pull_requests = pull_requests or {}
        self.reviews = reviews or {}
        self.review_comments = review_comments or {}
        self.issue_comments_map = issue_comments or {}
        self.timelines = timelines or {}
        self.issues = issues or {}
        self.fail_on_commit_prs = fail_on_commit_prs

    def get_repository(self, owner: str, repo: str) -> dict[str, object]:
        payload = dict(self.repo_payload) if self.repo_payload else {}
        payload.setdefault("owner", {"login": owner})
        payload.setdefault("name", repo)
        payload.setdefault("clone_url", f"https://github.com/{owner}/{repo}.git")
        payload.setdefault("html_url", f"https://github.com/{owner}/{repo}")
        payload.setdefault("default_branch", "main")
        return payload

    def list_commit_pull_requests(self, owner: str, repo: str, sha: str) -> list[dict[str, object]]:
        _ = (owner, repo)
        if self.fail_on_commit_prs:
            raise RuntimeError("GitHub commit pull request lookup failed")
        return [dict(item) for item in self.commit_prs.get(sha, [])]

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, object]:
        _ = (owner, repo)
        return dict(self.pull_requests[number])

    def list_pull_request_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, object]]:
        _ = (owner, repo)
        return [dict(item) for item in self.reviews.get(number, [])]

    def list_pull_request_review_comments(self, owner: str, repo: str, number: int) -> list[dict[str, object]]:
        _ = (owner, repo)
        return [dict(item) for item in self.review_comments.get(number, [])]

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, object]]:
        _ = (owner, repo)
        return [dict(item) for item in self.issue_comments_map.get(number, [])]

    def list_issue_timeline(self, owner: str, repo: str, number: int) -> list[dict[str, object]]:
        _ = (owner, repo)
        return [dict(item) for item in self.timelines.get(number, [])]

    def list_pull_request_linked_issue_numbers(self, owner: str, repo: str, number: int) -> tuple[int, ...]:
        _ = (owner, repo)
        return tuple(self.linked_issue_numbers.get(number, ()))

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        _ = (owner, repo)
        return dict(self.issues[number])


class StaticRepoResolver:
    def __init__(self, repo_ref: GitHubRepoRef) -> None:
        self.repo_ref = repo_ref

    def resolve(self, **kwargs) -> GitHubRepoRef | None:
        _ = kwargs
        return self.repo_ref


class FakeEpisodeLLMClient:
    def __init__(
        self,
        *,
        extracted_entity_names: tuple[str, ...] = (),
        extracted_edges: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.extracted_entity_names = extracted_entity_names
        self.extracted_edges = extracted_edges

    async def generate_response(self, messages, response_model=None, **kwargs):
        _ = (messages, kwargs)
        model_name = getattr(response_model, "__name__", "")
        if model_name == "ExtractedEntities":
            return {
                "extracted_entities": [
                    {"name": name, "entity_type_id": 0}
                    for name in self.extracted_entity_names
                ]
            }
        if model_name == "ExtractedEdges":
            return {"edges": list(self.extracted_edges)}
        raise AssertionError(f"unexpected response model {model_name!r}")


def _statement_rows(statements, query_fragment: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for statement in statements:
        if query_fragment in statement.query:
            rows.extend(dict(row) for row in statement.parameters.get("rows", []))
    return rows


def _github_entity_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (n:GitHubEntity:")


def _github_commit_link_rows(statements, relation_type: str) -> list[dict[str, object]]:
    return _statement_rows(statements, f"MERGE (commit)-[r:{relation_type}]->(entity)")


def _github_project_link_rows(statements, relation_type: str) -> list[dict[str, object]]:
    return _statement_rows(statements, f"MERGE (project)-[r:{relation_type}]->(entity)")


def _github_entity_link_rows(statements, relation_type: str) -> list[dict[str, object]]:
    return _statement_rows(statements, f"MERGE (source)-[r:{relation_type}]->(target)")


def _episode_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (e:Episode:ContextNode")


def _episode_on_rows(statements) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for statement in statements:
        if "MERGE (episode)-[r:ON]->(target)" in statement.query:
            rows.append(dict(statement.parameters))
    return rows


def _mention_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (episode)-[r:MENTIONS")


class SnapshotBuilderTests(unittest.TestCase):
    def test_build_snapshot_from_graph_export_preserves_generic_edge_types(self) -> None:
        export_payload = {
            "project": "demo",
            "root_path": "/tmp/repo",
            "node_count": 4,
            "edge_count": 3,
            "nodes": [
                {
                    "temp_id": 1,
                    "label": "Project",
                    "name": "demo",
                    "qualified_name": "demo",
                    "file_path": None,
                    "start_line": 0,
                    "end_line": 0,
                    "properties_json": "{}",
                },
                {
                    "temp_id": 2,
                    "label": "File",
                    "name": "app.py",
                    "qualified_name": "demo.app.__file__",
                    "file_path": "app.py",
                    "start_line": 0,
                    "end_line": 0,
                    "properties_json": "{\"language\":\"Python\"}",
                },
                {
                    "temp_id": 3,
                    "label": "Function",
                    "name": "main",
                    "qualified_name": "demo.app.main",
                    "file_path": "app.py",
                    "start_line": 1,
                    "end_line": 4,
                    "properties_json": "{\"signature\":\"()\"}",
                },
                {
                    "temp_id": 4,
                    "label": "File",
                    "name": "test_app.py",
                    "qualified_name": "demo.test_app.__file__",
                    "file_path": "test_app.py",
                    "start_line": 0,
                    "end_line": 0,
                    "properties_json": "{}",
                },
            ],
            "edges": [
                {
                    "temp_id": 11,
                    "source_temp_id": 1,
                    "target_temp_id": 2,
                    "type": "CONTAINS_FILE",
                    "properties_json": "{}",
                },
                {
                    "temp_id": 12,
                    "source_temp_id": 2,
                    "target_temp_id": 3,
                    "type": "DEFINES",
                    "properties_json": "{}",
                },
                {
                    "temp_id": 13,
                    "source_temp_id": 4,
                    "target_temp_id": 3,
                    "type": "TESTS",
                    "properties_json": "{\"framework\":\"pytest\"}",
                },
            ],
        }

        snapshot = build_snapshot_from_graph_export(export_payload, repo_id="demo", revision_id="r1")

        self.assertEqual(snapshot.repo_id, "demo")
        self.assertEqual(snapshot.metadata["source"], "codebase-memory-mcp-graph-export")
        edge_types = {edge.edge_type for edge in snapshot.edges}
        self.assertIn("TESTS", edge_types)
        test_edge = next(edge for edge in snapshot.edges if edge.edge_type == "TESTS")
        self.assertEqual(test_edge.properties["framework"], "pytest")


class EpisodeLLMSettingsTests(unittest.TestCase):
    def test_load_openai_settings_from_args(self) -> None:
        class Args:
            llm_provider = "openai"
            llm_model = "demo-model"
            llm_small_model = "demo-small"
            llm_base_url = "http://localhost:4000/v1"
            llm_api_key = "demo-key"
            aws_region = None

        settings = load_episode_llm_settings(Args())

        self.assertEqual(
            settings,
            EpisodeLLMSettings(
                provider="openai",
                model="demo-model",
                small_model="demo-small",
                base_url="http://localhost:4000/v1",
                api_key="demo-key",
                aws_region=None,
            ),
        )

    def test_load_bedrock_settings_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CONTEXT_GRAPH_LLM_PROVIDER": "bedrock",
                "CONTEXT_GRAPH_LLM_MODEL": "bedrock-main",
                "CONTEXT_GRAPH_LLM_SMALL_MODEL": "bedrock-small",
                "AWS_REGION": "us-east-1",
            },
            clear=False,
        ):
            settings = load_episode_llm_settings()

        self.assertEqual(
            settings,
            EpisodeLLMSettings(
                provider="bedrock",
                model="bedrock-main",
                small_model="bedrock-small",
                base_url=None,
                api_key=None,
                aws_region="us-east-1",
            ),
        )


class DiffAndWriterTests(unittest.TestCase):
    def test_diff_detects_changed_nodes_and_added_edges(self) -> None:
        before = make_snapshot("r1")
        after = make_snapshot("r2", include_extra_call=True)

        diff = diff_snapshots(before, after)
        self.assertIn("symbol:demo:demo.app.main", diff.changed_nodes)
        self.assertEqual(len(diff.added_edges), 1)
        self.assertEqual(len(diff.added_nodes), 1)

    def test_temporal_writer_records_and_updates_snapshot(self) -> None:
        metadata = InMemoryMetadataStore()
        writer = TemporalGraphWriter(metadata_store=metadata, graph_sink=Neo4jGraphSink())

        first = writer.apply_snapshot(make_snapshot("r1"), execute=False)
        second = writer.apply_snapshot(make_snapshot("r2", include_extra_call=True), execute=False)

        self.assertEqual(first.statements_executed, 0)
        self.assertFalse(first.diff.is_empty())
        self.assertEqual(second.diff.previous_revision_id, "r1")
        self.assertEqual(metadata.load_latest_snapshot("demo").revision_id, "r2")


class SinkPlanningTests(unittest.TestCase):
    def test_neo4j_and_neptune_generate_versioning_statements(self) -> None:
        before = make_snapshot("r1")
        after = make_snapshot("r2", include_extra_call=True)
        diff = diff_snapshots(before, after)

        neo4j_statements = Neo4jGraphSink().plan_apply_diff(after, diff)
        neptune_statements = NeptuneGraphSink().plan_apply_diff(after, diff)

        neo4j_queries = "\n".join(statement.query for statement in neo4j_statements)
        neptune_queries = "\n".join(statement.query for statement in neptune_statements)

        self.assertIn("MERGE (project:CodeNode:Project", neo4j_queries)
        self.assertIn("CREATE (v:CodeNodeVersion", neo4j_queries)
        self.assertIn("CREATE (s)-[r:CALLS", neo4j_queries)
        self.assertIn("edge_kind: row.edge_kind", neo4j_queries)
        self.assertIn("MERGE (project:CodeNode:Project", neptune_queries)
        self.assertIn("CREATE (v:CodeNodeVersion", neptune_queries)
        self.assertIn("CREATE (s)-[r:CALLS", neptune_queries)
        self.assertIn("edge_kind: row.edge_kind", neptune_queries)

    def test_plan_reset_repo_emits_code_node_deletes(self) -> None:
        statements = Neo4jGraphSink().plan_reset_repo("demo")
        queries = "\n".join(statement.query for statement in statements)

        self.assertEqual(len(statements), 6)
        self.assertIn("MATCH (e:Episode {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (g:GitHubEntity {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (c:Commit {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (v:CodeNodeVersion {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (n:CodeNode {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (n:ContextNode {repo_id: $repo_id})", queries)
        self.assertIn("AND NOT n:CodeNode", queries)
        self.assertTrue(all(statement.parameters == {"repo_id": "demo"} for statement in statements))

    def test_episode_processor_plans_episode_on_and_mentions(self) -> None:
        metadata = InMemoryMetadataStore()
        metadata.save_latest_snapshot(make_snapshot("r1", include_extra_call=True))
        processor = EpisodeProcessor(
            graph_sink=Neo4jGraphSink(),
            read_repository=GraphReadRepository(metadata_store=metadata),
        )

        batch = processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=EpisodeRecord(
                        episode_id="turn-1",
                        repo_id="demo",
                        source_type="user_message",
                        source_id="turn-1",
                        author="user",
                        created_at="2026-03-22T00:00:00+00:00",
                        updated_at="2026-03-22T00:00:00+00:00",
                        text="Please inspect `main` in app.py",
                        metadata={"thread_id": "chat-1"},
                        parent_node_id="project:demo",
                    )
                )
            ],
            execute=False,
        )

        queries = "\n".join(statement.query for statement in batch.statements)
        episode_rows = _episode_rows(batch.statements)
        mention_rows = _mention_rows(batch.statements)
        on_rows = _episode_on_rows(batch.statements)

        self.assertIn("MERGE (e:Episode:ContextNode", queries)
        self.assertIn("MERGE (episode)-[r:ON]->(target)", queries)
        self.assertIn("MERGE (episode)-[r:MENTIONS", queries)
        self.assertTrue(any(row["source_type"] == "user_message" for row in episode_rows))
        self.assertTrue(any(row["target_node_id"] == "project:demo" for row in on_rows))
        self.assertTrue(any(row["target_node_id"] == "symbol:demo:demo.app.main" for row in mention_rows))

    def test_episode_processor_merges_deterministic_and_llm_mentions(self) -> None:
        metadata = InMemoryMetadataStore()
        metadata.save_latest_snapshot(make_snapshot("r1", include_extra_call=True))
        processor = EpisodeProcessor(
            graph_sink=Neo4jGraphSink(),
            read_repository=GraphReadRepository(metadata_store=metadata),
            adapter=GraphitiAdapter(
                llm_client=FakeEpisodeLLMClient(
                    extracted_entity_names=("helper", "app.py"),
                )
            ),
        )

        batch = processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=EpisodeRecord(
                        episode_id="turn-2",
                        repo_id="demo",
                        source_type="user_message",
                        source_id="turn-2",
                        author="user",
                        created_at="2026-03-22T00:00:00+00:00",
                        updated_at="2026-03-22T00:00:00+00:00",
                        text="Why was helper added to app.py?",
                        metadata={"thread_id": "chat-2"},
                    )
                )
            ],
            execute=False,
        )

        mention_rows = _mention_rows(batch.statements)
        helper_row = next(
            row for row in mention_rows if row["target_node_id"] == "symbol:demo:demo.app.helper"
        )
        app_row = next(
            row for row in mention_rows if row["target_node_id"] == "file:demo:demo.app.__file__"
        )
        helper_meta = json.loads(helper_row["metadata_json"])
        app_meta = json.loads(app_row["metadata_json"])

        self.assertEqual(helper_meta["match_sources"], ["deterministic", "llm"])
        self.assertIn("helper", helper_meta["matched_terms"])
        self.assertEqual(app_meta["match_sources"], ["deterministic", "llm"])
        self.assertIn("app.py", app_meta["matched_terms"])

    def test_episode_processor_creates_generic_entities_for_unresolved_llm_mentions(self) -> None:
        metadata = InMemoryMetadataStore()
        sink = Neo4jGraphSink()
        sink.execute = lambda statements: 0  # type: ignore[method-assign]
        processor = EpisodeProcessor(
            graph_sink=sink,
            read_repository=GraphReadRepository(metadata_store=metadata, graph_sink=sink),
            adapter=GraphitiAdapter(
                llm_client=FakeEpisodeLLMClient(
                    extracted_entity_names=("Alice Johnson",),
                )
            ),
        )

        batch = processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=EpisodeRecord(
                        episode_id="turn-3",
                        repo_id="demo",
                        source_type="user_message",
                        source_id="turn-3",
                        author="user",
                        created_at="2026-03-22T00:00:00+00:00",
                        updated_at="2026-03-22T00:00:00+00:00",
                        text="Alice Johnson asked about the new design.",
                        metadata={"thread_id": "chat-3"},
                    )
                )
            ],
            execute=True,
        )

        mention_rows = _mention_rows(batch.statements)
        entity_rows = _statement_rows(batch.statements, "MERGE (n:ContextNode:Entity")

        self.assertEqual(len(entity_rows), 1)
        self.assertEqual(entity_rows[0]["name"], "Alice Johnson")
        self.assertEqual(len(mention_rows), 1)
        self.assertEqual(mention_rows[0]["target_node_id"], entity_rows[0]["node_id"])


@unittest.skipUnless(shutil.which("git"), "git executable is required for git metadata tests")
class GitMetadataTests(unittest.TestCase):
    def test_reads_commit_info_and_temporary_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha = create_git_repo(temp_dir)
            metadata = GitMetadata()

            info = metadata.get_commit_info(repo_path, second_sha)
            changed_files = metadata.list_changed_files(repo_path, first_sha, second_sha)
            commits = metadata.list_commits_between(repo_path, first_sha, second_sha)

            self.assertEqual(info.sha, second_sha)
            self.assertEqual(info.author_name, "Context Graph Test")
            self.assertEqual(info.author_email, "context-graph@example.com")
            self.assertEqual(info.changed_files, ("app.py",))
            self.assertEqual(changed_files, ("app.py",))
            self.assertEqual(commits, (second_sha,))

            with metadata.temporary_worktree(repo_path, first_sha) as worktree_path:
                self.assertNotIn("helper", Path(worktree_path, "app.py").read_text(encoding="utf-8"))

            self.assertFalse(Path(worktree_path).exists())

    def test_is_ancestor_reports_commit_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha = create_git_repo(temp_dir)
            metadata = GitMetadata()

            self.assertTrue(metadata.is_ancestor(repo_path, first_sha, second_sha))
            self.assertFalse(metadata.is_ancestor(repo_path, second_sha, first_sha))

    def test_lists_remotes_and_resolves_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, bare_path, _first_sha, second_sha = create_git_remote_repo(temp_dir)
            metadata = GitMetadata()

            remotes = metadata.list_remotes(repo_path)
            resolved = metadata.resolve_revision(repo_path, "origin/main")

            self.assertEqual(remotes["origin"], bare_path)
            self.assertEqual(resolved, second_sha)


class MetadataStoreTests(unittest.TestCase):
    def test_sqlite_metadata_store_round_trips_snapshot(self) -> None:
        store = SQLiteMetadataStore(":memory:")
        snapshot = make_snapshot("r1", include_extra_call=True)

        store.save_latest_snapshot(snapshot)
        loaded = store.load_latest_snapshot("demo")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.revision_id, "r1")
        self.assertEqual(len(loaded.nodes), len(snapshot.nodes))
        self.assertEqual(len(loaded.edges), len(snapshot.edges))

    def test_sqlite_metadata_store_round_trips_repo_index_state(self) -> None:
        store = SQLiteMetadataStore(":memory:")
        state = RepoIndexState(
            repo_id="demo",
            root_path="/tmp/demo",
            latest_revision_id="r2",
            latest_indexed_at="2026-03-21T00:00:00+00:00",
            metadata={"source": "git"},
            latest_commit_sha="abc123",
            latest_branch="main",
            last_commit_indexed_at="2026-03-21T00:00:00+00:00",
        )

        store.save_repo_index_state(state)
        loaded = store.load_repo_index_state("demo")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.latest_commit_sha, "abc123")
        self.assertEqual(loaded.latest_branch, "main")
        self.assertEqual(loaded.metadata, {"source": "git"})

    def test_sqlite_metadata_store_round_trips_repo_source_state(self) -> None:
        store = SQLiteMetadataStore(":memory:")
        state = RepoSourceState(
            repo_id="demo",
            provider="github",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            default_branch="main",
            clone_path="/mnt/context-graph/repos/octo/demo",
            last_fetch_at="2026-03-21T00:00:00+00:00",
            last_fetched_head_sha="abc123",
            last_github_sync_at="2026-03-21T00:05:00+00:00",
            last_github_sync_status="applied",
            last_github_sync_error=None,
            metadata={"source": "github_url"},
        )

        store.save_repo_source_state(state)
        loaded = store.load_repo_source_state("demo")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.canonical_repo, "octo/demo")
        self.assertEqual(loaded.clone_path, "/mnt/context-graph/repos/octo/demo")
        self.assertEqual(loaded.metadata, {"source": "github_url"})

    def test_sqlite_metadata_store_bootstrap_creates_profiles_table(self) -> None:
        store = SQLiteMetadataStore(":memory:")

        store.bootstrap()

        rows = store.connection.execute("PRAGMA table_info(cg_profiles)").fetchall()
        column_names = {row[1] for row in rows}

        self.assertEqual(len(rows), 18)
        self.assertSetEqual(
            column_names,
            {
                "profile_id",
                "repo_id",
                "profile_kind",
                "profile_target_kind",
                "profile_target_id",
                "relation_type",
                "source_node_id",
                "source_node_kind",
                "target_node_id",
                "target_node_kind",
                "target_revision_id",
                "value_text",
                "keys_json",
                "metadata_json",
                "is_active",
                "valid_from_revision",
                "valid_to_revision",
                "updated_at",
            },
        )

    def test_sqlite_metadata_store_reset_repo_context_clears_repo_rows(self) -> None:
        store = SQLiteMetadataStore(":memory:")
        snapshot = make_snapshot("r1", include_extra_call=True)

        store.save_latest_snapshot(snapshot)
        store.record_index_run(
            repo_id="demo",
            revision_id="r1",
            status="applied",
            stats={"nodes": len(snapshot.nodes), "edges": len(snapshot.edges)},
        )
        store.connection.execute(
            """
            INSERT INTO cg_profiles (
                profile_id,
                repo_id,
                profile_kind,
                profile_target_kind,
                profile_target_id,
                relation_type,
                source_node_id,
                source_node_kind,
                target_node_id,
                target_node_kind,
                target_revision_id,
                value_text,
                keys_json,
                metadata_json,
                is_active,
                valid_from_revision,
                valid_to_revision,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "profile:demo:calls:main-helper",
                "demo",
                "code_relation_profile",
                "CodeEdge",
                "edge:demo:CALLS:symbol:demo:demo.app.main:symbol:demo:demo.app.helper",
                "CALLS",
                "symbol:demo:demo.app.main",
                "Function",
                "symbol:demo:demo.app.helper",
                "Function",
                "r1",
                "main calls helper in app.py",
                json.dumps(["main helper", "calls helper"]),
                json.dumps({"file_path": "app.py"}),
                1,
                "r1",
                None,
                "2026-03-21T00:00:00+00:00",
            ),
        )
        store.connection.commit()

        deleted = store.reset_repo_context("demo")

        self.assertEqual(
            deleted,
            {
                "repo_source": 0,
                "repo_state": 1,
                "current_nodes": len(snapshot.nodes),
                "current_edges": len(snapshot.edges),
                "context_nodes": 0,
                "profiles": 1,
                "index_runs": 1,
            },
        )
        self.assertIsNone(store.load_latest_snapshot("demo"))
        self.assertEqual(
            store.connection.execute("SELECT COUNT(*) FROM cg_profiles WHERE repo_id = 'demo'").fetchone()[0],
            0,
        )

    def test_reset_context_execute_requires_graph_config_before_deleting_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "context_graph.sqlite")
            store = SQLiteMetadataStore(db_path)
            snapshot = make_snapshot("r1", include_extra_call=True)
            store.save_latest_snapshot(snapshot)
            store.record_index_run(
                repo_id="demo",
                revision_id="r1",
                status="applied",
                stats={"nodes": len(snapshot.nodes), "edges": len(snapshot.edges)},
            )

            with self.assertRaisesRegex(RuntimeError, "Neo4j execution requires"):
                cli_main(
                    [
                        "reset-context",
                        "--repo-id",
                        "demo",
                        "--metadata-db",
                        db_path,
                        "--execute",
                    ]
                )

            reloaded = SQLiteMetadataStore(db_path)
            self.assertIsNotNone(reloaded.load_latest_snapshot("demo"))
            self.assertEqual(
                reloaded.connection.execute(
                    "SELECT COUNT(*) FROM cg_index_runs WHERE repo_id = 'demo'"
                ).fetchone()[0],
                1,
            )


@unittest.skipUnless(shutil.which("git"), "git executable is required for commit indexer tests")
class CommitIndexerTests(unittest.TestCase):
    def test_bootstrap_commit_only_touches_files_changed_in_target_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, _first_sha, second_sha = create_git_repo_with_unchanged_file(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = CommitIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
            )

            result = indexer.apply_latest_commit(repo_path, repo_id="demo", execute=False)
            touch_rows = _statement_rows(result.statements, "TOUCHED {stable_node_id")
            file_rows = _statement_rows(result.statements, "TOUCHED_FILE {stable_node_id")
            introduced_rows = _statement_rows(
                result.statements,
                "INTRODUCED_VERSION {version_id: row.version_id}",
            )

            self.assertEqual(result.commit.sha, second_sha)
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "file:demo:demo.app.__file__",
                    "change_kind": "changed",
                    "node_kind": "File",
                    "qualified_name": "demo.app.__file__",
                    "file_path": "app.py",
                },
                file_rows,
            )
            self.assertNotIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "file:demo:demo.config.__file__",
                    "change_kind": "changed",
                    "node_kind": "File",
                    "qualified_name": "demo.config.__file__",
                    "file_path": "config.py",
                },
                file_rows,
            )
            self.assertNotIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "file:demo:demo.config.__file__",
                    "change_kind": "changed",
                    "node_kind": "File",
                    "qualified_name": "demo.config.__file__",
                    "file_path": "config.py",
                },
                touch_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "file:demo:demo.app.__file__",
                    "version_id": f"file:demo:demo.app.__file__@{second_sha}",
                    "change_kind": "changed",
                    "node_kind": "File",
                    "qualified_name": "demo.app.__file__",
                    "file_path": "app.py",
                },
                introduced_rows,
            )
            self.assertNotIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "file:demo:demo.config.__file__",
                    "version_id": f"file:demo:demo.config.__file__@{second_sha}",
                    "change_kind": "changed",
                    "node_kind": "File",
                    "qualified_name": "demo.config.__file__",
                    "file_path": "config.py",
                },
                introduced_rows,
            )

    def test_apply_latest_commit_indexes_head_and_updates_repo_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, _first_sha, second_sha = create_git_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = CommitIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
            )

            result = indexer.apply_latest_commit(repo_path, repo_id="demo", execute=False)

            self.assertFalse(result.skipped)
            self.assertIsNotNone(result.snapshot)
            self.assertEqual(result.commit.sha, second_sha)
            self.assertEqual(result.snapshot.revision_id, second_sha)
            self.assertEqual(Path(result.snapshot.root_path), Path(repo_path).resolve())
            self.assertEqual(result.snapshot.metadata["commit_changed_files"], ["app.py"])
            self.assertEqual(result.applied_commit_shas, (second_sha,))
            self.assertEqual(metadata.load_latest_snapshot("demo").revision_id, second_sha)
            self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, second_sha)

            episode_rows = _episode_rows(result.statements)
            episode_on_rows = _episode_on_rows(result.statements)

            self.assertTrue(any(row["source_type"] == "git_commit_message" for row in episode_rows))
            self.assertTrue(
                any(
                    row["repo_id"] == "demo"
                    and row["episode_node_id"] == f"episode:demo:git-commit-message:{second_sha}"
                    and row["target_node_id"] == f"commit:demo:{second_sha}"
                    for row in episode_on_rows
                )
            )

    def test_apply_latest_commit_skips_when_head_is_already_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, _first_sha, _second_sha = create_git_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = CommitIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
            )

            first = indexer.apply_latest_commit(repo_path, repo_id="demo", execute=False)
            second = indexer.apply_latest_commit(repo_path, repo_id="demo", execute=False)

            self.assertFalse(first.skipped)
            self.assertTrue(second.skipped)
            self.assertEqual(second.skip_reason, "head commit already indexed")
            self.assertEqual(second.statements, ())

    def test_apply_latest_commit_replays_multiple_missing_commits_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha, third_sha = create_git_repo_with_deletions(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = CommitIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
            )

            first_result = indexer.apply_commit(repo_path, commit_sha=first_sha, repo_id="demo", execute=False)
            replay_result = indexer.apply_latest_commit(repo_path, repo_id="demo", execute=False)

            self.assertFalse(first_result.skipped)
            self.assertFalse(replay_result.skipped)
            self.assertEqual(replay_result.previous_commit_sha, first_sha)
            self.assertEqual(replay_result.applied_commit_shas, (second_sha, third_sha))
            self.assertEqual(replay_result.commit.sha, third_sha)
            self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, third_sha)

    def test_apply_latest_commit_rejects_branch_switches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, _first_sha, second_sha, _feature_sha = create_git_repo_with_branch_divergence(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = CommitIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
            )

            _git(repo_path, "checkout", "main")
            indexer.apply_latest_commit(repo_path, repo_id="demo", execute=False)
            _git(repo_path, "checkout", "feature")

            with self.assertRaisesRegex(RuntimeError, "does not support switching branches yet"):
                indexer.apply_latest_commit(repo_path, repo_id="demo", execute=False)

            self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, second_sha)

    def test_apply_commit_rejects_non_descendant_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha = create_git_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = CommitIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
            )

            indexer.apply_commit(repo_path, commit_sha=second_sha, repo_id="demo", execute=False)

            with self.assertRaisesRegex(RuntimeError, "is not an ancestor of target commit"):
                indexer.apply_commit(repo_path, commit_sha=first_sha, repo_id="demo", execute=False)

    def test_commit_statements_cover_added_changed_and_removed_nodes_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha, third_sha = create_git_repo_with_deletions(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = CommitIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
            )

            first_result = indexer.apply_commit(repo_path, commit_sha=first_sha, repo_id="demo", execute=False)
            second_result = indexer.apply_commit(repo_path, commit_sha=second_sha, repo_id="demo", execute=False)
            third_result = indexer.apply_commit(repo_path, commit_sha=third_sha, repo_id="demo", execute=False)

            self.assertFalse(first_result.skipped)

            second_queries = "\n".join(statement.query for statement in second_result.statements)
            third_queries = "\n".join(statement.query for statement in third_result.statements)
            second_touch_rows = _statement_rows(second_result.statements, "TOUCHED {stable_node_id")
            second_file_rows = _statement_rows(second_result.statements, "TOUCHED_FILE {stable_node_id")
            second_introduced_rows = _statement_rows(
                second_result.statements,
                "INTRODUCED_VERSION {version_id: row.version_id}",
            )
            second_invalidated_rows = _statement_rows(
                second_result.statements,
                "INVALIDATED_VERSION {version_id: hv.version_id}",
            )
            third_touch_rows = _statement_rows(third_result.statements, "TOUCHED {stable_node_id")
            third_file_rows = _statement_rows(third_result.statements, "TOUCHED_FILE {stable_node_id")
            third_invalidated_rows = _statement_rows(
                third_result.statements,
                "INVALIDATED_VERSION {version_id: hv.version_id}",
            )

            self.assertIn("MERGE (commit:Commit", second_queries)
            self.assertIn("MERGE (parent)-[r:PRECEDES]->(commit)", second_queries)
            self.assertIn("MERGE (commit)-[r:TOUCHED {stable_node_id: row.node_id}]->(node)", second_queries)
            self.assertIn("MERGE (commit)-[r:TOUCHED_FILE {stable_node_id: row.node_id}]->(file)", second_queries)
            self.assertIn(
                "MERGE (commit)-[r:INTRODUCED_VERSION {version_id: row.version_id}]->(version)",
                second_queries,
            )
            self.assertIn(
                "MERGE (commit)-[r:INVALIDATED_VERSION {version_id: hv.version_id}]->(version)",
                second_queries,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "symbol:demo:demo.app.helper",
                    "change_kind": "added",
                    "node_kind": "Function",
                    "qualified_name": "demo.app.helper",
                    "file_path": "app.py",
                },
                second_touch_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "file:demo:demo.legacy.__file__",
                    "change_kind": "added",
                    "node_kind": "File",
                    "qualified_name": "demo.legacy.__file__",
                    "file_path": "legacy.py",
                },
                second_touch_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "file:demo:demo.app.__file__",
                    "change_kind": "changed",
                    "node_kind": "File",
                    "qualified_name": "demo.app.__file__",
                    "file_path": "app.py",
                },
                second_file_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "symbol:demo:demo.app.helper",
                    "version_id": f"symbol:demo:demo.app.helper@{second_sha}",
                    "change_kind": "added",
                    "node_kind": "Function",
                    "qualified_name": "demo.app.helper",
                    "file_path": "app.py",
                },
                second_introduced_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": second_sha,
                    "node_id": "symbol:demo:demo.app.main",
                    "change_kind": "changed",
                    "node_kind": "Function",
                    "qualified_name": "demo.app.main",
                    "file_path": "app.py",
                },
                second_invalidated_rows,
            )

            self.assertIn("MERGE (commit:Commit", third_queries)
            self.assertIn(
                "MERGE (commit)-[r:INVALIDATED_VERSION {version_id: hv.version_id}]->(version)",
                third_queries,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": third_sha,
                    "node_id": "symbol:demo:demo.app.helper",
                    "change_kind": "removed",
                    "node_kind": "Function",
                    "qualified_name": "demo.app.helper",
                    "file_path": "app.py",
                },
                third_touch_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": third_sha,
                    "node_id": "file:demo:demo.legacy.__file__",
                    "change_kind": "removed",
                    "node_kind": "File",
                    "qualified_name": "demo.legacy.__file__",
                    "file_path": "legacy.py",
                },
                third_touch_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": third_sha,
                    "node_id": "file:demo:demo.legacy.__file__",
                    "change_kind": "removed",
                    "node_kind": "File",
                    "qualified_name": "demo.legacy.__file__",
                    "file_path": "legacy.py",
                },
                third_file_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": third_sha,
                    "node_id": "file:demo:demo.app.__file__",
                    "change_kind": "changed",
                    "node_kind": "File",
                    "qualified_name": "demo.app.__file__",
                    "file_path": "app.py",
                },
                third_file_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "commit_sha": third_sha,
                    "node_id": "symbol:demo:demo.app.helper",
                    "change_kind": "removed",
                    "node_kind": "Function",
                    "qualified_name": "demo.app.helper",
                    "file_path": "app.py",
                },
                third_invalidated_rows,
            )

    def test_apply_commit_cli_indexes_explicit_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, _second_sha = create_git_repo(temp_dir)
            db_path = os.path.join(temp_dir, "context_graph.sqlite")
            output = io.StringIO()

            with patch("context_graph.cli._producer", return_value=FakeSnapshotProducer()):
                with redirect_stdout(output):
                    exit_code = cli_main(
                        [
                            "apply-commit",
                            repo_path,
                            "--repo-id",
                            "demo",
                            "--commit",
                            first_sha,
                            "--metadata-db",
                            db_path,
                        ]
                    )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertFalse(payload["skipped"])
            self.assertEqual(payload["commit"]["sha"], first_sha)
            self.assertEqual(payload["snapshot"]["revision_id"], first_sha)
            self.assertEqual(Path(payload["snapshot"]["root_path"]), Path(repo_path).resolve())
            self.assertEqual(payload["applied_commit_shas"], [first_sha])
            self.assertEqual(payload["repo_index_state"]["latest_commit_sha"], first_sha)

    def test_apply_latest_commit_cli_skips_after_first_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, _first_sha, second_sha = create_git_repo(temp_dir)
            db_path = os.path.join(temp_dir, "context_graph.sqlite")

            with patch("context_graph.cli._producer", return_value=FakeSnapshotProducer()):
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    first_exit = cli_main(
                        [
                            "apply-latest-commit",
                            repo_path,
                            "--repo-id",
                            "demo",
                            "--metadata-db",
                            db_path,
                        ]
                    )

                second_output = io.StringIO()
                with redirect_stdout(second_output):
                    second_exit = cli_main(
                        [
                            "apply-latest-commit",
                            repo_path,
                            "--repo-id",
                            "demo",
                            "--metadata-db",
                            db_path,
                        ]
                    )

            first_payload = json.loads(first_output.getvalue())
            second_payload = json.loads(second_output.getvalue())

            self.assertEqual(first_exit, 0)
            self.assertEqual(second_exit, 0)
            self.assertEqual(first_payload["commit"]["sha"], second_sha)
            self.assertFalse(first_payload["skipped"])
            self.assertEqual(first_payload["applied_commit_shas"], [second_sha])


@unittest.skipUnless(shutil.which("git"), "git executable is required for hosted indexer tests")
class HostedIndexTests(unittest.TestCase):
    def _make_repo_ref(self, clone_url: str) -> GitHubRepoRef:
        return GitHubRepoRef(
            owner="octo",
            name="demo",
            canonical_repo="octo/demo",
            clone_url=clone_url,
            html_url="https://github.com/octo/demo",
            default_branch="main",
            source="github_repo",
        )

    def _make_github_api(self, commit_sha: str) -> FakeGitHubApiClient:
        return FakeGitHubApiClient(
            repo_payload={
                "owner": {"login": "octo"},
                "name": "demo",
                "clone_url": "https://github.com/octo/demo.git",
                "html_url": "https://github.com/octo/demo",
                "default_branch": "main",
            },
            commit_prs={
                commit_sha: [{"number": 7}],
            },
            linked_issue_numbers={
                7: (42,),
            },
            pull_requests={
                7: {
                    "id": 700,
                    "number": 7,
                    "html_url": "https://github.com/octo/demo/pull/7",
                    "title": "Add helper call",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-21T00:00:00Z",
                    "updated_at": "2026-03-21T00:01:00Z",
                    "merged_at": None,
                    "merge_commit_sha": commit_sha,
                    "head": {"ref": "main", "sha": commit_sha},
                    "base": {"ref": "main"},
                    "body": "PR body",
                }
            },
            reviews={
                7: [
                    {
                        "id": 701,
                        "state": "APPROVED",
                        "user": {"login": "reviewer"},
                        "submitted_at": "2026-03-21T00:02:00Z",
                        "body": "Looks good",
                        "commit_id": commit_sha,
                    }
                ]
            },
            review_comments={
                7: [
                    {
                        "id": 702,
                        "html_url": "https://github.com/octo/demo/pull/7#discussion_r1",
                        "user": {"login": "reviewer"},
                        "created_at": "2026-03-21T00:02:30Z",
                        "updated_at": "2026-03-21T00:02:30Z",
                        "body": "Please rename this",
                        "path": "app.py",
                        "commit_id": commit_sha,
                        "pull_request_review_id": 701,
                    }
                ]
            },
            issue_comments={
                7: [
                    {
                        "id": 703,
                        "html_url": "https://github.com/octo/demo/issues/7#issuecomment-1",
                        "user": {"login": "commenter"},
                        "created_at": "2026-03-21T00:03:00Z",
                        "updated_at": "2026-03-21T00:03:00Z",
                        "body": "Ship it",
                    }
                ],
                42: [
                    {
                        "id": 704,
                        "html_url": "https://github.com/octo/demo/issues/42#issuecomment-2",
                        "user": {"login": "issue-user"},
                        "created_at": "2026-03-21T00:04:00Z",
                        "updated_at": "2026-03-21T00:04:00Z",
                        "body": "Issue discussion",
                    }
                ],
            },
            timelines={
                7: [
                    {
                        "id": 705,
                        "event": "merged",
                        "created_at": "2026-03-21T00:03:30Z",
                    }
                ],
                42: [
                    {
                        "id": 706,
                        "event": "referenced",
                        "created_at": "2026-03-21T00:04:30Z",
                        "source": {
                            "issue": {
                                "number": 42,
                                "repository_url": "https://api.github.com/repos/octo/demo",
                            }
                        },
                    }
                ],
            },
            issues={
                42: {
                    "id": 4200,
                    "number": 42,
                    "html_url": "https://github.com/octo/demo/issues/42",
                    "title": "Track helper rollout",
                    "state": "open",
                    "user": {"login": "issue-author"},
                    "created_at": "2026-03-21T00:00:00Z",
                    "updated_at": "2026-03-21T00:05:00Z",
                    "body": "Issue body",
                    "closed_at": None,
                }
            },
        )

    def test_repo_cache_reuses_clone_and_fetches_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, bare_path, _first_sha, second_sha = create_git_remote_repo(temp_dir)
            cache = RepoCacheManager(base_dir=os.path.join(temp_dir, "cache"))
            repo_ref = self._make_repo_ref(bare_path)

            first = cache.ensure_managed_repo(
                repo_ref,
                repo_id="demo",
                request_id="req-1",
                auth_token=None,
            )
            third_sha = push_remote_commit(repo_path)
            second = cache.ensure_managed_repo(
                repo_ref,
                repo_id="demo",
                request_id="req-2",
                auth_token=None,
                existing_state=first.source_state,
            )

            self.assertEqual(Path(first.repo_path), Path(second.repo_path))
            self.assertTrue(Path(first.repo_path).exists())
            self.assertEqual(cache.resolve_ref_sha(second.repo_path, "main"), third_sha)
            self.assertEqual(first.source_state.last_fetched_head_sha, second_sha)
            self.assertEqual(second.source_state.last_fetched_head_sha, third_sha)

    def test_hosted_index_clones_repo_and_attaches_github_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _repo_path, bare_path, _first_sha, second_sha = create_git_remote_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            github_api = self._make_github_api(second_sha)
            hosted = HostedIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
                github_api=github_api,  # type: ignore[arg-type]
                repo_cache=RepoCacheManager(base_dir=os.path.join(temp_dir, "cache")),
            )
            hosted.repo_resolver = StaticRepoResolver(self._make_repo_ref(bare_path))

            result = hosted.index(
                HostedIndexRequest(
                    github_repo="octo/demo",
                    repo_id="demo",
                    execute=False,
                )
            )

            queries = "\n".join(statement.query for statement in result.statements)

            self.assertEqual(result.commit_result.commit.sha, second_sha)
            self.assertEqual(result.repo_source_state.canonical_repo, "octo/demo")
            self.assertTrue(Path(result.repo_source_state.clone_path).exists())
            self.assertIsNotNone(result.github_result)
            self.assertEqual(result.github_result.pull_request_numbers, (7,))
            self.assertEqual(result.github_result.issue_numbers, (42,))
            self.assertGreater(result.github_result.episode_count, 0)
            self.assertIsNone(result.github_error)
            self.assertIn("MERGE (n:GitHubEntity:PullRequest", queries)
            self.assertIn("MERGE (project)-[r:HAS_PULL_REQUEST]->(entity)", queries)
            self.assertIn("MERGE (commit)-[r:ASSOCIATED_WITH_PR]->(entity)", queries)
            self.assertIn("MERGE (source)-[r:REFERENCES_ISSUE]->(target)", queries)
            self.assertIn("MERGE (e:Episode:ContextNode", queries)
            self.assertIn("MERGE (episode)-[r:ON]->(target)", queries)
            self.assertEqual(metadata.load_repo_source_state("demo").last_github_sync_status, "applied")

    def test_hosted_index_soft_fails_when_github_sync_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _repo_path, bare_path, _first_sha, second_sha = create_git_remote_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            github_api = FakeGitHubApiClient(fail_on_commit_prs=True)
            hosted = HostedIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
                github_api=github_api,  # type: ignore[arg-type]
                repo_cache=RepoCacheManager(base_dir=os.path.join(temp_dir, "cache")),
            )
            hosted.repo_resolver = StaticRepoResolver(self._make_repo_ref(bare_path))

            result = hosted.index(
                HostedIndexRequest(
                    github_repo="octo/demo",
                    repo_id="demo",
                    execute=False,
                )
            )

            self.assertEqual(result.commit_result.commit.sha, second_sha)
            self.assertIsNone(result.github_result)
            self.assertIn("GitHub commit pull request lookup failed", result.github_error)
            self.assertEqual(metadata.load_repo_source_state("demo").last_github_sync_status, "failed")

    def test_hosted_index_pipeline_replays_commits_and_matches_github_relationships(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _repo_path, bare_path, first_sha, second_sha = create_git_remote_repo(temp_dir)
            third_sha = push_remote_commit(_repo_path)
            metadata = InMemoryMetadataStore()
            github_api = FakeGitHubApiClient(
                repo_payload={
                    "owner": {"login": "octo"},
                    "name": "demo",
                    "clone_url": "https://github.com/octo/demo.git",
                    "html_url": "https://github.com/octo/demo",
                    "default_branch": "main",
                },
                commit_prs={
                    first_sha: [{"number": 7}],
                    second_sha: [{"number": 7}],
                    third_sha: [{"number": 7}],
                },
                linked_issue_numbers={
                    7: (42,),
                },
                pull_requests={
                    7: {
                        "id": 700,
                        "number": 7,
                        "html_url": "https://github.com/octo/demo/pull/7",
                        "title": "Feature rollout",
                        "state": "closed",
                        "user": {"login": "octocat"},
                        "created_at": "2026-03-21T00:00:00Z",
                        "updated_at": "2026-03-21T00:06:00Z",
                        "merged_at": "2026-03-21T00:07:00Z",
                        "merge_commit_sha": third_sha,
                        "head": {"ref": "main", "sha": third_sha},
                        "base": {"ref": "main"},
                        "body": "Closes #42",
                    }
                },
                reviews={
                    7: [
                        {
                            "id": 701,
                            "state": "APPROVED",
                            "user": {"login": "reviewer"},
                            "submitted_at": "2026-03-21T00:02:00Z",
                            "body": "Looks good",
                            "commit_id": second_sha,
                        }
                    ]
                },
                review_comments={
                    7: [
                        {
                            "id": 702,
                            "html_url": "https://github.com/octo/demo/pull/7#discussion_r1",
                            "user": {"login": "reviewer"},
                            "created_at": "2026-03-21T00:02:30Z",
                            "updated_at": "2026-03-21T00:02:30Z",
                            "body": "Please rename this",
                            "path": "app.py",
                            "commit_id": second_sha,
                            "pull_request_review_id": 701,
                        }
                    ]
                },
                issue_comments={
                    7: [
                        {
                            "id": 703,
                            "html_url": "https://github.com/octo/demo/issues/7#issuecomment-1",
                            "user": {"login": "commenter"},
                            "created_at": "2026-03-21T00:03:00Z",
                            "updated_at": "2026-03-21T00:03:00Z",
                            "body": "Ship it",
                        }
                    ],
                    42: [
                        {
                            "id": 704,
                            "html_url": "https://github.com/octo/demo/issues/42#issuecomment-2",
                            "user": {"login": "issue-user"},
                            "created_at": "2026-03-21T00:04:00Z",
                            "updated_at": "2026-03-21T00:04:00Z",
                            "body": "Issue discussion",
                        }
                    ],
                },
                timelines={
                    7: [
                        {
                            "id": 705,
                            "event": "merged",
                            "created_at": "2026-03-21T00:05:30Z",
                        }
                    ],
                    42: [
                        {
                            "id": 706,
                            "event": "closed",
                            "created_at": "2026-03-21T00:06:30Z",
                        }
                    ],
                },
                issues={
                    42: {
                        "id": 4200,
                        "number": 42,
                        "html_url": "https://github.com/octo/demo/issues/42",
                        "title": "Track helper rollout",
                        "state": "closed",
                        "user": {"login": "issue-author"},
                        "created_at": "2026-03-21T00:00:00Z",
                        "updated_at": "2026-03-21T00:05:00Z",
                        "body": "Issue body",
                        "closed_at": "2026-03-21T00:07:00Z",
                    },
                    99: {
                        "id": 9900,
                        "number": 99,
                        "html_url": "https://github.com/octo/demo/issues/99",
                        "title": "Control issue",
                        "state": "open",
                        "user": {"login": "issue-author"},
                        "created_at": "2026-03-21T00:00:00Z",
                        "updated_at": "2026-03-21T00:05:00Z",
                        "body": "Should remain unrelated",
                        "closed_at": None,
                    },
                },
            )
            hosted = HostedIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
                github_api=github_api,  # type: ignore[arg-type]
                repo_cache=RepoCacheManager(base_dir=os.path.join(temp_dir, "cache")),
            )
            hosted.repo_resolver = StaticRepoResolver(self._make_repo_ref(bare_path))

            first_result = hosted.index(
                HostedIndexRequest(
                    github_repo="octo/demo",
                    repo_id="demo",
                    commit_sha=second_sha,
                    execute=False,
                )
            )
            replay_result = hosted.index(
                HostedIndexRequest(
                    github_repo="octo/demo",
                    repo_id="demo",
                    execute=False,
                )
            )

            self.assertEqual(first_result.commit_result.applied_commit_shas, (second_sha,))
            self.assertEqual(replay_result.commit_result.applied_commit_shas, (third_sha,))
            self.assertEqual(replay_result.commit_result.commit.sha, third_sha)
            self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, third_sha)
            self.assertEqual(first_result.github_result.pull_request_numbers, (7,))
            self.assertEqual(first_result.github_result.issue_numbers, (42,))
            self.assertEqual(replay_result.github_result.pull_request_numbers, (7,))
            self.assertEqual(replay_result.github_result.issue_numbers, (42,))

            combined_statements = tuple(first_result.statements) + tuple(replay_result.statements)
            entity_rows = _github_entity_rows(combined_statements)
            associated_rows = _github_commit_link_rows(combined_statements, "ASSOCIATED_WITH_PR")
            references_rows = _github_commit_link_rows(combined_statements, "REFERENCES_ISSUE")
            project_pr_rows = _github_project_link_rows(combined_statements, "HAS_PULL_REQUEST")
            project_issue_rows = _github_project_link_rows(combined_statements, "HAS_ISSUE")
            episode_rows = _episode_rows(combined_statements)
            episode_on_rows = _episode_on_rows(combined_statements)
            mention_rows = _mention_rows(combined_statements)

            self.assertIn(
                {
                    "repo_id": "demo",
                    "target_node_id": "github:pr:demo:7",
                    "commit_sha": second_sha,
                    "source_node_id": None,
                    "metadata_json": "{\"pull_request_number\":7}",
                },
                associated_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "target_node_id": "github:pr:demo:7",
                    "commit_sha": third_sha,
                    "source_node_id": None,
                    "metadata_json": "{\"pull_request_number\":7}",
                },
                associated_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "target_node_id": "github:issue:demo:42",
                    "commit_sha": second_sha,
                    "source_node_id": None,
                    "metadata_json": "{\"issue_number\":42}",
                },
                references_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "target_node_id": "github:issue:demo:42",
                    "commit_sha": third_sha,
                    "source_node_id": None,
                    "metadata_json": "{\"issue_number\":42}",
                },
                references_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "target_node_id": "github:pr:demo:7",
                    "commit_sha": None,
                    "source_node_id": None,
                    "metadata_json": "{\"number\":7}",
                },
                project_pr_rows,
            )
            self.assertIn(
                {
                    "repo_id": "demo",
                    "target_node_id": "github:issue:demo:42",
                    "commit_sha": None,
                    "source_node_id": None,
                    "metadata_json": "{\"number\":42}",
                },
                project_issue_rows,
            )
            self.assertNotIn(
                {
                    "repo_id": "demo",
                    "target_node_id": "github:issue:demo:99",
                    "commit_sha": None,
                    "source_node_id": None,
                    "metadata_json": "{\"number\":99}",
                },
                project_issue_rows,
            )

            kinds_by_node = {row["node_id"]: row["kind"] for row in entity_rows}
            self.assertEqual(kinds_by_node["github:pr:demo:7"], "PullRequest")
            self.assertEqual(kinds_by_node["github:issue:demo:42"], "Issue")
            self.assertNotIn("github:issue:demo:99", kinds_by_node)
            self.assertTrue(any(row["source_type"] == "github_pr_body" for row in episode_rows))
            self.assertTrue(any(row["source_type"] == "github_issue_body" for row in episode_rows))
            self.assertTrue(any(row["source_type"] == "github_pr_review" for row in episode_rows))
            self.assertTrue(any(row["source_type"] == "github_pr_review_comment" for row in episode_rows))
            self.assertTrue(any(row["source_type"] == "github_pr_issue_comment" for row in episode_rows))
            self.assertTrue(any(row["source_type"] == "github_issue_comment" for row in episode_rows))

            self.assertTrue(
                any(
                    row["target_node_id"] == "github:pr:demo:7"
                    for row in episode_on_rows
                )
            )
            self.assertTrue(
                any(
                    row["target_node_id"] == "github:issue:demo:42"
                    for row in episode_on_rows
                )
            )
            self.assertTrue(
                any(
                    row["target_node_id"] == "github:pr:demo:7"
                    for row in mention_rows
                )
            )
            self.assertTrue(
                any(
                    row["target_node_id"] == "github:issue:demo:42"
                    for row in mention_rows
                )
            )

            for commit_sha in (second_sha, third_sha):
                self.assertEqual(
                    collect_unique_numbers(github_api.list_commit_pull_requests("octo", "demo", commit_sha)),
                    (7,),
                )
            self.assertEqual(github_api.list_pull_request_linked_issue_numbers("octo", "demo", 7), (42,))

    def test_hosted_index_cli_accepts_local_repo_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, _first_sha, second_sha = create_git_repo(temp_dir)
            db_path = os.path.join(temp_dir, "context_graph.sqlite")
            output = io.StringIO()

            with patch("context_graph.cli._producer", return_value=FakeSnapshotProducer()):
                with redirect_stdout(output):
                    exit_code = cli_main(
                        [
                            "hosted-index",
                            repo_path,
                            "--repo-id",
                            "demo",
                            "--repo-cache-dir",
                            os.path.join(temp_dir, "cache"),
                            "--metadata-db",
                            db_path,
                        ]
                    )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["commit"]["sha"], second_sha)
            self.assertIsNone(payload["repo_ref"])
            self.assertIsNone(payload["github"])


@unittest.skipUnless(
    os.getenv("CONTEXT_GRAPH_RUN_LIVE_GITHUB_TESTS") == "1"
    and (
        os.getenv("GITHUB_TOKEN")
        or Path("/tmp/context_graph_github_token").exists()
    ),
    "live GitHub pipeline test requires CONTEXT_GRAPH_RUN_LIVE_GITHUB_TESTS=1 and a GitHub token",
)
class LiveGitHubPipelineTests(unittest.TestCase):
    def _token(self) -> str:
        token = os.getenv("GITHUB_TOKEN")
        if token:
            return token
        return Path("/tmp/context_graph_github_token").read_text(encoding="utf-8").strip()

    def test_live_hosted_index_pipeline_matches_github_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = InMemoryMetadataStore()
            github_api = GitHubApiClient(token=self._token())
            hosted = HostedIndexer(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(),
                snapshot_producer=FakeSnapshotProducer(),
                github_api=github_api,
                repo_cache=RepoCacheManager(base_dir=os.path.join(temp_dir, "cache")),
            )

            first_commit = "d43d5d275f7fed193d729b1660fc30591dd408ff"
            second_commit = "5fc3fb28126b4e5e9cff07cafda4deb8095456f8"
            merge_commit = "6a07e6d9aaf86b32e00ab80cf53b3dc441159e93"

            first_result = hosted.index(
                HostedIndexRequest(
                    github_url="https://github.com/KirillUtyashev/ContextGraphTestRepo",
                    repo_id="live-demo",
                    commit_sha=first_commit,
                    execute=False,
                )
            )
            replay_result = hosted.index(
                HostedIndexRequest(
                    github_url="https://github.com/KirillUtyashev/ContextGraphTestRepo",
                    repo_id="live-demo",
                    execute=False,
                )
            )

            self.assertEqual(first_result.commit_result.applied_commit_shas, (first_commit,))
            self.assertEqual(replay_result.commit_result.applied_commit_shas, (second_commit, merge_commit))
            self.assertEqual(replay_result.commit_result.commit.sha, merge_commit)
            self.assertEqual(first_result.github_result.pull_request_numbers, (3,))
            self.assertEqual(replay_result.github_result.pull_request_numbers, (3,))
            self.assertIn(1, first_result.github_result.issue_numbers)
            self.assertIn(1, replay_result.github_result.issue_numbers)
            self.assertNotIn(2, first_result.github_result.issue_numbers)
            self.assertNotIn(2, replay_result.github_result.issue_numbers)

            combined_statements = tuple(first_result.statements) + tuple(replay_result.statements)
            associated_rows = _github_commit_link_rows(combined_statements, "ASSOCIATED_WITH_PR")
            references_rows = _github_commit_link_rows(combined_statements, "REFERENCES_ISSUE")

            self.assertTrue(
                any(
                    row["commit_sha"] == first_commit and row["target_node_id"] == "github:pr:live-demo:3"
                    for row in associated_rows
                )
            )
            self.assertTrue(
                any(
                    row["commit_sha"] == first_commit and row["target_node_id"] == "github:issue:live-demo:1"
                    for row in references_rows
                )
            )
            self.assertTrue(
                any(
                    row["commit_sha"] == second_commit and row["target_node_id"] == "github:issue:live-demo:1"
                    for row in references_rows
                )
            )
            self.assertEqual(
                collect_unique_numbers(
                    github_api.list_commit_pull_requests(
                        "KirillUtyashev",
                        "ContextGraphTestRepo",
                        first_commit,
                    )
                ),
                (3,),
            )
            self.assertEqual(
                github_api.list_pull_request_linked_issue_numbers(
                    "KirillUtyashev",
                    "ContextGraphTestRepo",
                    3,
                ),
                (1,),
            )


class GitHubRepoResolverTests(unittest.TestCase):
    def test_parse_github_repo_input_accepts_slug_and_urls(self) -> None:
        self.assertEqual(parse_github_repo_input("octo/demo"), ("octo", "demo"))
        self.assertEqual(parse_github_repo_input("https://github.com/octo/demo"), ("octo", "demo"))
        self.assertEqual(parse_github_repo_input("git@github.com:octo/demo.git"), ("octo", "demo"))

    def test_resolver_prefers_upstream_over_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, bare_path, _first_sha, _second_sha = create_git_remote_repo(temp_dir)
            upstream_path = os.path.join(temp_dir, "upstream.git")
            subprocess.run(
                ["git", "clone", "--bare", bare_path, upstream_path],
                check=True,
                capture_output=True,
                text=True,
            )
            _git(repo_path, "remote", "add", "upstream", "git@github.com:upstream/project.git")
            _git(repo_path, "remote", "set-url", "origin", "git@github.com:fork/project.git")

            resolver = GitHubRepoResolver(github_api=FakeGitHubApiClient())  # type: ignore[arg-type]
            repo_ref = resolver.resolve(repo_path=repo_path)

            self.assertEqual(repo_ref.canonical_repo, "upstream/project")
            self.assertEqual(repo_ref.source, "git_remote_upstream")


if __name__ == "__main__":
    unittest.main()
