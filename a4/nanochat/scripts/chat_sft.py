"""
Supervised fine-tuning (SFT) the model.
Run as:

python -m scripts.chat_sft

Or torchrun for training:

torchrun --standalone --nproc_per_node=8 -m scripts.chat_sft -- --device-batch-size=16
"""

import gc
import argparse
import os
os.environ["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"
import time
import wandb
import torch
from contextlib import nullcontext
from nanochat.common import compute_init, compute_cleanup, print0, DummyWandb, get_base_dir, autodetect_device_type, get_peak_flops
from nanochat.tokenizer import get_token_bytes
from nanochat.checkpoint_manager import save_checkpoint, load_model, load_optimizer_state
from nanochat.loss_eval import evaluate_bpb
import torch.distributed as dist
from nanochat.flash_attention import HAS_FA3
from nanochat.engine import Engine
from scripts.chat_eval import run_chat_eval

from tasks.common import TaskMixture
from tasks.gsm8k import GSM8K
from tasks.mmlu import MMLU
from tasks.numinamath import NuminaMathCoT
from tasks.orca_math import OrcaMath
from tasks.smoltalk import SmolTalk
from tasks.customjson import CustomJSON
from tasks.spellingbee import SimpleSpelling, SpellingBee

# -----------------------------------------------------------------------------
# CLI arguments
parser = argparse.ArgumentParser(description="Supervised fine-tuning (SFT) the model")
# Logging
parser.add_argument("--run", type=str, default="dummy", help="wandb run name ('dummy' disables wandb logging)")
# Runtime
parser.add_argument("--device-type", type=str, default="", help="cuda|cpu|mps (empty = autodetect)")
# Model loading
parser.add_argument("--model-tag", type=str, default=None, help="model tag to load from")
parser.add_argument("--model-step", type=int, default=None, help="model step to load from")
parser.add_argument("--load-optimizer", type=int, default=1, help="warm-start optimizer from pretrained checkpoint (0=no, 1=yes)")
# Training horizon
parser.add_argument("--num-iterations", type=int, default=-1, help="number of optimization steps (-1 = full epoch)")
# Batch sizes (default: inherit from pretrained checkpoint)
parser.add_argument("--max-seq-len", type=int, default=None, help="max context length (default: inherit from pretrain)")
parser.add_argument("--device-batch-size", type=int, default=None, help="per-device batch size (default: inherit from pretrain)")
parser.add_argument("--total-batch-size", type=int, default=None, help="total batch size in tokens (default: inherit from pretrain)")
# Optimization (default: inherit from pretrained checkpoint)
parser.add_argument("--embedding-lr", type=float, default=None, help="learning rate for embedding parameters (Adam) (default: inherit from pretrain)")
parser.add_argument("--unembedding-lr", type=float, default=None, help="learning rate for unembedding parameters (Adam) (default: inherit from pretrain)")
parser.add_argument("--matrix-lr", type=float, default=None, help="learning rate for matrix parameters (Muon) (default: inherit from pretrain)")
parser.add_argument("--init-lr-frac", type=float, default=0.8, help="initial LR as fraction of base LR")
parser.add_argument("--warmup-ratio", type=float, default=0.0, help="ratio of iterations for LR warmup")
parser.add_argument("--warmdown-ratio", type=float, default=0.5, help="ratio of iterations for LR warmdown")
parser.add_argument("--final-lr-frac", type=float, default=0.0, help="final LR as fraction of initial LR")
# Evaluation
parser.add_argument("--eval-every", type=int, default=200, help="evaluate val bpb every N steps (-1 = disable)")
parser.add_argument("--eval-tokens", type=int, default=40*524288, help="number of tokens to evaluate val loss on")
parser.add_argument("--chatcore-every", type=int, default=200, help="evaluate ChatCORE metric every N steps (-1 = disable)")
parser.add_argument("--chatcore-max-cat", type=int, default=-1, help="max problems per categorical task for ChatCORE")
parser.add_argument("--chatcore-max-sample", type=int, default=24, help="max problems per generative task for ChatCORE")
# Data mixture
parser.add_argument("--mmlu-epochs", type=int, default=3, help="number of epochs of MMLU in training mixture (teaches Multiple Choice)")
parser.add_argument("--gsm8k-epochs", type=int, default=4, help="number of epochs of GSM8K in training mixture (teaches Math and Tool Use)")
parser.add_argument("--smoltalk-train-rows", type=int, default=-1, help="number of SmolTalk train rows to include (-1 = full split)")
parser.add_argument("--mmlu-train-rows", type=int, default=-1, help="number of MMLU train rows to include in total (-1 = use --mmlu-epochs)")
parser.add_argument("--gsm8k-train-rows", type=int, default=-1, help="number of GSM8K train rows to include in total (-1 = use --gsm8k-epochs)")
parser.add_argument("--simple-spelling-train-rows", type=int, default=200000, help="number of SimpleSpelling train rows to include")
parser.add_argument("--spellingbee-train-rows", type=int, default=80000, help="number of SpellingBee train rows to include")
parser.add_argument("--identity-train-copies", type=int, default=2, help="how many copies of the identity conversations to include")
parser.add_argument("--orca-math-train-rows", type=int, default=0, help="number of Orca-Math train rows to include")
parser.add_argument("--numinamath-train-rows", type=int, default=0, help="number of NuminaMath train rows to include")
parser.add_argument("--smoltalk-val-rows", type=int, default=-1, help="number of SmolTalk validation rows to include (-1 = full test split)")
parser.add_argument("--mmlu-val-rows", type=int, default=5200, help="number of MMLU validation rows to include")
parser.add_argument("--gsm8k-val-rows", type=int, default=420, help="number of GSM8K validation rows to include")
parser.add_argument("--orca-math-val-rows", type=int, default=0, help="number of Orca-Math validation rows to include")
parser.add_argument("--numinamath-val-rows", type=int, default=0, help="number of NuminaMath validation rows to include")
args = parser.parse_args()
user_config = vars(args).copy()
# -----------------------------------------------------------------------------

# Compute init
device_type = autodetect_device_type() if args.device_type == "" else args.device_type
ddp, ddp_rank, ddp_local_rank, ddp_world_size, device = compute_init(device_type)
master_process = ddp_rank == 0
autocast_ctx = torch.amp.autocast(device_type=device_type, dtype=torch.bfloat16) if device_type == "cuda" else nullcontext()
synchronize = torch.cuda.synchronize if device_type == "cuda" else lambda: None
get_max_memory = torch.cuda.max_memory_allocated if device_type == "cuda" else lambda: 0
if device_type == "cuda":
    gpu_device_name = torch.cuda.get_device_name(0)
    gpu_peak_flops = get_peak_flops(gpu_device_name)
    print0(f"GPU: {gpu_device_name} | Peak FLOPS (BF16): {gpu_peak_flops:.2e}")
else:
    gpu_peak_flops = float('inf')  # MFU not meaningful for CPU/MPS

# wandb logging init
use_dummy_wandb = args.run == "dummy" or not master_process
wandb_run = DummyWandb() if use_dummy_wandb else wandb.init(project="nanochat-sft", name=args.run, config=user_config)

# Flash Attention status
if not HAS_FA3:
    print0("WARNING: Flash Attention 3 not available, using PyTorch SDPA fallback. Training will be less efficient.")

# Load the model and tokenizer
model, tokenizer, meta = load_model("base", device, phase="train", model_tag=args.model_tag, step=args.model_step)

# Inherit training hyperparameters from pretrained checkpoint (None = inherit, explicit value = override)
pretrain_user_config = meta.get("user_config", {})
for name, fallback, source in [
    ("max_seq_len",       2048,  meta),
    ("device_batch_size", 32,    meta),
    ("total_batch_size",  524288, meta),
    ("embedding_lr",      0.3,   pretrain_user_config),
    ("unembedding_lr",    0.004, pretrain_user_config),
    ("matrix_lr",         0.02,  pretrain_user_config),
]:
    arg_val = getattr(args, name)
    pretrain_val = source.get(name)
    if arg_val is None:
        resolved = pretrain_val if pretrain_val is not None else fallback
        setattr(args, name, resolved)
        print0(f"Inherited {name}={resolved} from pretrained checkpoint")
    elif pretrain_val is not None and arg_val != pretrain_val:
        print0(f"NOTE: --{name.replace('_', '-')}={arg_val} overrides pretrained value of {pretrain_val}")
    else:
        print0(f"Using {name}={arg_val}")

orig_model = model
model = torch.compile(model, dynamic=False)
depth = model.config.n_layer
num_flops_per_token = model.estimate_flops()
tokens_per_fwdbwd = args.device_batch_size * args.max_seq_len # tokens per iteration for a single rank
world_tokens_per_fwdbwd = tokens_per_fwdbwd * ddp_world_size # total tokens per iteration for all ranks
assert args.total_batch_size % world_tokens_per_fwdbwd == 0
grad_accum_steps = args.total_batch_size // world_tokens_per_fwdbwd
print0(f"Tokens / micro-batch / rank: {args.device_batch_size} x {args.max_seq_len} = {tokens_per_fwdbwd:,}")
print0(f"Tokens / micro-batch: {world_tokens_per_fwdbwd:,}")
print0(f"Total batch size {args.total_batch_size:,} => gradient accumulation steps: {grad_accum_steps}")
token_bytes = get_token_bytes(device=device)

# Initialize the Optimizer (combined MuonAdamW: Muon for matrix params, AdamW for rest)
# Note that pretraining ramps weight_decay to zero by end of pretraining, so SFT continues with zero
optimizer = model.setup_optimizer(unembedding_lr=args.unembedding_lr, embedding_lr=args.embedding_lr, matrix_lr=args.matrix_lr, weight_decay=0.0)

# Optionally warm-start optimizer from pretrained checkpoint (momentum buffers etc.)
# Note: load_state_dict overwrites param_group metadata (LRs, betas, etc.) with the
# pretrained values. Since pretraining warmdown brings LRs to ~0, we must save and
# restore our fresh SFT LRs after loading.
base_dir = get_base_dir()
if args.load_optimizer:
    optimizer_data = load_optimizer_state("base", device, rank=ddp_rank, model_tag=args.model_tag, step=args.model_step)
    if optimizer_data is not None:
        base_lrs = [group["lr"] for group in optimizer.param_groups]
        optimizer.load_state_dict(optimizer_data)
        del optimizer_data
        for group, base_lr in zip(optimizer.param_groups, base_lrs):
            group["lr"] = base_lr
        print0("Loaded optimizer state from pretrained checkpoint (momentum buffers only, LRs reset)")
    else:
        print0("WARNING: optimizer checkpoint not found, starting with fresh optimizer (slightly worse)")

# Override the initial learning rate as a fraction of the base learning rate
for group in optimizer.param_groups:
    group["lr"] = group["lr"] * args.init_lr_frac
    group["initial_lr"] = group["lr"]

# SFT data mixture and DataLoader
def maybe_slice(task_ctor, rows):
    """Build zero or one task instance depending on the requested row budget."""
    if rows == 0:
        return []
    if rows < 0:
        return [task_ctor()]
    return [task_ctor(stop=rows)]

def repeat_to_rows(task_ctor, rows):
    """Repeat/slice a task until we hit the requested total number of rows."""
    if rows == 0:
        return []
    first_task = task_ctor()
    full_len = len(first_task)
    if rows < 0:
        return [first_task]
    tasks = []
    remaining = rows
    use_first = True
    while remaining > 0:
        if remaining >= full_len:
            tasks.append(first_task if use_first else task_ctor())
            remaining -= full_len
        else:
            tasks.append(task_ctor(stop=remaining))
            remaining = 0
        use_first = False
    return tasks

def grouped_counts(tasks):
    counts = {}
    for task in tasks:
        name = type(task).__name__
        counts[name] = counts.get(name, 0) + len(task)
    return counts

identity_conversations_filepath = os.path.join(base_dir, "identity_conversations.jsonl")
resolved_mmlu_rows = args.mmlu_train_rows if args.mmlu_train_rows >= 0 else args.mmlu_epochs * len(MMLU(subset="auxiliary_train", split="train"))
resolved_gsm8k_rows = args.gsm8k_train_rows if args.gsm8k_train_rows >= 0 else args.gsm8k_epochs * len(GSM8K(subset="main", split="train"))
train_tasks = [
    *maybe_slice(lambda **kwargs: SmolTalk(split="train", **kwargs), args.smoltalk_train_rows),
    *[CustomJSON(filepath=identity_conversations_filepath) for _ in range(args.identity_train_copies)],
    *repeat_to_rows(lambda **kwargs: MMLU(subset="auxiliary_train", split="train", **kwargs), resolved_mmlu_rows),
    *repeat_to_rows(lambda **kwargs: GSM8K(subset="main", split="train", **kwargs), resolved_gsm8k_rows),
    *maybe_slice(lambda **kwargs: OrcaMath(split="train", **kwargs), args.orca_math_train_rows),
    *maybe_slice(lambda **kwargs: NuminaMathCoT(split="train", **kwargs), args.numinamath_train_rows),
    SimpleSpelling(size=args.simple_spelling_train_rows, split="train"),
    SpellingBee(size=args.spellingbee_train_rows, split="train"),
]
train_dataset = TaskMixture(train_tasks)
train_counts = grouped_counts(train_tasks)
print0(f"Training mixture: {len(train_dataset):,} rows")
print0("Train rows by task: " + ", ".join(f"{name}={count:,}" for name, count in sorted(train_counts.items())))
val_dataset = TaskMixture([
    *maybe_slice(lambda **kwargs: SmolTalk(split="test", **kwargs), args.smoltalk_val_rows),
    *maybe_slice(lambda **kwargs: MMLU(subset="all", split="test", **kwargs), args.mmlu_val_rows),
    *maybe_slice(lambda **kwargs: GSM8K(subset="main", split="test", **kwargs), args.gsm8k_val_rows),
    *maybe_slice(lambda **kwargs: OrcaMath(split="test", **kwargs), args.orca_math_val_rows),
    *maybe_slice(lambda **kwargs: NuminaMathCoT(split="test", **kwargs), args.numinamath_val_rows),
])
val_counts = grouped_counts(val_dataset.tasks)
print0(f"Validation mixture: {len(val_dataset):,} rows")
print0("Val rows by task: " + ", ".join(f"{name}={count:,}" for name, count in sorted(val_counts.items())))
# DataLoader is defined here, it emits inputs, targets : 2D tensors of shape (device_batch_size, max_seq_len)
# A big problem is that we don't know the final num_iterations in advance. So we create
# these two global variables and update them from within the data generator.
last_step = False # we will toggle this to True when we reach the end of the training dataset
approx_progress = 0.0 # will go from 0 to 1 over the course of the epoch
current_epoch = 1 # track epoch for logging
def sft_data_generator_bos_bestfit(split, buffer_size=100):
    """
    BOS-aligned dataloader for SFT with bestfit-pad packing.

    Each row in the batch starts with BOS (beginning of a conversation).
    Conversations are packed using best-fit algorithm. When no conversation fits,
    the row is padded (instead of cropping) to ensure no tokens are ever discarded.
    Padding positions have targets masked with -1 (ignore_index for cross-entropy).
    """
    global last_step, approx_progress, current_epoch
    assert split in {"train", "val"}, "split must be 'train' or 'val'"
    dataset = train_dataset if split == "train" else val_dataset
    dataset_size = len(dataset)
    assert dataset_size > 0
    row_capacity = args.max_seq_len + 1  # +1 for target at last position
    bos_token = tokenizer.get_bos_token_id()

    # Conversation buffer: list of token lists
    conv_buffer = []
    cursor = ddp_rank  # Each rank processes different conversations (for fetching)
    consumed = ddp_rank  # Track actual consumption separately from buffering
    epoch = 1
    it = 0  # iteration counter

    def refill_buffer():
        nonlocal cursor, epoch
        while len(conv_buffer) < buffer_size:
            conversation = dataset[cursor]
            ids, _ = tokenizer.render_conversation(conversation)
            conv_buffer.append(ids)
            cursor += ddp_world_size
            if cursor >= dataset_size:
                cursor = cursor % dataset_size
                epoch += 1
                # Note: last_step is now triggered based on consumption, not fetching

    while True:
        rows = []
        row_lengths = []  # Track actual content length (excluding padding) for each row
        for _ in range(args.device_batch_size):
            row = []
            padded = False
            while len(row) < row_capacity:
                # Ensure buffer has conversations
                while len(conv_buffer) < buffer_size:
                    refill_buffer()

                remaining = row_capacity - len(row)

                # Find largest conversation that fits entirely
                best_idx = -1
                best_len = 0
                for i, conv in enumerate(conv_buffer):
                    conv_len = len(conv)
                    if conv_len <= remaining and conv_len > best_len:
                        best_idx = i
                        best_len = conv_len

                if best_idx >= 0:
                    # Found a conversation that fits - use it entirely
                    conv = conv_buffer.pop(best_idx)
                    row.extend(conv)
                    consumed += ddp_world_size  # Track actual consumption
                else:
                    # No conversation fits - pad the remainder instead of cropping
                    # This ensures we never discard any tokens
                    content_len = len(row)
                    row.extend([bos_token] * remaining)  # Pad with BOS tokens
                    padded = True
                    break  # Row is now full (with padding)

            # Track content length: full row if no padding, otherwise the length before padding
            if padded:
                row_lengths.append(content_len)
            else:
                row_lengths.append(row_capacity)
            rows.append(row[:row_capacity])

        # Stopping condition to respect num_iterations, if given
        it += 1
        if 0 < args.num_iterations <= it and split == "train":
            last_step = True

        # Update progress tracking (based on consumed, not cursor, to account for buffering)
        if split == "train":
            current_epoch = epoch
            if args.num_iterations > 0:
                approx_progress = it / args.num_iterations
            else:
                approx_progress = consumed / dataset_size
            # Trigger last_step when we've consumed enough (instead of when cursor wraps)
            if consumed >= dataset_size:
                last_step = True

        # Build tensors
        use_cuda = device_type == "cuda"
        batch_tensor = torch.tensor(rows, dtype=torch.long, pin_memory=use_cuda)
        inputs = batch_tensor[:, :-1].to(device=device, dtype=torch.int32, non_blocking=use_cuda)
        targets = batch_tensor[:, 1:].to(device=device, dtype=torch.int64, non_blocking=use_cuda)

        # Mask out padding positions in targets (set to -1 = ignore_index)
        # For each row, positions >= (content_length - 1) in targets should be masked
        for i, content_len in enumerate(row_lengths):
            if content_len < row_capacity:
                targets[i, content_len-1:] = -1

        yield inputs, targets

train_loader = sft_data_generator_bos_bestfit("train")
build_val_loader = lambda: sft_data_generator_bos_bestfit("val")
progress = 0 # will go from 0 to 1 over the course of the epoch

# Learning rate schedule (linear warmup, constant, linear warmdown)
# Same shape as base_train but uses progress (0→1) instead of absolute step counts,
# because SFT doesn't always know num_iterations in advance (dataset-driven stopping).
def get_lr_multiplier(progress):
    if progress < args.warmup_ratio:
        return (progress + 1e-8) / args.warmup_ratio
    elif progress <= 1.0 - args.warmdown_ratio:
        return 1.0
    else:
        decay = (progress - (1.0 - args.warmdown_ratio)) / args.warmdown_ratio
        return (1 - decay) * 1.0 + decay * args.final_lr_frac

# Momentum scheduler for Muon optimizer
def get_muon_momentum(it):
    frac = min(it / 300, 1)
    momentum = (1 - frac) * 0.85 + frac * 0.95
    return momentum

# -----------------------------------------------------------------------------
# Training loop
x, y = next(train_loader) # prefetch the very first batch of data
min_val_bpb = float("inf")
smooth_train_loss = 0 # EMA of training loss
ema_beta = 0.9 # EMA decay factor
total_training_time = 0 # total wall-clock time of training
step = 0
while True:
    flops_so_far = num_flops_per_token * args.total_batch_size * step

    # Synchronize last_step across all ranks to avoid hangs in the distributed setting
    if ddp:
        last_step_tensor = torch.tensor(last_step, dtype=torch.int32, device=device)
        dist.all_reduce(last_step_tensor, op=dist.ReduceOp.MAX)
        last_step = bool(last_step_tensor.item())

    # once in a while: evaluate the val bpb (all ranks participate)
    if last_step or (args.eval_every > 0 and step % args.eval_every == 0):
        model.eval()
        val_loader = build_val_loader()
        eval_steps = args.eval_tokens // (args.device_batch_size * args.max_seq_len * ddp_world_size)
        with autocast_ctx:
            val_bpb = evaluate_bpb(model, val_loader, eval_steps, token_bytes)
        print0(f"Step {step:05d} | Validation bpb: {val_bpb:.4f}")
        if val_bpb < min_val_bpb:
            min_val_bpb = val_bpb
        wandb_run.log({
            "step": step,
            "total_training_flops": flops_so_far,
            "total_training_time": total_training_time,
            "val/bpb": val_bpb,
        })
        model.train()

    # once in a while: estimate the ChatCORE metric (all ranks participate)
    # use the original uncompiled model because the inputs keep changing shape
    chatcore_results = {}
    if args.chatcore_every > 0 and (last_step or (step > 0 and step % args.chatcore_every == 0)):
        model.eval()
        engine = Engine(orig_model, tokenizer)
        all_tasks = ['ARC-Easy', 'ARC-Challenge', 'MMLU', 'GSM8K', 'HumanEval', 'SpellingBee']
        categorical_tasks = {'ARC-Easy', 'ARC-Challenge', 'MMLU'}
        baseline_accuracies = {
            'ARC-Easy': 0.25, 'ARC-Challenge': 0.25, 'MMLU': 0.25,
            'GSM8K': 0.0, 'HumanEval': 0.0, 'SpellingBee': 0.0,
        }
        task_results = {}
        for task_name in all_tasks:
            limit = args.chatcore_max_cat if task_name in categorical_tasks else args.chatcore_max_sample
            max_problems = None if limit < 0 else limit  # -1 means no limit
            with autocast_ctx:
                acc = run_chat_eval(task_name, orig_model, tokenizer, engine,
                                    batch_size=args.device_batch_size, max_problems=max_problems)
            task_results[task_name] = acc
            print0(f"  {task_name}: {100*acc:.2f}%")
        # Compute ChatCORE metrics (mean centered accuracy, ranges from 0=random to 1=perfect)
        def centered_mean(tasks):
            return sum((task_results[t] - baseline_accuracies[t]) / (1.0 - baseline_accuracies[t]) for t in tasks) / len(tasks)
        chatcore = centered_mean(all_tasks)
        chatcore_cat = centered_mean(categorical_tasks)
        print0(f"Step {step:05d} | ChatCORE: {chatcore:.4f} | ChatCORE_cat: {chatcore_cat:.4f}")
        wandb_run.log({
            "step": step,
            "total_training_flops": flops_so_far,
            "chatcore_metric": chatcore,
            "chatcore_cat": chatcore_cat,
            **{f"chatcore/{task_name}": acc for task_name, acc in task_results.items()},
        })
        model.train()

    # save checkpoint at the end of the run (all ranks participate so each saves its optimizer shard)
    if last_step:
        output_dirname = args.model_tag if args.model_tag else f"d{depth}" # e.g. d12
        checkpoint_dir = os.path.join(base_dir, "chatsft_checkpoints", output_dirname)
        save_checkpoint(
            checkpoint_dir,
            step,
            orig_model.state_dict(),
            optimizer.state_dict(),
            {
                "step": step,
                "val_bpb": val_bpb, # loss at last step
                "model_config": {
                    "sequence_len": args.max_seq_len,
                    "vocab_size": tokenizer.get_vocab_size(),
                    "n_layer": depth,
                    "n_head": model.config.n_head,
                    "n_kv_head": model.config.n_kv_head,
                    "n_embd": model.config.n_embd,
                    "window_pattern": model.config.window_pattern,
                },
                "user_config": user_config, # inputs to the training script
            },
            rank=ddp_rank,
        )

    if last_step:
        break

    # -------------------------------------------------------------------------
    # single training step
    # evaluate the gradient
    synchronize()
    t0 = time.time()
    for micro_step in range(grad_accum_steps):
        with autocast_ctx:
            loss = model(x, y)
        train_loss = loss.detach() # for logging
        loss = loss / grad_accum_steps # each .backward() is a grad sum => normalize loss here
        loss.backward()
        x, y = next(train_loader) # prefetch the next batch while the GPU is busy with forward/backward
        progress = max(progress, approx_progress) # only increase progress monotonically
    # step the optimizer
    lrm = get_lr_multiplier(progress)
    muon_momentum = get_muon_momentum(step)
    for group in optimizer.param_groups:
        group["lr"] = group["initial_lr"] * lrm
        if group['kind'] == 'muon':
            group["momentum"] = muon_momentum
    optimizer.step()
    model.zero_grad(set_to_none=True)
    synchronize()
    t1 = time.time()
    dt = t1 - t0
    # -------------------------------------------------------------------------

    # State
    step += 1

    # logging
    smooth_train_loss = ema_beta * smooth_train_loss + (1 - ema_beta) * train_loss.item() # EMA the training loss
    debiased_smooth_loss = smooth_train_loss / (1 - ema_beta**(step + 1)) # debias the EMA
    pct_done = 100 * progress
    tok_per_sec = int(args.total_batch_size / dt)
    flops_per_sec = num_flops_per_token * args.total_batch_size / dt
    mfu = 100 * flops_per_sec / (gpu_peak_flops * ddp_world_size)
    if step > 10:
        total_training_time += dt # only count the time after the first 10 steps
    print0(f"step {step:05d} ({pct_done:.2f}%) | loss: {debiased_smooth_loss:.6f} | lrm: {lrm:.2f} | dt: {dt * 1000:.2f}ms | tok/sec: {tok_per_sec:,} | mfu: {mfu:.2f} | epoch: {current_epoch} | total time: {total_training_time/60:.2f}m")
    if step % 10 == 0:
        wandb_run.log({
            "step": step,
            "total_training_flops": flops_so_far,
            "total_training_time": total_training_time,
            "train/loss": debiased_smooth_loss,
            "train/lrm": lrm,
            "train/dt": dt,
            "train/tok_per_sec": tok_per_sec,
            "train/mfu": mfu,
            "train/epoch": current_epoch,
        })

    # The garbage collector spends ~500ms scanning for cycles quite frequently.
    # We manually manage it to avoid these pauses during training.
    if step == 1:
        gc.collect() # manually collect a lot of garbage from setup
        gc.freeze() # freeze all currently surviving objects and exclude them from GC
        gc.disable() # disable GC entirely except:
    elif step % 5000 == 0: # every 5000 steps...
        gc.collect() # manually collect, just to be safe for very long runs

# print a few more stats
print0(f"Peak memory usage: {get_max_memory() / 1024 / 1024:.2f}MiB")
print0(f"Total training time: {total_training_time/60:.2f}m")
print0(f"Minimum validation bpb: {min_val_bpb:.4f}")

# Log to report
from nanochat.report import get_report
get_report().log(section="SFT", data=[
    user_config, # CLI args
    { # stats about the training setup
        "Number of iterations": step,
        "DDP world size": ddp_world_size,
    },
    { # stats about training outcomes
        "Minimum validation bpb": min_val_bpb,
    }
])

# cleanup
wandb_run.finish() # wandb run finish
compute_cleanup()