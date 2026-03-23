# Context Graph — Implementation Progress Summary

**Date**: 2026-03-23

---

## What We Built

A **provenance-aware context graph MCP server** for coding agents. Agents can call `add_memory` to persist conversations, decisions, and code context, and `search_memory` to retrieve semantically relevant context from any future session.

The system uses two complementary backends:

- **Amazon Neptune** (via Graphiti) — structured knowledge graph with named entities, temporal facts, and provenance edges
- **Aurora PostgreSQL** (via context_graph) — episodic metadata, semantic relation profiles, and pgvector embeddings for LightRAG-style retrieval

---

## Phase-by-Phase Accomplishments

### Graphiti Port (pre-LightRAG baseline)

Documented in [GRAPHITI_PORT.md](GRAPHITI_PORT.md).

Accomplished:

- Vendored `graphiti-core` into `vendor/graphiti/`
- Implemented `BedrockLLMClient` (boto3 Converse API, IAM role)
- Implemented `BedrockTitanEmbedder` (Titan embed text v2, 1024 dims)
- Implemented `NoOpCrossEncoder` (no OpenAI dependency)
- Fixed `NeptuneDriver` for use with Amazon Neptune Serverless (6 bug fixes: `_database` attribute, `get_status` crash, `execute_query` parameter handling, parameters not sent to Neptune, `np.float64` JSON serialization, AOSS `KeyError`)
- Deployed EC2 MCP server with systemd, Python 3.11, IAM role auth
- `add_memory`, `search_memory_facts`, `search_nodes`, `get_episodes`, `get_entity_edge` all operational against Neptune

---

### LightRAG Phase 1 — Metadata and Schema ✅

Accomplished in `context_graph/`:

- `ProfileRecord` dataclass (frozen, immutable) with `keys_json()` and `metadata_json()` serialization
- `ProfileDeactivation` dataclass for temporal invalidation
- `cg_profiles` SQL table with 18 columns including `is_active`, `valid_from_revision`, `valid_to_revision`
- `load_profiles()`, `upsert_profiles()`, `deactivate_profiles()` implemented on:
  - `InMemoryMetadataStore` (for testing)
  - `SQLiteMetadataStore` (local dev)
  - `PostgresMetadataStore` (Aurora production)
- `reset_repo_context()` clears `cg_profiles` to keep profile metadata aligned with graph reset

---

### LightRAG Phase 2 — Profile Generation ✅

Accomplished in `context_graph/profiles.py`:

- `SEMANTIC_FACT_PROFILE_KIND = "semantic_fact_profile"` — the first profile kind, scoped to semantically extracted `RELATES_TO` facts only (low-level structural edges like `CALLS`, `DEFINES`, `TOUCHES` intentionally excluded)
- `semantic_fact_profile_from_fact()` — deterministic profile builder:
  - `value_text`: `"{source_kind} {source_name} {relation_label} {target_kind} {target_name}. {fact}"`
  - `keys`: relation type, normalized label, fact text, source/target searchable terms
- `profile_deactivation_from_fact_invalidation()` — marks profiles inactive when Graphiti invalidates a fact
- Profile generation wired into `EpisodeProcessor.apply_batch()` — profiles created automatically during every ingestion run

---

### LightRAG Phase 3 — Vector Indexing ✅

Accomplished in `context_graph/profile_vectors.py`:

- `cg_profile_keys` Aurora PostgreSQL table with `vector(1024)` embedding column
- HNSW vector index (`ivfflat`-compatible) on the embedding column for ANN search
- `AuroraProfileVectorStore` — production pgvector backend:
  - `bootstrap()` creates extension + table + indexes
  - `upsert_profile_keys()` for incremental writes
  - `deactivate_profile_keys()` for precise per-fact invalidation
  - `search_profile_keys()` using cosine distance (`<=>`)
- `InMemoryProfileVectorStore` — in-memory backend for unit tests
- `ProfileVectorIndexer` — write-side bridge: embeds profile keys and upserts into vector store
- `ProfileVectorIndexer` is now wired into `AuroraIngestionService` and passed to `EpisodeProcessor` — **every `add_memory` call with a `repo_id` auto-indexes semantic facts into pgvector**
- `EmbeddingProvider` abstraction over Bedrock Titan / OpenAI with `embed_texts()` interface

---

### LightRAG Phase 4 — Query Parsing ✅

Accomplished in `context_graph/query_parser.py`:

- `QueryParser` with two-track parsing:
  - **Deterministic anchors**: backtick references, quoted strings, `#123` artifact numbers, commit SHAs (7–40 hex chars), API routes, file paths, symbol identifiers
  - **LLM high-level keywords**: optional Bedrock-powered extraction of themes (subsystem labels, design concerns, intent words) — intentionally distinct from low-level code anchors
- **Intent tags**: `why`, `impact`, `rollback`, `constraint`, `migration`, `history`
- `QueryParseResult` dataclass with `anchors`, `high_level_keywords`, `intent_tags`
- Fixed a Python 3.10 regex compatibility bug (`\\[\\]` → `\[\]` in `_ROUTE_RE`)

---

### LightRAG Phase 5 — Retrieval Channel ✅ *(implemented this session)*

Accomplished via `search_memory` MCP tool in `vendor/graphiti/mcp_server/src/graphiti_mcp_server.py`:

- `ProfileSearchService.search()` end-to-end pipeline:
  1. Parse query with `QueryParser` (deterministic anchors + optional LLM keywords)
  2. Embed high-level keywords via `EmbeddingProvider` (Bedrock Titan)
  3. Search `cg_profile_keys` via pgvector cosine similarity (`AuroraProfileVectorStore.search_profile_keys()`)
  4. Merge hits by `profile_id` (keeping highest score per profile)
  5. Load full `ProfileRecord` rows from `cg_profiles`
  6. Return `ProfileSearchResult` with hits and profiles

- New `search_memory` MCP tool:
  - `query`, `group_ids`, `repo_id`, `max_facts` (default 10), `max_profiles` (default 5)
  - **Neptune path** (always): `graphiti_client.search()` for structured entity facts
  - **Aurora path** (when `repo_id` provided): `ProfileSearchService.search()` for semantic relation profiles
  - Merged response: `{ message, facts: [...], profiles: [...] }`
  - Graceful fallback: Aurora unavailable → Neptune-only with informative message
  - `search_profiles_sync()` bridge method for `asyncio.to_thread()` safety

- `AuroraIngestionService` extended:
  - Initializes `EmbeddingProvider`, `AuroraProfileVectorStore`, `ProfileVectorIndexer`, `ProfileSearchService` at startup
  - Bootstraps `cg_profile_keys` table + HNSW index on first init
  - Vector init failure is non-fatal — service degrades gracefully

- New response types: `ProfileResult`, `SearchMemoryResponse` in `models/response_types.py`

---

### Episodic Ingestion (dual-backend write path) ✅

Documented in [EPISODIC_INGESTION_IMPLEMENTATION.md](EPISODIC_INGESTION_IMPLEMENTATION.md).

Accomplished:

- `AuroraIngestionService` integrated into `add_memory` MCP tool
- Aurora write is non-blocking (`asyncio.to_thread`), non-fatal (Neptune path unaffected by Aurora errors)
- `_NullGraphSink` enables `EpisodeProcessor` to run Bedrock-powered fact extraction without Neptune Bolt auth issues
- 5 new `add_memory` parameters: `repo_id`, `source_type`, `source_id`, `author`, `thread_id`
- Backward compatible: calls without `repo_id` skip Aurora silently
- Deduplication via `episode_id = "{source_type}:{source_id}"` + `ON CONFLICT`
- IAM policy fix: removed broken `Deny/StringNotEqualsIfExists` on `bedrock:ModelId`; replaced with scoped `Allow` on both `foundation-model/` and `inference-profile/` ARN patterns

---

## Smoke Test Results (2026-03-23)

All tests run live against the deployed EC2 MCP server at `http://15.156.203.208:8000/mcp/`.

| Test | Result |
|------|--------|
| `get_status` (Neptune connection) | ✅ `"status": "ok"` |
| `search_memory` registered in `tools/list` | ✅ |
| Neptune-only path (no `repo_id`) | ✅ Returns facts with correct hint in message |
| `add_memory` dual-backend write | ✅ Neptune queued + Aurora: 9 facts extracted |
| Aurora profile vector indexing (new episode) | ✅ Profiles embedded and indexed into `cg_profile_keys` |
| `search_memory` Aurora-only (`max_facts=0`) | ✅ 5 profiles returned, top score 1.000 |
| `search_memory` combined Neptune + Aurora | ✅ 1 fact + 3 profiles, merged message |

Example combined result:
```
message: Found 1 fact and 3 profiles for: 'Neptune Graphiti search_memory tool'

Neptune facts:
  [QUERIES] The search_memory MCP tool queries Neptune.

Aurora profiles:
  [score=0.810] search_memory MCP tool queries Aurora PostgreSQL. The search_memory MCP tool queries Aurora PostgreSQL.
  [score=0.810] search_memory MCP tool queries Neptune. The search_memory MCP tool queries Neptune.
  [score=0.810] search_memory MCP tool has parameter repo_id. The search_memory MCP tool has a repo_id parameter.
```

---

## Current Architecture

```
Cursor / Agent
     │  MCP (HTTP :8000)
     ▼
┌──────────────────────────────────────────────────────┐
│  EC2 MCP Server  (graphiti_mcp_server.py)            │
│                                                      │
│  add_memory  ──────────────────┬──────────────────── │
│                                │                     │
│              QueueService  AuroraIngestionService    │
│              (async)       (asyncio.to_thread)       │
│                 │               │                    │
│                 │    ┌──────────┴────────────┐       │
│                 │    │  EpisodeProcessor     │       │
│                 │    │  GraphitiAdapter(LLM) │       │
│                 │    │  ProfileVectorIndexer │       │
│                 │    └──────────┬────────────┘       │
└─────────────────┼───────────────┼────────────────────┘
                  │               │
                  ▼               ▼
           Amazon Neptune   Aurora PostgreSQL
           (Graphiti)       cg_profiles (facts)
           Entities/Facts   cg_profile_keys (vectors)
           Knowledge Graph  pgvector HNSW index

  search_memory ──────────────────┬──────────────────
                                  │
                   ┌──────────────┴──────────────┐
                   │  Neptune path               │  Aurora path
                   │  client.search()            │  ProfileSearchService
                   │  (entity facts)             │  QueryParser → embed
                   │                             │  → pgvector ANN
                   │                             │  → load ProfileRecords
                   └──────────────┬──────────────┘
                                  │ merged response
                                  ▼
                   { facts: [...], profiles: [...] }
```

---

## What Remains (LightRAG Phases 6–8)

### Phase 6 — Global merge and context building

- Score normalization across Neptune facts and Aurora profiles (currently returned as separate lists, not jointly ranked)
- Intent-aware caps per result type (e.g. `why` queries → boost profiles over facts)
- A unified context constructor that replaces Graphiti's community output

### Phase 7 — History-aware retrieval

- `include_inactive=True` flag on `search_memory` to query expired profiles
- Revision/time range filters on `cg_profiles`
- Diff mode: compare active vs. historical profile sets

### Phase 8 — Evaluation

- Retrieval relevance benchmarks on repository QA tasks
- File/function localization accuracy
- Provenance lookup precision
- Compare plain Graphiti retrieval vs. hybrid profile retrieval on coding-agent tasks

### Other open items

- Backfill CLI (`sync-profile-vectors`) to index pre-existing `cg_profiles` rows that predate the vector indexer wiring
- Elastic IP for EC2 (IP currently changes on restart)
- AOSS (OpenSearch) for `search_nodes` fulltext recall — currently returns empty without BM25 backend
- Provision AOSS in Terraform and set `aoss_host` in `config-neptune-bedrock.yaml`
- GitHub Actions OIDC for automated deploy on push to `main`
