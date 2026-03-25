# Local Development

This project can now test the current snapshot, temporal-update flow, and episode extraction against:

- local PostgreSQL for metadata
- local Neo4j for graph writes
- Graphiti's Neo4j driver as the graph execution backend
- a locally configured LLM provider for Graphiti-style fact extraction

## 1. Start local services

From the repo root:

```bash
docker compose -f context_graph/docker-compose.local.yml up -d
```

This starts:

- PostgreSQL + `pgvector` on `localhost:5433`
- Neo4j on `localhost:7687` and browser UI on `http://localhost:7474`

Default local credentials:

- PostgreSQL:
  - database: `context_graph`
  - user: `context_graph`
  - password: `context_graph`
- Neo4j:
  - user: `neo4j`
  - password: `contextgraph`

## 2. Install local Python drivers

```bash
python3 -m pip install -r context_graph/requirements-local.txt
```

That local requirements file includes the vendored Graphiti base Python deps needed by the
`GraphitiNeo4jExecutor`, including `python-dotenv`, `pydantic`, `numpy`, and `tenacity`.

## 3. Optional: enable LLM-backed episode extraction

If you do not configure an LLM provider, episode ingestion still works, but it stays in deterministic mode:

- `Episode` nodes are written
- `ON` edges are written
- `MENTIONS` edges are resolved from current graph context
- no semantic `RELATES_TO` facts are extracted

To test the full Graphiti-style path locally, choose one of these providers.

### OpenAI-compatible

```bash
export CONTEXT_GRAPH_LLM_PROVIDER=openai
export OPENAI_API_KEY=your_key_here
export CONTEXT_GRAPH_LLM_MODEL=your_model_id
```

Optional overrides:

```bash
export CONTEXT_GRAPH_LLM_SMALL_MODEL=your_small_model_id
export CONTEXT_GRAPH_LLM_BASE_URL=http://localhost:4000/v1
```

`CONTEXT_GRAPH_LLM_BASE_URL` is useful if you want to point local development at an OpenAI-compatible proxy or gateway instead of the default API endpoint.
If that local gateway does not require real auth, still set `OPENAI_API_KEY` to a placeholder string because the Python OpenAI client requires one.

### AWS Bedrock

```bash
export CONTEXT_GRAPH_LLM_PROVIDER=bedrock
export AWS_REGION=us-east-1
```

Optional overrides:

```bash
export CONTEXT_GRAPH_LLM_MODEL=anthropic.claude-3-5-haiku-20241022-v1:0
export CONTEXT_GRAPH_LLM_SMALL_MODEL=anthropic.claude-3-haiku-20240307-v1:0
```

If you omit the model ids, the vendored Graphiti Bedrock client already defaults to those two Anthropic Haiku model ids.

## 4. Run a real local apply

All graph writes now go through `GraphitiNeo4jExecutor` — there is no `--graph-executor` flag
anymore. Just provide Neo4j credentials and add `--execute`.

You can keep config inline:

```bash
python3 -m context_graph apply-latest-commit \
  /Users/utya.kirill/Desktop/UOFT/Context-Graph \
  --repo-id demo-context \
  --metadata-backend postgres \
  --metadata-dsn postgresql://context_graph:context_graph@localhost:5433/context_graph \
  --graph-backend neo4j \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password contextgraph \
  --execute
```

For the lightest possible smoke test, skip PostgreSQL and use SQLite metadata:

```bash
python3 -m context_graph apply-latest-commit \
  /Users/utya.kirill/Desktop/UOFT/Context-Graph \
  --repo-id demo-context \
  --metadata-db /tmp/context-graph.sqlite \
  --graph-backend neo4j \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password contextgraph \
  --execute
```

Or use environment variables:

```bash
export CONTEXT_GRAPH_METADATA_DSN=postgresql://context_graph:context_graph@localhost:5433/context_graph
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=contextgraph
```

Then:

```bash
python3 -m context_graph apply-latest-commit \
  /Users/utya.kirill/Desktop/UOFT/Context-Graph \
  --repo-id demo-context \
  --metadata-backend postgres \
  --graph-backend neo4j \
  --execute
```

To test a one-off episode directly:

```bash
python3 -m context_graph ingest-episode \
  --repo-id demo-context \
  --source-type user_message \
  --source-id local-1 \
  --author you \
  --parent-node-id commit:demo-context:YOUR_COMMIT_SHA \
  --metadata-backend postgres \
  --graph-backend neo4j \
  --text "Why was the helper function added in this commit?" \
  --execute
```

To sync active semantic fact profiles into the local pgvector store:

```bash
python3 -m context_graph sync-profile-vectors \
  --repo-id demo-context \
  --metadata-backend postgres \
  --metadata-dsn postgresql://context_graph:context_graph@localhost:5433/context_graph \
  --embed-provider bedrock
```

To parse a retrieval query into anchors and high-level keywords:

```bash
python3 -m context_graph parse-query \
  --query "Why was helper added to app.py and what is the rollback impact?"
```

To search semantic relation profiles through pgvector:

```bash
python3 -m context_graph search-profiles \
  --repo-id demo-context \
  --query "What is the rollback impact of the helper change?" \
  --metadata-backend postgres \
  --metadata-dsn postgresql://context_graph:context_graph@localhost:5433/context_graph \
  --embed-provider bedrock
```

To test the hosted GitHub URL flow locally:

```bash
export GITHUB_TOKEN=your_token_here

python3 -m context_graph hosted-index \
  --github-url https://github.com/OWNER/REPO \
  --repo-id owner-repo \
  --repo-cache-dir /Users/utya.kirill/Desktop/UOFT/Context-Graph/.cache/context-graph \
  --metadata-backend postgres \
  --graph-backend neo4j \
  --execute
```

To clear a repo's persisted graph + metadata context:

```bash
python3 -m context_graph reset-context \
  --repo-id demo-context \
  --metadata-backend postgres \
  --graph-backend neo4j \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password contextgraph \
  --execute
```

On the first Graphiti-backed run, the driver will also build its usual Neo4j range/fulltext indices.
Those extra Graphiti indices are safe to keep in a local dev database even though `context_graph`
is still using its own schema and query planners.

## 5. Inspect results

- Neo4j browser: `http://localhost:7474`
- Bolt URI: `bolt://localhost:7687`

Useful browser query:

```cypher
MATCH (n:CodeNode)
RETURN labels(n) AS labels, count(*) AS count
ORDER BY count DESC
```

Useful relationship query:

```cypher
MATCH ()-[r]->()
RETURN type(r) AS type, count(*) AS count
ORDER BY count DESC
```

To inspect the episode layer:

```cypher
MATCH (e:Episode)-[r]->(n)
RETURN labels(e) AS episode_labels, type(r) AS rel, labels(n) AS target_labels, count(*) AS count
ORDER BY count DESC
```

To inspect semantic facts:

```cypher
MATCH (s)-[r:RELATES_TO]->(t)
RETURN s.node_id, r.name, r.fact, t.node_id
LIMIT 25
```

## Notes

- `apply-commit` indexes one explicit commit SHA from a local checkout.
- `apply-latest-commit --execute` writes commit-aware graph updates and metadata for a local checkout.
- `hosted-index --execute` does the same thing starting from a GitHub URL or slug and a persistent repo cache.
- `ingest-episode --execute` is the easiest way to test the LLM-backed episode layer without cloning a GitHub repo.
- `sync-profile-vectors` backfills active semantic fact profiles into Aurora/local Postgres pgvector.
- `search-profiles` is the current LightRAG-style high-level retrieval entrypoint for semantic `RELATES_TO` facts.
- `reset-context --execute` removes the stored `CodeNode` / `CodeNodeVersion` graph and metadata rows for a repo id.
- If you skip `--execute`, the index commands only print the planned work without mutating storage.


---

## Change log

### Graphiti-only execution (removed Direct executors)

Previously the codebase had three executor implementations behind a `--graph-executor` CLI flag:

- `DirectNeo4jExecutor` — used the raw `neo4j` Python driver synchronously
- `DirectNeptuneExecutor` — same but pointed at Neptune's Bolt endpoint
- `GraphitiNeo4jExecutor` — used the vendored Graphiti async Neo4j driver

All three shared the same `BaseCypherExecutor` ABC and were selected at runtime via
`--graph-executor direct` (default) or `--graph-executor graphiti`.

**What changed:**

- `DirectNeo4jExecutor`, `DirectNeptuneExecutor`, and the `_search_context_nodes_with_neo4j`
  helper were removed entirely from `graph_sinks.py`.
- `GraphitiNeo4jExecutor` is now the only executor. `Neo4jGraphSink` and `NeptuneGraphSink`
  both default to it.
- The `--graph-executor` flag was removed from all CLI subcommands (`apply-latest-commit`,
  `apply-commit`, `hosted-index`, `ingest-episode`, `reset-context`).
- `_make_sink` in `cli.py` always constructs a `GraphitiNeo4jExecutor` — no branching logic.
- `GraphitiNeo4jExecutor.search_context_nodes` returns `()` gracefully when no credentials
  are configured, so planning-only runs (no `--execute`) work without a live Neo4j connection.
- All 46 unit tests pass after updating test fixtures to inject `RecordingExecutor` where
  bare `Neo4jGraphSink()` / `NeptuneGraphSink()` were used without credentials.

### GraphitiNeptuneExecutor (proper Neptune driver support)

Previously `NeptuneGraphSink` defaulted to `GraphitiNeo4jExecutor` with a manufactured
`bolt://` URI — this was incorrect because Graphiti's `NeptuneDriver` uses the HTTPS
openCypher endpoint via `langchain_aws` / boto3, not the Neo4j Bolt protocol.

**What changed:**

- Added `GraphitiNeptuneExecutor` to `graph_sinks.py`. It lazily instantiates Graphiti's
  `NeptuneDriver` with `neptune-db://` or `neptune-graph://` host prefix and optional
  AOSS (OpenSearch Serverless) for BM25 keyword search.
- `NeptuneGraphSink` now defaults to `GraphitiNeptuneExecutor` instead of
  `GraphitiNeo4jExecutor`.
- New CLI flags on all relevant subcommands:
  - `--neptune-host-scheme` (`neptune-db` or `neptune-graph`, default `neptune-db`)
  - `--aoss-host` (optional AOSS host, also via `CONTEXT_GRAPH_AOSS_HOST` env var)
- `_make_sink` in `cli.py` passes `host_scheme` and `aoss_host` to `NeptuneGraphSink`.
- `GraphitiNeptuneExecutor` exported from `context_graph.__init__`.
- 5 new tests added (50 total pass).
