# Deterministic Ingestion

This package is the deterministic repository indexing block for Context Graph.
It is responsible for:

- resolving a local or hosted repository checkout
- selecting the commit or ref to index
- exporting a structural code graph snapshot
- diffing that snapshot against the previous indexed revision
- planning and optionally executing deterministic graph writes
- recording repository and snapshot metadata

It is intentionally not responsible for:

- GitHub PR, issue, review, or comment ingestion
- episode extraction
- profile generation
- LLM-driven enrichment

## Mental Model

The package is organized around a thin public driver and a few focused internal
modules:

- `driver.py`
  - public entrypoint used by the CLI and runtime callers
- `models.py`
  - request/result dataclasses shared across the ingestion flow
- `commit_indexing.py`
  - deterministic commit replay and single-commit indexing
- `hosted_indexing.py`
  - repo resolution, clone/fetch management, and hosted request orchestration
- `repo_cache.py`
  - on-disk cache and per-request workspace management
- `snapshot_builder.py`
  - exporter execution
- `snapshot_parsing.py`
  - conversion from exporter JSON to stable node/edge ids and snapshots
- `diffing.py`
  - snapshot-to-snapshot structural diffing
- `deterministic_planner.py`
  - translation from snapshot diffs / commit provenance into typed backend mutations
- `temporal.py`
  - metadata coordination plus apply/finalization of typed mutation plans
- `git/`
  - raw git and GitHub client helpers used by deterministic ingestion

The rule of thumb is:

- keep tiny pure logic modules flat
- keep orchestration in focused services
- keep `driver.py` small and readable

## Main Entry Point

Use `IngestionDriver` when you want deterministic indexing from application code:

```python
from context_graph.ingestion import HostedIndexRequest, IngestionDriver

driver = IngestionDriver(
    metadata_store=metadata_store,
    backend_driver=backend_driver,
    github_api=github_api,
)

result = driver.index_hosted(
    HostedIndexRequest(
        github_repo="owner/repo",
        repo_id="my-repo",
        execute=True,
    )
)
```

The main public methods are:

- `apply_latest_commit(...)`
- `apply_commit(...)`
- `index_hosted(...)`

## End-to-End Flow

For a hosted request, the deterministic flow is:

1. Resolve repo identity from a local path, GitHub URL, or `owner/repo`.
2. Reuse or refresh a managed clone.
3. Resolve the target commit or ref.
4. Build a temporary worktree for the exact commit being indexed.
5. Run the exporter and parse it into a stable `GraphSnapshot`.
6. Diff the new snapshot against the previous indexed snapshot.
7. Plan typed node/edge/search-document mutations for structural changes and commit provenance.
8. Optionally execute those writes.
9. Persist snapshot metadata and repo index state.

## Reading the Code

If you are trying to understand the package quickly, start here:

1. `driver.py`
2. `hosted_indexing.py`
3. `commit_indexing.py`
4. `snapshot_builder.py`
5. `snapshot_parsing.py`
6. `temporal.py`

That order follows the main control flow from request entry to graph writes.

## Notes for Future Work

If we later add a separate provenance pipeline, it should consume the outputs of
this package rather than pushing more responsibilities back into it. The
deterministic boundary here is deliberate.
