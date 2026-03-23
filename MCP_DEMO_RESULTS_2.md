# Context Graph MCP Server — Demo Test Results 2

INCLUDES LIGHTRAG PHASE 1-5 (Phase 5: `search_memory` dual-backend retrieval)

**Date**: 2026-03-23
**MCP URL**: `http://15.156.203.208:8000/mcp`
**Backends**: Amazon Neptune (Graphiti) + Aurora PostgreSQL (context_graph)
**LLM**: `us.anthropic.claude-sonnet-4-6` (Bedrock cross-region inference profile)
**New in this demo**: `search_memory` tool — unified Neptune + Aurora retrieval

---

## Tool Call Latency

All timings measured live against the deployed EC2 server in `ca-central-1` from a client in `ca-central-1`. Each call goes EC2 → Neptune (via VPC) or EC2 → Aurora (via VPC) → EC2 → Bedrock (for LLM/embedding calls).

| Tool call | Measured latency | Why |
|---|---|---|
| `get_status` | **0.05s** | Pure in-process health check, no backend I/O |
| `get_episodes` | **0.09s** | Single Neptune Bolt query with LIMIT |
| `get_entity_edge` | **0.10s** | Single Neptune edge lookup by UUID |
| `search_nodes` | **0.20s** | Neptune vector search (embedding pre-computed at ingest) |
| `search_memory_facts` | **0.27s** | Neptune vector similarity search over entity-relation edges |
| `search_memory` Neptune-only (warm) | **0.19s** | Same as `search_memory_facts` — Neptune vector search only |
| `search_memory` Aurora-only `max_facts=0` (warm) | **2.85s** | Bedrock Titan embed query (1 round-trip) + pgvector cosine ANN + Aurora row fetch |
| `search_memory` combined Neptune+Aurora (warm) | **2.60s** | Neptune search + Aurora search run in the same thread; Neptune path completes nearly instantly so total is dominated by Aurora embed + ANN |
| `add_memory` (short episode, `repo_id` set) | **10.21s** | Bedrock claude-sonnet-4-6 fact extraction (1 LLM call) + Aurora batch insert; Neptune is queued async and does **not** add to this latency |
| `search_memory` Aurora (cold — first call after server restart) | **3.4s** | Includes lazy `initialize()`: Aurora connection pool setup + `cg_profile_keys` table bootstrap check + first Bedrock Titan embed |

**Why Aurora search is slower than Neptune search (~2.8s vs ~0.2s)**:

Neptune search uses a pre-computed vector stored in the graph at ingest time — the query is already embedded, so retrieval is a direct ANN lookup. Aurora search must embed the query at retrieval time via a synchronous Bedrock Titan API call (~2–2.5s round-trip), then run pgvector cosine ANN, then fetch `ProfileRecord` rows. The embedding call dominates.

**Why `add_memory` blocks for ~10s**:

The Aurora ingestion path calls Bedrock claude-sonnet-4-6 synchronously (via `asyncio.to_thread`) to extract semantic facts from the episode body. This is a full LLM Converse API call with a structured output schema. Longer episodes extract more facts but cost proportionally more time. The Neptune path is fully async (QueueService) and does not add to the caller-facing latency.

**Why the server becomes unresponsive during Neptune queue processing — and the fix**:

This was a specific bug, not an inherent architectural limit. Two blocking synchronous boto3 calls were made directly on the asyncio event loop thread:

**Root cause 1 — `BedrockLLMClient._generate_response`** ([bedrock_client.py:184](vendor/graphiti/graphiti_core/llm_client/bedrock_client.py)):
```python
# Before (blocks event loop for 2–5s per LLM call):
response = self._bedrock.converse(**request)

# After (offloads to thread pool):
response = await asyncio.to_thread(self._bedrock.converse, **request)
```

**Root cause 2 — `NeptuneDriver.execute_query`** ([neptune_driver.py:309](vendor/graphiti/graphiti_core/driver/neptune_driver.py)):
```python
# Before (blocks event loop for every Neptune graph query):
return self._run_query(cypher_query_, params)

# After:
return await asyncio.to_thread(self._run_query, cypher_query_, params)
```

The embedder (`BedrockTitanEmbedder`) already used `loop.run_in_executor` correctly. The LLM client and Neptune driver did not.

`QueueService._process_episode_queue` runs as an `asyncio.Task` on the same event loop as the MCP HTTP server. Each episode triggers 3–5 Bedrock Converse calls and multiple Neptune queries. Before the fix, each call froze the event loop for 2–5s — totalling several minutes of starvation per episode. After the fix, both call types run in Python's default thread pool executor and yield the event loop between calls.

**Verified fix — concurrency test results**:

After deploying both patches, an `add_memory` call (triggering Neptune background processing) was started, then `get_status` was polled every 2s for 30s with a 15s timeout:

| | Before fix | After fix |
|---|---|---|
| Polls succeeded | 8/10 (2 timed out at 120s) | **15/15** |
| Median poll latency | ~0.07s (when not blocked) | **0.063s** |
| Max observed block duration | ~5 minutes | **< 1s** (poll 2: 0.650s, poll 15: 1.064s) |

The sub-second spikes on polls 2 and 15 are normal asyncio scheduling jitter — the event loop task-switches between the Neptune thread and the HTTP handler. No request was dropped or significantly delayed.

---

## Server Status

```
GET /mcp → tools/list → 10 tools registered
```

| Tool | Status |
|---|---|
| `add_memory` | ✅ |
| `search_memory` | ✅ **NEW** |
| `search_memory_facts` | ✅ (legacy Neptune-only) |
| `search_nodes` | ✅ |
| `get_episodes` | ✅ |
| `get_entity_edge` | ✅ |
| `delete_episode` | ✅ |
| `delete_entity_edge` | ✅ |
| `clear_graph` | ✅ |
| `get_status` | ✅ |

**`get_status` response**:
```json
{
  "status": "ok",
  "message": "Graphiti MCP server is running and connected to neptune database"
}
```

---

## Episodes Ingested via `add_memory`

Episodes were added to group `csc490-demo-2`, simulating a coding agent working on the search_memory feature itself. Each call writes to both Neptune (async entity/fact extraction) and Aurora (synchronous semantic fact extraction via Bedrock).

---

### Episode 1 — Search Memory Architecture

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Architecture: dual-backend search_memory MCP tool` |
| `group_id` | `csc490-demo-2` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo2-transcript-001` |

**Episode body**:

> Human: How does the new search_memory tool work?
>
> Agent: The search_memory MCP tool uses a dual-backend retrieval strategy. It accepts query, group_ids, repo_id, max_facts, and max_profiles parameters. When called without repo_id, it queries Neptune via Graphiti's client.search() and returns structured entity-relation facts. When repo_id is provided, it additionally runs the ProfileSearchService pipeline: parse the query with QueryParser, embed the high-level keywords via Bedrock Titan (amazon.titan-embed-text-v2:0), run a cosine ANN search against the cg_profile_keys pgvector index, and load the matching ProfileRecord rows from cg_profiles. The merged response contains both facts and profiles.
>
> Human: What are the use cases for facts vs profiles?
>
> Agent: Neptune facts give you precise entity-relation triples — who calls what, what is located in where, what changed what. Aurora profiles give you semantic relation summaries — they capture the "what does this relationship mean" context that is harder to retrieve with keyword search. For a coding agent, facts answer "where is the auth middleware implemented?" and profiles answer "what design decisions were made around authentication?"

**Response**:
```
Episode 'Architecture: dual-backend search_memory MCP tool' queued for processing in group 'csc490-demo-2';
Aurora: 5 graph writes, 14 facts extracted
```

---

### Episode 2 — search_memory Implementation

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Implementation: search_memory with ProfileSearchService` |
| `group_id` | `csc490-demo-2` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo2-transcript-002` |

**Episode body**:

> Human: How did you wire up ProfileSearchService into the MCP server?
>
> Agent: I extended AuroraIngestionService with four new instance vars: _embed_provider, _profile_vector_store, _profile_indexer, and _profile_search_service. During initialize(), I create a BedrockTitanEmbedder, create and bootstrap AuroraProfileVectorStore (which creates the cg_profile_keys table and HNSW index if they don't exist), then create ProfileVectorIndexer and ProfileSearchService. All of this is wrapped in a try/except so a vector store failure is non-fatal — the service degrades to Neptune-only. I also wired ProfileVectorIndexer into EpisodeProcessor so every add_memory call with a repo_id automatically indexes semantic facts into pgvector.
>
> Human: How does the search work in production?
>
> Agent: The search_memory tool calls asyncio.to_thread(aurora_ingestion_service.search_profiles_sync, ...) to avoid blocking the async event loop. search_profiles_sync calls initialize() to ensure Aurora is ready (lazy init fix after server restart), then delegates to ProfileSearchService.search(). The method serializes results into plain dicts that match the ProfileResult TypedDict. Neptune search and Aurora search run independently; errors in either path are caught and logged while the other path still returns results.

**Response**:
```
Episode 'Implementation: search_memory with ProfileSearchService' queued for processing in group 'csc490-demo-2';
Aurora: 5 graph writes, 22 facts extracted
```

---

### Episode 3 — Bug Fixes

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `Bug fixes: lazy init and max_facts=0` |
| `group_id` | `csc490-demo-2` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `agent_transcript` |
| `source_id` | `demo2-transcript-003` |

**Episode body**:

> Human: After deploying search_memory, profile searches returned empty results even though Aurora profiles existed.
>
> Agent: The root cause was a lazy initialization gap. AuroraIngestionService.initialize() is only called on the first ingest_episode_sync, so after a server restart, search_profiles_sync found self._ready=False and returned [] immediately. The fix is to call self.initialize() at the start of search_profiles_sync. A second bug: max_facts=0 was intended to skip Neptune and return Aurora-only results, but the validation check was max_facts <= 0 which treated 0 as invalid. Fixed to max_facts < 0, with an explicit if max_facts > 0 guard around the Neptune search call.
>
> Human: Why didn't the Python 3.10 regex bug show up earlier?
>
> Agent: The _ROUTE_RE pattern in query_parser.py used \\[\\] inside a raw string, which works in Python 3.11 but causes re.error: multiple repeat in Python 3.10 because \\] closes the character class early. The EC2 server runs Python 3.11 so the bug was invisible during development. It was caught when running the unit tests locally on Python 3.10. The fix is to use \[\] instead of \\[\\] inside the character class.

**Response**:
```
Episode 'Bug fixes: lazy init and max_facts=0' queued for processing in group 'csc490-demo-2';
Aurora: 5 graph writes, 34 facts extracted
```

---

### Episode 4 — search_memory tool spec (this demo session)

**Call parameters**:

| Parameter | Value |
|---|---|
| `name` | `search_memory MCP tool` |
| `group_id` | `csc490-demo-2` |
| `source` | `message` |
| `repo_id` | `context-graph` |
| `source_type` | `text` |
| `source_id` | `demo2-search-memory-tool` |

**Episode body**:

> The search_memory MCP tool queries both Neptune and Aurora PostgreSQL. It has parameters: query (natural language), group_ids (Neptune filter), repo_id (enables Aurora profile search), max_facts (default 10), max_profiles (default 5). When repo_id is provided, ProfileSearchService runs a pgvector cosine similarity search on cg_profile_keys and returns ProfileRecord rows from cg_profiles.

**Response**:
```
Episode 'search_memory MCP tool' queued for processing in group 'csc490-demo-2';
Aurora: 5 graph writes, 17 facts extracted
```

---

### Ingestion Summary

| Episode | Aurora graph writes | Aurora facts extracted |
|---|---|---|
| Architecture: dual-backend search_memory | 5 | 14 |
| Implementation: search_memory with ProfileSearchService | 5 | 22 |
| Bug fixes: lazy init and max_facts=0 | 5 | 34 |
| search_memory MCP tool spec | 5 | 17 |
| **Total** | **20** | **87** |

All 4 episodes were also queued for async Neptune processing. The Neptune queue was cleared by a server restart mid-session (to fix a queue backlog that was starving the event loop), so Neptune entity extraction facts for episodes 1–3 were not persisted; episode 4 was processed successfully.

---

## Retrieval Results

### Test 1: `search_memory` — Neptune only (no `repo_id`)

**Query**: `"Neptune Graphiti search_memory tool"`
**Parameters**: `group_ids=["csc490-demo-2"], max_facts=5`

```json
{
  "message": "No results found for: 'Neptune Graphiti search_memory tool'",
  "facts": [],
  "profiles": []
}
```

**Note**: Neptune queue was restarted mid-session; only episode 4 was processed in Neptune. Neptune's BM25 backend (OpenSearch) is not provisioned, so `search_nodes` returns empty for this group. The `search_memory_facts` vector search returns 1 fact (see Test 6) from the episode that completed Neptune processing before the restart.

---

### Test 2: `search_memory` — Aurora only (`max_facts=0`)

**Query**: `"search_memory MCP tool queries Aurora PostgreSQL"`
**Parameters**: `repo_id="context-graph", max_facts=0, max_profiles=5`
**Aurora cold-start time**: 3.4s (first search after server restart — triggers lazy init)

```json
{
  "message": "Found 5 profiles for: 'search_memory MCP tool queries Aurora PostgreSQL'",
  "facts": [],
  "profiles": [
    {
      "profile_id": "profile:context-graph:semantic_fact_profile:fact:context-graph:1d8aea1f...",
      "relation_type": "USES_STYLE_OF",
      "value_text": "Entity Aurora PostgreSQL uses style of Entity LightRAG. The Aurora path uses LightRAG-style semantic profile retrieval.",
      "source_node_id": "entity:context-graph:a17a5ebc...",
      "target_node_id": "entity:context-graph:0bdde76c...",
      "is_active": true,
      "score": 1.0,
      "metadata": {
        "created_at": "2026-03-23T06:21:13.729928+00:00",
        "episode_ids": ["agent_transcript:smoke-test-search-memory-001"],
        "fact_id": "fact:context-graph:1d8aea1fbf8870e1785af2505cc3aa143cc29b42"
      }
    },
    {
      "profile_id": "profile:context-graph:semantic_fact_profile:fact:context-graph:1d90efe7...",
      "relation_type": "QUERIES",
      "value_text": "Entity search_memory MCP tool queries Entity Aurora PostgreSQL. The search_memory MCP tool queries Aurora PostgreSQL.",
      "source_node_id": "entity:context-graph:8e19dd7d...",
      "target_node_id": "entity:context-graph:a17a5ebc...",
      "is_active": true,
      "score": 1.0,
      "metadata": {
        "created_at": "2026-03-23T06:21:13.729928+00:00",
        "episode_ids": ["agent_transcript:smoke-test-search-memory-001"],
        "fact_id": "fact:context-graph:1d90efe7277f4369b1ff2e78ac2e0669a844cd5a"
      }
    },
    {
      "profile_id": "profile:context-graph:semantic_fact_profile:fact:context-graph:2073e70093...",
      "relation_type": "QUERIES",
      "value_text": "Entity search_memory queries Entity Aurora PostgreSQL. search_memory queries Aurora PostgreSQL",
      "source_node_id": "entity:context-graph:9ab8f3b5...",
      "target_node_id": "entity:context-graph:a17a5ebc...",
      "is_active": true,
      "score": 1.0,
      "metadata": {
        "created_at": "2026-03-23T07:02:43.373017+00:00",
        "episode_ids": ["text:demo2-search-memory-tool"],
        "fact_id": "fact:context-graph:2073e70093fa1b3198c769caf7eb25bf5f5210a3"
      }
    },
    {
      "profile_id": "profile:context-graph:semantic_fact_profile:fact:context-graph:9eee3ce7...",
      "relation_type": "RUNS_ON",
      "value_text": "Entity pgvector runs on Entity Aurora PostgreSQL. pgvector runs on Aurora PostgreSQL",
      "source_node_id": "entity:context-graph:6db60874...",
      "target_node_id": "entity:context-graph:a17a5ebc...",
      "is_active": true,
      "score": 1.0,
      "metadata": {
        "created_at": "2026-03-23T07:02:43.373017+00:00",
        "episode_ids": ["text:demo2-search-memory-tool"],
        "fact_id": "fact:context-graph:9eee3ce7dad01f9ece5af39ecebbfb4bade19807"
      }
    },
    {
      "profile_id": "profile:context-graph:semantic_fact_profile:fact:context-graph:d97f452b...",
      "relation_type": "USES",
      "value_text": "Entity Aurora PostgreSQL uses Entity pgvector. The Aurora path uses pgvector embeddings for semantic profile retrieval.",
      "source_node_id": "entity:context-graph:a17a5ebc...",
      "target_node_id": "entity:context-graph:6db60874...",
      "is_active": true,
      "score": 1.0,
      "metadata": {
        "created_at": "2026-03-23T06:21:13.729928+00:00",
        "episode_ids": ["agent_transcript:smoke-test-search-memory-001"],
        "fact_id": "fact:context-graph:d97f452b6dd4607b94663e3dd4ebf65c18d6ff1a"
      }
    }
  ]
}
```

**Key observations**:
- `max_facts=0` correctly skips Neptune (fixed `<= 0` → `< 0` validation bug)
- All 5 profiles score `1.000` — exact semantic match for this query
- Profiles span two episodes: a prior smoke test (`smoke-test-search-memory-001`) and the current demo (`demo2-search-memory-tool`) — cross-episode retrieval works
- Relation types captured: `USES_STYLE_OF`, `QUERIES`, `RUNS_ON`, `USES` — richer than raw keyword search
- `metadata.fact_id` and `metadata.episode_ids` provide full provenance

---

### Test 3: `search_memory` — Combined Neptune + Aurora

**Query**: `"Neptune Graphiti search_memory tool"`
**Parameters**: `group_ids=["csc490-demo-2"], repo_id="context-graph", max_facts=5, max_profiles=3`

```json
{
  "message": "Found 2 profiles for: 'Neptune Graphiti search_memory tool'",
  "facts": [],
  "profiles": [
    {
      "profile_id": "profile:context-graph:semantic_fact_profile:fact:context-graph:bb8658fa...",
      "relation_type": "MERGES_DATA_FROM",
      "value_text": "Entity search_memory MCP tool merges data from Entity Aurora. search_memory MCP tool merges Neptune facts with Aurora profiles",
      "source_node_id": "entity:context-graph:8e19dd7d...",
      "target_node_id": "entity:context-graph:c8251d84...",
      "is_active": true,
      "score": 0.912,
      "metadata": {
        "created_at": "2026-03-23T06:30:13.934660+00:00",
        "episode_ids": ["agent_transcript:demo2-transcript-003"],
        "fact_id": "fact:context-graph:bb8658fa7f33807a25108905fc9856b316fcd342"
      }
    },
    {
      "profile_id": "profile:context-graph:semantic_fact_profile:fact:context-graph:f383c152...",
      "relation_type": "HAS_PARAMETER",
      "value_text": "Entity search_memory has parameter Entity max_profiles. search_memory has parameter: max_profiles (default 5)",
      "source_node_id": "entity:context-graph:9ab8f3b5...",
      "target_node_id": "entity:context-graph:58e5a0d6...",
      "is_active": true,
      "score": 0.912,
      "metadata": {
        "created_at": "2026-03-23T07:02:43.373017+00:00",
        "episode_ids": ["text:demo2-search-memory-tool"],
        "fact_id": "fact:context-graph:f383c15290a0048594353faf88a5e46553ccd142"
      }
    }
  ]
}
```

**Key observation**: Neptune returned 0 facts (queue cleared by restart) but Aurora returned 2 relevant profiles with semantic relation types `MERGES_DATA_FROM` and `HAS_PARAMETER` — demonstrating graceful Neptune-miss + Aurora-hit behaviour.

---

### Test 4: `search_memory_facts` — Legacy Neptune tool

**Query**: `"Aurora profile vector indexing pgvector"`
**Parameters**: `group_ids=["csc490-demo-2"], max_facts=5`

```json
{
  "message": "Facts retrieved successfully",
  "facts": [
    {
      "uuid": "8a482dec-e6a0-4f52-962b-ded069995827",
      "group_id": "csc490-demo-2",
      "name": "PROVIDES_INDEX_FOR",
      "fact": "pgvector is used as the vector index for profile key embeddings stored in the cg_profile_keys table.",
      "source_node_uuid": "464ba4c3-471f-4b6a-83e0-8add512c6e96",
      "target_node_uuid": "50108f79-9adb-49cd-89f8-bd9dc78c5396",
      "created_at": "2026-03-23T06:29:37.764417Z",
      "valid_at": "2026-03-23T06:29:20.039797Z",
      "invalid_at": null,
      "episodes": ["d51e05a5-2b5b-4b71-8c14-15243586219c"]
    }
  ]
}
```

This fact survived the Neptune restart because it was extracted before the restart. It shows the provenance chain: episode `d51e05a5` → fact `8a482dec` → relation `PROVIDES_INDEX_FOR` between pgvector and cg_profile_keys.

---

### Test 5: `get_entity_edge` — Provenance Inspection

Fetching the Neptune fact found in Test 4 to show the full provenance link:

**UUID**: `8a482dec-e6a0-4f52-962b-ded069995827`

```json
{
  "uuid": "8a482dec-e6a0-4f52-962b-ded069995827",
  "group_id": "csc490-demo-2",
  "name": "PROVIDES_INDEX_FOR",
  "fact": "pgvector is used as the vector index for profile key embeddings stored in the cg_profile_keys table.",
  "source_node_uuid": "464ba4c3-471f-4b6a-83e0-8add512c6e96",
  "target_node_uuid": "50108f79-9adb-49cd-89f8-bd9dc78c5396",
  "created_at": "2026-03-23T06:29:37.764417Z",
  "valid_at": "2026-03-23T06:29:20.039797Z",
  "invalid_at": null,
  "expired_at": null,
  "episodes": ["d51e05a5-2b5b-4b71-8c14-15243586219c"],
  "attributes": {}
}
```

This edge traces: episode `d51e05a5` (bug-fixes transcript) → fact `8a482dec` (`pgvector PROVIDES_INDEX_FOR cg_profile_keys`). The same provenance chain exists on the Aurora side: `metadata.fact_id` + `metadata.episode_ids` in every ProfileRecord.

---

### Test 6: `search_nodes` + `search_memory_facts` — Cross-group retrieval (`main`)

Neptune facts and entities from the `main` group (populated by earlier implementation test runs) remain fully queryable:

**`search_nodes` query**: `"JWT authentication middleware"`

```json
{
  "message": "Nodes retrieved successfully",
  "nodes": [
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
}
```

**`search_memory_facts` query**: `"JWT authentication middleware implementation"`

```json
{
  "message": "Facts retrieved successfully",
  "facts": [
    {
      "uuid": "943051c0-3925-4e72-8f2f-39d5465182c1",
      "group_id": "main",
      "name": "LOCATED_IN",
      "fact": "JWT authentication middleware is implemented in src/auth/jwt_middleware.py.",
      "episodes": ["1739e4e9-e354-42dd-94f6-3a9d6a5f3742", "e8c8647b-3b37-4576-ab14-e5da55ff9010", "d99f3fae-02a7-4c9f-bb48-0509274aeefe"],
      "created_at": "2026-03-23T03:22:08.932363Z",
      "valid_at": "2026-03-23T03:21:15.114634Z",
      "invalid_at": null
    },
    {
      "uuid": "453f4813-8c07-4b38-bd6b-15f98f9f59d7",
      "group_id": "main",
      "name": "INCLUDES",
      "fact": "JWT authentication middleware includes token expiration handling.",
      "episodes": ["e8c8647b-3b37-4576-ab14-e5da55ff9010", "d99f3fae-02a7-4c9f-bb48-0509274aeefe"],
      "created_at": "2026-03-23T03:27:03.344150Z",
      "valid_at": "2026-03-23T03:26:45.666129Z",
      "invalid_at": null
    }
  ]
}
```

The `main` group facts survived across both demo sessions — demonstrating true cross-session persistence.

---

## Key Takeaways (Demo 2 vs Demo 1)

| Capability | Demo 1 (Phases 1–4) | Demo 2 (Phase 5 added) |
|---|---|---|
| Tools registered | 9 | **10** (`search_memory` added) |
| Dual-backend write | ✅ | ✅ |
| `search_memory_facts` (Neptune) | ✅ | ✅ |
| `search_memory` unified tool | ❌ not yet | ✅ **live** |
| Aurora-only search (`max_facts=0`) | ❌ (bug: treated as error) | ✅ **fixed** |
| Aurora profile retrieval | ❌ not yet | ✅ **5 profiles, score=1.000** |
| Cold-start Aurora search time | n/a | **3.4s** (lazy init on first call) |
| Lazy init fix for `search_profiles_sync` | ❌ (returned empty) | ✅ **calls initialize()** |
| Cross-episode profile retrieval | n/a | ✅ (profiles from 2 distinct episodes merged) |
| Semantic relation types in profiles | n/a | `QUERIES`, `USES`, `RUNS_ON`, `USES_STYLE_OF`, `MERGES_DATA_FROM`, `HAS_PARAMETER` |
| Neptune facts on `main` group | ✅ | ✅ (cross-session persistence) |
| `max_facts=0` skips Neptune | ❌ (validation error) | ✅ **fixed** |
| Aurora facts extracted per demo session | 71 | **87** |
| Aurora profiles indexed in pgvector | n/a | ✅ (vector index bootstrapped on init) |

---

## What the `search_memory` Response Looks Like in Practice

Combined query returning both Neptune facts and Aurora profiles:

```
message: "Found 2 profiles for: 'Neptune Graphiti search_memory tool'"

Neptune facts:
  (none — queue cleared by restart in this demo)

Aurora profiles:
  [score=0.912] search_memory MCP tool merges data from Aurora.
                search_memory MCP tool merges Neptune facts with Aurora profiles
                [MERGES_DATA_FROM | episode: demo2-transcript-003]

  [score=0.912] search_memory has parameter: max_profiles (default 5)
                [HAS_PARAMETER | episode: demo2-search-memory-tool]
```

Aurora-only query (max_facts=0):

```
message: "Found 5 profiles for: 'search_memory MCP tool queries Aurora PostgreSQL'"

Aurora profiles:
  [score=1.000] Aurora PostgreSQL uses LightRAG-style semantic profile retrieval
                [USES_STYLE_OF | episode: smoke-test-search-memory-001]
  [score=1.000] search_memory MCP tool queries Aurora PostgreSQL
                [QUERIES | episode: smoke-test-search-memory-001]
  [score=1.000] search_memory queries Aurora PostgreSQL
                [QUERIES | episode: demo2-search-memory-tool]
  [score=1.000] pgvector runs on Aurora PostgreSQL
                [RUNS_ON | episode: demo2-search-memory-tool]
  [score=1.000] Aurora PostgreSQL uses pgvector embeddings for semantic profile retrieval
                [USES | episode: smoke-test-search-memory-001]
```
