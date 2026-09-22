"""Calculate a declared KV workload's link budget and compare host bandwidth samples."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path


def positive(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


def integer(document: dict, key: str) -> int:
    value = document.get(key)
    if type(value) is not int or value < 1:
        raise ValueError(f"model config requires a positive integer {key}")
    return value


def cache_shape(model: dict, tp: int, element_bytes: int) -> tuple[str, int]:
    """Sum cache elements across TP ranks, including replicated KV heads or MLA latents."""
    model = model.get("text_config", model)
    if type(tp) is not int or tp < 1 or element_bytes not in (1, 2, 4):
        raise ValueError("TP must be positive; cache elements must use 1, 2 or 4 bytes")
    if model.get("layer_types") and set(model["layer_types"]) != {"full_attention"}:
        raise ValueError("hybrid/windowed cache needs an explicit measured --bytes-per-token")
    if model.get("sliding_window") or model.get("mamba_d_state"):
        raise ValueError("hybrid/windowed cache needs an explicit measured --bytes-per-token")
    layers = integer(model, "num_hidden_layers")
    if model.get("kv_lora_rank"):
        if element_bytes != 2:
            raise ValueError("packed MLA cache needs an explicit measured --bytes-per-token")
        width = integer(model, "kv_lora_rank") + integer(model, "qk_rope_head_dim")
        return "mla", layers * width * element_bytes * tp
    heads = integer(model, "num_attention_heads")
    kv_heads = integer(model, "num_key_value_heads") if "num_key_value_heads" in model else heads
    if heads % tp or (kv_heads >= tp and kv_heads % tp) or (kv_heads < tp and tp % kv_heads):
        raise ValueError("attention heads and TP require an even shard or replication mapping")
    if "head_dim" in model:
        width = integer(model, "head_dim")
    else:
        hidden = integer(model, "hidden_size")
        if hidden % heads:
            raise ValueError("hidden_size must divide by attention heads or supply head_dim")
        width = hidden // heads
    # Each rank stores at least one KV head when TP exceeds the KV head count.
    stored_heads = max(kv_heads // tp, 1) * tp
    return "attention", 2 * layers * stored_heads * width * element_bytes


def calculate(
    bytes_per_token: int,
    prompt_tokens: int,
    block_tokens: int,
    handoffs_per_s: float,
    burst: int,
    transfer_budget_s: float,
    headroom: float,
) -> dict:
    for name, value in (
        ("bytes_per_token", bytes_per_token),
        ("prompt_tokens", prompt_tokens),
        ("block_tokens", block_tokens),
        ("burst", burst),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    positive(handoffs_per_s, "handoffs_per_s")
    positive(transfer_budget_s, "transfer_budget_s")
    if positive(headroom, "headroom") < 1:
        raise ValueError("headroom must be at least 1")
    padded_tokens = ((prompt_tokens + block_tokens - 1) // block_tokens) * block_tokens
    payload = bytes_per_token * padded_tokens
    required = 8 * payload * max(handoffs_per_s, burst / transfer_budget_s) * headroom / 1e9
    return {
        "bytes_per_token_all_ranks": bytes_per_token,
        "prompt_tokens": prompt_tokens,
        "block_tokens": block_tokens,
        "padded_tokens": padded_tokens,
        "payload_bytes_per_handoff": payload,
        "peak_handoffs_per_s": handoffs_per_s,
        "burst_handoffs": burst,
        "transfer_budget_s": transfer_budget_s,
        "headroom": headroom,
        "required_gbps": required,
        "scope": "one directed host edge; all TP ranks; declared workload",
    }


def received_gbps(sample: dict) -> float:
    if sample.get("error"):
        raise ValueError("iperf3 reported an error; inspect the private sample")
    if sample["start"]["test_start"]["protocol"] != "TCP":
        raise ValueError("this comparison expects an iperf3 TCP sample")
    received = sample["end"]["sum_received"]
    positive(float(received["seconds"]), "sample duration")
    return positive(float(received["bits_per_second"]), "received bitrate") / 1e9


def write_private(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    budget = commands.add_parser("calculate", help="write a private workload budget")
    budget.add_argument("--model-config", type=Path, required=True)
    budget.add_argument("--launch-config", type=Path, required=True)
    budget.add_argument("--element-bytes", type=int, choices=(1, 2, 4), required=True)
    budget.add_argument("--bytes-per-token", type=int, help="measured total across TP ranks")
    budget.add_argument("--prompt-tokens", type=int, required=True)
    budget.add_argument("--block-tokens", type=int, required=True)
    budget.add_argument("--handoffs-per-s", type=float, required=True)
    budget.add_argument("--burst", type=int, required=True)
    budget.add_argument("--transfer-budget-s", type=float, required=True)
    budget.add_argument("--headroom", type=float, required=True)
    budget.add_argument("--out", type=Path, required=True)
    compare = commands.add_parser("compare", help="compare a sample with its workload budget")
    compare.add_argument("--budget", type=Path, required=True)
    measurement = compare.add_mutually_exclusive_group(required=True)
    measurement.add_argument("--iperf", type=Path)
    measurement.add_argument("--gbps", type=float, help="RDMA average from the retained report")
    args = parser.parse_args(argv)
    try:
        if args.command == "calculate":
            model_data = args.model_config.read_bytes()
            launch_data = args.launch_config.read_bytes()
            launch = json.loads(launch_data)
            tp = launch["tensor_parallel_size"]
            if args.bytes_per_token is None:
                layout, size = cache_shape(json.loads(model_data), tp, args.element_bytes)
            else:
                layout, size = "measured", args.bytes_per_token
            result = calculate(
                size,
                args.prompt_tokens,
                args.block_tokens,
                args.handoffs_per_s,
                args.burst,
                args.transfer_budget_s,
                args.headroom,
            )
            result.update(
                layout=layout,
                tensor_parallel_size=tp,
                element_bytes=args.element_bytes,
                model_config_sha256=hashlib.sha256(model_data).hexdigest(),
                launch_config_sha256=hashlib.sha256(launch_data).hexdigest(),
            )
            write_private(args.out, result)
            print(f"Required per directed edge: {result['required_gbps']:.6f} Gbit/s")
        else:
            required = positive(
                float(json.loads(args.budget.read_text())["required_gbps"]), "required_gbps"
            )
            measured = (
                received_gbps(json.loads(args.iperf.read_text()))
                if args.iperf
                else positive(args.gbps, "measured_gbps")
            )
            passed = measured >= required
            print(
                json.dumps({"measured_gbps": measured, "required_gbps": required, "passed": passed})
            )
            return 0 if passed else 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, FileExistsError):
            parser.exit(2, "Budget exists; select a fresh output path.\n")
        if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            parser.exit(2, f"{error}\n")
        parser.exit(2, "Check the model, launch record, measurement fields and file paths.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
