# Course Project Plan

## Goal

This project should be framed as a **provenance-aware context system for coding agents**, not as a thin integration of existing tools.

The intended story is:

- deterministic code indexing gives us precise repository structure
- Graphiti gives us temporal semantic memory over unstructured artifacts
- our contribution is the **bridge**, **ontology**, **retrieval/augmentation method**, and **evaluation**

## Current Risk

If we stop at the current version of the repo, the project can still be read as:

- a wrapper around a vendored code indexing pipeline
- a wrapper around Graphiti
- plus deployment and provider glue

There is real engineering work here already, but without a stronger end-to-end contribution it may still look like a systems integration project rather than an original project.

## What Makes This Not "Just a Wrapper"

To avoid the wrapper label, the center of the project must be something that neither upstream project provides on its own.

The core claim we want to make is:

> We built a hybrid temporal context graph for coding agents that combines deterministic code structure, temporal provenance, and semantically extracted developer artifacts in order to improve code-writing tasks.

That means our novelty should live in:

- a unified representation of code + provenance
- a custom retrieval/augmentation method for agent tasks
- an evaluation that shows our system helps an agent do real repository work

## Proposed Technical Contributions

### 1. Bridge indexing and Graphiti

Implement a Graphiti-backed ingestion path instead of keeping indexing and Graphiti as parallel systems.

Planned work:

- add a `GraphitiGraphSink` or equivalent bridge layer
- convert indexed artifacts into Graphiti episodes
- emit episodes from:
  - changed files
  - commits
  - PR descriptions and review comments
  - issues
  - agent/user conversations
- link semantic entities back to stable code node IDs from the deterministic graph

### 2. Define a code/provenance ontology

We need first-class entities that are specific to our project domain.

Planned entities:

- `Repository`
- `File`
- `Function`
- `Class`
- `Commit`
- `PullRequest`
- `Issue`
- `ConversationTurn`
- `Decision`

Important point:

- GitHub ingestion alone is still just integration
- it becomes a contribution when these artifacts are represented as first-class provenance objects and used during retrieval and augmentation

### 3. Build a provenance-aware retrieval and augmentation method

This is one of the most important pieces for originality.

The retrieval layer should combine:

- structural code graph information
- temporal version information
- Graphiti facts and episodes
- GitHub artifacts such as PRs and issues
- provenance signals such as recency, author, discussion links, and decision trails

This method should help a coding agent:

- find the right files/functions to edit
- understand why code was changed
- surface constraints from PRs/issues/discussions
- avoid stale or contradicted edits
- produce better grounded patches

### 4. Support coding-agent tasks, not only QA

The project should not be framed as only repository question answering.

The stronger framing is:

- helping coding agents plan edits
- helping coding agents choose what to change
- helping coding agents write more grounded code
- helping coding agents respect prior context and rationale

Representative tasks:

- bug fixing from an issue
- implementing a small feature from a PR description
- following a design constraint from discussion history
- making a code change that is consistent with recent commits

## Evaluation Plan

We should keep a retrieval/QA-style evaluation, but only as a proxy layer.

The main evaluation should be on coding-agent tasks.

### Baselines

We should compare against:

- code graph only
- Graphiti only
- our hybrid system

If possible, also compare against:

- naive file-level retrieval
- keyword/semantic retrieval without provenance-aware ranking

### Task Types

Candidate evaluation tasks:

- answer repository provenance questions
- identify the correct files/functions to edit
- generate a change plan for an issue
- apply a small patch and run tests
- explain why a function/module changed using commit/PR/issue context

### Metrics

Useful metrics include:

- retrieval relevance
- correct file/function localization
- patch success or test pass rate
- fewer unnecessary edits
- fewer contradictions with PRs/issues/discussions
- better grounding in provenance artifacts

### What Would Be Strong Evidence

Strong evidence that this is not a wrapper:

- our hybrid retrieval beats code-graph-only and Graphiti-only baselines
- provenance artifacts improve file selection or patch quality
- the agent makes fewer incorrect or stale edits when provenance-aware context is available

## What Does Not Count As Enough Novelty

These are useful, but they are not enough by themselves:

- deployment automation
- cloud infrastructure work
- Bedrock/MCP/provider integration
- scraping GitHub data without using it in the model or retrieval method
- exposing vendored project capabilities through a thin API

## Presentation / Report Framing

Bad framing:

- "We integrated Graphiti and a code indexing project."

Better framing:

- "We designed and evaluated a provenance-aware context graph for coding agents."
- "Our system combines deterministic code structure with temporal semantic memory and developer artifacts."
- "Our contribution is the ontology, ingestion bridge, retrieval/augmentation method, and evaluation."

## Minimum Deliverables To Be In Good Shape

At minimum, we should try to ship:

1. a real bridge between code indexing and Graphiti
2. first-class provenance entities for PRs/issues/commits/conversations
3. one custom retrieval/augmentation method for coding-agent tasks
4. one clean evaluation with baselines and ablations

If we deliver those four pieces, the project should be defensible as a real original system rather than just a wrapper around two open-source projects.
