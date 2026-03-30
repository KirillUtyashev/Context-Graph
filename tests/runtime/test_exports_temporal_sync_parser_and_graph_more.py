from __future__ import annotations

import asyncio
import builtins
import importlib
from types import SimpleNamespace

import pytest

from context_graph.backend.maintenance import RepoResetResult, ScopeResetRequest, ScopeResetResult
from context_graph.backend.mutations import MutationApplyResult, MutationPlan
from context_graph.backend.sync_bridge import BackendSyncBridge, _PersistentLoop
from context_graph.ingestion.temporal import TemporalGraphWriter
from context_graph.models import (
    CanonicalEdge,
    CanonicalNode,
    GraphSnapshot,
    SnapshotApplyResult,
    SnapshotDiff,
)
from context_graph.models.profiles import (
    PROFILE_KEY_ORIGIN_RAW_TEXT,
    SEMANTIC_CLAIM_PROFILE_KIND,
    ProfileDeactivation,
    ProfileKeyDefinition,
    ProfileRecord,
)
from context_graph.query import parser as parser_module
from context_graph.backend import profile_vector_services as profile_vector_services_module
from context_graph.runtime import llm as runtime_llm_module
from context_graph.runtime import embedding_clients as runtime_embedding_clients_module


def _make_snapshot(*, repo_id: str = "demo", revision_id: str = "rev-1") -> GraphSnapshot:
    node = CanonicalNode(
        node_id="node:1",
        repo_id=repo_id,
        kind="Function",
        name="main",
        qualified_name="pkg.main",
        file_path="src/app.py",
        start_line=10,
        end_line=12,
        properties={"language": "python"},
    )
    edge = CanonicalEdge(
        edge_id="edge:1",
        repo_id=repo_id,
        edge_type="CALLS",
        source_id=node.node_id,
        target_id=node.node_id,
        properties={"weight": 1},
    )
    return GraphSnapshot(
        repo_id=repo_id,
        revision_id=revision_id,
        root_path="/tmp/demo",
        generated_at="2026-03-30T00:00:00Z",
        nodes=(node,),
        edges=(edge,),
        metadata={"source": "tests"},
    )


def _make_diff(snapshot: GraphSnapshot) -> SnapshotDiff:
    return SnapshotDiff(
        repo_id=snapshot.repo_id,
        previous_revision_id=None,
        new_revision_id=snapshot.revision_id,
        added_nodes={snapshot.nodes[0].node_id: snapshot.nodes[0]},
        changed_nodes={},
        removed_nodes={},
        added_edges={snapshot.edges[0].edge_id: snapshot.edges[0]},
        changed_edges={},
        removed_edges={},
    )


class FakeMetadataStore:
    def __init__(self, previous_snapshot: GraphSnapshot | None = None) -> None:
        self.previous_snapshot = previous_snapshot
        self.bootstrap_calls = 0
        self.loaded_repo_ids: list[str] = []
        self.saved_snapshots: list[GraphSnapshot] = []
        self.index_runs: list[dict[str, object]] = []

    def bootstrap(self) -> None:
        self.bootstrap_calls += 1

    def load_latest_snapshot(self, repo_id: str) -> GraphSnapshot | None:
        self.loaded_repo_ids.append(repo_id)
        return self.previous_snapshot

    def save_latest_snapshot(self, snapshot: GraphSnapshot) -> None:
        self.saved_snapshots.append(snapshot)

    def record_index_run(
        self,
        *,
        repo_id: str,
        revision_id: str,
        status: str,
        stats: dict[str, object],
    ) -> None:
        self.index_runs.append(
            {
                "repo_id": repo_id,
                "revision_id": revision_id,
                "status": status,
                "stats": stats,
            }
        )


class FakePlanner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def plan_snapshot_diff(self, *, previous_snapshot, snapshot, diff):
        self.calls.append(
            {
                "previous_snapshot": previous_snapshot,
                "snapshot": snapshot,
                "diff": diff,
            }
        )
        return "planned-diff"


class FakeAsyncDriver:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.mutations = SimpleNamespace(apply=self._apply)
        self.maintenance = SimpleNamespace(
            reset_repo=self._reset_repo,
            reset_scope=self._reset_scope,
        )

    async def health_check(self) -> None:
        self.calls.append("health_check")

    async def build_indices(self, *, delete_existing: bool = False) -> None:
        self.calls.append(("build_indices", delete_existing))

    async def close(self) -> None:
        self.calls.append("close")

    async def _apply(self, plan: MutationPlan) -> MutationApplyResult:
        self.calls.append(("apply", plan))
        return MutationApplyResult(1, 2, 3)

    async def _reset_repo(self, repo_id: str) -> RepoResetResult:
        self.calls.append(("reset_repo", repo_id))
        return RepoResetResult(repo_id, 2, 1, 1)

    async def _reset_scope(self, request: ScopeResetRequest) -> ScopeResetResult:
        self.calls.append(("reset_scope", request.repo_id))
        return ScopeResetResult(request.repo_id, 1, 1, 0)


class FakeVectorStore:
    def __init__(self) -> None:
        self.bootstrap_calls = 0
        self.deactivations: list[tuple[ProfileDeactivation, ...]] = []
        self.upserts: list[tuple[object, ...]] = []

    def bootstrap(self) -> None:
        self.bootstrap_calls += 1

    def deactivate_profile_keys(self, deactivations) -> None:
        self.deactivations.append(tuple(deactivations))

    def upsert_profile_keys(self, records) -> None:
        self.upserts.append(tuple(records))


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(index + 1)] for index, _ in enumerate(texts)]


def test_lazy_package_exports_cover_backend_and_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    neo4j_module = importlib.import_module("context_graph.backend.neo4j")
    neptune_module = importlib.import_module("context_graph.backend.neptune")
    ingestion_module = importlib.import_module("context_graph.ingestion")

    monkeypatch.setattr(
        neo4j_module,
        "import_module",
        lambda module_name, package_name: {
            (".config", "context_graph.backend.neo4j"): SimpleNamespace(Neo4jConfig="neo4j-config"),
            (".driver", "context_graph.backend.neo4j"): SimpleNamespace(Neo4jBackendDriver="neo4j-driver"),
            (".search", "context_graph.backend.neo4j"): SimpleNamespace(Neo4jSearchOperations="neo4j-search"),
        }[(module_name, package_name)],
    )
    monkeypatch.setattr(
        neptune_module,
        "import_module",
        lambda module_name, package_name: {
            (".config", "context_graph.backend.neptune"): SimpleNamespace(NeptuneConfig="neptune-config"),
            (".driver", "context_graph.backend.neptune"): SimpleNamespace(NeptuneBackendDriver="neptune-driver"),
        }[(module_name, package_name)],
    )
    monkeypatch.setattr(
        ingestion_module,
        "import_module",
        lambda module_name, package_name: {
            (".temporal", "context_graph.ingestion"): SimpleNamespace(TemporalGraphWriter="temporal-writer"),
            (".repo_cache", "context_graph.ingestion"): SimpleNamespace(RepoCacheManager="repo-cache"),
        }[(module_name, package_name)],
    )

    assert neo4j_module.Neo4jConfig == "neo4j-config"
    assert neo4j_module.Neo4jBackendDriver == "neo4j-driver"
    assert neo4j_module.Neo4jSearchOperations == "neo4j-search"
    assert neptune_module.NeptuneConfig == "neptune-config"
    assert neptune_module.NeptuneBackendDriver == "neptune-driver"
    assert ingestion_module.TemporalGraphWriter == "temporal-writer"
    assert ingestion_module.RepoCacheManager == "repo-cache"

    with pytest.raises(AttributeError):
        getattr(neo4j_module, "MissingExport")
    with pytest.raises(AttributeError):
        getattr(neptune_module, "MissingExport")
    with pytest.raises(AttributeError):
        getattr(ingestion_module, "MissingExport")


def test_optional_runtime_package_exports_skip_bedrock_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        package = (globals or {}).get("__package__")
        if package == "context_graph.runtime.embedding_clients" and name == "bedrock" and level == 1:
            raise ImportError("bedrock unavailable")
        if package == "context_graph.runtime.llm" and name == "bedrock_client" and level == 1:
            raise ImportError("bedrock unavailable")
        return real_import(name, globals, locals, fromlist, level)

    try:
        monkeypatch.setattr(builtins, "__import__", fake_import)
        embedding_module = importlib.reload(runtime_embedding_clients_module)
        llm_module = importlib.reload(runtime_llm_module)
        assert "OpenAIEmbedder" in embedding_module.__all__
        assert "BedrockEmbedder" not in embedding_module.__all__
        assert "OpenAIClient" in llm_module.__all__
        assert "BedrockLLMClient" not in llm_module.__all__
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)
        importlib.reload(runtime_embedding_clients_module)
        importlib.reload(runtime_llm_module)


def test_query_parser_fallback_schema_without_pydantic(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pydantic":
            raise ImportError("pydantic unavailable")
        return real_import(name, globals, locals, fromlist, level)

    try:
        monkeypatch.setattr(builtins, "__import__", fake_import)
        reloaded_parser = importlib.reload(parser_module)
        schema = reloaded_parser.ParsedHighLevelKeywords.model_json_schema()
        assert schema["required"] == ["high_level_keywords"]
        assert schema["properties"]["high_level_keywords"]["type"] == "array"
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)
        importlib.reload(parser_module)


def test_query_parser_helpers_cover_anchor_edges_and_threaded_errors() -> None:
    prompt_message = parser_module.PromptMessage(role="user", content="hello")
    assert prompt_message.model_dump() == {"role": "user", "content": "hello"}

    anchors = parser_module._extract_deterministic_anchors(
        '`pkg.main` `   ` "quoted phrase" #123 #123 abcdef0 /api/v1 src/app.py src/app.py auth_service CamelCaseSymbol'
    )
    assert parser_module.QueryAnchor(kind="quoted", value="pkg.main") in anchors
    assert parser_module.QueryAnchor(kind="quoted", value="quoted phrase") in anchors
    assert parser_module.QueryAnchor(kind="artifact_number", value="#123") in anchors
    assert parser_module.QueryAnchor(kind="commit_sha", value="abcdef0") in anchors
    assert parser_module.QueryAnchor(kind="route", value="/api/v1") in anchors
    assert parser_module.QueryAnchor(kind="file_path", value="src/app.py") in anchors
    assert parser_module.QueryAnchor(kind="symbol", value="pkg.main") in anchors
    assert parser_module.QueryAnchor(kind="symbol", value="auth_service") in anchors
    assert parser_module.QueryAnchor(kind="symbol", value="CamelCaseSymbol") in anchors
    assert anchors.count(parser_module.QueryAnchor(kind="artifact_number", value="#123")) == 1
    assert anchors.count(parser_module.QueryAnchor(kind="quoted", value="")) == 0
    assert parser_module._looks_like_symbol("HTTP") is False
    assert parser_module._looks_like_symbol("camelCaseSymbol") is True
    assert parser_module._looks_like_file_path("src/app.py") is True
    assert parser_module._looks_like_file_path("not-a-path") is False

    async def bad_coro() -> None:
        raise ValueError("boom")

    async def run_inside_loop() -> None:
        with pytest.raises(ValueError, match="boom"):
            parser_module._run_async(bad_coro())

    asyncio.run(run_inside_loop())


def test_temporal_graph_writer_covers_planning_finalize_and_execute_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_snapshot = _make_snapshot(revision_id="rev-0")
    snapshot = _make_snapshot(revision_id="rev-1")
    diff = _make_diff(snapshot)
    metadata_store = FakeMetadataStore(previous_snapshot=previous_snapshot)
    planner = FakePlanner()
    writer = TemporalGraphWriter(
        metadata_store=metadata_store,
        backend_driver=None,
        planner=planner,
    )
    monkeypatch.setattr("context_graph.ingestion.temporal.diff_snapshots", lambda previous, current: diff)

    assert metadata_store.bootstrap_calls == 1
    assert writer.plan_snapshot(snapshot) == "planned-diff"
    assert metadata_store.loaded_repo_ids == ["demo"]
    assert planner.calls[0]["previous_snapshot"] == previous_snapshot
    assert planner.calls[0]["diff"] == diff

    operations = MutationPlan()
    with pytest.raises(RuntimeError, match="configured backend driver"):
        writer.apply_planned_snapshot(snapshot, diff, operations, execute=True)

    writer.sync_bridge = SimpleNamespace(
        ensure_write_ready=lambda: None,
        apply=lambda plan: MutationApplyResult(3, 0, 1),
    )
    result = writer.apply_planned_snapshot(snapshot, diff, operations, execute=True)
    assert result.operations_emitted == 0
    assert result.operations_applied == 4
    assert metadata_store.saved_snapshots[-1] == snapshot
    assert metadata_store.index_runs[-1]["stats"] == {
        "nodes": 1,
        "edges": 1,
        "added_nodes": 1,
        "changed_nodes": 0,
        "removed_nodes": 0,
        "added_edges": 1,
        "changed_edges": 0,
        "removed_edges": 0,
    }


def test_backend_sync_bridge_and_persistent_loop_cover_remaining_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_bridge_module = importlib.import_module("context_graph.backend.sync_bridge")
    driver = FakeAsyncDriver()
    custom_loop = asyncio.new_event_loop()
    driver._context_graph_loop = custom_loop
    bridge = BackendSyncBridge(driver)

    run_calls: list[object] = []

    def fake_run(coro, *, loop=None):
        run_calls.append(loop)
        return asyncio.run(coro)

    monkeypatch.setattr(sync_bridge_module, "_run_coroutine_sync", fake_run)

    assert bridge._driver_loop() is custom_loop
    bridge.health_check()
    bridge.build_indices(delete_existing=True)
    bridge.ensure_write_ready()
    bridge.ensure_write_ready()
    apply_result = bridge.apply(MutationPlan())
    repo_result = bridge.reset_repo("demo")
    scope_result = bridge.reset_scope(ScopeResetRequest(repo_id="demo"))
    bridge.close()

    assert all(loop is custom_loop for loop in run_calls)
    assert apply_result.operations_applied == 6
    assert repo_result.total_deleted == 4
    assert scope_result.total_deleted == 2
    assert ("build_indices", True) in driver.calls
    assert ("apply", MutationPlan()) in driver.calls
    assert ("reset_repo", "demo") in driver.calls
    assert ("reset_scope", "demo") in driver.calls
    assert "close" in driver.calls

    class FakeLoop:
        def is_running(self) -> bool:
            return True

    class InjectingLock:
        def __init__(self, owner: _PersistentLoop, loop: FakeLoop) -> None:
            self.owner = owner
            self.loop = loop

        def __enter__(self) -> None:
            self.owner._loop = self.loop

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    persistent_loop = _PersistentLoop()
    running_loop = FakeLoop()
    persistent_loop._lock = InjectingLock(persistent_loop, running_loop)
    assert persistent_loop._ensure_running() is running_loop

    class StoppedLoop:
        def is_running(self) -> bool:
            return False

    async def noop() -> None:
        return None

    coro = noop()
    try:
        with pytest.raises(RuntimeError, match="target event loop is not running"):
            persistent_loop.run(coro, loop=StoppedLoop())
    finally:
        coro.close()

    monkeypatch.setattr(sync_bridge_module, "_run_coroutine_sync", lambda coro, loop=None: (coro, loop))
    sentinel = object()
    assert sync_bridge_module.run_backend_coroutine_sync(sentinel, loop=running_loop) == (
        sentinel,
        running_loop,
    )
    custom_loop.close()


def test_graph_models_round_trip_and_apply_result_aliases() -> None:
    snapshot = _make_snapshot()
    node = snapshot.nodes[0]
    edge = snapshot.edges[0]
    diff = _make_diff(snapshot)
    empty_diff = SnapshotDiff(
        repo_id="demo",
        previous_revision_id="rev-0",
        new_revision_id="rev-1",
        added_nodes={},
        changed_nodes={},
        removed_nodes={},
        added_edges={},
        changed_edges={},
        removed_edges={},
    )

    assert node.properties_json() == '{"language":"python"}'
    assert CanonicalNode.from_dict(node.to_dict()) == node
    assert CanonicalEdge.from_dict(edge.to_dict()) == edge
    assert GraphSnapshot.from_dict(snapshot.to_dict()) == snapshot
    assert snapshot.node_map() == {node.node_id: node}
    assert snapshot.edge_map() == {edge.edge_id: edge}
    assert empty_diff.is_empty() is True
    assert diff.is_empty() is False

    apply_result = SnapshotApplyResult(
        snapshot=snapshot,
        diff=diff,
        operations_emitted=2,
        operations_applied=1,
    )
    assert apply_result.statements_emitted == 2
    assert apply_result.statements_executed == 1


def test_profile_vector_helpers_cover_blank_inputs_and_deactivations() -> None:
    vector_store = FakeVectorStore()
    embedding_provider = FakeEmbeddingProvider()
    indexer = profile_vector_services_module.ProfileVectorIndexer(
        vector_store=vector_store,
        embedding_provider=embedding_provider,
    )
    deactivation = ProfileDeactivation(
        repo_id="demo",
        profile_kind=SEMANTIC_CLAIM_PROFILE_KIND,
        relation_type=None,
        source_node_id=None,
        target_node_id=None,
        observed_at="2026-03-30T00:00:00Z",
    )
    assert indexer.apply_profile_updates(profiles=(), deactivations=(deactivation,)) == 0
    assert vector_store.bootstrap_calls == 1
    assert vector_store.deactivations == [(deactivation,)]
    assert vector_store.upserts == []

    blank_profile = ProfileRecord(
        profile_id="profile:1",
        repo_id="demo",
        profile_kind=SEMANTIC_CLAIM_PROFILE_KIND,
        profile_target_kind="Claim",
        profile_target_id="claim:1",
        relation_type=None,
        source_node_id=None,
        source_node_kind=None,
        target_node_id=None,
        target_node_kind=None,
        target_revision_id=None,
        value_text="A claim",
        keys=(
            ProfileKeyDefinition(text="   ", origin=PROFILE_KEY_ORIGIN_RAW_TEXT),
        ),
        updated_at="2026-03-30T00:00:00Z",
    )
    assert profile_vector_services_module.build_profile_key_records(
        (blank_profile,),
        embedding_provider,
    ) == ()
    assert profile_vector_services_module._dedupe_query_texts([" Alpha ", " ", "alpha"]) == ["Alpha"]
    assert embedding_provider.calls == []
