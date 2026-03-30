# Evaluation Datasets

This folder holds local datasets for the evaluation scaffold.

## Current Dataset

The runner currently loads:

- `evaluation/datasets/gold_eval_set.json`

Each dataset entry is a JSON object with:

- `question`: the evaluation prompt
- `gold_docs`: the expected supporting documents
- `gold_answer`: a simple reference answer string

Example:

```json
{
  "question": "What documents mention the authentication flow?",
  "gold_docs": ["doc1", "doc2"],
  "gold_answer": "doc1 doc2"
}
```

## Current Behavior

The evaluation pipeline is intentionally stubbed for now.

- No Neptune calls
- No Lambda calls
- No MCP server calls
- No external API calls
- No real LLM generation

The runner uses placeholder retrieval modes defined in
[`evaluation/runners/eval_runner.py`](../runners/eval_runner.py):

- `none` -> `[]`
- `embedding` -> `["doc1", "doc2"]`
- `graph` -> `["doc1", "doc2", "doc3"]`

The answer generator simply joins the retrieved docs into a string.

## Running The Scaffold

From the repo root:

```bash
python3 -m evaluation.runners.eval_runner
```

This will:

1. Load `gold_eval_set.json`
2. Run the stub retrieval pipeline
3. Generate placeholder answers
4. Save outputs to `evaluation/logs/results.json`
5. Compute simple aggregate metrics

## Metrics

The current metrics live in
[`evaluation/metrics/metrics.py`](../metrics/metrics.py):

- `file_hit_rate`: `1` if all gold docs are retrieved, else `0`
- `fact_hit_rate`: `1` if `gold_answer` is a substring of the generated answer, else `0`

These are intentionally simple placeholders so the scaffold can run end-to-end now.

## Future Work

- Replace stubbed embedding retrieval with the real embedding search path
- Replace stubbed graph retrieval with actual graph traversal and schema-aware lookup
- Expand the dataset schema with provenance annotations, citations, and task metadata
- Add richer answer grading and retrieval metrics
