from __future__ import annotations

import asyncio
import json
import io
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import context_graph.cli as cli_module

from context_graph.ingestion.commit_indexing import CommitIndexingService, normalize_snapshot_for_commit
from context_graph.ingestion.deterministic_planner import DeterministicMutationPlanner
from context_graph.ingestion.git.github_client import GitHubApiClient, GitHubRepoRef
from context_graph.ingestion.git.github_indexer import (
    GitHubCommitLink,
    GitHubEntity,
    GitHubEntityLink,
    GitHubProjectLink,
    GitHubProvenanceBundle,
    GitHubSyncResult,
)
from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.ingestion.git.structural_planner import GitHubStructuralPlanner
from context_graph.ingestion.hosted_indexing import HostedIndexingService
from context_graph.ingestion import diff_snapshots
from context_graph.ingestion.models import ManagedRepo
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.backend.profile_vector_services import (
    ProfileSearchService,
    ProfileVectorIndexer,
)
from context_graph.models.profiles import (
    PROFILE_KEY_ORIGIN_RAW_TEXT,
    ProfileDeactivation,
    ProfileKeyDefinition,
    ProfileRecord,
    RelationProfileSpec,
    SEMANTIC_RELATION_PROFILE_KIND,
    semantic_relation_profile_from_spec,
)
from context_graph.query.parser import QueryParseResult
from context_graph.backend import (
    BackendFeatureNotImplementedError,
    EdgeRecord,
    GraphRecordRef,
    InMemoryMetadataConfig,
    InMemoryMetadataStore,
    InMemoryProfileVectorConfig,
    InMemoryProfileVectorStore,
    MetadataStore,
    MutationPlan,
    MutationApplyResult,
    Neo4jConfig,
    NodeRecord,
    PgVectorProfileVectorStore,
    PostgresMetadataStore,
    ProfileKeyRecord,
    RecordKind,
    RepoIndexState,
    RepoSourceState,
    SearchDocumentRecord,
    SearchMode,
    SearchQuery,
    TraversalQuery,
    SQLiteMetadataStore,
    NeptuneConfig,
    create_driver,
    create_metadata_store,
    create_profile_vector_store,
    create_profile_vector_store_from_metadata,
)
from context_graph.backend import BackendSyncBridge
from context_graph.backend.neo4j import Neo4jBackendDriver
from context_graph.backend.neo4j import Neo4jSearchOperations
from context_graph.backend.neptune.driver import NeptuneBackendDriver
from context_graph.backend.neptune.search import (
    NEPTUNE_NODE_AOSS_INDEX,
    NeptuneSearchOperations,
)


class FakeRecord:
    def __init__(self, payload: dict):
        self._payload = payload

    def data(self) -> dict:
        return self._payload


class FakeNeo4jSession:
    def __init__(self):
        self.run_calls: list[tuple[str, dict]] = []
        self.closed = False

    async def run(self, query: str, params: dict):
        self.run_calls.append((query, params))
        return SimpleNamespace(data=lambda: [])

    async def close(self):
        self.closed = True

    async def execute_write(self, func, *args, **kwargs):
        return await func(self, *args, **kwargs)

    async def begin_transaction(self):
        return FakeNeo4jTransactionHandle()


class FakeNeo4jTransactionHandle:
    def __init__(self):
        self.run_calls: list[tuple[str, dict]] = []
        self.committed = False
        self.rolled_back = False

    async def run(self, query: str, params: dict):
        self.run_calls.append((query, params))
        return SimpleNamespace(data=lambda: [])

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class FakeNeo4jClient:
    def __init__(self, responses: list[list[dict]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict, str]] = []
        self.verified = False
        self.closed = False

    async def execute_query(self, query: str, parameters_: dict, database_: str):
        self.calls.append((query, parameters_, database_))
        payload = self.responses.pop(0) if self.responses else []
        return SimpleNamespace(records=[FakeRecord(item) for item in payload])

    def session(self, database: str):
        return FakeNeo4jSession()

    async def verify_connectivity(self):
        self.verified = True

    async def close(self):
        self.closed = True


class FakeLLMClient:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    async def generate_response(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


class FakeNeptuneOpenCypherClient:
    def __init__(self, responses: list[list[dict]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.closed = False

    def execute_open_cypher_query(self, **payload):
        self.calls.append(payload)
        results = self.responses.pop(0) if self.responses else []
        return {"results": results}

    def close(self):
        self.closed = True


class FakeAossIndices:
    def __init__(self):
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.existing: set[str] = set()

    def exists(self, *, index: str) -> bool:
        return index in self.existing

    def create(self, *, index: str, body: dict):
        self.created.append((index, body))
        self.existing.add(index)

    def delete(self, *, index: str):
        self.deleted.append(index)
        self.existing.discard(index)


class FakeAossClient:
    def __init__(self, response: dict | None = None):
        self.indices = FakeAossIndices()
        self.response = response or {"hits": {"hits": []}}
        self.search_calls: list[tuple[str, dict]] = []
        self.index_calls: list[tuple[str, str, dict, bool]] = []
        self.delete_calls: list[tuple[str, dict, bool]] = []

    def search(self, *, index: str, body: dict):
        self.search_calls.append((index, body))
        return self.response

    def index(self, *, index: str, id: str, body: dict, refresh: bool):
        self.index_calls.append((index, id, body, refresh))

    def delete_by_query(self, *, index: str, body: dict, refresh: bool):
        self.delete_calls.append((index, body, refresh))
        return {"deleted": 1}


class FakeSnapshotProducer:
    def __init__(self, snapshot: GraphSnapshot | dict[str, GraphSnapshot]):
        self.snapshot = snapshot
        self.calls: list[tuple[str, str, str, int]] = []

    def build_snapshot(self, repo_path: str, *, repo_id: str, revision_id: str, limit: int):
        self.calls.append((repo_path, repo_id, revision_id, limit))
        if isinstance(self.snapshot, dict):
            return self.snapshot[revision_id]
        return self.snapshot


class FakeGitMetadata:
    def __init__(self, commit: CommitInfo | dict[str, CommitInfo]):
        if isinstance(commit, dict):
            self.commits = dict(commit)
            self.commit = next(iter(self.commits.values()))
        else:
            self.commits = {commit.sha: commit}
            self.commit = commit
        self.first_parent_ancestor = True
        self.first_parent_between_result: tuple[str, ...] = (self.commit.sha,)
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.worktree_calls: list[tuple[str, str]] = []
        self.hard_reset_calls: list[tuple[str, str]] = []

    def resolve_repo_root(self, repo_path: str) -> str:
        return repo_path

    def get_commit_info(self, repo_path: str, sha: str = "HEAD", *, branch_hint: str | None = None):
        _ = repo_path, sha, branch_hint
        return self.commits.get(sha, self.commit)

    def is_ancestor(self, repo_path: str, ancestor_sha: str, descendant_sha: str) -> bool:
        _ = repo_path, ancestor_sha, descendant_sha
        return True

    def is_first_parent_ancestor(self, repo_path: str, ancestor_sha: str, descendant_sha: str) -> bool:
        _ = repo_path
        self.calls.append(("is_first_parent_ancestor", (ancestor_sha, descendant_sha)))
        return self.first_parent_ancestor

    def has_new_commit(self, repo_path: str, previous_commit_sha: str | None) -> bool:
        _ = repo_path, previous_commit_sha
        return True

    def list_commits_between(self, repo_path: str, old_sha: str, new_sha: str) -> tuple[str, ...]:
        self.calls.append(("list_commits_between", (old_sha, new_sha)))
        _ = repo_path, old_sha, new_sha
        return (new_sha,)

    def list_first_parent_commits_between(
        self,
        repo_path: str,
        old_sha: str,
        new_sha: str,
    ) -> tuple[str, ...]:
        _ = repo_path
        self.calls.append(("list_first_parent_commits_between", (old_sha, new_sha)))
        return self.first_parent_between_result

    def list_commit_window(
        self,
        repo_path: str,
        end_sha: str,
        *,
        max_count: int,
        first_parent: bool = True,
    ) -> tuple[str, ...]:
        _ = repo_path, max_count, first_parent
        return (end_sha,)

    def temporary_worktree(self, repo_path: str, sha: str):
        self.worktree_calls.append((repo_path, sha))

        class _Worktree:
            def __enter__(self_inner):
                return repo_path

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _Worktree()

    def hard_reset(self, repo_path: str, target_rev: str) -> None:
        self.hard_reset_calls.append((repo_path, target_rev))


class FakeRepoResolver:
    def __init__(self, repo_ref: GitHubRepoRef):
        self.repo_ref = repo_ref

    def resolve(self, **kwargs):
        _ = kwargs
        return self.repo_ref


class FakeRepoCache:
    def __init__(self, managed_repo: ManagedRepo):
        self.managed_repo = managed_repo
        self.cleaned: list[tuple[str, str]] = []

    def ensure_managed_repo(self, *args, **kwargs):
        _ = args, kwargs
        return self.managed_repo

    def manage_local_repo(self, *args, **kwargs):
        _ = args, kwargs
        return self.managed_repo

    def cleanup_request_workspace(self, repo_id: str, request_id: str) -> None:
        self.cleaned.append((repo_id, request_id))


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection
        self._fetchone = None
        self._fetchall = []

    def execute(self, sql, params=()):
        self.connection.executed.append((sql, params))
        normalized = sql.lstrip().upper() if isinstance(sql, str) else ""
        if normalized.startswith("SELECT") and self.connection.fetchone_values:
            self._fetchone = self.connection.fetchone_values.pop(0)
        else:
            self._fetchone = (0,)
        if normalized.startswith("SELECT") and self.connection.fetchall_values:
            self._fetchall = self.connection.fetchall_values.pop(0)
        else:
            self._fetchall = []
        return self

    def executemany(self, sql, rows):
        materialized = list(rows)
        self.connection.executed.append((sql, materialized))
        return self

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class RecordingConnection:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []
        self.fetchone_values: list[tuple] = []
        self.fetchall_values: list[list[tuple]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FailingCursor:
    def __init__(self, connection, *, fail_on: str) -> None:
        self.connection = connection
        self.fail_on = fail_on

    def execute(self, sql, params=()):
        self.connection.executed.append((sql, params))
        if self.fail_on == "execute":
            raise RuntimeError("boom")
        return self

    def executemany(self, sql, rows):
        materialized = list(rows)
        self.connection.executed.append((sql, materialized))
        if self.fail_on == "executemany":
            raise RuntimeError("boom")
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FailingConnection(RecordingConnection):
    def __init__(self, *, fail_on: str) -> None:
        super().__init__()
        self.fail_on = fail_on

    def cursor(self):
        return FailingCursor(self, fail_on=self.fail_on)


class FakeEmbeddingProvider:
    def embed_texts(self, texts):
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    1.0 if "helper" in lowered else 0.0,
                    1.0 if "rollback" in lowered else 0.0,
                    1.0 if "main" in lowered else 0.0,
                ]
            )
        return vectors


class FakeQueryParser:
    def __init__(self, keywords):
        self._keywords = tuple(keywords)

    def parse(self, query: str, llm_enabled: bool = True):
        _ = query, llm_enabled
        return SimpleNamespace(high_level_keywords=self._keywords)


def make_snapshot() -> GraphSnapshot:
    node_repo = CanonicalNode("project:demo", "demo", "Project", "demo", qualified_name="demo")
    node_file = CanonicalNode(
        "file:demo:demo.app.__file__",
        "demo",
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    node_func = CanonicalNode(
        "symbol:demo:demo.app.main",
        "demo",
        "Function",
        "main",
        qualified_name="demo.app.main",
        file_path="app.py",
    )
    edge = CanonicalEdge(
        "edge:demo:DEFINES:file:demo:demo.app.__file__:symbol:demo:demo.app.main",
        "demo",
        "DEFINES",
        node_file.node_id,
        node_func.node_id,
    )
    return GraphSnapshot(
        repo_id="demo",
        revision_id="r1",
        root_path="/tmp/demo",
        generated_at="2026-03-26T00:00:00+00:00",
        nodes=(node_repo, node_file, node_func),
        edges=(edge,),
        metadata={"source": "test"},
    )


def make_tree_snapshot(
    *,
    revision_id: str = "r1",
    root_path: str = "/tmp/demo",
    file_path: str = "src/auth/login.py",
    file_name: str = "login.py",
    folder_qualified_names: tuple[str, ...] = ("demo.src", "demo.src.auth"),
    file_qualified_name: str = "demo.src.auth.login.__file__",
    function_qualified_name: str | None = "demo.src.auth.login",
) -> GraphSnapshot:
    nodes = [
        CanonicalNode(
            "project:demo",
            "demo",
            "Project",
            "demo",
            qualified_name="demo",
            properties={"root_path": root_path},
        )
    ]
    for qualified_name in folder_qualified_names:
        nodes.append(
            CanonicalNode(
                f"folder:demo:{qualified_name}",
                "demo",
                "Folder",
                qualified_name.rsplit(".", 1)[-1],
                qualified_name=qualified_name,
            )
        )
    file_node = CanonicalNode(
        f"file:demo:{file_qualified_name}",
        "demo",
        "File",
        file_name,
        qualified_name=file_qualified_name,
        file_path=file_path,
    )
    nodes.append(file_node)
    edges: list[CanonicalEdge] = []
    if function_qualified_name is not None:
        function_node = CanonicalNode(
            f"function:demo:{function_qualified_name}",
            "demo",
            "Function",
            function_qualified_name.rsplit(".", 1)[-1],
            qualified_name=function_qualified_name,
            file_path=file_path,
        )
        nodes.append(function_node)
        edges.append(
            CanonicalEdge(
                f"edge:demo:DEFINES:{file_node.node_id}:{function_node.node_id}",
                "demo",
                "DEFINES",
                file_node.node_id,
                function_node.node_id,
            )
        )

    return GraphSnapshot(
        repo_id="demo",
        revision_id=revision_id,
        root_path=root_path,
        generated_at="2026-03-26T00:00:00+00:00",
        nodes=tuple(nodes),
        edges=tuple(edges),
        metadata={"source": "test"},
    )


def make_commit(
    *,
    sha: str = "abc123",
    parents: tuple[str, ...] = ("parent1",),
    changed_files: tuple[str, ...] = ("app.py",),
) -> CommitInfo:
    return CommitInfo(
        sha=sha,
        parent_shas=parents,
        branch="main",
        author_name="Dev",
        author_email="dev@example.com",
        authored_at="2026-03-26T00:00:00+00:00",
        committed_at="2026-03-26T00:00:05+00:00",
        subject="Add helper support",
        body="body text",
        changed_files=changed_files,
    )


class BackendContractTests(unittest.TestCase):
    def test_record_kind_only_has_node_and_edge(self):
        self.assertEqual({kind.value for kind in RecordKind}, {"node", "edge"})

    def test_search_query_accepts_research_artifact_record_type(self):
        ref = GraphRecordRef(
            record_kind=RecordKind.NODE,
            record_id="artifact-1",
            record_type="research_artifact",
            repo_id="demo",
        )
        self.assertEqual(ref.record_type, "research_artifact")

    def test_lexical_search_requires_text(self):
        with self.assertRaises(ValueError):
            SearchQuery(mode=SearchMode.LEXICAL, limit=1)


class Neo4jBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_driver_instantiates_neo4j_backend(self):
        fake_client = FakeNeo4jClient()
        with patch(
            "context_graph.backend.neo4j.driver.AsyncGraphDatabase.driver",
            return_value=fake_client,
        ):
            driver = create_driver(Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw"))
        self.assertIsInstance(driver, Neo4jBackendDriver)
        await driver.close()

    async def test_driver_reports_capabilities_and_builds_fulltext_indices(self):
        fake_client = FakeNeo4jClient()
        with patch(
            "context_graph.backend.neo4j.driver.AsyncGraphDatabase.driver",
            return_value=fake_client,
        ):
            driver = Neo4jBackendDriver(
                Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw")
            )

        self.assertTrue(driver.capabilities.supports_lexical_search)
        self.assertFalse(driver.capabilities.uses_external_text_index)

        await driver.build_indices()

        issued_queries = [query for query, _, _ in fake_client.calls]
        self.assertTrue(any("CREATE FULLTEXT INDEX backend_node_search_documents" in query for query in issued_queries))
        self.assertTrue(any("CREATE FULLTEXT INDEX backend_edge_search_documents" in query for query in issued_queries))
        await driver.close()

    async def test_neo4j_lexical_search_dispatches_to_node_index(self):
        fake_client = FakeNeo4jClient(
            responses=[
                [
                    {
                        "record_id": "artifact-1",
                        "record_type": "research_artifact",
                        "repo_id": "demo",
                        "group_id": "g1",
                        "score": 3.5,
                    }
                ]
            ]
        )
        with patch(
            "context_graph.backend.neo4j.driver.AsyncGraphDatabase.driver",
            return_value=fake_client,
        ):
            driver = Neo4jBackendDriver(
                Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw")
            )

        hits = await driver.search.search(
            SearchQuery(
                mode=SearchMode.LEXICAL,
                text="payments invariant",
                record_kinds=(RecordKind.NODE,),
                record_types=("research_artifact",),
                repo_ids=("demo",),
                group_ids=("g1",),
                filters={"subsystem": "payments"},
                limit=5,
            )
        )

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].ref.record_type, "research_artifact")
        query, params, _ = fake_client.calls[0]
        self.assertIn('db.index.fulltext.queryNodes("backend_node_search_documents"', query)
        self.assertIn("doc.record_type IN $record_types", query)
        self.assertEqual(params["filter_subsystem"], "payments")
        await driver.close()

    async def test_neo4j_fetch_hydrates_by_record_id(self):
        fake_client = FakeNeo4jClient(
            responses=[
                [
                    {
                        "record_kind": "node",
                        "record_id": "artifact-1",
                        "record_type": "research_artifact",
                        "repo_id": "demo",
                        "group_id": "g1",
                        "labels": ["ResearchArtifact"],
                        "properties": {"title": "Payments invariant"},
                    }
                ]
            ]
        )
        with patch(
            "context_graph.backend.neo4j.driver.AsyncGraphDatabase.driver",
            return_value=fake_client,
        ):
            driver = Neo4jBackendDriver(
                Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw")
            )

        records = await driver.search.fetch(
            [
                GraphRecordRef(
                    record_kind=RecordKind.NODE,
                    record_id="artifact-1",
                    record_type="research_artifact",
                )
            ]
        )

        self.assertEqual(records[0]["record_id"], "artifact-1")
        query, params, _ = fake_client.calls[0]
        self.assertIn("MATCH (n:BackendRecord)", query)
        self.assertIn("n.record_id = ref.record_id", query)
        self.assertEqual(params["refs"][0]["record_id"], "artifact-1")
        await driver.close()

    async def test_neo4j_traversal_expand_returns_normalized_node_and_edge_hits(self):
        fake_client = FakeNeo4jClient(
            responses=[
                [
                    {
                        "record_kind": "node",
                        "record_id": "claim:2",
                        "record_type": "claim",
                        "repo_id": "demo",
                        "group_id": "main",
                        "distance": 1,
                        "relationship_type": None,
                        "source_record_id": None,
                        "target_record_id": None,
                    }
                ],
                [
                    {
                        "record_kind": "edge",
                        "record_id": "claim_relation:claim:2:claim:1:contradicts",
                        "record_type": "claim_relation",
                        "repo_id": "demo",
                        "group_id": "main",
                        "distance": 1,
                        "relationship_type": "CONTRADICTS",
                        "source_record_id": "claim:2",
                        "target_record_id": "claim:1",
                    }
                ],
            ]
        )
        with patch(
            "context_graph.backend.neo4j.driver.AsyncGraphDatabase.driver",
            return_value=fake_client,
        ):
            driver = Neo4jBackendDriver(
                Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw")
            )

        hits = await driver.traversal.expand(
            TraversalQuery(
                origin_refs=(
                    GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="claim:1",
                        record_type="claim",
                        repo_id="demo",
                        group_id="main",
                    ),
                ),
                max_depth=2,
                edge_types=("CLAIM_RELATION", "contradicts"),
                direction="inbound",
                repo_ids=("demo",),
                group_ids=("main",),
                limit=10,
            )
        )

        self.assertEqual([(hit.ref.record_kind.value, hit.ref.record_id) for hit in hits], [("node", "claim:2"), ("edge", "claim_relation:claim:2:claim:1:contradicts")])
        self.assertEqual(hits[1].path_metadata["source_record_id"], "claim:2")
        self.assertEqual(hits[1].path_metadata["target_record_id"], "claim:1")
        node_query, node_params, _ = fake_client.calls[0]
        self.assertIn("MATCH path=(origin)<-[rels*1..2]-(neighbor:BackendRecord)", node_query)
        self.assertEqual(node_params["edge_types"], ["claim_relation", "contradicts"])
        self.assertEqual(node_params["repo_ids"], ["demo"])
        self.assertEqual(node_params["group_ids"], ["main"])
        self.assertFalse(node_params["edge_type_filter_disabled"])
        await driver.close()

    async def test_vector_mode_is_explicitly_unimplemented(self):
        with self.assertRaises(BackendFeatureNotImplementedError):
            await Neo4jSearchOperations(driver=SimpleNamespace(execute_query=None)).search(
                SearchQuery(
                    mode=SearchMode.VECTOR,
                    embedding=(0.1, 0.2),
                    limit=1,
                )
            )

    async def test_neo4j_mutation_apply_writes_nodes_edges_and_search_documents(self):
        fake_client = FakeNeo4jClient()
        with patch(
            "context_graph.backend.neo4j.driver.AsyncGraphDatabase.driver",
            return_value=fake_client,
        ):
            driver = Neo4jBackendDriver(
                Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw")
            )

        plan = MutationPlan(
            upsert_nodes=(
                NodeRecord(
                    record_id="file:demo:app.py",
                    record_type="code_node",
                    repo_id="demo",
                    group_id="demo",
                    labels=("BackendRecord", "CodeNode", "File"),
                    properties={"node_id": "file:demo:app.py", "name": "app.py"},
                ),
            ),
            upsert_edges=(
                EdgeRecord(
                    record_id="defines:1",
                    record_type="code_edge_defines",
                    repo_id="demo",
                    group_id="demo",
                    relationship_type="DEFINES",
                    source_record_id="file:demo:app.py",
                    target_record_id="symbol:demo:demo.app.main",
                    properties={"edge_type": "DEFINES"},
                ),
            ),
            upsert_search_documents=(
                SearchDocumentRecord(
                    record_kind=RecordKind.NODE,
                    record_id="file:demo:app.py",
                    record_type="code_node",
                    repo_id="demo",
                    group_id="demo",
                    search_text="File app.py",
                    properties={"kind": "File"},
                ),
            ),
        )

        result = await driver.mutations.apply(plan)

        self.assertEqual(result.operations_applied, 3)
        queries = [query for query, _, _ in fake_client.calls]
        self.assertTrue(any("MERGE (n:BackendRecord:CodeNode:File" in query for query in queries))
        self.assertTrue(
            any(
                "MATCH (source:BackendRecord {record_id: row.source_record_id})" in query
                and "MATCH (target:BackendRecord {record_id: row.target_record_id})" in query
                and "MERGE (source)-[rel:DEFINES" in query
                for query in queries
            )
        )
        self.assertTrue(any("MERGE (doc:NodeSearchDocument" in query for query in queries))
        await driver.close()

    async def test_neo4j_reset_repo_deletes_graph_records_and_search_documents(self):
        fake_client = FakeNeo4jClient(
            responses=[
                [{"count": 2}],
                [{"count": 3}],
                [{"count": 1}],
                [],
                [],
                [],
            ]
        )
        with patch(
            "context_graph.backend.neo4j.driver.AsyncGraphDatabase.driver",
            return_value=fake_client,
        ):
            driver = Neo4jBackendDriver(
                Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw")
            )

        result = await driver.maintenance.reset_repo("demo")

        self.assertEqual(result.nodes_deleted, 2)
        self.assertEqual(result.edges_deleted, 3)
        self.assertEqual(result.search_documents_deleted, 1)
        queries = [query for query, _, _ in fake_client.calls]
        self.assertTrue(any("DETACH DELETE doc" in query for query in queries))
        self.assertTrue(any("DELETE rel" in query for query in queries))
        self.assertTrue(any("DETACH DELETE n" in query for query in queries))
        await driver.close()


class NeptuneBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_capabilities_depend_on_aoss_configuration(self):
        fake_graph = SimpleNamespace(client=FakeNeptuneOpenCypherClient())
        with (
            patch.object(NeptuneBackendDriver, "_create_neptune_client", return_value=fake_graph),
            patch.object(NeptuneBackendDriver, "_create_aoss_client", return_value=None),
        ):
            driver = NeptuneBackendDriver(NeptuneConfig(host="neptune-db://example"))
        self.assertFalse(driver.capabilities.supports_lexical_search)
        await driver.close()

    async def test_build_indices_creates_aoss_indices_when_enabled(self):
        fake_graph = SimpleNamespace(client=FakeNeptuneOpenCypherClient())
        fake_aoss = FakeAossClient()
        with (
            patch.object(NeptuneBackendDriver, "_create_neptune_client", return_value=fake_graph),
            patch.object(NeptuneBackendDriver, "_create_aoss_client", return_value=fake_aoss),
        ):
            driver = NeptuneBackendDriver(
                NeptuneConfig(host="neptune-db://example", aoss_host="aoss.example")
            )

        await driver.build_indices()

        self.assertEqual({name for name, _ in fake_aoss.indices.created}, {"backend_node_documents", "backend_edge_documents"})
        await driver.close()

    async def test_neptune_lexical_search_uses_aoss_and_fetch_uses_graph_hydration(self):
        fake_graph_client = FakeNeptuneOpenCypherClient(
            responses=[
                [
                    {
                        "record_kind": "node",
                        "record_id": "artifact-1",
                        "record_type": "research_artifact",
                        "repo_id": "demo",
                        "group_id": "g1",
                        "labels": ["ResearchArtifact"],
                        "properties": {"title": "Payments invariant"},
                    }
                ]
            ]
        )
        fake_graph = SimpleNamespace(client=fake_graph_client)
        fake_aoss = FakeAossClient(
            response={
                "hits": {
                    "hits": [
                        {
                            "_index": "backend_node_documents",
                            "_score": 2.4,
                            "_source": {
                                "record_id": "artifact-1",
                                "record_type": "research_artifact",
                                "repo_id": "demo",
                                "group_id": "g1",
                            },
                        }
                    ]
                }
            }
        )

        with (
            patch.object(NeptuneBackendDriver, "_create_neptune_client", return_value=fake_graph),
            patch.object(NeptuneBackendDriver, "_create_aoss_client", return_value=fake_aoss),
        ):
            driver = NeptuneBackendDriver(
                NeptuneConfig(host="neptune-db://example", aoss_host="aoss.example")
            )

        hits = await driver.search.search(
            SearchQuery(
                mode=SearchMode.LEXICAL,
                text="payments invariant",
                record_kinds=(RecordKind.NODE,),
                record_types=("research_artifact",),
                repo_ids=("demo",),
                group_ids=("g1",),
                limit=3,
            )
        )

        self.assertEqual(len(hits), 1)
        self.assertEqual(fake_aoss.search_calls[0][0], NEPTUNE_NODE_AOSS_INDEX)
        self.assertEqual(
            fake_aoss.search_calls[0][1]["query"]["bool"]["filter"][0],
            {"terms": {"record_type": ["research_artifact"]}},
        )

        hydrated = await driver.search.fetch([hits[0].ref])
        self.assertEqual(hydrated[0]["record_id"], "artifact-1")
        payload = fake_graph_client.calls[0]
        self.assertIn("MATCH (n)", payload["openCypherQuery"])
        params = json.loads(payload["parameters"])
        self.assertEqual(params["refs"][0]["record_id"], "artifact-1")
        await driver.close()

    async def test_neptune_traversal_expand_returns_normalized_node_and_edge_hits(self):
        fake_graph_client = FakeNeptuneOpenCypherClient(
            responses=[
                [
                    {
                        "record_kind": "node",
                        "record_id": "claim:2",
                        "record_type": "claim",
                        "repo_id": "demo",
                        "group_id": "main",
                        "distance": 1,
                        "relationship_type": None,
                        "source_record_id": None,
                        "target_record_id": None,
                    }
                ],
                [
                    {
                        "record_kind": "edge",
                        "record_id": "claim_relation:claim:2:claim:1:contradicts",
                        "record_type": "claim_relation",
                        "repo_id": "demo",
                        "group_id": "main",
                        "distance": 1,
                        "relationship_type": "CONTRADICTS",
                        "source_record_id": "claim:2",
                        "target_record_id": "claim:1",
                    }
                ],
            ]
        )
        fake_graph = SimpleNamespace(client=fake_graph_client)
        with (
            patch.object(NeptuneBackendDriver, "_create_neptune_client", return_value=fake_graph),
            patch.object(NeptuneBackendDriver, "_create_aoss_client", return_value=None),
        ):
            driver = NeptuneBackendDriver(NeptuneConfig(host="neptune-db://example"))

        hits = await driver.traversal.expand(
            TraversalQuery(
                origin_refs=(
                    GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="claim:1",
                        record_type="claim",
                        repo_id="demo",
                        group_id="main",
                    ),
                ),
                max_depth=2,
                edge_types=("CLAIM_RELATION", "contradicts"),
                direction="outbound",
                repo_ids=("demo",),
                group_ids=("main",),
                limit=10,
            )
        )

        self.assertEqual([(hit.ref.record_kind.value, hit.ref.record_id) for hit in hits], [("node", "claim:2"), ("edge", "claim_relation:claim:2:claim:1:contradicts")])
        self.assertEqual(hits[1].path_metadata["relationship_type"], "CONTRADICTS")
        payload = fake_graph_client.calls[0]
        self.assertIn("MATCH path=(origin)-[rels*1..2]->(neighbor:BackendRecord)", payload["openCypherQuery"])
        params = json.loads(payload["parameters"])
        self.assertEqual(params["edge_types"], ["claim_relation", "contradicts"])
        self.assertEqual(params["repo_ids"], ["demo"])
        self.assertEqual(params["group_ids"], ["main"])
        self.assertFalse(params["group_filter_disabled"])
        await driver.close()

    async def test_neptune_vector_mode_is_explicitly_unimplemented(self):
        fake_driver = SimpleNamespace(aoss_client=object())
        with self.assertRaises(BackendFeatureNotImplementedError):
            await NeptuneSearchOperations(driver=fake_driver).search(
                SearchQuery(
                    mode=SearchMode.VECTOR,
                    embedding=(0.1, 0.2),
                    limit=1,
                )
            )

    async def test_neptune_mutation_apply_syncs_aoss_search_documents(self):
        fake_graph_client = FakeNeptuneOpenCypherClient()
        fake_graph = SimpleNamespace(client=fake_graph_client)
        fake_aoss = FakeAossClient()
        with (
            patch.object(NeptuneBackendDriver, "_create_neptune_client", return_value=fake_graph),
            patch.object(NeptuneBackendDriver, "_create_aoss_client", return_value=fake_aoss),
        ):
            driver = NeptuneBackendDriver(
                NeptuneConfig(host="neptune-db://example", aoss_host="aoss.example")
            )

        plan = MutationPlan(
            upsert_nodes=(
                NodeRecord(
                    record_id="file:demo:app.py",
                    record_type="code_node",
                    repo_id="demo",
                    group_id="demo",
                    labels=("BackendRecord", "CodeNode", "File"),
                    properties={"node_id": "file:demo:app.py"},
                ),
            ),
            upsert_edges=(
                EdgeRecord(
                    record_id="defines:1",
                    record_type="code_edge_defines",
                    repo_id="demo",
                    group_id="demo",
                    relationship_type="DEFINES",
                    source_record_id="file:demo:app.py",
                    target_record_id="symbol:demo:demo.app.main",
                    properties={},
                ),
            ),
            upsert_search_documents=(
                SearchDocumentRecord(
                    record_kind=RecordKind.NODE,
                    record_id="file:demo:app.py",
                    record_type="code_node",
                    repo_id="demo",
                    group_id="demo",
                    search_text="File app.py",
                    properties={"kind": "File"},
                ),
            ),
        )

        result = await driver.mutations.apply(plan)

        self.assertEqual(result.operations_applied, 3)
        self.assertEqual(len(fake_graph_client.calls), 2)
        self.assertIn(
            "MATCH (source:BackendRecord {record_id: row.source_record_id})",
            fake_graph_client.calls[1]["openCypherQuery"],
        )
        self.assertEqual(len(fake_aoss.index_calls), 1)
        self.assertEqual(fake_aoss.index_calls[0][0], "backend_node_documents")
        await driver.close()

    async def test_neptune_reset_scope_deletes_graph_records_and_aoss_documents(self):
        fake_graph_client = FakeNeptuneOpenCypherClient(
            responses=[
                [{"count": 4}],
                [{"count": 2}],
                [],
                [],
            ]
        )
        fake_graph = SimpleNamespace(client=fake_graph_client)
        fake_aoss = FakeAossClient()
        with (
            patch.object(NeptuneBackendDriver, "_create_neptune_client", return_value=fake_graph),
            patch.object(NeptuneBackendDriver, "_create_aoss_client", return_value=fake_aoss),
        ):
            driver = NeptuneBackendDriver(
                NeptuneConfig(host="neptune-db://example", aoss_host="aoss.example")
            )

        result = await driver.maintenance.reset_scope(
            SimpleNamespace(
                repo_id="demo",
                node_record_types=("github_pull_request",),
                edge_record_types=("has_pull_request",),
                delete_search_documents=True,
                search_document_record_types=("github_pull_request",),
            )
        )

        self.assertEqual(result.nodes_deleted, 4)
        self.assertEqual(result.edges_deleted, 2)
        self.assertEqual(result.search_documents_deleted, 2)
        self.assertEqual(len(fake_aoss.delete_calls), 2)
        await driver.close()


class GitHubStructuralPlannerTests(unittest.TestCase):
    def test_planner_emits_nodes_edges_and_search_documents_for_bundle(self):
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
                    title="Tighten rollback handling",
                    state="open",
                    author_login="octocat",
                    created_at="2026-03-26T00:00:00+00:00",
                    updated_at="2026-03-26T00:01:00+00:00",
                    metadata={"base_ref": "main"},
                ),
                GitHubEntity(
                    node_id="github:issue:demo:9",
                    kind="Issue",
                    repo_id="demo",
                    github_repo="octo/demo",
                    external_id="900",
                    number=9,
                    url="https://github.com/octo/demo/issues/9",
                    title="Rollback bug",
                    state="open",
                    author_login="octocat",
                    created_at="2026-03-26T00:00:00+00:00",
                    updated_at="2026-03-26T00:01:00+00:00",
                    metadata={"closed_at": None},
                ),
            ),
            project_links=(
                GitHubProjectLink(
                    target_node_id="github:pr:demo:7",
                    relation_type="HAS_PULL_REQUEST",
                    metadata={},
                ),
                GitHubProjectLink(
                    target_node_id="github:issue:demo:9",
                    relation_type="HAS_ISSUE",
                    metadata={},
                ),
            ),
            commit_links=(
                GitHubCommitLink(
                    commit_sha="abc123",
                    target_node_id="github:pr:demo:7",
                    relation_type="ASSOCIATED_WITH_PR",
                    metadata={},
                ),
            ),
            entity_links=(
                GitHubEntityLink(
                    source_node_id="github:pr:demo:7",
                    target_node_id="github:issue:demo:9",
                    relation_type="REFERENCES_ISSUE",
                    metadata={},
                ),
            ),
        )

        plan = GitHubStructuralPlanner().plan_bundle(bundle)

        self.assertEqual(len(plan.upsert_nodes), 2)
        self.assertEqual(len(plan.upsert_edges), 4)
        self.assertEqual(len(plan.upsert_search_documents), 2)
        self.assertEqual({node.record_type for node in plan.upsert_nodes}, {"github_pull_request", "github_issue"})
        self.assertEqual(
            {edge.relationship_type for edge in plan.upsert_edges},
            {"HAS_PULL_REQUEST", "HAS_ISSUE", "ASSOCIATED_WITH_PR", "REFERENCES_ISSUE"},
        )
        self.assertTrue(
            any("Tighten rollback handling" in doc.search_text for doc in plan.upsert_search_documents)
        )


class DeterministicPlannerTests(unittest.TestCase):
    def test_snapshot_diff_planning_builds_structural_operations_and_temporal_state(self):
        snapshot = make_snapshot()
        planner = DeterministicMutationPlanner()

        planned = planner.plan_snapshot_diff(
            previous_snapshot=None,
            snapshot=snapshot,
            diff=diff_snapshots(None, snapshot),
        )

        self.assertEqual(planned.plan.operation_count(), 11)
        self.assertEqual(len(planned.plan.upsert_nodes), 5)
        self.assertEqual(len(planned.plan.upsert_edges), 3)
        self.assertEqual(len(planned.plan.upsert_search_documents), 3)
        self.assertIn("symbol:demo:demo.app.main", planned.state.active_node_versions)
        self.assertIn("edge:demo:DEFINES:file:demo:demo.app.__file__:symbol:demo:demo.app.main", planned.state.active_edge_versions)
        self.assertIn("deterministic_mutation_state", planned.snapshot.metadata)

    def test_commit_provenance_planning_matches_deterministic_scope(self):
        snapshot = make_snapshot()
        planner = DeterministicMutationPlanner()
        structural = planner.plan_snapshot_diff(
            previous_snapshot=None,
            snapshot=snapshot,
            diff=diff_snapshots(None, snapshot),
        )
        commit_plan = planner.plan_commit_provenance(
            snapshot=structural.snapshot,
            diff=structural.diff,
            commit=make_commit(),
            planning_state=structural.state,
        )

        self.assertEqual(len(commit_plan.upsert_nodes), 2)
        self.assertEqual(len(commit_plan.upsert_search_documents), 1)
        self.assertEqual(len(commit_plan.upsert_edges), 6)
        relationship_types = {edge.relationship_type for edge in commit_plan.upsert_edges}
        self.assertEqual(
            relationship_types,
            {
                "HAS_COMMIT",
                "PRECEDES",
                "TOUCHED",
                "INTRODUCED_VERSION",
            },
        )
        touched_targets = {
            edge.target_record_id
            for edge in commit_plan.upsert_edges
            if edge.relationship_type == "TOUCHED"
        }
        introduced_targets = {
            edge.target_record_id
            for edge in commit_plan.upsert_edges
            if edge.relationship_type == "INTRODUCED_VERSION"
        }
        self.assertTrue(all("@" not in record_id for record_id in touched_targets))
        self.assertTrue(all("@" in record_id for record_id in introduced_targets))

    def test_commit_provenance_adds_file_and_folder_touches_without_structural_diff(self):
        previous_snapshot = make_tree_snapshot(revision_id="r0")
        snapshot = make_tree_snapshot(revision_id="r1")
        planner = DeterministicMutationPlanner()
        diff = diff_snapshots(previous_snapshot, snapshot)
        structural = planner.plan_snapshot_diff(
            previous_snapshot=previous_snapshot,
            snapshot=snapshot,
            diff=diff,
        )

        commit_plan = planner.plan_commit_provenance(
            snapshot=structural.snapshot,
            diff=structural.diff,
            commit=make_commit(
                sha="r1",
                parents=(),
                changed_files=("src/auth/login.py",),
            ),
            planning_state=structural.state,
        )

        touched_targets = {
            edge.target_record_id
            for edge in commit_plan.upsert_edges
            if edge.relationship_type == "TOUCHED"
        }
        self.assertEqual(
            touched_targets,
            {
                "file:demo:demo.src.auth.login.__file__",
                "folder:demo:demo.src",
                "folder:demo:demo.src.auth",
            },
        )
        self.assertNotIn("project:demo", touched_targets)

    def test_commit_provenance_deduplicates_file_touches_when_changed_files_overlap_structural_diff(self):
        snapshot = make_tree_snapshot()
        planner = DeterministicMutationPlanner()
        structural = planner.plan_snapshot_diff(
            previous_snapshot=None,
            snapshot=snapshot,
            diff=diff_snapshots(None, snapshot),
        )

        commit_plan = planner.plan_commit_provenance(
            snapshot=structural.snapshot,
            diff=structural.diff,
            commit=make_commit(
                sha="r1",
                parents=(),
                changed_files=("src/auth/login.py",),
            ),
            planning_state=structural.state,
        )

        touched_targets = [
            edge.target_record_id
            for edge in commit_plan.upsert_edges
            if edge.relationship_type == "TOUCHED"
        ]
        self.assertEqual(
            touched_targets.count("file:demo:demo.src.auth.login.__file__"),
            1,
        )

    def test_normalize_snapshot_for_commit_stabilizes_project_root_path_across_replays(self):
        previous_raw = make_tree_snapshot(revision_id="r0", root_path="/tmp/worktree-a")
        current_raw = make_tree_snapshot(revision_id="r1", root_path="/tmp/worktree-b")

        previous_snapshot = normalize_snapshot_for_commit(
            previous_raw,
            repo_path="/tmp/demo",
            commit=make_commit(sha="r0", parents=()),
        )
        current_snapshot = normalize_snapshot_for_commit(
            current_raw,
            repo_path="/tmp/demo",
            commit=make_commit(sha="r1", parents=("r0",), changed_files=("src/auth/login.py",)),
        )

        diff = diff_snapshots(previous_snapshot, current_snapshot)

        self.assertNotIn("project:demo", diff.changed_nodes)

    def test_markdown_only_commit_still_touches_file_and_folder_nodes(self):
        previous_snapshot = make_tree_snapshot(
            revision_id="r0",
            file_path="test_events/GROUND_TRUTH.md",
            file_name="GROUND_TRUTH.md",
            folder_qualified_names=("demo.test_events",),
            file_qualified_name="demo.test_events.GROUND_TRUTH.__file__",
            function_qualified_name=None,
        )
        snapshot = make_tree_snapshot(
            revision_id="r1",
            file_path="test_events/GROUND_TRUTH.md",
            file_name="GROUND_TRUTH.md",
            folder_qualified_names=("demo.test_events",),
            file_qualified_name="demo.test_events.GROUND_TRUTH.__file__",
            function_qualified_name=None,
        )
        planner = DeterministicMutationPlanner()
        diff = diff_snapshots(previous_snapshot, snapshot)
        structural = planner.plan_snapshot_diff(
            previous_snapshot=previous_snapshot,
            snapshot=snapshot,
            diff=diff,
        )

        commit_plan = planner.plan_commit_provenance(
            snapshot=structural.snapshot,
            diff=structural.diff,
            commit=make_commit(
                sha="r1",
                parents=(),
                changed_files=("test_events/GROUND_TRUTH.md",),
            ),
            planning_state=structural.state,
        )

        touched_targets = {
            edge.target_record_id
            for edge in commit_plan.upsert_edges
            if edge.relationship_type == "TOUCHED"
        }
        self.assertEqual(
            touched_targets,
            {
                "file:demo:demo.test_events.GROUND_TRUTH.__file__",
                "folder:demo:demo.test_events",
            },
        )


class DeterministicIngestionTests(unittest.TestCase):
    def test_apply_commit_uses_mutation_planner_without_graph_sinks(self):
        snapshot = make_snapshot()
        commit = make_commit(sha="r1", parents=(), changed_files=("app.py",))
        producer = FakeSnapshotProducer(snapshot)
        metadata = InMemoryMetadataStore()
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=FakeGitMetadata(commit),
        )

        result = service.apply_commit(
            "/tmp/demo",
            commit_sha="r1",
            repo_id="demo",
            execute=False,
        )

        self.assertFalse(result.skipped)
        self.assertEqual(result.operations.operation_count(), 22)
        self.assertEqual(result.observation_operations.operation_count(), 4)
        self.assertIn(
            "MENTIONS",
            {edge.relationship_type for edge in result.observation_operations.upsert_edges},
        )
        self.assertEqual(result.evidence_operations.operation_count(), 0)
        self.assertEqual(result.claim_candidates_extracted, 0)
        self.assertEqual(result.claim_candidates_validated, 0)
        self.assertEqual(result.claim_candidates_rejected, 0)
        self.assertEqual(result.apply_result.operations_applied, 0)
        self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, "r1")

    def test_apply_commit_extracts_evidence_when_llm_is_configured(self):
        snapshot = make_snapshot()
        commit = make_commit(sha="r1", parents=(), changed_files=("app.py",))
        producer = FakeSnapshotProducer(snapshot)
        metadata = InMemoryMetadataStore()
        llm_client = FakeLLMClient(
            {
                "claims": [
                    {
                        "claim_text": "Helper support was added to demo.app.main.",
                        "claim_subkind": "fix_or_mitigation",
                        "about_hints": ["demo.app.main"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "Add helper support",
                                "summary_text": "The commit adds helper support to demo.app.main.",
                                "span_start": 0,
                                "span_end": 18,
                                "about_hints": ["demo.app.main"],
                                "support_kind": "direct_statement",
                                "metadata": {},
                            }
                        ],
                    }
                ]
            }
        )
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=FakeGitMetadata(commit),
            llm_client=llm_client,
        )

        result = service.apply_commit(
            "/tmp/demo",
            commit_sha="r1",
            repo_id="demo",
            execute=False,
        )

        self.assertFalse(result.skipped)
        self.assertEqual(result.observation_operations.operation_count(), 4)
        self.assertIn(
            "MENTIONS",
            {edge.relationship_type for edge in result.observation_operations.upsert_edges},
        )
        self.assertEqual(result.evidence_operations.operation_count(), 7)
        self.assertIn(
            "SUPPORTS_CLAIM",
            {edge.relationship_type for edge in result.evidence_operations.upsert_edges},
        )
        self.assertEqual(
            {node.record_type for node in result.evidence_operations.upsert_nodes},
            {"claim", "evidence_record"},
        )
        self.assertEqual(result.claim_candidates_extracted, 1)
        self.assertEqual(result.claim_candidates_validated, 1)
        self.assertEqual(result.claim_candidates_rejected, 0)
        self.assertEqual(result.operations.operation_count(), 29)
        self.assertEqual(
            llm_client.calls[0]["prompt_name"],
            "provenance.extract_claims_and_evidence.from_commit_message.v1",
        )

    def test_apply_commit_does_not_invoke_dedup_scaffolding_services(self):
        snapshot = make_snapshot()
        commit = make_commit(sha="r1", parents=(), changed_files=("app.py",))
        producer = FakeSnapshotProducer(snapshot)
        metadata = InMemoryMetadataStore()
        llm_client = FakeLLMClient(
            {
                "claims": [
                    {
                        "claim_text": "Helper support was added to demo.app.main.",
                        "claim_subkind": "fix_or_mitigation",
                        "about_hints": ["demo.app.main"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "Add helper support",
                                "summary_text": "The commit adds helper support to demo.app.main.",
                                "span_start": 0,
                                "span_end": 18,
                                "about_hints": ["demo.app.main"],
                                "support_kind": "direct_statement",
                                "metadata": {},
                            }
                        ],
                    }
                ]
            }
        )
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=FakeGitMetadata(commit),
            llm_client=llm_client,
        )

        with patch(
            "context_graph.provenance.services.EvidenceDedupService.pregroup",
            side_effect=AssertionError("evidence dedup scaffolding should stay inactive during indexing"),
        ), patch(
            "context_graph.provenance.services.ClaimCanonicalizationService.pregroup",
            side_effect=AssertionError("claim canonicalization scaffolding should stay inactive during indexing"),
        ):
            result = service.apply_commit(
                "/tmp/demo",
                commit_sha="r1",
                repo_id="demo",
                execute=False,
            )

        self.assertFalse(result.skipped)
        self.assertEqual(result.claim_candidates_validated, 1)

    def test_apply_commit_with_history_seeded_replays_first_parent_commits(self):
        snapshot = make_snapshot()
        commit = make_commit(sha="head123", parents=("prev123",), changed_files=("app.py",))
        producer = FakeSnapshotProducer(snapshot)
        metadata = InMemoryMetadataStore()
        metadata.save_repo_index_state(
            RepoIndexState(
                repo_id="demo",
                root_path="/tmp/demo",
                latest_revision_id="prev123",
                latest_indexed_at="2026-03-26T00:00:00+00:00",
                metadata={},
                latest_commit_sha="prev123",
                latest_branch="main",
                last_commit_indexed_at="2026-03-26T00:00:00+00:00",
            )
        )
        git_metadata = FakeGitMetadata(commit)
        git_metadata.first_parent_between_result = ("mid456", "head123")
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=git_metadata,
        )
        captured: dict[str, object] = {}
        expected_result = SimpleNamespace(repo_id="demo")
        service.apply_commit_sequence = lambda repo_path, **kwargs: (
            captured.update({"repo_path": repo_path, **kwargs}),
            expected_result,
        )[1]  # type: ignore[method-assign]

        result = service.apply_commit_with_history(
            "/tmp/demo",
            commit_sha="head123",
            repo_id="demo",
            execute=False,
            history_window=5,
        )

        self.assertIs(result, expected_result)
        self.assertEqual(captured["commit_shas"], ("mid456", "head123"))
        self.assertIn(
            ("list_first_parent_commits_between", ("prev123", "head123")),
            git_metadata.calls,
        )
        self.assertNotIn(
            ("list_commits_between", ("prev123", "head123")),
            git_metadata.calls,
        )

    def test_apply_latest_commit_seeded_replays_first_parent_commits(self):
        snapshot = make_snapshot()
        commit = make_commit(sha="head123", parents=("prev123",), changed_files=("app.py",))
        producer = FakeSnapshotProducer(snapshot)
        metadata = InMemoryMetadataStore()
        metadata.save_repo_index_state(
            RepoIndexState(
                repo_id="demo",
                root_path="/tmp/demo",
                latest_revision_id="prev123",
                latest_indexed_at="2026-03-26T00:00:00+00:00",
                metadata={},
                latest_commit_sha="prev123",
                latest_branch="main",
                last_commit_indexed_at="2026-03-26T00:00:00+00:00",
            )
        )
        git_metadata = FakeGitMetadata(commit)
        git_metadata.first_parent_between_result = ("mid456", "head123")
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=git_metadata,
        )
        captured: dict[str, object] = {}
        expected_result = SimpleNamespace(repo_id="demo")
        service.apply_commit_sequence = lambda repo_path, **kwargs: (
            captured.update({"repo_path": repo_path, **kwargs}),
            expected_result,
        )[1]  # type: ignore[method-assign]

        result = service.apply_latest_commit(
            "/tmp/demo",
            repo_id="demo",
            execute=False,
        )

        self.assertIs(result, expected_result)
        self.assertEqual(captured["commit_shas"], ("mid456", "head123"))
        self.assertIn(
            ("list_first_parent_commits_between", ("prev123", "head123")),
            git_metadata.calls,
        )
        self.assertNotIn(
            ("list_commits_between", ("prev123", "head123")),
            git_metadata.calls,
        )

    def test_apply_commit_sequence_reuses_one_worktree_and_resets_between_commits(self):
        commits = {
            "mid456": make_commit(sha="mid456", parents=(), changed_files=("app.py",)),
            "head123": make_commit(sha="head123", parents=("mid456",), changed_files=("app.py",)),
        }
        producer = FakeSnapshotProducer(
            {
                "mid456": make_tree_snapshot(revision_id="mid456"),
                "head123": make_tree_snapshot(revision_id="head123"),
            }
        )
        metadata = InMemoryMetadataStore()
        git_metadata = FakeGitMetadata(commits)
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=git_metadata,
        )

        result = service.apply_commit_sequence(
            "/tmp/demo",
            commit_shas=("mid456", "head123"),
            repo_id="demo",
            execute=False,
        )

        self.assertEqual(git_metadata.worktree_calls, [("/tmp/demo", "mid456")])
        self.assertEqual(git_metadata.hard_reset_calls, [("/tmp/demo", "head123")])
        self.assertEqual(result.applied_commit_shas, ("mid456", "head123"))
        self.assertEqual(result.commit.sha, "head123")
        self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, "head123")

    def test_apply_commit_sequence_finalizes_metadata_after_each_commit(self):
        commits = {
            "mid456": make_commit(sha="mid456", parents=(), changed_files=("app.py",)),
            "head123": make_commit(sha="head123", parents=("mid456",), changed_files=("app.py",)),
        }
        metadata = InMemoryMetadataStore()

        class InspectingSnapshotProducer:
            def __init__(self) -> None:
                self.observed_before_second: tuple[str | None, str | None] | None = None

            def build_snapshot(self, repo_path: str, *, repo_id: str, revision_id: str, limit: int):
                _ = repo_path, limit
                if revision_id == "head123":
                    snapshot = metadata.load_latest_snapshot(repo_id)
                    state = metadata.load_repo_index_state(repo_id)
                    self.observed_before_second = (
                        snapshot.revision_id if snapshot is not None else None,
                        state.latest_commit_sha if state is not None else None,
                    )
                return make_tree_snapshot(revision_id=revision_id)

        producer = InspectingSnapshotProducer()
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=FakeGitMetadata(commits),
        )

        result = service.apply_commit_sequence(
            "/tmp/demo",
            commit_shas=("mid456", "head123"),
            repo_id="demo",
            execute=False,
        )

        self.assertEqual(producer.observed_before_second, ("mid456", "mid456"))
        self.assertEqual(result.applied_commit_shas, ("mid456", "head123"))
        self.assertEqual(metadata.load_latest_snapshot("demo").revision_id, "head123")
        self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, "head123")

    def test_apply_commit_sequence_stops_after_failure_and_keeps_processed_prefix_finalized(self):
        commits = {
            "mid456": make_commit(sha="mid456", parents=(), changed_files=("app.py",)),
            "head123": make_commit(sha="head123", parents=("mid456",), changed_files=("app.py",)),
            "tail789": make_commit(sha="tail789", parents=("head123",), changed_files=("app.py",)),
        }
        metadata = InMemoryMetadataStore()

        class FailingSnapshotProducer:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def build_snapshot(self, repo_path: str, *, repo_id: str, revision_id: str, limit: int):
                _ = repo_path, repo_id, limit
                self.calls.append(revision_id)
                if revision_id == "head123":
                    raise RuntimeError("snapshot build failed for head123")
                return make_tree_snapshot(revision_id=revision_id)

        producer = FailingSnapshotProducer()
        git_metadata = FakeGitMetadata(commits)
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=git_metadata,
        )

        with self.assertRaisesRegex(RuntimeError, "snapshot build failed for head123"):
            service.apply_commit_sequence(
                "/tmp/demo",
                commit_shas=("mid456", "head123", "tail789"),
                repo_id="demo",
                execute=False,
            )

        self.assertEqual(producer.calls, ["mid456", "head123"])
        self.assertEqual(metadata.load_latest_snapshot("demo").revision_id, "mid456")
        self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, "mid456")
        self.assertEqual(git_metadata.worktree_calls, [("/tmp/demo", "mid456")])
        self.assertEqual(git_metadata.hard_reset_calls, [("/tmp/demo", "head123")])

    def test_apply_commit_rejects_target_off_first_parent_chain(self):
        snapshot = make_snapshot()
        commit = make_commit(sha="head123", parents=("prev123",), changed_files=("app.py",))
        producer = FakeSnapshotProducer(snapshot)
        metadata = InMemoryMetadataStore()
        metadata.save_repo_index_state(
            RepoIndexState(
                repo_id="demo",
                root_path="/tmp/demo",
                latest_revision_id="prev123",
                latest_indexed_at="2026-03-26T00:00:00+00:00",
                metadata={},
                latest_commit_sha="prev123",
                latest_branch="main",
                last_commit_indexed_at="2026-03-26T00:00:00+00:00",
            )
        )
        git_metadata = FakeGitMetadata(commit)
        git_metadata.first_parent_ancestor = False
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            snapshot_producer=producer,
            git_metadata=git_metadata,
        )

        with self.assertRaisesRegex(RuntimeError, "first-parent chain; reset or reseed this repo id"):
            service.apply_commit(
                "/tmp/demo",
                commit_sha="head123",
                repo_id="demo",
                execute=False,
            )

    def test_hosted_explicit_commit_indexes_target_plus_previous_four_when_unseeded(self):
        metadata = InMemoryMetadataStore()
        service = HostedIndexingService(
            metadata_store=metadata,
            backend_driver=None,
        )
        expected_result = SimpleNamespace(repo_id="demo")
        commit_service = SimpleNamespace(
            apply_commit_with_history=lambda repo_path, **kwargs: (
                self.assertEqual(kwargs["commit_sha"], "target"),
                self.assertEqual(kwargs["history_window"], 5),
                expected_result,
            )[2],
        )

        result = service._index_commits(
            request=SimpleNamespace(commit_sha="target", ref=None, limit=100, execute=False),
            repo_path="/tmp/demo",
            repo_id="demo",
            branch_hint="main",
            commit_service=commit_service,
        )

        self.assertIs(result, expected_result)

    def test_hosted_explicit_commit_only_replays_missing_first_parent_commits_when_seeded(self):
        metadata = InMemoryMetadataStore()
        metadata.save_repo_index_state(
            RepoIndexState(
                repo_id="demo",
                root_path="/tmp/demo",
                latest_revision_id="seed",
                latest_indexed_at="2026-03-26T00:00:00+00:00",
                metadata={},
                latest_commit_sha="c3",
                latest_branch="main",
                last_commit_indexed_at="2026-03-26T00:00:00+00:00",
            )
        )
        service = HostedIndexingService(
            metadata_store=metadata,
            backend_driver=None,
        )
        expected_result = SimpleNamespace(repo_id="demo")
        commit_service = SimpleNamespace(
            apply_commit_with_history=lambda repo_path, **kwargs: (
                self.assertEqual(kwargs["commit_sha"], "target"),
                self.assertEqual(kwargs["history_window"], 5),
                expected_result,
            )[2],
        )

        result = service._index_commits(
            request=SimpleNamespace(commit_sha="target", ref=None, limit=100, execute=False),
            repo_path="/tmp/demo",
            repo_id="demo",
            branch_hint="main",
            commit_service=commit_service,
        )

        self.assertIs(result, expected_result)

    def test_hosted_latest_commit_also_uses_five_commit_history_window(self):
        metadata = InMemoryMetadataStore()
        service = HostedIndexingService(
            metadata_store=metadata,
            backend_driver=None,
        )
        expected_result = SimpleNamespace(repo_id="demo")
        commit_service = SimpleNamespace(
            git_metadata=SimpleNamespace(
                get_commit_info=lambda repo_path, branch_hint=None: make_commit(sha="head123")
            ),
            apply_commit_with_history=lambda repo_path, **kwargs: (
                self.assertEqual(kwargs["commit_sha"], "head123"),
                self.assertEqual(kwargs["history_window"], 5),
                expected_result,
            )[2],
        )

        result = service._index_commits(
            request=SimpleNamespace(commit_sha=None, ref=None, limit=100, execute=False),
            repo_path="/tmp/demo",
            repo_id="demo",
            branch_hint="main",
            commit_service=commit_service,
        )

        self.assertIs(result, expected_result)

    def test_hosted_service_defaults_to_unauthenticated_github_client(self):
        service = HostedIndexingService(
            metadata_store=InMemoryMetadataStore(),
            backend_driver=None,
        )

        self.assertIsInstance(service.github_api, GitHubApiClient)
        self.assertIsNone(service.github_api.token)

    def test_hosted_indexing_records_github_sync_success_in_repo_source_state(self):
        metadata = InMemoryMetadataStore()
        source_state = RepoSourceState(
            repo_id="demo",
            provider="github",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            default_branch="main",
            clone_path="/tmp/demo",
        )
        managed_repo = ManagedRepo(
            repo_id="demo",
            repo_path="/tmp/demo",
            request_id="req-1",
            work_root="/tmp/work",
            repo_ref=None,
            source_state=source_state,
        )
        repo_ref = GitHubRepoRef(
            owner="octo",
            name="demo",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            html_url="https://github.com/octo/demo",
            default_branch="main",
            source="github_url",
        )
        commit_result = SimpleNamespace(
            operations=MutationPlan(),
            applied_commit_shas=("abc123",),
            apply_result=SimpleNamespace(operations_applied=0),
            commit=make_commit(),
            skipped=False,
            skip_reason=None,
            previous_commit_sha=None,
            snapshot=make_snapshot(),
        )
        github_result = GitHubSyncResult(
            repo_ref=repo_ref,
            skipped=False,
            skip_reason=None,
            error=None,
            operations=MutationPlan(
                upsert_nodes=(
                    NodeRecord(
                        record_id="github:pr:demo:7",
                        record_type="github_pull_request",
                        repo_id="demo",
                        group_id="demo",
                        labels=("BackendRecord", "GitHubEntity", "PullRequest"),
                    ),
                )
            ),
            operations_applied=0,
            entity_counts={"PullRequest": 1},
            pull_request_numbers=(7,),
            issue_numbers=(),
        )
        service = HostedIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            github_api=SimpleNamespace(token="secret"),
            repo_cache=FakeRepoCache(managed_repo),
            repo_resolver=FakeRepoResolver(repo_ref),
        )
        service._index_commits = lambda **kwargs: commit_result  # type: ignore[method-assign]

        with patch(
            "context_graph.ingestion.hosted_indexing.GitHubIndexer.sync_for_commits",
            return_value=github_result,
        ):
            result = service.index(
                SimpleNamespace(
                    repo_path=None,
                    github_url="https://github.com/octo/demo",
                    github_repo=None,
                    repo_id="demo",
                    ref=None,
                    commit_sha=None,
                    limit=100,
                    execute=False,
                )
            )

        self.assertIsNotNone(result.github_sync_result)
        self.assertEqual(result.operations.operation_count(), 1)
        self.assertEqual(result.observation_operations.operation_count(), 0)
        saved_source = metadata.load_repo_source_state("demo")
        self.assertEqual(saved_source.last_github_sync_status, "applied")
        self.assertIsNone(saved_source.last_github_sync_error)

    def test_hosted_indexing_can_sync_github_without_explicit_token(self):
        metadata = InMemoryMetadataStore()
        source_state = RepoSourceState(
            repo_id="demo",
            provider="github",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            default_branch="main",
            clone_path="/tmp/demo",
        )
        managed_repo = ManagedRepo(
            repo_id="demo",
            repo_path="/tmp/demo",
            request_id="req-1",
            work_root="/tmp/work",
            repo_ref=None,
            source_state=source_state,
        )
        repo_ref = GitHubRepoRef(
            owner="octo",
            name="demo",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            html_url="https://github.com/octo/demo",
            default_branch="main",
            source="github_url",
        )
        commit_result = SimpleNamespace(
            operations=MutationPlan(),
            applied_commit_shas=("abc123",),
            apply_result=SimpleNamespace(operations_applied=0),
            commit=make_commit(),
            skipped=False,
            skip_reason=None,
            previous_commit_sha=None,
            snapshot=make_snapshot(),
        )
        github_result = GitHubSyncResult(
            repo_ref=repo_ref,
            skipped=False,
            skip_reason=None,
            error=None,
            operations=MutationPlan(),
            operations_applied=0,
            entity_counts={"PullRequest": 1},
            pull_request_numbers=(7,),
            issue_numbers=(),
        )
        service = HostedIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            github_api=None,
            repo_cache=FakeRepoCache(managed_repo),
            repo_resolver=FakeRepoResolver(repo_ref),
        )
        service._index_commits = lambda **kwargs: commit_result  # type: ignore[method-assign]

        with patch(
            "context_graph.ingestion.hosted_indexing.GitHubIndexer.sync_for_commits",
            return_value=github_result,
        ) as sync_mock:
            result = service.index(
                SimpleNamespace(
                    repo_path=None,
                    github_url="https://github.com/octo/demo",
                    github_repo=None,
                    repo_id="demo",
                    ref=None,
                    commit_sha=None,
                    limit=100,
                    execute=False,
                )
            )

        self.assertIsNotNone(result.github_sync_result)
        self.assertEqual(result.github_sync_result.pull_request_numbers, (7,))
        sync_mock.assert_called_once()

    def test_hosted_indexing_preserves_commit_result_when_github_sync_fails(self):
        metadata = InMemoryMetadataStore()
        source_state = RepoSourceState(
            repo_id="demo",
            provider="github",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            default_branch="main",
            clone_path="/tmp/demo",
        )
        managed_repo = ManagedRepo(
            repo_id="demo",
            repo_path="/tmp/demo",
            request_id="req-1",
            work_root="/tmp/work",
            repo_ref=None,
            source_state=source_state,
        )
        repo_ref = GitHubRepoRef(
            owner="octo",
            name="demo",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            html_url="https://github.com/octo/demo",
            default_branch="main",
            source="github_url",
        )
        commit_plan = MutationPlan(
            upsert_nodes=(
                NodeRecord(
                    record_id="project:demo",
                    record_type="project",
                    repo_id="demo",
                    group_id="demo",
                    labels=("BackendRecord", "CodeNode", "Project"),
                ),
            )
        )
        commit_result = SimpleNamespace(
            operations=commit_plan,
            applied_commit_shas=("abc123",),
            apply_result=SimpleNamespace(operations_applied=0),
            commit=make_commit(),
            skipped=False,
            skip_reason=None,
            previous_commit_sha=None,
            snapshot=make_snapshot(),
        )
        service = HostedIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            github_api=SimpleNamespace(token="secret"),
            repo_cache=FakeRepoCache(managed_repo),
            repo_resolver=FakeRepoResolver(repo_ref),
        )
        service._index_commits = lambda **kwargs: commit_result  # type: ignore[method-assign]

        with patch(
            "context_graph.ingestion.hosted_indexing.GitHubIndexer.sync_for_commits",
            side_effect=RuntimeError("github exploded"),
        ):
            result = service.index(
                SimpleNamespace(
                    repo_path=None,
                    github_url="https://github.com/octo/demo",
                    github_repo=None,
                    repo_id="demo",
                    ref=None,
                    commit_sha=None,
                    limit=100,
                    execute=False,
                )
        )

        self.assertEqual(result.operations.operation_count(), commit_plan.operation_count())
        self.assertEqual(result.observation_operations.operation_count(), 0)
        self.assertEqual(result.github_sync_result.error, "github exploded")
        saved_source = metadata.load_repo_source_state("demo")
        self.assertEqual(saved_source.last_github_sync_status, "failed")
        self.assertEqual(saved_source.last_github_sync_error, "github exploded")


class DeterministicCliTests(unittest.TestCase):
    def test_apply_commit_dry_run_emits_operations_preview(self):
        import context_graph.cli as cli

        snapshot = make_snapshot()
        plan = MutationPlan(
            upsert_nodes=(
                NodeRecord(
                    record_id="project:demo",
                    record_type="project",
                    repo_id="demo",
                    group_id="demo",
                    labels=("BackendRecord", "CodeNode", "Project"),
                    properties={"node_id": "project:demo", "name": "demo"},
                ),
            )
        )
        fake_result = SimpleNamespace(
            repo_id="demo",
            repo_path="/tmp/demo",
            skipped=False,
            skip_reason=None,
            previous_commit_sha=None,
            applied_commit_shas=("abc123",),
            commit=make_commit(),
            snapshot=snapshot,
            apply_result=SimpleNamespace(
                diff=SimpleNamespace(
                    previous_revision_id=None,
                    new_revision_id="r1",
                    added_nodes={},
                    changed_nodes={},
                    removed_nodes={},
                    added_edges={},
                    changed_edges={},
                    removed_edges={},
                ),
                operations_applied=0,
            ),
            operations=plan,
        )

        with (
            patch("context_graph.cli._make_metadata_store", return_value=InMemoryMetadataStore()),
            patch("context_graph.cli.IngestionDriver") as driver_cls,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            driver_cls.return_value.apply_commit.return_value = fake_result
            exit_code = cli.main(
                [
                    "apply-commit",
                    "/tmp/demo",
                    "--commit",
                    "abc123",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIn("operations_preview", payload)
        self.assertNotIn("statements_preview", payload)

    def test_reset_context_dry_run_uses_structured_reset_preview(self):
        import context_graph.cli as cli

        with (
            patch("context_graph.cli._make_metadata_store", return_value=InMemoryMetadataStore()),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = cli.main(
                [
                    "reset-context",
                    "--repo-id",
                    "demo",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIn("graph_reset", payload)
        self.assertIn("graph_reset_preview", payload)
        self.assertNotIn("graph_statements_preview", payload)




class MetadataBackendTests(unittest.TestCase):
    def test_create_metadata_store_returns_in_memory_store(self):
        store = create_metadata_store(InMemoryMetadataConfig())
        self.assertIsInstance(store, InMemoryMetadataStore)
        self.assertIsInstance(store, MetadataStore)

    def test_in_memory_metadata_round_trips_snapshot_source_context_and_profiles(self):
        store = InMemoryMetadataStore()
        snapshot = make_snapshot()
        source_state = RepoSourceState(
            repo_id="demo",
            provider="github",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            default_branch="main",
            clone_path="/tmp/demo",
            metadata={"source": "github_url"},
        )
        index_state = RepoIndexState(
            repo_id="demo",
            root_path="/tmp/demo",
            latest_revision_id="r1",
            latest_indexed_at="2026-03-26T00:00:00+00:00",
            metadata={"source": "git"},
            latest_commit_sha="abc123",
            latest_branch="main",
            last_commit_indexed_at="2026-03-26T00:00:00+00:00",
        )
        profile = ProfileRecord(
            profile_id="profile:demo:semantic:1",
            repo_id="demo",
            profile_kind="semantic_fact_profile",
            profile_target_kind="FactEdge",
            profile_target_id="fact:1",
            relation_type="JUSTIFIES",
            source_node_id="symbol:demo:demo.app.main",
            source_node_kind="Function",
            target_node_id="symbol:demo:demo.app.helper",
            target_node_kind="Function",
            target_revision_id=None,
            value_text="main uses helper",
            keys=("main helper",),
            metadata={"episode_ids": ["turn-1"]},
            is_active=True,
            updated_at="2026-03-26T00:00:00+00:00",
        )

        store.save_latest_snapshot(snapshot)
        store.save_repo_source_state(source_state)
        store.save_repo_index_state(index_state)
        store.upsert_profiles((profile,))

        self.assertEqual(store.load_latest_snapshot("demo").revision_id, "r1")
        self.assertEqual(store.load_repo_source_state("demo").canonical_repo, "octo/demo")
        self.assertEqual(store.load_repo_index_state("demo").latest_commit_sha, "abc123")
        self.assertEqual(store.load_profiles("demo", active_only=True)[0].profile_id, profile.profile_id)

    def test_in_memory_metadata_round_trips_relation_profiles(self):
        store = InMemoryMetadataStore()
        profile = semantic_relation_profile_from_spec(
            RelationProfileSpec(
                repo_id="demo",
                profile_target_id="claim_relation:claim:1:claim:2:contradicts",
                relation_type="contradicts",
                source_record_id="claim:1",
                source_record_type="claim",
                target_record_id="claim:2",
                target_record_type="claim",
                value_text="Leaving MAX_RETRIES at 3 contradicts the earlier increase to 5.",
                metadata={"resolution_confidence": "high"},
                updated_at="2026-03-26T00:00:00+00:00",
            )
        )

        assert profile is not None
        store.upsert_profiles((profile,))

        stored = store.load_profiles("demo", active_only=True)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].profile_kind, SEMANTIC_RELATION_PROFILE_KIND)
        self.assertEqual(stored[0].metadata["source_record_type"], "claim")

    def test_sqlite_metadata_bootstrap_and_reset_match_existing_behavior(self):
        store = SQLiteMetadataStore(":memory:")
        snapshot = make_snapshot()
        profile = ProfileRecord(
            profile_id="profile:demo:semantic:1",
            repo_id="demo",
            profile_kind="semantic_fact_profile",
            profile_target_kind="FactEdge",
            profile_target_id="fact:1",
            relation_type="JUSTIFIES",
            source_node_id="symbol:demo:demo.app.main",
            source_node_kind="Function",
            target_node_id="symbol:demo:demo.app.helper",
            target_node_kind="Function",
            target_revision_id=None,
            value_text="main uses helper",
            keys=("main helper",),
            metadata={},
            is_active=True,
            updated_at="2026-03-26T00:00:00+00:00",
        )

        store.bootstrap()
        rows = store.connection.execute("PRAGMA table_info(cg_profiles)").fetchall()
        self.assertEqual(len(rows), 18)

        store.save_latest_snapshot(snapshot)
        store.upsert_profiles((profile,))
        store.record_index_run(
            repo_id="demo",
            revision_id="r1",
            status="applied",
            stats={"nodes": len(snapshot.nodes), "edges": len(snapshot.edges)},
        )

        deleted = store.reset_repo_context("demo")
        self.assertEqual(
            deleted,
            {
                "repo_source": 0,
                "repo_state": 1,
                "current_nodes": len(snapshot.nodes),
                "current_edges": len(snapshot.edges),
                "profiles": 1,
                "index_runs": 1,
            },
        )

    def test_postgres_metadata_repo_lock_uses_advisory_locking(self):
        connection = RecordingConnection()
        store = PostgresMetadataStore(connection)

        with store.repo_lock("octo/demo"):
            pass

        sql_text = "\n".join(sql for sql, _ in connection.executed)
        self.assertIn("SELECT pg_advisory_lock(%s)", sql_text)
        self.assertIn("SELECT pg_advisory_unlock(%s)", sql_text)

    def test_postgres_metadata_store_rolls_back_after_failed_query(self):
        connection = FailingConnection(fail_on="execute")
        store = PostgresMetadataStore(connection)

        with self.assertRaises(RuntimeError):
            store.load_profiles("demo")

        self.assertEqual(connection.rollbacks, 1)


class ProfileVectorBackendTests(unittest.TestCase):
    def test_create_profile_vector_store_returns_in_memory_store(self):
        store = create_profile_vector_store(InMemoryProfileVectorConfig())
        self.assertIsInstance(store, InMemoryProfileVectorStore)

    def test_create_profile_vector_store_from_metadata_rejects_non_postgres(self):
        with self.assertRaises(RuntimeError):
            create_profile_vector_store_from_metadata(InMemoryMetadataStore())

    def test_in_memory_vector_store_ranks_hits_and_honors_filters(self):
        store = InMemoryProfileVectorStore()
        store.upsert_profile_keys(
            (
                ProfileKeyRecord(
                    profile_key_id="pkey:1",
                    profile_id="profile:1",
                    repo_id="demo",
                    profile_kind="semantic_fact_profile",
                    relation_type="JUSTIFIES",
                    source_node_id="main",
                    target_node_id="helper",
                    key_text="helper rollback",
                    key_origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
                    embedding=(1.0, 1.0, 0.0),
                    is_active=True,
                    valid_from_revision=None,
                    valid_to_revision=None,
                    updated_at="2026-03-26T00:00:00+00:00",
                ),
                ProfileKeyRecord(
                    profile_key_id="pkey:2",
                    profile_id="profile:2",
                    repo_id="demo",
                    profile_kind="code_relation_profile",
                    relation_type="CALLS",
                    source_node_id="main",
                    target_node_id="helper",
                    key_text="main helper",
                    key_origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
                    embedding=(1.0, 0.0, 1.0),
                    is_active=False,
                    valid_from_revision=None,
                    valid_to_revision=None,
                    updated_at="2026-03-26T00:00:00+00:00",
                ),
            )
        )

        hits = store.search_profile_keys(
            repo_id="demo",
            embedding=(1.0, 1.0, 0.0),
            limit=5,
            active_only=True,
            profile_kinds=("semantic_fact_profile",),
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].profile_id, "profile:1")

    def test_pgvector_store_bootstrap_and_mutations_issue_expected_sql(self):
        connection = RecordingConnection()
        store = PgVectorProfileVectorStore(connection, embedding_dim=1536)
        record = ProfileKeyRecord(
            profile_key_id="pkey:1",
            profile_id="profile:1",
            repo_id="demo",
            profile_kind="semantic_fact_profile",
            relation_type="JUSTIFIES",
            source_node_id="main",
            target_node_id="helper",
            key_text="helper rollback",
            key_origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
            embedding=(1.0, 0.0, 0.0),
            is_active=True,
            valid_from_revision=None,
            valid_to_revision=None,
            updated_at="2026-03-26T00:00:00+00:00",
        )
        deactivation = ProfileDeactivation(
            repo_id="demo",
            profile_kind="semantic_fact_profile",
            relation_type="JUSTIFIES",
            source_node_id="main",
            target_node_id="helper",
            observed_at="2026-03-26T00:05:00+00:00",
        )

        store.bootstrap()
        store.upsert_profile_keys((record,))
        store.deactivate_profile_keys((deactivation,))
        connection.fetchone_values = [("cg_profile_keys",), (1536,), (2,)]
        deleted = store.reset_repo_context("demo")

        sql_text = "\n".join(sql for sql, _ in connection.executed if isinstance(sql, str))
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", sql_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS cg_profile_keys", sql_text)
        self.assertIn("vector(1536)", sql_text)
        self.assertIn("INSERT INTO cg_profile_keys", sql_text)
        self.assertIn("UPDATE cg_profile_keys", sql_text)
        self.assertIn("DELETE FROM cg_profile_keys", sql_text)
        self.assertEqual(deleted, 2)

    def test_pgvector_store_rolls_back_after_failed_search(self):
        connection = FailingConnection(fail_on="execute")
        store = PgVectorProfileVectorStore(connection, embedding_dim=1536)

        with self.assertRaises(RuntimeError):
            store.search_profile_keys(
                repo_id="demo",
                embedding=(1.0, 0.0, 0.0),
                limit=5,
            )

        self.assertGreaterEqual(connection.rollbacks, 1)

    def test_pgvector_store_rebuilds_table_when_embedding_dimension_changes(self):
        connection = RecordingConnection()
        connection.fetchone_values = [
            ("cg_profile_keys",),
            (1024,),
        ]
        store = PgVectorProfileVectorStore(connection, embedding_dim=1536)

        store.bootstrap()

        sql_text = "\n".join(sql for sql, _ in connection.executed if isinstance(sql, str))
        self.assertIn("DROP TABLE IF EXISTS cg_profile_keys", sql_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS cg_profile_keys", sql_text)
        self.assertTrue(store.needs_resync)


class ProfileServiceIntegrationTests(unittest.TestCase):
    def test_profile_services_still_work_with_backend_store_interfaces(self):
        metadata = InMemoryMetadataStore()
        profile = ProfileRecord(
            profile_id="profile:demo:semantic:1",
            repo_id="demo",
            profile_kind="semantic_fact_profile",
            profile_target_kind="FactEdge",
            profile_target_id="fact:1",
            relation_type="JUSTIFIES",
            source_node_id="symbol:demo:demo.app.main",
            source_node_kind="Function",
            target_node_id="symbol:demo:demo.app.helper",
            target_node_kind="Function",
            target_revision_id=None,
            value_text="main uses helper and has rollback guidance",
            keys=(
                ProfileKeyDefinition("helper rollback", PROFILE_KEY_ORIGIN_RAW_TEXT),
                ProfileKeyDefinition("main helper", PROFILE_KEY_ORIGIN_RAW_TEXT),
            ),
            metadata={"episode_ids": ["turn-1"]},
            is_active=True,
            updated_at="2026-03-26T00:00:00+00:00",
        )
        metadata.upsert_profiles((profile,))

        vector_store = InMemoryProfileVectorStore()
        indexer = ProfileVectorIndexer(
            vector_store=vector_store,
            embedding_provider=FakeEmbeddingProvider(),
        )
        indexed = indexer.sync_repo(repo_id="demo", metadata_store=metadata)

        service = ProfileSearchService(
            metadata_store=metadata,
            vector_store=vector_store,
            embedding_provider=FakeEmbeddingProvider(),
            query_parser=FakeQueryParser(("helper rollback",)),
        )
        result = service.search(
            repo_id="demo",
            query="What is the rollback plan for helper?",
            limit=5,
            active_only=True,
            llm_enabled=True,
        )

        self.assertGreater(indexed, 0)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.profiles[0].profile_id, profile.profile_id)


class BackendSyncBridgeTests(unittest.TestCase):
    def test_bridge_uses_driver_owning_loop_when_present(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        class LoopAwareDriver:
            def __init__(self, owning_loop):
                self._context_graph_loop = owning_loop
                self.health_loop = None
                self.build_loop = None
                self.apply_loop = None
                self.close_loop = None
                self.mutations = SimpleNamespace(apply=self._apply)
                self.maintenance = SimpleNamespace(
                    reset_repo=self._reset_repo,
                    reset_scope=self._reset_scope,
                )

            async def health_check(self):
                self.health_loop = asyncio.get_running_loop()

            async def build_indices(self, delete_existing: bool = False):
                self.build_loop = asyncio.get_running_loop()

            async def _apply(self, plan):
                self.apply_loop = asyncio.get_running_loop()
                return MutationApplyResult(0, 0, 0)

            async def _reset_repo(self, repo_id):
                return None

            async def _reset_scope(self, request):
                return None

            async def close(self):
                self.close_loop = asyncio.get_running_loop()

        try:
            driver = LoopAwareDriver(loop)
            bridge = BackendSyncBridge(driver)

            result = bridge.apply(MutationPlan())
            bridge.close()

            self.assertEqual(result.operations_applied, 0)
            self.assertIs(driver.health_loop, loop)
            self.assertIs(driver.build_loop, loop)
            self.assertIs(driver.apply_loop, loop)
            self.assertIs(driver.close_loop, loop)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)

    def test_profile_services_can_filter_to_relation_profiles(self):
        metadata = InMemoryMetadataStore()
        profile = semantic_relation_profile_from_spec(
            RelationProfileSpec(
                repo_id="demo",
                profile_target_id="claim_relation:claim:1:claim:2:contradicts",
                relation_type="contradicts",
                source_record_id="claim:1",
                source_record_type="claim",
                target_record_id="claim:2",
                target_record_type="claim",
                value_text="Leaving MAX_RETRIES at 3 contradicts the earlier increase to 5.",
                updated_at="2026-03-26T00:00:00+00:00",
            )
        )

        assert profile is not None
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
            query_parser=FakeQueryParser(("MAX_RETRIES contradicts",)),
        )
        result = service.search(
            repo_id="demo",
            query="Which contradiction mentioned MAX_RETRIES?",
            limit=5,
            profile_kinds=(SEMANTIC_RELATION_PROFILE_KIND,),
        )

        self.assertEqual(len(result.profiles), 1)
        self.assertEqual(result.profiles[0].profile_kind, SEMANTIC_RELATION_PROFILE_KIND)
        self.assertEqual(result.hits[0].key_origin, PROFILE_KEY_ORIGIN_RAW_TEXT)


class CLISearchProfilesCommandTests(unittest.TestCase):
    def test_search_profiles_defaults_to_fused_claim_and_observation_search(self):
        base_service = object()
        fused_service = SimpleNamespace(
            search=lambda **kwargs: SimpleNamespace(
                parse_result=QueryParseResult(
                    raw_query=kwargs["query"],
                    anchors=(),
                    high_level_keywords=(),
                    intent_tags=(),
                ),
                hits=(),
                profiles=(),
            )
        )
        vector_store = SimpleNamespace(bootstrap=lambda: None)

        with patch.object(cli_module, "_make_metadata_store", return_value=InMemoryMetadataStore()), patch.object(
            cli_module, "_make_embedding_provider", return_value=object()
        ), patch.object(
            cli_module, "_make_profile_vector_store", return_value=vector_store
        ), patch.object(
            cli_module, "load_episode_llm_settings", return_value=None
        ), patch.object(
            cli_module, "make_episode_llm_client", return_value=None
        ), patch.object(
            cli_module, "ProfileSearchService", return_value=base_service
        ), patch.object(
            cli_module, "SemanticProvenanceSearchService", return_value=fused_service
        ) as fused_ctor, patch.object(
            cli_module, "_emit_json", side_effect=lambda payload: payload
        ):
            payload = cli_module.main(
                ["search-profiles", "--repo-id", "demo", "--query", "auth migration"]
            )

        self.assertEqual(payload["requested_profile_kinds"], [])
        self.assertEqual(payload["profiles"], [])
        fused_ctor.assert_called_once_with(profile_search_service=base_service)

    def test_search_profiles_uses_generic_profile_search_when_filter_is_present(self):
        relation_profile = semantic_relation_profile_from_spec(
            RelationProfileSpec(
                repo_id="demo",
                profile_target_id="claim_relation:claim:1:claim:2:contradicts",
                relation_type="contradicts",
                source_record_id="claim:1",
                source_record_type="claim",
                target_record_id="claim:2",
                target_record_type="claim",
                value_text="Leaving MAX_RETRIES at 3 contradicts the earlier increase to 5.",
                updated_at="2026-03-26T00:00:00+00:00",
            )
        )
        assert relation_profile is not None
        relation_hit = SimpleNamespace(
            score=0.91,
            key_text=relation_profile.value_text,
            key_origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
        )
        base_search_service = SimpleNamespace(
            search=lambda **kwargs: SimpleNamespace(
                parse_result=QueryParseResult(
                    raw_query=kwargs["query"],
                    anchors=(),
                    high_level_keywords=(),
                    intent_tags=(),
                ),
                hits=(relation_hit,),
                profiles=(relation_profile,),
            )
        )
        vector_store = SimpleNamespace(bootstrap=lambda: None)

        with patch.object(cli_module, "_make_metadata_store", return_value=InMemoryMetadataStore()), patch.object(
            cli_module, "_make_embedding_provider", return_value=object()
        ), patch.object(
            cli_module, "_make_profile_vector_store", return_value=vector_store
        ), patch.object(
            cli_module, "load_episode_llm_settings", return_value=None
        ), patch.object(
            cli_module, "make_episode_llm_client", return_value=None
        ), patch.object(
            cli_module, "ProfileSearchService", return_value=base_search_service
        ), patch.object(
            cli_module, "SemanticProvenanceSearchService"
        ) as fused_ctor, patch.object(
            cli_module, "_emit_json", side_effect=lambda payload: payload
        ):
            payload = cli_module.main(
                [
                    "search-profiles",
                    "--repo-id",
                    "demo",
                    "--query",
                    "MAX_RETRIES contradiction",
                    "--profile-kind",
                    SEMANTIC_RELATION_PROFILE_KIND,
                ]
            )

        self.assertEqual(payload["requested_profile_kinds"], [SEMANTIC_RELATION_PROFILE_KIND])
        self.assertEqual(payload["profiles"][0]["profile_kind"], SEMANTIC_RELATION_PROFILE_KIND)
        fused_ctor.assert_not_called()
