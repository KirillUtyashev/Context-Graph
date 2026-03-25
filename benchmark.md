# Benchmark Ideas: Temporal Provenance for Coding Agents

## 1. Summary

We want a benchmark that tests provenance for coding agents, not just grounded dialogue or static repository retrieval.

The key idea is to make tasks unsolvable from the repository alone. Some of the facts required for task completion should live in the conversation with the user, and some of those facts should change over time. This turns the benchmark into a test of whether an agent can:

* retrieve the right evidence,
* remember and cite the right evidence,
* update its beliefs when newer evidence arrives,
* avoid relying on stale or contradicted evidence,
* produce patches that are both correct and well-supported.

---

## 2. Core Hypothesis

A coding agent with strong provenance support should outperform agents without provenance tools on tasks where the source of truth is distributed across:

* repository state,
* user conversation,
* test output and logs,
* runtime observations,
* later clarifications or contradictions,
* external repository changes.

This benchmark should measure not only whether the agent solves the task, but whether it can explain and justify the solution using valid, current evidence.

---

## 3. Benchmark Design

### 3.1 Benchmark Structure

Each benchmark instance should be a timeline rather than a single prompt.

#### Task timeline

1. Initial repository state
2. Initial issue or task description
3. One or more user messages containing hidden facts required for solving the task
4. Optional intermediate evidence such as test failures, logs, or tool outputs
5. Optional later user messages that refine, contradict, or supersede earlier messages
6. Optional external repository changes such as a new commit or modified file
7. Final patch submission plus provenance output

This structure tests whether the agent can combine code evidence with conversational and temporal evidence.

---

### 3.2 Why Hidden Facts Matter

Real coding tasks are often not solvable from the repository alone. Important facts are frequently only available through interaction. Good hidden facts for this benchmark include:

* intended behavior that is not documented in the code,
* constraints like "do not change this API" or "must preserve backward compatibility",
* environment-specific bug conditions mentioned only in chat,
* production log snippets shared by the user,
* references to a specific regression commit,
* clarification that one seemingly relevant file must not be modified,
* business logic that overrides stale comments or misleading tests.

The hidden facts should be necessary, not just helpful.

---

### 3.3 Temporal Variants

The benchmark should include tasks where evidence changes over time. Useful patterns:

#### Clarification

The initial issue is incomplete. A later user message provides the missing constraint.

#### Correction

An earlier user message says one thing, and a later message explicitly corrects it.

#### Supersession

Older logs or test outputs become stale after a later run.

#### External change

A new commit lands during the session and changes the relevant code or invalidates the current plan.

#### Intent shift

The user changes the desired behavior after the agent has already started implementing a fix.

These variants let us measure freshness, not just retrieval quality.

---

### 3.4 Task Families

We should design tasks that require combining multiple evidence sources:

* Cross-file bug: the symptom appears in one file, but the fix lives elsewhere.
* Test-to-code bug: the failing test output is the crucial provenance signal.
* Conversation-to-code bug: a key requirement is only mentioned in chat.
* Doc-vs-code conflict: comments or docs are stale, but newer user clarification is authoritative.
* Multi-hop bug: the correct fix requires chaining issue text, chat clarification, test output, and repository code.
* Temporal bug: the correct answer depends on updating to the latest message or repo state.
* Distractor-heavy repo: many files look relevant, but only a few truly support the fix.

---



## 4. Systems to Compare

At minimum, evaluate:

* Codex alone
* Codex + Graphiti
* Codex + our provenance tool

If possible, also add:

* Codex + repository retrieval only
* Codex + conversation memory only
* Codex + Graphiti + our provenance tool

This helps separate memory improvements from provenance improvements.

---

## 5. Required Agent Output

Every system should emit the same structured provenance format so we can compare them fairly.

Example:

```json
{
  "task_id": "task_001",
  "final_patch": "...",
  "claims": [
    {
      "claim": "The bug is caused by the handler ignoring the tenant override from the request context.",
      "citations": [
        {
          "type": "file",
          "path": "app/handler.py",
          "start": 42,
          "end": 61
        },
        {
          "type": "conversation_message",
          "message_id": "m3"
        }
      ]
    }
  ],
  "hunk_justifications": [
    {
      "hunk_id": "h1",
      "why": "Reads the tenant override from the request context before building the cache key.",
      "citations": [
        {
          "type": "file",
          "path": "app/cache.py",
          "start": 10,
          "end": 24
        },
        {
          "type": "conversation_message",
          "message_id": "m3"
        },
        {
          "type": "test_output",
          "event_id": "pytest_run_2"
        }
      ]
    }
  ]
}
```

The important part is that each major claim and each patch hunk is tied to explicit supporting evidence.

---

## 6. Gold Annotation

### 6.1 Gold Annotation Per Task

For each task, annotate:

* the minimal required evidence set,
* which source is authoritative when sources conflict,
* which earlier evidence becomes stale after later events,
* the final intended behavior,
* the expected patch or hidden test outcome,
* the accepted citation spans or IDs.

Example:

* required evidence: `conversation m3` and `src/router.ts:88-116`
* stale evidence: `conversation m1`
* authoritative source: latest user clarification overrides stale code comments
* final correctness check: hidden tests pass

---

## 7. Evaluation

Evaluation should combine deterministic checks with LLM judging.

### 7.1 Task Success

Use automated checks for:

* hidden tests,
* build success,
* lint or static checks when needed,
* patch application validity,
* citation span resolution.

---

### 7.2 Provenance Quality

Use an LLM judge to evaluate whether the cited evidence actually supports the claim or code change.

Important provenance dimensions:

* Validity: does the cited evidence support the claim?
* Sufficiency: is the evidence enough, or is the agent making unsupported leaps?
* Specificity: are the citations precise, not vague or oversized?
* Faithfulness: does the explanation rely on uncited information?
* Freshness: does it use the latest authoritative evidence instead of stale evidence?

---

### 7.3 Coverage and Minimality

Additional quantitative signals:

* Evidence recall: did the agent include the necessary gold evidence?
* Evidence precision: how much cited evidence is actually relevant?
* Hunk support rate: what fraction of diff hunks have valid support?
* Source coverage: did the agent use the right mix of repo, conversation, and temporal evidence?
* Minimality: did the agent cite just enough evidence rather than dumping irrelevant context?

---

### 7.4 Joint Score

Report:

* overall task success,
* provenance quality on successful runs only,
* a joint score that rewards both correctness and valid provenance.

This prevents a system from appearing strong just because it produces careful-looking citations while still failing the task.

---

## 8. Retrieval Evaluation (System-Level)

In addition to end-to-end agent evaluation, we explicitly evaluate the underlying retrieval system.

This is necessary because the Context Graph system introduces a hybrid retrieval architecture combining:

* graph-native retrieval (nodes, edges, temporal structure),
* profile-based high-level retrieval (LightRAG-style),
* merged multi-channel context construction.

---

### 8.1 Retrieval Objectives

We evaluate whether the system:

* retrieves the correct evidence required to solve the task,
* prioritizes relevant and current information,
* avoids stale or contradicted evidence,
* improves over graph-only and agent-only baselines.

---

### 8.2 Retrieval Baselines

We compare:

1. **Graph-only retrieval (Graphiti-style)**

   * node, edge, and temporal traversal
   * no profile-based high-level retrieval

2. **Agent-only retrieval**

   * direct LLM interaction with repository
   * no structured retrieval system

3. **Hybrid retrieval (ours)**

   * graph-native retrieval + profile retrieval
   * global result merging

---

### 8.3 Retrieval Metrics

#### Context Recall@k

* Whether the retrieved context includes all required gold evidence

#### Evidence Coverage

* Fraction of required evidence sources retrieved:

  * repository
  * conversation
  * temporal events

#### Freshness

* Whether the retrieved context reflects the latest authoritative evidence
* Penalizes reliance on stale or superseded information

#### Context Precision

* Fraction of retrieved context that is actually relevant

#### Context Chain Correctness

* Whether the retrieved evidence forms a valid reasoning chain for the task

---

### 8.4 Retrieval Procedure

For each task:

1. Run retrieval independently of generation:

   * graph-only
   * hybrid system

2. Capture retrieved context:

   * nodes
   * edges
   * profiles
   * conversation references

3. Compare retrieved context against gold annotation:

   * required evidence set
   * stale vs valid evidence
   * authoritative sources

4. Compute retrieval metrics before running the agent.

---

### 8.5 Relationship to Agent Evaluation

Retrieval evaluation and agent evaluation are complementary:

* Retrieval evaluation measures:

  * whether the system *finds* the right evidence

* Agent evaluation measures:

  * whether the system *uses* the evidence correctly

A system may fail due to:

* missing evidence (retrieval failure),
* incorrect reasoning (agent failure),
* or both.

---

## 9. LLM-as-Judge Plan

LLM-as-judge is useful here, but it should not be the only evaluation signal.

### 9.1 Judge Inputs

For each claim or patch hunk, the judge should see:

* task description,
* current authoritative timeline state,
* the claim or hunk explanation,
* the cited evidence bundle,
* the patch hunk if relevant.

By default, the judge should not be shown full system trajectories unless that is part of a separate analysis.

---

### 9.2 Judge Outputs

The judge can score each item on a 0-2 or 0-3 rubric for:

* validity,
* sufficiency,
* specificity,
* faithfulness,
* freshness.

The judge should also provide a short rationale and a binary answer for whether the evidence is acceptable.

---

### 9.3 Judge Guardrails

* Blind the judge to system identity.
* Prefer a judge model different from the evaluated agent.
* Use multiple judge samples or multiple judge models and aggregate.
* Calibrate the judge on a human-labeled subset.

---

## 10. Human Calibration

We should label a small subset of tasks by hand to validate the LLM judge.

A practical starting point:

* 50-100 manually reviewed examples,
* balanced across static, contradictory, and external-change tasks,
* annotate both valid and invalid provenance cases.

This gives us a way to measure judge agreement and catch drift.

---

## 11. Dataset Construction Strategy

This should be a hybrid benchmark, not purely synthetic.

### 11.1 Real-task component

Use existing repository-level coding tasks as a base, especially:

* SWE-bench-Lite style issue-resolution tasks,
* RepoEval-style repository completion tasks,
* CodeRAG-Bench repository-level tasks.

Then add a provenance overlay with conversation turns, changing evidence, and structured annotation.

---

### 11.2 Controlled component

Create synthetic or bug-injected repositories where we control:

* the true evidence chain,
* the distractors,
* the contradiction timing,
* the external changes.

This makes evaluation easier and lets us stress specific failure modes.

---

## 12. MVP Proposal

Start with 30-50 tasks across three slices:

* Static hidden-fact tasks
* Contradictory-message tasks
* External-change tasks

For each task, provide:

* repo snapshot,
* issue text,
* conversation timeline,
* optional logs or test outputs,
* optional external git event,
* hidden tests,
* gold provenance annotation,
* required output schema.

---

## 13. Main Research Questions

Possible headline questions:

* Does provenance tooling improve coding task success when key facts are only available through conversation?
* Does provenance tooling help agents recover from stale or contradicted evidence?
* Does provenance tooling improve the validity and freshness of citations compared with memory-only baselines?
* Does better provenance correlate with better final patch quality?

---

## 14. Design Principle

The benchmark should not collapse into a memory test where success depends on remembering a single sentence from chat.

Each task should ideally require combining:

* repository evidence,
* conversational evidence,
* temporal evidence.

That combination is what makes the benchmark a realistic test of coding-agent provenance rather than a static repo QA task or a simple chat-memory task.
