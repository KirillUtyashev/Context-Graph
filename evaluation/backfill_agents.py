#!/usr/bin/env python3
"""
backfill_agents.py — Fetch answers from existing Cursor agents and patch results_mcp.jsonl.

Use this when an agent ran successfully but the polling request timed out before we
could capture the answer. Pass agent IDs and the question IDs they correspond to.

Usage:
    python baseline_run/backfill_agents.py \
        --map main_v2_Q57=<agent_id> main_v2_Q93=<agent_id> \
        [--results baseline_run/results_mcp.jsonl] \
        [--delete]

Options:
    --map     One or more question_id=agent_id pairs
    --results Results JSONL to patch (default: baseline_run/results_mcp.jsonl)
    --delete  Delete agents from Cursor after fetching (default: off)
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

API_KEY  = os.environ.get("CURSOR_API_KEY", "")
BASE_URL = "https://api.cursor.com"


def auth():
    return (API_KEY, "")


def get_conversation(agent_id):
    resp = requests.get(
        f"{BASE_URL}/v0/agents/{agent_id}/conversation",
        auth=auth(),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("messages", [])


def extract_answer(messages):
    for msg in reversed(messages):
        if msg.get("type") == "assistant_message" and msg.get("text", "").strip():
            return msg["text"].strip()
    return ""


def get_agent_meta(agent_id):
    resp = requests.get(
        f"{BASE_URL}/v0/agents/{agent_id}",
        auth=auth(),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def delete_agent(agent_id):
    resp = requests.delete(
        f"{BASE_URL}/v0/agents/{agent_id}",
        auth=auth(),
        timeout=30,
    )
    if resp.ok:
        print(f"  deleted agent {agent_id}")
    else:
        print(f"  Warning: could not delete {agent_id}: {resp.status_code}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", nargs="+", required=True,
                        metavar="QID=AGENT_ID",
                        help="question_id=agent_id pairs to backfill")
    parser.add_argument("--results", default="baseline_run/results_mcp.jsonl",
                        help="Results JSONL to patch (default: baseline_run/results_mcp.jsonl)")
    parser.add_argument("--delete", action="store_true",
                        help="Delete agents from Cursor after fetching")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: CURSOR_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    # Parse --map args
    mapping = {}
    for item in args.map:
        if "=" not in item:
            print(f"ERROR: invalid --map entry '{item}' (expected QID=AGENT_ID)")
            sys.exit(1)
        qid, agent_id = item.split("=", 1)
        mapping[qid] = agent_id

    results_file = Path(args.results)
    if not results_file.exists():
        print(f"ERROR: {results_file} not found.")
        sys.exit(1)

    # Load all rows
    rows = []
    with open(results_file) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    # Patch each matching row
    patched = 0
    for qid, agent_id in mapping.items():
        row_idx = next((i for i, r in enumerate(rows) if r["id"] == qid), None)
        if row_idx is None:
            print(f"  WARNING: {qid} not found in {results_file} — skipping")
            continue

        print(f"\n{qid} ← agent {agent_id}")
        try:
            meta = get_agent_meta(agent_id)
            messages = get_conversation(agent_id)
            answer = extract_answer(messages)

            if not answer:
                print(f"  WARNING: no assistant message found for agent {agent_id}")
                continue

            created_at = meta.get("createdAt", rows[row_idx]["created_at"])
            rows[row_idx]["agent_id"]   = agent_id
            rows[row_idx]["created_at"] = created_at
            rows[row_idx]["model_answer"] = answer
            rows[row_idx].pop("error", None)

            print(f"  status:  {meta.get('status', 'unknown')}")
            print(f"  answer:  {answer[:120]}{'...' if len(answer) > 120 else ''}")
            patched += 1

            if args.delete:
                delete_agent(agent_id)

        except Exception as e:
            print(f"  FAILED: {e}")

    if patched == 0:
        print("\nNothing patched.")
        return

    # Write back
    with open(results_file, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    print(f"\nPatched {patched} row(s) in {results_file}")
    print("\nNEXT STEPS:")
    print("  1. Run judge_results.py with --ids to generate a mini batch for just these questions:")
    print(f"     python baseline_run/judge_results.py --results {results_file} \\")
    print(f"         --batches baseline_run/judge_batches_mcp --ids {' '.join(mapping.keys())}")
    print("  2. Paste the new batch into ChatGPT, save response as response_N.txt")
    print("  3. Run parse_judge_responses.py as normal — it will fill in the missing verdicts")


if __name__ == "__main__":
    main()
