#!/usr/bin/env python3
"""
parse_judge_responses.py — Parse ChatGPT judge responses and write scored.jsonl.

Reads response_N.txt files (your saved ChatGPT replies), extracts HIT/MISS per
line position, maps positions to question IDs via index.json, and writes scored.jsonl.

After running this script, manually open scored.jsonl and fill in miss_class for
each MISS row: "abstained" (model said it didn't know) or "guessed_wrong" (model
gave a confident but incorrect answer).

Usage:
    # Baseline:
    python baseline_run/parse_judge_responses.py

    # MCP condition:
    python baseline_run/parse_judge_responses.py \\
        --batches  baseline_run/judge_batches_mcp \\
        --results  baseline_run/results_mcp.jsonl \\
        --scored   baseline_run/scored_mcp.jsonl

Input:
    <batches_dir>/index.json
    <batches_dir>/response_1.txt  ...  response_N.txt
    <results_file>

Output:
    <scored_file>
"""

import argparse
import json
import re
from pathlib import Path

# Matches lines like "1. HIT", "2. MISS", "10. HIT" (with optional surrounding text)
VERDICT_RE = re.compile(r"^\s*(\d+)[.)]\s*(HIT|MISS)\s*$", re.IGNORECASE)


def load_results_by_id(results_file):
    # Default: prefer token-enriched, fall back to plain
    if results_file:
        source = Path(results_file)
    else:
        token_file = Path("baseline_run/results_with_tokens.jsonl")
        source = token_file if token_file.exists() else Path("baseline_run/results.jsonl")
    rows = {}
    with open(source) as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows[r["id"]] = r
    return rows


def parse_response_file(path):
    """
    Parse a ChatGPT response file.
    Returns a dict: {position (1-indexed int): "HIT" or "MISS"}
    """
    verdicts = {}
    with open(path) as f:
        for line in f:
            m = VERDICT_RE.match(line)
            if m:
                pos     = int(m.group(1))
                verdict = m.group(2).upper()
                verdicts[pos] = verdict
    return verdicts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batches", default="baseline_run/judge_batches",
                        help="Directory containing index.json and response_N.txt files "
                             "(default: baseline_run/judge_batches)")
    parser.add_argument("--results", default=None,
                        help="Input results JSONL (default: results_with_tokens.jsonl or results.jsonl)")
    parser.add_argument("--scored", default="baseline_run/scored.jsonl",
                        help="Output scored JSONL (default: baseline_run/scored.jsonl)")
    args = parser.parse_args()

    batches_dir = Path(args.batches)
    scored_file = Path(args.scored)

    index_path = batches_dir / "index.json"
    if not index_path.exists():
        print(f"ERROR: {index_path} not found. Run judge_results.py first.")
        return

    with open(index_path) as f:
        index = json.load(f)  # {"1": [id1, id2, ...], "2": [...], ...}

    results_by_id = load_results_by_id(args.results)
    n_batches = len(index)

    all_verdicts = {}  # question_id → "HIT" or "MISS"
    warnings = []

    for batch_num_str, question_ids in index.items():
        batch_num     = int(batch_num_str)
        response_path = batches_dir / f"response_{batch_num}.txt"

        if not response_path.exists():
            print(f"  WARNING: {response_path} not found — skipping batch {batch_num}")
            warnings.append(f"Missing response file: response_{batch_num}.txt")
            continue

        verdicts = parse_response_file(response_path)
        n_expected = len(question_ids)
        n_parsed   = len(verdicts)

        print(f"  batch_{batch_num}: {n_parsed}/{n_expected} verdicts parsed from {response_path.name}")

        for pos, qid in enumerate(question_ids, 1):
            if pos in verdicts:
                all_verdicts[qid] = verdicts[pos]
            else:
                print(f"    WARNING: no verdict for position {pos} (ID: {qid})")
                warnings.append(f"batch_{batch_num} pos {pos} ({qid}): no verdict parsed")

    # Write scored.jsonl
    scored_rows = []
    for qid, result in results_by_id.items():
        verdict = all_verdicts.get(qid)
        row = {
            **result,
            "judge_verdict": verdict or "UNSCORED",
            "miss_class":    "" if verdict == "MISS" else None,
        }
        scored_rows.append(row)

    # Sort by set then raw_id for readability
    scored_rows.sort(key=lambda r: (r.get("set", ""), r.get("raw_id", 0)))

    with open(scored_file, "w") as f:
        for row in scored_rows:
            f.write(json.dumps(row) + "\n")

    # Summary
    hits     = sum(1 for r in scored_rows if r["judge_verdict"] == "HIT")
    misses   = sum(1 for r in scored_rows if r["judge_verdict"] == "MISS")
    unscored = sum(1 for r in scored_rows if r["judge_verdict"] == "UNSCORED")

    print(f"\nWrote {len(scored_rows)} rows to {scored_file}")
    print(f"  HIT:      {hits}")
    print(f"  MISS:     {misses}")
    print(f"  UNSCORED: {unscored}")

    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  • {w}")

    if misses > 0:
        print(f"\nNEXT STEP: open {scored_file} and fill in miss_class for each MISS row.")
        print('  "abstained"    — model said it didn\'t know / couldn\'t answer')
        print('  "guessed_wrong" — model gave a confident but incorrect answer')

    print("\nThen run:  python baseline_run/compute_scores.py")


if __name__ == "__main__":
    main()
