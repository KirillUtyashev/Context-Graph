from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_graph import cli as cli_module
from context_graph.cli import main as cli_main
from context_graph.episode_processor import EpisodeProcessor, EpisodeWorkItem
from context_graph.episodes import (
    make_entity_edge,
    make_entity_node,
    make_episode,
)
from context_graph.ingestion.diffing import diff_snapshots
from context_graph.ingestion.driver import HostedIndexRequest, IngestionDriver
from context_graph.ingestion.git.git_metadata import GitMetadata
from context_graph.ingestion.git.github_client import (
    GitHubApiClient,
    GitHubRepoRef,
    GitHubRepoResolver,
    collect_unique_numbers,
    parse_github_repo_input,
)
from context_graph.ingestion.git.github_indexer import GitHubEntity, GitHubProvenanceBundle
from context_graph.ingestion.repo_cache import RepoCacheManager
from context_graph.ingestion.snapshot_builder import build_snapshot_from_graph_export
from context_graph.ingestion.temporal import TemporalGraphWriter
from context_graph.graph_read import GraphReadRepository
from context_graph.legacy_graph_backend import (
    BaseCypherExecutor,
    CypherStatement,
    GraphitiNeo4jExecutor,
    GraphitiNeptuneExecutor,
    NeptuneGraphSink,
    Neo4jGraphSink,
)
from context_graph.llm import EpisodeLLMSettings, load_episode_llm_settings
from context_graph.metadata import InMemoryMetadataStore, RepoIndexState, RepoSourceState, SQLiteMetadataStore
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.profile_vectors import (
    AuroraProfileVectorStore,
    InMemoryProfileVectorStore,
    ProfileSearchService,
    ProfileVectorIndexer,
)
from context_graph.profiles import ProfileDeactivation, ProfileRecord, SEMANTIC_FACT_PROFILE_KIND
from context_graph.query_parser import QueryParser


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
            return {
                "edges": [
                    {
                        **edge,
                        "relationship_keywords": list(edge.get("relationship_keywords", ["related"])),
                    }
                    for edge in self.extracted_edges
                ]
            }
        raise AssertionError(f"unexpected response model {model_name!r}")


class RecordingExecutor(BaseCypherExecutor):
    def __init__(
        self,
        *,
        execute_result: int = 0,
        search_result: tuple = (),
    ) -> None:
        self.execute_result = execute_result
        self.search_result = search_result
        self.validated = False
        self.executed_statements: list[CypherStatement] = []
        self.search_calls: list[dict[str, object]] = []

    def validate_execute_config(self) -> None:
        self.validated = True

    def execute(self, statements) -> int:
        self.executed_statements.extend(list(statements))
        return self.execute_result

    def search_context_nodes(
        self,
        *,
        repo_id: str,
        search_terms,
        limit: int = 25,
    ) -> tuple:
        self.search_calls.append(
            {
                "repo_id": repo_id,
                "search_terms": tuple(search_terms),
                "limit": limit,
            }
        )
        return self.search_result


class FakeQueryLLMClient:
    def __init__(
        self,
        *,
        keywords: tuple[str, ...] = (),
    ) -> None:
        self.keywords = keywords

    async def generate_response(self, messages, response_model=None, **kwargs):
        _ = (messages, kwargs)
        model_name = getattr(response_model, "__name__", "")
        if model_name == "ParsedHighLevelKeywords":
            return {
                "high_level_keywords": list(self.keywords),
            }
        raise AssertionError(f"unexpected response model {model_name!r}")


class FakeEmbeddingProvider:
    def embed_texts(self, texts):
        vectors = []
        for text in texts:
            normalized = text.casefold()
            vectors.append(
                [
                    1.0 if "helper" in normalized else 0.0,
                    1.0 if "main" in normalized else 0.0,
                    1.0 if "rollback" in normalized else 0.0,
                    float(len(normalized.split())),
                ]
            )
        return vectors


class RecordingEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed_texts(self, texts):
        batch = tuple(str(text) for text in texts)
        self.calls.append(batch)
        return [
            [float(index + 1), float(len(text))]
            for index, text in enumerate(batch)
        ]


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection
        self._fetchall = []
        self._fetchone = None

    def execute(self, sql, params=()):
        self.connection.executed.append((sql, params))
        if "SELECT COUNT(*) FROM cg_profile_keys" in sql:
            self._fetchone = (0,)
        elif "SELECT" in sql and "FROM cg_profile_keys" in sql:
            self._fetchall = [
                (
                    "profile:demo:semantic_fact_profile:fact:1",
                    "pkey:test",
                    "demo",
                    SEMANTIC_FACT_PROFILE_KIND,
                    "helper rollback",
                    0.91,
                )
            ]
        else:
            self._fetchall = []
            self._fetchone = None
        return self

    def executemany(self, sql, rows):
        materialized = list(rows)
        self.connection.executemany_calls.append((sql, materialized))
        return self

    def fetchall(self):
        return list(self._fetchall)

    def fetchone(self):
        return self._fetchone


class RecordingConnection:
    def __init__(self):
        self.executed = []
        self.executemany_calls = []
        self.commits = 0

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1


def _statement_rows(statements, query_fragment: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for statement in statements:
        if query_fragment in statement.query:
            rows.extend(dict(row) for row in statement.parameters.get("rows", []))
    return rows


def _statement_parameters(statements, query_fragment: str) -> dict[str, object]:
    for statement in statements:
        if query_fragment in statement.query:
            return dict(statement.parameters)
    raise AssertionError(f"no statement matched {query_fragment!r}")


def _code_node_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (n:CodeNode:")


def _context_entity_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (n:Entity:")


def _github_entity_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (n:GitHubEntity:")


def _github_commit_link_rows(statements, relation_type: str) -> list[dict[str, object]]:
    return _statement_rows(statements, f"MERGE (commit)-[r:{relation_type}]->(entity)")


def _github_project_link_rows(statements, relation_type: str) -> list[dict[str, object]]:
    return _statement_rows(statements, f"MERGE (project)-[r:{relation_type}]->(entity)")


def _github_entity_link_rows(statements, relation_type: str) -> list[dict[str, object]]:
    return _statement_rows(statements, f"MERGE (source)-[r:{relation_type}]->(target)")


def _episode_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (e:Episode:Episodic")


def _episode_on_rows(statements) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for statement in statements:
        if "MERGE (episode)-[r:ON]->(target)" in statement.query:
            rows.append(dict(statement.parameters))
    return rows


def _mention_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (episode)-[r:MENTIONS")


def _fact_rows(statements) -> list[dict[str, object]]:
    return _statement_rows(statements, "MERGE (source)-[r:RELATES_TO")


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


class QueryParserTests(unittest.TestCase):
    def test_query_parser_extracts_deterministic_anchors(self) -> None:
        parser = QueryParser()

        result = parser.parse("Why was `helper` added to app.py in commit abc1234 for #42?")

        anchors = {(anchor.kind, anchor.value) for anchor in result.anchors}
        self.assertIn(("quoted", "helper"), anchors)
        self.assertIn(("symbol", "app.py"), anchors)
        self.assertIn(("commit_sha", "abc1234"), anchors)
        self.assertIn(("artifact_number", "#42"), anchors)
        self.assertIn("why", result.intent_tags)

    def test_query_parser_merges_llm_high_level_keywords_with_deterministic_intents(self) -> None:
        parser = QueryParser(
            llm_client=FakeQueryLLMClient(
                keywords=("auth rollback", "stability"),
            )
        )

        result = parser.parse("What is the rollback impact for auth?", llm_enabled=True)

        self.assertEqual(result.high_level_keywords, ("auth rollback", "stability"))
        self.assertIn("rollback", result.intent_tags)
        self.assertIn("impact", result.intent_tags)


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
        writer = TemporalGraphWriter(metadata_store=metadata, graph_sink=Neo4jGraphSink(executor=RecordingExecutor()))

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

        neo4j_statements = Neo4jGraphSink(executor=RecordingExecutor()).plan_apply_diff(after, diff)
        neptune_statements = NeptuneGraphSink(executor=RecordingExecutor()).plan_apply_diff(after, diff)

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

    def test_sink_execute_delegates_to_injected_executor(self) -> None:
        executor = RecordingExecutor(execute_result=2)
        sink = Neo4jGraphSink(executor=executor)
        statements = (
            CypherStatement("RETURN 1", {}),
            CypherStatement("RETURN 2", {"value": 2}),
        )

        executed = sink.execute(statements)

        self.assertEqual(executed, 2)
        self.assertEqual(executor.executed_statements, list(statements))

    def test_sink_validate_and_search_delegate_to_injected_executor(self) -> None:
        executor = RecordingExecutor()
        sink = Neo4jGraphSink(executor=executor)

        sink.validate_execute_config()
        sink.search_context_nodes(repo_id="demo", search_terms=("helper",), limit=5)

        self.assertTrue(executor.validated)
        self.assertEqual(
            executor.search_calls,
            [{"repo_id": "demo", "search_terms": ("helper",), "limit": 5}],
        )

    def test_make_sink_uses_graphiti_executor_for_neo4j(self) -> None:
        sink = cli_module._make_sink(
            SimpleNamespace(
                graph_backend="neo4j",
                neo4j_uri="bolt://localhost:7687",
                neo4j_user="neo4j",
                neo4j_password="contextgraph",
                neo4j_database="neo4j",
                neptune_host=None,
                neptune_port=8182,
            )
        )

        self.assertIsInstance(sink, Neo4jGraphSink)
        self.assertIsInstance(sink.executor, GraphitiNeo4jExecutor)
        self.assertEqual(sink.executor.uri, "bolt://localhost:7687")
        self.assertEqual(sink.executor.username, "neo4j")
        self.assertEqual(sink.executor.password, "contextgraph")
        self.assertEqual(sink.executor.database, "neo4j")

    def test_neptune_executor_validate_requires_host(self) -> None:
        executor = GraphitiNeptuneExecutor()
        with self.assertRaises(RuntimeError):
            executor.validate_execute_config()

    def test_neptune_executor_validate_passes_with_driver(self) -> None:
        executor = GraphitiNeptuneExecutor(driver=object())
        executor.validate_execute_config()

    def test_neptune_sink_defaults_to_neptune_executor(self) -> None:
        sink = NeptuneGraphSink(host="my-neptune-endpoint")
        self.assertIsInstance(sink.executor, GraphitiNeptuneExecutor)
        self.assertEqual(sink.executor.host, "my-neptune-endpoint")
        self.assertEqual(sink.executor.host_scheme, "neptune-db")
        self.assertEqual(sink.executor.port, 8182)
        self.assertIsNone(sink.executor.aoss_host)

    def test_neptune_sink_passes_aoss_host(self) -> None:
        sink = NeptuneGraphSink(
            host="my-neptune-endpoint",
            aoss_host="my-aoss-host",
            host_scheme="neptune-graph",
        )
        self.assertIsInstance(sink.executor, GraphitiNeptuneExecutor)
        self.assertEqual(sink.executor.aoss_host, "my-aoss-host")
        self.assertEqual(sink.executor.host_scheme, "neptune-graph")

    def test_make_sink_uses_neptune_executor_for_neptune_backend(self) -> None:
        sink = cli_module._make_sink(
            SimpleNamespace(
                graph_backend="neptune",
                neptune_host="my-neptune-cluster",
                neptune_port=8182,
                neptune_host_scheme="neptune-db",
                aoss_host=None,
                neo4j_uri=None,
                neo4j_user=None,
                neo4j_password=None,
                neo4j_database=None,
            )
        )

        self.assertIsInstance(sink, NeptuneGraphSink)
        self.assertIsInstance(sink.executor, GraphitiNeptuneExecutor)
        self.assertEqual(sink.executor.host, "my-neptune-cluster")
        self.assertEqual(sink.executor.host_scheme, "neptune-db")

    def test_make_sink_threads_embedding_provider_into_graph_sink(self) -> None:
        provider = object()

        with patch("context_graph.cli._make_embedding_provider", return_value=provider):
            sink = cli_module._make_sink(
                SimpleNamespace(
                    graph_backend="neo4j",
                    neo4j_uri="bolt://localhost:7687",
                    neo4j_user="neo4j",
                    neo4j_password="contextgraph",
                    neo4j_database="neo4j",
                    neptune_host=None,
                    neptune_port=8182,
                )
            )

        self.assertIs(sink.embedding_provider, provider)

    def test_plan_apply_diff_enriches_project_and_code_nodes_for_graphiti_search(self) -> None:
        snapshot = make_snapshot("r1", include_extra_call=True)
        diff = diff_snapshots(None, snapshot)
        embedder = RecordingEmbeddingProvider()
        sink = Neo4jGraphSink(executor=RecordingExecutor(), embedding_provider=embedder)

        statements = sink.plan_apply_diff(
            snapshot,
            diff,
            observed_at="2026-03-24T00:00:00+00:00",
        )

        project_params = _statement_parameters(statements, "MERGE (project:CodeNode:Project:Entity")
        code_rows = _code_node_rows(statements)
        main_row = next(row for row in code_rows if row["node_id"] == "symbol:demo:demo.app.main")

        self.assertEqual(project_params["uuid"], "project:demo")
        self.assertEqual(project_params["group_id"], "demo")
        self.assertEqual(project_params["created_at"], "2026-03-24T00:00:00+00:00")
        self.assertIn("Project demo", project_params["summary"])
        self.assertIsNotNone(project_params["name_embedding"])

        self.assertEqual(main_row["uuid"], "symbol:demo:demo.app.main")
        self.assertEqual(main_row["group_id"], "demo")
        self.assertEqual(main_row["created_at"], "2026-03-24T00:00:00+00:00")
        self.assertIn("Function main", main_row["summary"])
        self.assertIn("qualified demo.app.main", main_row["summary"])
        self.assertIn("file app.py", main_row["summary"])
        self.assertIsNotNone(main_row["name_embedding"])
        self.assertTrue(any("main" in call for call in embedder.calls))

    def test_plan_upsert_context_nodes_enriches_rows_and_skips_blank_names(self) -> None:
        embedder = RecordingEmbeddingProvider()
        sink = Neo4jGraphSink(executor=RecordingExecutor(), embedding_provider=embedder)
        statements = sink.plan_upsert_context_nodes(
            [
                make_entity_node(
                    node_id="entity:demo:alice",
                    repo_id="demo",
                    kind="Entity",
                    name="Alice Johnson",
                    aliases=("Alice",),
                    metadata={"origin": "episode_llm"},
                ),
                make_entity_node(
                    node_id="entity:demo:blank",
                    repo_id="demo",
                    kind="Entity",
                    name="",
                    metadata={"origin": "test"},
                ),
            ],
            observed_at="2026-03-24T01:00:00+00:00",
        )

        context_rows = _context_entity_rows(statements)
        alice_row = next(row for row in context_rows if row["node_id"] == "entity:demo:alice")
        blank_row = next(row for row in context_rows if row["node_id"] == "entity:demo:blank")

        self.assertEqual(alice_row["uuid"], "entity:demo:alice")
        self.assertEqual(alice_row["group_id"], "demo")
        self.assertEqual(alice_row["created_at"], "2026-03-24T01:00:00+00:00")
        self.assertIn("Entity Alice Johnson", alice_row["summary"])
        self.assertIn("aliases Alice", alice_row["summary"])
        self.assertIsNotNone(alice_row["name_embedding"])

        self.assertEqual(blank_row["group_id"], "demo")
        self.assertEqual(blank_row["created_at"], "2026-03-24T01:00:00+00:00")
        self.assertEqual(blank_row["name_embedding"], None)
        self.assertEqual(embedder.calls, [("Alice Johnson",)])

    def test_plan_attach_github_provenance_normalizes_github_entities(self) -> None:
        embedder = RecordingEmbeddingProvider()
        sink = Neo4jGraphSink(executor=RecordingExecutor(), embedding_provider=embedder)
        bundle = GitHubProvenanceBundle(
            repo_id="demo",
            github_repo="octo/demo",
            entities=(
                GitHubEntity(
                    node_id="github:pr:demo:7",
                    kind="PullRequest",
                    repo_id="demo",
                    github_repo="octo/demo",
                    external_id="700",
                    number=7,
                    url="https://github.com/octo/demo/pull/7",
                    title="Feature rollout",
                    state="closed",
                    author_login="octocat",
                    created_at="2026-03-21T00:00:00Z",
                    updated_at="2026-03-21T00:06:00Z",
                    raw={},
                    metadata={},
                ),
                GitHubEntity(
                    node_id="github:issue:demo:42",
                    kind="Issue",
                    repo_id="demo",
                    github_repo="octo/demo",
                    external_id="4200",
                    number=42,
                    url="https://github.com/octo/demo/issues/42",
                    title=None,
                    state="open",
                    author_login="issue-author",
                    created_at=None,
                    updated_at="2026-03-21T00:05:00Z",
                    raw={},
                    metadata={},
                ),
            ),
            project_links=(),
            commit_links=(),
            entity_links=(),
            episode_work_items=(),
        )

        statements = sink.plan_attach_github_provenance(
            bundle,
            observed_at="2026-03-24T02:00:00+00:00",
        )

        entity_rows = _github_entity_rows(statements)
        pr_row = next(row for row in entity_rows if row["node_id"] == "github:pr:demo:7")
        issue_row = next(row for row in entity_rows if row["node_id"] == "github:issue:demo:42")

        self.assertEqual(pr_row["uuid"], "github:pr:demo:7")
        self.assertEqual(pr_row["group_id"], "demo")
        self.assertEqual(pr_row["name"], "Feature rollout")
        self.assertIn("repo octo/demo", pr_row["summary"])
        self.assertIn("url https://github.com/octo/demo/pull/7", pr_row["summary"])
        self.assertIsNotNone(pr_row["name_embedding"])

        self.assertEqual(issue_row["uuid"], "github:issue:demo:42")
        self.assertEqual(issue_row["group_id"], "demo")
        self.assertEqual(issue_row["created_at"], "2026-03-24T02:00:00+00:00")
        self.assertEqual(issue_row["name"], "Issue #42")
        self.assertIn("number 42", issue_row["summary"])
        self.assertIsNotNone(issue_row["name_embedding"])
        self.assertTrue(any("Issue #42" in call for call in embedder.calls))

    def test_plan_apply_fact_edges_sets_graphiti_edge_fields_and_embeds_only_nonempty_facts(self) -> None:
        embedder = RecordingEmbeddingProvider()
        sink = Neo4jGraphSink(executor=RecordingExecutor(), embedding_provider=embedder)
        statements = sink.plan_apply_fact_edges(
            [
                make_entity_edge(
                    fact_id="fact:demo:1",
                    repo_id="demo",
                    source_node_id="symbol:demo:demo.app.main",
                    target_node_id="symbol:demo:demo.app.helper",
                    relation_type="JUSTIFIES",
                    fact="main uses helper for shared logic",
                ),
                make_entity_edge(
                    fact_id="fact:demo:2",
                    repo_id="demo",
                    source_node_id="symbol:demo:demo.app.helper",
                    target_node_id="symbol:demo:demo.app.main",
                    relation_type="RELATES_TO",
                    fact="",
                ),
            ],
            observed_at="2026-03-24T03:00:00+00:00",
        )

        fact_rows = _fact_rows(statements)
        first_row = next(row for row in fact_rows if row["fact_id"] == "fact:demo:1")
        second_row = next(row for row in fact_rows if row["fact_id"] == "fact:demo:2")

        self.assertEqual(first_row["uuid"], "fact:demo:1")
        self.assertEqual(first_row["group_id"], "demo")
        self.assertEqual(first_row["episodes"], [])
        self.assertIsNotNone(first_row["fact_embedding"])
        self.assertEqual(second_row["uuid"], "fact:demo:2")
        self.assertEqual(second_row["group_id"], "demo")
        self.assertEqual(second_row["episodes"], [])
        self.assertEqual(second_row["fact_embedding"], None)
        self.assertEqual(embedder.calls, [("main uses helper for shared logic",)])

    def test_plan_reset_repo_emits_code_node_deletes(self) -> None:
        statements = Neo4jGraphSink(executor=RecordingExecutor()).plan_reset_repo("demo")
        queries = "\n".join(statement.query for statement in statements)

        self.assertEqual(len(statements), 6)
        self.assertIn("MATCH (e:Episode {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (g:GitHubEntity {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (c:Commit {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (v:CodeNodeVersion {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (n:CodeNode {repo_id: $repo_id})", queries)
        self.assertIn("MATCH (n {repo_id: $repo_id}) WHERE (n:Entity OR n:Episodic)", queries)
        self.assertIn("AND NOT n:CodeNode", queries)
        self.assertTrue(all(statement.parameters == {"repo_id": "demo"} for statement in statements))

    def test_episode_processor_plans_episode_on_and_mentions(self) -> None:
        metadata = InMemoryMetadataStore()
        metadata.save_latest_snapshot(make_snapshot("r1", include_extra_call=True))
        processor = EpisodeProcessor(
            graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
            read_repository=GraphReadRepository(metadata_store=metadata),
        )

        batch = processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=make_episode(
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

        self.assertIn("MERGE (e:Episode:Episodic", queries)
        self.assertIn("MERGE (episode)-[r:ON]->(target)", queries)
        self.assertIn("MERGE (episode)-[r:MENTIONS", queries)
        self.assertTrue(any(row["source_type"] == "user_message" for row in episode_rows))
        self.assertTrue(any(row["target_node_id"] == "project:demo" for row in on_rows))
        self.assertTrue(any(row["target_node_id"] == "symbol:demo:demo.app.main" for row in mention_rows))

    def test_episode_processor_merges_deterministic_and_llm_mentions(self) -> None:
        metadata = InMemoryMetadataStore()
        metadata.save_latest_snapshot(make_snapshot("r1", include_extra_call=True))
        processor = EpisodeProcessor(
            graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
            read_repository=GraphReadRepository(metadata_store=metadata),
            llm_client=FakeEpisodeLLMClient(
                extracted_entity_names=("helper", "app.py"),
            ),
        )

        batch = processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=make_episode(
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
        executor = RecordingExecutor()
        sink = Neo4jGraphSink(executor=executor)
        sink.execute = lambda statements: 0  # type: ignore[method-assign]
        processor = EpisodeProcessor(
            graph_sink=sink,
            read_repository=GraphReadRepository(metadata_store=metadata, graph_sink=sink),
            llm_client=FakeEpisodeLLMClient(
                extracted_entity_names=("Alice Johnson",),
            ),
        )

        batch = processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=make_episode(
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
        entity_rows = _statement_rows(batch.statements, "MERGE (n:Entity:Entity")

        self.assertEqual(len(entity_rows), 1)
        self.assertEqual(entity_rows[0]["name"], "Alice Johnson")
        self.assertEqual(len(mention_rows), 1)
        self.assertEqual(mention_rows[0]["target_node_id"], entity_rows[0]["node_id"])

    def test_episode_processor_persists_semantic_fact_profiles_for_relates_to(self) -> None:
        metadata = InMemoryMetadataStore()
        metadata.save_latest_snapshot(make_snapshot("r1", include_extra_call=True))
        executor = RecordingExecutor()
        sink = Neo4jGraphSink(executor=executor)
        sink.execute = lambda statements: 0  # type: ignore[method-assign]
        processor = EpisodeProcessor(
            graph_sink=sink,
            read_repository=GraphReadRepository(metadata_store=metadata, graph_sink=sink),
            llm_client=FakeEpisodeLLMClient(
                extracted_entity_names=("main", "helper"),
                extracted_edges=(
                    {
                        "source_entity_name": "main",
                        "target_entity_name": "helper",
                        "relation_type": "JUSTIFIES",
                        "fact": "main uses helper for shared logic",
                        "valid_at": None,
                        "invalid_at": None,
                    },
                ),
            ),
        )

        first_batch = processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=make_episode(
                        episode_id="turn-4",
                        repo_id="demo",
                        source_type="user_message",
                        source_id="turn-4",
                        author="user",
                        created_at="2026-03-22T00:00:00+00:00",
                        updated_at="2026-03-22T00:00:00+00:00",
                        text="Why does `main` use `helper` in app.py?",
                        metadata={"thread_id": "chat-4"},
                    )
                )
            ],
            execute=True,
        )

        self.assertEqual(len(first_batch.results[0].profiles), 1)
        profiles = metadata.load_profiles("demo", active_only=True)
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].profile_kind, SEMANTIC_FACT_PROFILE_KIND)
        self.assertEqual(profiles[0].profile_target_kind, "FactEdge")
        self.assertEqual(profiles[0].relation_type, "JUSTIFIES")
        self.assertIn("main", profiles[0].value_text.lower())
        self.assertIn("helper", profiles[0].value_text.lower())
        self.assertIn("main", [key.lower() for key in profiles[0].keys])
        self.assertIn("helper", [key.lower() for key in profiles[0].keys])

        processor = EpisodeProcessor(
            graph_sink=sink,
            read_repository=GraphReadRepository(metadata_store=metadata, graph_sink=sink),
            llm_client=FakeEpisodeLLMClient(
                extracted_entity_names=("main", "helper"),
                extracted_edges=(
                    {
                        "source_entity_name": "main",
                        "target_entity_name": "helper",
                        "relation_type": "JUSTIFIES",
                        "fact": "main delegates shared helper behavior",
                        "valid_at": None,
                        "invalid_at": None,
                    },
                ),
            ),
        )

        processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=make_episode(
                        episode_id="turn-5",
                        repo_id="demo",
                        source_type="user_message",
                        source_id="turn-5",
                        author="user",
                        created_at="2026-03-22T00:10:00+00:00",
                        updated_at="2026-03-22T00:10:00+00:00",
                        text="Actually, `main` delegates behavior to `helper`.",
                        metadata={"thread_id": "chat-4"},
                    )
                )
            ],
            execute=True,
        )

        all_profiles = metadata.load_profiles("demo", active_only=False)
        active_profiles = metadata.load_profiles("demo", active_only=True)
        self.assertEqual(len(all_profiles), 2)
        self.assertEqual(len(active_profiles), 1)
        inactive_profiles = [profile for profile in all_profiles if not profile.is_active]
        self.assertEqual(len(inactive_profiles), 1)
        self.assertIn("uses helper for shared logic", inactive_profiles[0].value_text)
        self.assertIn("delegates shared helper behavior", active_profiles[0].value_text)

    def test_episode_processor_can_sync_profiles_into_vector_index(self) -> None:
        metadata = InMemoryMetadataStore()
        metadata.save_latest_snapshot(make_snapshot("r1", include_extra_call=True))
        executor = RecordingExecutor()
        sink = Neo4jGraphSink(executor=executor)
        sink.execute = lambda statements: 0  # type: ignore[method-assign]
        vector_store = InMemoryProfileVectorStore()
        processor = EpisodeProcessor(
            graph_sink=sink,
            read_repository=GraphReadRepository(metadata_store=metadata, graph_sink=sink),
            llm_client=FakeEpisodeLLMClient(
                extracted_entity_names=("main", "helper"),
                extracted_edges=(
                    {
                        "source_entity_name": "main",
                        "target_entity_name": "helper",
                        "relation_type": "JUSTIFIES",
                        "fact": "main uses helper for shared logic",
                        "valid_at": None,
                        "invalid_at": None,
                    },
                ),
            ),
            profile_indexer=ProfileVectorIndexer(
                vector_store=vector_store,
                embedding_provider=FakeEmbeddingProvider(),
            ),
        )

        processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=make_episode(
                        episode_id="turn-6",
                        repo_id="demo",
                        source_type="user_message",
                        source_id="turn-6",
                        author="user",
                        created_at="2026-03-22T00:00:00+00:00",
                        updated_at="2026-03-22T00:00:00+00:00",
                        text="Why does `main` use `helper` in app.py?",
                        metadata={"thread_id": "chat-5"},
                    )
                )
            ],
            execute=True,
        )

        hits = vector_store.search_profile_keys(
            repo_id="demo",
            embedding=FakeEmbeddingProvider().embed_texts(["helper"])[0],
            limit=5,
            active_only=True,
        )
        self.assertTrue(hits)
        self.assertEqual(hits[0].profile_kind, SEMANTIC_FACT_PROFILE_KIND)


class ProfileVectorStoreTests(unittest.TestCase):
    def test_aurora_profile_vector_store_bootstraps_pgvector_schema(self) -> None:
        connection = RecordingConnection()
        store = AuroraProfileVectorStore(connection, embedding_dim=1024)

        store.bootstrap()

        sql_text = "\n".join(sql for sql, _ in connection.executed)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", sql_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS cg_profile_keys", sql_text)
        self.assertIn("vector(1024)", sql_text)
        self.assertIn("USING hnsw", sql_text)

    def test_profile_search_service_uses_query_parser_and_vector_hits(self) -> None:
        metadata = InMemoryMetadataStore()
        profile = ProfileRecord(
            profile_id="profile:demo:semantic_fact_profile:fact:1",
            repo_id="demo",
            profile_kind=SEMANTIC_FACT_PROFILE_KIND,
            profile_target_kind="FactEdge",
            profile_target_id="fact:1",
            relation_type="JUSTIFIES",
            source_node_id="symbol:demo:demo.app.main",
            source_node_kind="Function",
            target_node_id="symbol:demo:demo.app.helper",
            target_node_kind="Function",
            target_revision_id=None,
            value_text="Function main justifies Function helper. main uses helper for shared logic",
            keys=("helper rollback", "main helper"),
            metadata={"episode_ids": ["turn-1"]},
            is_active=True,
            updated_at="2026-03-22T00:00:00+00:00",
        )
        metadata.upsert_profiles((profile,))

        vector_store = InMemoryProfileVectorStore()
        indexer = ProfileVectorIndexer(
            vector_store=vector_store,
            embedding_provider=FakeEmbeddingProvider(),
        )
        indexer.sync_repo(repo_id="demo", metadata_store=metadata)

        service = ProfileSearchService(
            metadata_store=metadata,
            vector_store=vector_store,
            embedding_provider=FakeEmbeddingProvider(),
            query_parser=QueryParser(
                llm_client=FakeQueryLLMClient(
                    keywords=("helper rollback",),
                )
            ),
        )

        result = service.search(
            repo_id="demo",
            query="What is the rollback plan for helper?",
            limit=5,
            active_only=True,
            llm_enabled=True,
        )

        self.assertEqual(result.parse_result.high_level_keywords, ("helper rollback",))
        self.assertEqual(result.parse_result.intent_tags, ("rollback",))
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.profiles[0].profile_id, profile.profile_id)


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

    def test_sqlite_metadata_store_round_trips_and_deactivates_profiles(self) -> None:
        store = SQLiteMetadataStore(":memory:")
        profile = ProfileRecord(
            profile_id="profile:demo:semantic_fact_profile:fact:1",
            repo_id="demo",
            profile_kind=SEMANTIC_FACT_PROFILE_KIND,
            profile_target_kind="FactEdge",
            profile_target_id="fact:1",
            relation_type="JUSTIFIES",
            source_node_id="symbol:demo:demo.app.main",
            source_node_kind="Function",
            target_node_id="symbol:demo:demo.app.helper",
            target_node_kind="Function",
            target_revision_id=None,
            value_text="Function main justifies Function helper. main uses helper for shared logic",
            keys=("JUSTIFIES", "main", "helper"),
            metadata={"episode_ids": ["turn-1"]},
            is_active=True,
            updated_at="2026-03-22T00:00:00+00:00",
        )

        store.upsert_profiles((profile,))
        loaded = store.load_profiles("demo", active_only=True)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].profile_id, profile.profile_id)
        self.assertEqual(loaded[0].keys, profile.keys)

        store.deactivate_profiles(
            (
                ProfileDeactivation(
                    repo_id="demo",
                    profile_kind=SEMANTIC_FACT_PROFILE_KIND,
                    relation_type="JUSTIFIES",
                    source_node_id="symbol:demo:demo.app.main",
                    target_node_id="symbol:demo:demo.app.helper",
                    observed_at="2026-03-22T00:05:00+00:00",
                ),
            )
        )

        self.assertEqual(store.load_profiles("demo", active_only=True), ())
        loaded_all = store.load_profiles("demo", active_only=False)
        self.assertEqual(len(loaded_all), 1)
        self.assertFalse(loaded_all[0].is_active)
        self.assertEqual(loaded_all[0].updated_at, "2026-03-22T00:05:00+00:00")

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
            indexer = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
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
            indexer = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
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

            self.assertEqual(episode_rows, [])
            self.assertEqual(episode_on_rows, [])

    def test_apply_latest_commit_skips_when_head_is_already_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, _first_sha, _second_sha = create_git_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
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
            indexer = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
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
            indexer = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
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
            indexer = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
                snapshot_producer=FakeSnapshotProducer(),
            )

            indexer.apply_commit(repo_path, commit_sha=second_sha, repo_id="demo", execute=False)

            with self.assertRaisesRegex(RuntimeError, "is not an ancestor of target commit"):
                indexer.apply_commit(repo_path, commit_sha=first_sha, repo_id="demo", execute=False)

    def test_commit_statements_cover_added_changed_and_removed_nodes_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha, third_sha = create_git_repo_with_deletions(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
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

    def test_hosted_index_clones_repo_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _repo_path, bare_path, _first_sha, second_sha = create_git_remote_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            github_api = self._make_github_api(second_sha)
            hosted = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
                snapshot_producer=FakeSnapshotProducer(),
                github_api=github_api,  # type: ignore[arg-type]
                repo_cache=RepoCacheManager(base_dir=os.path.join(temp_dir, "cache")),
            )
            hosted.repo_resolver = StaticRepoResolver(self._make_repo_ref(bare_path))

            result = hosted.index_hosted(
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
            self.assertNotIn("MERGE (n:GitHubEntity:PullRequest", queries)
            self.assertNotIn("MERGE (project)-[r:HAS_PULL_REQUEST]->(entity)", queries)
            self.assertNotIn("MERGE (commit)-[r:ASSOCIATED_WITH_PR]->(entity)", queries)
            self.assertNotIn("MERGE (source)-[r:REFERENCES_ISSUE]->(target)", queries)
            self.assertNotIn("MERGE (e:Episode:Episodic", queries)
            self.assertNotIn("MERGE (episode)-[r:ON]->(target)", queries)
            self.assertIsNone(metadata.load_repo_source_state("demo").last_github_sync_status)

    def test_hosted_index_does_not_call_github_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _repo_path, bare_path, _first_sha, second_sha = create_git_remote_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            github_api = FakeGitHubApiClient(fail_on_commit_prs=True)
            hosted = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
                snapshot_producer=FakeSnapshotProducer(),
                github_api=github_api,  # type: ignore[arg-type]
                repo_cache=RepoCacheManager(base_dir=os.path.join(temp_dir, "cache")),
            )
            hosted.repo_resolver = StaticRepoResolver(self._make_repo_ref(bare_path))

            result = hosted.index_hosted(
                HostedIndexRequest(
                    github_repo="octo/demo",
                    repo_id="demo",
                    execute=False,
                )
            )

            self.assertEqual(result.commit_result.commit.sha, second_sha)
            self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, second_sha)
            self.assertIsNone(metadata.load_repo_source_state("demo").last_github_sync_status)

    def test_hosted_index_pipeline_replays_commits_deterministically(self) -> None:
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
            hosted = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
                snapshot_producer=FakeSnapshotProducer(),
                github_api=github_api,  # type: ignore[arg-type]
                repo_cache=RepoCacheManager(base_dir=os.path.join(temp_dir, "cache")),
            )
            hosted.repo_resolver = StaticRepoResolver(self._make_repo_ref(bare_path))

            first_result = hosted.index_hosted(
                HostedIndexRequest(
                    github_repo="octo/demo",
                    repo_id="demo",
                    commit_sha=second_sha,
                    execute=False,
                )
            )
            replay_result = hosted.index_hosted(
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
            combined_queries = "\n".join(statement.query for statement in (*first_result.statements, *replay_result.statements))
            self.assertNotIn("GitHubEntity", combined_queries)
            self.assertNotIn("HAS_PULL_REQUEST", combined_queries)
            self.assertNotIn("REFERENCES_ISSUE", combined_queries)
            self.assertNotIn("Episode:Episodic", combined_queries)

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
            self.assertNotIn("github", payload)


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

    def test_live_hosted_index_pipeline_replays_commits_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = InMemoryMetadataStore()
            github_api = GitHubApiClient(token=self._token())
            hosted = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
                snapshot_producer=FakeSnapshotProducer(),
                github_api=github_api,
                repo_cache=RepoCacheManager(base_dir=os.path.join(temp_dir, "cache")),
            )

            first_commit = "d43d5d275f7fed193d729b1660fc30591dd408ff"
            second_commit = "5fc3fb28126b4e5e9cff07cafda4deb8095456f8"
            merge_commit = "6a07e6d9aaf86b32e00ab80cf53b3dc441159e93"

            first_result = hosted.index_hosted(
                HostedIndexRequest(
                    github_url="https://github.com/KirillUtyashev/ContextGraphTestRepo",
                    repo_id="live-demo",
                    commit_sha=first_commit,
                    execute=False,
                )
            )
            replay_result = hosted.index_hosted(
                HostedIndexRequest(
                    github_url="https://github.com/KirillUtyashev/ContextGraphTestRepo",
                    repo_id="live-demo",
                    execute=False,
                )
            )

            self.assertEqual(first_result.commit_result.applied_commit_shas, (first_commit,))
            self.assertEqual(replay_result.commit_result.applied_commit_shas, (second_commit, merge_commit))
            self.assertEqual(replay_result.commit_result.commit.sha, merge_commit)
            combined_queries = "\n".join(statement.query for statement in (*first_result.statements, *replay_result.statements))
            self.assertNotIn("GitHubEntity", combined_queries)
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
