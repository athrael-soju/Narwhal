from __future__ import annotations

import re
from collections.abc import Callable

from ...deployment.launch_engine.plan import discovered_settings, option
from .plan import CUDA_BUILD_SUFFIX, MAX_RUNNING_REQUESTS, TRANSFER_PACKAGES

IMAGE_PACKAGES = "sglang|torch|" + "|".join(
    f"{re.escape(name)}(?:{CUDA_BUILD_SUFFIX})?" for name in TRANSFER_PACKAGES.values()
)


def runtime(observed: dict, setting: Callable[[str, str], str], connector: str) -> dict:
    dtype, args, environment, _ = discovered_settings(
        observed, setting, lambda max_len: ["--context-length", str(max_len)]
    )
    if observed.get("requires_trust_remote_code") and "--trust-remote-code" not in args:
        args.append("--trust-remote-code")
    if (
        observed.get("conv_state_layers")
        and "--disable-radix-cache" not in args
        and "--mamba-radix-cache-strategy" not in args
    ):
        # A switched decode engine caches finished requests' SSM states. With the extra buffer a
        # request needs three state slots, SGLang's decode admission evicts a cached state only
        # when none is free, and the engine exits under load.
        args += ["--mamba-radix-cache-strategy", "no_buffer"]
        if "--disable-overlap-schedule" not in args:
            args.append("--disable-overlap-schedule")
    record = {
        "backend": "sglang",
        "connector": connector,
        "expected_packages": observed["packages"],
        "model_dtype": dtype,
        "kv_cache_dtype": "auto",
    }
    memory = setting("DECODE_CUDA_GRAPH_MEMORY_GB", "")
    if not memory:
        raise ValueError(f"set DECODE_CUDA_GRAPH_MEMORY_GB in .env for {connector} engines")
    record["decode_cuda_graph_memory_gb"] = float(memory)
    return {**record, "environment": environment, "extra_args": args}


def sequence_limit(args: list[str]) -> int:
    cap = option(args, "--max-running-requests")
    return int(cap) if cap is not None and cap.isdigit() else MAX_RUNNING_REQUESTS
