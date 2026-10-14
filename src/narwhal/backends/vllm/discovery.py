from __future__ import annotations

from collections.abc import Callable

from ...deployment.launch_engine.plan import discovered_launch_settings, discovered_settings

IMAGE_PACKAGES = r"vllm|torch|nixl|nixl-rocm"
DEFAULT_ARGS = ["--gpu-memory-utilization", "0.9", "--enforce-eager"]


def runtime(observed: dict, setting: Callable[[str, str], str]) -> dict:
    dtype, context_length, args, environment, overrides = discovered_settings(
        observed, setting, DEFAULT_ARGS
    )
    if observed.get("requires_trust_remote_code") and "--trust-remote-code" not in args:
        args.append("--trust-remote-code")
    if observed.get("conv_state_layers"):
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
        "context_length": context_length,
        "max_sequences": int(setting("MAX_SEQUENCES", "8")),
        **discovered_launch_settings(setting),
        "environment": environment,
        "extra_args": args,
    }


def sequence_limit(runtime: dict) -> int:
    if "max_sequences" not in runtime:
        raise ValueError("runtime.max_sequences is required")
    return int(runtime["max_sequences"])
