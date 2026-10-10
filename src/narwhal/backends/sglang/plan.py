from __future__ import annotations

import json
import math
import re

CONNECTORS = ("mooncake", "nixl")
# Only these transfer backends switch roles at runtime.
ROLE_SWITCH_CONNECTORS = ("mooncake",)
TRANSFER_PACKAGES = {"mooncake": "mooncake-transfer-engine", "nixl": "nixl"}
# Both roles must agree on page size, and some hybrid models force page 1 with Triton in prefill.
PAGE_SIZE = 1
ATTENTION_BACKEND = "triton"
# An uncapped request pool sized from the whole GPU fails decode kernel warmup.
MAX_RUNNING_REQUESTS = 256
# Prefill gives up on an absent decode peer after this many seconds; the connector's
# decode wait follows it.
BOOTSTRAP_TIMEOUT_S = 30
VALUE_OPTIONS = {
    "--context-length",
    "--mem-fraction-static",
    "--chunked-prefill-size",
    "--max-total-tokens",
    "--max-prefill-tokens",
    "--tokenizer-path",
    "--reasoning-parser",
    "--load-format",
    "--disaggregation-ib-device",
    "--mamba-ssm-dtype",
}
FLAG_OPTIONS = {
    "--trust-remote-code",
    "--disable-radix-cache",
    "--disable-cuda-graph",
    "--enable-mixed-chunk",
}
MANAGED_ENV = {
    "CUDA_VISIBLE_DEVICES",
    "UCX_NET_DEVICES",
    "UCX_TLS",
    "UCX_TCP_PORT_RANGE",
    "SGLANG_HOST_IP",
    "SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT",
    "NARWHAL_SGLANG_BOOTSTRAP_HOST",
    "NARWHAL_SGLANG_BOOTSTRAP_PORT",
    "NARWHAL_SGLANG_API_KEY",
}
ENV_PREFIXES = ("SGLANG_", "MOONCAKE_", "MC_", "UCX_", "NIXL_", "NCCL_", "PYTORCH_", "SAFETENSORS_")


def connector(runtime: dict) -> str:
    return runtime.get("connector", CONNECTORS[0])


def validate_runtime(runtime: dict) -> None:
    name = connector(runtime)
    if name not in CONNECTORS:
        raise ValueError(f"runtime.connector must be one of {', '.join(CONNECTORS)}")
    packages = runtime.get("expected_packages", {})
    if not packages.get("sglang") or not packages.get(TRANSFER_PACKAGES[name]):
        raise ValueError(
            f"runtime.expected_packages requires pinned sglang and {TRANSFER_PACKAGES[name]} "
            "versions"
        )
    if any(not isinstance(v, str) or not v or "<" in v for v in packages.values()):
        raise ValueError("runtime.expected_packages requires resolved version strings")
    if (
        runtime.get("model_dtype") not in ("bfloat16", "float16")
        or runtime.get("kv_cache_dtype") != "auto"
    ):
        raise ValueError("this launcher uses a two-byte model dtype with kv_cache_dtype=auto")
    if name not in ROLE_SWITCH_CONNECTORS and runtime.get("role") not in ("prefill", "decode"):
        raise ValueError(
            f"runtime.role must be prefill or decode; {name} engines keep their launch role"
        )
    if name in ROLE_SWITCH_CONNECTORS and "role" in runtime:
        raise ValueError(f"runtime.role is set by the router for {name} engines")
    if name in ROLE_SWITCH_CONNECTORS:
        memory = runtime.get("decode_cuda_graph_memory_gb")
        if (
            not isinstance(memory, int | float)
            or isinstance(memory, bool)
            or not math.isfinite(memory)
            or memory <= 0
        ):
            raise ValueError(
                "runtime.decode_cuda_graph_memory_gb must be positive; a prefill-launched "
                "engine reserves it when it switches to decode"
            )
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
            or any(word in name for word in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "SSH"))
            or not isinstance(value, str)
            or any(c in value for c in "\r\n\0")
        ):
            raise ValueError(f"unsupported runtime environment field: {name}")


def publishes_kv_events(args: list[str]) -> bool:
    # The radix cache is on unless disabled, and its events follow it.
    return "--disable-radix-cache" not in args


def serve_args(
    record: dict,
    *,
    model: str,
    served_name: str,
    host: str,
    port: int,
    kv_events: dict | None,
) -> tuple[list[str], dict]:
    runtime = record["runtime"]
    name = connector(runtime)
    switches = name in ROLE_SWITCH_CONNECTORS
    # A switching engine starts as prefill and the router switches decode engines at admission:
    # an engine launched as decode fails after a decode-prefill-decode round trip.
    role = "prefill" if switches else runtime["role"]
    transfer = {
        "transfer_backend": name,
        "launch_role": role,
        "role_switch": switches,
        **(
            {"decode_cuda_graph_memory_gb": float(runtime["decode_cuda_graph_memory_gb"])}
            if switches
            else {}
        ),
    }
    args = [
        "-m",
        "launch_engine",
        "serve",
        "--model-path",
        model,
        "--served-model-name",
        served_name,
        "--host",
        host,
        "--port",
        str(port),
        "--tp-size",
        str(record["tensor_parallel_size"]),
        "--dtype",
        runtime["model_dtype"],
        "--kv-cache-dtype",
        runtime["kv_cache_dtype"],
        "--page-size",
        str(PAGE_SIZE),
        "--attention-backend",
        ATTENTION_BACKEND,
        "--max-running-requests",
        str(MAX_RUNNING_REQUESTS),
        "--stream-interval",
        "1",
        "--enable-metrics",
        "--enable-cache-report",
        "--disaggregation-mode",
        role,
        "--disaggregation-transfer-backend",
        name,
        *(["--enable-pd-role-switch"] if switches else []),
        *(
            []
            if kv_events is None
            else [
                "--kv-events-config",
                json.dumps(
                    {
                        "publisher": "zmq",
                        "endpoint": kv_events["endpoint"],
                        "replay_endpoint": kv_events["replay_endpoint"],
                    }
                ),
            ]
        ),
        *runtime.get("extra_args", []),
    ]
    return args, transfer


def option(args: list[str], name: str) -> str | None:
    positions = [i for i, arg in enumerate(args) if arg == name]
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        return None
    return args[positions[0] + 1]


def memory_fraction(args: list[str]) -> str | None:
    return option(args, "--mem-fraction-static")
