# Local Development

## Setup

```bash
uv sync --all-packages --dev --no-editable
cp .env.example .env
docker compose up -d
```

Use `--no-editable` here. On Python 3.13, `uv` workspace editable installs currently land in hidden `.pth` files, and the interpreter skips those files during startup. Non-editable installs avoid that issue and make both `context_graph` and `context_graph_mcp_server` importable from the CLI and from outside the repo root.

Local infrastructure includes:

- Postgres + pgvector on `localhost:5433`
- Neo4j on `localhost:7687`

## Index a Repo

```bash
python -m context_graph apply-latest-commit \
  /absolute/path/to/repo \
  --repo-id demo-repo \
  --metadata-backend postgres \
  --metadata-dsn postgresql://context_graph:context_graph@localhost:5433/context_graph \
  --graph-backend neo4j \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password contextgraph \
  --execute
```

## Run the MCP Server

```bash
uv run --package context-graph-mcp context-graph-mcp \
  --config /absolute/path/to/Context-Graph/mcp_server/config/config.yaml \
  --transport http
```

Health check:

```bash
curl -i http://localhost:8000/health
```
