#!/usr/bin/env python3
"""
judge_results.py — Generate batched ChatGPT judge prompts from results.jsonl.

Splits all questions into batches of BATCH_SIZE, writes one paste-ready .txt file
per batch, and writes an index.json mapping batch number → list of question IDs.

Usage:
    # Baseline:
    python baseline_run/judge_results.py

    # MCP condition:
    python baseline_run/judge_results.py \\
        --results  baseline_run/results_mcp.jsonl \\
        --batches  baseline_run/judge_batches_mcp

Input:
    <results_file>  (default: results_with_tokens.jsonl, fallback: results.jsonl)

Output:
    <batches_dir>/batch_1.txt  ...  batch_N.txt
    <batches_dir>/index.json
"""

import argparse
import json
from pathlib import Path

BATCH_SIZE = 15

BATCH_HEADER = """\
You are evaluating whether model answers contain required facts.
For each numbered item below, reply with "HIT" or "MISS" on its own line.
If it is ambiguous, still reply with "HIT" or "MISS" (depending on where you lean) but add a brief explanation of why.
Do not mark a response as "MISS" if it a model failed to cite PR numbers or documentation files, but got the right answer anyway.
Format your ENTIRE reply as a simple numbered list — nothing else:

1. HIT
2. MISS
3. HIT
...

"""


def load_results(results_arg):
    # If explicitly provided use it; otherwise prefer token-enriched, fall back to plain
    if results_arg:
        source = Path(results_arg)
    else:
        token_file = Path("baseline_run/results_with_tokens.jsonl")
        source = token_file if token_file.exists() else Path("baseline_run/results.jsonl")
    rows = []
    with open(source) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    # Only include rows that have a model answer (skip failed runs)
    valid = [r for r in rows if r.get("model_answer")]
    skipped = len(rows) - len(valid)
    if skipped:
        print(f"  Skipping {skipped} rows with no model_answer (failed agent runs).")
    print(f"  Loaded {len(valid)} answerable questions from {source}")
    return valid


def build_batch_prompt(batch):
    """Build the paste-ready prompt string for a batch of questions."""
    lines = [BATCH_HEADER]
    for pos, row in enumerate(batch, 1):
        lines.append(f"{pos}. Required fact: {row['expected_answer']}")
        lines.append(f"   Model answer: {row['model_answer']}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default=None,
                        help="Input results JSONL (default: results_with_tokens.jsonl or results.jsonl)")
    parser.add_argument("--batches", default="baseline_run/judge_batches",
                        help="Output directory for batch files (default: baseline_run/judge_batches)")
    parser.add_argument("--ids", nargs="+", default=None,
                        metavar="QID",
                        help="Only generate batches for these question IDs (for backfill)")
    args = parser.parse_args()

    batches_dir = Path(args.batches)
    batches_dir.mkdir(parents=True, exist_ok=True)

    results = load_results(args.results)

    if args.ids:
        id_set  = set(args.ids)
        results = [r for r in results if r["id"] in id_set]
        missing = id_set - {r["id"] for r in results}
        if missing:
            print(f"  WARNING: these IDs not found in results: {sorted(missing)}")
        print(f"  Filtering to {len(results)} question(s): {[r['id'] for r in results]}")

    total = len(results)

    # Split into batches
    batches = [results[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    n_batches = len(batches)

    print(f"  Splitting {total} questions into {n_batches} batches of ≤{BATCH_SIZE}\n")

    index = {}  # batch_number (str) → [question IDs in order]

    for batch_num, batch in enumerate(batches, 1):
        batch_path = batches_dir / f"batch_{batch_num}.txt"

        prompt = build_batch_prompt(batch)
        with open(batch_path, "w") as f:
            f.write(prompt)

        index[str(batch_num)] = [row["id"] for row in batch]

        print(f"  batch_{batch_num}.txt — {len(batch)} questions")
        for row in batch:
            print(f"    • {row['id']}: {row['question'][:65]}")
        print()

    # Write index
    index_path = batches_dir / "index.json"
    with open(index_path, "w") as f:
        json.dump(index, f, indent=2)

    print("=" * 70)
    print(f"Wrote {n_batches} batch files to {batches_dir}/")
    print()
    print("NEXT STEPS:")
    print(f"  For each batch_N.txt (1 through {n_batches}):")
    print("  1. Open a NEW ChatGPT conversation (thinking model recommended)")
    print("  2. Paste the ENTIRE contents of batch_N.txt")
    print("  3. Copy ChatGPT's response")
    print(f"  4. Save it as {batches_dir}/response_N.txt")
    print()
    print(f"  Then run:  python baseline_run/parse_judge_responses.py --batches {batches_dir}")


if __name__ == "__main__":
    main()
