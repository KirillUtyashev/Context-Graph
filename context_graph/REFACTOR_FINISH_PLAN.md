# Refactor Finish Plan

This checklist captures the remaining work needed to finish the Graphiti-to-Context-Graph refactor cleanly.

## Current Status

The important runtime logic is already internalized:

- the MCP server now lives under `context_graph.server.mcp`
- the memory/search/runtime/storage code is under `context_graph/*`
- local scripts run the internal server
- app code no longer depends on `vendor/graphiti` at runtime

What remains is mostly cleanup, renaming, packaging, deployment, and parity hardening.

## Remaining Steps

### 1. Finalize the public entrypoints

- Decide which entrypoints are official going forward:
  - `python -m context_graph.server.mcp`
  - `python -m context_graph`
- Update every script, deploy path, service unit, and README to use only those entrypoints.
- Remove compatibility wrappers once nothing depends on them anymore:
  - `graphiti_mcp_server.py`
  - `main.py`

Definition of done:

- no deploy or local script shells out to the compatibility wrappers
- all docs and examples use internal `context_graph` module entrypoints only

### 2. Rename internal Graphiti-branded interfaces

The code works, but a lot of internal names still read like a vendored Graphiti app.

- Rename Graphiti-specific service names where they are now just internal Context Graph services.
- Replace stale help text, comments, and docstrings in:
  - `context_graph/server/mcp/server.py`
  - `context_graph/server/mcp/config.py`
  - `context_graph/server/mcp/formatting.py`
  - `context_graph/server/mcp/response_types.py`
  - `context_graph/episode_processor.py`
  - `context_graph/graph_sinks.py`
- Keep Graphiti references only where they describe historical origin or algorithmic compatibility.

Definition of done:

- internal code reads as `context_graph` first
- "Graphiti" only appears where it is historically or conceptually correct

### 3. Clean the deployment packaging

The runtime is internal, but some deployment artifacts still describe the old vendored packaging model.

- Remove stale `graphiti_core_src` wording from:
  - `context_graph/server/mcp/requirements.txt`
  - deployment comments and artifact notes
- Simplify `scripts/deploy-mcp.sh` so it packages only the internal source tree and the final entrypoint shape we want to support.
- Verify the EC2 deploy script installs only the internal package layout and does not rely on old wrapper assumptions.

Definition of done:

- deployment artifact structure matches the current internal runtime
- no deploy comment or requirement file claims vendored Graphiti is still being installed

### 4. Prune stale config variants

There are config files and comments left over from the upstream MCP server shape.

- Audit `context_graph/server/mcp/config/` and remove variants we do not support anymore.
- Keep only the config files needed for our current backends and environments.
- Confirm CLI help, config defaults, and environment variables match the real supported matrix.

Definition of done:

- every config file in `context_graph/server/mcp/config/` is used and documented
- no stale FalkorDB or other unused variants remain unless we intentionally support them

### 5. Refresh the main docs

Several docs still explain the old vendored architecture.

- Update:
  - `context_graph/README.md`
  - `context_graph/LOCAL_DEV.md`
  - `GRAPHITI_PORT.md`
  - `PROGRESS_SUMMARY.md`
- Decide whether `GRAPHITI_PORT.md` should become:
  - a historical archive document
  - or a short migration note pointing to the internal runtime
- Remove outdated claims about:
  - vendored runtime ownership
  - old MCP server paths
  - old deployment artifacts

Definition of done:

- a new contributor can understand the current architecture without reading `vendor/graphiti`

### 6. Delete the dormant vendored runtime

Once the deploy path and docs are clean, remove the old vendored runtime source tree.

- Delete the runtime portions of `vendor/graphiti` that are no longer used by the app.
- Keep only anything still required for:
  - licensing
  - attribution
  - temporary historical reference
- If needed, move attribution/licensing text into a dedicated note in the root docs.

Definition of done:

- the repository no longer carries a second dormant runtime implementation
- licensing obligations are still satisfied

### 7. Add end-to-end parity tests

We have basic verification, but the last step is locking in the refactor with explicit parity coverage.

- Add tests for:
  - hosted repo ingest
  - episode ingest
  - node search
  - fact search
  - profile search
  - MCP server startup and tool calls
- Add a packaging smoke test for the deployed MCP artifact shape.
- Add at least one regression test around the Graphiti-compatible entity fields we recently added:
  - `uuid`
  - `group_id`
  - `summary`
  - `name_embedding`
  - `fact_embedding`

Definition of done:

- the internal runtime can be validated without comparing behavior manually against old vendored code

### 8. Do one final import and dependency audit

- Search the repo for:
  - `vendor/graphiti`
  - `graphiti_core`
  - `graphiti_mcp_server`
- Remove any remaining runtime imports, stale comments, or deploy references.
- Confirm the Python dependency list only includes what the internal runtime now needs.

Definition of done:

- no production code path imports vendored Graphiti
- no dependency is present only because of the deleted runtime layout

## Suggested Execution Order

1. Finish entrypoints and deployment packaging.
2. Rename internal Graphiti-branded interfaces.
3. Prune config variants.
4. Refresh docs.
5. Add parity tests.
6. Delete vendored runtime.
7. Run one final import/dependency audit.

## Acceptance Checklist

- `python -m context_graph.server.mcp` is the only MCP server entrypoint we need.
- Local scripts and deploy scripts all use the same entrypoint.
- Docs no longer tell developers to look under `vendor/graphiti`.
- The vendored runtime is gone or archived outside the runtime path.
- End-to-end ingest plus `search_memory` works through the internal server.
- The repo has one coherent architecture story instead of an internal runtime plus a dormant legacy copy.
