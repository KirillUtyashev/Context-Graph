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

## Subsystems

The `context_graph/` library is organized into the following subsystem packages (post-refactoring layout — `backend/` sits directly under `context_graph/`, not under a `storage/` intermediary):

| Package | Purpose |
|---------|---------|
| `backend/` | Provider-agnostic storage drivers, metadata stores, profile-vector stores |
| `ingestion/` | Deterministic repo indexing, snapshot building, diffing, mutation planning |
| `provenance/` | Claim-centric provenance pipeline — extraction, validation, reconciliation |
| `query/` | Code graph query construction and query parsing |
| `runtime/` | LLM client configuration, reranking, tracing |
| `code_identity/` | Canonical identifier generation and path normalization |
| `models/` | Shared data models (graph snapshots, profiles, canonical nodes/edges) |
| `prompts/` | Prompt templates for LLM interactions |

## Dependency Direction

`mcp_server/` depends on `context_graph/` — never the reverse. The MCP app imports library modules for ingestion, provenance, query, and backend access, but `context_graph/` has no knowledge of the MCP layer. This keeps the core library reusable outside the MCP transport.
