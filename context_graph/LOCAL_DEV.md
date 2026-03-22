# Local Development

This project can now test the current snapshot, temporal-update flow, and episode extraction against:

- local PostgreSQL for metadata
- local Neo4j for graph writes
- a locally configured LLM provider for Graphiti-style fact extraction

## 1. Start local services

From the repo root:

```bash
docker compose -f context_graph/docker-compose.local.yml up -d
```

This starts:

- PostgreSQL on `localhost:5433`
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

When the LLM env vars above are set, the same command also runs commit-message episodes through the Graphiti-style extraction layer.

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

If the LLM provider is configured, the command above will try to produce semantic `RELATES_TO` facts in addition to `MENTIONS`.

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

`hosted-index` now uses the same `--llm-provider` / env-var based episode configuration, so PR bodies, issue bodies, reviews, and comments can all flow through the same extraction path.

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
- `reset-context --execute` removes the stored `CodeNode` / `CodeNodeVersion` graph and metadata rows for a repo id.
- If you skip `--execute`, the index commands only print the planned work without mutating storage.
