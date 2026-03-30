# Additive Backend Interface

This package introduces a new provider-agnostic storage backend at
[context_graph/storage/backend](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/storage/backend).
It is additive and intentionally separate from the archived Graphiti storage
code in
[context_graph/storage/graph/graphiti-backend](/Users/utya.kirill/Desktop/UOFT/Context-Graph/context_graph/storage/graph/graphiti-backend).

## What It Does

The backend provides:

- a shared graph-driver interface for Neo4j and Neptune
- provider-neutral search and traversal contracts
- provider-neutral mutation contracts for deterministic graph writes
- concrete metadata stores for in-memory, SQLite, and PostgreSQL
- concrete profile-vector stores for in-memory and pgvector/PostgreSQL
- lexical graph search support in V1
- raw graph record hydration via `fetch()`

The backend does not know about product ontology terms such as `Claim`,
`Theory`, or `ResearchArtifact`. Those are expressed through open-ended
`record_type` values.

## `RecordKind`

`RecordKind` is intentionally small:

- `node`
- `edge`

It models physical graph shape only. Domain meaning stays in `record_type`, for
example:

- `file`
- `symbol`
- `task`
- `claim`
- `theory`
- `research_artifact`
- `supports`
- `contradicts`

`research_artifact` is therefore represented as:

- `record_kind = node`
- `record_type = "research_artifact"`

## Search Model

Lexical search in V1 uses dedicated search documents rather than indexing
arbitrary graph labels or relationship types directly.

This keeps the backend generic:

- Neo4j stores `NodeSearchDocument` and `EdgeSearchDocument` nodes and uses
  native full-text indexes over their `search_text`
- Neptune uses AOSS sidecar indices with the same search-document fields

Search returns lightweight `GraphRecordRef` objects. `fetch()` then hydrates the
real graph node or edge by `record_id`.

## Mutation Model

Deterministic ingestion writes through typed mutation records instead of raw
Cypher strings:

- `NodeRecord`
- `EdgeRecord`
- `SearchDocumentRecord`
- `MutationPlan`
- `MutationApplyResult`

This keeps provider-specific write logic inside the backend:

- Neo4j writes graph nodes/edges plus `NodeSearchDocument` /
  `EdgeSearchDocument` nodes
- Neptune writes graph nodes/edges and mirrors search documents into AOSS when
  lexical search is enabled

The shared write surface stays deliberately small in V1:

- upsert graph nodes
- upsert graph edges
- upsert lexical search documents

## Metadata Model

The metadata package is intentionally domain-aware in V1 because the rest of the
repo already relies on repository and profile concepts.

It stores:

- latest graph snapshots
- repo index state
- hosted repo source state
- context nodes
- semantic profiles
- index run history

Canonical names live under:

- `context_graph.storage.backend.metadata`

Compatibility aliases remain available from:

- `context_graph.metadata`

## Profile Vectors

The vector side is explicitly profile-aware in V1. It stores profile-key
embeddings and returns `ProfileVectorHit` results used by the existing
`ProfileVectorIndexer` and `ProfileSearchService`.

Canonical names live under:

- `context_graph.storage.backend.profile_vectors`

Canonical PostgreSQL naming is:

- `PgVectorProfileVectorStore`

Compatibility aliases remain available as:

- `AuroraProfileVectorStore`

## Provider Differences

### Neo4j

- native transaction support
- native full-text search
- lexical search fully implemented in V1
- traversal, vector, and hybrid search are deferred

### Neptune

- no native transaction semantics in this layer
- lexical search only when `aoss_host` is configured
- AOSS is hidden inside the provider implementation
- traversal, vector, and hybrid search are deferred

## V1 Scope

Implemented now:

- graph driver lifecycle
- graph capability reporting
- lexical graph search
- graph record hydration with `fetch()`
- typed mutation planning/apply support
- Neo4j full-text bootstrap
- Neptune AOSS index bootstrap
- metadata store interfaces and providers
- profile-vector store interfaces and providers

Deferred for later:

- vector search
- hybrid search
- traversal execution
- public write-side search document sync APIs
- moving profile indexing/search services into the backend package

## Usage

```python
from context_graph.backend import (
    MutationPlan,
    NodeRecord,
    GraphRecordRef,
    InMemoryMetadataConfig,
    InMemoryProfileVectorConfig,
    Neo4jConfig,
    RecordKind,
    SearchMode,
    SearchQuery,
    create_driver,
    create_metadata_store,
    create_profile_vector_store,
)


async def demo() -> None:
    metadata = create_metadata_store(InMemoryMetadataConfig())
    driver = create_driver(
        Neo4jConfig(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="contextgraph",
        )
    )

    await driver.build_indices()
    vector_store = create_profile_vector_store(
        InMemoryProfileVectorConfig(),
    )
    vector_store.bootstrap()

    await driver.mutations.apply(
        MutationPlan(
            upsert_nodes=(
                NodeRecord(
                    record_id="project:checkout-service",
                    record_type="project",
                    repo_id="checkout-service",
                    group_id="checkout-service",
                    labels=("BackendRecord", "CodeNode", "Project"),
                    properties={"node_id": "project:checkout-service",
                                "name": "checkout-service"},
                ),
            )
        )
    )

    hits = await driver.search.search(
        SearchQuery(
            mode=SearchMode.LEXICAL,
            text="payment retry behavior",
            record_kinds=(RecordKind.NODE,),
            record_types=("research_artifact", "claim"),
            repo_ids=("checkout-service",),
            limit=5,
        )
    )

    records = await driver.search.fetch([hit.ref for hit in hits])
    await driver.close()
```
