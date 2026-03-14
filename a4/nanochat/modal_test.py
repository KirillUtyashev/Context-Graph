from pathlib import Path
import subprocess

import modal

APP_NAME = "nanochat-rl-smoke"
REMOTE_REPO = "/root/nanochat"
REMOTE_CACHE = "/root/.cache/nanochat"

app = modal.App(APP_NAME)
cache_volume = modal.Volume.from_name("nanochat-cache", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("git")
    .pip_install("uv")
    .add_local_dir(Path(__file__).resolve().parent, remote_path=REMOTE_REPO)
)

@app.function(
    image=image,
    gpu="T4",
    timeout=20 * 60,
    volumes={REMOTE_CACHE: cache_volume},
)
def run_rl_smoke_test(
    reward_system: str = "format",
    model_tag: str | None = None,
    model_step: int | None = None,
):
    """
    Tiny bounded RL run for validating the RL code path on Modal.

    Prereqs in the mounted cache volume:
    - /root/.cache/nanochat/tokenizer/tokenizer.pkl
    - /root/.cache/nanochat/chatsft_checkpoints/<model_tag>/...
    """
    cmd = [
        "uv",
        "run",
        "--extra",
        "gpu",
        "python",
        "-m",
        "scripts.chat_rl",
        "--",
        "--run=dummy",
        "--reward-system",
        reward_system,
        "--num-epochs=1",
        "--examples-per-step=1",
        "--num-samples=2",
        "--device-batch-size=1",
        "--max-new-tokens=24",
        "--eval-every=999999",
        "--save-every=999999",
        "--eval-examples=8",
    ]
    if model_tag is not None:
        cmd.extend(["--model-tag", model_tag])
    if model_step is not None:
        cmd.extend(["--model-step", str(model_step)])

    result = subprocess.run(
        cmd,
        cwd=REMOTE_REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


@app.local_entrypoint()
def main(
    reward_system: str = "format",
    model_tag: str = "",
    model_step: int = -1,
):
    result = run_rl_smoke_test.remote(
        reward_system=reward_system,
        model_tag=model_tag or None,
        model_step=None if model_step < 0 else model_step,
    )
    print("returncode:", result["returncode"])
    if result["stdout"]:
        print("\n=== stdout ===")
        print(result["stdout"])
    if result["stderr"]:
        print("\n=== stderr ===")
        print(result["stderr"])
