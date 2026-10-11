from __future__ import annotations

import json
from collections.abc import Callable

from .plan import MAX_RUNNING_REQUESTS, ROLE_SWITCH_CONNECTORS, option

IMAGE_PACKAGES = r"sglang|torch|nixl(-cu(da)?[0-9]+)?|mooncake-transfer-engine(-cu(da)?[0-9]+)?"


def runtime(observed: dict, setting: Callable[[str, str], str], connector: str, role: str) -> dict:
    dtype = setting("MODEL_DTYPE", observed.get("model_dtype") or "bfloat16")
    if dtype not in {"bfloat16", "float16"}:
        raise ValueError("set MODEL_DTYPE to bfloat16 or float16 in .env")
    max_len = min(int(observed.get("max_model_len") or 16384), 16384)
    args = json.loads(setting("ENGINE_ARGS", json.dumps(["--context-length", str(max_len)])))
    if not isinstance(args, list):
        raise ValueError("ENGINE_ARGS must be a JSON array")
    if observed.get("requires_trust_remote_code") and "--trust-remote-code" not in args:
        args.append("--trust-remote-code")
    if (
        connector in ROLE_SWITCH_CONNECTORS
        and observed.get("requires_ds_conv_state_layout")
        and "--disable-radix-cache" not in args
        and "--mamba-radix-cache-strategy" not in args
    ):
        # A switched decode engine caches finished requests' SSM states. With the extra buffer a
        # request needs three state slots, SGLang's decode admission evicts a cached state only
        # when none is free, and the engine exits under load.
        args += ["--mamba-radix-cache-strategy", "no_buffer"]
        if "--disable-overlap-schedule" not in args:
            args.append("--disable-overlap-schedule")
    environment = dict(observed["image_environment"])
    overrides = json.loads(setting("ENGINE_ENV", "{}"))
    if not isinstance(overrides, dict):
        raise ValueError("ENGINE_ENV must be a JSON object")
    environment.update(overrides)
    record = {
        "backend": "sglang",
        "connector": connector,
        "expected_packages": observed["packages"],
        "model_dtype": dtype,
        "kv_cache_dtype": "auto",
    }
    if connector in ROLE_SWITCH_CONNECTORS:
        memory = setting("DECODE_CUDA_GRAPH_MEMORY_GB", "")
        if not memory:
            raise ValueError(f"set DECODE_CUDA_GRAPH_MEMORY_GB in .env for {connector} engines")
        record["decode_cuda_graph_memory_gb"] = float(memory)
    else:
        record["role"] = role
    return {**record, "environment": environment, "extra_args": args}


def sequence_limit(args: list[str]) -> int:
    cap = option(args, "--max-running-requests")
    return int(cap) if cap is not None and cap.isdigit() else MAX_RUNNING_REQUESTS
