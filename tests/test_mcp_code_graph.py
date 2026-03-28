from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_graph.metadata import InMemoryMetadataStore
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.server.mcp import code_graph as code_graph_queries
from context_graph.server.mcp import server as server_module


def make_code_graph_snapshot(root_path: str) -> GraphSnapshot:
    repo_id = "demo"
    project = CanonicalNode(
        "project:demo",
        repo_id,
        "Project",
        "demo",
        qualified_name="demo",
    )
    app_file = CanonicalNode(
        "file:demo:app",
        repo_id,
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    caller_file = CanonicalNode(
        "file:demo:caller",
        repo_id,
        "File",
        "caller.py",
        qualified_name="demo.caller.__file__",
        file_path="caller.py",
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
    helper_extra = CanonicalNode(
        "symbol:demo:demo.app.helper_extra",
        repo_id,
        "Function",
        "helper_extra",
        qualified_name="demo.app.helper_extra",
        file_path="app.py",
        start_line=7,
        end_line=8,
    )
    caller = CanonicalNode(
        "symbol:demo:demo.caller.run",
        repo_id,
        "Function",
        "run",
        qualified_name="demo.caller.run",
        file_path="caller.py",
        start_line=1,
        end_line=2,
    )
    edges = (
        CanonicalEdge("edge:project-app", repo_id, "CONTAINS_FILE", project.node_id, app_file.node_id),
        CanonicalEdge("edge:project-caller", repo_id, "CONTAINS_FILE", project.node_id, caller_file.node_id),
        CanonicalEdge("edge:defines-main", repo_id, "DEFINES", app_file.node_id, main.node_id),
        CanonicalEdge("edge:defines-helper", repo_id, "DEFINES", app_file.node_id, helper.node_id),
        CanonicalEdge("edge:defines-helper-extra", repo_id, "DEFINES", app_file.node_id, helper_extra.node_id),
        CanonicalEdge("edge:defines-caller", repo_id, "DEFINES", caller_file.node_id, caller.node_id),
        CanonicalEdge("edge:calls-main-helper", repo_id, "CALLS", main.node_id, helper.node_id),
        CanonicalEdge("edge:calls-main-helper-extra", repo_id, "CALLS", main.node_id, helper_extra.node_id),
        CanonicalEdge("edge:calls-caller-helper", repo_id, "CALLS", caller.node_id, helper.node_id),
    )
    return GraphSnapshot(
        repo_id=repo_id,
        revision_id="rev-123",
        root_path=root_path,
        generated_at="2026-03-27T00:00:00+00:00",
        nodes=(project, app_file, caller_file, main, helper, helper_extra, caller),
        edges=edges,
        metadata={},
    )


class FakeAuroraService:
    def __init__(self, metadata_store: InMemoryMetadataStore, *, ready: bool = True) -> None:
        self._metadata = metadata_store
        self._ready = ready
        self._metadata_init_error: str | None = None

    def initialize(self) -> bool:
        return self._ready

    def initialize_metadata_store(self) -> bool:
        return self._ready


class CodeGraphQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "app.py").write_text(
            "def main():\n"
            "    return helper()\n\n"
            "def helper():\n"
            "    return 1\n\n"
            "def helper_extra():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        (root / "caller.py").write_text(
            "def run():\n"
            "    return helper()\n",
            encoding="utf-8",
        )
        self.snapshot = make_code_graph_snapshot(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_code_graph_schema_is_deterministic(self) -> None:
        response = code_graph_queries.get_code_graph_schema(self.snapshot)

        self.assertEqual(response["repo_id"], "demo")
        self.assertEqual(response["revision_id"], "rev-123")
        self.assertEqual(response["node_type_counts"][0], {"node_kind": "File", "count": 2})
        self.assertEqual(response["edge_type_counts"][0], {"edge_type": "CALLS", "count": 3})
        self.assertIn(
            {
                "source_kind": "Function",
                "edge_type": "CALLS",
                "target_kind": "Function",
                "count": 3,
            },
            response["relationship_patterns"],
        )

    def test_search_code_graph_applies_rank_and_filters(self) -> None:
        response = code_graph_queries.search_code_graph(
            self.snapshot,
            query="helper",
            node_kinds=["Function"],
            file_pattern="app.py",
            limit=10,
            offset=0,
        )

        self.assertEqual(response["total"], 2)
        self.assertEqual(
            [match["qualified_name"] for match in response["matches"]],
            ["demo.app.helper", "demo.app.helper_extra"],
        )

    def test_search_code_graph_supports_pagination(self) -> None:
        response = code_graph_queries.search_code_graph(
            self.snapshot,
            node_kinds=["Function"],
            limit=1,
            offset=1,
        )

        self.assertEqual(response["total"], 4)
        self.assertEqual(len(response["matches"]), 1)

    def test_get_code_snippet_reads_exact_qualified_name(self) -> None:
        response = code_graph_queries.get_code_snippet(
            self.snapshot,
            qualified_name="demo.app.helper",
            include_neighbors=False,
        )

        self.assertEqual(response["node"]["qualified_name"], "demo.app.helper")
        self.assertEqual(response["file_path"], "app.py")
        self.assertEqual(response["snippet"], "def helper():\n    return 1")

    def test_get_code_snippet_errors_for_missing_symbol(self) -> None:
        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.get_code_snippet(
                self.snapshot,
                qualified_name="demo.app.missing",
            )

    def test_trace_call_path_returns_deterministic_bfs_steps(self) -> None:
        response = code_graph_queries.trace_call_path(
            self.snapshot,
            qualified_name="demo.app.main",
            direction="outbound",
            depth=1,
        )

        self.assertEqual(
            [(step["target_qualified_name"], step["depth"]) for step in response["steps"]],
            [("demo.app.helper", 1), ("demo.app.helper_extra", 1)],
        )

    def test_trace_call_path_validates_depth(self) -> None:
        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.trace_call_path(
                self.snapshot,
                qualified_name="demo.app.main",
                depth=0,
            )


class McpCodeGraphWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "app.py").write_text(
            "def main():\n"
            "    return helper()\n\n"
            "def helper():\n"
            "    return 1\n",
            encoding="utf-8",
        )
        (root / "caller.py").write_text(
            "def run():\n"
            "    return helper()\n",
            encoding="utf-8",
        )
        self.snapshot = make_code_graph_snapshot(self.temp_dir.name)
        self.metadata_store = InMemoryMetadataStore()
        self.metadata_store.bootstrap()
        self.metadata_store.save_latest_snapshot(self.snapshot)
        self.fake_service = FakeAuroraService(self.metadata_store)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_get_code_graph_schema_wrapper_returns_structured_payload(self) -> None:
        with patch.object(server_module, "aurora_ingestion_service", self.fake_service):
            response = await server_module.get_code_graph_schema("demo")

        self.assertEqual(response["repo_id"], "demo")
        self.assertEqual(response["revision_id"], "rev-123")
        self.assertNotIn("message", response)

    async def test_search_code_graph_wrapper_returns_error_when_snapshot_missing(self) -> None:
        empty_store = InMemoryMetadataStore()
        empty_store.bootstrap()
        with patch.object(server_module, "aurora_ingestion_service", FakeAuroraService(empty_store)):
            response = await server_module.search_code_graph("missing")

        self.assertEqual(
            response,
            {"error": "No indexed snapshot found for repo_id=missing"},
        )

    async def test_get_code_snippet_wrapper_returns_structured_payload(self) -> None:
        with patch.object(server_module, "aurora_ingestion_service", self.fake_service):
            response = await server_module.get_code_snippet("demo", "demo.app.main")

        self.assertEqual(response["repo_id"], "demo")
        self.assertEqual(response["node"]["qualified_name"], "demo.app.main")
        self.assertNotIn("message", response)

    async def test_trace_call_path_wrapper_rejects_invalid_direction(self) -> None:
        with patch.object(server_module, "aurora_ingestion_service", self.fake_service):
            response = await server_module.trace_call_path(
                "demo",
                "demo.app.main",
                direction="sideways",
            )

        self.assertEqual(
            response,
            {"error": "direction must be one of: inbound, outbound, both"},
        )

    async def test_wrapper_uses_metadata_only_initialization(self) -> None:
        class MetadataOnlyAuroraService(FakeAuroraService):
            def initialize(self) -> bool:
                raise AssertionError("full Aurora initialization should not run for snapshot-backed MCP tools")

        with patch.object(
            server_module,
            "aurora_ingestion_service",
            MetadataOnlyAuroraService(self.metadata_store),
        ):
            response = await server_module.get_code_graph_schema("demo")

        self.assertEqual(response["revision_id"], "rev-123")
