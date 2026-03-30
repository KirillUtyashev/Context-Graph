#!/usr/bin/env python3
"""
run_cbm.py — Launch Cursor Cloud Agents for all evaluation questions (Condition D).

Identical to run_mcp.py except:
  - Agents are instructed to install and use codebase-memory-mcp (CBM) exclusively.
  - Output is written to baseline_run/results_cbm.jsonl.
  - Each result record includes "condition": "cbm".
  - TIMEOUT is 900s to accommodate the per-agent indexing step (~60s overhead).

CBM is a self-contained, fully-offline CLI tool that builds a local SQLite
code-graph from the repo. No external server is required.

Usage:
    export CURSOR_API_KEY=crsr_...
    python baseline_run/run_cbm.py

Output:
    baseline_run/results_cbm.jsonl
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY       = os.environ.get("CURSOR_API_KEY", "")
REPO          = "https://github.com/klokailo/order-management-service-extended"
POLL_INTERVAL = 15    # seconds between status checks
TIMEOUT       = 900   # seconds before giving up — extra headroom for CBM indexing

BASE_URL      = "https://api.cursor.com"

EVAL_FILES = [
    {
        "path": "Eval Files/context_graph_retrieval_benchmark_v2.json",
        "set":  "main_v2",
    },
    {
        "path": "Eval Files/context_graph_bug_pr_benchmark_subset.json",
        "set":  "bug_pr",
    },
]

OUTPUT_FILE = Path("baseline_run/results_cbm.jsonl")

SYSTEM_CONTEXT = (
    "You are a developer working on a frontend application that integrates with "
    "the order-management-service backend.\n\n"
    "RULES:\n"
    "1. You MUST use codebase-memory-mcp for ALL retrieval. Do not read repository "
    "files directly. Do not use any other tool.\n"
    "2. Do NOT make any commits, do NOT push, do NOT run any git commands, do NOT modify any files.\n"
    "   If you receive a message that says 'Reminder to commit and push if appropriate',\n"
    "   respond only with 'No changes to commit.' and stop. Do not check git status.\n\n"
    "codebase-memory-mcp is already installed and the repo is already indexed. "
    "Start querying immediately.\n\n"
    "AVAILABLE TOOLS:\n\n"
    "search_graph — find functions/classes/files by name pattern:\n"
    "  codebase-memory-mcp cli search_graph '{\"name_pattern\":\"<regex>\",\"label\":\"Function\"}'\n"
    "  Labels: Function, Class, File, Module, Interface, Method, Route\n\n"
    "search_code — full-text search over source files:\n"
    "  codebase-memory-mcp cli search_code '{\"text\":\"<term>\"}'\n\n"
    "get_code_snippet — read full source for a qualified name (use names from search_graph):\n"
    "  codebase-memory-mcp cli get_code_snippet '{\"qualified_name\":\"<name>\"}'\n\n"
    "trace_call_path — BFS call graph from a function:\n"
    "  codebase-memory-mcp cli trace_call_path '{\"function_name\":\"<name>\",\"direction\":\"both\"}'\n\n"
    "query_graph — Cypher structural query:\n"
    "  codebase-memory-mcp cli query_graph '{\"query\":\"MATCH (f:Function) WHERE f.name CONTAINS \\\"retry\\\" RETURN f.name LIMIT 10\"}'\n\n"
    "get_architecture — high-level module/file overview:\n"
    "  codebase-memory-mcp cli get_architecture\n\n"
    "WORKFLOW: Start with search_graph or search_code. Use get_code_snippet with "
    "qualified_name values from results. Use trace_call_path to follow call chains. "
    "Provide a concise answer based only on what CBM returns.\n\n"
    "Question: "
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def auth():
    return (API_KEY, "")


def load_questions():
    """Load and flatten questions from all eval files, attaching type metadata."""
    repo_root = Path(__file__).parent.parent  # order-management-service-extended/
    questions = []

    for spec in EVAL_FILES:
        path = repo_root / spec["path"]
        with open(path) as f:
            data = json.load(f)

        id_to_type = {}
        for qtype, ids in data.get("question_type_coverage", {}).items():
            for qid in ids:
                id_to_type[qid] = qtype

        for item in data["eval_set"]:
            qid = item["id"]
            questions.append({
                "id":              f"{spec['set']}_Q{qid}",
                "raw_id":          qid,
                "set":             spec["set"],
                "type":            id_to_type.get(qid, "unknown"),
                "hops":            item.get("hops", 0),
                "leakage_risk":    item.get("leakage_risk", ""),
                "question":        item["question"],
                "expected_answer": item["expected_answer"],
            })

    return questions


def launch_agent(question_text):
    """POST /v0/agents and return the response JSON."""
    payload = {
        "prompt": {"text": SYSTEM_CONTEXT + question_text},
        "source": {"repository": REPO, "ref": "main"},
        "target": {"autoCreatePr": False},
    }
    resp = requests.post(
        f"{BASE_URL}/v0/agents",
        auth=auth(),
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(
            f"HTTP {resp.status_code} launching agent: {resp.text[:500]}"
        )
    return resp.json()


def delete_agent(agent_id):
    """DELETE /v0/agents/{id} to free quota after use."""
    try:
        resp = requests.delete(
            f"{BASE_URL}/v0/agents/{agent_id}",
            auth=auth(),
            timeout=30,
        )
        if resp.ok:
            print(f"         deleted agent {agent_id}")
        else:
            print(f"         Warning: could not delete agent {agent_id}: {resp.status_code}")
    except Exception as e:
        print(f"         Warning: delete failed: {e}")


def poll_until_done(agent_id):
    """Poll agent status until FINISHED or timeout. Returns final status dict."""
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        resp = requests.get(
            f"{BASE_URL}/v0/agents/{agent_id}",
            auth=auth(),
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "")
        if status == "FINISHED":
            return data
        if status not in ("CREATING", "RUNNING"):
            raise RuntimeError(f"Unexpected agent status: {status}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Agent {agent_id} did not finish within {TIMEOUT}s")


def get_answer(agent_id):
    """GET /v0/agents/{id}/conversation and return the last assistant message text."""
    resp = requests.get(
        f"{BASE_URL}/v0/agents/{agent_id}/conversation",
        auth=auth(),
        timeout=30,
    )
    resp.raise_for_status()
    messages = resp.json().get("messages", [])
    for msg in reversed(messages):
        if msg.get("type") == "assistant_message" and msg.get("text", "").strip():
            return msg["text"].strip()
    return ""


def append_result(record):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def already_done():
    """Return set of IDs already recorded in results_cbm.jsonl (for resumability)."""
    done = set()
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:
                    pass
    return done


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("ERROR: CURSOR_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    questions = load_questions()
    done = already_done()

    remaining = [q for q in questions if q["id"] not in done]
    total = len(questions)
    skipped = len(done)

    print(f"Loaded {total} questions ({skipped} already done, {len(remaining)} to run)")
    print(f"Output: {OUTPUT_FILE}\n")

    for i, q in enumerate(remaining, start=skipped + 1):
        print(f"[{i}/{total}] {q['id']} — {q['question'][:70]}...")
        t0 = time.time()

        time.sleep(3)
        try:
            launch_resp = launch_agent(q["question"])
            agent_id   = launch_resp["id"]
            created_at = launch_resp.get("createdAt", datetime.now(timezone.utc).isoformat())

            print(f"         agent {agent_id} launched, polling...")
            poll_until_done(agent_id)

            answer = get_answer(agent_id)
            delete_agent(agent_id)
            elapsed = round(time.time() - t0)

            record = {
                "id":              q["id"],
                "raw_id":          q["raw_id"],
                "set":             q["set"],
                "condition":       "cbm",
                "type":            q["type"],
                "hops":            q["hops"],
                "leakage_risk":    q["leakage_risk"],
                "question":        q["question"],
                "expected_answer": q["expected_answer"],
                "agent_id":        agent_id,
                "created_at":      created_at,
                "model_answer":    answer,
            }
            append_result(record)
            print(f"         done ({elapsed}s)\n")

        except Exception as e:
            print(f"         FAILED: {e}\n")
            record = {
                "id":              q["id"],
                "raw_id":          q["raw_id"],
                "set":             q["set"],
                "condition":       "cbm",
                "type":            q["type"],
                "hops":            q["hops"],
                "leakage_risk":    q["leakage_risk"],
                "question":        q["question"],
                "expected_answer": q["expected_answer"],
                "agent_id":        None,
                "created_at":      datetime.now(timezone.utc).isoformat(),
                "model_answer":    None,
                "error":           str(e),
            }
            append_result(record)

    print("\n" + "=" * 80)
    print("TOKEN MATCHING TABLE")
    print("Match this table (top-to-bottom) against your Cursor dashboard CSV export.")
    print("Dashboard rows are in reverse-chronological order — reverse them first.")
    print("=" * 80)
    print(f"{'Run':<5} {'ID':<20} {'created_at':<30} {'Question (short)'}")
    print("-" * 80)
    all_rows = []
    with open(OUTPUT_FILE) as f:
        for line in f:
            try:
                r = json.loads(line)
                all_rows.append(r)
            except Exception:
                pass
    all_rows.sort(key=lambda r: r.get("created_at", ""))
    for idx, r in enumerate(all_rows, 1):
        print(f"{idx:<5} {r['id']:<20} {r.get('created_at',''):<30} {r['question'][:45]}")

    print("\nDone. Next step: export usage CSV from Cursor dashboard and run:")
    print("  python baseline_run/merge_tokens.py baseline_run/usage_export_cbm.csv \\")
    print("      --results baseline_run/results_cbm.jsonl \\")
    print("      --output  baseline_run/results_with_tokens_cbm.jsonl")


if __name__ == "__main__":
    main()
