# MCP Server

The canonical MCP implementation now lives under `mcp_server/`.

## Run Locally

```bash
uv run --package context-graph-mcp context-graph-mcp \
  --config /absolute/path/to/Context-Graph/mcp_server/config/config.yaml \
  --transport http
```

For stdio:

```bash
uv run --package context-graph-mcp python -m context_graph_mcp_server \
  --config /absolute/path/to/Context-Graph/mcp_server/config/config.yaml \
  --transport stdio
```

## Public Interfaces

- Canonical: `context-graph-mcp`
- Canonical: `python -m context_graph_mcp_server`

The MCP app exposes deterministic code tools and provenance-oriented graph tools on top of the core library.
