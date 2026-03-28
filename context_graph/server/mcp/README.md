# Context Graph MCP Server

This package contains the internal MCP server for `context_graph`.

Today, the public MCP surface is focused on deterministic, snapshot-backed code inspection for repositories that have already been indexed by the `context_graph` CLI. The server can run over streamable HTTP, stdio, or SSE, with HTTP as the default transport.

## What It Exposes

The current decorated MCP tools live in [`server.py`](./server.py):

- `search_code`
  Search indexed repository files by text or regex.
- `get_code_graph_schema`
  Summarize node kinds, edge types, and relationship patterns from the latest stored snapshot.
- `search_code_graph`
  Search structural code nodes such as functions, classes, files, and modules.
- `trace_call_path`
  Trace bounded `CALLS` relationships from one exact qualified name.
- `get_code_snippet`
  Return source code for one exact qualified name from the indexed snapshot.

The server also exposes an HTTP health route:

- `GET /health`

## How It Works

The MCP tools in this package query the latest stored `GraphSnapshot` for a `repo_id`. That means the normal flow is:

1. Index a repository with `python -m context_graph ... --execute`
2. Start this MCP server
3. Query the indexed snapshot through MCP tools

For the code-graph tools above, the important runtime dependency is access to metadata that contains the latest snapshot for a repository. The server currently resolves metadata from one of:

- `POSTGRES_DSN`
- `DATABASE_URL`
- `METADATA_SQLITE_PATH`

The code tools also read source files from the indexed snapshot's `root_path`, so the indexed checkout still needs to exist on disk.

## Quick Start

### 1. Index a repository

Example using the hosted GitHub flow:

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

### 2. Start the MCP server over HTTP

From the repository root:

```bash
POSTGRES_DSN='postgresql://context_graph:context_graph@localhost:5433/context_graph' \
./.venv/bin/python -m context_graph.server.mcp \
  --config /Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/config.yaml \
  --transport http
```

Then check:

```bash
curl -i http://localhost:8000/health
```

### 3. Start the MCP server over stdio

```bash
POSTGRES_DSN='postgresql://context_graph:context_graph@localhost:5433/context_graph' \
./.venv/bin/python -m context_graph.server.mcp \
  --config /Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/config.yaml \
  --transport stdio
```

## Using MCP Inspector

For local interactive testing, MCP Inspector is the simplest option:

```bash
npx @modelcontextprotocol/inspector \
  -e POSTGRES_DSN='postgresql://context_graph:context_graph@localhost:5433/context_graph' \
  -- ./.venv/bin/python -m context_graph.server.mcp \
  --config /Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/config.yaml \
  --transport stdio
```

Inspector typically starts a local UI at `http://localhost:6274`.

## CLI Options

The server entrypoint accepts these command-line flags:

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
- `--destroy-graph`

`--host` and `--port` only matter for HTTP and SSE transports.

## Configuration Files

This package includes:

- [`config.yaml`](./config.yaml)
- [`config/config.yaml`](./config/config.yaml)
- [`config/config-docker-neo4j.yaml`](./config/config-docker-neo4j.yaml)
- [`config/config-neptune-bedrock.yaml`](./config/config-neptune-bedrock.yaml)
- [`config/mcp_config_stdio_example.json`](./config/mcp_config_stdio_example.json)

The server loads configuration from:

1. CLI flags
2. Environment variables
3. YAML config
4. Pydantic defaults

If you do not pass `--config`, the code falls back to `CONFIG_PATH` and then to a relative `config/config.yaml`. In practice, passing an explicit `--config` path is the least surprising option when launching from the repo root.

## Important Runtime Variables

Common environment variables for local development:

- `POSTGRES_DSN`
- `DATABASE_URL`
- `METADATA_SQLITE_PATH`
- `NEO4J_URI`
- `NEO4J_USER`
- `NEO4J_PASSWORD`
- `GRAPHITI_GROUP_ID`
- `OPENAI_API_KEY`

Notes:

- The current code-graph tools are snapshot-backed and primarily depend on metadata access.
- `repo_id` is passed to the tool call itself and selects which indexed repository snapshot to query.
- `group_id` namespaces graph/memory data and is configured separately from `repo_id`.

## Example Tool Calls

Example `search_code` arguments:

```json
{
  "repo_id": "owner-repo",
  "query": "GraphitiConfig",
  "file_pattern": "context_graph/server/**/*.py",
  "limit": 10
}
```

Example `search_code_graph` arguments:

```json
{
  "repo_id": "owner-repo",
  "query": "initialize_server",
  "node_kinds": ["Function"],
  "limit": 10
}
```

Example `trace_call_path` arguments:

```json
{
  "repo_id": "owner-repo",
  "qualified_name": "context_graph.server.mcp.server.initialize_server",
  "direction": "both",
  "depth": 2
}
```

Example `get_code_snippet` arguments:

```json
{
  "repo_id": "owner-repo",
  "qualified_name": "context_graph.server.mcp.server.initialize_server",
  "include_neighbors": true
}
```

## Package Layout

- [`__main__.py`](./__main__.py): module entrypoint
- [`server.py`](./server.py): transport setup, tool registration, runtime initialization
- [`config.py`](./config.py): Pydantic settings and YAML/env loading
- [`code_graph.py`](./code_graph.py): deterministic code-graph query helpers
- [`response_types.py`](./response_types.py): MCP response payload types
- [`queue.py`](./queue.py): queue helpers for legacy memory flows
- [`formatting.py`](./formatting.py): response formatting helpers

## Troubleshooting

- `No indexed snapshot found for repo_id=...`
  Index the repository first, and make sure the server can read the same metadata store used during indexing.
- `Metadata store is unavailable: ...`
  Check `POSTGRES_DSN`, `DATABASE_URL`, or `METADATA_SQLITE_PATH`.
- Source file not found for a symbol
  The indexed checkout path in the snapshot no longer exists on disk.
- HTTP works but stdio client does not connect
  Make sure the stdio client launches the module with `--transport stdio`.

## Related Docs

- [`/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/LOCAL_DEV.md`](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/LOCAL_DEV.md)
- [`/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/server.py`](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/server.py)
- [`/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/config.py`](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/server/mcp/config.py)
