# Context Graph MCP Server — Demo Test Results 3

INCLUDES AWS MIGRATION: Internal MCP Server + Neptune/Aurora Bug Fixes

**Date**: 2026-03-26
**MCP URL**: `http://15.156.203.208:8000/mcp`
**Backends**: Amazon Neptune (Graphiti) + Aurora PostgreSQL (context_graph)
**LLM**: `us.anthropic.claude-sonnet-4-6` (Bedrock cross-region inference profile)
**New in this demo**: Internal MCP server (`context_graph.server.mcp`) replaces vendored server; three Neptune/Aurora bugs fixed

---

## What Changed Since Demo 2

| Area | Demo 2 (2026-03-23) | Demo 3 (2026-03-26) |
|---|---|---|
| MCP server binary | `graphiti_mcp_server.py` (vendored graphiti-core) | `context_graph.server.mcp.server` (internal, fully owned) |
| `add_memory` Aurora response | `"Aurora: 5 graph writes, 14 facts extracted"` (sync) | `"Aurora: queued for background ingestion"` (async) |
| Aurora fact counts | Returned in HTTP response | Written to server logs only |
| `source_type="agent_transcript"` | `ValueError: 'agent_transcript' is not a valid EpisodeType` | ✅ works — variant added to `EpisodeType` enum |
| Neptune list parameters (`$terms`) | `MalformedQueryException: Type mismatch: expected List<T> but was String` | ✅ fixed — `_sanitize_parameters` inlines primitive lists as Cypher literals |
| `search_context_nodes` Cypher | List comprehension `[term IN $terms WHERE ... ENDS WITH '.' + term]` | ✅ fixed — `UNWIND $terms AS term` + `last(split(lqualified, '.')) = term` |
| Neptune facts on `main` group | Fully queryable | ✅ still fully queryable (cross-session persistence) |

### Key behaviour change: Aurora ingestion is now fully async

In Demo 2, the old vendored server called `AuroraIngestionService.ingest_episode_sync` synchronously during the `add_memory` HTTP request, so the response included the extraction count immediately. In the new internal server, both Neptune and Aurora are queued as background `asyncio` tasks. The HTTP response returns immediately with `"Aurora: queued for background ingestion"`, and the fact counts appear in the EC2 systemd service logs once Bedrock finishes extraction (~10–30s later).

This means `add_memory` is now faster to return (~0.1–0.5s vs ~10s in Demo 2 when Aurora was blocking the response), but callers can no longer read the fact count from the MCP response.

---

## Server Status

```
get_status → { "status": "ok", "message": "Graphiti MCP server is running and connected to neptune database" }
```

All 10 tools registered (same set as Demo 2).

---

## Episodes Ingested via `add_memory`

Four episodes were added to group `csc490-demo-3`, documenting the migration work itself.

---

### Episode 1 — Internal Server Migration

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Migration: internal MCP server deployed to EC2` |
| `group_id` | `csc490-demo-3` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo3-transcript-001` |
| `author` | `kai` |

**Response**:
```
Episode 'Migration: internal MCP server deployed to EC2' queued for processing in group 'csc490-demo-3';
Aurora: queued for background ingestion
```

**Aurora result** (from logs, ~40s later):
```
Aurora ingestion completed for repo_id=context-graph source_id=demo3-transcript-001 with 5 statements and 22 facts
```

---

### Episode 2 — Neptune List Parameter Bug Fix

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Bug fix: Neptune list parameter rejection` |
| `group_id` | `csc490-demo-3` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo3-transcript-002` |

**Response**:
```
Episode 'Bug fix: Neptune list parameter rejection' queued for processing in group 'csc490-demo-3';
Aurora: queued for background ingestion
```

**Aurora result** (logs):
```
Aurora ingestion completed for repo_id=context-graph source_id=demo3-transcript-002 with 5 statements and 11 facts
```

---

### Episode 3 — EpisodeType and Async Aurora

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Bug fix: agent_transcript EpisodeType and Aurora background ingestion` |
| `group_id` | `csc490-demo-3` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo3-transcript-003` |

**Response**:
```
Episode 'Bug fix: agent_transcript EpisodeType and Aurora background ingestion' queued for processing in group 'csc490-demo-3';
Aurora: queued for background ingestion
```

**Aurora result** (logs):
```
Aurora ingestion completed for repo_id=context-graph source_id=demo3-transcript-003 with 5 statements and 16 facts
```

---

### Episode 4 — AWS Deployment Status

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `search_memory verified on AWS with dual-backend retrieval` |
| `group_id` | `csc490-demo-3` |
| `source_type` | `text` |
| `source_id` | `demo3-status-001` |

**Response**:
```
Episode 'search_memory verified on AWS with dual-backend retrieval' queued for processing in group 'csc490-demo-3';
Aurora: queued for background ingestion
```

**Aurora result** (logs):
```
Aurora ingestion completed for repo_id=context-graph source_id=demo3-status-001 with 5 statements and 9 facts
```

---

### Ingestion Summary

| Episode | Aurora statements | Aurora facts extracted |
|---|---|---|
| Migration: internal MCP server deployed to EC2 | 5 | 22 |
| Bug fix: Neptune list parameter rejection | 5 | 11 |
| Bug fix: agent_transcript EpisodeType and Aurora background ingestion | 5 | 16 |
| search_memory verified on AWS with dual-backend retrieval | 5 | 9 |
| **Total** | **20** | **58** |

All 4 episodes were also queued for async Neptune processing.

---

## Retrieval Results

### Test 1: `search_memory` — Aurora only (`max_facts=0`)

**Query**: `"Neptune list parameter bug fix Cypher inline"`
**Parameters**: `repo_id="context-graph", max_facts=0, max_profiles=5`

```json
{
  "message": "Found 5 profiles for: 'Neptune list parameter bug fix Cypher inline'",
  "facts": [],
  "profiles": [
    {
      "profile_id": "profile:context-graph:semantic_fact_profile:289ea735-...",
      "relation_type": "USED",
      "value_text": "search_context_nodes previously used a Cypher list comprehension with a $terms list parameter, which Neptune rejected",
      "source_node_id": "entity:context-graph:efa21b41...",
      "target_node_id": "entity:context-graph:71670cae...",
      "is_active": true,
      "score": 1.0,
      "metadata": {
        "created_at": "2026-03-26 01:07:57.831549+00:00",
        "episode_ids": ["episode:context-graph:agent_transcript:demo3-transcript-002"],
        "fact_id": "289ea735-7671-45c3-a6f1-757fa75b0786",
        "source_name": "search_context_nodes",
        "target_name": "Cypher list comprehension"
      }
    },
    {
      "relation_type": "INLINES_AS",
      "value_text": "_sanitize_parameters was updated to inline primitive-only lists as Cypher array literals before sending parameters to Neptune",
      "score": 1.0,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:demo3-transcript-002"],
        "fact_id": "65a97c6f-b4a1-4907-9963-595885bb0739",
        "source_name": "_sanitize_parameters method",
        "target_name": "Cypher array literals"
      }
    },
    {
      "relation_type": "RAISES",
      "value_text": "Neptune raises a MalformedQueryException with 'Type mismatch: expected List<T> but was String' when list-typed parameters are passed via the execute_open_cypher_query API",
      "score": 1.0,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:demo3-transcript-002"],
        "fact_id": "81b93e7d-d82d-430a-b14b-210ac06803b2",
        "source_name": "Neptune",
        "target_name": "MalformedQueryException"
      }
    },
    {
      "relation_type": "USES",
      "value_text": "Neptune provides entity-relation facts via Graphiti cosine similarity search.",
      "score": 1.0,
      "metadata": {
        "episode_ids": ["episode:context-graph:text:demo3-status-001"],
        "fact_id": "9fdd5b9c-95ce-425f-8eac-9c9392244bf3",
        "source_name": "Neptune",
        "target_name": "cosine similarity search"
      }
    },
    {
      "relation_type": "SUPPORTED_BY",
      "value_text": "Lists of dicts used for UNWIND batch operations are left as JSON parameters since Neptune handles those correctly",
      "score": 1.0,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:demo3-transcript-002"],
        "fact_id": "b9c03657-b60f-432c-bda6-221e9111e547",
        "source_name": "UNWIND batch operations",
        "target_name": "Neptune"
      }
    }
  ]
}
```

**Key observations**:
- All 5 profiles score `1.000` — semantically exact match
- The system indexed the bug fix itself: `_sanitize_parameters INLINES_AS Cypher array literals`, `Neptune RAISES MalformedQueryException`
- `UNWIND batch operations SUPPORTED_BY Neptune` — the fix correctly distinguishes which list types to inline vs. pass as parameters
- Profile `episode_ids` use the new `episode:context-graph:agent_transcript:` prefix format (internal server), vs. the old `agent_transcript:` bare prefix in Demo 2

---

### Test 2: `search_memory` — Combined Neptune + Aurora

**Query**: `"internal MCP server context_graph deploy EC2"`
**Parameters**: `group_ids=["csc490-demo-3"], repo_id="context-graph", max_facts=5, max_profiles=4`

```json
{
  "message": "Found 3 profiles for: 'internal MCP server context_graph deploy EC2'",
  "facts": [],
  "profiles": [
    {
      "relation_type": "ACCESSIBLE_VIA",
      "value_text": "The Neptune endpoint is accessible via EC2.",
      "score": 1.0,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:demo3-transcript-001"],
        "source_name": "Amazon Neptune",
        "target_name": "EC2"
      }
    },
    {
      "relation_type": "RUNS_ON",
      "value_text": "The Context Graph MCP Server is running on EC2.",
      "score": 1.0,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:migration-smoke-test-004"],
        "source_name": "Context Graph MCP Server",
        "target_name": "EC2"
      }
    },
    {
      "relation_type": "RUNS_ON",
      "value_text": "The Context Graph MCP Server is running on Amazon EC2.",
      "score": 1.0,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:migration-smoke-test-002"],
        "source_name": "Context Graph MCP Server",
        "target_name": "Amazon EC2"
      }
    }
  ]
}
```

**Note**: Neptune returned 0 facts for `csc490-demo-3` — Neptune processing for this group's episodes was still in the queue at retrieval time (Neptune entity extraction takes ~40s per episode and each group has its own serial queue). Aurora profiles are available immediately once background ingestion completes. The Neptune facts from the `main` group (Demo 1/2 data) remain fully queryable (see Test 4).

Also note two nearly-identical `RUNS_ON` profiles (`EC2` vs `Amazon EC2`) — Aurora's deduplication is fuzzy; Graphiti's Neptune path uses LLM-driven entity resolution to merge these, which will collapse them once Neptune processing completes.

---

### Test 3: `search_memory` — Aurora only, EpisodeType bug fix

**Query**: `"agent_transcript EpisodeType Aurora async background ingestion"`
**Parameters**: `repo_id="context-graph", max_facts=0, max_profiles=5`

```json
{
  "message": "Found 4 profiles for: 'agent_transcript EpisodeType Aurora async background ingestion'",
  "facts": [],
  "profiles": [
    {
      "relation_type": "RAISED_FOR",
      "value_text": "The ValueError was raised because agent_transcript is not a valid EpisodeType.",
      "score": 0.729,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:demo3-transcript-003"],
        "source_name": "ValueError",
        "target_name": "agent_transcript"
      }
    },
    {
      "relation_type": "FIXED_AND_DEPLOYED_IN",
      "value_text": "The agent_transcript EpisodeType bug has been fixed and deployed in the Context Graph MCP Server.",
      "score": 0.729,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:migration-smoke-test-002"],
        "source_name": "agent_transcript EpisodeType Bug",
        "target_name": "Context Graph MCP Server"
      }
    },
    {
      "relation_type": "MISSING_VARIANT",
      "value_text": "The EpisodeType enum did not include agent_transcript as a variant, which caused the ValueError.",
      "score": 0.729,
      "metadata": {
        "episode_ids": ["episode:context-graph:agent_transcript:demo3-transcript-003"],
        "source_name": "EpisodeType",
        "target_name": "agent_transcript"
      }
    },
    {
      "relation_type": "PART_OF",
      "value_text": "Entity NeptuneDriver.execute_query part of Entity Neptune episode processing pipeline.",
      "score": 0.618,
      "metadata": {
        "episode_ids": ["text:neptune-fix-test-001"],
        "source_name": "NeptuneDriver.execute_query",
        "target_name": "Neptune episode processing pipeline"
      }
    }
  ]
}
```

**Key observations**:
- The fix itself is indexed: `EpisodeType MISSING_VARIANT agent_transcript`, `ValueError RAISED_FOR agent_transcript`, `bug FIXED_AND_DEPLOYED_IN Context Graph MCP Server`
- The 4th result (`PART_OF`, score=0.618) is a lower-relevance cross-session match from the Demo 2 era — shows the HNSW index returning semantically related but not exact hits at the tail of the result set
- Scores are ~0.73 rather than 1.0 — the query contains the word "async" which is in the episode but not directly in any single extracted fact; the semantic match is strong but not perfect

---

### Test 4: `search_memory_facts` — Cross-session `main` group Neptune facts

**Query**: `"JWT authentication middleware implementation"`
**Parameters**: `group_ids=["main"], max_facts=3`

```json
{
  "message": "Facts retrieved successfully",
  "facts": [
    {
      "uuid": "943051c0-3925-4e72-8f2f-39d5465182c1",
      "group_id": "main",
      "name": "LOCATED_IN",
      "fact": "JWT authentication middleware is implemented in src/auth/jwt_middleware.py.",
      "episodes": [
        "1739e4e9-e354-42dd-94f6-3a9d6a5f3742",
        "e8c8647b-3b37-4576-ab14-e5da55ff9010",
        "d99f3fae-02a7-4c9f-bb48-0509274aeefe"
      ],
      "created_at": "2026-03-23T03:22:08.932363Z",
      "valid_at": "2026-03-23T03:21:15.114634Z",
      "invalid_at": null
    },
    {
      "uuid": "453f4813-8c07-4b38-bd6b-15f98f9f59d7",
      "group_id": "main",
      "name": "INCLUDES",
      "fact": "JWT authentication middleware includes token expiration handling.",
      "episodes": [
        "e8c8647b-3b37-4576-ab14-e5da55ff9010",
        "d99f3fae-02a7-4c9f-bb48-0509274aeefe"
      ],
      "created_at": "2026-03-23T03:27:03.344150Z",
      "valid_at": "2026-03-23T03:26:45.666129Z",
      "invalid_at": null
    }
  ]
}
```

The `main` group Neptune facts from Demo 1 (2026-03-23, 3 days ago) are fully intact after the server swap — the new internal server uses the same Neptune cluster and the same Graphiti index, so no data was lost.

---

## Key Takeaways (Demo 3 vs Demo 2)

| Capability | Demo 2 (Phase 5) | Demo 3 (AWS Migration) |
|---|---|---|
| MCP server | Vendored `graphiti_mcp_server.py` | ✅ Internal `context_graph.server.mcp` |
| `add_memory` Aurora latency | ~10s (blocking Bedrock call) | ✅ ~0.1s (returns immediately, background) |
| `add_memory` Aurora feedback | Fact count in response | Fact count in server logs |
| `source_type="agent_transcript"` | `ValueError` crash | ✅ accepted — `EpisodeType` extended |
| Neptune `$terms` list param | `MalformedQueryException` | ✅ inlined as Cypher literal |
| `search_context_nodes` query | List comprehension + `ENDS WITH '.' + term` | ✅ `UNWIND` + `last(split(..., '.'))` |
| Neptune facts persistence | ✅ | ✅ (cross-server-swap, cross-session) |
| Aurora profiles persistence | ✅ | ✅ (3+ days of accumulated profiles searchable) |
| Aurora profile `episode_id` prefix | `agent_transcript:source-id` | `episode:repo:source_type:source-id` (more structured) |
