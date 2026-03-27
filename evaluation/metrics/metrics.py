from __future__ import annotations

from typing import Any


def file_hit_rate(retrieved_docs: list[str], gold_docs: list[str]) -> int:
    retrieved_set = set(retrieved_docs)
    gold_set = set(gold_docs)
    return int(gold_set.issubset(retrieved_set))


def fact_hit_rate(answer: str, gold_answer: str) -> int:
    return int(gold_answer in answer)


def compute_metrics(results: list[dict[str, Any]], dataset: list[dict[str, Any]]) -> dict[str, float]:
    if not dataset:
        return {
            "file_hit_rate": 0.0,
            "fact_hit_rate": 0.0,
        }

    total_file_hits = 0
    total_fact_hits = 0

    for result, example in zip(results, dataset):
        total_file_hits += file_hit_rate(
            result.get("retrieved_docs", []),
            example.get("gold_docs", []),
        )
        total_fact_hits += fact_hit_rate(
            result.get("answer", ""),
            example.get("gold_answer", ""),
        )

    count = len(dataset)
    return {
        "file_hit_rate": total_file_hits / count,
        "fact_hit_rate": total_fact_hits / count,
    }
