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

## Agent Usage

Use the MCP tools in two phases when possible:

- Start with a search tool when you do not yet know an exact identifier.
- Switch to the exact-id tool after the search response gives you the right `qualified_name`, `node_id`, or `target_id`.

Identifier examples:

- `repo_id`: stable repository identifier used at indexing time, for example `owner-repo`
- `qualified_name`: fully qualified code symbol such as `pkg.module.func`
- `node_id`: exact indexed graph id such as `project:owner-repo`, `file:owner-repo:path/to/file.py`, or `symbol:owner-repo:pkg.module.func`
- `target_id`: exact provenance graph id such as `claim:123`, `observation:456`, or `evidence:789`

Tool selection guide:

- Use `search_code` for plain-text or regex search in indexed files.
- Use `get_code_graph_schema` to inspect the latest snapshot structure for a repo.
- Use `search_code_graph` when you are discovering symbols or files and do not yet know an exact `qualified_name`.
- Use `trace_call_path` and `get_code_snippet` only after you have an exact `qualified_name`.
- Use `get_code_context` for a deterministic file or symbol-centered summary, optionally narrowed with `target_kind`.
- Use `search_context` when you need provenance, claims, observations, relations, or code retrieval and do not yet know an exact graph anchor.
- Use `get_node_neighborhood` only with an exact `node_id`.
- Use `get_timeline` only with an exact `target_id`.
- Use `record_agent_observation` to persist an agent note or trace into the provenance pipeline.

Example code workflow:

1. Call `search_code_graph(repo_id="owner-repo", query="AuthService")`
2. Read the returned `qualified_name`
3. Call `get_code_snippet(...)` or `trace_call_path(...)` with that exact `qualified_name`

Example provenance workflow:

1. Call `search_context(repo_id="owner-repo", query="JWT audience validation", sources=["claims", "code"])`
2. Read the returned `record_id` or `node_id`
3. Call `get_timeline(...)` or `get_node_neighborhood(...)` with that exact graph id

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
