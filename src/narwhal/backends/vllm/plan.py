from __future__ import annotations

import json
import re
from pathlib import Path

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


def validate_runtime(runtime: dict) -> None:
    packages = runtime.get("expected_packages", {})
    if not packages.get("vllm") or not any(name in packages for name in ("nixl", "nixl-rocm")):
        raise ValueError("runtime.expected_packages requires pinned vLLM and NIXL versions")
    if any(not isinstance(v, str) or not v or "<" in v for v in packages.values()):
        raise ValueError("runtime.expected_packages requires resolved version strings")
    if (
        runtime.get("model_dtype") not in ("bfloat16", "float16")
        or runtime.get("kv_cache_dtype") != "auto"
    ):
        raise ValueError("this launcher uses a two-byte model dtype with kv_cache_dtype=auto")
    if type(runtime.get("block_size")) is not int or runtime["block_size"] < 1:
        raise ValueError("runtime.block_size must be positive")
    lease = runtime.get("kv_lease_s", KV_LEASE_S)
    if type(lease) is not int or lease < MIN_KV_LEASE_S:
        raise ValueError(f"runtime.kv_lease_s must be an integer of at least {MIN_KV_LEASE_S}")
    args = runtime.get("extra_args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("runtime.extra_args must be an argument list")
    index = 0
    while index < len(args):
        option = args[index]
        if (
            option in VALUE_OPTIONS
            and index + 1 < len(args)
            and not args[index + 1].startswith("--")
        ):
            index += 2
        elif option in FLAG_OPTIONS:
            index += 1
        else:
            raise ValueError(f"unsupported or incomplete runtime option: {option}")
    for name, value in runtime.get("environment", {}).items():
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            or name in MANAGED_ENV
            or not (name.startswith(ENV_PREFIXES) or name in ("LD_LIBRARY_PATH", "PYTHONPATH"))
            or any(
                word in name
                for word in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "SSH", "SKIP_COMPAT")
            )
            or not isinstance(value, str)
            or any(c in value for c in "\r\n\0")
        ):
            raise ValueError(f"unsupported runtime environment field: {name}")


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
    args = [
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        model,
        "--served-model-name",
        served_name,
        "--host",
        host,
        "--port",
        str(port),
        "--tensor-parallel-size",
        str(record["tensor_parallel_size"]),
        "--dtype",
        runtime["model_dtype"],
        "--kv-cache-dtype",
        runtime["kv_cache_dtype"],
        "--block-size",
        str(runtime["block_size"]),
        *(
            []
            if kv_events is None
            else [
                "--kv-events-config",
                json.dumps(
                    {
                        "enable_kv_cache_events": True,
                        "publisher": "zmq",
                        "endpoint": kv_events["endpoint"],
                        "replay_endpoint": kv_events["replay_endpoint"],
                    }
                ),
            ]
        ),
        "--kv-transfer-config",
        json.dumps(connector),
        *runtime.get("extra_args", []),
    ]
    return args, connector


def memory_fraction(args: list[str]) -> str | None:
    positions = [i for i, arg in enumerate(args) if arg == "--gpu-memory-utilization"]
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        return None
    return args[positions[0] + 1]


def requires_ds_conv_state_layout(model_dir: Path) -> bool:
    return ds_conv_state_layout_required(json.loads((model_dir / "config.json").read_text()))


def ds_conv_state_layout_required(model: dict) -> bool:
    text_model = model.get("text_config", model)
    linear = text_model.get("linear_attn_config", {})
    if (
        isinstance(linear, dict)
        and linear.get("kda_layers")
        and linear.get("short_conv_kernel_size")
    ):
        return True
    if text_model.get("mamba_d_conv") or text_model.get("mamba_d_state"):
        return True
    if text_model.get("linear_conv_kernel_dim") and "linear_attention" in text_model.get(
        "layer_types", []
    ):
        return True
    return any(
        isinstance(layer, str) and ("mamba" in layer.lower() or "ssm" in layer.lower())
        for layer in text_model.get("layer_types", [])
    )
