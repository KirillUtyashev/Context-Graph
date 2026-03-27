# Evaluation

This directory contains the current evaluation scaffold for the Context Graph project.

The goal of this scaffold is to let us run a fully local, end-to-end evaluation loop
before the real retrieval stack is wired up.

## Current Scope

The current implementation is intentionally placeholder-only:

- no Neptune calls
- no Lambda calls
- no MCP server calls
- no external API calls
- no real LLM answer generation

This gives us a stable place to define:

- dataset shape
- runner flow
- output format
- metric interfaces

## Directory Layout

- `evaluation/datasets/`
  Stores gold evaluation data and dataset documentation.
- `evaluation/runners/`
  Stores evaluation execution code.
- `evaluation/metrics/`
  Stores metric computation utilities.
- `evaluation/logs/`
  Stores generated outputs such as `results.json`.

## Main Files

- [`evaluation/runners/eval_runner.py`](/home/dkucz/Context-Graph/evaluation/runners/eval_runner.py)
  Loads the dataset, runs stub retrieval, generates placeholder answers, saves results, and computes metrics.
- [`evaluation/metrics/metrics.py`](/home/dkucz/Context-Graph/evaluation/metrics/metrics.py)
  Defines simple placeholder metrics for retrieval and answer checking.
- [`evaluation/datasets/gold_eval_set.json`](/home/dkucz/Context-Graph/evaluation/datasets/gold_eval_set.json)
  Small local gold dataset used by the scaffold.
- [`evaluation/datasets/README.md`](/home/dkucz/Context-Graph/evaluation/datasets/README.md)
  Dataset-specific notes and schema details.

## Stub Retrieval Modes

The runner currently supports three retrieval modes:

- `none` -> returns `[]`
- `embedding` -> returns `["doc1", "doc2"]`
- `graph` -> returns `["doc1", "doc2", "doc3"]`

These are placeholders only. They exist so the evaluation flow can be tested now and replaced later with real retrieval components.

## Running It

From the repo root:

```bash
python3 -m evaluation.runners.eval_runner
```

This will:

1. Load the gold dataset
2. Retrieve placeholder context
3. Generate a simple answer from the retrieved docs
4. Save outputs to `evaluation/logs/results.json`
5. Compute aggregate metrics

## Current Output Shape

Each result record looks like:

```json
{
  "question": "What documents mention the authentication flow?",
  "retrieved_docs": ["doc1", "doc2"],
  "answer": "doc1 doc2"
}
```

## Current Metrics

The scaffold currently computes:

- `file_hit_rate`
  Returns `1` when all gold docs are retrieved, else `0`.
- `fact_hit_rate`
  Returns `1` when the gold answer is a substring of the generated answer, else `0`.

These metrics are intentionally simple and should be treated as interface placeholders, not final evaluation methodology.

## Future Integration

Planned next steps for this scaffold:

- replace stubbed embedding retrieval with the repo's real embedding search path
- replace stubbed graph retrieval with actual graph traversal and schema-aware lookup
- add richer retrieval metrics and answer-quality metrics
- expand dataset examples to include provenance, temporal updates, and multi-step evidence
