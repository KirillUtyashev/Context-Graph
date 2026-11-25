# Local Development

## Setup

Follow the [Quick Start in the README](../README.md#quick-start) for the complete walkthrough: install dependencies, copy the tracked `.env.example`, set your OpenAI API key, start the databases, index a repository, and launch the MCP server. Run all commands from the repository root.

Local infrastructure includes:

- Postgres + pgvector on `localhost:5433`
- Neo4j on `localhost:7687`

The root `.env.example` supplies both metadata DSNs (`CONTEXT_GRAPH_METADATA_DSN` for the CLI and `POSTGRES_DSN` for the MCP server), Neo4j credentials, and model settings. Keep the two DSNs pointed at the same database. `uv run --env-file .env` explicitly loads these settings for each command.

## Working on the Code

After changing Python code, refresh the non-editable workspace installation before starting the MCP server:

```bash
uv sync --all-packages --dev --no-editable
```

Use `uv run --no-sync` for subsequent commands to preserve that installation. Run both test suites with:

```bash
uv run --no-sync python -m pytest tests
PYTHONPATH=mcp_server uv run --no-sync python -m pytest mcp_server/tests
```

The MCP test command uses `PYTHONPATH=mcp_server` to import the local server sources.

## Troubleshooting

- If indexing cannot connect to Postgres or Neo4j, check `docker compose ps` and confirm `.env` matches `docker-compose.yml`.
- If the native indexer cannot build, confirm `make`, a C11/C++17 compiler, and zlib development headers are installed. You can build it directly with `make -C codebase_memory_pipeline repo-graph-export`.
- See the [benchmarking guide](../evaluation/MCP_RUN_GUIDE.md) for evaluation commands.
