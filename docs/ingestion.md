# Ingestion

The ingestion entrypoint remains the core library CLI:

```bash
python -m context_graph ...
```

The main orchestration boundary is `IngestionDriver`, which coordinates:

- repository resolution and checkout
- snapshot generation and parsing
- deterministic diffing and mutation planning
- commit/document/GitHub observation ingestion
- metadata and graph persistence

Typical local indexing flow:

```bash
python -m context_graph hosted-index \
  --github-url https://github.com/OWNER/REPO \
  --repo-id owner-repo \
  --metadata-backend postgres \
  --graph-backend neo4j \
  --execute
```

The ingestion surface is intentionally unchanged by the MCP refactor.
