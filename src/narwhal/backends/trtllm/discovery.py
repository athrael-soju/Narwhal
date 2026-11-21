from __future__ import annotations

from collections.abc import Callable

from ...deployment.launch_engine.plan import discovered_launch_settings, discovered_settings
from .plan import sequence_limit as plan_sequence_limit

IMAGE_PACKAGES = "tensorrt_llm|torch"


def runtime(observed: dict, setting: Callable[[str, str], str], connector: str) -> dict:
    dtype, context_length, args, environment, _ = discovered_settings(observed, setting, [])
    fields = discovered_launch_settings(setting)
    if fields.pop("kv_events", False):
        raise ValueError("set KV_EVENTS to false: TensorRT-LLM publishes no cache-event stream")
    if observed.get("requires_trust_remote_code") and "--trust_remote_code" not in args:
        args.append("--trust_remote_code")
    record = {
        "backend": "trtllm",
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
    return {**record, "environment": environment, "extra_args": args}


def sequence_limit(runtime: dict) -> int:
    return plan_sequence_limit(runtime)
