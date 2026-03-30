#!/usr/bin/env python3
"""
compute_scores.py — Compute and print evaluation scores from scored.jsonl.

Prints fact hit rate broken down by question type and set, unsupported guess rate,
and token consumption statistics (if token data is present).

Usage:
    # Baseline:
    python baseline_run/compute_scores.py

    # MCP condition:
    python baseline_run/compute_scores.py --scored baseline_run/scored_mcp.jsonl

Input:
    <scored_file>  (default: baseline_run/scored.jsonl)

Note: miss_class must be filled in manually for unsupported_guess_rate to be accurate.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load_scored(scored_file):
    rows = []
    with open(scored_file) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def pct(num, denom):
    if denom == 0:
        return "  —  "
    return f"{100 * num / denom:5.1f}%"


def print_table(title, rows, col_headers, data_fn):
    print(f"\n### {title}")
    col_w = max(len(h) for h in col_headers)
    header = "  ".join(h.ljust(col_w) for h in col_headers)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(v).ljust(col_w) for v in data_fn(row)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scored", default="baseline_run/scored.jsonl",
                        help="Input scored JSONL (default: baseline_run/scored.jsonl)")
    args = parser.parse_args()

    scored_file = Path(args.scored)
    if not scored_file.exists():
        print(f"ERROR: {scored_file} not found. Run parse_judge_responses.py first.")
        return

    rows = load_scored(scored_file)
    print(f"Loaded {len(rows)} scored rows\n")

    # Filter to rows with a real verdict
    scored = [r for r in rows if r.get("judge_verdict") in ("HIT", "MISS")]
    unscored_count = len(rows) - len(scored)
    if unscored_count:
        print(f"Note: {unscored_count} rows are UNSCORED (missing response files) — excluded from calculations.\n")

    total = len(scored)
    hits  = sum(1 for r in scored if r["judge_verdict"] == "HIT")
    misses = total - hits

    print("=" * 60)
    print("BASELINE EVALUATION RESULTS (Condition A — agentic, no MCP)")
    print("=" * 60)
    print(f"\nOverall fact hit rate: {hits}/{total} = {pct(hits, total)}")

    # ── Breakdown by question type ──────────────────────────────────────────
    type_order = [
        "information_extraction",
        "multi_hop_retrieval",
        "knowledge_update",
        "temporal_reasoning",
        "debugging_and_provenance",
        "unknown",
    ]
    by_type = defaultdict(list)
    for r in scored:
        by_type[r.get("type", "unknown")].append(r)

    print("\n### Fact Hit Rate by Question Type")
    print(f"{'Type':<30} {'Hit':>5}  {'Total':>6}  {'Hit Rate':>8}")
    print("-" * 56)
    for qtype in type_order:
        if qtype not in by_type:
            continue
        group = by_type[qtype]
        h = sum(1 for r in group if r["judge_verdict"] == "HIT")
        print(f"{qtype:<30} {h:>5}  {len(group):>6}  {pct(h, len(group)):>8}")
    # Catch any types not in order list
    for qtype, group in by_type.items():
        if qtype not in type_order:
            h = sum(1 for r in group if r["judge_verdict"] == "HIT")
            print(f"{qtype:<30} {h:>5}  {len(group):>6}  {pct(h, len(group)):>8}")

    # ── Breakdown by dataset ────────────────────────────────────────────────
    by_set = defaultdict(list)
    for r in scored:
        by_set[r.get("set", "unknown")].append(r)

    print("\n### Fact Hit Rate by Dataset")
    print(f"{'Dataset':<20} {'Hit':>5}  {'Total':>6}  {'Hit Rate':>8}")
    print("-" * 44)
    for ds, group in sorted(by_set.items()):
        h = sum(1 for r in group if r["judge_verdict"] == "HIT")
        print(f"{ds:<20} {h:>5}  {len(group):>6}  {pct(h, len(group)):>8}")

    # ── Breakdown by hop count ──────────────────────────────────────────────
    by_hops = defaultdict(list)
    for r in scored:
        by_hops[r.get("hops", 0)].append(r)

    print("\n### Fact Hit Rate by Hop Count")
    print(f"{'Hops':<8} {'Hit':>5}  {'Total':>6}  {'Hit Rate':>8}")
    print("-" * 34)
    for hops in sorted(by_hops.keys()):
        group = by_hops[hops]
        h = sum(1 for r in group if r["judge_verdict"] == "HIT")
        print(f"{hops:<8} {h:>5}  {len(group):>6}  {pct(h, len(group)):>8}")

    # ── Unsupported guess rate ──────────────────────────────────────────────
    miss_rows = [r for r in scored if r["judge_verdict"] == "MISS"]
    classified = [r for r in miss_rows if r.get("miss_class") in ("abstained", "guessed_wrong")]
    guessed_wrong = sum(1 for r in classified if r["miss_class"] == "guessed_wrong")
    abstained     = sum(1 for r in classified if r["miss_class"] == "abstained")
    unclassified  = len(miss_rows) - len(classified)

    print(f"\n### Unsupported Guess Rate (MISSes only)")
    print(f"  Total MISSes:      {len(miss_rows)}")
    print(f"  Classified:        {len(classified)}")
    print(f"    guessed_wrong:   {guessed_wrong}")
    print(f"    abstained:       {abstained}")
    if unclassified:
        print(f"  Unclassified:     {unclassified}  ← fill in miss_class in scored.jsonl")
    if classified:
        rate = guessed_wrong / len(classified)
        print(f"  Unsupported guess rate: {guessed_wrong}/{len(classified)} = {100*rate:.1f}%")

    # ── Token statistics (if available) ────────────────────────────────────
    token_rows = [r for r in scored if r.get("tokens") and r["tokens"].get("total", 0) > 0]
    if token_rows:
        totals      = [r["tokens"]["total"]          for r in token_rows]
        inputs      = [r["tokens"]["input_no_cache"] for r in token_rows]
        outputs     = [r["tokens"]["output"]         for r in token_rows]
        cache_reads = [r["tokens"]["cache_read"]     for r in token_rows]

        def avg(lst): return sum(lst) / len(lst) if lst else 0

        print(f"\n### Token Consumption (n={len(token_rows)} rows with data)")
        print(f"  Avg total tokens:       {avg(totals):,.0f}")
        print(f"  Avg input (no cache):   {avg(inputs):,.0f}")
        print(f"  Avg cache read:         {avg(cache_reads):,.0f}")
        print(f"  Avg output tokens:      {avg(outputs):,.0f}")
        print(f"  Min total:              {min(totals):,}")
        print(f"  Max total:              {max(totals):,}")

        # Token breakdown by type
        print(f"\n  Avg total tokens by question type:")
        for qtype in type_order:
            group = [r for r in token_rows if r.get("type") == qtype]
            if group:
                t = [r["tokens"]["total"] for r in group]
                print(f"    {qtype:<30} {avg(t):>8,.0f}")
    else:
        print("\n### Token Consumption")
        print("  No token data found. Run merge_tokens.py after exporting the dashboard CSV.")

    print("\n" + "=" * 60)
    print("Markdown table for reports:")
    print("=" * 60)
    print()
    print("| Metric | Value |")
    print("|--------|-------|")
    print(f"| Overall fact hit rate | {pct(hits, total)} ({hits}/{total}) |")
    for qtype in type_order:
        if qtype not in by_type:
            continue
        group = by_type[qtype]
        h = sum(1 for r in group if r["judge_verdict"] == "HIT")
        label = qtype.replace("_", " ").title()
        print(f"| Hit rate — {label} | {pct(h, len(group))} ({h}/{len(group)}) |")
    if classified:
        print(f"| Unsupported guess rate | {100*guessed_wrong/len(classified):.1f}% |")
    if token_rows:
        print(f"| Avg tokens per question | {avg(totals):,.0f} |")


if __name__ == "__main__":
    main()
