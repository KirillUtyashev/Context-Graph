# Context Graph MCP Server

This package contains the internal MCP server for `context_graph`. The server uses the additive backend in `context_graph.storage.backend` plus the existing metadata snapshot store.

## Capability Matrix

| Tool | Status | Backend dependency |
| --- | --- | --- |
| `search_code` | implemented | metadata snapshot + checked-out repo files |
| `get_code_graph_schema` | implemented | metadata snapshot |
| `search_code_graph` | implemented | metadata snapshot |
| `trace_call_path` | implemented | metadata snapshot |
| `get_code_snippet` | implemented | metadata snapshot |
| `get_code_context` | implemented | metadata snapshot |
| `search_context` | implemented | split claim/observation/code node retrieval plus relation retrieval; Neptune lexical retrieval requires AOSS |
| `get_node_neighborhood` | implemented | exact `node_id`; snapshot-backed for code nodes, graph-backed for provenance nodes |
| `get_timeline` | implemented | exact `target_id` with claim-only historical provenance assembly |
| `record_agent_observation` | implemented | backend driver + metadata store; semantic indexing when available |

Notes:

- `search_context` runs a node pipeline over claims, observations, and code plus a separate relation-profile pipeline, and can include inactive semantic profiles for provenance/history inspection.
- `get_node_neighborhood` intentionally does not support concept anchoring in this phase; callers must pass an exact `node_id`.
- `get_timeline` intentionally works from an exact graph `target_id` and returns claim and claim-relation events only.
- `record_agent_observation` persists agent notes/traces as provenance observations and routes extractable kinds through the same observation-ingestion service used by repository ingestion.
- Neptune provenance retrieval uses backend traversal parity, but lexical `search_context` on Neptune still requires AOSS to be configured.

## Runtime Model

The server has two runtime layers:

- `MetadataSnapshotService`
  Loads the latest stored `GraphSnapshot` from Postgres or SQLite for deterministic code tools.
- `MCPBackendRuntime`
  Lazily creates a concrete backend driver through `context_graph.storage.backend.create_driver(...)`, plus optional LLM/embedder-backed provenance services.

Normal flow:

1. Index a repository with `python -m context_graph ... --execute`
2. Start this MCP server
3. Query the indexed repo by `repo_id`

## Quick Start

### 1. Index a repository

```bash
python3 -m context_graph hosted-index \
  --github-url https://github.com/OWNER/REPO \
  --repo-id owner-repo \
  --repo-cache-dir /absolute/path/to/.cache/context-graph \
  --metadata-backend postgres \
  --metadata-dsn postgresql://context_graph:context_graph@localhost:5433/context_graph \
  --graph-backend neo4j \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password contextgraph \
  --llm-provider openai \
  --llm-model gpt-4o \
  --execute
```

### 2. Start the MCP server

```bash
POSTGRES_DSN='postgresql://context_graph:context_graph@localhost:5433/context_graph' \
./.venv/bin/python -m context_graph.server.mcp \
  --config /Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/config.yaml \
  --transport http
```

Health check:

```bash
curl -i http://localhost:8000/health
```

### 3. Start over stdio

```bash
POSTGRES_DSN='postgresql://context_graph:context_graph@localhost:5433/context_graph' \
./.venv/bin/python -m context_graph.server.mcp \
  --config /Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/config.yaml \
  --transport stdio
```

## CLI Options

- `--config <path>`
- `--transport http|stdio|sse`
- `--host <host>`
- `--port <port>`
- `--llm-provider openai|bedrock`
- `--embedder-provider openai|bedrock|bedrock_titan`
- `--database-provider neo4j|neptune`
- `--model <model>`
- `--small-model <model>`
- `--temperature <float>`
- `--embedder-model <model>`
- `--group-id <group_id>`
- `--user-id <user_id>`

## Configuration Surface

The MCP config surface is:

- `server`
- `llm`
- `embedder`
- `database`
- `namespace`

Example namespace block:

```yaml
namespace:
  group_id: ${NAMESPACE_GROUP_ID:main}
  episode_id_prefix: ${NAMESPACE_EPISODE_ID_PREFIX:}
  user_id: ${NAMESPACE_USER_ID:mcp_user}
```

Included examples:

- [`config.yaml`](./config.yaml)
- [`config/config.yaml`](./config/config.yaml)
- [`config/config-docker-neo4j.yaml`](./config/config-docker-neo4j.yaml)
- [`config/config-neptune-bedrock.yaml`](./config/config-neptune-bedrock.yaml)
- [`config/mcp_config_stdio_example.json`](./config/mcp_config_stdio_example.json)

## Important Environment Variables

- `POSTGRES_DSN`
- `DATABASE_URL`
- `METADATA_SQLITE_PATH`
- `NEO4J_URI`
- `NEO4J_USER`
- `NEO4J_PASSWORD`
- `NAMESPACE_GROUP_ID`
- `NAMESPACE_USER_ID`
- `OPENAI_API_KEY`

Hybrid provenance search is implemented through backend lexical retrieval plus semantic profile fusion.
