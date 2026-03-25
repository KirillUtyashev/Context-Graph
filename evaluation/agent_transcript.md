# Evaluation Handoff

This file summarizes the current state of the `evaluation/` scaffold so a teammate, or another coding agent, can understand it quickly.

## What Was Added

The repo now has an evaluation scaffold at:

- `evaluation/datasets/`
- `evaluation/runners/`
- `evaluation/metrics/`
- `evaluation/logs/`

Key files:

- [`evaluation/README.md`](/home/dkucz/Context-Graph/evaluation/README.md)
- [`evaluation/datasets/README.md`](/home/dkucz/Context-Graph/evaluation/datasets/README.md)
- [`evaluation/datasets/gold_eval_set.json`](/home/dkucz/Context-Graph/evaluation/datasets/gold_eval_set.json)
- [`evaluation/runners/eval_runner.py`](/home/dkucz/Context-Graph/evaluation/runners/eval_runner.py)
- [`evaluation/metrics/metrics.py`](/home/dkucz/Context-Graph/evaluation/metrics/metrics.py)

## Why The Current Evaluation Is Synthetic

The current evaluation output is intentionally synthetic / placeholder-based.

Reason:

- the graph-backed retrieval path is not complete enough yet for a real end-to-end evaluation
- the retrieval Lambda is still a placeholder
- the scaffold was explicitly built to avoid all external systems for now

That means the current scaffold does **not** call:

- Neptune
- Lambda
- MCP server
- external APIs
- any real LLM

## Current Runner Behavior

The runner in [`evaluation/runners/eval_runner.py`](/home/dkucz/Context-Graph/evaluation/runners/eval_runner.py) does four things:

1. loads `evaluation/datasets/gold_eval_set.json`
2. retrieves placeholder context
3. generates a placeholder answer
4. saves results to `evaluation/logs/results.json`

### Stub retrieval modes

- `none` -> `[]`
- `embedding` -> `["doc1", "doc2"]`
- `graph` -> `["doc1", "doc2", "doc3"]`

These are hardcoded stubs only.

### Answer generation

There is no model call.

The answer is currently just:

- `" ".join(context)` when context exists
- otherwise `"No context retrieved for: <question>"`

## Current Metrics

Metrics live in [`evaluation/metrics/metrics.py`](/home/dkucz/Context-Graph/evaluation/metrics/metrics.py).

Implemented metrics:

- `file_hit_rate(retrieved_docs, gold_docs)`
  Returns `1` if all gold docs are present in retrieved docs, else `0`.
- `fact_hit_rate(answer, gold_answer)`
  Returns `1` if `gold_answer` is a substring of `answer`, else `0`.
- `compute_metrics(results, dataset)`
  Computes dataset-level averages for both metrics.

These are intentionally lightweight placeholder metrics.

## Important Constraints Used So Far

When this scaffold was built, the constraints were:

- no real retrieval logic yet
- no Neptune / Lambda / MCP / external API calls
- keep the code modular so retrieval can be swapped in later
- include TODO comments for future embedding search and graph traversal integration

## Current Dataset

The local dataset at [`evaluation/datasets/gold_eval_set.json`](/home/dkucz/Context-Graph/evaluation/datasets/gold_eval_set.json) is a tiny synthetic dataset added so the runner can execute without errors.

Each entry currently has:

- `question`
- `gold_docs`
- `gold_answer`

This dataset is not a real benchmark yet.

## Verified Behavior

The scaffold has been run locally with:

```bash
python3 -m evaluation.runners.eval_runner
```

And it successfully:

- produced `evaluation/logs/results.json`
- returned aggregate metrics

## Documentation Split

The readmes are now split like this:

- [`evaluation/README.md`](/home/dkucz/Context-Graph/evaluation/README.md)
  Overall evaluation scaffold overview
- [`evaluation/datasets/README.md`](/home/dkucz/Context-Graph/evaluation/datasets/README.md)
  Dataset-specific notes

## What A Future Agent Should Probably Do Next

If asked to continue this work, likely next steps are:

1. replace stub retrieval in `eval_runner.py` with real retrieval hooks
2. define a richer dataset schema aligned with `benchmark.md`
3. improve metrics beyond substring matching
4. keep a clean separation between:
   - dataset loading
   - retrieval
   - answer generation
   - scoring

## What A Future Agent Should Not Assume

Do **not** assume:

- `results.json` reflects real system performance
- `graph` mode currently uses graph traversal
- `embedding` mode currently uses vector search
- any output is based on live graph data

Right now it is only a runnable scaffold.
