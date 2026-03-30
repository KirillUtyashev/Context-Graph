from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.metrics.metrics import compute_metrics


ROOT_DIR = Path(__file__).resolve().parents[2]
DATASET_PATH = ROOT_DIR / "evaluation" / "datasets" / "gold_eval_set.json"
RESULTS_PATH = ROOT_DIR / "evaluation" / "logs" / "results.json"


def load_dataset() -> list[dict[str, Any]]:
    with DATASET_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def retrieve_context(question: str, mode: str) -> list[str]:
    _ = question

    # TODO: Replace this placeholder branch with real embedding_clients retrieval once
    # the embedding_clients index and search pipeline are implemented in the repo.
    if mode == "none":
        return []
    if mode == "embedding_clients":
        return ["doc1", "doc2"]

    # TODO: Replace this placeholder branch with real graph-aware retrieval
    # backed by traversal/schema logic once graph querying is implemented.
    if mode == "graph":
        return ["doc1", "doc2", "doc3"]

    raise ValueError(f"unsupported mode: {mode!r}")


def generate_answer(question: str, context: list[str]) -> str:
    if context:
        return " ".join(context)
    return f"No context retrieved for: {question}"


def run_eval(mode: str) -> dict[str, Any]:
    dataset = load_dataset()
    results: list[dict[str, Any]] = []

    for example in dataset:
        question = example["question"]
        retrieved_docs = retrieve_context(question, mode)
        answer = generate_answer(question, retrieved_docs)
        results.append(
            {
                "question": question,
                "retrieved_docs": retrieved_docs,
                "answer": answer,
            }
        )

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)

    return {
        "mode": mode,
        "results_path": str(RESULTS_PATH),
        "results": results,
        "metrics": compute_metrics(results, dataset),
    }


if __name__ == "__main__":
    summary = run_eval(mode="graph")
    print(json.dumps(summary, indent=2))
