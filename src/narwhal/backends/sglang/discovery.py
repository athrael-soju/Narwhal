from __future__ import annotations

import re
from collections.abc import Callable

from ...deployment.launch_engine.plan import discovered_launch_settings, discovered_settings
from ...deployment.launch_engine.settings import count
from .plan import CUDA_BUILD_SUFFIX, MAX_RUNNING_REQUESTS, SETTINGS, TRANSFER_PACKAGES

# At page size 1, FlashInfer decode attention costs less per KV token in the batch than Triton.
DECODE_ATTENTION_BACKEND = "flashinfer"
# Engines from this release switch roles with the overlap scheduler and FlashInfer decode
# attention. Before it, a switched decode engine with the overlap scheduler on exits under load,
# or on its second switch back to decode; sgl-project/sglang#41927 and #43760 fix both.
OVERLAP_RELEASE = (0, 5, 23)

IMAGE_PACKAGES = "sglang|torch|" + "|".join(
    f"{re.escape(name)}(?:{CUDA_BUILD_SUFFIX})?" for name in TRANSFER_PACKAGES.values()
)


def runtime(observed: dict, setting: Callable[[str, str], str], connector: str) -> dict:
    dtype, context_length, args, environment, _ = discovered_settings(observed, setting, [])
    fields = discovered_launch_settings(setting)
    if observed.get("requires_trust_remote_code") and "--trust-remote-code" not in args:
        args.append("--trust-remote-code")
    current = at_least(observed["packages"].get("sglang", ""), OVERLAP_RELEASE)
    if current and "--decode-attention-backend" not in args:
        args += ["--decode-attention-backend", DECODE_ATTENTION_BACKEND]
    if (
        not current
        and observed.get("conv_state_layers")
        and fields.get("prefix_caching") is not False
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
        "context_length": context_length,
        **fields,
    }
    sequences = setting("MAX_SEQUENCES", "")
    if sequences:
        record["max_sequences"] = int(sequences)
    memory = setting("DECODE_CUDA_GRAPH_MEMORY_GB", "")
    if not memory:
        raise ValueError(f"set DECODE_CUDA_GRAPH_MEMORY_GB in .env for {connector} engines")
    record["decode_cuda_graph_memory_gb"] = float(memory)
    return {**record, "environment": environment, "extra_args": args}


def at_least(version: str, release: tuple[int, int, int]) -> bool:
    """Whether `version` is `release` or later; a pre-release or development build is earlier."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?(.*)", version.split("+")[0].strip().lower())
    if match is None:
        return False
    major, minor, patch, rest = match.groups()
    numbers = (int(major), int(minor), int(patch or 0))
    early = re.match(r"[-_.]?(a|b|c|rc|alpha|beta|pre|preview|dev)", rest) is not None
    return numbers > release or (numbers == release and not early)


def sequence_limit(runtime: dict) -> int:
    return count(runtime, SETTINGS, "max_sequences") or MAX_RUNNING_REQUESTS
