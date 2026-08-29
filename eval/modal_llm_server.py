"""
Self-hosted OpenAI-compatible LLM endpoint on Modal, for the eval harness.

Free API tiers (OpenRouter, Groq) cap out at a few hundred tokens/day per
account — nowhere near the ~6M tokens a full-scale eval needs. Modal bills by
GPU-second instead of a daily token quota, so this sidesteps the ceiling
entirely. Points LLM_BASE_URL at this and nothing else in the app changes —
PolicyAgent already just talks to any OpenAI-compatible endpoint.

The URL Modal prints is public — anyone who has it can run inference on your
GPU. vLLM's --api-key gates it with a bearer token, sourced from a Modal
secret so the key itself is never in this file or in source control:

    modal secret create llm-eval-api-key LLM_EVAL_API_KEY=<some-random-string>
    modal deploy eval/modal_llm_server.py

Then set (in .env, not committed):
    LLM_PROVIDER=openai
    LLM_BASE_URL=<the printed https://...modal.run URL>/v1
    LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
    OPENAI_API_KEY=<the same LLM_EVAL_API_KEY value>
"""

import modal

MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"

app = modal.App("payment-recovery-llm-eval")

vllm_image = (
    # debian_slim lacks nvcc — some of vLLM's kernels JIT-compile against it at
    # startup ("Could not find nvcc and default cuda_home ... doesn't exist").
    # The CUDA devel base ships the full toolkit on PATH.
    modal.Image.from_registry("nvidia/cuda:12.4.0-devel-ubuntu22.04", add_python="3.11")
    .pip_install("vllm", "huggingface_hub[hf_transfer]")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "VLLM_USE_V1": "1"})
)

model_cache = modal.Volume.from_name("llm-eval-model-cache", create_if_missing=True)


@app.function(
    image=vllm_image,
    gpu="A10G",
    timeout=30 * 60,
    scaledown_window=5 * 60,
    # Without @modal.concurrent, each in-flight HTTP request looks like a
    # separate container's-worth of load to the autoscaler — a burst of 20
    # concurrent eval calls spun up 10 separate $-per-hour GPU containers,
    # each redundantly loading the model, instead of vLLM's own batching
    # serving all 20 on the one container that was already warm. max_inputs
    # tells Modal this container handles many concurrent requests itself;
    # max_containers=1 is the hard backstop against ever scaling out to a
    # second GPU no matter how bursty the caller gets.
    max_containers=1,
    volumes={"/root/.cache/huggingface": model_cache},
    secrets=[modal.Secret.from_name("llm-eval-api-key")],
)
@modal.concurrent(max_inputs=32)
@modal.web_server(port=8000, startup_timeout=10 * 60)
def serve():
    import os
    import subprocess

    subprocess.Popen(
        [
            "python",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            MODEL_NAME,
            "--port",
            "8000",
            "--host",
            "0.0.0.0",
            "--max-model-len",
            "8192",
            "--api-key",
            os.environ["LLM_EVAL_API_KEY"],
        ]
    )
