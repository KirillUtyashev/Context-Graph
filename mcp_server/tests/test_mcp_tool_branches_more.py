from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from context_graph.models import CanonicalNode, GraphSnapshot
from context_graph.query import code_graph as code_graph_queries
from src.state import ServerState
from src.tools import code_graph as code_graph_tools
from src.tools import observations as observation_tools
from src.tools import provenance as provenance_tool_module
from src.tools import search_code as search_code_module


class FakeFastMCP:
    def __init__(self) -> None:
        self.tools: list[SimpleNamespace] = []

    def tool(self, name: str | None = None, **_kwargs):
        def decorator(fn):
            self.tools.append(SimpleNamespace(name=name or fn.__name__, fn=fn))
            return fn

        return decorator


def make_snapshot(root_path: str) -> GraphSnapshot:
    node = CanonicalNode(
        "file:demo:app",
        "demo",
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    return GraphSnapshot(
        repo_id="demo",
        revision_id="rev-1",
        root_path=root_path,
        generated_at="2026-03-30T00:00:00Z",
        nodes=(node,),
        edges=(),
        metadata={},
    )


class FakeObservationMutationPlanner:
    pass


class FakeClaimBundleValidator:
    pass


class FakeEvidenceMutationPlanner:
    pass


class FakeClaimEvidenceExtractionService:
    def __init__(self, *, llm_client) -> None:
        self.llm_client = llm_client


class FakeClaimProfileKeyGenerationService:
    def __init__(self, *, llm_client) -> None:
        self.llm_client = llm_client


class FakeClaimResolutionService:
    def __init__(self, *, llm_client) -> None:
        self.llm_client = llm_client


class FakeFactConflictResolutionService:
    def __init__(self, *, llm_client) -> None:
        self.llm_client = llm_client


class FakeClaimSemanticRetrievalService:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FakeClaimResolutionCandidateRetriever:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FakeEvidenceInvalidationPlanner:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FakeObservationIngestionItem:
    def __init__(self, *, planned_observation, source_context) -> None:
        self.planned_observation = planned_observation
        self.source_context = source_context


class FakeObservationIngestionService:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def apply_ingestion(self, *, items, snapshot, execute):
        self.items = items
        self.snapshot = snapshot
        self.execute = execute
        return SimpleNamespace(result=SimpleNamespace(claim_ids=["claim:1", "claim:2"]))


class FakeAgentObservationPayload:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeAgentObservationAdapter:
    def build_observation(self, *, planner, payload):
        return SimpleNamespace(
            observation=SimpleNamespace(
                observation_id="observation:1",
                metadata={"document_path": "notes/demo.md"},
            )
        )


def _tool_map(app: FakeFastMCP) -> dict[str, object]:
    return {tool.name: tool.fn for tool in app.tools}


@pytest.mark.asyncio
async def test_search_code_finds_matches_and_handles_error_branches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_file = tmp_path / "app.py"
    app_file.write_text("Alpha\nBeta\ngamma\n", encoding="utf-8")
    snapshot = make_snapshot(str(tmp_path))
    state = ServerState()
    monkeypatch.setattr(search_code_module, "_get_metadata_snapshot", AsyncMock(return_value=snapshot))

    response = await search_code_module.search_code(state, "demo", "beta", limit=5)
    assert response["matches"] == [
        {"file_path": "app.py", "line_number": 2, "line_text": "Beta"}
    ]

    regex_response = await search_code_module.search_code(
        state,
        "demo",
        "^g",
        file_pattern="*.py",
        limit=5,
        regex=True,
    )
    assert regex_response["matches"][0]["line_number"] == 3

    no_match_response = await search_code_module.search_code(state, "demo", "delta", limit=5)
    assert no_match_response["matches"] == []

    invalid_limit = await search_code_module.search_code(state, "demo", "beta", limit=0)
    assert invalid_limit == {"error": "limit must be a positive integer"}

    invalid_regex = await search_code_module.search_code(state, "demo", "[", regex=True)
    assert invalid_regex["error"].startswith("Invalid regex:")

    monkeypatch.setattr(
        search_code_module,
        "_get_metadata_snapshot",
        AsyncMock(side_effect=code_graph_queries.CodeGraphQueryError("snapshot missing")),
    )
    query_error = await search_code_module.search_code(state, "demo", "beta")
    assert query_error == {"error": "snapshot missing"}

    monkeypatch.setattr(
        search_code_module,
        "_get_metadata_snapshot",
        AsyncMock(side_effect=RuntimeError("unexpected")),
    )
    unexpected = await search_code_module.search_code(state, "demo", "beta")
    assert unexpected == {"error": "Error in search_code: unexpected"}


def test_iter_file_matches_handles_limits_missing_files_and_read_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_snapshot = make_snapshot(str(tmp_path))
    assert (
        search_code_module._iter_file_matches(
            snapshot=missing_snapshot,
            query="anything",
            file_pattern=None,
            limit=0,
            regex=False,
        )
        == []
    )

    existing = tmp_path / "app.py"
    existing.write_text("Alpha\nBeta\n", encoding="utf-8")
    snapshot = make_snapshot(str(tmp_path))
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: (_ for _ in ()).throw(OSError("no read")))
    assert (
        search_code_module._iter_file_matches(
            snapshot=snapshot,
            query="alpha",
            file_pattern="*.py",
            limit=5,
            regex=False,
        )
        == []
    )


@pytest.mark.asyncio
async def test_tool_registration_wrappers_delegate_to_underlying_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ServerState()

    search_app = FakeFastMCP()
    search_code_module.register_search_code_tools(search_app, state)
    monkeypatch.setattr(search_code_module, "search_code", AsyncMock(return_value={"status": "ok"}))
    assert await _tool_map(search_app)["search_code"]("demo", "jwt") == {"status": "ok"}
    search_code_module.search_code.assert_awaited_once_with(
        state,
        "demo",
        "jwt",
        file_pattern=None,
        limit=20,
        regex=False,
    )

    code_app = FakeFastMCP()
    code_graph_tools.register_code_graph_tools(code_app, state)
    monkeypatch.setattr(code_graph_tools, "get_code_graph_schema", AsyncMock(return_value={"kind": "schema"}))
    monkeypatch.setattr(code_graph_tools, "search_code_graph", AsyncMock(return_value={"kind": "search"}))
    monkeypatch.setattr(code_graph_tools, "trace_call_path", AsyncMock(return_value={"kind": "trace"}))
    monkeypatch.setattr(code_graph_tools, "get_code_snippet", AsyncMock(return_value={"kind": "snippet"}))
    tool_map = _tool_map(code_app)
    assert await tool_map["get_code_graph_schema"]("demo") == {"kind": "schema"}
    assert await tool_map["search_code_graph"]("demo", query="main") == {"kind": "search"}
    assert await tool_map["trace_call_path"]("demo", "pkg.main") == {"kind": "trace"}
    assert await tool_map["get_code_snippet"]("demo", "pkg.main") == {"kind": "snippet"}

    provenance_app = FakeFastMCP()
    provenance_tool_module.register_provenance_tools(provenance_app, state)
    monkeypatch.setattr(provenance_tool_module, "search_context", AsyncMock(return_value={"kind": "context"}))
    monkeypatch.setattr(
        provenance_tool_module,
        "get_node_neighborhood",
        AsyncMock(return_value={"kind": "neighborhood"}),
    )
    monkeypatch.setattr(provenance_tool_module, "get_timeline", AsyncMock(return_value={"kind": "timeline"}))
    monkeypatch.setattr(
        provenance_tool_module,
        "get_code_context",
        AsyncMock(return_value={"kind": "code-context"}),
    )
    tool_map = _tool_map(provenance_app)
    assert await tool_map["search_context"]("demo", "jwt") == {"kind": "context"}
    assert await tool_map["get_node_neighborhood"]("node-1") == {"kind": "neighborhood"}
    assert await tool_map["get_timeline"]("claim:1") == {"kind": "timeline"}
    assert await tool_map["get_code_context"]("demo", "pkg.main") == {"kind": "code-context"}

    observation_app = FakeFastMCP()
    observation_tools.register_observation_tools(observation_app, state)
    monkeypatch.setattr(
        observation_tools,
        "record_agent_observation",
        AsyncMock(return_value={"kind": "observation"}),
    )
    assert await _tool_map(observation_app)["record_agent_observation"]("demo", "note") == {
        "kind": "observation"
    }


@pytest.mark.asyncio
async def test_record_agent_observation_covers_validation_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ServerState(
        config=SimpleNamespace(namespace=SimpleNamespace(group_id="main")),
        backend_runtime=object(),
        metadata_snapshot_service=object(),
    )
    empty = await observation_tools.record_agent_observation(state, "demo", "   ")
    assert empty == {"error": "observation_text must not be empty"}

    monkeypatch.setattr(
        observation_tools,
        "_persist_agent_observation_request",
        AsyncMock(side_effect=RuntimeError("persist failed")),
    )
    errored = await observation_tools.record_agent_observation(state, "demo", "hello")
    assert errored == {"error": "Error in record_agent_observation: persist failed"}


@pytest.mark.asyncio
async def test_persist_agent_observation_request_builds_and_applies_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_store = SimpleNamespace(load_latest_snapshot=lambda repo_id: {"repo_id": repo_id})
    snapshot_service = SimpleNamespace(
        initialize_metadata_store=lambda: True,
        metadata_store=metadata_store,
        metadata_init_error=None,
        initialize_profile_search=lambda: True,
        profile_indexer=SimpleNamespace(vector_store="vector-store", embedding_provider="embedder"),
    )
    runtime = SimpleNamespace(
        llm_client="llm",
        driver="driver",
        claim_resolution_service=None,
    )
    state = ServerState(
        config=SimpleNamespace(namespace=SimpleNamespace(group_id="main")),
        backend_runtime=runtime,
        metadata_snapshot_service=snapshot_service,
    )
    request = observation_tools.ObservationWriteRequest(
        repo_id="demo",
        group_id="main",
        observation_text="Investigated auth regression.",
        observation_kind="agent_note",
        title="Auth regression",
        source_kind="agent",
        source_id=None,
        context_kind=None,
        context_id="task-7",
        observed_at="2026-03-30T00:00:00Z",
        task_id="task-7",
        metadata={"severity": "high"},
    )

    monkeypatch.setattr(observation_tools, "_get_backend_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr("context_graph.provenance.ClaimBundleValidator", FakeClaimBundleValidator)
    monkeypatch.setattr(
        "context_graph.provenance.ClaimEvidenceExtractionService",
        FakeClaimEvidenceExtractionService,
    )
    monkeypatch.setattr(
        "context_graph.provenance.ClaimProfileKeyGenerationService",
        FakeClaimProfileKeyGenerationService,
    )
    monkeypatch.setattr(
        "context_graph.provenance.ClaimResolutionCandidateRetriever",
        FakeClaimResolutionCandidateRetriever,
    )
    monkeypatch.setattr("context_graph.provenance.ClaimResolutionService", FakeClaimResolutionService)
    monkeypatch.setattr(
        "context_graph.provenance.ClaimSemanticRetrievalService",
        FakeClaimSemanticRetrievalService,
    )
    monkeypatch.setattr(
        "context_graph.provenance.EvidenceInvalidationPlanner",
        FakeEvidenceInvalidationPlanner,
    )
    monkeypatch.setattr("context_graph.provenance.EvidenceMutationPlanner", FakeEvidenceMutationPlanner)
    monkeypatch.setattr(
        "context_graph.provenance.FactConflictResolutionService",
        FakeFactConflictResolutionService,
    )
    monkeypatch.setattr("context_graph.provenance.ObservationMutationPlanner", FakeObservationMutationPlanner)
    monkeypatch.setattr("context_graph.query.parser.QueryParser", lambda *, llm_client: ("parser", llm_client))
    monkeypatch.setattr(
        "context_graph.provenance.observation_ingestion.ObservationIngestionItem",
        FakeObservationIngestionItem,
    )
    monkeypatch.setattr(
        "context_graph.provenance.observation_ingestion.ObservationIngestionService",
        FakeObservationIngestionService,
    )
    monkeypatch.setattr(
        "context_graph.provenance.source_adapters.AgentObservationAdapter",
        FakeAgentObservationAdapter,
    )
    monkeypatch.setattr(
        "context_graph.provenance.source_adapters.AgentObservationPayload",
        FakeAgentObservationPayload,
    )

    response = await observation_tools._persist_agent_observation_request(state, request)
    assert response["observation_id"] == "observation:1"
    assert response["claim_ids"] == ["claim:1", "claim:2"]


@pytest.mark.asyncio
async def test_persist_agent_observation_request_handles_missing_metadata_and_optional_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_service = SimpleNamespace(
        initialize_metadata_store=lambda: False,
        metadata_store=None,
        metadata_init_error="metadata unavailable",
        initialize_profile_search=lambda: True,
        profile_indexer=None,
    )
    runtime = SimpleNamespace(llm_client=None, driver="driver", claim_resolution_service=object())
    state = ServerState(
        config=SimpleNamespace(namespace=SimpleNamespace(group_id="main")),
        backend_runtime=runtime,
        metadata_snapshot_service=snapshot_service,
    )
    request = observation_tools.ObservationWriteRequest(
        repo_id="demo",
        group_id="main",
        observation_text="Investigated auth regression.",
        observation_kind="agent_note",
        title=None,
        source_kind="agent",
        source_id=None,
        context_kind=None,
        context_id=None,
        observed_at="2026-03-30T00:00:00Z",
        task_id=None,
        metadata={},
    )

    monkeypatch.setattr(observation_tools, "_get_backend_runtime", AsyncMock(return_value=runtime))
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        await observation_tools._persist_agent_observation_request(state, request)
