# Architecture

Context Graph has two top-level runtime boundaries:

- `context_graph/`: the reusable library that owns deterministic ingestion, provenance ingestion, storage abstractions, and retrieval helpers
- `mcp_server/`: the standalone MCP app that exposes the library through HTTP or stdio transports

At a high level:

1. The ingestion pipeline resolves a repo, builds a deterministic snapshot, diffs it against the previous revision, and writes structural mutations.
2. The provenance pipeline stores authored artifacts as observations, extracts claims and evidence when configured, validates them, and persists temporal claim state.
3. The MCP app reads deterministic snapshots for code tools and uses backend-native retrieval plus semantic profiles for provenance tools.

The key design constraint is directional dependency:

- the MCP app depends on `context_graph`
- `context_graph` does not depend on the MCP app

Reusable deterministic code-query helpers live under `context_graph/query/`, while app-specific config loading, transport setup, and tool registration live under `mcp_server/src/context_graph_mcp_server/`.
