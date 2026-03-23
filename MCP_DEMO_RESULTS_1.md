# Context Graph MCP Server — Demo Test Results

INCLUDES LIGHTRAG PHASE 1-4

**Date**: 2026-03-22  
**MCP URL**: `http://15.156.203.208:8000/mcp/`  
**Backends**: Amazon Neptune (Graphiti) + Aurora PostgreSQL (context_graph)  
**LLM**: `us.anthropic.claude-sonnet-4-6` (Bedrock cross-region inference profile)

---

## Episodes Ingested via `add_memory`

Four episodes were added to group `csc490-demo`, simulating a coding agent working on the context-graph project itself. Each call writes to both Neptune (async entity/fact extraction) and Aurora (synchronous semantic fact extraction via Bedrock).

---

### Episode 1 — Architecture Decision

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Architecture decision: dual-backend storage` |
| `group_id` | `csc490-demo` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo-transcript-001` |
| `thread_id` | `demo-thread-arch` |
| `author` | `kai` |

**Episode body**:

> Human: We need to decide how to store context graph data for our MCP server. We want both a structured knowledge graph and fast semantic retrieval.
>
> Agent: I recommend a dual-backend approach. Neptune (via Graphiti) will store the knowledge graph with entities and facts — entity extraction, deduplication, and temporal edges give us graph traversal. Aurora PostgreSQL with pgvector will store semantic fact profiles extracted by context_graph's EpisodeProcessor — this gives us cosine similarity search over embeddings for LightRAG-style retrieval. The two backends are complementary: Neptune for "what entities are related" queries and Aurora for "what context is semantically similar to this task" queries.
>
> Human: Sounds good. Who owns the Neptune write path?
>
> Agent: Graphiti's QueueService owns the Neptune write path — it serializes episodes per group_id and uses Bedrock claude-sonnet-4-6 via an inference profile for extraction. The Aurora path is a synchronous call via asyncio.to_thread so it doesn't block the MCP response.

**Response**:
```
Episode 'Architecture decision: dual-backend storage' queued for processing in group 'csc490-demo';
Aurora: 5 graph writes, 14 facts extracted
```

---

### Episode 2 — Implementation Details

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Implementation: AuroraIngestionService in add_memory` |
| `group_id` | `csc490-demo` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo-transcript-002` |
| `thread_id` | `demo-thread-impl` |
| `author` | `kai` |

**Episode body**:

> Human: Please implement the add_memory MCP tool with Aurora ingestion support.
>
> Agent: I've added the AuroraIngestionService class to graphiti_mcp_server.py. It reads AURORA_ENDPOINT, AURORA_DB_NAME, AURORA_USERNAME, and AURORA_PASSWORD_SECRET_ARN from environment variables set by Terraform. The password is fetched from AWS Secrets Manager at initialization using the EC2 instance IAM role — no credentials in code. I also added a _NullGraphSink that inherits from BaseCypherGraphSink and discards Cypher statements — this lets EpisodeProcessor run Bedrock-powered fact extraction without attempting a Neptune Bolt connection, since Neptune IAM auth rejects the plain neo4j driver. The add_memory tool now accepts five new optional parameters: repo_id, source_type, source_id, author, thread_id. When repo_id is provided, the Aurora path runs concurrently via asyncio.to_thread.
>
> Human: What happens if Aurora is down?
>
> Agent: The Aurora path is non-fatal. Errors are caught and logged, and the Neptune path continues normally. The response message appends "Aurora: skipped (error)" so the caller can see what happened.

**Response**:
```
Episode 'Implementation: AuroraIngestionService in add_memory' queued for processing in group 'csc490-demo';
Aurora: 5 graph writes, 24 facts extracted
```

---

### Episode 3 — Bug Fix: psycopg3 NULL handling

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Bug fix: psycopg3 ClientCursor for NULL handling in Aurora batch inserts` |
| `group_id` | `csc490-demo` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo-transcript-003` |
| `thread_id` | `demo-thread-bugs` |
| `author` | `kai` |

**Episode body**:

> Human: We're getting an error: "could not determine data type of parameter $8" when batch inserting profiles into Aurora.
>
> Agent: This is a psycopg3 compatibility issue. psycopg3's default server-side prepared statements cannot infer the PostgreSQL type of Python None values for nullable TEXT columns in executemany calls. The fix is to use psycopg.ClientCursor as the default cursor factory in PostgresMetadataStore.from_dsn(). Client-side binding substitutes parameter values into the SQL string before sending to the server, so NULL is always unambiguous. I've applied the patch to context_graph/metadata.py.
>
> Human: Is there a risk of SQL injection with client-side binding?
>
> Agent: Low risk here because all values come from trusted internal data — Bedrock extraction results and our own episode records. But it's worth noting for future contributors. The psycopg documentation recommends server-side binding when possible; this is a pragmatic workaround for the NULL-typing limitation.

**Response**:
```
Episode 'Bug fix: psycopg3 ClientCursor for NULL handling in Aurora batch inserts' queued for processing in group 'csc490-demo';
Aurora: 5 graph writes, 10 facts extracted
```

---

### Episode 4 — Bug Fix: IAM / Bedrock Converse

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `IAM fix: Bedrock Converse AccessDeniedException with StringNotEqualsIfExists` |
| `group_id` | `csc490-demo` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo-transcript-004` |
| `thread_id` | `demo-thread-bugs` |
| `author` | `kai` |

**Episode body**:

> Human: Bedrock is returning AccessDeniedException for all Converse API calls even though we allow specific model ARNs.
>
> Agent: The IAM policy bug is subtle. The old approach used a Deny statement with StringNotEqualsIfExists on bedrock:ModelId. The Converse API does not set bedrock:ModelId in the IAM context — it sets bedrock:InferredModelId instead. StringNotEqualsIfExists evaluates to true when the key is absent, so the Deny fires for every Converse call regardless of model. The fix is to remove the Deny entirely and replace it with a scoped Allow on specific resource ARNs, covering both foundation-model/ ARN patterns for InvokeModel and inference-profile/ ARN patterns for Converse. I've updated infra/modules/mcp_ec2/main.tf with a locals block that generates both ARN patterns from var.bedrock_allowed_model_ids.
>
> Human: Will this work for the cross-region inference profile for claude-sonnet-4-6?
>
> Agent: Yes. In ca-central-1, the on-demand throughput for anthropic.claude-sonnet-4-6 is not available directly, so we use the us.anthropic.claude-sonnet-4-6 cross-region inference profile. The ARN pattern for that is arn:aws:bedrock:*:*:inference-profile/us.anthropic.claude-sonnet-4-6, which the new scoped Allow covers.

**Response**:
```
Episode 'IAM fix: Bedrock Converse AccessDeniedException with StringNotEqualsIfExists' queued for processing in group 'csc490-demo';
Aurora: 5 graph writes, 23 facts extracted
```

---

### Ingestion Summary

| Episode | Aurora graph writes | Aurora facts extracted |
|---|---|---|
| Architecture decision: dual-backend storage | 5 | 14 |
| Implementation: AuroraIngestionService in add_memory | 5 | 24 |
| Bug fix: psycopg3 ClientCursor for NULL handling | 5 | 10 |
| IAM fix: Bedrock Converse AccessDeniedException | 5 | 23 |
| **Total** | **20** | **71** |

All 4 episodes are also queued for async Neptune processing (~40s per episode). The MCP tool returns immediately after queuing Neptune and completing the Aurora write.

---

## Retrieval Results

The following queries were run against the Neptune graph (group `main`, populated by prior test runs from the implementation testing phase). The `csc490-demo` group episodes will be available for retrieval once Neptune finishes processing them.

---

### `search_memory_facts`

**Query**: `"JWT authentication middleware implementation"`

```json
[
  {
    "uuid": "943051c0-3925-4e72-8f2f-39d5465182c1",
    "group_id": "main",
    "name": "LOCATED_IN",
    "fact": "JWT authentication middleware is implemented in src/auth/jwt_middleware.py.",
    "source_node_uuid": "c7c00715-16dc-4e09-8f02-efb19e724642",
    "target_node_uuid": "3519dd0a-461a-4fe2-8c3d-3201963a908b",
    "created_at": "2026-03-23T03:22:08.932363Z",
    "valid_at": "2026-03-23T03:21:15.114634Z",
    "invalid_at": null,
    "episodes": ["1739e4e9-e354-42dd-94f6-3a9d6a5f3742", "e8c8647b-3b37-4576-ab14-e5da55ff9010", "d99f3fae-02a7-4c9f-bb48-0509274aeefe"]
  },
  {
    "uuid": "453f4813-8c07-4b38-bd6b-15f98f9f59d7",
    "group_id": "main",
    "name": "INCLUDES",
    "fact": "JWT authentication middleware includes token expiration handling.",
    "source_node_uuid": "c7c00715-16dc-4e09-8f02-efb19e724642",
    "target_node_uuid": "4b11056b-c980-4b5b-8621-7001b1dc9694",
    "created_at": "2026-03-23T03:27:03.344150Z",
    "valid_at": "2026-03-23T03:26:45.666138Z",
    "invalid_at": null,
    "episodes": ["e8c8647b-3b37-4576-ab14-e5da55ff9010", "d99f3fae-02a7-4c9f-bb48-0509274aeefe"]
  }
]
```

**Query**: `"refresh tokens database migration schema"`

```json
[
  {
    "uuid": "c56ee13f-e1a4-43fb-bad5-e1c8b522e634",
    "group_id": "main",
    "name": "CREATED",
    "fact": "Agent created a migration file at db/migrations/001_add_refresh_tokens.sql for the refresh_tokens table.",
    "source_node_uuid": "1ea6ed94-9c78-47bd-944b-31823cfcc578",
    "target_node_uuid": "bf13ad63-d0b4-4cc0-9058-792877145988",
    "created_at": "2026-03-23T03:35:57.451829Z",
    "valid_at": "2026-03-23T03:35:33.498277Z",
    "invalid_at": null,
    "episodes": ["afee0711-eaa7-4125-abee-726e23e30156"]
  },
  {
    "uuid": "788e5484-4f03-4bb7-bf7a-fbbbb5ee0df9",
    "group_id": "main",
    "name": "DEFINES",
    "fact": "The migration file db/migrations/001_add_refresh_tokens.sql defines the refresh_tokens table.",
    "source_node_uuid": "bf13ad63-d0b4-4cc0-9058-792877145988",
    "target_node_uuid": "855b0849-5093-405b-9847-f57cfc29af58",
    "created_at": "2026-03-23T03:35:57.451843Z",
    "valid_at": "2026-03-23T03:35:33.498277Z",
    "invalid_at": null,
    "episodes": ["afee0711-eaa7-4125-abee-726e23e30156"]
  },
  {
    "uuid": "3cc2e307-dd15-4788-824c-e18fe7b74e13",
    "group_id": "main",
    "name": "SUPPORTS",
    "fact": "The refresh_tokens table is created to support JWT refresh tokens.",
    "source_node_uuid": "855b0849-5093-405b-9847-f57cfc29af58",
    "target_node_uuid": "7027ecbb-bbd4-418c-a227-65cf3ee76428",
    "created_at": "2026-03-23T03:35:57.451809Z",
    "valid_at": "2026-03-23T03:35:33.498277Z",
    "invalid_at": null,
    "episodes": ["afee0711-eaa7-4125-abee-726e23e30156"]
  }
]
```

---

### `search_nodes`

**Query**: `"JWT authentication token middleware"`

```json
[
  {
    "uuid": "c7c00715-16dc-4e09-8f02-efb19e724642",
    "name": "JWT authentication middleware",
    "labels": ["Entity"],
    "group_id": "main",
    "created_at": "2026-03-23T03:21:20.649906+00:00",
    "summary": "Agent implemented JWT authentication middleware. JWT authentication middleware is implemented in src/auth/jwt_middleware.py. JWT authentication middleware validates Bearer tokens. JWT authentication middleware extracts user_id from the token payload. JWT authentication middleware injects user_id into the request context. JWT authentication middleware includes token expiration handling with a 24-hour TTL. The JWT authentication middleware changes are contained in PR #42."
  },
  {
    "uuid": "3519dd0a-461a-4fe2-8c3d-3201963a908b",
    "name": "src/auth/jwt_middleware.py",
    "labels": ["Entity"],
    "group_id": "main",
    "created_at": "2026-03-23T03:21:20.649938+00:00",
    "summary": "JWT authentication middleware is implemented in src/auth/jwt_middleware.py."
  }
]
```

**Query**: `"refresh tokens SQL migration database table"`

```json
[
  {
    "uuid": "855b0849-5093-405b-9847-f57cfc29af58",
    "name": "refresh_tokens table",
    "labels": ["Entity"],
    "group_id": "main",
    "created_at": "2026-03-23T03:35:36.932661+00:00",
    "summary": "Human requested that a refresh_tokens table be added to support JWT refresh tokens. The refresh_tokens table has an id UUID column (PRIMARY KEY), a user_id UUID column (REFERENCES users(id)), a token TEXT column (UNIQUE), expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(). An index on (user_id, expires_at) was added for fast expiry lookups."
  },
  {
    "uuid": "bf13ad63-d0b4-4cc0-9058-792877145988",
    "name": "db/migrations/001_add_refresh_tokens.sql",
    "labels": ["Entity"],
    "group_id": "main",
    "created_at": "2026-03-23T03:35:36.932684+00:00",
    "summary": "Agent created a migration file at db/migrations/001_add_refresh_tokens.sql for the refresh_tokens table. The migration file defines the refresh_tokens table."
  }
]
```

---

### `get_episodes`

Episodes retrieved from group `main` (persisted from prior implementation test runs):

```json
[
  {
    "uuid": "1739e4e9-e354-42dd-94f6-3a9d6a5f3742",
    "name": "JWT auth implementation - agent transcript",
    "source": "message",
    "group_id": "main",
    "created_at": "2026-03-23T03:21:15.114643+00:00",
    "content": "Agent: I implemented JWT authentication middleware in src/auth/jwt_middleware.py. The middleware validates Bearer tokens, extracts user_id from the payload, and injects it into the request context. I updated the User model with a jwt_secret field and added token expiration handling with a 24h TTL. The changes are in PR #42. Human: Great work! Please also add refresh token support."
  },
  {
    "uuid": "e8c8647b-3b37-4576-ab14-e5da55ff9010",
    "name": "JWT auth implementation - agent transcript",
    "source": "message",
    "group_id": "main",
    "created_at": "2026-03-23T03:26:45.666138+00:00",
    "content": "Agent: I implemented JWT authentication middleware in src/auth/jwt_middleware.py. The middleware validates Bearer tokens, extracts user_id from the payload, and injects it into the request context. I updated the User model with a jwt_secret field and added token expiration handling with a 24h TTL. The changes are in PR #42. Human: Great work! Please also add refresh token support."
  },
  {
    "uuid": "d99f3fae-02a7-4c9f-bb48-0509274aeefe",
    "name": "JWT auth implementation - agent transcript",
    "source": "message",
    "group_id": "main",
    "created_at": "2026-03-23T03:30:56.021061+00:00",
    "content": "Agent: I implemented JWT authentication middleware in src/auth/jwt_middleware.py. The middleware validates Bearer tokens, extracts user_id from the payload, and injects it into the request context. I updated the User model with a jwt_secret field and added token expiration handling with a 24h TTL. The changes are in PR #42. Human: Great work! Please also add refresh token support."
  },
  {
    "uuid": "afee0711-eaa7-4125-abee-726e23e30156",
    "name": "Database schema migration discussion",
    "source": "message",
    "group_id": "main",
    "created_at": "2026-03-23T03:35:33.498290+00:00",
    "content": "Human: We need to add a refresh_tokens table to support JWT refresh tokens. Agent: I will create a migration in db/migrations/001_add_refresh_tokens.sql. The table needs columns: id UUID PRIMARY KEY, user_id UUID REFERENCES users(id), token TEXT UNIQUE, expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(). I also added an index on (user_id, expires_at) for fast expiry lookups."
  }
]
```

---

### `get_entity_edge` — Provenance Inspection

Fetching a specific edge by UUID to show the full provenance link (episode → fact):

**UUID**: `788e5484-4f03-4bb7-bf7a-fbbbb5ee0df9`

```json
{
  "uuid": "788e5484-4f03-4bb7-bf7a-fbbbb5ee0df9",
  "group_id": "main",
  "name": "DEFINES",
  "fact": "The migration file db/migrations/001_add_refresh_tokens.sql defines the refresh_tokens table.",
  "source_node_uuid": "bf13ad63-d0b4-4cc0-9058-792877145988",
  "target_node_uuid": "855b0849-5093-405b-9847-f57cfc29af58",
  "created_at": "2026-03-23T03:35:57.451843Z",
  "valid_at": "2026-03-23T03:35:33.498277Z",
  "invalid_at": null,
  "expired_at": null,
  "episodes": ["afee0711-eaa7-4125-abee-726e23e30156"],
  "attributes": {}
}
```

This edge links:
- **Source node** `bf13ad63...` → `db/migrations/001_add_refresh_tokens.sql`
- **Target node** `855b0849...` → `refresh_tokens table`
- **Backed by episode** `afee0711...` → `"Database schema migration discussion"` transcript

This is the "breadcrumb trail" property: you can trace any fact in the graph back to the exact conversation episode that produced it.

---

## Key Takeaways

| Capability | Result |
|---|---|
| Dual-backend write (Neptune + Aurora) | Both paths live end-to-end |
| Aurora fact extraction per episode | 10–24 facts per call via Bedrock |
| Neptune entity extraction | Named entities with accumulated summaries |
| Named relation types | `LOCATED_IN`, `INCLUDES`, `DEFINED`, `CREATED`, `SUPPORTS` |
| Temporal metadata | Every fact has `valid_at` / `invalid_at` timestamps |
| Provenance | Every fact edge links back to its source episode UUID |
| Non-blocking ingestion | Neptune queued async; Aurora via `asyncio.to_thread` |
| Fault isolation | Aurora errors do not block the Neptune path |
| Backward compatibility | Calls without `repo_id` skip Aurora silently |
