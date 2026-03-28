from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_graph.ingestion.commit_indexing import CommitIndexingService, _merge_reason_counts
from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.ingestion.hosted_indexing import (
    HostedIndexingService,
    branch_hint_for_request,
    looks_like_commit,
    repo_lock_key,
)
from context_graph.ingestion.models import HostedIndexRequest
from context_graph.ingestion.repo_cache import RepoCacheManager
from context_graph.ingestion.snapshot_builder import CodebaseMemorySnapshotProducer
from context_graph.models import CanonicalNode, GraphSnapshot
from context_graph.provenance import (
    ClaimCandidate,
    ClaimDerivationService,
    ClaimEvidenceExtractionService,
    ClaimRelationService,
    EvidenceCandidate,
    FactConflictResolutionService,
    Observation,
    ObservationKind,
    ResearchArtifactCandidate,
    ResearchArtifactSynthesisService,
)
from context_graph.provenance import services as provenance_services
from context_graph.provenance.claim_planner import ClaimMutationPlanner, ClaimPromotionService
from context_graph.provenance.grounding import (
    resolve_about_record_id,
    resolve_about_record_ids,
)
from context_graph.provenance.hint_enrichment import EnrichedAboutHint, HintKind, HintSource
from context_graph.provenance.validation import ClaimValidationResult, ValidatedClaimBundle
from context_graph.profile_vectors import (
    ProfileSearchService,
    ProfileVectorIndexer,
    build_profile_key_records,
)
from context_graph.query_parser import QueryParseResult
from context_graph.storage.backend import (
    InMemoryMetadataStore,
    MutationPlan,
    Neo4jConfig,
    NeptuneConfig,
    RepoIndexState,
    RepoSourceState,
    SearchMode,
    SearchQuery,
)
from context_graph.storage.backend.errors import BackendConfigurationError
from context_graph.storage.backend.neo4j.driver import Neo4jBackendDriver
from context_graph.storage.backend.neptune.driver import NeptuneBackendDriver


def make_commit(
    *,
    sha: str = "abc123",
    parents: tuple[str, ...] = ("prev123",),
    branch: str | None = "main",
    changed_files: tuple[str, ...] = ("app.py",),
) -> CommitInfo:
    return CommitInfo(
        sha=sha,
        parent_shas=parents,
        branch=branch,
        author_name="Dev",
        author_email="dev@example.com",
        authored_at="2026-03-26T00:00:00+00:00",
        committed_at="2026-03-26T00:00:05+00:00",
        subject="Add helper support",
        body="body text",
        changed_files=changed_files,
    )


def make_observation(kind: ObservationKind, text: str = "Some text") -> Observation:
    return Observation(
        observation_id=f"observation:{kind.value}:demo:1",
        repo_id="demo",
        observation_kind=kind,
        source_kind="github",
        source_id="github:1",
        observed_at="2026-03-26T00:00:00+00:00",
        author="octocat",
        title="Title",
        text=text,
        metadata={"n": 1},
    )


class _Record:
    def __init__(self, payload):
        self._payload = payload

    def data(self):
        return self._payload


class _AsyncCursor:
    def __init__(self, payload):
        self._payload = payload

    async def data(self):
        return self._payload


class _LegacyNeo4jTx:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.calls = []

    async def run(self, query, params):
        self.calls.append((query, params))
        return _AsyncCursor([{"query": query, "params": params}])

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _LegacyNeo4jSession:
    def __init__(self, tx: _LegacyNeo4jTx):
        self.tx = tx
        self.closed = False
        self.run_calls = []
        self.write_calls = []

    async def begin_transaction(self):
        return self.tx

    async def run(self, query, params):
        self.run_calls.append((query, params))
        return _AsyncCursor([{"query": query, "params": params}])

    async def close(self):
        self.closed = True

    async def execute_write(self, func, *args, **kwargs):
        self.write_calls.append((func, args, kwargs))
        return await func(self, *args, **kwargs)


class _LegacyNeo4jClient:
    def __init__(self, session: _LegacyNeo4jSession):
        self._session = session
        self.execute_calls = []
        self.verified = False
        self.closed = False

    async def execute_query(self, query, parameters_, database_):
        self.execute_calls.append((query, parameters_, database_))
        return SimpleNamespace(records=[_Record({"query": query, "params": parameters_})])

    def session(self, database):
        return self._session

    async def verify_connectivity(self):
        self.verified = True

    async def close(self):
        self.closed = True


class _NeptuneGraphClient:
    def __init__(self):
        self.calls = []
        self.closed = False

    def execute_open_cypher_query(self, **payload):
        self.calls.append(payload)
        return {"results": [{"payload": payload}]}

    def close(self):
        self.closed = True


class _AossIndices:
    def __init__(self):
        self.existing = set()
        self.created = []
        self.deleted = []

    def exists(self, *, index):
        return index in self.existing

    def create(self, *, index, body):
        self.created.append((index, body))
        self.existing.add(index)

    def delete(self, *, index):
        self.deleted.append(index)
        self.existing.discard(index)


class _AossClient:
    def __init__(self):
        self.indices = _AossIndices()
        self.search_calls = []

    def search(self, *, index, body):
        self.search_calls.append((index, body))
        return {"hits": {"hits": []}}


class _FakeGitMetadata:
    def __init__(self, commit: CommitInfo):
        self.commit = commit
        self.has_new_commit_value = True
        self.first_parent_ancestor = True
        self.first_parent_between_result = (commit.sha,)
        self.commit_window = (commit.sha,)
        self.calls = []
        self.ref_map = {}

    def resolve_repo_root(self, repo_path: str) -> str:
        return repo_path

    def get_commit_info(self, repo_path: str, sha: str = "HEAD", *, branch_hint: str | None = None):
        self.calls.append(("get_commit_info", repo_path, sha, branch_hint))
        if branch_hint is not None:
            return make_commit(sha=self.commit.sha, branch=branch_hint)
        return self.commit

    def has_new_commit(self, repo_path: str, previous_commit_sha: str | None) -> bool:
        self.calls.append(("has_new_commit", repo_path, previous_commit_sha))
        return self.has_new_commit_value

    def is_first_parent_ancestor(self, repo_path: str, ancestor_sha: str, descendant_sha: str) -> bool:
        self.calls.append(("is_first_parent_ancestor", repo_path, ancestor_sha, descendant_sha))
        return self.first_parent_ancestor

    def list_first_parent_commits_between(self, repo_path: str, old_sha: str, new_sha: str):
        self.calls.append(("list_first_parent_commits_between", repo_path, old_sha, new_sha))
        return self.first_parent_between_result

    def list_commit_window(self, repo_path: str, end_sha: str, *, max_count: int):
        self.calls.append(("list_commit_window", repo_path, end_sha, max_count))
        return self.commit_window

    def resolve_revision(self, repo_path: str, revision: str) -> str:
        self.calls.append(("resolve_revision", repo_path, revision))
        if revision not in self.ref_map:
            raise RuntimeError(revision)
        return self.ref_map[revision]

    def checkout_branch(self, repo_path: str, branch: str) -> None:
        self.calls.append(("checkout_branch", repo_path, branch))

    def hard_reset(self, repo_path: str, target: str) -> None:
        self.calls.append(("hard_reset", repo_path, target))

    def clone_repo(self, clone_url: str, clone_path: str, *, branch: str | None, auth_token: str | None) -> None:
        self.calls.append(("clone_repo", clone_url, clone_path, branch, auth_token))
        Path(clone_path).mkdir(parents=True, exist_ok=True)

    def remove_repo(self, clone_path: str) -> None:
        self.calls.append(("remove_repo", clone_path))

    def fetch_remote(self, clone_path: str, *, auth_token: str | None) -> None:
        self.calls.append(("fetch_remote", clone_path, auth_token))
        if auth_token == "explode":
            raise RuntimeError("fetch failed")

    def get_head_sha(self, clone_path: str) -> str:
        self.calls.append(("get_head_sha", clone_path))
        return "headsha"

    @contextmanager
    def temporary_worktree(self, repo_path: str, sha: str):
        worktree = Path(repo_path) / "worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        yield str(worktree)


class BackendDriverMoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_neo4j_driver_covers_fallback_transaction_session_and_health(self):
        tx = _LegacyNeo4jTx()
        session = _LegacyNeo4jSession(tx)
        client = _LegacyNeo4jClient(session)
        with patch("context_graph.storage.backend.neo4j.driver.AsyncGraphDatabase.driver", return_value=client):
            driver = Neo4jBackendDriver(Neo4jConfig(uri="bolt://localhost", user="neo4j", password="pw"))

        rows = await driver.execute_query("RETURN 1 AS ok", value=1)
        self.assertEqual(rows[0]["params"]["value"], 1)

        async with driver.transaction() as transaction:
            tx_rows = await transaction.run("RETURN $v AS ok", v=2)
        self.assertEqual(tx_rows[0]["params"]["v"], 2)
        self.assertTrue(tx.committed)

        session_wrapper = driver.session()
        async def _writer(_session, value):
            return value + 1

        wrapped_rows = await session_wrapper.run("RETURN $v AS ok", v=3)
        write_result = await session_wrapper.execute_write(_writer, 4)
        await session_wrapper.__aexit__(None, None, None)
        await driver.health_check()
        await driver.close()

        self.assertEqual(wrapped_rows[0]["params"]["v"], 3)
        self.assertEqual(write_result, 5)
        self.assertTrue(session.closed)
        self.assertTrue(client.verified)
        self.assertTrue(client.closed)

    async def test_neo4j_driver_rolls_back_and_drops_indices(self):
        tx = _LegacyNeo4jTx()
        session = _LegacyNeo4jSession(tx)
        client = _LegacyNeo4jClient(session)
        with patch("context_graph.storage.backend.neo4j.driver.AsyncGraphDatabase.driver", return_value=client):
            driver = Neo4jBackendDriver(Neo4jConfig(uri="bolt://localhost", user="neo4j", password="pw"))

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with driver.transaction():
                raise RuntimeError("boom")

        await driver.build_indices(delete_existing=True)

        queries = [call[0] for call in client.execute_calls]
        self.assertTrue(any(query.startswith("DROP INDEX") for query in queries))
        self.assertTrue(tx.rolled_back)

    async def test_neptune_driver_covers_execute_query_indices_close_and_session(self):
        graph_client = _NeptuneGraphClient()
        graph = SimpleNamespace(client=graph_client)
        aoss = _AossClient()
        aoss.indices.existing.add("backend_node_documents")
        async def _inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(NeptuneBackendDriver, "_create_neptune_client", return_value=graph),
            patch.object(NeptuneBackendDriver, "_create_aoss_client", return_value=aoss),
            patch("context_graph.storage.backend.neptune.driver.asyncio.to_thread", side_effect=_inline_to_thread),
        ):
            driver = NeptuneBackendDriver(
                NeptuneConfig(host="neptune-db://example", aoss_host="aoss.example")
            )

            rows = await driver.execute_query("RETURN $value AS ok", value=7)
            await driver.build_indices(delete_existing=True)
            await driver.health_check()
            session = driver.session()

            async def _writer(session_obj, value):
                query_rows = await session_obj.run("RETURN $value AS ok", value=value)
                return query_rows[0]["payload"]["parameters"]

            write_result = await session.execute_write(_writer, 8)
            await session.__aexit__(None, None, None)
            await driver.close()

        self.assertIn("parameters", rows[0]["payload"])
        self.assertTrue(aoss.indices.deleted)
        self.assertTrue(aoss.indices.created)
        self.assertEqual(json.loads(write_result)["value"], 8)
        self.assertTrue(graph_client.closed)

    def test_neptune_driver_helper_constructors_cover_all_config_paths(self):
        driver = object.__new__(NeptuneBackendDriver)

        with patch.multiple(
            "context_graph.storage.backend.neptune.driver",
            NeptuneGraph=lambda endpoint, port: ("db", endpoint, port),
            NeptuneAnalyticsGraph=lambda graph_id: ("graph", graph_id),
        ):
            self.assertEqual(
                driver._create_neptune_client(NeptuneConfig(host="neptune-db://db.example", port=9999)),
                ("db", "db.example", 9999),
            )
            self.assertEqual(
                driver._create_neptune_client(NeptuneConfig(host="neptune-graph://graph-123")),
                ("graph", "graph-123"),
            )

        with patch.multiple(
            "context_graph.storage.backend.neptune.driver",
            NeptuneGraph=None,
            NeptuneAnalyticsGraph=None,
        ):
            with self.assertRaisesRegex(BackendConfigurationError, "dependencies are not installed"):
                driver._create_neptune_client(NeptuneConfig(host="neptune-db://db.example"))

        with patch.multiple(
            "context_graph.storage.backend.neptune.driver",
            NeptuneGraph=lambda endpoint, port: ("db", endpoint, port),
            NeptuneAnalyticsGraph=lambda graph_id: ("graph", graph_id),
        ):
            with self.assertRaisesRegex(BackendConfigurationError, "must start with neptune-db://"):
                driver._create_neptune_client(NeptuneConfig(host="https://bad"))

        self.assertIsNone(driver._create_aoss_client(NeptuneConfig(host="neptune-db://db.example")))

        with patch.multiple(
            "context_graph.storage.backend.neptune.driver",
            boto3=None,
            OpenSearch=None,
            Urllib3AWSV4SignerAuth=None,
            Urllib3HttpConnection=None,
        ):
            with self.assertRaisesRegex(BackendConfigurationError, "AOSS dependencies are not installed"):
                driver._create_aoss_client(
                    NeptuneConfig(host="neptune-db://db.example", aoss_host="aoss.example")
                )

        fake_session = SimpleNamespace(get_credentials=lambda: "creds", region_name="us-east-1")
        with (
            patch("context_graph.storage.backend.neptune.driver.boto3.Session", return_value=fake_session),
            patch("context_graph.storage.backend.neptune.driver.Urllib3AWSV4SignerAuth", return_value="auth"),
            patch("context_graph.storage.backend.neptune.driver.Urllib3HttpConnection", "http-conn"),
            patch(
                "context_graph.storage.backend.neptune.driver.OpenSearch",
                side_effect=lambda **kwargs: kwargs,
            ),
        ):
            client = driver._create_aoss_client(
                NeptuneConfig(host="neptune-db://db.example", aoss_host="aoss.example", aoss_port=9443)
            )
        self.assertEqual(client["hosts"][0]["port"], 9443)
        self.assertEqual(client["http_auth"], "auth")

    def test_neptune_driver_misc_helpers_cover_aoss_and_query_paths(self):
        graph_client = _NeptuneGraphClient()
        driver = object.__new__(NeptuneBackendDriver)
        driver.client = SimpleNamespace(client=graph_client)
        driver.aoss_client = None

        no_params = driver._run_query("RETURN 1 AS ok", {})
        self.assertEqual(no_params[0]["payload"]["openCypherQuery"], "RETURN 1 AS ok")
        self.assertEqual(driver._run_aoss_query(index_name="idx", query=SearchQuery(mode=SearchMode.LEXICAL, text="t", limit=1)), {})

        driver.aoss_client = _AossClient()
        response = driver._run_aoss_query(
            index_name="backend_node_documents",
            query=SearchQuery(mode=SearchMode.LEXICAL, text="payments", limit=2),
        )
        self.assertIn("hits", response)

    def test_backend_package_exports_are_lazy(self):
        from context_graph.storage.backend import neo4j, neptune

        self.assertEqual(neo4j.Neo4jConfig.__name__, "Neo4jConfig")
        self.assertEqual(neptune.NeptuneConfig.__name__, "NeptuneConfig")
        with self.assertRaises(AttributeError):
            _ = neo4j.NotReal
        with self.assertRaises(AttributeError):
            _ = neptune.NotReal


class CommitAndRepoMoreTests(unittest.TestCase):
    def test_commit_indexing_service_covers_replay_and_guard_edge_cases(self):
        metadata = InMemoryMetadataStore()
        commit = make_commit(sha="head123")
        git_metadata = _FakeGitMetadata(commit)
        service = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            git_metadata=git_metadata,
        )

        git_metadata.has_new_commit_value = False
        skipped = service.apply_latest_commit("/tmp/demo", repo_id="demo")
        self.assertTrue(skipped.skipped)

        git_metadata.has_new_commit_value = True
        applied = []
        service.apply_commit = lambda repo_path, **kwargs: applied.append((repo_path, kwargs)) or "applied"
        self.assertEqual(service.apply_latest_commit("/tmp/demo", repo_id="demo"), "applied")
        self.assertEqual(applied[-1][1]["commit_sha"], "head123")

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
        git_metadata.first_parent_between_result = ()
        self.assertEqual(service.apply_latest_commit("/tmp/demo", repo_id="demo"), "applied")

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            service.apply_commit_sequence("/tmp/demo", commit_shas=(), repo_id="demo")
        self.assertEqual(
            service.apply_commit_sequence("/tmp/demo", commit_shas=("abc123",), repo_id="demo"),
            "applied",
        )

        applied.clear()
        self.assertEqual(
            service.apply_commit_with_history("/tmp/demo", commit_sha="prev123", repo_id="demo"),
            "applied",
        )

        git_metadata.first_parent_ancestor = False
        self.assertEqual(
            service.apply_commit_with_history("/tmp/demo", commit_sha="other456", repo_id="demo"),
            "applied",
        )

        metadata2 = InMemoryMetadataStore()
        git2 = _FakeGitMetadata(make_commit(sha="window999"))
        git2.commit_window = ("older111", "window999")
        service2 = CommitIndexingService(metadata_store=metadata2, backend_driver=None, git_metadata=git2)
        captured = {}
        service2.apply_commit_sequence = lambda repo_path, **kwargs: captured.update(kwargs) or "windowed"
        self.assertEqual(
            service2.apply_commit_with_history(
                "/tmp/demo",
                commit_sha="window999",
                repo_id="demo",
                history_window=0,
            ),
            "windowed",
        )
        self.assertEqual(captured["commit_shas"], ("older111", "window999"))
        self.assertIn(("list_commit_window", "/tmp/demo", "window999", 1), git2.calls)

        with self.assertRaisesRegex(RuntimeError, "switching branches"):
            service._ensure_supported_lineage(
                "/tmp/demo",
                previous_commit_sha="prev123",
                previous_branch="main",
                target_commit=make_commit(branch="dev"),
            )

    def test_commit_indexing_helpers_cover_aggregation_evidence_and_reason_merging(self):
        service = CommitIndexingService(metadata_store=InMemoryMetadataStore(), backend_driver=None)
        service.planner = SimpleNamespace(merge_plans=lambda *plans: MutationPlan())

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            service._aggregate_commit_results(())

        first = SimpleNamespace(
            repo_id="demo",
            repo_path="/tmp/demo",
            commit=make_commit(sha="a"),
            previous_commit_sha=None,
            branch="main",
            skipped=True,
            skip_reason="skip",
            snapshot="snap-a",
            apply_result=SimpleNamespace(snapshot="snap-a", diff="diff-a", operations_emitted=2, operations_applied=1),
            operations=MutationPlan(),
            applied_commit_shas=("a",),
            observation_operations=MutationPlan(),
            evidence_operations=MutationPlan(),
            claim_candidates_extracted=1,
            claim_candidates_validated=1,
            claim_candidates_rejected=0,
            claim_rejection_reasons={"missing_hint": 1},
            evidence_invalidated=1,
        )
        second = SimpleNamespace(
            **{**first.__dict__, "commit": make_commit(sha="b"), "snapshot": "snap-b", "skip_reason": None,
               "apply_result": SimpleNamespace(snapshot="snap-b", diff="diff-b", operations_emitted=3, operations_applied=2),
               "applied_commit_shas": ("b",), "claim_rejection_reasons": {"missing_hint": 2, "other": 1},
               "skipped": False}
        )
        self.assertIs(service._aggregate_commit_results((first,)), first)
        aggregate = service._aggregate_commit_results((first, second))
        self.assertEqual(aggregate.apply_result.operations_emitted, 5)
        self.assertEqual(aggregate.apply_result.operations_applied, 3)
        self.assertEqual(aggregate.claim_rejection_reasons, {"missing_hint": 3, "other": 1})
        self.assertEqual(_merge_reason_counts({"a": 1}, {"a": 2, "b": 1}), {"a": 3, "b": 1})

        planned_observation = SimpleNamespace(
            observation=make_observation(ObservationKind.GIT_COMMIT_MESSAGE),
            source=SimpleNamespace(
                source_kind="commit",
                source_record_type="git_commit",
                source_id="commit:demo:abc123",
                display_name="abc123",
            ),
        )
        zero_plan, zero_counts = service._plan_commit_evidence(
            planned_observation=None,
            snapshot=None,
            commit=make_commit(),
        )
        self.assertEqual(zero_plan.operation_count(), 0)
        self.assertEqual(zero_counts["extracted"], 0)

        service.claim_extraction_service = SimpleNamespace(extract=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
        empty_plan, empty_counts = service._plan_commit_evidence(
            planned_observation=planned_observation,
            snapshot="snapshot",
            commit=make_commit(),
        )
        self.assertEqual(empty_plan.operation_count(), 0)
        self.assertEqual(empty_counts["validated"], 0)

        validation = SimpleNamespace(
            extracted_count=2,
            validated_count=1,
            rejected_count=1,
            rejection_reason_counts={"ambiguous": 1},
        )
        invalidation_result = SimpleNamespace(plan=MutationPlan(), invalidated_count=3)
        service.claim_extraction_service = SimpleNamespace(extract=lambda *args, **kwargs: "extraction")
        service.claim_validator = SimpleNamespace(validate=lambda **kwargs: validation)
        service.evidence_invalidation_planner = SimpleNamespace(
            plan_validated_claims=lambda **kwargs: invalidation_result
        )
        plan, counts = service._plan_commit_evidence(
            planned_observation=planned_observation,
            snapshot="snapshot",
            commit=make_commit(),
        )
        self.assertEqual(plan.operation_count(), 0)
        self.assertEqual(counts["invalidated"], 3)
        self.assertEqual(counts["reasons"], {"ambiguous": 1})

    def test_repo_cache_snapshot_and_hosted_helpers_cover_remaining_paths(self):
        with TemporaryDirectory() as tmpdir:
            git = _FakeGitMetadata(make_commit())
            manager = RepoCacheManager(base_dir=tmpdir, git_metadata=git)
            self.assertEqual(
                manager.manage_local_repo("/tmp/demo", repo_id="demo", request_id="req1").work_root,
                str(Path(tmpdir) / "work" / "demo" / "req1"),
            )

            git.ref_map = {"origin/main": "sha-main", "refs/tags/v1": "sha-tag"}
            self.assertEqual(manager.resolve_ref_sha("/tmp/demo", "main"), "sha-main")
            self.assertEqual(manager.resolve_ref_sha("/tmp/demo", "v1"), "sha-tag")
            with self.assertRaisesRegex(RuntimeError, "unable to resolve git ref"):
                manager.resolve_ref_sha("/tmp/demo", "missing")

            clone_path = Path(tmpdir) / "repos" / "octo" / "demo"
            repo_ref = SimpleNamespace(
                owner="octo",
                name="demo",
                canonical_repo="octo/demo",
                clone_url="https://example.com/octo/demo.git",
                default_branch="main",
                html_url="https://example.com/octo/demo",
                source="github",
            )
            manager._ensure_clone(repo_ref, clone_path, auth_token="token")
            self.assertTrue(any(call[0] == "clone_repo" for call in git.calls))

            clone_path.mkdir(parents=True, exist_ok=True)
            manager._looks_like_git_repo = lambda path: False
            manager._ensure_clone(repo_ref, clone_path, auth_token="token")
            self.assertTrue(any(call[0] == "remove_repo" for call in git.calls))

            manager._looks_like_git_repo = lambda path: True
            manager._ensure_clone(repo_ref, clone_path, auth_token="explode")
            self.assertGreaterEqual(sum(1 for call in git.calls if call[0] == "clone_repo"), 2)
            manager.prepare_default_branch("/tmp/demo", "main")
            self.assertIn(("checkout_branch", "/tmp/demo", "main"), git.calls)

            builder = CodebaseMemorySnapshotProducer(root=Path(tmpdir))
            builder.binary.parent.mkdir(parents=True, exist_ok=True)
            builder.binary.write_text("")
            with patch("context_graph.ingestion.snapshot_builder.subprocess.run") as run:
                builder.ensure_binary()
            run.assert_not_called()

            builder.binary.unlink()
            with patch("context_graph.ingestion.snapshot_builder.subprocess.run") as run:
                builder.ensure_binary()
            run.assert_called_once()

            def _fake_run(command, cwd, check, capture_output=False, text=False):
                if len(command) >= 3:
                    output_path = Path(command[2])
                    output_path.write_text(json.dumps({"nodes": [], "edges": [], "metadata": {}}))
                return SimpleNamespace()

            builder.binary.write_text("")
            with patch("context_graph.ingestion.snapshot_builder.subprocess.run", side_effect=_fake_run):
                with patch(
                    "context_graph.ingestion.snapshot_builder.build_snapshot_from_graph_export",
                    return_value="snapshot",
                ) as build_snapshot:
                    self.assertEqual(builder.build_snapshot("/tmp/demo", repo_id="demo", revision_id="r1"), "snapshot")
            build_snapshot.assert_called_once()

        self.assertEqual(repo_lock_key(None, "demo"), "demo")
        self.assertEqual(repo_lock_key(SimpleNamespace(canonical_repo="octo/demo"), "demo"), "octo/demo")
        self.assertEqual(branch_hint_for_request(request=HostedIndexRequest(ref="feature"), repo_ref=None, source_state=None), "feature")
        self.assertEqual(
            branch_hint_for_request(
                request=HostedIndexRequest(ref="refs/tags/v1"),
                repo_ref=SimpleNamespace(default_branch="main"),
                source_state=None,
            ),
            "main",
        )
        self.assertTrue(looks_like_commit("abc1234"))
        self.assertFalse(looks_like_commit("feature/main"))

        service = HostedIndexingService(metadata_store=InMemoryMetadataStore(), backend_driver=None)
        commit_service = SimpleNamespace(
            apply_commit_with_history=lambda repo_path, **kwargs: kwargs["commit_sha"],
            git_metadata=SimpleNamespace(get_commit_info=lambda repo_path, branch_hint=None: make_commit(sha="latest999")),
        )
        self.assertEqual(
            service._index_commits(
                request=HostedIndexRequest(commit_sha="abc123"),
                repo_path="/tmp/demo",
                repo_id="demo",
                branch_hint="main",
                commit_service=commit_service,
            ),
            "abc123",
        )
        self.assertEqual(
            service._index_commits(
                request=HostedIndexRequest(),
                repo_path="/tmp/demo",
                repo_id="demo",
                branch_hint="main",
                commit_service=commit_service,
            ),
            "latest999",
        )

    def test_commit_indexing_sequence_and_skipped_apply_commit_cover_remaining_lines(self):
        service = CommitIndexingService(metadata_store=InMemoryMetadataStore(), backend_driver=None)
        service.apply_commit = lambda repo_path, **kwargs: kwargs["commit_sha"]
        service._aggregate_commit_results = lambda results: ("aggregate", results)  # type: ignore[method-assign]

        aggregate = service.apply_commit_sequence(
            "/tmp/demo",
            commit_shas=("a", "b"),
            repo_id="demo",
        )
        self.assertEqual(aggregate[0], "aggregate")

        metadata = InMemoryMetadataStore()
        metadata.save_repo_index_state(
            RepoIndexState(
                repo_id="demo",
                root_path="/tmp/demo",
                latest_revision_id="same123",
                latest_indexed_at="2026-03-26T00:00:00+00:00",
                metadata={},
                latest_commit_sha="same123",
                latest_branch="main",
                last_commit_indexed_at="2026-03-26T00:00:00+00:00",
            )
        )
        git_metadata = _FakeGitMetadata(make_commit(sha="same123"))
        service2 = CommitIndexingService(
            metadata_store=metadata,
            backend_driver=None,
            git_metadata=git_metadata,
        )
        result = service2.apply_commit("/tmp/demo", commit_sha="same123", repo_id="demo")
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "commit already indexed")


class ProvenanceMoreTests(unittest.TestCase):
    def test_provenance_services_cover_error_branches_and_remaining_builders(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            ClaimEvidenceExtractionService().build_request(make_observation(ObservationKind.GITHUB_PR_BODY, text="  "))

        for kind in (
            ObservationKind.GITHUB_ISSUE_BODY,
            ObservationKind.GITHUB_PR_REVIEW_COMMENT,
            ObservationKind.GITHUB_ISSUE_COMMENT,
        ):
            request = ClaimEvidenceExtractionService().build_request(make_observation(kind, text="Useful text"))
            self.assertTrue(request.prompt_name.startswith("provenance.extract_claims_and_evidence"))
        generic_request = ClaimEvidenceExtractionService().build_request(
            make_observation(SimpleNamespace(value="generic"), text="Useful text")
        )
        self.assertTrue(generic_request.prompt_name.startswith("provenance.extract_claims_and_evidence"))

        with self.assertRaisesRegex(RuntimeError, "requires a configured llm_client"):
            ClaimEvidenceExtractionService().extract(make_observation(ObservationKind.GITHUB_PR_BODY))

        with self.assertRaisesRegex(ValueError, "evidence must not be empty"):
            ClaimDerivationService().build_request([])
        with self.assertRaisesRegex(ValueError, "at least two claims"):
            ClaimRelationService().build_request(
                [ClaimCandidate(claim_text="one", claim_kind="state_claim", about_hints=[], supported_evidence_ids=[], derivation_kind="normalized_from_evidence")]
            )

        conflict_service = FactConflictResolutionService()
        with self.assertRaisesRegex(ValueError, "claim_text must not be empty"):
            conflict_service.build_request(new_fact={"claim_text": "   "}, current_facts=[{"claim_text": "x"}])
        with self.assertRaisesRegex(ValueError, "current_facts must not be empty"):
            conflict_service.build_request(new_fact={"claim_text": "x"}, current_facts=[])
        with self.assertRaisesRegex(RuntimeError, "requires a configured llm_client"):
            conflict_service.resolve(repo_id="demo", new_fact={"claim_text": "x"}, current_facts=[{"claim_text": "y"}])

        with self.assertRaisesRegex(ValueError, "needs code context, evidence, or claims"):
            ResearchArtifactSynthesisService().build_request()

        artifact_request = ResearchArtifactSynthesisService().build_request(
            claims=[
                ResearchArtifactCandidate(
                    title="T",
                    summary="S",
                    artifact_kind="design_note",
                    evidence_refs=[],
                    about_refs=[],
                )
            ]
        )
        self.assertEqual(artifact_request.response_model.__name__, "ResearchArtifactSynthesisResponse")

        payload = provenance_services._observation_payload(make_observation(ObservationKind.GITHUB_ISSUE_BODY))
        self.assertEqual(payload["observation_kind"], "github_issue_body")
        self.assertEqual(provenance_services._normalize_text_key("  A   B  "), "a b")

        class _InlineThread:
            def __init__(self, *, target, daemon, name):
                self._target = target

            def start(self):
                self._target()

            def join(self):
                return None

        with (
            patch("context_graph.provenance.services.asyncio.get_running_loop", return_value=object()),
            patch("context_graph.provenance.services.threading.Thread", _InlineThread),
        ):
            self.assertEqual(
                provenance_services._run_coroutine_sync(asyncio.sleep(0, result="ok")),
                "ok",
            )

        with (
            patch("context_graph.provenance.services.asyncio.get_running_loop", return_value=object()),
            patch("context_graph.provenance.services.threading.Thread", _InlineThread),
            patch("context_graph.provenance.services.asyncio.run", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                provenance_services._run_coroutine_sync(object())


class GroundingAndPlanningMoreTests(unittest.TestCase):
    def test_grounding_helpers_cover_none_snapshot_and_resolver_branches(self):
        self.assertEqual(resolve_about_record_ids(None, ["demo.app.main"]), ())
        self.assertIsNone(resolve_about_record_id(None, "demo.app.main"))

        hint = EnrichedAboutHint(
            value="demo.app.main",
            hint_kind=HintKind.QUALIFIED_NAME,
            source=HintSource.CLAIM_TEXT,
            raw_value="demo.app.main",
        )
        with patch("context_graph.provenance.grounding.SnapshotCodeResolver") as resolver_cls:
            resolver = resolver_cls.return_value
            resolver.resolve_about_record_ids.return_value = ("symbol:demo:demo.app.main@r1",)
            resolver.resolve_about_record_id.return_value = "symbol:demo:demo.app.main@r1"

            ids = resolve_about_record_ids("snapshot", ["src/app.py", hint])
            record_id = resolve_about_record_id("snapshot", "demo.app.main")

        self.assertEqual(ids, ("symbol:demo:demo.app.main@r1",))
        self.assertEqual(record_id, "symbol:demo:demo.app.main@r1")

    def test_claim_planner_helpers_cover_candidate_building_and_not_implemented(self):
        validation = ClaimValidationResult(
            validated_claims=(
                ValidatedClaimBundle(
                    claim_text="demo.app.main is enabled.",
                    claim_kind="state_claim",
                    about_hints=("demo.app.main",),
                    resolved_about_record_ids=("symbol:demo:demo.app.main@r1",),
                    unresolved_about_hints=(),
                    confidence="high",
                    derivation_kind="direct_from_observation",
                    metadata={},
                    supporting_evidence=(),
                ),
            )
        )
        planner = ClaimMutationPlanner()
        candidates = planner.build_promotion_candidates(validation)
        self.assertEqual(candidates[0].claim_kind, "state_claim")
        with self.assertRaises(NotImplementedError):
            planner.plan_validated_claims(validation)

        service = ClaimPromotionService()
        self.assertEqual(service.prepare_candidates(validation)[0].claim_text, "demo.app.main is enabled.")


class TemporalAndProfileVectorMoreTests(unittest.TestCase):
    def test_temporal_writer_and_profile_vector_helpers_cover_remaining_small_branches(self):
        metadata = InMemoryMetadataStore()
        writer = provenance_services  # placeholder to keep line lengths tame
        del writer

        temporal = __import__("context_graph.ingestion.temporal", fromlist=["TemporalGraphWriter"]).TemporalGraphWriter
        graph_snapshot = GraphSnapshot(
            repo_id="demo",
            revision_id="r1",
            root_path="/tmp/demo",
            generated_at="2026-03-26T00:00:00+00:00",
            nodes=(CanonicalNode("project:demo", "demo", "Project", "demo", qualified_name="demo"),),
            edges=(),
            metadata={},
        )
        diff = SimpleNamespace(
            added_nodes={},
            changed_nodes={},
            removed_nodes={},
            added_edges={},
            changed_edges={},
            removed_edges={},
        )
        operations = MutationPlan()
        writer_obj = temporal(metadata_store=metadata, backend_driver=None)
        with self.assertRaisesRegex(RuntimeError, "configured backend driver"):
            writer_obj.apply_planned_snapshot(graph_snapshot, diff, operations, execute=True)

        writer_obj.sync_bridge = SimpleNamespace(
            ensure_write_ready=lambda: None,
            apply=lambda plan: SimpleNamespace(operations_applied=7),
        )
        applied = writer_obj.apply_planned_snapshot(graph_snapshot, diff, operations, execute=True)
        self.assertEqual(applied.operations_applied, 7)

        vector_store = SimpleNamespace(
            bootstrap=lambda: None,
            deactivate_profile_keys=lambda deactivations: setattr(vector_store, "deactivated", tuple(deactivations)),
            upsert_profile_keys=lambda records: setattr(vector_store, "upserted", tuple(records)),
            search_profile_keys=lambda **kwargs: [
                SimpleNamespace(
                    profile_id="p2",
                    profile_key_id="k2",
                    repo_id="demo",
                    profile_kind="semantic_fact_profile",
                    key_text="helper",
                    score=0.5,
                )
            ],
        )
        embedding_provider = SimpleNamespace(embed_texts=lambda texts: [tuple(float(i + 1) for i in range(2)) for _ in texts])
        indexer = ProfileVectorIndexer(vector_store=vector_store, embedding_provider=embedding_provider)
        deactivation = SimpleNamespace(repo_id="demo")
        self.assertEqual(indexer.apply_profile_updates(profiles=(), deactivations=(deactivation,)), 0)

        profile = SimpleNamespace(
            profile_id="p1",
            repo_id="demo",
            profile_kind="semantic_fact_profile",
            relation_type="RELATES_TO",
            source_node_id="s1",
            target_node_id="t1",
            keys=("helper", "   "),
            is_active=True,
            valid_from_revision=None,
            valid_to_revision=None,
            updated_at="2026-03-26T00:00:00+00:00",
        )
        records = build_profile_key_records((profile,), embedding_provider)
        self.assertEqual(len(records), 1)
        self.assertEqual(build_profile_key_records((), embedding_provider), ())

        parser = SimpleNamespace(
            parse=lambda query, llm_enabled=True: QueryParseResult(
                raw_query=query,
                anchors=(),
                high_level_keywords=(),
                intent_tags=(),
            )
        )
        metadata_store = SimpleNamespace(
            load_profiles=lambda repo_id, active_only=True: [
                SimpleNamespace(profile_id="p2"),
            ]
        )
        search_service = ProfileSearchService(
            metadata_store=metadata_store,
            vector_store=vector_store,
            embedding_provider=embedding_provider,
            query_parser=parser,
        )
        result = search_service.search(repo_id="demo", query="fallback query", limit=1)
        self.assertEqual(result.profiles[0].profile_id, "p2")
