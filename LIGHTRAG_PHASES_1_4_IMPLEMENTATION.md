# LightRAG Phases 1-4 Implementation Notes

## Overview

This document describes the current implementation of the LightRAG-inspired phases 1-4 in the repository after the latest changes.

The implementation is intentionally **narrow**:

- the graph remains the canonical source of truth
- only semantic `RELATES_TO` facts participate in the LightRAG-style profile/vector path
- low-level structural edges such as `CALLS`, `DEFINES`, `TOUCHES`, `HAS_COMMIT`, `ON`, and `MENTIONS` remain graph-native and are **not** embedded into the profile index

This matches the architectural decision we made during implementation:

- low-level retrieval should stay graph-based
- the LightRAG-inspired high-level retrieval branch should focus on semantically meaningful relation facts

## What Was Added

### 1. Semantic fact profiles

File:

- [context_graph/profiles.py](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/profiles.py)

Implemented:

- `ProfileRecord`
- `ProfileDeactivation`
- `SEMANTIC_FACT_PROFILE_KIND = "semantic_fact_profile"`
- `semantic_fact_profile_from_fact(...)`
- `profile_deactivation_from_fact_invalidation(...)`

What this does:

- every extracted semantic `RELATES_TO` fact can now be materialized as one profile row
- the profile is keyed by the underlying fact edge id
- the profile stores:
  - profile identity
  - source/target ids and kinds
  - relation type
  - deterministic `value_text`
  - deterministic `keys`
  - active/inactive state

Current value construction:

- `value_text` is deterministic
- it is built from:
  - source kind/name
  - relation label
  - target kind/name
  - fact text

Current key construction:

- keys are deterministic and come from:
  - relation type
  - normalized relation label
  - fact text
  - source searchable terms
  - target searchable terms

So a semantic fact now produces both:

- the graph fact edge itself
- and a retrieval profile row suitable for vector indexing

### 2. Profile persistence in metadata

File:

- [context_graph/metadata.py](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/metadata.py)

Implemented on the metadata abstraction:

- `load_profiles(...)`
- `upsert_profiles(...)`
- `deactivate_profiles(...)`

Implemented in:

- `InMemoryMetadataStore`
- `SQLiteMetadataStore`
- `PostgresMetadataStore`

What this means:

- profile rows are now real persisted data, not only schema
- SQLite can store profile rows for local/unit tests
- PostgreSQL/Aurora can store the same rows in `cg_profiles`

Deactivation logic:

- when a semantic fact is invalidated by the Graphiti-style fact path
- the matching active profile is marked inactive
- `updated_at` and `valid_to_revision` are updated
- replacement profiles stay active

Reset behavior:

- `reset_repo_context(...)` still clears `cg_profiles`
- this keeps profile metadata aligned with graph reset

### 3. Aurora/Postgres pgvector profile-key index

File:

- [context_graph/profile_vectors.py](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/profile_vectors.py)

Implemented:

- `ProfileKeyRecord`
- `ProfileVectorHit`
- `ProfileSearchResult`
- `BaseProfileVectorStore`
- `InMemoryProfileVectorStore`
- `AuroraProfileVectorStore`
- `ProfileVectorIndexer`
- `ProfileSearchService`
- `build_profile_key_records(...)`

#### `AuroraProfileVectorStore`

This is the production-style vector backend for Aurora PostgreSQL.

Bootstrap behavior:

- runs `CREATE EXTENSION IF NOT EXISTS vector`
- creates `cg_profile_keys`
- creates supporting indexes
- creates an HNSW vector index on the embedding column

Current table shape:

- `profile_key_id`
- `profile_id`
- `repo_id`
- `profile_kind`
- `relation_type`
- `source_node_id`
- `target_node_id`
- `key_text`
- `embedding`
- `is_active`
- `valid_from_revision`
- `valid_to_revision`
- `updated_at`

Important design detail:

- vector key rows store relation/source/target identity
- this was added so profile-key invalidation can be precise
- without this, deactivating one fact profile could accidentally deactivate unrelated key rows of the same kind

#### `ProfileVectorIndexer`

This is the write-side bridge between profile metadata and pgvector.

Responsibilities:

- embed profile keys
- upsert embedded key rows into the vector store
- deactivate old key rows when fact profiles are invalidated
- optionally backfill active profiles for a repo

Two main methods:

- `apply_profile_updates(...)`
  - used incrementally after fact/profile updates
- `sync_repo(...)`
  - used for backfill of all active profiles

#### `ProfileSearchService`

This is the read-side search layer for the LightRAG-style profile branch.

Responsibilities:

- parse a retrieval query
- embed high-level keywords
- search pgvector
- load matching profile rows from metadata
- return merged hits + parsed query info + profile records

Current behavior:

- if the query parser returns high-level keywords, those are embedded
- if not, the raw query is used as fallback text for vector search
- profile hits are merged by `profile_id`
- active-only filtering is supported

### 4. Embedding provider abstraction

File:

- [context_graph/embeddings.py](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/embeddings.py)

Implemented:

- `EmbeddingSettings`
- `EmbeddingProvider`
- `load_embedding_settings(...)`
- `make_embedding_provider(...)`

Supported providers:

- `openai`
- `bedrock`

Current Bedrock implementation:

- uses the vendored Graphiti `BedrockTitanEmbedder`
- default model: `amazon.titan-embed-text-v2:0`
- default dimension: `1024`

Current OpenAI implementation:

- uses the vendored Graphiti `OpenAIEmbedder`
- default model: `text-embedding-3-small`

Environment / arg model:

- `CONTEXT_GRAPH_EMBED_PROVIDER`
- `CONTEXT_GRAPH_EMBED_MODEL`
- `CONTEXT_GRAPH_EMBED_DIMENSIONS`
- `CONTEXT_GRAPH_EMBED_BASE_URL`
- `CONTEXT_GRAPH_EMBED_API_KEY`
- `AWS_REGION`

### 5. Query parsing

File:

- [context_graph/query_parser.py](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/query_parser.py)

Implemented:

- `QueryAnchor`
- `QueryParseResult`
- `QueryParser`

Current parser split:

#### Deterministic anchors

The parser now extracts:

- backticked references
- quoted strings
- artifact numbers like `#123`
- commit SHAs
- routes
- path-like tokens
- symbol-like identifiers

Anchor kinds currently used:

- `quoted`
- `artifact_number`
- `commit_sha`
- `route`
- `file_path`
- `symbol`

#### Deterministic intent tags

The parser recognizes:

- `why`
- `impact`
- `rollback`
- `constraint`
- `migration`
- `history`

#### LLM high-level keywords

When an LLM client is configured, the parser also asks for:

- short high-level retrieval keywords only

Important design detail:

- the prompt is intentionally about **themes**, not exact code anchors
- the prompt now follows the paper-style keyword extraction format, but only returns `high_level_keywords`
- this keeps the LightRAG branch high-level
- low-level code references remain the job of graph-native retrieval

### 6. Episode pipeline integration

File:

- [context_graph/episode_processor.py](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/episode_processor.py)

Implemented:

- `EpisodeProcessor` now accepts an optional `profile_indexer`
- when `execute=True`, after graph writes and metadata profile updates:
  - active semantic fact profiles are upserted
  - invalidated semantic fact profiles are deactivated
  - if a vector indexer is configured, pgvector key rows are updated too

This means:

- if episode ingestion extracts semantic `RELATES_TO` facts
- and embeddings are configured
- profile vectors are updated automatically during ingestion

This is the first live end-to-end LightRAG write path in the codebase.

### 7. CLI support

File:

- [context_graph/cli.py](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/cli.py)

Added commands:

- `parse-query`
- `sync-profile-vectors`
- `search-profiles`

Added embedding args:

- `--embed-provider`
- `--embed-model`
- `--embed-dimensions`
- `--embed-base-url`
- `--embed-api-key`

Behavior:

#### `parse-query`

Parses a retrieval query and prints:

- deterministic anchors
- high-level keywords
- intent tags

#### `sync-profile-vectors`

Backfills all active profiles for a repo into pgvector.

Used for:

- backfill
- repair
- manual indexing after a metadata-only restore

#### `search-profiles`

Runs:

1. query parsing
2. keyword embedding
3. pgvector search
4. metadata profile lookup

and prints:

- parse result
- vector hits
- resolved profiles

#### Automatic vector sync during episode ingestion

The CLI now also tries to construct a `ProfileVectorIndexer` for episode-based commands when:

- metadata backend is PostgreSQL-compatible
- embedding config is present

That affects:

- `ingest-episode`
- `apply-commit`
- `apply-latest-commit`
- `hosted-index`

if those commands produce semantic facts through the episode path.

### 8. Local development update

File:

- [context_graph/docker-compose.local.yml](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/docker-compose.local.yml)

Changed:

- local Postgres image now uses `pgvector/pgvector:pg16`

Reason:

- local vector testing now needs `CREATE EXTENSION vector`
- the previous plain `postgres:16` image would not support the local pgvector path cleanly

### 9. Documentation updates

Files:

- [context_graph/README.md](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/README.md)
- [context_graph/LOCAL_DEV.md](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/LOCAL_DEV.md)

Updated to describe:

- the current semantic fact profile scope
- pgvector-backed profile retrieval
- local `sync-profile-vectors`
- local `parse-query`
- local `search-profiles`

## Retrieval Scope After These Changes

The system now has a clear split.

### Graph-native retrieval

Still responsible for:

- code structure
- temporal code evolution
- commits / PR / issue structure
- `MENTIONS`
- `ON`
- low-level traversal
- deterministic anchor resolution

Examples of graph-native edges:

- `CALLS`
- `DEFINES`
- `TOUCHES`
- `HAS_COMMIT`
- `ON`
- `MENTIONS`

### LightRAG-style profile retrieval

Currently responsible only for:

- semantic `RELATES_TO` facts

This means we now have a real high-level semantic retrieval channel without polluting it with low-level structural edges.

## Current Limitations

1. Only `RELATES_TO` is indexed into the LightRAG branch.
2. Profile vectors are currently key-only; we do not embed full `value_text`.
3. Query parsing currently mixes deterministic anchors with optional LLM-derived themes, but does not yet feed into the actual retrieve lambda.
4. Aurora `pgvector` is implemented in the Python layer, but AWS infra env/IAM wiring for embedding usage still needs to be finished.
5. Local `reset-context` now clears graph + metadata + vector keys when using the vector store path, but the main retrieve lambda is still a placeholder.

## Tests Added

File:

- [tests/test_context_graph.py](/Users/utya.kirill/Desktop/UOFT/Context-Graph/tests/test_context_graph.py)

Added coverage for:

- deterministic query parsing
- LLM-augmented query parsing
- Aurora pgvector bootstrap SQL
- semantic fact profile creation
- semantic fact profile deactivation
- vector indexing from semantic facts
- profile search service end-to-end using the in-memory vector store
- episode ingestion auto-syncing profile vectors

Current test command:

```bash
python3 -m unittest tests/test_context_graph.py -v
```

Current result during implementation:

- `43` passed
- `1` skipped

## Recommended Next Step

The next high-value step is to connect the new profile search path into the actual retrieve flow:

- parse incoming retrieve queries
- use deterministic anchors for graph-native retrieval
- use high-level keywords for `search-profiles`-style semantic profile retrieval
- merge profile hits with graph/episode hits under a token budget

That would complete the first usable LightRAG-style retrieval branch on top of the current graph.
