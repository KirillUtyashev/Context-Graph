"""
Cluster incorrect GSM8K model outputs for a nanochat checkpoint.

Example:
uv run python graph_script.py --source rl --model-tag d20_rl_dense --model-step 360 --task-field completion
"""

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.feature_extraction.text import TfidfVectorizer

from nanochat.checkpoint_manager import build_model, find_last_step, load_model
from nanochat.common import autodetect_device_type, compute_cleanup, compute_init, print0
from nanochat.engine import Engine
from tasks.gsm8k import GSM8K


def collect_incorrect_records(task, tokenizer, engine, num_problems, num_samples, max_new_tokens, temperature, top_k):
    records = []
    for idx in range(num_problems):
        conversation = task[idx]
        encoded_prompt = tokenizer.render_for_completion(conversation)
        prefix_length = len(encoded_prompt)
        results, _ = engine.generate_batch(
            encoded_prompt,
            num_samples=num_samples,
            max_tokens=max_new_tokens,
            temperature=temperature,
            top_k=top_k,
        )
        for sample_idx, result_tokens in enumerate(results):
            completion = tokenizer.decode(result_tokens[prefix_length:])
            is_correct = task.evaluate(conversation, completion)
            if not is_correct:
                records.append({
                    "problem_idx": idx,
                    "sample_idx": sample_idx,
                    "question": conversation["messages"][0]["content"],
                    "completion": completion,
                })
    return records


def cluster_texts(texts, n_clusters):
    vectorizer = TfidfVectorizer(stop_words="english")
    tfidf = vectorizer.fit_transform(texts)
    kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init="auto")
    labels = kmeans.fit_predict(tfidf)
    coords = PCA(n_components=2).fit_transform(tfidf.toarray())
    return labels, coords


def main():
    parser = argparse.ArgumentParser(description="Cluster incorrect GSM8K outputs for a nanochat checkpoint")
    parser.add_argument("--source", choices=["sft", "rl"], default="rl", help="Which checkpoint family to load")
    parser.add_argument("--model-tag", type=str, default=None, help="Checkpoint tag to load")
    parser.add_argument("--model-step", type=int, default=None, help="Checkpoint step to load")
    parser.add_argument("--checkpoint-dir", type=str, default=None, help="Direct path to a checkpoint directory containing model_*.pt and meta_*.json")
    parser.add_argument("--device-type", choices=["cuda", "cpu", "mps"], default="", help="Device type (empty = autodetect)")
    parser.add_argument("--subset", choices=["main", "socratic"], default="main", help="GSM8K subset")
    parser.add_argument("--split", choices=["train", "test"], default="test", help="GSM8K split")
    parser.add_argument("--max-problems", type=int, default=100, help="Number of GSM8K problems to inspect")
    parser.add_argument("--num-samples", type=int, default=1, help="Number of sampled completions per problem")
    parser.add_argument("--max-new-tokens", type=int, default=256, help="Max completion length")
    parser.add_argument("--temperature", type=float, default=0.7, help="Sampling temperature")
    parser.add_argument("--top-k", type=int, default=50, help="Top-k sampling")
    parser.add_argument("--n-clusters", type=int, default=3, help="Number of KMeans clusters")
    parser.add_argument("--task-field", choices=["completion", "question"], default="completion", help="Text field to cluster")
    parser.add_argument("--output-prefix", type=str, default="gsm8k_error_clusters", help="Prefix for output files")
    args = parser.parse_args()

    device_type = autodetect_device_type() if args.device_type == "" else args.device_type
    ddp, ddp_rank, ddp_local_rank, ddp_world_size, device = compute_init(device_type)
    if ddp:
        raise RuntimeError("graph_script.py is intended for single-process use")

    if args.checkpoint_dir is not None:
        checkpoint_dir = Path(args.checkpoint_dir).resolve()
        checkpoint_step = args.model_step if args.model_step is not None else find_last_step(str(checkpoint_dir))
        model, tokenizer, meta = build_model(str(checkpoint_dir), checkpoint_step, device, phase="eval")
        source_label = str(checkpoint_dir)
    else:
        model, tokenizer, meta = load_model(args.source, device, phase="eval", model_tag=args.model_tag, step=args.model_step)
        source_label = args.source
    engine = Engine(model, tokenizer)
    task = GSM8K(subset=args.subset, split=args.split)
    num_problems = min(args.max_problems, len(task))
    print0(f"Collecting incorrect outputs from {source_label} over {num_problems} GSM8K problems")

    records = collect_incorrect_records(
        task,
        tokenizer,
        engine,
        num_problems=num_problems,
        num_samples=args.num_samples,
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_k=args.top_k,
    )
    if not records:
        raise RuntimeError("No incorrect records found. Increase max-problems or num-samples.")

    texts = [record[args.task_field] for record in records]
    labels, coords = cluster_texts(texts, args.n_clusters)
    for record, label in zip(records, labels):
        record["cluster"] = int(label)

    output_prefix = Path(args.output_prefix)
    jsonl_path = output_prefix.with_suffix(".jsonl")
    png_path = output_prefix.with_suffix(".png")

    with jsonl_path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    plt.figure(figsize=(8, 6))
    plt.scatter(coords[:, 0], coords[:, 1], c=labels, cmap="viridis", s=120, edgecolor="black")
    plt.title(f"K-Means Clusters of Incorrect GSM8K {args.task_field.title()}s")
    plt.xlabel("PCA Component 1")
    plt.ylabel("PCA Component 2")
    plt.grid(True, linestyle="--", alpha=0.6)
    plt.savefig(png_path, bbox_inches="tight")

    print(f"Saved incorrect records to {jsonl_path}")
    print(f"Saved cluster plot to {png_path}")
    print(f"Collected {len(records)} incorrect samples from {num_problems} problems")
    compute_cleanup()


if __name__ == "__main__":
    main()
