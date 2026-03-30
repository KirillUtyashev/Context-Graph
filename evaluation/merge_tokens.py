#!/usr/bin/env python3
"""
merge_tokens.py — Join Cursor dashboard usage CSV to results.jsonl by time window.

Each Cloud Agent run corresponds to one or more CSV rows whose Date falls in the
window [created_at[i], created_at[i+1]). Zero-token rows are excluded. All matching
rows are summed and attached to the result entry.

Usage:
    # Baseline condition:
    python baseline_run/merge_tokens.py baseline_run/usage_export.csv

    # MCP condition (explicit paths):
    python baseline_run/merge_tokens.py baseline_run/usage_export_mcp.csv \\
        --results baseline_run/results_mcp.jsonl \\
        --output  baseline_run/results_with_tokens_mcp.jsonl

Input:
    <results_file>  (default: baseline_run/results.jsonl)
    <csv_path>      (exported from Cursor dashboard Settings → Usage → Export)

Output:
    <output_file>   (default: baseline_run/results_with_tokens.jsonl)
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Warn if a time window contains more than this many non-zero CSV rows
# (suggests other Cursor activity interfered during the benchmark run)
ROW_COUNT_WARNING_THRESHOLD = 3


def parse_dt(s):
    """Parse ISO 8601 timestamp string to aware datetime."""
    s = s.strip().strip('"')
    # Handle both Z and +00:00 suffixes
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def load_results(results_file):
    rows = []
    with open(results_file) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    # Sort by created_at ascending (the order agents were launched)
    rows.sort(key=lambda r: r.get("created_at", ""))
    return rows


def load_csv(csv_path):
    """Load dashboard CSV, return list of dicts with parsed datetime and int token fields."""
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dt = parse_dt(row["Date"])
                total = int(row.get("Total Tokens", "0") or "0")
                rows.append({
                    "dt":                dt,
                    "input_cache_write": int(row.get("Input (w/ Cache Write)", "0") or "0"),
                    "input_no_cache":    int(row.get("Input (w/o Cache Write)", "0") or "0"),
                    "cache_read":        int(row.get("Cache Read", "0") or "0"),
                    "output":            int(row.get("Output Tokens", "0") or "0"),
                    "total":             total,
                    "model":             row.get("Model", ""),
                })
            except Exception as e:
                print(f"  Warning: skipping unparseable CSV row: {row} ({e})")
    # Sort ascending by date
    rows.sort(key=lambda r: r["dt"])
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", help="Path to Cursor dashboard usage CSV export")
    parser.add_argument("--results", default="baseline_run/results.jsonl",
                        help="Input results JSONL (default: baseline_run/results.jsonl)")
    parser.add_argument("--output", default="baseline_run/results_with_tokens.jsonl",
                        help="Output JSONL with tokens attached (default: baseline_run/results_with_tokens.jsonl)")
    args = parser.parse_args()

    csv_path     = Path(args.csv)
    results_file = Path(args.results)
    output_file  = Path(args.output)

    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}")
        sys.exit(1)

    if not results_file.exists():
        print(f"ERROR: {results_file} not found.")
        sys.exit(1)

    results = load_results(results_file)
    csv_rows = load_csv(csv_path)

    print(f"Loaded {len(results)} result rows")
    print(f"Loaded {len(csv_rows)} CSV rows ({sum(1 for r in csv_rows if r['total'] > 0)} non-zero)\n")

    # Build time windows: window[i] = [created_at[i], created_at[i+1])
    MAX_DT = datetime(9999, 12, 31, tzinfo=timezone.utc)
    windows = []
    for i, result in enumerate(results):
        start = parse_dt(result["created_at"])
        end   = parse_dt(results[i + 1]["created_at"]) if i + 1 < len(results) else MAX_DT
        windows.append((start, end))

    enriched = []
    for i, (result, (win_start, win_end)) in enumerate(zip(results, windows), 1):
        # Find CSV rows in this window with non-zero tokens
        matching = [
            r for r in csv_rows
            if win_start <= r["dt"] < win_end and r["total"] > 0
        ]

        if len(matching) > ROW_COUNT_WARNING_THRESHOLD:
            print(
                f"  Warning: agent {i} ({result['id']}) has {len(matching)} matching CSV rows "
                f"(threshold {ROW_COUNT_WARNING_THRESHOLD}) — possible interference from "
                f"other Cursor activity during the benchmark run."
            )

        if not matching:
            tokens = None
            status = "NO_MATCH"
        else:
            tokens = {
                "input_cache_write": sum(r["input_cache_write"] for r in matching),
                "input_no_cache":    sum(r["input_no_cache"]    for r in matching),
                "cache_read":        sum(r["cache_read"]        for r in matching),
                "output":            sum(r["output"]            for r in matching),
                "total":             sum(r["total"]             for r in matching),
                "csv_row_count":     len(matching),
            }
            status = "OK"

        enriched_row = {**result, "tokens": tokens, "token_match_status": status}
        enriched.append(enriched_row)

        token_summary = f"total={tokens['total']}" if tokens else "NO_MATCH"
        print(f"  [{i:3}] {result['id']:<25} window [{win_start.isoformat()[:19]} → {win_end.isoformat()[:19]}]  {token_summary}")

    # Write output
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        for row in enriched:
            f.write(json.dumps(row) + "\n")

    matched   = sum(1 for r in enriched if r["token_match_status"] == "OK")
    unmatched = sum(1 for r in enriched if r["token_match_status"] == "NO_MATCH")

    print(f"\nWrote {len(enriched)} rows to {output_file}")
    print(f"  Matched:   {matched}")
    print(f"  Unmatched: {unmatched}")
    if unmatched:
        print("  Tip: unmatched rows mean no CSV entries fell in that agent's time window.")
        print("  This can happen if the agent failed or if the CSV export is incomplete.")

    print("\nNext step: run judge_results.py to generate ChatGPT judge prompts.")


if __name__ == "__main__":
    main()
