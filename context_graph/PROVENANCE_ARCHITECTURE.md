# Provenance Architecture Note

This note captures the high-level direction for evolving `context_graph` away from a Graphiti-shaped architecture and toward a temporal provenance system for coding agents.

## Core Position

Graphiti can remain a useful extraction backend, but it should not define:

- the top-level concepts of the repo
- the public MCP surface
- the product identity
- the benchmark schema

Instead, the repo should be organized around temporal coding provenance.

## Top-Level Concepts

The system should be centered on these product concepts:

- `project_id`: top-level workspace or software project
- `repo_id`: one repository within a project
- code entities: repository, file, symbol, test, config, API, commit, PR, issue
- observations: raw events or sources of information
- evidence: the specific pieces of information extracted from observations
- claims: normalized statements supported by evidence
- tasks: objectives such as fixing a bug or implementing a feature
- repair sessions: one concrete attempt or run against a task

## Multi-Repo Model

Multi-repo support should be first-class.

- One `project_id` can contain multiple `repo_id` values.
- Snapshots are still generated per repository.
- Provenance can connect entities across repositories inside the same project.
- If the backend still requires `group_id`, it should be treated as an internal mapping:
  - `group_id = project_id`
  - `repo_id` remains an explicit field on nodes and edges

This keeps `project_id` and `repo_id` as product concepts instead of leaking backend terminology.

## Layered Model

The cleanest mental model is five layers.

### 1. Project and Repository Layer

Deterministic project structure:

- `Project`
- `Repository`

Recommended relation:

- `Project -[:HAS_REPO]-> Repository`

### 2. Code Entity Layer

This mostly already exists.

Examples:

- `File`
- `Symbol`
- `TestCase`
- `ConfigKey`
- `Commit`
- `PullRequest`
- `Issue`

This layer remains the deterministic code and artifact graph.

### 3. Observation Layer

An observation is any raw event or source that enters the system.

Examples:

- user message
- agent message
- PR review comment
- commit message
- test run output
- log snippet
- benchmark hint

`Episode` should remain as a useful ingestion primitive for text-bearing observations, but it should not be the only conceptual unit in the model.

Useful distinction:

- `Observation`: any raw event or source, including structured events
- `Episode`: a text-bearing observation

This allows the system to later ingest structured sources without forcing everything into an episode-shaped container.

### 4. Evidence and Claim Layer

This is the main missing layer in the current design.

Important distinction:

- an observation is where something was seen
- evidence is the exact relevant piece extracted from that observation
- a claim is the normalized statement the system believes based on that evidence

Example:

- one user message may contain multiple distinct pieces of evidence
- those evidence items may support different claims
- different claims may affect different files, symbols, or patches

This layer is what makes the benchmark-friendly features possible:

- citations
- authority
- freshness
- contradiction handling
- hunk justifications

### 5. Task and Repair Session Layer

Tasks and repair sessions should be explicit deterministic nodes, not episodes.

- `Task`: the objective
- `RepairSession`: one attempt or working timeline for that task

This gives the graph a natural place to attach:

- observations
- tool runs
- decisions
- patches
- tests
- final outcomes

## How This Fits the Current Pipeline

The current pipeline does not need to be thrown away. It can be extended.

### What stays as-is

- snapshot generation per repo
- deterministic code entity extraction
- temporal diffing and graph application
- episode ingestion for text-bearing sources

### What changes

The current `EpisodeProcessor` should evolve conceptually into an observation and evidence pipeline.

A reasonable staged flow is:

1. ingest repo snapshots per `repo_id`
2. ingest observations for a `project_id` and optional `task_id` / `repair_session_id`
3. store raw text observations as episodes when appropriate
4. derive evidence records from episodes, tests, logs, and benchmark events
5. derive claims from evidence
6. resolve conflicts using freshness and authority rules
7. answer provenance-centric MCP queries over tasks, sessions, files, symbols, claims, and evidence

## Why Evidence Should Not Be Just an Episode

Evidence can come from an episode, but it should not be treated as equivalent to an episode.

Reasons:

- one episode can contain multiple separate evidence items
- some evidence is structured, not conversational
- benchmark evaluation wants citation-level precision, not whole-message granularity
- authority and contradiction usually apply to specific claims or evidence items, not to the entire raw episode

So the recommended model is:

- `Episode` stores the raw text event
- `EvidenceRecord` stores the extracted relevant pieces
- `Claim` stores the normalized statements derived from evidence

## Task and Repair Session Modeling

Recommended semantics:

- `Task`: the durable objective
- `RepairSession`: one concrete attempt, run, or interaction timeline for the task

Examples:

- A bug report is a task.
- An agent trying to fix that bug today is a repair session.
- A second attempt after new user clarification is another repair session, or a continuation depending on product semantics.

Suggested relations:

- `Task -[:IN_PROJECT]-> Project`
- `RepairSession -[:FOR_TASK]-> Task`
- `Observation -[:IN_SESSION]-> RepairSession`
- `Observation -[:ABOUT]-> File`
- `Observation -[:ABOUT]-> Symbol`
- `Claim -[:ABOUT]-> File`
- `Claim -[:ABOUT]-> Symbol`

## Contradiction, Authority, and Freshness

These fit best at the evidence and claim layer, not at the raw episode layer.

### Freshness

Freshness should mostly be represented using timestamps and supersession.

Useful fields:

- `observed_at`
- `valid_from`
- `valid_to`

Useful relation:

- `Claim -[:SUPERSEDES]-> Claim`

Examples:

- a later failing test run supersedes an older passing run for the same scenario
- a later user clarification supersedes an earlier ambiguous instruction

### Contradiction

Contradiction should be explicit between normalized claims.

Useful relation:

- `Claim -[:CONTRADICTS]-> Claim`

Examples:

- “API must remain unchanged”
- “breaking the API is acceptable”

These should not just coexist as unrelated facts.

### Authority

Authority can start as a rule-based property or resolution layer.

Useful fields:

- `authority_kind`
- `authority_score`
- `source_kind`
- `derivation_kind`

Examples of likely authority order:

- benchmark oracle or explicit user clarification
- deterministic repository state
- recent test output or logs
- code comments or stale docs
- LLM-inferred claims

This does not need to be fully learned at first. A deterministic rule system is a good starting point.

## Recommended Minimal Schema

The exact implementation can vary, but this is a practical starting point.

### Node types

- `Project`
- `Repository`
- `Task`
- `RepairSession`
- `Observation`
- `Episode`
- `EvidenceRecord`
- `Claim`
- existing code and artifact entities such as `File`, `Symbol`, `Commit`, `PullRequest`, `Issue`

### Edge types

- `HAS_REPO`
- `IN_PROJECT`
- `FOR_TASK`
- `IN_SESSION`
- `ABOUT`
- `FROM_OBSERVATION`
- `SUPPORTS`
- `CITES`
- `CONTRADICTS`
- `SUPERSEDES`
- `OVERRIDES`

## MCP Implications

The public MCP should become provenance-centric rather than generic-memory-centric.

Examples of future tool directions:

- `get_symbol_provenance`
- `get_file_timeline`
- `get_latest_authoritative_evidence`
- `find_conflicting_claims`
- `cross_repo_impact`
- `build_patch_justification`

This is a better match for the benchmark and for the product identity of the project.

## Recommended Incremental Implementation Plan

To minimize churn, the next implementation steps should be:

1. Keep `Episode` as the current text-ingestion primitive.
2. Add explicit `Project`, `Task`, and `RepairSession` concepts.
3. Add `EvidenceRecord` as a new derived layer on top of episodes and other observations.
4. Add `Claim` as a normalized statement layer above evidence.
5. Store contradiction, freshness, and authority on evidence and claims, not on episodes.
6. Move Graphiti-shaped code behind general interfaces and isolate it under a backend-specific implementation.
7. Redesign the MCP around provenance queries over files, symbols, tasks, sessions, evidence, and claims.

## Practical Refactor Rule

The simplest architectural rule for the refactor is:

- keep Graphiti-compatible code only behind backend interfaces
- make the rest of the system speak in terms of projects, repos, tasks, sessions, observations, evidence, claims, and provenance

That preserves the useful extraction work while changing the identity of the system from a generic memory graph to a temporal provenance platform for coding agents.
