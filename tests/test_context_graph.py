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
from context_graph.profile_vectors import (
    AuroraProfileVectorStore,
    InMemoryProfileVectorStore,
    ProfileSearchService,
    ProfileVectorIndexer,
)
from context_graph.profiles import (
    PROFILE_KEY_ORIGIN_RAW_TEXT,
    ProfileDeactivation,
    ProfileKeyDefinition,
    ProfileRecord,
    SEMANTIC_FACT_PROFILE_KIND,
)
from context_graph.query_parser import QueryParser


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
            keys=(
                ProfileKeyDefinition("helper rollback", PROFILE_KEY_ORIGIN_RAW_TEXT),
                ProfileKeyDefinition("main helper", PROFILE_KEY_ORIGIN_RAW_TEXT),
            ),
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

    def test_first_parent_helpers_follow_branch_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha, feature_sha = create_git_repo_with_branch_divergence(temp_dir)
            metadata = GitMetadata()

            self.assertTrue(metadata.is_first_parent_ancestor(repo_path, first_sha, second_sha))
            self.assertFalse(metadata.is_first_parent_ancestor(repo_path, feature_sha, second_sha))
            self.assertEqual(
                metadata.list_first_parent_commits_between(repo_path, first_sha, second_sha),
                (second_sha,),
            )

    def test_first_parent_replay_excludes_side_branch_commits_but_keeps_merge_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha, feature_sha, merge_sha = create_git_repo_with_merge_commit(temp_dir)
            metadata = GitMetadata()

            self.assertTrue(metadata.is_first_parent_ancestor(repo_path, second_sha, merge_sha))
            self.assertFalse(metadata.is_first_parent_ancestor(repo_path, feature_sha, merge_sha))
            self.assertEqual(
                metadata.list_first_parent_commits_between(repo_path, first_sha, merge_sha),
                (second_sha, merge_sha),
            )

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
            keys=(
                ProfileKeyDefinition("JUSTIFIES", PROFILE_KEY_ORIGIN_RAW_TEXT),
                ProfileKeyDefinition("main", PROFILE_KEY_ORIGIN_RAW_TEXT),
                ProfileKeyDefinition("helper", PROFILE_KEY_ORIGIN_RAW_TEXT),
            ),
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

    def test_apply_commit_rejects_target_off_first_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path, first_sha, second_sha = create_git_repo(temp_dir)
            metadata = InMemoryMetadataStore()
            indexer = IngestionDriver(
                metadata_store=metadata,
                graph_sink=Neo4jGraphSink(executor=RecordingExecutor()),
                snapshot_producer=FakeSnapshotProducer(),
            )

            indexer.apply_commit(repo_path, commit_sha=second_sha, repo_id="demo", execute=False)

            with self.assertRaisesRegex(RuntimeError, "first-parent chain; reset or reseed this repo id"):
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
