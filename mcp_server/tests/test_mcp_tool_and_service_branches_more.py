from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.query import code_graph as code_graph_queries

import src.app as app_module
from src.config.settings import MCPServerConfig
from src.provenance import neighborhood as neighborhood_module
from src.provenance import timeline as timeline_module
from src.runtime import backend as backend_module
from src.state import ServerState
from src.tools import common as common_tools
from src.tools import code_graph as code_graph_tool_module
from src.tools import provenance as provenance_tool_module


class FakeFastMCP:
    def __init__(self, name: str, *, instructions: str) -> None:
        self.name = name
        self.instructions = instructions
        self.tools: list[SimpleNamespace] = []
        self.routes: list[SimpleNamespace] = []
        self.settings = SimpleNamespace(host="0.0.0.0", port=8000, transport_security=None)

    def tool(self, name: str | None = None, **_kwargs):
        def decorator(fn):
            self.tools.append(SimpleNamespace(name=name or fn.__name__, fn=fn))
            return fn

        return decorator

    def custom_route(self, path: str, methods: list[str]):
        def decorator(fn):
            self.routes.append(SimpleNamespace(path=path, methods=tuple(methods), fn=fn))
            return fn

        return decorator


def make_snapshot(root_path: str) -> GraphSnapshot:
    repo_id = "demo"
    project = CanonicalNode("project:demo", repo_id, "Project", "demo", qualified_name="demo")
    app_file = CanonicalNode(
        "file:demo:app.py",
        repo_id,
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    main = CanonicalNode(
        "symbol:demo:demo.app.main",
        repo_id,
        "Function",
        "main",
        qualified_name="demo.app.main",
        file_path="app.py",
        start_line=1,
        end_line=2,
    )
    helper = CanonicalNode(
        "symbol:demo:demo.app.helper",
        repo_id,
        "Function",
        "helper",
        qualified_name="demo.app.helper",
        file_path="app.py",
        start_line=4,
        end_line=5,
    )
    return GraphSnapshot(
        repo_id=repo_id,
        revision_id="rev-1",
        root_path=root_path,
        generated_at="2026-03-30T00:00:00Z",
        nodes=(project, app_file, main, helper),
        edges=(
            CanonicalEdge("edge:contains", repo_id, "CONTAINS_FILE", project.node_id, app_file.node_id),
            CanonicalEdge("edge:defines", repo_id, "DEFINES", app_file.node_id, main.node_id),
            CanonicalEdge("edge:calls", repo_id, "CALLS", main.node_id, helper.node_id),
        ),
        metadata={},
    )


def test_build_app_imports_fastmcp_when_no_class_is_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_module = ModuleType("mcp")
    server_module = ModuleType("mcp.server")
    fastmcp_module = ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = FakeFastMCP
    server_module.fastmcp = fastmcp_module
    mcp_module.server = server_module
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    app = app_module.build_app()

    assert isinstance(app, FakeFastMCP)
    assert any(tool.name == "search_code" for tool in app.tools)
    assert any(route.path == "/health" for route in app.routes)


@pytest.mark.asyncio
async def test_common_helpers_and_backend_runtime_cover_remaining_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        common_tools._normalize_supported_value(
            None,
            parameter_name="direction",
            supported_values=("inbound", "outbound"),
        )
        is None
    )

    runtime = backend_module.MCPBackendRuntime(MCPServerConfig())
    runtime.driver = object()
    monkeypatch.setattr(runtime, "ensure_initialized", AsyncMock())
    assert await runtime.get_driver() is runtime.driver

    snapshot_service = SimpleNamespace(
        initialize_metadata_store=lambda: False,
        metadata_store=object(),
        metadata_init_error=None,
    )
    with pytest.raises(code_graph_queries.CodeGraphQueryError, match="No indexed snapshot found"):
        await common_tools._get_metadata_snapshot(
            ServerState(metadata_snapshot_service=snapshot_service),
            "demo",
        )


@pytest.mark.asyncio
async def test_code_graph_tool_handlers_cover_success_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ServerState(metadata_snapshot_service=object())

    monkeypatch.setattr(
        code_graph_tool_module,
        "_get_metadata_snapshot",
        AsyncMock(side_effect=code_graph_queries.CodeGraphQueryError("schema missing")),
    )
    assert await code_graph_tool_module.get_code_graph_schema(state, "demo") == {
        "error": "schema missing"
    }

    monkeypatch.setattr(code_graph_tool_module, "_get_metadata_snapshot", AsyncMock(return_value="snapshot"))

    def raise_schema_error(_snapshot):
        raise RuntimeError("schema exploded")

    monkeypatch.setattr(
        code_graph_tool_module.code_graph_queries,
        "get_code_graph_schema",
        raise_schema_error,
    )
    assert await code_graph_tool_module.get_code_graph_schema(state, "demo") == {
        "error": "Error in get_code_graph_schema: schema exploded"
    }

    monkeypatch.setattr(
        code_graph_tool_module.code_graph_queries,
        "search_code_graph",
        lambda snapshot, **kwargs: {"kind": "search", "snapshot": snapshot, **kwargs},
    )
    search_response = await code_graph_tool_module.search_code_graph(
        state,
        "demo",
        query="main",
        node_kinds=["Function"],
        file_pattern="*.py",
        limit=5,
        offset=1,
    )
    assert search_response["kind"] == "search"
    assert search_response["snapshot"] == "snapshot"

    def raise_search_error(snapshot, **kwargs):
        raise RuntimeError(f"search failed for {snapshot}")

    monkeypatch.setattr(
        code_graph_tool_module.code_graph_queries,
        "search_code_graph",
        raise_search_error,
    )
    assert await code_graph_tool_module.search_code_graph(state, "demo") == {
        "error": "Error in search_code_graph: search failed for snapshot"
    }

    monkeypatch.setattr(
        code_graph_tool_module,
        "_get_metadata_snapshot",
        AsyncMock(side_effect=code_graph_queries.CodeGraphQueryError("trace missing")),
    )
    assert await code_graph_tool_module.trace_call_path(state, "demo", "demo.app.main") == {
        "error": "trace missing"
    }

    monkeypatch.setattr(code_graph_tool_module, "_get_metadata_snapshot", AsyncMock(return_value="snapshot"))

    def raise_trace_error(snapshot, **kwargs):
        raise RuntimeError(f"trace failed for {snapshot}")

    monkeypatch.setattr(
        code_graph_tool_module.code_graph_queries,
        "trace_call_path",
        raise_trace_error,
    )
    assert await code_graph_tool_module.trace_call_path(state, "demo", "demo.app.main") == {
        "error": "Error in trace_call_path: trace failed for snapshot"
    }

    monkeypatch.setattr(
        code_graph_tool_module,
        "_get_metadata_snapshot",
        AsyncMock(side_effect=code_graph_queries.CodeGraphQueryError("snippet missing")),
    )
    assert await code_graph_tool_module.get_code_snippet(state, "demo", "demo.app.main") == {
        "error": "snippet missing"
    }

    monkeypatch.setattr(code_graph_tool_module, "_get_metadata_snapshot", AsyncMock(return_value="snapshot"))

    def raise_snippet_error(snapshot, **kwargs):
        raise RuntimeError(f"snippet failed for {snapshot}")

    monkeypatch.setattr(
        code_graph_tool_module.code_graph_queries,
        "get_code_snippet",
        raise_snippet_error,
    )
    assert await code_graph_tool_module.get_code_snippet(state, "demo", "demo.app.main") == {
        "error": "Error in get_code_snippet: snippet failed for snapshot"
    }


@pytest.mark.asyncio
async def test_provenance_tool_handlers_cover_success_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(get_driver=AsyncMock(return_value="driver"))
    snapshot_service = SimpleNamespace(search_profiles_sync="semantic-search")
    state = ServerState(
        config=SimpleNamespace(namespace=SimpleNamespace(group_id="main")),
        metadata_snapshot_service=snapshot_service,
    )

    invalid_limit = await provenance_tool_module.search_context(state, "demo", "jwt", limit=0)
    assert invalid_limit == {"error": "limit must be a positive integer"}

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_backend_runtime",
        AsyncMock(return_value=runtime),
    )
    search_context_async = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(provenance_tool_module, "run_search_context_async", search_context_async)

    response = await provenance_tool_module.search_context(state, "demo", "jwt")
    assert response == {"status": "ok"}
    assert search_context_async.await_args.kwargs["group_id"] == "main"
    assert search_context_async.await_args.kwargs["sources"] == list(
        provenance_tool_module.SEARCH_CONTEXT_SOURCES
    )
    assert search_context_async.await_args.kwargs["semantic_search"] == "semantic-search"

    monkeypatch.setattr(
        provenance_tool_module,
        "run_search_context_async",
        AsyncMock(side_effect=ValueError("bad search")),
    )
    assert await provenance_tool_module.search_context(state, "demo", "jwt") == {
        "error": "bad search"
    }

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_backend_runtime",
        AsyncMock(side_effect=RuntimeError("runtime offline")),
    )
    assert await provenance_tool_module.search_context(state, "demo", "jwt") == {
        "error": "Error in search_context: runtime offline"
    }

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_backend_runtime",
        AsyncMock(return_value=runtime),
    )
    assert await provenance_tool_module.get_node_neighborhood(
        state,
        "symbol:demo:demo.app.main",
        hops=0,
    ) == {"error": "hops must be a positive integer"}

    neighborhood_async = AsyncMock(return_value={"kind": "neighborhood"})
    monkeypatch.setattr(
        provenance_tool_module,
        "_get_metadata_snapshot",
        AsyncMock(side_effect=code_graph_queries.CodeGraphQueryError("missing snapshot")),
    )
    monkeypatch.setattr(
        provenance_tool_module,
        "get_node_neighborhood_async",
        neighborhood_async,
    )
    neighborhood = await provenance_tool_module.get_node_neighborhood(
        state,
        "symbol:demo:demo.app.main",
        repo_id="demo",
        hops=1,
    )
    assert neighborhood == {"kind": "neighborhood"}
    assert neighborhood_async.await_args.kwargs["service"].snapshot is None

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_backend_runtime",
        AsyncMock(side_effect=RuntimeError("driver offline")),
    )
    assert await provenance_tool_module.get_node_neighborhood(
        state,
        "symbol:demo:demo.app.main",
        repo_id="demo",
    ) == {"error": "Error in get_node_neighborhood: driver offline"}

    assert await provenance_tool_module.get_timeline(state, "claim:1", limit=0) == {
        "error": "limit must be a positive integer"
    }

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_backend_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        provenance_tool_module,
        "get_timeline_async",
        AsyncMock(return_value={"kind": "timeline"}),
    )
    assert await provenance_tool_module.get_timeline(state, "claim:1", repo_id="demo") == {
        "kind": "timeline"
    }

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_backend_runtime",
        AsyncMock(side_effect=RuntimeError("timeline backend offline")),
    )
    assert await provenance_tool_module.get_timeline(state, "claim:1") == {
        "error": "Error in get_timeline: timeline backend offline"
    }

    assert await provenance_tool_module.get_code_context(
        state,
        "demo",
        "demo.app.main",
        max_related_symbols=0,
    ) == {"error": "max_related_symbols must be a positive integer"}

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_metadata_snapshot",
        AsyncMock(return_value="snapshot"),
    )
    monkeypatch.setattr(
        provenance_tool_module,
        "build_code_context",
        lambda **kwargs: {"kind": "code-context", **kwargs},
    )
    code_context = await provenance_tool_module.get_code_context(
        state,
        "demo",
        "demo.app.main",
        target_kind="function",
        include_neighbors=False,
        include_references=False,
        max_related_symbols=2,
    )
    assert code_context["kind"] == "code-context"
    assert code_context["target_kind"] == "function"

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_metadata_snapshot",
        AsyncMock(side_effect=code_graph_queries.CodeGraphQueryError("code context missing")),
    )
    assert await provenance_tool_module.get_code_context(state, "demo", "demo.app.main") == {
        "error": "code context missing"
    }

    monkeypatch.setattr(
        provenance_tool_module,
        "_get_metadata_snapshot",
        AsyncMock(return_value="snapshot"),
    )

    def raise_context_error(**kwargs):
        raise RuntimeError("context exploded")

    monkeypatch.setattr(provenance_tool_module, "build_code_context", raise_context_error)
    assert await provenance_tool_module.get_code_context(state, "demo", "demo.app.main") == {
        "error": "Error in get_code_context: context exploded"
    }


@pytest.mark.asyncio
async def test_neighborhood_and_timeline_services_cover_guard_branches(
    tmp_path,
) -> None:
    snapshot = make_snapshot(str(tmp_path))

    service = neighborhood_module.NeighborhoodQueryService(driver=object(), snapshot=snapshot)
    filtered = service._get_snapshot_neighborhood(
        repo_id="demo",
        node_id="symbol:demo:demo.app.main",
        hops=1,
        edge_types=["CALLS"],
    )
    assert all(edge["edge_type"] == "CALLS" for edge in filtered["edges"])

    with pytest.raises(ValueError, match="exact node_id"):
        await neighborhood_module.NeighborhoodQueryService(driver=object()).get_node_neighborhood_async(
            repo_id="demo",
            node_id=None,
            hops=1,
            edge_types=None,
        )
    with pytest.raises(ValueError, match="positive integer"):
        await neighborhood_module.NeighborhoodQueryService(driver=object()).get_node_neighborhood_async(
            repo_id="demo",
            node_id="claim:1",
            hops=0,
            edge_types=None,
        )
    with pytest.raises(RuntimeError, match="backend driver"):
        await neighborhood_module.NeighborhoodQueryService().get_node_neighborhood_async(
            repo_id="demo",
            node_id="claim:1",
            hops=1,
            edge_types=None,
        )
    with pytest.raises(ValueError, match="indexed snapshot"):
        await neighborhood_module.NeighborhoodQueryService(driver=object()).get_node_neighborhood_async(
            repo_id="demo",
            node_id="symbol:demo:demo.app.main",
            hops=1,
            edge_types=None,
        )
    with pytest.raises(ValueError, match="snapshot is required"):
        neighborhood_module.NeighborhoodQueryService(driver=object())._get_snapshot_neighborhood(
            repo_id="demo",
            node_id="symbol:demo:demo.app.main",
            hops=1,
            edge_types=None,
        )
    with pytest.raises(ValueError, match="No indexed code node found"):
        service._get_snapshot_neighborhood(
            repo_id="demo",
            node_id="symbol:demo:missing",
            hops=1,
            edge_types=None,
        )

    wrapped_neighborhood_service = SimpleNamespace(
        get_node_neighborhood_async=AsyncMock(return_value={"status": "ok"})
    )
    assert await neighborhood_module.get_node_neighborhood_async(
        service=wrapped_neighborhood_service,
        repo_id="demo",
        node_id="claim:1",
        hops=1,
        edge_types=None,
    ) == {"status": "ok"}

    with pytest.raises(ValueError, match="positive integer"):
        await timeline_module.TimelineQueryService(driver=object()).get_timeline_async(
            repo_id="demo",
            target_id="claim:1",
            limit=0,
        )
    with pytest.raises(RuntimeError, match="backend driver"):
        await timeline_module.TimelineQueryService().get_timeline_async(
            repo_id="demo",
            target_id="claim:1",
            limit=5,
        )

    wrapped_timeline_service = SimpleNamespace(get_timeline_async=AsyncMock(return_value={"status": "ok"}))
    assert await timeline_module.get_timeline_async(
        service=wrapped_timeline_service,
        repo_id="demo",
        target_id="claim:1",
        limit=5,
    ) == {"status": "ok"}
