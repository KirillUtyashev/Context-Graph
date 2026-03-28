# Claim-Centric Provenance Pipeline

This directory contains the provenance layer that turns raw authored observations
into grounded claims, evidence, and claim relations.

The current system is claim-centric:

- observations are the raw source records
- extraction produces claim candidates plus exact supporting evidence
- validation filters and grounds those candidates deterministically
- claim planning reconciles new claims against existing claims
- evidence is attached to claims, not treated as the top-level fact model

Claim nodes are now the authoritative layer for currentness, reconciliation,
and invalidation. New evidence writes are lean: they keep only source-bound
support data plus claim attachments. Legacy evidence rows are still supported
for backfill and compatibility reads.

## Core Objects

### Observation

An `Observation` is a single raw authored artifact, for example:

- a commit message
- a GitHub PR body
- a GitHub issue body
- a PR review
- a review comment
- an issue comment

Observations are created outside this package by the ingestion entrypoints and
passed into provenance as source-local text plus metadata.

### Claim

A `Claim` is a normalized, coding-relevant proposition extracted from one
observation. Claims are persisted as first-class backend nodes.

Important current claim fields:

- `claim_text`
- `claim_kind`
- `claim_subkind`
- `canonical_key`
- `is_current`
- `first_observed_at`
- `latest_observed_at`
- `invalidated_at`
- `invalidated_by_claim_id`

Claims also keep stance information in metadata:

- `assertion_status`
- `claim_frame`

### Evidence

An `EvidenceRecord` is an exact support item extracted from a single
observation. Evidence stays source-bound and immutable.

Important evidence fields:

- `quote_text`
- `support_source`
- `span_start` / `span_end`
- `support_kind`

Claim-relative semantics now live on the evidence-to-claim attachment:

- `support_role`
- `support_rationale`

Those are written on `SUPPORTS_CLAIM` edges from evidence to claim.

## End-to-End Flow

The runtime flow is:

1. Build an observation.
2. Build a sanitized prompt payload for extraction.
3. Ask the extraction LLM for bounded claim candidates and exact support.
4. Validate the returned claims and evidence deterministically.
5. Resolve claim `ABOUT` targets against the current snapshot.
6. Reconcile each validated claim against existing persisted claims.
7. Persist claim nodes, evidence nodes, support edges, and claim relations.

### 1. Observation Creation

Observation creation happens in the ingestion entrypoints:

- `context_graph/ingestion/commit_indexing.py`
- `context_graph/ingestion/git/github_indexer.py`

Those layers construct the raw `Observation` and also provide bounded
deterministic context such as:

- changed files
- PR number and title
- issue number and title
- mentioned or touched code entities from the current snapshot

### 2. Sanitized Prompt Context

Extraction prompt payloads are built in `prompt_context.py`.

We do not send raw `Observation` dataclasses or backend internals to the LLM.
Instead we build a compact prompt DTO with three parts:

- `observation`
- `source_context`
- `code_context`

The extraction LLM currently sees only:

- observation:
  - `source_type`
  - `title`
  - `text`
  - `observed_at`
- source context:
  - commit `changed_files`
  - PR `pr_number`, `pr_title`
  - issue `issue_number`, `issue_title`
- code context:
  - up to 10 `changed_files`
  - up to 8 `mentioned_entities`
  - up to 8 `touched_entities`

We intentionally do not pass internal ids such as:

- `observation_id`
- `repo_id`
- `source_id`
- graph record ids
- raw metadata blobs

The goal is to let the model classify and scope the observation without asking
it to reason over internal storage details.

### 3. Claim + Evidence Extraction

`ClaimEvidenceExtractionService` in `services.py` chooses a source-specific prompt
from `prompts/extract_claims_and_evidence.py`.

The extraction prompt asks for small, source-local claim instances with:

- a normalized `claim_text`
- an ontology-aligned `claim_subkind`
- a coarse compatibility `claim_kind`
- `assertion_status`
- `claim_frame`
- 1 to 3 exact support items

Each support item must come from the observation title or text and includes:

- `quote_text`
- `support_source`
- `support_kind`
- `support_role`
- `support_rationale`

The prompt explicitly tells the model:

- not to infer repo-wide truths
- not to merge across documents
- not to treat context as quoted evidence
- to keep authored stance separate from ground truth

### 4. Deterministic Validation and Grounding

`ClaimBundleValidator` in `validation.py` is the hard gate before graph writes.
We do not run a second LLM for validation.

Validation currently checks:

- allowed enums for `claim_kind`, `claim_subkind`, `assertion_status`,
  `claim_frame`, `evidence_kind`, `support_kind`, `support_source`,
  and `support_role`
- the claim has 1 to 3 support items
- each support item has a non-empty `support_rationale`
- support quotes actually match `observation.text` or `observation.title`
- title-sourced support cannot use spans
- speculative language is rejected for `confirmed` and `observed` claims

If a snapshot is available, validation also resolves `about_hints` with
`SnapshotCodeResolver` and produces:

- `resolved_about_record_ids`
- `unresolved_about_hints`

At the end of validation we have a `ValidatedClaimBundle`, which is the
deterministic input to claim persistence.

### 5. Claim Reconciliation and Planning

`ClaimMutationPlanner` in `claim_planner.py` is the main claim-ingestion layer.

For each validated claim bundle it:

1. prepares evidence records with grounded `ABOUT` targets
2. computes the claim `canonical_key`
3. loads current candidate claims from the backend
4. backfills legacy evidence-carried claims into temporary claim candidates
5. checks for an exact canonical-key match
6. if needed, asks the claim-resolution LLM about a bounded candidate set
7. applies deterministic acceptance and winner-selection rules
8. emits mutation plans for claims, evidence, and relations

#### Canonical Identity

Claim identity is currently:

- `claim_kind`
- canonicalized `claim_text`
- sorted grounded `about_record_ids`

This becomes `canonical_key`.

`assertion_status` and `claim_frame` do not participate in identity.

#### Candidate Retrieval

Candidate retrieval is deterministic and local. Candidates are restricted to the
same repo and same coarse `claim_kind`, then filtered by scope:

- overlapping `ABOUT` targets when we have them
- otherwise same source scope such as PR or issue

Candidates are ranked by:

- `ABOUT` overlap
- lexical overlap of normalized claim text
- recency

Only a small candidate set is sent to the resolution LLM.

#### Claim Resolution

`ClaimResolutionService` in `services.py` uses
`prompts/relate_claims.py` to compare one new claim against bounded existing
candidates.

The relation set is closed:

- `same_as`
- `refines`
- `contradicts`
- `explains`
- `supersedes`
- `mitigates`
- `depends_on`
- `supports`
- `unrelated`

Important behavior:

- only `high` confidence results are accepted
- `same_as` is a resolution outcome, not a persisted graph edge
- accepted non-`same_as` results become real claim-to-claim edges

If no accepted claim-resolution result exists and the legacy conflict resolver is
configured, the planner can fall back to the older contradiction-only prompt in
`prompts/resolve_fact_conflicts.py` for migration compatibility.

### 6. Persistence

Persistence currently writes:

- claim nodes
- evidence nodes
- `ABOUT` edges from claim to grounded code targets
- `ABOUT` edges from evidence to grounded code targets
- `DERIVED_FROM` edges from claim to observation
- `DERIVED_FROM` edges from evidence to observation
- `SUPPORTS_CLAIM` edges from evidence to claim
- claim-to-claim relation edges

Persisted claim-to-claim edge types are:

- `REFINES`
- `CONTRADICTS`
- `EXPLAINS`
- `SUPERSEDES`
- `MITIGATES`
- `DEPENDS_ON`
- `SUPPORTS`

`SUPPORTS_CLAIM` edges currently carry:

- `support_kind`
- `support_source`
- `support_role`
- `support_rationale`
- `assertion_status`
- `claim_frame`

## Current Invalidation Semantics

Invalidation now happens at the claim level, not the evidence level.

When a new claim is accepted as contradicting or superseding an older current
claim:

- the old claim is persisted with `is_current = false`
- the new claim stays current if it is newer
- if the new claim is older, the new claim is inserted already non-current
- evidence remains immutable provenance

This means we preserve:

- the old claim
- the new claim
- the claim-to-claim contradiction or supersession relation
- the exact evidence supporting each side

`rejected` claims are stored as historical, non-current claims and do not
participate in conflict resolution.

## Migration State

The migration is now claim-first for new writes.

Current behavior:

- new claim nodes carry identity, currentness, and invalidation state
- new evidence nodes store only source-bound support fields
- new evidence-to-claim semantics live on `SUPPORTS_CLAIM`
- legacy evidence rows with embedded claim fields are still readable for backfill

Why legacy parsing still exists:

- older persisted evidence needs to reconcile into claim nodes
- compatibility readers still need to understand pre-claim data already in the graph

The intended steady state is now mostly in place:

- claim identity and currentness live on claim nodes
- evidence stores exact support only
- claim-relative support meaning lives on `SUPPORTS_CLAIM`

## What the LLM Does vs. What Stays Deterministic

### LLM responsibilities

- extract candidate claims from one observation
- extract exact support items from that observation
- classify claim subkind and stance
- propose semantic relations between a new claim and bounded candidates

### Deterministic responsibilities

- observation creation
- prompt sanitization
- source and code context construction
- evidence quote/span validation
- code/entity grounding
- candidate retrieval
- canonical-key construction
- graph writes
- confidence thresholds
- current/non-current winner selection

This is the core design principle of the current pipeline:

- the LLM proposes
- the pipeline verifies and writes

## Debugging

Set:

```bash
CONTEXT_GRAPH_DEBUG_PROVENANCE_LLM=1
```

When enabled, provenance emits structured debug logs to stderr through
`debug.py`.

Current debug events include:

- `llm_request`
- `llm_response`
- `llm_error`
- `claim_validation`
- `claim_resolution`
- `claim_resolution_compat`

This is the easiest way to inspect:

- the sanitized extraction prompt payload
- the parsed LLM output
- validation rejection reasons
- accepted claim-resolution outcomes

## Main Files

- `models.py`
  - domain models for observations, evidence, claims, and LLM responses
- `prompt_context.py`
  - sanitized extraction prompt payload builders
- `services.py`
  - LLM-facing service boundaries
- `validation.py`
  - deterministic claim and evidence validation
- `evidence_planner.py`
  - evidence nodes and evidence-to-claim attachment planning
- `claim_planner.py`
  - claim persistence, reconciliation, backfill, and relation planning
- `invalidation.py`
  - compatibility wrapper for claim-level invalidation flows
- `schema_registry.py`
  - allowed ontology enums and compatibility mappings
- `prompts/extract_claims_and_evidence.py`
  - source-specific extraction prompts
- `prompts/relate_claims.py`
  - bounded claim-resolution prompts
- `prompts/resolve_fact_conflicts.py`
  - legacy contradiction-only compatibility prompt

## What Is Not Done Yet

The current implementation is intentionally conservative. We do not yet have:

- embedding-based candidate retrieval
- a separate canonical claim promotion layer beyond the current `canonical_key`
- read APIs fully switched over to claim-first access patterns
- complete removal of legacy evidence-field readers once old rows no longer matter
- richer authority models for deciding which claim should remain current

Those are the next layers on top of the current claim-centric ingestion path.
