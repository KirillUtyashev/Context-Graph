from __future__ import annotations

import json
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_graph.ingestion.commit_indexing import CommitIndexingService
from context_graph.ingestion.deterministic_planner import DeterministicMutationPlanner
from context_graph.ingestion.git.github_client import GitHubRepoRef
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
from context_graph.metadata import (
    InMemoryMetadataStore as LegacyInMemoryMetadataStore,
    PostgresMetadataStore as LegacyPostgresMetadataStore,
    SQLiteMetadataStore as LegacySQLiteMetadataStore,
)
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.profile_vectors import (
    InMemoryProfileVectorStore as LegacyInMemoryProfileVectorStore,
    ProfileSearchService,
    ProfileVectorIndexer,
)
from context_graph.profiles import ProfileDeactivation, ProfileRecord
from context_graph.storage.backend import (
    BackendFeatureNotImplementedError,
    EdgeRecord,
    GraphRecordRef,
    InMemoryMetadataConfig,
    InMemoryMetadataStore,
    InMemoryProfileVectorConfig,
    InMemoryProfileVectorStore,
    MetadataStore,
    MutationPlan,
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
    SQLiteMetadataStore,
    NeptuneConfig,
    create_driver,
    create_metadata_store,
    create_profile_vector_store,
    create_profile_vector_store_from_metadata,
)
from context_graph.storage.backend.neo4j.driver import Neo4jBackendDriver
from context_graph.storage.backend.neo4j.search import Neo4jSearchOperations
from context_graph.storage.backend.neptune.driver import NeptuneBackendDriver
from context_graph.storage.backend.neptune.search import (
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
    def __init__(self, snapshot: GraphSnapshot):
        self.snapshot = snapshot
        self.calls: list[tuple[str, str, str, int]] = []

    def build_snapshot(self, repo_path: str, *, repo_id: str, revision_id: str, limit: int):
        self.calls.append((repo_path, repo_id, revision_id, limit))
        return self.snapshot


class FakeGitMetadata:
    def __init__(self, commit: CommitInfo):
        self.commit = commit

    def resolve_repo_root(self, repo_path: str) -> str:
        return repo_path

    def get_commit_info(self, repo_path: str, sha: str = "HEAD", *, branch_hint: str | None = None):
        _ = repo_path, sha, branch_hint
        return self.commit

    def is_ancestor(self, repo_path: str, ancestor_sha: str, descendant_sha: str) -> bool:
        _ = repo_path, ancestor_sha, descendant_sha
        return True

    def has_new_commit(self, repo_path: str, previous_commit_sha: str | None) -> bool:
        _ = repo_path, previous_commit_sha
        return True

    def list_commits_between(self, repo_path: str, old_sha: str, new_sha: str) -> tuple[str, ...]:
        _ = repo_path, old_sha, new_sha
        return (new_sha,)

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
        _ = sha

        class _Worktree:
            def __enter__(self_inner):
                return repo_path

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _Worktree()


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
            "context_graph.storage.backend.neo4j.driver.AsyncGraphDatabase.driver",
            return_value=fake_client,
        ):
            driver = create_driver(Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw"))
        self.assertIsInstance(driver, Neo4jBackendDriver)
        await driver.close()

    async def test_driver_reports_capabilities_and_builds_fulltext_indices(self):
        fake_client = FakeNeo4jClient()
        with patch(
            "context_graph.storage.backend.neo4j.driver.AsyncGraphDatabase.driver",
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
            "context_graph.storage.backend.neo4j.driver.AsyncGraphDatabase.driver",
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
            "context_graph.storage.backend.neo4j.driver.AsyncGraphDatabase.driver",
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
            "context_graph.storage.backend.neo4j.driver.AsyncGraphDatabase.driver",
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
            "context_graph.storage.backend.neo4j.driver.AsyncGraphDatabase.driver",
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
        self.assertEqual(result.operations.operation_count(), 21)
        self.assertEqual(result.observation_operations.operation_count(), 3)
        self.assertEqual(result.apply_result.operations_applied, 0)
        self.assertEqual(metadata.load_repo_index_state("demo").latest_commit_sha, "r1")

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

    def test_hosted_explicit_commit_only_replays_missing_descendants_when_seeded(self):
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
        connection.fetchone_values = [(2,)]
        deleted = store.reset_repo_context("demo")

        sql_text = "\n".join(sql for sql, _ in connection.executed if isinstance(sql, str))
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", sql_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS cg_profile_keys", sql_text)
        self.assertIn("vector(1536)", sql_text)
        self.assertIn("INSERT INTO cg_profile_keys", sql_text)
        self.assertIn("UPDATE cg_profile_keys", sql_text)
        self.assertIn("DELETE FROM cg_profile_keys", sql_text)
        self.assertEqual(deleted, 2)


class CompatibilityWrapperTests(unittest.TestCase):
    def test_metadata_wrappers_reexport_backend_classes(self):
        self.assertIs(LegacyInMemoryMetadataStore, InMemoryMetadataStore)
        self.assertIs(LegacySQLiteMetadataStore, SQLiteMetadataStore)
        self.assertIs(LegacyPostgresMetadataStore, PostgresMetadataStore)

    def test_profile_vector_wrappers_reexport_backend_classes(self):
        self.assertIs(LegacyInMemoryProfileVectorStore, InMemoryProfileVectorStore)

    def test_profile_services_still_work_with_backend_store_interfaces(self):
        metadata = LegacyInMemoryMetadataStore()
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
            keys=("helper rollback", "main helper"),
            metadata={"episode_ids": ["turn-1"]},
            is_active=True,
            updated_at="2026-03-26T00:00:00+00:00",
        )
        metadata.upsert_profiles((profile,))

        vector_store = LegacyInMemoryProfileVectorStore()
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
