from __future__ import annotations

import re
from collections.abc import Callable

from ...deployment.launch_engine.plan import (
    MODEL_MOUNT,
    discovered_launch_settings,
    discovered_settings,
)
from .plan import GGUF_PLUGIN_PINS

IMAGE_PACKAGES = r"vllm|torch|nixl|nixl-rocm"
# vLLM loads a GGUF model file through this plugin.
GGUF_PLUGIN = "vllm-gguf-plugin"
DEFAULT_ARGS = ["--gpu-memory-utilization", "0.9", "--enforce-eager"]
# A GGUF file reads its Hugging Face configuration and tokenizer from the model directory.
GGUF_ARGS = (("--load-format", "gguf"), ("--hf-config-path", MODEL_MOUNT))
# A GGUF file holds the language model; this flag leaves out a multimodal model's encoders.
LANGUAGE_MODEL_ONLY = "--language-model-only"


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
    launch = discovered_launch_settings(setting)
    if observed.get("model_file"):
        if not observed["model_file"].endswith(".gguf"):
            raise ValueError("set NARWHAL_MODEL_PATH to a .gguf file")
        if not observed["packages"].get(GGUF_PLUGIN):
            raise ValueError(f"a GGUF model requires {GGUF_PLUGIN} in the engine image")
        for flag, value in GGUF_ARGS:
            if flag not in args:
                args.extend((flag, value))
        if observed.get("multimodal") and LANGUAGE_MODEL_ONLY not in args:
            args.append(LANGUAGE_MODEL_ONLY)
        launch.setdefault("tokenizer", MODEL_MOUNT)
        for name in GGUF_PLUGIN_PINS:
            pinned = setting(name.upper(), "")
            if not re.fullmatch(r"[0-9a-f]{64}", pinned):
                raise ValueError(
                    "set GGUF_PLUGIN_PYTHON_SHA256 and GGUF_PLUGIN_EXTENSION_SHA256 in .env "
                    "to the SHA-256 digests of the qualified GGUF plugin"
                )
            launch[name] = pinned
    return {
        "expected_packages": observed["packages"],
        "model_dtype": dtype,
        "kv_cache_dtype": "auto",
        "block_size": int(setting("BLOCK_SIZE", "128")),
        "context_length": context_length,
        "max_sequences": int(setting("MAX_SEQUENCES", "8")),
        **launch,
        "environment": environment,
        "extra_args": args,
    }


def sequence_limit(runtime: dict) -> int:
    if "max_sequences" not in runtime:
        raise ValueError("runtime.max_sequences is required")
    return int(runtime["max_sequences"])
