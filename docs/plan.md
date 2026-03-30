# Overall Project Summary

## Vision

We are shifting the project from a **Graphiti-shaped conversational memory system** into a **provenance-aware context system for coding agents**.

Graphiti can still remain as a useful backend for extracting and searching conversation-derived entities and relations, but it should no longer define the product identity, top-level schema, MCP surface, or evaluation target.

Instead, the system should be organized around:
- projects
- repositories
- tasks
- repair sessions
- observations
- evidence
- claims
- code artifacts
- research artifacts

The high-level goal is not just to “store memory,” but to help an agent answer:

> **What do we know, why do we think it, what evidence supports it, what changed recently, and what should be done next?**

That is the core shift from generic graph memory to **temporal coding provenance**.

A second part of the vision is that the system should help agents **understand before they act**. Instead of relying only on raw code search or long conversational context, the system should precompute reviewable **research artifacts** that compress repo understanding into something both a human and an agent can inspect.

So the project becomes not just memory retrieval, but a system for:
- preserving understanding
- grounding decisions in evidence
- surfacing relevant past work
- improving planning and implementation for coding agents

---

## What Stays the Same

We are **not throwing away the current pipeline**.

The existing useful parts still remain:
- per-repo snapshot generation
- deterministic code-entity extraction
- temporal diffing and graph application
- episode ingestion for text-bearing sources

Graphiti also still has a role, but a narrower one:

> **Graphiti is a subsystem for conversation-memory extraction and retrieval, not the foundation of the whole architecture.**

---

## What Changes

The biggest change is that **Episode stops being the main conceptual unit**.

Instead, episodes become just one ingestion primitive inside a broader model:

- **Observation** = any raw event or source
- **EvidenceRecord** = the precise relevant piece extracted from an observation
- **Claim** = a normalized statement supported by evidence

On top of that, we make **Task** and **RepairSession** first-class nodes, so the graph has an explicit place to attach:
- user instructions
- tool runs
- tests
- patches
- outcomes
- decisions

This gives us a small but meaningful ontology for coding work:
- **Project / Repository**
- **Code entities**: files, symbols, tests, commits, PRs
- **Task / RepairSession**
- **Observation / Evidence / Claim**
- **ResearchArtifact**

This is enough structure to make the graph coding-specific without overengineering it.

---

## MCP and Product Surface

The MCP should become **provenance-centric** rather than memory-centric.

Instead of returning raw episodes or generic graph chunks, it should return structured outputs for:
- code context
- task context
- evidence / claim context
- past conversation constraints
- research artifacts

The public surface should start to look like:
- symbol provenance
- file timeline
- latest authoritative evidence
- conflicting claims
- patch justification
- cross-repo impact

This matters because it makes the system’s identity much clearer:

> The output is not “memory retrieval,” but **actionable, justified context for coding tasks**.

---

## Retrieval Strategy

The retrieval story becomes **multi-channel**.

### 1. Code Retrieval
Search over:
- files
- symbols
- tests
- diffs
- deterministic code artifacts

### 2. Provenance Retrieval
Search over:
- claims
- observations
- evidence
- patches
- outcomes

This is the **main new part** of the project.

### 3. Conversation Retrieval
Use Graphiti’s entity/edge search pipeline over user conversations.

### 4. Research-Artifact Retrieval
Retrieve high-level synthesized understanding of the repo or subsystem before making changes.

These channels are then merged and reranked into a final **structured context bundle**.

The final result should not be a bag of search hits, but a compact package containing:
- relevant code
- top claims
- supporting evidence
- contradictions
- prior attempts
- user constraints
- useful research notes

---

## Lightweight Reasoning Signals

Without adding heavy reasoning machinery, the system should still model four simple but valuable ideas.

### Authority
Some evidence is more trustworthy than others.

For example:
- explicit user clarification
- deterministic repo state
- recent test output

should rank above:
- old comments
- vague summaries
- LLM-inferred hypotheses

### Recency / Supersession
Newer evidence can override older evidence, and later claims can supersede earlier ones.

### Contradiction
Conflicting claims should be explicit in the graph rather than silently coexisting.

### Task Scope
Retrieval should prioritize:
- the current task
- related sessions
- nearby code/test artifacts

rather than searching the whole graph uniformly.

These signals improve usefulness a lot without requiring a complex reasoning engine.

---

## Research Artifacts

This is the main new addition on top of the provenance plan.

Whenever we ingest a repo, we also run a pipeline that generates **research artifacts**: bounded, typed, evidence-backed summaries of the repo that compress understanding into reviewable form.

The purpose is **not** to make one giant repo summary.

It is to build small, useful artifacts such as:
- subsystem summaries
- invariants / assumptions
- change-impact notes
- implementation-pattern notes
- prior migration templates

These artifacts should be:
- derived from bounded repo regions
- grounded by static signals plus LLM synthesis
- stored in the graph as first-class objects
- linked back to files, symbols, tests, and source evidence

This gives the system a way to **preserve and reuse understanding**, rather than rediscovering it from scratch every time.

---

## Role of the LLM

The LLM is still used, but in a narrower and more disciplined way.

It should **not** be asked to “understand the whole repo” in one shot.

Instead, it should operate over bounded contexts and produce typed outputs with explicit schemas.

The LLM’s role is to:
- synthesize structure into useful artifacts
- interpret grounded inputs
- produce bounded, typed research notes

The LLM should **not**:
- replace deterministic extraction
- become the source of truth
- invent unsupported architecture

Static analysis and deterministic code extraction provide the grounding.  
The LLM adds interpretation.

---

## Practical Implementation Vision

A good implementation story is:

1. keep the current repo snapshot and code graph pipeline
2. keep episode ingestion for text-bearing observations
3. add explicit project, task, and repair-session nodes
4. derive `EvidenceRecord` from episodes, tests, logs, and other observations
5. derive `Claim` from evidence
6. attach authority, recency, and contradiction at the evidence/claim layer
7. keep Graphiti behind a backend interface for conversational memory only
8. add a repo-ingestion research pipeline that generates structured research artifacts
9. redesign MCP around provenance and structured context retrieval
10. merge code, provenance, conversation, and research retrieval into one final context bundle

---

## Why This Is Not a Graphiti Wrapper

This project is no longer defined by Graphiti’s episode model or search pipeline.

Instead:
- **our schema** defines the graph
- **our MCP** defines the product surface
- **our retrieval** combines multiple channels
- **our research artifacts** add a new capability
- **Graphiti is only one subsystem**, mainly for conversation memory

That makes the project a genuine system of its own.

---

## One-Sentence Version

> **We are turning the project from a Graphiti-based episode memory wrapper into a provenance-aware, task-centered context system for coding agents, augmented with repo research artifacts that compress understanding into structured, retrievable context.**
