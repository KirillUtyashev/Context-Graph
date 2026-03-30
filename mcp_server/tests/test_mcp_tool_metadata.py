from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints
import unittest

import src.app as app_module
from src.state import ServerState
from src.tools import provenance as provenance_tool_module


@dataclass
class FakeTool:
    name: str
    fn: Any
    description: str
    annotations: dict[str, Any]


class FakeFastMCP:
    def __init__(self, name: str, *, instructions: str) -> None:
        self.name = name
        self.instructions = instructions
        self.tools: list[FakeTool] = []
        self.routes: list[SimpleNamespace] = []
        self.settings = SimpleNamespace(host="0.0.0.0", port=8000, transport_security=None)

    def tool(self, name: str | None = None, **_kwargs):
        def decorator(fn):
            self.tools.append(
                FakeTool(
                    name=name or fn.__name__,
                    fn=fn,
                    description=(fn.__doc__ or "").strip(),
                    annotations=get_type_hints(fn, include_extras=True),
                )
            )
            return fn

        return decorator

    def custom_route(self, path: str, methods: list[str]):
        def decorator(fn):
            self.routes.append(SimpleNamespace(path=path, methods=tuple(methods), fn=fn))
            return fn

        return decorator


def _build_fake_app() -> FakeFastMCP:
    return app_module.build_app(fastmcp_cls=FakeFastMCP)


def _tool_map(app: FakeFastMCP) -> dict[str, FakeTool]:
    return {tool.name: tool for tool in app.tools}


def _collect_enums(annotation: Any) -> list[list[str]]:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _collect_enums(get_args(annotation)[0])
    if origin in {list, tuple, set, frozenset}:
        enums: list[list[str]] = []
        for arg in get_args(annotation):
            enums.extend(_collect_enums(arg))
        return enums
    if origin in {Union, getattr(__import__("types"), "UnionType", Union)}:
        enums: list[list[str]] = []
        for arg in get_args(annotation):
            enums.extend(_collect_enums(arg))
        return enums
    if origin is Literal:
        return [[str(value) for value in get_args(annotation)]]
    return []


class McpToolMetadataTests(unittest.TestCase):
    def test_build_app_registers_expected_tools_and_health_route(self) -> None:
        app = _build_fake_app()

        self.assertEqual(
            {tool.name for tool in app.tools},
            {
                "search_code",
                "get_code_graph_schema",
                "search_code_graph",
                "trace_call_path",
                "get_code_snippet",
                "search_context",
                "get_node_neighborhood",
                "get_timeline",
                "get_code_context",
                "record_agent_observation",
            },
        )
        self.assertEqual([(route.path, route.methods) for route in app.routes], [("/health", ("GET",))])

    def test_exported_tools_have_non_empty_descriptions(self) -> None:
        app = _build_fake_app()
        self.assertGreaterEqual(len(app.tools), 10)
        for tool in app.tools:
            self.assertTrue(tool.description)

    def test_annotations_expose_supported_enums(self) -> None:
        tools = _tool_map(_build_fake_app())

        trace_enums = _collect_enums(tools["trace_call_path"].annotations["direction"])
        self.assertIn(["inbound", "outbound", "both"], trace_enums)

        source_enums = _collect_enums(tools["search_context"].annotations["sources"])
        self.assertIn(["claims", "observations", "relations", "code"], source_enums)

        target_kind_enums = _collect_enums(tools["get_code_context"].annotations["target_kind"])
        self.assertIn(["symbol", "function", "class", "module", "file"], target_kind_enums)

    def test_get_node_neighborhood_signature_requires_node_id(self) -> None:
        tool = _tool_map(_build_fake_app())["get_node_neighborhood"]
        self.assertIn("node_id", tool.annotations)
        self.assertNotIn("concept", tool.annotations)

    def test_instructions_include_tool_selection_and_identifier_examples(self) -> None:
        instructions = app_module.CONTEXT_GRAPH_MCP_INSTRUCTIONS

        self.assertIn("Use search_code when", instructions)
        self.assertIn("Use search_context when", instructions)
        self.assertIn("Use get_node_neighborhood when", instructions)
        self.assertIn("repo_id", instructions)
        self.assertIn("qualified_name", instructions)
        self.assertIn("node_id", instructions)
        self.assertIn("target_id", instructions)
        self.assertIn("If an exact id is unknown", instructions)


class McpToolValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_context_rejects_invalid_sources_with_supported_values(self) -> None:
        response = await provenance_tool_module.search_context(
            ServerState(),
            "demo",
            "jwt audience validation",
            sources=["claims", "not-a-source"],
        )

        self.assertEqual(
            response,
            {"error": "sources must only contain: claims, observations, relations, code"},
        )

    async def test_get_code_context_rejects_invalid_target_kind_with_supported_values(self) -> None:
        response = await provenance_tool_module.get_code_context(
            ServerState(),
            "demo",
            "AuthService",
            target_kind="widget",
        )

        self.assertEqual(
            response,
            {"error": "target_kind must be one of: symbol, function, class, module, file"},
        )
