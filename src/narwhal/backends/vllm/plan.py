from __future__ import annotations

import json

from ...deployment.launch_engine.plan import (
    SECRET_WORDS,
    check_environment,
    check_extra_args,
    check_versions_and_dtype,
    option,
)
from ...deployment.launch_engine.settings import LaunchSettings, Setting, check_settings
from ...deployment.launch_engine.settings import serve_args as render

VALUE_OPTIONS = {
    "--max-model-len",
    "--gpu-memory-utilization",
    "--max-num-batched-tokens",
    "--max-num-seqs",
    "--reasoning-parser",
    "--attention-backend",
    "--tokenizer",
    "--hf-config-path",
    "--load-format",
    "--kv-events-config",
}
FLAG_OPTIONS = {
    "--trust-remote-code",
    "--language-model-only",
    "--enforce-eager",
    "--async-scheduling",
    "--no-disable-hybrid-kv-cache-manager",
    "--disable-hybrid-kv-cache-manager",
    "--enable-prefix-caching",
    "--no-enable-prefix-caching",
}
# NIXL engine_ttl for CUDA IPC peers with the UCX IPC cache off; the router's first peer
# release round follows it.
ENGINE_TTL_S = 60
# vLLM's default NIXL producer lease, set explicitly so the attested launch arguments record
# it. A consumer renews it every lease // 6 seconds.
KV_LEASE_S = 30
MIN_KV_LEASE_S = 6
MANAGED_ENV = {
    "ROCR_VISIBLE_DEVICES",
    "CUDA_VISIBLE_DEVICES",
    "UCX_NET_DEVICES",
    "UCX_TLS",
    "UCX_TCP_PORT_RANGE",
    "NIXL_HOST_IP",
    "VLLM_NIXL_SIDE_CHANNEL_HOST",
    "VLLM_NIXL_SIDE_CHANNEL_PORT",
    "VLLM_API_KEY",
}
ENV_PREFIXES = (
    "VLLM_",
    "UCX_",
    "NIXL_",
    "ROCM_",
    "HIP_",
    "HSA_",
    "AITER_",
    "PYTORCH_",
    "SAFETENSORS_",
)
KV_EVENTS_OFF = ("--kv-events-config", json.dumps({"enable_kv_cache_events": False}))
SETTINGS = LaunchSettings(
    entrypoint=("-m", "vllm.entrypoints.openai.api_server"),
    model_flag="--model",
    tensor_parallel_flag="--tensor-parallel-size",
    kv_events_flag="--kv-events-config",
    remote_code_flag="--trust-remote-code",
    kv_events_fields={"enable_kv_cache_events": True},
    settings={
        "block_size": Setting(flag="--block-size", required=True),
        "context_length": Setting(flag="--max-model-len"),
        "max_sequences": Setting(flag="--max-num-seqs"),
        "tokenizer": Setting(flag="--tokenizer"),
        "prefix_caching": Setting(
            on=("--enable-prefix-caching",), off=("--no-enable-prefix-caching",), default=True
        ),
        "kv_events": Setting(off=KV_EVENTS_OFF, default=True),
    },
)


def validate_runtime(runtime: dict) -> None:
    packages = runtime.get("expected_packages", {})
    if not packages.get("vllm") or not any(name in packages for name in ("nixl", "nixl-rocm")):
        raise ValueError("runtime.expected_packages requires pinned vLLM and NIXL versions")
    check_versions_and_dtype(runtime)
    if type(runtime.get("block_size")) is not int or runtime["block_size"] < 1:
        raise ValueError("runtime.block_size must be positive")
    lease = runtime.get("kv_lease_s", KV_LEASE_S)
    if type(lease) is not int or lease < MIN_KV_LEASE_S:
        raise ValueError(f"runtime.kv_lease_s must be an integer of at least {MIN_KV_LEASE_S}")
    check_extra_args(runtime.get("extra_args", []), VALUE_OPTIONS, FLAG_OPTIONS)
    check_settings(runtime, SETTINGS)
    check_environment(
        runtime.get("environment", {}),
        MANAGED_ENV,
        ENV_PREFIXES,
        forbidden=(*SECRET_WORDS, "SKIP_COMPAT"),
    )


def publishes_kv_events(args: list[str]) -> bool:
    # Publication follows prefix caching unless --kv-events-config disables it.
    if {"--enable-prefix-caching", "--no-enable-prefix-caching"} <= set(args):
        raise ValueError("runtime.extra_args must select prefix caching at most once")
    supplied = [args[i + 1] for i, arg in enumerate(args) if arg == "--kv-events-config"]
    if len(supplied) > 1:
        raise ValueError("runtime.extra_args must supply --kv-events-config at most once")
    if supplied:
        try:
            value = json.loads(supplied[0])
        except ValueError:
            value = None
        if value != {"enable_kv_cache_events": False}:
            raise ValueError(
                "runtime.extra_args --kv-events-config may only set "
                '{"enable_kv_cache_events": false}; the launcher selects event endpoints'
            )
        return False
    return "--no-enable-prefix-caching" not in args


def serve_args(
    record: dict,
    *,
    model: str,
    served_name: str,
    host: str,
    port: int,
    kv_events: dict | None,
    evict_peers: bool,
) -> tuple[list[str], dict]:
    runtime = record["runtime"]
    connector = {
        "kv_connector": "NixlConnector",
        "kv_role": "kv_both",
        "kv_load_failure_policy": "fail",
        "kv_connector_extra_config": {
            "backends": ["UCX"],
            "enforce_handshake_compat": True,
            "kv_lease_duration": runtime.get("kv_lease_s", KV_LEASE_S),
            **({"engine_ttl": ENGINE_TTL_S} if evict_peers else {}),
        },
    }
    args = render(
        record,
        SETTINGS,
        (
            "common",
            "block_size",
            "context_length",
            "max_sequences",
            "tokenizer",
            "prefix_caching",
            "kv_events",
            ("--kv-transfer-config", json.dumps(connector)),
            "extra",
        ),
        model=model,
        served_name=served_name,
        host=host,
        port=port,
        kv_events=kv_events,
    )
    return args, connector


def memory_fraction(args: list[str]) -> str | None:
    return option(args, "--gpu-memory-utilization")
