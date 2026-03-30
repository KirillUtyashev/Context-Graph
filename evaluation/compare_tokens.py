#!/usr/bin/env python3
"""
compare_tokens.py — Per-question token delta: baseline vs MCP condition.

Joins the two results_with_tokens files on question ID and prints:
  - Per-question table: baseline tokens, MCP tokens, delta, % saved
  - Aggregate savings by question type
  - Aggregate savings by dataset (main_v2 vs bug_pr)
  - Aggregate savings by hop count
  - Overall summary

Usage:
    python baseline_run/compare_tokens.py

    # Or with explicit file paths:
    python baseline_run/compare_tokens.py \\
        --baseline baseline_run/results_with_tokens.jsonl \\
        --mcp      baseline_run/results_with_tokens_mcp.jsonl

Input:
    baseline_run/results_with_tokens.jsonl        (Condition A)
    baseline_run/results_with_tokens_mcp.jsonl    (Condition C)

Output:
    Printed tables + markdown summary
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path


DEFAULT_BASELINE = Path("baseline_run/results_with_tokens.jsonl")
DEFAULT_MCP      = Path("baseline_run/results_with_tokens_mcp.jsonl")


def load(path):
    rows = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows[r["id"]] = r
    return rows


def tokens(row):
    t = row.get("tokens")
    if t and t.get("total", 0) > 0:
        return t["total"]
    return None


def pct_saved(base, mcp):
    if base and mcp and base > 0:
        return 100 * (base - mcp) / base
    return None


def fmt(n):
    if n is None:
        return "     —"
    return f"{n:>6,}"


def fmt_pct(p):
    if p is None:
        return "    —  "
    sign = "+" if p < 0 else ""  # negative = MCP used MORE
    return f"{sign}{p:+6.1f}%"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default=DEFAULT_BASELINE, type=Path)
    parser.add_argument("--mcp",      default=DEFAULT_MCP,      type=Path)
    args = parser.parse_args()

    if not args.baseline.exists():
        print(f"ERROR: baseline file not found: {args.baseline}")
        print("  Run merge_tokens.py after the baseline run first.")
        return
    if not args.mcp.exists():
        print(f"ERROR: MCP file not found: {args.mcp}")
        print("  Run: cp baseline_run/results_mcp.jsonl baseline_run/results.jsonl")
        print("  Then: python baseline_run/merge_tokens.py <mcp_usage.csv>")
        print("  Then: cp baseline_run/results_with_tokens.jsonl baseline_run/results_with_tokens_mcp.jsonl")
        return

    baseline = load(args.baseline)
    mcp      = load(args.mcp)

    common_ids = sorted(set(baseline) & set(mcp),
                        key=lambda i: (baseline[i].get("set",""), baseline[i].get("raw_id", 0)))
    only_baseline = set(baseline) - set(mcp)
    only_mcp      = set(mcp) - set(baseline)

    if only_baseline:
        print(f"Note: {len(only_baseline)} IDs only in baseline (no MCP result): {sorted(only_baseline)}")
    if only_mcp:
        print(f"Note: {len(only_mcp)} IDs only in MCP (no baseline result): {sorted(only_mcp)}")

    print(f"\nComparing {len(common_ids)} questions present in both files.\n")

    # ── Per-question table ──────────────────────────────────────────────────
    print("### Per-Question Token Comparison")
    print(f"{'ID':<22} {'Type':<28} {'Hops':>4}  {'Baseline':>9}  {'MCP':>9}  {'Delta':>9}  {'Saved':>8}")
    print("-" * 100)

    rows_with_data = []
    for qid in common_ids:
        b = baseline[qid]
        m = mcp[qid]
        bt = tokens(b)
        mt = tokens(m)
        delta = (bt - mt) if (bt and mt) else None
        pct   = pct_saved(bt, mt)
        qtype = b.get("type", "unknown")[:27]
        hops  = b.get("hops", 0)
        print(f"{qid:<22} {qtype:<28} {hops:>4}  {fmt(bt)}  {fmt(mt)}  {fmt(delta)}  {fmt_pct(pct)}")
        if bt and mt:
            rows_with_data.append({
                "id":    qid,
                "set":   b.get("set", "unknown"),
                "type":  b.get("type", "unknown"),
                "hops":  hops,
                "base":  bt,
                "mcp":   mt,
                "delta": delta,
                "pct":   pct,
            })

    n = len(rows_with_data)
    if n == 0:
        print("\nNo rows with token data in both files — nothing to aggregate.")
        return

    # ── Overall summary ─────────────────────────────────────────────────────
    total_base = sum(r["base"] for r in rows_with_data)
    total_mcp  = sum(r["mcp"]  for r in rows_with_data)
    total_delta = total_base - total_mcp
    avg_base  = total_base / n
    avg_mcp   = total_mcp  / n
    avg_delta = total_delta / n
    overall_pct = 100 * total_delta / total_base if total_base else 0

    print(f"\n{'='*60}")
    print("OVERALL TOKEN SAVINGS (Condition A → Condition C)")
    print(f"{'='*60}")
    print(f"  Questions compared:   {n}")
    print(f"  Total baseline tokens: {total_base:>12,}")
    print(f"  Total MCP tokens:      {total_mcp:>12,}")
    print(f"  Total saved:           {total_delta:>12,}  ({overall_pct:+.1f}%)")
    print(f"  Avg baseline/question: {avg_base:>12,.0f}")
    print(f"  Avg MCP/question:      {avg_mcp:>12,.0f}")
    print(f"  Avg delta/question:    {avg_delta:>12,.0f}")

    # ── By question type ─────────────────────────────────────────────────────
    type_order = [
        "information_extraction",
        "multi_hop_retrieval",
        "knowledge_update",
        "temporal_reasoning",
        "debugging_and_provenance",
        "unknown",
    ]
    by_type = defaultdict(list)
    for r in rows_with_data:
        by_type[r["type"]].append(r)

    print(f"\n### Token Savings by Question Type")
    print(f"{'Type':<30} {'N':>4}  {'Avg Base':>10}  {'Avg MCP':>10}  {'Avg Delta':>10}  {'Saved':>8}")
    print("-" * 80)
    for qtype in type_order:
        grp = by_type.get(qtype)
        if not grp:
            continue
        ab = sum(r["base"] for r in grp) / len(grp)
        am = sum(r["mcp"]  for r in grp) / len(grp)
        ad = ab - am
        ps = 100 * ad / ab if ab else 0
        print(f"{qtype:<30} {len(grp):>4}  {ab:>10,.0f}  {am:>10,.0f}  {ad:>10,.0f}  {ps:>+7.1f}%")
    for qtype, grp in by_type.items():
        if qtype not in type_order:
            ab = sum(r["base"] for r in grp) / len(grp)
            am = sum(r["mcp"]  for r in grp) / len(grp)
            ad = ab - am
            ps = 100 * ad / ab if ab else 0
            print(f"{qtype:<30} {len(grp):>4}  {ab:>10,.0f}  {am:>10,.0f}  {ad:>10,.0f}  {ps:>+7.1f}%")

    # ── By dataset ───────────────────────────────────────────────────────────
    by_set = defaultdict(list)
    for r in rows_with_data:
        by_set[r["set"]].append(r)

    print(f"\n### Token Savings by Dataset")
    print(f"{'Dataset':<20} {'N':>4}  {'Avg Base':>10}  {'Avg MCP':>10}  {'Avg Delta':>10}  {'Saved':>8}")
    print("-" * 70)
    for ds, grp in sorted(by_set.items()):
        ab = sum(r["base"] for r in grp) / len(grp)
        am = sum(r["mcp"]  for r in grp) / len(grp)
        ad = ab - am
        ps = 100 * ad / ab if ab else 0
        print(f"{ds:<20} {len(grp):>4}  {ab:>10,.0f}  {am:>10,.0f}  {ad:>10,.0f}  {ps:>+7.1f}%")

    # ── By hop count ─────────────────────────────────────────────────────────
    by_hops = defaultdict(list)
    for r in rows_with_data:
        by_hops[r["hops"]].append(r)

    print(f"\n### Token Savings by Hop Count")
    print(f"{'Hops':<8} {'N':>4}  {'Avg Base':>10}  {'Avg MCP':>10}  {'Avg Delta':>10}  {'Saved':>8}")
    print("-" * 60)
    for hops in sorted(by_hops.keys()):
        grp = by_hops[hops]
        ab = sum(r["base"] for r in grp) / len(grp)
        am = sum(r["mcp"]  for r in grp) / len(grp)
        ad = ab - am
        ps = 100 * ad / ab if ab else 0
        print(f"{hops:<8} {len(grp):>4}  {ab:>10,.0f}  {am:>10,.0f}  {ad:>10,.0f}  {ps:>+7.1f}%")

    # ── Markdown table for reports ───────────────────────────────────────────
    print(f"\n{'='*60}")
    print("Markdown table for reports:")
    print(f"{'='*60}\n")
    print("| Metric | Baseline (A) | MCP (C) | Delta | % Saved |")
    print("|--------|-------------|---------|-------|---------|")
    print(f"| Avg tokens/question | {avg_base:,.0f} | {avg_mcp:,.0f} | {avg_delta:,.0f} | {overall_pct:+.1f}% |")
    for qtype in type_order:
        grp = by_type.get(qtype)
        if not grp:
            continue
        ab = sum(r["base"] for r in grp) / len(grp)
        am = sum(r["mcp"]  for r in grp) / len(grp)
        ad = ab - am
        ps = 100 * ad / ab if ab else 0
        label = qtype.replace("_", " ").title()
        print(f"| Avg tokens — {label} | {ab:,.0f} | {am:,.0f} | {ad:,.0f} | {ps:+.1f}% |")


if __name__ == "__main__":
    main()
