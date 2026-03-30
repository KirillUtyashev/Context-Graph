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

## Change Detection

The ingestion pipeline uses **snapshot diffing** to detect changes between
indexed revisions. Each indexing run exports a structural code-graph snapshot
and diffs it against the previously stored snapshot. Only the resulting
differences are forwarded for processing, keeping writes minimal and
reproducible.

## Deterministic Planner

The **deterministic planner**
(`context_graph/ingestion/deterministic_planner.py`) translates snapshot diffs
into typed backend mutations. Given a set of added, removed, and modified
nodes/edges it produces an ordered list of concrete write operations that can be
applied to the configured storage backend.

## Further Reading

For a detailed look at the snapshot format, diff algorithm, and planner
internals see [Ingestion Internals](ingestion-internals.md).

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
