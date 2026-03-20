# Local Development

This project can now test the current snapshot and temporal-update flow against:

- local PostgreSQL for metadata
- local Neo4j for graph writes

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

## 3. Run a real local apply

You can keep config inline:

```bash
python3 -m context_graph plan-apply \
  /Users/utya.kirill/Desktop/UOFT/Context-Graph \
  --repo-id demo-context \
  --revision-id local-r1 \
  --metadata-backend postgres \
  --metadata-dsn postgresql://context_graph:context_graph@localhost:5433/context_graph \
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
python3 -m context_graph plan-apply \
  /Users/utya.kirill/Desktop/UOFT/Context-Graph \
  --repo-id demo-context \
  --revision-id local-r1 \
  --metadata-backend postgres \
  --graph-backend neo4j \
  --execute
```

## 4. Inspect results

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

## Notes

- `build-snapshot` only builds and prints a snapshot summary.
- `plan-apply --execute` is the command that writes to Neo4j and updates metadata storage.
- If you skip `--execute`, the tool only plans statements and stores the latest snapshot metadata.
