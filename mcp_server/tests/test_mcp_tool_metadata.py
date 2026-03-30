from __future__ import annotations

import unittest

from context_graph_mcp_server import server as server_module


def _tool_map():
    return {tool.name: tool for tool in server_module.mcp._tool_manager.list_tools()}


def _collect_enums(payload):
    enums: list[list[str]] = []
    if isinstance(payload, dict):
        enum = payload.get("enum")
        if isinstance(enum, list):
            enums.append(enum)
        for value in payload.values():
            enums.extend(_collect_enums(value))
    elif isinstance(payload, list):
        for value in payload:
            enums.extend(_collect_enums(value))
    return enums


class McpToolMetadataTests(unittest.TestCase):
    def test_exported_tools_have_non_empty_descriptions(self) -> None:
        tools = server_module.mcp._tool_manager.list_tools()
        self.assertGreaterEqual(len(tools), 10)
        for tool in tools:
            self.assertTrue(tool.description)
            self.assertTrue(tool.description.strip())

    def test_schema_exposes_supported_enums(self) -> None:
        tools = _tool_map()

        trace_enums = _collect_enums(tools["trace_call_path"].parameters["properties"]["direction"])
        self.assertIn(["inbound", "outbound", "both"], trace_enums)

        source_enums = _collect_enums(tools["search_context"].parameters["properties"]["sources"])
        self.assertIn(["claims", "observations", "relations", "code"], source_enums)

        target_kind_enums = _collect_enums(tools["get_code_context"].parameters["properties"]["target_kind"])
        self.assertIn(["symbol", "function", "class", "module", "file"], target_kind_enums)

    def test_get_node_neighborhood_schema_omits_concept(self) -> None:
        tool = _tool_map()["get_node_neighborhood"]
        self.assertNotIn("concept", tool.parameters["properties"])
        self.assertIn("node_id", tool.parameters["required"])

    def test_instructions_include_tool_selection_and_identifier_examples(self) -> None:
        instructions = server_module.CONTEXT_GRAPH_MCP_INSTRUCTIONS

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
        response = await server_module.search_context(
            "demo",
            "jwt audience validation",
            sources=["claims", "not-a-source"],
        )

        self.assertEqual(
            response,
            {"error": "sources must only contain: claims, observations, relations, code"},
        )

    async def test_get_code_context_rejects_invalid_target_kind_with_supported_values(self) -> None:
        response = await server_module.get_code_context(
            "demo",
            "AuthService",
            target_kind="widget",
        )

        self.assertEqual(
            response,
            {"error": "target_kind must be one of: symbol, function, class, module, file"},
        )
