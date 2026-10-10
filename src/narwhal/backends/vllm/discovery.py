from __future__ import annotations

import json
from collections.abc import Callable

IMAGE_PACKAGES = r"vllm|torch|nixl|nixl-rocm"


def runtime(observed: dict, setting: Callable[[str, str], str]) -> dict:
    dtype = setting("MODEL_DTYPE", observed.get("model_dtype") or "bfloat16")
    if dtype not in {"bfloat16", "float16"}:
        raise ValueError("set MODEL_DTYPE to bfloat16 or float16 in .env")
    max_len = min(int(observed.get("max_model_len") or 16384), 16384)
    args = json.loads(
        setting(
            "ENGINE_ARGS",
            json.dumps(
                [
                    "--max-model-len",
                    str(max_len),
                    "--max-num-seqs",
                    "8",
                    "--gpu-memory-utilization",
                    "0.9",
                    "--enforce-eager",
                ]
            ),
        )
    )
    if not isinstance(args, list):
        raise ValueError("ENGINE_ARGS must be a JSON array")
    if observed.get("requires_trust_remote_code") and "--trust-remote-code" not in args:
        args.append("--trust-remote-code")
    environment = dict(observed["image_environment"])
    overrides = json.loads(setting("ENGINE_ENV", "{}"))
    if not isinstance(overrides, dict):
        raise ValueError("ENGINE_ENV must be a JSON object")
    environment.update(overrides)
    if observed.get("requires_ds_conv_state_layout"):
        if (
            "VLLM_SSM_CONV_STATE_LAYOUT" in overrides
            and overrides["VLLM_SSM_CONV_STATE_LAYOUT"] != "DS"
        ):
            raise ValueError("convolutional SSM transfer requires VLLM_SSM_CONV_STATE_LAYOUT=DS")
        environment["VLLM_SSM_CONV_STATE_LAYOUT"] = "DS"
    return {
        "expected_packages": observed["packages"],
        "model_dtype": dtype,
        "kv_cache_dtype": "auto",
        "block_size": int(setting("BLOCK_SIZE", "128")),
        "environment": environment,
        "extra_args": args,
    }


def sequence_limit(args: list[str]) -> int:
    values = []
    for index, arg in enumerate(args):
        if arg == "--max-num-seqs" and index + 1 < len(args):
            values.append(args[index + 1])
        elif isinstance(arg, str) and arg.startswith("--max-num-seqs="):
            values.append(arg.partition("=")[2])
    if len(values) != 1 or not str(values[0]).isdigit() or int(values[0]) < 1:
        raise ValueError("runtime.extra_args needs one positive --max-num-seqs")
    return int(values[0])
