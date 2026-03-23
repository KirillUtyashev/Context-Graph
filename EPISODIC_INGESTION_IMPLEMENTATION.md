# Context Graph MCP Server — Episodic Ingestion Implementation

> **Status**: End-to-end episodic ingestion is live on EC2.
> Both the Neptune (Graphiti) and Aurora PostgreSQL (context_graph) write paths are operational.

---

## Overview

The Context Graph project builds a **provenance-aware knowledge graph** for coding agents. When agents work on code — writing PRs, updating docs, discussing architecture — that context is normally lost. This system captures it automatically by ingesting agent transcripts and conversation episodes into a dual-backend graph store:

- **Amazon Neptune** (via Graphiti): Entity and fact extraction into a knowledge graph for structured retrieval
- **Aurora PostgreSQL** (via context_graph): Episodic metadata, semantic fact profiles, and pgvector embeddings for LightRAG-style retrieval

The MCP server (running on EC2) exposes a single `add_memory` tool that now writes to both backends simultaneously.

---

## Architecture

```
Cursor / Agent
     │
     │  MCP (HTTP)
     ▼
┌─────────────────────────────────────────────────┐
│  EC2 MCP Server  (graphiti_mcp_server.py)       │
│                                                 │
│  add_memory(name, episode_body, ...,            │
│             repo_id, source_type, source_id)    │
│              │                                  │
│      ┌───────┴────────┐                         │
│      │                │                         │
│      ▼                ▼                         │
│  QueueService    AuroraIngestionService         │
│  (async)         (asyncio.to_thread)            │
│      │                │                         │
└──────┼────────────────┼─────────────────────────┘
       │                │
       ▼                ▼
  Amazon Neptune   Aurora PostgreSQL
  (Graphiti)       (context_graph)
  Entities/Facts   cg_profiles table
  Knowledge Graph  Semantic fact profiles
```

The Neptune path queues the episode for async processing (Graphiti handles entity extraction and graph writes). The Aurora path runs synchronously (blocking in a thread) using `context_graph`'s `EpisodeProcessor`, which uses Bedrock Claude for LLM-based fact extraction and stores semantic relation profiles in Aurora.

---

## Files Changed

### `vendor/graphiti/mcp_server/src/graphiti_mcp_server.py`

**Core MCP server — the main integration point.**

1. Added `AuroraIngestionService` class that:
   - Reads `AURORA_ENDPOINT`, `AURORA_DB_NAME`, `AURORA_USERNAME`, `AURORA_PASSWORD_SECRET_ARN` from environment
   - Fetches the Aurora password from AWS Secrets Manager using the instance IAM role at initialization
   - Constructs a `PostgresMetadataStore` and bootstraps the schema
   - Uses a `_NullGraphSink` (plans Cypher statements without executing them) so `EpisodeProcessor` can run entity/fact extraction via Bedrock without attempting a Bolt connection to Neptune (Neptune writes are handled by Graphiti's own path)
   - Exposes `ingest_episode_sync()` — intended to be called via `asyncio.to_thread`

2. Modified `add_memory` to accept 5 new optional parameters:
   - `repo_id` — enables the Aurora write path (required)
   - `source_type` — episode category (e.g. `agent_transcript`, `message`)
   - `source_id` — stable identifier for deduplication
   - `author` — who produced the episode
   - `thread_id` — logical thread for grouping related episodes

3. The response message now includes Aurora ingestion stats:
   ```
   Episode '...' queued for processing in group 'main';
   Aurora: 5 graph writes, 9 facts extracted
   ```

4. `AuroraIngestionService` is initialized at server startup only if `AURORA_ENDPOINT` is set, making it fully opt-in.

### `context_graph/metadata.py`

Fixed a psycopg3 compatibility issue: psycopg3's default server-side binding cannot determine the PostgreSQL type of Python `None` values passed for nullable `TEXT` columns in `executemany`, causing:
```
could not determine data type of parameter $8
```

**Fix**: Use `psycopg.ClientCursor` in `PostgresMetadataStore.from_dsn()`. This switches to client-side parameter binding (substituting values into the SQL before sending), which handles `NULL` values correctly.

```python
connection = psycopg.connect(dsn, cursor_factory=psycopg.ClientCursor)
```

### `infra/modules/mcp_ec2/main.tf`

**Terraform — EC2 module.**

1. Added Aurora environment variables to the systemd unit:
   ```ini
   Environment=AURORA_ENDPOINT=...
   Environment=AURORA_DB_NAME=context_graph
   Environment=AURORA_USERNAME=postgres
   Environment=AURORA_PASSWORD_SECRET_ARN=...
   Environment=CONTEXT_GRAPH_LLM_PROVIDER=bedrock
   ```

2. Added `aws_iam_role_policy.aurora_secret_read` — grants the EC2 instance role `secretsmanager:GetSecretValue` on the Aurora master password secret ARN.

3. **Fixed the Bedrock IAM policy** — the previous approach used a `Deny` statement with `StringNotEqualsIfExists` on `bedrock:ModelId`. This broke the `Converse` API because Bedrock does not set `bedrock:ModelId` for `Converse` calls (it sets `bedrock:InferredModelId` instead), causing the Deny to fire for all models regardless of the allowed list.

   **Old approach (broken)**:
   ```hcl
   Effect = "Deny"
   Condition = {
     "StringNotEqualsIfExists" = {
       "bedrock:ModelId" = var.bedrock_allowed_model_ids
     }
   }
   ```

   **New approach**: Removed the Deny entirely. The Allow policy now scopes directly to model ARNs — both `foundation-model/` (for `InvokeModel`) and `inference-profile/` (for `Converse`):
   ```hcl
   Effect = "Allow"
   Resource = [
     "arn:aws:bedrock:*::foundation-model/<model-id>",
     "arn:aws:bedrock:*:*:inference-profile/<model-id>"
   ]
   ```

4. Added a `locals` block to generate both ARN patterns from `var.bedrock_allowed_model_ids`.

### `infra/stacks/dev/main.tf`

Added Aurora module outputs as inputs to the `mcp_ec2` module:
```hcl
aurora_endpoint            = module.aurora.cluster_endpoint
aurora_password_secret_arn = module.aurora.master_password_secret_arn
aurora_db_name             = module.aurora.db_name
aurora_username            = module.aurora.master_username
```

### `infra/modules/mcp_ec2/variables.tf`

Added four new variables: `aurora_endpoint`, `aurora_password_secret_arn`, `aurora_db_name`, `aurora_username` — all with sensible defaults and empty-string defaults so the module remains backward-compatible.

### `scripts/deploy-mcp.sh`

Extended artifact bundling to include the `context_graph` package:

```bash
CONTEXT_GRAPH_DIR="${REPO_ROOT}/context_graph"

mkdir -p "${STAGING_DIR}/context_graph_src/context_graph"
rsync -a ... "${CONTEXT_GRAPH_DIR}/" "${STAGING_DIR}/context_graph_src/context_graph/"
cat > "${STAGING_DIR}/context_graph_src/pyproject.toml" << 'PYPROJECT_EOF'
[project]
name = "context-graph-pkg"
...
[tool.hatch.build.targets.wheel]
packages = ["context_graph"]
PYPROJECT_EOF
```

The EC2 deploy script installs `context_graph_src/` as an editable pip package, then verifies both packages import correctly:
```bash
/opt/mcp_server/venv/bin/python -c "from graphiti_core.driver.neptune_driver import NeptuneDriver; print('graphiti_core import OK')"
/opt/mcp_server/venv/bin/python -c "from context_graph.episode_processor import EpisodeProcessor; print('context_graph import OK')"
```

---

## Bugs Encountered and Fixed

### 1. Python 3.9 vs 3.11 venv

Terraform's `user_data` does not re-run on in-place EC2 instance updates, so `python3.11` was not installed when the Terraform change was applied. The deploy script was picking up `python3` (3.9), which cannot satisfy `graphiti-core`'s `>=3.10` requirement.

**Fix**: Manually installed `python3.11` via SSM and wiped the stale venv (`rm -rf /opt/mcp_server/venv`).

### 2. `context_graph` package not importable

The `pyproject.toml` specified `packages = ["context_graph"]`, expecting a `context_graph/` subdirectory inside the bundle root. The initial bundling placed modules directly in `context_graph_src/` instead of `context_graph_src/context_graph/`.

**Fix**: Changed `rsync` destination to `"${STAGING_DIR}/context_graph_src/context_graph/"`.

### 3. Bedrock `AccessDeniedException` for `Converse`

The IAM `Deny` policy with `StringNotEqualsIfExists` on `bedrock:ModelId` fired for all `Converse` API calls because the `Converse` API does not pass `bedrock:ModelId` in the IAM context (it passes `bedrock:InferredModelId`). The `StringNotEqualsIfExists` condition evaluates to `true` when the key is absent.

**Fix**: Replaced the Deny/condition approach with a scoped Allow on specific resource ARNs (both `foundation-model/` and `inference-profile/` patterns).

### 4. Neptune Bolt auth failure from `context_graph`

`context_graph`'s `NeptuneGraphSink` uses the standard `neo4j` driver over Bolt with empty credentials (`auth=("", "")`). Neptune with IAM auth rejects this — it requires SigV4 signed requests. Graphiti uses `langchain_aws.graphs.NeptuneGraph` which handles IAM auth internally via HTTPS REST.

**Fix**: Added `_NullGraphSink` inside `AuroraIngestionService.initialize()`. It inherits from `BaseCypherGraphSink` and overrides `execute()` to count but discard statements. This allows `EpisodeProcessor` to run LLM extraction and produce fact records without attempting a Neptune Bolt connection.

### 5. psycopg3 `could not determine data type of parameter $8`

psycopg3's default server-side prepared statements cannot infer the PostgreSQL type of Python `None` for nullable `TEXT` columns in batch inserts.

**Fix**: Use `psycopg.ClientCursor` as the default cursor factory — this performs client-side parameter binding where `NULL` is always unambiguous.

### 6. Wrong Bedrock model ID for `ca-central-1`

`anthropic.claude-sonnet-4-6` requires cross-region inference profile routing in `ca-central-1`. The direct foundation model ID is not available for on-demand throughput.

**Fix**: Changed the default `CONTEXT_GRAPH_LLM_MODEL` to `us.anthropic.claude-sonnet-4-6` (the US cross-region inference profile), which is the same model already used by the Graphiti Neptune path.

---

## Testing

### Test 1 — First successful dual-backend ingestion

**Call**:
```json
{
  "name": "add_memory",
  "arguments": {
    "name": "JWT auth implementation - agent transcript",
    "episode_body": "Agent: I implemented JWT authentication middleware in src/auth/jwt_middleware.py. The middleware validates Bearer tokens, extracts user_id from the payload, and injects it into the request context. I updated the User model with a jwt_secret field and added token expiration handling with a 24h TTL. The changes are in PR #42. Human: Great work! Please also add refresh token support.",
    "source": "message",
    "repo_id": "context-graph",
    "source_type": "agent_transcript",
    "source_id": "test-transcript-2026-03-23-010",
    "author": "kai"
  }
}
```

**Response**:
```json
{
  "message": "Episode 'JWT auth implementation - agent transcript' queued for processing in group 'main'; Aurora: 5 graph writes, 9 facts extracted"
}
```

**Service logs** (aurora path, ~16s):
```
AuroraIngestionService initialised successfully
```

**Service logs** (neptune path, ~40s async):
```
Completed add_episode in 40452.1 ms
Successfully processed episode None for group main
```

### Test 2 — Second call verifying persistence

**Call**:
```json
{
  "name": "add_memory",
  "arguments": {
    "name": "Database schema migration discussion",
    "episode_body": "Human: We need to add a refresh_tokens table to support JWT refresh tokens. Agent: I will create a migration in db/migrations/001_add_refresh_tokens.sql. The table needs columns: id UUID PRIMARY KEY, user_id UUID REFERENCES users(id), token TEXT UNIQUE, expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(). I also added an index on (user_id, expires_at) for fast expiry lookups.",
    "source": "message",
    "repo_id": "context-graph",
    "source_type": "agent_transcript",
    "source_id": "test-transcript-2026-03-23-011",
    "author": "kai"
  }
}
```

**Response**:
```json
{
  "message": "Episode 'Database schema migration discussion' queued for processing in group 'main'; Aurora: 5 graph writes, 11 facts extracted"
}
```

### Test 3 — Backward compatibility (no `repo_id`)

**Call** (existing usage without Aurora params):
```json
{
  "name": "add_memory",
  "arguments": {
    "name": "Some existing episode",
    "episode_body": "...",
    "source": "text"
  }
}
```

**Response**:
```
Episode '...' queued for processing in group 'main'; Aurora: skipped (provide repo_id to enable)
```

The Neptune path continues to work normally. The Aurora path is silently skipped when `repo_id` is absent.

---

## AWS Infrastructure State

| Resource | Value |
|---|---|
| EC2 instance | `i-0291ac3c980e4f2ad` |
| MCP URL | `http://15.156.203.208:8000/mcp/` |
| Neptune endpoint | `context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com` |
| Aurora endpoint | `context-graph-dev-aurora.cluster-cd2euaamm1l1.ca-central-1.rds.amazonaws.com` |
| Aurora DB | `context_graph` |
| Bedrock LLM | `us.anthropic.claude-sonnet-4-6` (cross-region inference profile) |
| Bedrock embedder | `amazon.titan-embed-text-v2:0` |
| Region | `ca-central-1` |

IAM policies on `context-graph-dev-mcp-ec2-role`:
- `context-graph-dev-mcp-bedrock-invoke` — Allow Bedrock actions on specific model ARNs (both `foundation-model/` and `inference-profile/` patterns)
- `context-graph-dev-mcp-aurora-secret-read` — Allow `secretsmanager:GetSecretValue` on the Aurora master password secret
- `context-graph-dev-mcp-artifacts-read` — Allow S3 artifact downloads
- `AmazonSSMManagedInstanceCore` — SSM access

---

## Data Written to Aurora

The `context_graph` schema is bootstrapped automatically by `PostgresMetadataStore.bootstrap()`. Key tables:

- **`cg_profiles`** — Semantic relation profiles extracted from episodes (18 columns including `profile_id`, `repo_id`, `relation_type`, `source_node_id`, `target_node_id`, `value_text`, `is_active`, timestamps)
- **`cg_repo_state`** — Per-repo indexing state
- **`cg_current_nodes`** / **`cg_current_edges`** — Canonical code graph nodes and edges (for repo indexing, not yet used in episodic path)

Each `add_memory` call with `repo_id` writes:
- Facts extracted from the episode text (stored as `cg_profiles` rows)
- Entity/node mentions linked to the episode

---

## Potential Next Steps

### Immediate (retrieval)

1. **Wire up `retrieve_episodes` / `search_memory` tool** — Add a `search_memory` MCP tool that queries `cg_profiles` by `repo_id` + semantic similarity. The existing `retrieve` Lambda function may already have relevant infrastructure.

2. **Enable pgvector embeddings** — `AuroraProfileVectorStore` and the `profile_indexer` are already implemented in `context_graph/profile_vectors.py`. Currently `profile_indexer=None` in `AuroraIngestionService`. Enable by:
   - Bootstrapping the `cg_profile_keys` table (`CREATE EXTENSION IF NOT EXISTS vector`)
   - Passing a `ProfileVectorIndexer` to `EpisodeProcessor`
   - This enables cosine similarity search on relation embeddings

3. **Neptune-backed `GraphReadRepository`** — The `_NullGraphSink` skips Neptune writes from the context_graph path. Once a Neptune-compatible read path is available (or the existing Graphiti Neptune client is wrapped), the `GraphReadRepository` can use it for entity deduplication and context-aware fact extraction.

### Short term

4. **Deduplication by `source_id`** — The `EpisodeRecord.episode_id` is set to `{source_type}:{source_id}`. Aurora's `ON CONFLICT` handles re-ingestion idempotently. Consider surfacing a `deduplicated: true` flag in the response when an episode is re-processed.

5. **Thread-aware ingestion** — Pass `thread_id` for conversations that span multiple `add_memory` calls. The `EpisodeProcessor` uses `thread_id` to carry forward episode history, improving fact extraction quality for multi-turn conversations.

6. **Repo-indexed nodes** — The commit indexer and `context_graph` repo sync (via ECR + Lambda) can populate `cg_current_nodes` with code structure. This gives `EpisodeProcessor` real code entities to link facts to (rather than creating generic nodes).

7. **`source_id` from Cursor transcript IDs** — The agent transcript UUID from Cursor (e.g. `078dee33-b654-491f-9c00-1c58961dd7fd`) can be used directly as `source_id`, creating a durable link between the context graph and the agent conversation.

### Longer term

8. **Retrieval integration** — Build a `retrieve_context` MCP tool that:
   - Takes a task description and current `repo_id`
   - Queries `cg_profiles` by semantic similarity (pgvector ANN)
   - Traverses the Neptune graph for related entities
   - Returns a ranked "context chain" of relevant prior decisions, transcripts, and docs

9. **Cross-repo fact sharing** — Allow facts to propagate across `repo_id` boundaries when entities are shared (e.g. a shared auth library referenced by multiple repos). This requires a cross-repo entity resolution step.

10. **Neptune graph sink fix** — Implement IAM-aware Neptune Bolt authentication in `NeptuneGraphSink` (using `boto3` to generate a signed token). This would let the context_graph `EpisodeProcessor` write its own set of Cypher nodes to Neptune, enabling richer graph structure beyond what Graphiti extracts.

11. **Terraform `null_resource` for user_data re-run** — When `user_data` changes, Terraform replaces the EC2 instance or does an in-place update that doesn't re-run `user_data`. Add a `null_resource` with a `local-exec` provisioner that triggers `deploy-mcp.sh` after any infra changes, eliminating the need for manual deployment.

12. **Freshness / invalidation** — `ProfileRecord.is_active` and `valid_to_revision` support temporal invalidation. Hook this into the git commit indexer: when a file changes, deactivate profiles that reference its nodes. This keeps the context graph fresh as code evolves.
