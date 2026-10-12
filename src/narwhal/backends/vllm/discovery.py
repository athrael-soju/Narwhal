from __future__ import annotations

from collections.abc import Callable

from ...deployment.launch_engine.plan import discovered_settings

IMAGE_PACKAGES = r"vllm|torch|nixl|nixl-rocm"


def runtime(observed: dict, setting: Callable[[str, str], str]) -> dict:
    dtype, args, environment, overrides = discovered_settings(
        observed,
        setting,
        lambda max_len: [
            "--max-model-len",
            str(max_len),
            "--max-num-seqs",
            "8",
            "--gpu-memory-utilization",
            "0.9",
            "--enforce-eager",
        ],
    )
    if observed.get("requires_trust_remote_code") and "--trust-remote-code" not in args:
        args.append("--trust-remote-code")
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
