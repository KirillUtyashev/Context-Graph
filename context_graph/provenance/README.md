# Provenance Fact Invalidation

This document describes the current "fact invalidation before claim nodes" implementation in `context_graph/provenance`.

## Why this exists

The provenance pipeline already extracts observations, validates claim bundles, and persists evidence records. What it did not previously do was handle the case where a newly ingested fact contradicts the current evidence for the same grounded target.

Until first-class `Claim` nodes exist, we treat each validated claim bundle as a provisional fact carried by its `EvidenceRecord`s. That gives us a place to:

- preserve contradictory history instead of overwriting it
- mark older evidence as no longer current
- connect newer winning evidence to older losing evidence with `SUPERSEDES`

This implementation is intentionally narrow and conservative.

## Current scope

V1 invalidation only runs when all of the following are true:

- the facts are in the same repo
- the facts share the same `claim_kind`
- the evidence overlaps on grounded `ABOUT` targets
- the existing evidence is still current

Conflict resolution is recency-based:

- newer `observed_at` wins
- if timestamps tie, the newly ingested fact wins
- if either timestamp is missing or unparsable, the newly ingested fact wins

Exact duplicates do not invalidate each other. We still keep separate evidence citations.

## Data model changes

`context_graph/provenance/models.py` extends `EvidenceRecord` with provisional fact state:

- `fact_key`
- `claim_text`
- `claim_kind`
- `claim_confidence`
- `claim_derivation_kind`
- `observation_id`
- `observed_at`
- `is_current`
- `invalidated_at`
- `invalidated_by_fact_key`
- `invalidated_by_observation_id`

These fields are mirrored onto both backend node properties and evidence search documents so later retrieval code can distinguish current evidence from historical evidence without losing provenance.

The same module also adds:

- `PersistedEvidenceRecord`: one hydrated evidence record plus its grounded `ABOUT` targets
- `PersistedEvidenceFact`: one current fact candidate aggregated from one or more persisted evidence records

## Main components

### `EvidenceMutationPlanner`

`context_graph/provenance/evidence_planner.py` is responsible for turning validated claim bundles into evidence mutation records.

Key responsibilities:

- prepare evidence records for a validated claim bundle
- attach provisional fact state to those records
- mirror fact state onto backend nodes and search docs
- update existing persisted evidence to `is_current = false`
- emit `SUPERSEDES` edges in either direction

The planner keeps the evidence layer source-bound: identical facts from different observations are not deduplicated into one record.

### `CurrentEvidenceReader`

`context_graph/provenance/invalidation.py` contains a backend query helper that loads the newest current evidence candidates for a new fact.

It filters candidates by:

- `repo_id`
- overlapping `ABOUT` target ids
- `claim_kind`
- `is_current = true` or missing `is_current`

It also backfills legacy rows when claim fields only exist in `metadata_json` or when `observed_at` has to be recovered from the `DERIVED_FROM` observation node.

### `FactConflictResolutionService`

`context_graph/provenance/services.py` and `context_graph/provenance/prompts/resolve_fact_conflicts.py` define the provenance-local contradiction check.

The service compares:

- one new fact
- up to ten current fact candidates

The prompt returns only the ids of contradicted current candidates. It does not choose the winner; winner selection stays deterministic in Python.

### `EvidenceInvalidationPlanner`

`context_graph/provenance/invalidation.py` orchestrates the full invalidation flow.

For each validated claim bundle it:

1. prepares the new evidence records
2. computes the new `fact_key`
3. loads current persisted fact candidates
4. runs contradiction resolution
5. chooses the winner by recency
6. emits insertion and invalidation mutation plans

## Runtime flow

The current ingestion path for commit and GitHub provenance looks like this:

1. Create an `Observation`.
2. Run claim-and-evidence extraction.
3. Validate and ground the resulting claim bundles.
4. For each validated bundle, prepare evidence records with grounded `ABOUT` targets.
5. Load current evidence candidates that match the new bundle's repo, claim kind, and grounded targets.
6. Exclude exact duplicates by `fact_key`.
7. Ask the contradiction resolver which current candidates are actually contradicted.
8. If there are no contradicted current facts, insert the new evidence as current.
9. If a contradicted current fact is strictly newer, insert the new evidence already invalidated and connect the existing winner to the new loser with `SUPERSEDES`.
10. Otherwise, insert the new evidence as current, invalidate the older contradictory evidence, and connect the new winner to the old losers with `SUPERSEDES`.

This means history is append-only at the conceptual level: we preserve old evidence, but we stop treating it as current.

## Conflict semantics

The implementation intentionally separates "is this contradictory?" from "which side wins?"

### Contradiction detection

Contradiction detection is handled by the LLM prompt and only decides whether the new fact conflicts with a current candidate.

### Winner selection

Winner selection is deterministic:

- existing fact wins only if its `observed_at` is strictly newer than the new observation
- otherwise the new fact wins

That policy makes ties and incomplete timestamps resolve in favor of the new ingest, which keeps the pipeline simple and predictable for now.

## Legacy compatibility

This implementation is designed to work with evidence that predates the new top-level fact fields.

The reader treats missing `is_current` as current and attempts to recover:

- `claim_text`
- `claim_kind`
- `claim_confidence`
- `claim_derivation_kind`
- `observation_id`
- `observed_at`

from existing metadata and observation links.

No migration is required for the feature to work.

## Fail-open behavior

The invalidation layer is deliberately non-blocking.

- If there is no readable backend driver, we skip persisted-current lookup and insert the new evidence unchanged.
- If contradiction resolution fails, we insert the new evidence unchanged.
- If no grounded `ABOUT` targets are available, invalidation does not run.

This keeps provenance ingestion usable even when the invalidation path cannot execute.

## Ingestion integration

The invalidation planner is wired into both provenance ingestion entry points:

- `context_graph/ingestion/commit_indexing.py`
- `context_graph/ingestion/git/github_indexer.py`

Both flows now report invalidation counts, and the CLI surfaces those counts through `evidence_invalidated`.

## Tests covered

`tests/test_provenance.py` includes coverage for the main behaviors:

- loading legacy evidence rows whose claim fields only live in `metadata_json`
- newer contradictory fact invalidates older current evidence
- older historical fact is inserted already invalidated
- exact duplicate `fact_key` does not invalidate anything
- different grounded `ABOUT` targets do not invalidate anything
- commit ingestion reports invalidated evidence counts
- GitHub ingestion reports invalidated evidence counts
- `SUPERSEDES` edges are emitted in the expected direction

## What this does not do yet

This implementation does not yet add first-class `Claim` nodes.

It also does not yet implement:

- authority-based winner selection
- explicit contradiction edges between claims
- retrieval APIs that filter to only current authoritative evidence
- cross-observation fact canonicalization beyond the provisional `fact_key`

Those are the next layer on top of this groundwork.
