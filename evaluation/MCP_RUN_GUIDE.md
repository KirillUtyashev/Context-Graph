# MCP Condition Run Guide (Condition C)

This guide covers running the full evaluation pipeline for Condition C — agents instructed to use the context-graph MCP server — and comparing results against the Condition A baseline.

---

## Prerequisites

- `baseline_run/results_with_tokens.jsonl` must exist (Condition A baseline is complete)
- `CURSOR_API_KEY` environment variable set
- MCP server running at `http://15.156.133.77:8000/mcp/`
- `.cursor/` folder committed to the GitHub repo (contains `cg.sh`, `mcp.json`, `install.sh`, etc.)

---

## Step 1 — Run all 90 agents

```bash
export CURSOR_API_KEY=crsr_...
python baseline_run/run_mcp.py
```

- Runs all 90 questions sequentially (required for chronological token matching)
- Output: `baseline_run/results_mcp.jsonl`
- Resumable — re-running skips already-completed questions
- **Keep your Cursor dashboard clean during this run** — any other Cursor LLM activity will pollute token windows

---

## Step 2 — Export token usage CSV

In the Cursor dashboard: **Settings → Usage → Export**

Save as `baseline_run/usage_export_mcp.csv`

---

## Step 3 — Merge token counts

```bash
python baseline_run/merge_tokens.py baseline_run/usage_export_mcp.csv \
    --results baseline_run/results_mcp.jsonl \
    --output  baseline_run/results_with_tokens_mcp.jsonl
```

This uses time-window matching to attach token counts from the CSV to each agent run.

---

## Step 4 — Generate judge batches

```bash
python baseline_run/judge_results.py \
    --results  baseline_run/results_with_tokens_mcp.jsonl \
    --batches  baseline_run/judge_batches_mcp
```

Output: `baseline_run/judge_batches_mcp/batch_1.txt` ... `batch_N.txt`

---

## Step 5 — Judge responses (manual)

For each `batch_N.txt`:
1. Open a new ChatGPT conversation (thinking model recommended)
2. Paste the full contents of `batch_N.txt`
3. Save ChatGPT's response as `baseline_run/judge_batches_mcp/response_N.txt`

---

## Step 6 — Parse judge responses

```bash
python baseline_run/parse_judge_responses.py \
    --batches  baseline_run/judge_batches_mcp \
    --results  baseline_run/results_with_tokens_mcp.jsonl \
    --scored   baseline_run/scored_mcp.jsonl
```

Output: `baseline_run/scored_mcp.jsonl`

---

## Step 7 — Fill in miss_class (manual)

Open `baseline_run/scored_mcp.jsonl` and fill in `miss_class` for each MISS row:
- `"abstained"` — model said it didn't know
- `"guessed_wrong"` — model gave a confident but wrong answer

---

## Step 8 — Compute MCP scores

```bash
python baseline_run/compute_scores.py --scored baseline_run/scored_mcp.jsonl
```

Save this output — it's your Condition C fact hit rate result.

---

## Step 9 — Token delta comparison

```bash
python baseline_run/compare_tokens.py \
    --baseline baseline_run/results_with_tokens.jsonl \
    --mcp      baseline_run/results_with_tokens_mcp.jsonl
```

Prints:
- Per-question table: baseline tokens, MCP tokens, delta, % saved
- Aggregate savings by question type
- Aggregate savings by dataset (main_v2 vs bug_pr)
- Aggregate savings by hop count
- Markdown table ready to paste into your report

---

## File state after full pipeline

```
baseline_run/
├── results_mcp.jsonl                  ← raw MCP agent answers
├── results_with_tokens_mcp.jsonl      ← MCP answers + token counts
├── scored_mcp.jsonl                   ← MCP judge verdicts
├── judge_batches_mcp/
│   ├── batch_1.txt ... batch_N.txt    ← judge prompts
│   └── response_1.txt ... response_N.txt  ← ChatGPT responses
└── [baseline files untouched]
    ├── results.jsonl
    ├── results_with_tokens.jsonl
    └── scored.jsonl
```
