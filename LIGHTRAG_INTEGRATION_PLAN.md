# LightRAG-Inspired Retrieval Integration Plan

## Goal

Integrate a LightRAG-style high-level retrieval layer into our system without replacing the parts of Graphiti that already work well.

The intended design is:

- keep Graphiti-style retrieval for entity nodes
- keep Graphiti-style retrieval for fact/edge nodes
- replace Graphiti community retrieval with our own edge/profile-based key/value lookup
- keep the temporal graph as the canonical source of truth

This gives us a hybrid system:

- graph DB for structure, temporality, provenance, and traversal
- SQL metadata store for retrieval profiles
- vector DB for profile-key lookup

## What We Are Replacing

We are **not** replacing all of Graphiti search.

We are specifically replacing the **community retrieval branch** with a LightRAG-inspired high-level retrieval branch.

What stays:

- edge BM25 / cosine / BFS retrieval
- node BM25 / cosine / BFS retrieval
- episode retrieval
- temporal edge invalidation and historical graph state

What changes:

- community summaries are no longer the main high-level abstraction
- instead, we retrieve high-level **relation profiles** using key/value search

## Core Design

### 1. Canonical storage

The graph remains the source of truth for:

- entities and facts
- temporal validity
- provenance links
- neighborhood traversal

The SQL metadata store keeps retrieval profiles.

The vector DB stores embeddings for profile keys and points back to profile IDs.

### 2. Profiles

A profile is a retrieval-oriented record attached to a graph relation, fact, or relation-centered neighborhood.

We already created the `cg_profiles` table for this.

Initial profile fields:

- `profile_id`
- `repo_id`
- `profile_kind`
- `profile_target_kind`
- `profile_target_id`
- `relation_type`
- `source_node_id`
- `source_node_kind`
- `target_node_id`
- `target_node_kind`
- `target_revision_id`
- `value_text`
- `keys_json`
- `metadata_json`
- `is_active`
- `valid_from_revision`
- `valid_to_revision`
- `updated_at`

### 3. What a profile represents

Profiles should represent higher-level context that communities were previously trying to provide.

Important scope decision:

- in the first version, profile retrieval is for **edges/relations**, not entity nodes
- entity-node retrieval remains Graphiti-native
- profile search is the new high-level relation-centric channel

Candidate first profile kinds:

- `code_relation_profile`
- `commit_relation_profile`
- `pull_request_relation_profile`
- `issue_relation_profile`
- `decision_relation_profile`
- `subsystem_relation_profile`

The first implementation should favor deterministic profile generation over LLM-written summaries wherever the relation comes from deterministic indexing data.

## How Search Should Work

### Existing Graphiti channels we keep

We keep these search channels as they are:

- node BM25 / cosine / BFS
- edge BM25 / cosine / BFS
- episode BM25

These already operate on full-query text and full-query embeddings.

### New high-level profile retrieval channel

The new channel should work like this:

1. Parse the query into:
- low-level anchors
- high-level keywords

2. Use the high-level keywords to search the profile-key vector index.

3. Resolve matching profile IDs from the vector DB back to `cg_profiles`.

4. Fetch the linked edge IDs and linked graph IDs from profile metadata.

5. Expand locally in the graph around those linked nodes/edges when needed.

6. Merge profile results with node/edge/episode results.

Important note:

- low-level anchors should continue to rely mainly on existing node/edge retrieval
- the new profile retrieval is intended to replace **communities**, not replace all retrieval
- we do **not** need a separate LightRAG-style key store for entities in the first version

## Query Parsing Plan

We should not blindly copy LightRAG's query parsing.

For our domain, query parsing should combine:

- deterministic extraction for code-specific anchors
- optional LLM extraction for higher-level themes

Deterministic anchors:

- function/class names
- file paths
- PR numbers
- issue numbers
- commit SHAs
- API routes
- error strings

LLM-generated high-level keywords:

- subsystem/theme labels
- design concerns
- intent words like "why", "impact", "rollback", "constraint", "migration"

## How To Build Profile Values

We do need `value_text`, but it does not need to be LLM-generated at first.

The first version should be deterministic and templated from existing graph/metadata fields.

Examples:

- code relation profile value:
  - edge type such as `CALLS`, `IMPORTS`, `DEFINES`, `CONTAINS`
  - source and target names
  - source and target kinds
  - file path or module context when available
  - revision metadata when available

- PR / issue / commit relation profile value:
  - relation type
  - linked artifact identifiers
  - short deterministic intent text from stored metadata
  - linked files/functions/decisions if available

Important distinction:

- deterministic code edges should get deterministic profile values
- LLM-extracted semantic facts can later get relation profile values built from the fact text and provenance

## How To Build Profile Keys

We should support multiple keys per profile.

Examples:

- relation type
- source/target canonical names
- qualified names when available
- path-like aliases
- subsystem labels
- PR / issue identifiers
- decision labels

For the first version, we should embed only **relation-profile keys** in the vector DB.

That is acceptable because:

- node and edge semantic retrieval already remains in Graphiti
- the profile channel is replacing communities, not replacing fact/entity semantic search

If recall is weak later, we can add value embeddings as a second retrieval channel.

## Temporal Model

Profiles should not replace the temporal graph model.

Instead:

- current/default retrieval uses `is_active = true`
- history retrieval uses metadata filters over active and inactive profiles
- the graph remains responsible for temporal validity and invalidation semantics

Default mode:

- search active profiles only
- ignore expired profiles unless explicitly requested

History mode:

- include inactive profiles
- optionally filter by revision or time range
- use graph history / invalidated edges when answering historical questions

We should keep one profile store and one vector index at first, with metadata filtering instead of separate current/history databases.

## Global Result Mixing

Graphiti currently reranks per result type and does not do a global balancing step across edges, nodes, episodes, and communities.

We will need to implement this ourselves.

Planned merged retrieval output:

- edge results
- node results
- episode results
- profile results

We need a global budgeting policy such as:

- fixed caps per type
- intent-aware caps per type
- or score normalization plus a total context budget

First implementation recommendation:

- use explicit caps per type
- then evaluate whether adaptive balancing is worth adding

Example first-pass budget:

- edges: 8
- nodes: 6
- episodes: 3
- profiles: 5

These numbers should be tuned during evaluation.

## Implementation Steps

### Phase 1. Metadata and schema

1. Add profile CRUD helpers on top of `cg_profiles`.
2. Define `ProfileRecord` / `ProfileKind` models in code.
3. Add serialization helpers for `keys_json` and `metadata_json`.

### Phase 2. Profile generation

4. Implement deterministic profile builders for:
- code edges / relations
- pull request relations
- issue relations
- commit relations
- decision relations when available

5. Attach profile generation to indexing / ingestion so changed artifacts update profiles.
6. Mark stale profiles inactive when newer versions replace them.

Important scope rule for Phase 2:

- do **not** add separate LightRAG-style profile builders for entity nodes in the first milestone
- code/entity nodes continue to use existing Graphiti retrieval
- profile generation should focus on high-level relation-style retrieval

### Phase 3. Vector indexing

7. Choose a vector store for profile-key embeddings.
8. Create a profile-key index schema:
- `profile_id`
- `key_text`
- `repo_id`
- `profile_kind`
- `is_active`
- revision metadata

9. Add sync logic from SQL profiles to the vector index.

### Phase 4. Query parsing

10. Implement deterministic extraction of code/provenance anchors from queries.
11. Implement an LLM-based high-level keyword extractor for broader themes.
12. Separate query parsing into:
- low-level anchors for node/edge retrieval
- high-level keywords for relation-profile retrieval

### Phase 5. Retrieval channel

13. Implement `profile_search(...)` as a new retrieval branch.
14. Search vector DB with high-level keywords.
15. Resolve matching profile IDs back to SQL.
16. Expand linked graph neighborhoods when needed.

### Phase 6. Global merge and context building

17. Replace community outputs in the advanced search pipeline with relation-profile outputs.
18. Add a global merge policy across:
- edges
- nodes
- episodes
- profiles

19. Build a new context constructor that emits profile context instead of community context.

### Phase 7. History-aware retrieval

20. Add query modes:
- current
- history
- diff

21. Filter profiles by activity / revision metadata.
22. Expose history-aware retrieval through a dedicated tool or flag instead of enabling it by default.

### Phase 8. Evaluation

23. Compare:
- Graphiti-style retrieval with communities
- our hybrid retrieval with profiles

24. Evaluate on:
- repository QA
- file/function localization
- provenance lookup
- code-edit planning

25. Measure:
- retrieval relevance
- downstream localization accuracy
- patch/test success on coding-agent tasks

## Immediate Next Tasks

The next concrete tasks we should implement in the repo are:

1. add profile models and CRUD helpers on top of `cg_profiles`
2. define the first 2-3 relation-profile kinds
3. generate deterministic `value_text` and `keys_json` for code edges and provenance relations
4. choose and wire a relation-profile key vector index
5. add a standalone `profile_search(...)` function for relation profiles
6. decide the first global result-budget policy

## Open Design Questions

These still need explicit decisions:

1. Which profile kinds are required for the first milestone?
2. Should subsystem profiles be derived from folders, imports, or commit clusters?
3. How should profile scores be normalized against node/edge scores?
4. Should profile search expand 1 hop only, or use type-specific expansion rules?
5. When do we regenerate profiles: every index run, only on changed artifacts, or both?

## Success Condition

This integration is successful if:

- we no longer depend on Graphiti communities for high-level retrieval
- profile retrieval improves high-level context selection
- the graph still handles temporal and structural reasoning
- the combined system performs better than plain Graphiti retrieval on coding-agent tasks
