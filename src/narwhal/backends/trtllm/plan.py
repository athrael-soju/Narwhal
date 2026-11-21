from __future__ import annotations

import json

from ...deployment.launch_engine.plan import (
    check_environment,
    check_extra_args,
    check_versions_and_dtype,
    option,
)
from ...deployment.launch_engine.settings import (
    LaunchSettings,
    Setting,
    check_settings,
    count,
    setting_args,
)
from .runtime import SHARED_GPU_FLAG

# Cache transceiver backends of the C++ transceiver runtime, by connector name.
CONNECTORS = {"ucx": "UCX", "nixl": "NIXL"}
CONFIG_FILE = "engine.json"
BLOCK_REUSE = "kv_cache_config.enable_block_reuse"
# The context worker frees an unclaimed handoff's KV after this many seconds.
KV_LEASE_S = 60
MIN_KV_LEASE_S = 6
MAX_BATCH_SIZE = 64
# The scheduler's token budget per iteration; chunked prefill serves longer prompts.
MAX_NUM_TOKENS = 8192
# MiB an engine on a shared GPU holds for activations, CUDA graphs and transfer buffers.
MEMORY_RESERVE_MIB = 8192
VALUE_OPTIONS = {
    "--free_gpu_memory_fraction",
    "--max_num_tokens",
    "--reasoning_parser",
    "--tool_parser",
    "--chat_template",
}
FLAG_OPTIONS = {"--trust_remote_code"}
MANAGED_ENV = {
    "CUDA_VISIBLE_DEVICES",
    "UCX_NET_DEVICES",
    "UCX_TLS",
    "UCX_TCP_PORT_RANGE",
    "TRTLLM_UCX_INTERFACE",
    "NARWHAL_TRTLLM_TRANSFER_HOST",
    "NARWHAL_TRTLLM_TRANSFER_PORT",
    "NARWHAL_TRTLLM_API_KEY",
}
ENV_PREFIXES = ("TRTLLM_", "TLLM_", "UCX_", "NIXL_", "NCCL_", "PYTORCH_", "SAFETENSORS_")
SETTINGS = LaunchSettings(
    entrypoint=("-m", "launch_engine", "serve"),
    model_flag="serve",
    tensor_parallel_flag="--tensor_parallel_size",
    kv_events_flag="--kv_events",
    remote_code_flag="--trust_remote_code",
    kv_events_fields={},
    settings={
        "context_length": Setting(flag="--max_seq_len", required=True),
        "max_sequences": Setting(flag="--max_batch_size", default=MAX_BATCH_SIZE),
        "block_size": Setting(),
        "tokenizer": Setting(flag="--tokenizer"),
        "prefix_caching": Setting(
            on=("--set", f"{BLOCK_REUSE}=true"), off=("--set", f"{BLOCK_REUSE}=false"), default=True
        ),
        # The server publishes no cache-event stream.
        "kv_events": Setting(default=False),
    },
)


def connector(runtime: dict) -> str:
    return runtime.get("connector", "ucx")


def validate_runtime(runtime: dict) -> None:
    if connector(runtime) not in CONNECTORS:
        raise ValueError(f"runtime.connector must be one of {', '.join(CONNECTORS)}")
    if not runtime.get("expected_packages", {}).get("tensorrt_llm"):
        raise ValueError("runtime.expected_packages requires a pinned tensorrt_llm version")
    check_versions_and_dtype(runtime)
    if "block_size" in runtime and runtime["block_size"] not in (8, 16, 32, 64, 128):
        raise ValueError("runtime.block_size must be 8, 16, 32, 64 or 128")
    lease = runtime.get("kv_lease_s", KV_LEASE_S)
    if type(lease) is not int or lease < MIN_KV_LEASE_S:
        raise ValueError(f"runtime.kv_lease_s must be an integer of at least {MIN_KV_LEASE_S}")
    reserve = runtime.get("memory_reserve_mib", MEMORY_RESERVE_MIB)
    if type(reserve) is not int or reserve < 1:
        raise ValueError("runtime.memory_reserve_mib must be a positive integer")
    check_settings(runtime, SETTINGS)
    check_extra_args(runtime.get("extra_args", []), VALUE_OPTIONS, FLAG_OPTIONS)
    if runtime.get("kv_events") is True:
        raise ValueError("runtime.kv_events is unavailable: the server publishes no event stream")
    check_environment(runtime.get("environment", {}), MANAGED_ENV, ENV_PREFIXES)


def engine_config(runtime: dict) -> dict:
    """The server configuration the launch writes, for both leg types on every worker."""
    kv_cache = {
        # The C++ transceiver requires the version 1 KV cache manager.
        "use_kv_cache_manager_v2": False,
    }
    if "block_size" in runtime:
        kv_cache["tokens_per_block"] = runtime["block_size"]
    return {
        "dtype": runtime["model_dtype"],
        # A worker that serves context legs completes none under load with the overlap
        # scheduler on.
        "disable_overlap_scheduler": True,
        "enable_chunked_prefill": True,
        # The iteration statistics feed the Prometheus route that return_perf_metrics serves.
        "enable_iter_perf_stats": True,
        "return_perf_metrics": True,
        # CUDA graphs cover every batch up to the sequence limit, padded to a captured size.
        "cuda_graph_config": {
            "max_batch_size": count(runtime, SETTINGS, "max_sequences"),
            "enable_padding": True,
        },
        "cache_transceiver_config": {
            "backend": CONNECTORS[connector(runtime)],
            "transceiver_runtime": "CPP",
            "kv_transfer_timeout_ms": runtime.get("kv_lease_s", KV_LEASE_S) * 1000,
        },
        "kv_cache_config": kv_cache,
    }


def config_files(record: dict, directory: str) -> tuple[dict[str, str], list[str]]:
    config = json.dumps(engine_config(record["runtime"]), indent=2, sort_keys=True) + "\n"
    return {CONFIG_FILE: config}, ["--config", f"{directory}/{CONFIG_FILE}"]


def serve_args(record: dict, *, model: str, served_name: str, host: str, port: int) -> list[str]:
    runtime = record["runtime"]
    args = [
        *SETTINGS.entrypoint,
        "serve",
        model,
        "--backend",
        "pytorch",
        "--host",
        host,
        "--port",
        str(port),
        "--served_model_name",
        served_name,
        SETTINGS.tensor_parallel_flag,
        str(record["tensor_parallel_size"]),
        "--kv_cache_dtype",
        runtime["kv_cache_dtype"],
        "--max_seq_len",
        str(runtime["context_length"]),
        "--max_batch_size",
        str(count(runtime, SETTINGS, "max_sequences")),
        # /metrics serves its statistics from one frontend only.
        "--num_serve_frontends",
        "1",
    ]
    if "--max_num_tokens" not in runtime.get("extra_args", []):
        args += ["--max_num_tokens", str(MAX_NUM_TOKENS)]
    if "tokenizer" in runtime:
        args += ["--tokenizer", runtime["tokenizer"]]
    args += setting_args(runtime, SETTINGS, "prefix_caching")
    if record.get("shared_device") is not None:
        # The wrapper bounds the KV cache to the engine's share of the GPU.
        args += [SHARED_GPU_FLAG, str(runtime.get("memory_reserve_mib", MEMORY_RESERVE_MIB))]
    return [*args, *runtime.get("extra_args", [])]


def transfer(runtime: dict) -> dict:
    return {
        "transfer_backend": CONNECTORS[connector(runtime)],
        "transceiver_runtime": "CPP",
        "kv_lease_s": runtime.get("kv_lease_s", KV_LEASE_S),
    }


def memory_fraction(args: list[str]) -> str | None:
    return option(args, "--free_gpu_memory_fraction")


def sequence_limit(runtime: dict) -> int:
    return count(runtime, SETTINGS, "max_sequences") or MAX_BATCH_SIZE
