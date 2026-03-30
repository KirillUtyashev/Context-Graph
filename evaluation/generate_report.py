#!/usr/bin/env python3
"""
generate_report.py — Three-condition comparison report (Markdown).

Combines scored + token data from all three evaluation conditions and writes
a single comparison_report.md.

Conditions:
  A — Baseline   (agent reads repo files directly)
  C — Context-Graph MCP  (agent uses context-graph HTTP MCP server)
  D — Codebase-Memory MCP (agent uses codebase-memory-mcp CLI)

Usage:
    python baseline_run/generate_report.py

    # Custom paths:
    python baseline_run/generate_report.py \\
        --baseline-scored  baseline_run/scored.jsonl \\
        --mcp-scored       baseline_run/scored_mcp.jsonl \\
        --cbm-scored       baseline_run/scored_cbm.jsonl \\
        --baseline-tokens  baseline_run/results_with_tokens.jsonl \\
        --mcp-tokens       baseline_run/results_with_tokens_mcp.jsonl \\
        --cbm-tokens       baseline_run/results_with_tokens_cbm.jsonl \\
        --output           baseline_run/comparison_report.md

Output:
    baseline_run/comparison_report.md
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULTS = {
    "baseline_scored":  Path("baseline_run/scored.jsonl"),
    "mcp_scored":       Path("baseline_run/scored_mcp.jsonl"),
    "cbm_scored":       Path("baseline_run/scored_cbm.jsonl"),
    "baseline_tokens":  Path("baseline_run/results_with_tokens.jsonl"),
    "mcp_tokens":       Path("baseline_run/results_with_tokens_mcp.jsonl"),
    "cbm_tokens":       Path("baseline_run/results_with_tokens_cbm.jsonl"),
    "output":           Path("baseline_run/comparison_report.md"),
}

TYPE_ORDER = [
    "information_extraction",
    "multi_hop_retrieval",
    "knowledge_update",
    "temporal_reasoning",
    "debugging_and_provenance",
]

COND_LABELS = {
    "A": "A — Baseline",
    "C": "C — Context-Graph MCP",
    "D": "D — Codebase-Memory MCP",
}

# ── Data loading ──────────────────────────────────────────────────────────────

def load_jsonl(path):
    rows = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows[r["id"]] = r
    return rows


def get_tokens(row):
    """Return total token count or None."""
    t = row.get("tokens")
    if t and t.get("total", 0) > 0:
        return t["total"]
    return None


def build_merged(scored_path, tokens_path):
    """
    Merge scored verdicts with token data.
    Returns dict: id -> {id, set, type, hops, judge_verdict, miss_class, tokens_total, tokens_row}
    """
    scored  = load_jsonl(scored_path)
    tokens  = load_jsonl(tokens_path)
    merged  = {}
    for qid, s in scored.items():
        t_row   = tokens.get(qid, {})
        t_total = get_tokens(t_row)
        merged[qid] = {
            "id":           qid,
            "raw_id":       s.get("raw_id", 0),
            "set":          s.get("set", "unknown"),
            "type":         s.get("type", "unknown"),
            "hops":         s.get("hops", 0),
            "question":     s.get("question", ""),
            "judge_verdict":s.get("judge_verdict", "UNSCORED"),
            "miss_class":   s.get("miss_class"),
            "tokens_total": t_total,
        }
    return merged


# ── Helpers ───────────────────────────────────────────────────────────────────

def pct(num, denom):
    if not denom:
        return "—"
    return f"{100 * num / denom:.1f}%"


def fmt_tok(n):
    if n is None:
        return "—"
    return f"{n:,}"


def fmt_delta(a, b):
    """% change from a to b, formatted with sign."""
    if a and b and a > 0:
        d = 100 * (b - a) / a
        return f"{d:+.1f}%"
    return "—"


def efficiency(hit_rate_frac, avg_tokens):
    """hits per 1,000 tokens = hit_rate / (avg_tokens / 1000)"""
    if avg_tokens and avg_tokens > 0:
        return hit_rate_frac / (avg_tokens / 1000)
    return None


def fmt_eff(e):
    if e is None:
        return "—"
    return f"{e:.4f}"


def verdict_icon(v):
    if v == "HIT":
        return "HIT"
    if v == "MISS":
        return "**MISS**"
    return v


# ── Section builders ──────────────────────────────────────────────────────────

def section_header(ts, all_ids):
    lines = [
        "# Evaluation Report: Context-Graph Retrieval Benchmark",
        "",
        f"**Generated:** {ts}  ",
        f"**Questions:** {len(all_ids)} (80 main\\_v2 + 10 bug\\_pr)  ",
        "**Conditions:** A (Baseline) · C (Context-Graph MCP) · D (Codebase-Memory MCP)  ",
        "",
    ]
    return lines


def section_exec_summary(conds, all_ids):
    lines = ["## Executive Summary", ""]

    rows_data = []
    for label, data in conds.items():
        scored = [data[qid] for qid in all_ids if qid in data]
        verdicts = [r for r in scored if r["judge_verdict"] in ("HIT", "MISS")]
        hits = sum(1 for r in verdicts if r["judge_verdict"] == "HIT")
        total = len(verdicts)
        tok_rows = [r["tokens_total"] for r in scored if r["tokens_total"] is not None]
        avg_tok = sum(tok_rows) / len(tok_rows) if tok_rows else None
        total_tok = sum(tok_rows) if tok_rows else None
        hit_frac = hits / total if total else 0
        eff = efficiency(hit_frac, avg_tok)
        rows_data.append({
            "label": COND_LABELS[label],
            "hits": hits, "total": total,
            "avg_tok": avg_tok, "total_tok": total_tok,
            "eff": eff,
        })

    base_avg = rows_data[0]["avg_tok"]

    lines += [
        "| Metric | " + " | ".join(r["label"] for r in rows_data) + " |",
        "|--------|" + "|".join(["------"] * len(rows_data)) + "|",
        "| **Hit Rate** | " + " | ".join(
            f"{pct(r['hits'], r['total'])} ({r['hits']}/{r['total']})" for r in rows_data
        ) + " |",
        "| **Avg Tokens / Question** | " + " | ".join(
            fmt_tok(round(r["avg_tok"])) if r["avg_tok"] else "—" for r in rows_data
        ) + " |",
        "| **Total Tokens (90 Qs)** | " + " | ".join(
            fmt_tok(r["total_tok"]) for r in rows_data
        ) + " |",
        "| **Token Efficiency** _(hits / 1k tokens)_ | " + " | ".join(
            fmt_eff(r["eff"]) for r in rows_data
        ) + " |",
        "| **Token Δ vs Baseline** | — | " + " | ".join(
            fmt_delta(base_avg, r["avg_tok"]) for r in rows_data[1:]
        ) + " |",
        "",
        "> **Token Efficiency** = Hit Rate / (Avg Tokens / 1000). Higher is better.",
        "",
    ]
    return lines


def section_accuracy(conds, all_ids):
    lines = ["## Accuracy Analysis", ""]

    def acc_table(title, groups_fn):
        tlines = [f"### {title}", ""]
        header = "| Category | A hits/total | A% | C hits/total | C% | D hits/total | D% |"
        sep    = "|----------|-------------|----|----|----|----|-----|"
        tlines += [header, sep]
        groups = {}
        for label, data in conds.items():
            for qid in all_ids:
                r = data.get(qid)
                if not r or r["judge_verdict"] not in ("HIT", "MISS"):
                    continue
                key = groups_fn(r)
                groups.setdefault(key, {}).setdefault(label, []).append(r)

        all_keys = sorted(groups.keys(), key=lambda k: (isinstance(k, str), k))
        for key in all_keys:
            row_parts = [str(key) if not isinstance(key, str) else key.replace("_", " ")]
            for label in ("A", "C", "D"):
                grp = groups.get(key, {}).get(label, [])
                h = sum(1 for r in grp if r["judge_verdict"] == "HIT")
                n = len(grp)
                row_parts.append(f"{h}/{n}")
                row_parts.append(pct(h, n))
            tlines.append("| " + " | ".join(row_parts) + " |")
        tlines.append("")
        return tlines

    lines += acc_table("Accuracy by Question Type",
                       lambda r: r.get("type", "unknown") if r.get("type") in TYPE_ORDER else "other")
    lines += acc_table("Accuracy by Dataset", lambda r: r.get("set", "unknown"))
    lines += acc_table("Accuracy by Hop Count", lambda r: r.get("hops", 0))
    return lines


def section_tokens(conds, all_ids):
    lines = ["## Token Consumption", ""]

    def tok_table(title, groups_fn):
        tlines = [f"### {title}", ""]
        header = "| Category | Avg A | Avg C | Avg D | C vs A | D vs A |"
        sep    = "|----------|-------|-------|-------|--------|--------|"
        tlines += [header, sep]

        groups = {}
        for label, data in conds.items():
            for qid in all_ids:
                r = data.get(qid)
                if not r or r["tokens_total"] is None:
                    continue
                key = groups_fn(r)
                groups.setdefault(key, {}).setdefault(label, []).append(r["tokens_total"])

        all_keys = sorted(groups.keys(), key=lambda k: (isinstance(k, str), k))
        for key in all_keys:
            avgs = {}
            for label in ("A", "C", "D"):
                vals = groups.get(key, {}).get(label, [])
                avgs[label] = sum(vals) / len(vals) if vals else None
            da = fmt_delta(avgs["A"], avgs["C"])
            db = fmt_delta(avgs["A"], avgs["D"])
            key_str = str(key) if not isinstance(key, str) else key.replace("_", " ")
            tlines.append(
                f"| {key_str} "
                f"| {fmt_tok(round(avgs['A'])) if avgs['A'] else '—'} "
                f"| {fmt_tok(round(avgs['C'])) if avgs['C'] else '—'} "
                f"| {fmt_tok(round(avgs['D'])) if avgs['D'] else '—'} "
                f"| {da} | {db} |"
            )
        tlines.append("")
        return tlines

    # Overall
    lines += ["### Overall Token Consumption", ""]
    lines += ["| Condition | Avg Tokens | Total Tokens | Min | Max |",
              "|-----------|-----------|-------------|-----|-----|"]
    for label, data in conds.items():
        vals = [data[qid]["tokens_total"] for qid in all_ids
                if qid in data and data[qid]["tokens_total"] is not None]
        if vals:
            lines.append(
                f"| {COND_LABELS[label]} "
                f"| {fmt_tok(round(sum(vals)/len(vals)))} "
                f"| {fmt_tok(sum(vals))} "
                f"| {fmt_tok(min(vals))} "
                f"| {fmt_tok(max(vals))} |"
            )
    lines.append("")

    lines += tok_table("Token Consumption by Question Type",
                       lambda r: r.get("type", "unknown") if r.get("type") in TYPE_ORDER else "other")
    lines += tok_table("Token Consumption by Dataset", lambda r: r.get("set", "unknown"))
    lines += tok_table("Token Consumption by Hop Count", lambda r: r.get("hops", 0))
    return lines


def section_efficiency(conds, all_ids):
    lines = ["## Token Efficiency", "",
             "> **Definition:** Efficiency = Hit Rate (%) ÷ (Avg Tokens / 1,000)  ",
             "> Interpretable as *hits per 1,000 tokens*. Higher = better cost-effectiveness.",
             ""]

    # Overall
    lines += ["### Overall Efficiency", ""]
    lines += ["| Condition | Hit Rate | Avg Tokens | Efficiency |",
              "|-----------|---------|-----------|-----------|"]
    for label, data in conds.items():
        scored = [data[qid] for qid in all_ids if qid in data]
        verdicts = [r for r in scored if r["judge_verdict"] in ("HIT", "MISS")]
        hits = sum(1 for r in verdicts if r["judge_verdict"] == "HIT")
        total = len(verdicts)
        tok_vals = [r["tokens_total"] for r in scored if r["tokens_total"] is not None]
        avg_tok = sum(tok_vals) / len(tok_vals) if tok_vals else None
        hit_frac = hits / total if total else 0
        eff = efficiency(hit_frac, avg_tok)
        lines.append(
            f"| {COND_LABELS[label]} "
            f"| {pct(hits, total)} "
            f"| {fmt_tok(round(avg_tok)) if avg_tok else '—'} "
            f"| {fmt_eff(eff)} |"
        )
    lines.append("")

    # By question type
    lines += ["### Efficiency by Question Type", ""]
    lines += ["| Type | A Eff | C Eff | D Eff |",
              "|------|-------|-------|-------|"]

    type_keys = TYPE_ORDER + sorted(
        set(data[qid].get("type","unknown") for data in conds.values() for qid in all_ids if qid in data)
        - set(TYPE_ORDER)
    )
    for qtype in type_keys:
        row_parts = [qtype.replace("_", " ")]
        for label, data in conds.items():
            grp = [data[qid] for qid in all_ids if qid in data
                   and data[qid].get("type") == qtype
                   and data[qid]["judge_verdict"] in ("HIT", "MISS")]
            hits = sum(1 for r in grp if r["judge_verdict"] == "HIT")
            total = len(grp)
            tok_vals = [r["tokens_total"] for r in grp if r["tokens_total"] is not None]
            avg_tok = sum(tok_vals) / len(tok_vals) if tok_vals else None
            hit_frac = hits / total if total else 0
            eff = efficiency(hit_frac, avg_tok)
            row_parts.append(fmt_eff(eff))
        if any(r != "—" for r in row_parts[1:]):
            lines.append("| " + " | ".join(row_parts) + " |")
    lines.append("")
    return lines


def section_misses(conds, all_ids):
    lines = ["## Miss Analysis", ""]

    lines += ["### Miss Counts and Classification", ""]
    lines += ["| Condition | Total MISSes | guessed\\_wrong | abstained | unclassified |",
              "|-----------|-------------|----------------|-----------|--------------|"]
    for label, data in conds.items():
        misses = [data[qid] for qid in all_ids if qid in data and data[qid]["judge_verdict"] == "MISS"]
        gw = sum(1 for r in misses if r.get("miss_class") == "guessed_wrong")
        ab = sum(1 for r in misses if r.get("miss_class") == "abstained")
        uc = len(misses) - gw - ab
        lines.append(f"| {COND_LABELS[label]} | {len(misses)} | {gw} | {ab} | {uc} |")
    lines.append("")

    lines += ["### Missed Questions per Condition", ""]
    for label, data in conds.items():
        misses = sorted(
            [data[qid] for qid in all_ids if qid in data and data[qid]["judge_verdict"] == "MISS"],
            key=lambda r: (r["set"], r["raw_id"])
        )
        if misses:
            lines.append(f"**{COND_LABELS[label]}** ({len(misses)} MISSes):")
            for r in misses:
                mc = r.get("miss_class") or "unclassified"
                lines.append(f"- `{r['id']}` ({r.get('type','?').replace('_',' ')}, {r['hops']} hops) — {mc}")
            lines.append("")
        else:
            lines.append(f"**{COND_LABELS[label]}**: no MISSes")
            lines.append("")

    # Which questions were missed by ALL conditions?
    miss_sets = {label: {qid for qid in all_ids if data.get(qid, {}).get("judge_verdict") == "MISS"}
                 for label, data in conds.items()}
    missed_all   = miss_sets["A"] & miss_sets["C"] & miss_sets["D"]
    missed_c_d   = (miss_sets["C"] | miss_sets["D"]) - miss_sets["A"]
    missed_c_not_d = miss_sets["C"] - miss_sets["D"] - miss_sets["A"]
    missed_d_not_c = miss_sets["D"] - miss_sets["C"] - miss_sets["A"]

    lines += ["### Miss Overlap", ""]
    if missed_all:
        lines.append(f"**Missed by all 3 conditions:** {', '.join(sorted(missed_all))}")
    else:
        lines.append("**Missed by all 3 conditions:** none")
    lines.append("")
    if missed_c_d:
        lines.append(f"**Missed by C or D but not A (MCP-only misses):** {', '.join(sorted(missed_c_d))}")
        lines.append("")
    if missed_c_not_d:
        lines.append(f"**Missed by C but not D:** {', '.join(sorted(missed_c_not_d))}")
        lines.append("")
    if missed_d_not_c:
        lines.append(f"**Missed by D but not C:** {', '.join(sorted(missed_d_not_c))}")
        lines.append("")
    return lines


def section_appendix(conds, all_ids):
    lines = ["## Appendix: Per-Question Results", "",
             "All 90 questions with verdicts and token counts for each condition.",
             ""]
    lines += [
        "| ID | Type | Hops | A verdict | A tokens | C verdict | C tokens | D verdict | D tokens |",
        "|----|------|------|-----------|----------|-----------|----------|-----------|----------|",
    ]

    miss_ids = set()
    for data in conds.values():
        for qid in all_ids:
            if data.get(qid, {}).get("judge_verdict") == "MISS":
                miss_ids.add(qid)

    # Sort: misses first (by set+raw_id), then hits
    def sort_key(qid):
        r = next((data[qid] for data in conds.values() if qid in data), {})
        is_miss = qid in miss_ids
        return (0 if is_miss else 1, r.get("set",""), r.get("raw_id", 0))

    sorted_ids = sorted(all_ids, key=sort_key)

    prev_was_miss = None
    for qid in sorted_ids:
        is_miss = qid in miss_ids
        if prev_was_miss is not None and prev_was_miss != is_miss:
            lines.append("| | | | | | | | | |")  # visual separator row

        # Get metadata from any condition
        meta = next((data[qid] for data in conds.values() if qid in data), {})
        qtype = meta.get("type","unknown").replace("_"," ")[:22]
        hops  = meta.get("hops", 0)

        row_parts = [qid, qtype, str(hops)]
        for label, data in conds.items():
            r = data.get(qid, {})
            v = r.get("judge_verdict", "—")
            t = r.get("tokens_total")
            row_parts.append(verdict_icon(v))
            row_parts.append(fmt_tok(t))

        lines.append("| " + " | ".join(row_parts) + " |")
        prev_was_miss = is_miss

    lines.append("")
    return lines


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-scored",  default=DEFAULTS["baseline_scored"],  type=Path)
    parser.add_argument("--mcp-scored",       default=DEFAULTS["mcp_scored"],       type=Path)
    parser.add_argument("--cbm-scored",       default=DEFAULTS["cbm_scored"],       type=Path)
    parser.add_argument("--baseline-tokens",  default=DEFAULTS["baseline_tokens"],  type=Path)
    parser.add_argument("--mcp-tokens",       default=DEFAULTS["mcp_tokens"],       type=Path)
    parser.add_argument("--cbm-tokens",       default=DEFAULTS["cbm_tokens"],       type=Path)
    parser.add_argument("--output",           default=DEFAULTS["output"],           type=Path)
    args = parser.parse_args()

    # Validate inputs
    for attr, path in [
        ("baseline_scored", args.baseline_scored),
        ("mcp_scored",      args.mcp_scored),
        ("cbm_scored",      args.cbm_scored),
        ("baseline_tokens", args.baseline_tokens),
        ("mcp_tokens",      args.mcp_tokens),
        ("cbm_tokens",      args.cbm_tokens),
    ]:
        if not path.exists():
            print(f"ERROR: {attr} file not found: {path}")
            return

    # Load merged data for each condition
    conds = {
        "A": build_merged(args.baseline_scored, args.baseline_tokens),
        "C": build_merged(args.mcp_scored,      args.mcp_tokens),
        "D": build_merged(args.cbm_scored,       args.cbm_tokens),
    }

    # Union of all question IDs, sorted by set then raw_id
    all_ids_set = set()
    for data in conds.values():
        all_ids_set |= set(data.keys())
    all_ids = sorted(
        all_ids_set,
        key=lambda qid: (
            next((data[qid]["set"]    for data in conds.values() if qid in data), ""),
            next((data[qid]["raw_id"] for data in conds.values() if qid in data), 0),
        )
    )

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build report
    report_lines = []
    report_lines += section_header(ts, all_ids)
    report_lines += section_exec_summary(conds, all_ids)
    report_lines += section_accuracy(conds, all_ids)
    report_lines += section_tokens(conds, all_ids)
    report_lines += section_efficiency(conds, all_ids)
    report_lines += section_misses(conds, all_ids)
    report_lines += section_appendix(conds, all_ids)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(report_lines))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
