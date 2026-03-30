# MCP Server Tests

This directory contains the standalone test suite for the `src` package (distributed as `context-graph-mcp`).

## What Is Covered

- `test_config.py`: config loading, YAML expansion, and CLI override behavior
- `test_mcp_bootstrap.py`: `bootstrap.py` CLI/help handling and transport dispatch
- `test_mcp_code_graph.py`: deterministic code-graph wrappers and MCP-facing adapters
- `test_mcp_imports.py`: import smoke coverage for the `src` package
- `test_mcp_provenance_tools.py`: provenance search, neighborhood, timeline, code-context, and observation tool behavior
- `test_mcp_tool_metadata.py`: app construction, registered tools, route coverage, and tool metadata

## Test Setup

`conftest.py` adds both the repo root and `mcp_server/` to `sys.path` so the tests can import the main `context_graph` package and the `src` MCP package from one checkout.

## Run

From the repo root:

```bash
uv run pytest -q mcp_server/tests
```

Run one file:

```bash
uv run pytest -q mcp_server/tests/test_mcp_provenance_tools.py
```

Run the MCP tests together with the root repo tests:

```bash
uv run pytest -q tests mcp_server/tests
```

## Notes

- These tests verify the MCP package structure and tool contract without needing a running MCP server process.
- When adding a new MCP tool or app module, update this directory with focused unit coverage and extend the import or metadata smoke tests when appropriate.
