"""Calculate a declared KV workload's link budget and compare host bandwidth samples."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
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
        raise ValueError("hybrid/windowed cache needs measured pages from --runtime-layout")
    if model.get("sliding_window") or model.get("mamba_d_state"):
        raise ValueError("hybrid/windowed cache needs measured pages from --runtime-layout")
    layers = integer(model, "num_hidden_layers")
    if model.get("kv_lora_rank"):
        if element_bytes != 2:
            raise ValueError("packed MLA cache needs measured pages from --runtime-layout")
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
    padded_tokens = ((prompt_tokens + block_tokens - 1) // block_tokens) * block_tokens
    payload = bytes_per_token * padded_tokens
    return {
        **workload_budget(payload, handoffs_per_s, burst, transfer_budget_s, headroom),
        "bytes_per_token_all_ranks": bytes_per_token,
        "prompt_tokens": prompt_tokens,
        "block_tokens": block_tokens,
        "padded_tokens": padded_tokens,
    }


def workload_budget(
    payload: int, handoffs_per_s: float, burst: int, transfer_budget_s: float, headroom: float
) -> dict:
    integer({"payload": payload}, "payload")
    integer({"burst": burst}, "burst")
    positive(handoffs_per_s, "handoffs_per_s")
    positive(transfer_budget_s, "transfer_budget_s")
    if positive(headroom, "headroom") < 1:
        raise ValueError("headroom must be at least 1")
    required = 8 * payload * max(handoffs_per_s, burst / transfer_budget_s) * headroom / 1e9
    positive(required, "required_gbps")
    return {
        "payload_bytes_per_handoff": payload,
        "peak_handoffs_per_s": handoffs_per_s,
        "burst_handoffs": burst,
        "transfer_budget_s": transfer_budget_s,
        "headroom": headroom,
        "required_gbps": required,
        "scope": "one directed host edge; all TP ranks; declared workload",
    }


def runtime_payload(layout: dict, tp: int, prompt_tokens: int) -> int:
    """Bound a complete prompt's padded cache pages, summing every recorded TP rank."""
    integer({"prompt_tokens": prompt_tokens}, "prompt_tokens")
    integer({"tp": tp}, "tp")
    if layout["schema_version"] != 1 or layout["sizing"] != "runtime_padded_page_upper_bound":
        raise ValueError("unsupported runtime cache sizing record")
    ranks = layout["ranks"]
    if len(ranks) != tp or sorted(rank["rank"] for rank in ranks) != list(range(tp)):
        raise ValueError("runtime cache sizing requires every TP rank exactly once")
    payload = 0
    for rank in ranks:
        layers = rank["layers"]
        if not layers or len({layer["layer"] for layer in layers}) != len(layers):
            raise ValueError("runtime cache sizing requires distinct layers on every rank")
        for layer in layers:
            page = integer(layer, "page_bytes")
            block = integer(layer, "block_tokens")
            extra = layer["extra_blocks"]
            if type(extra) is not int or extra < 0:
                raise ValueError("runtime cache extra_blocks must be a nonnegative integer")
            payload += (((prompt_tokens + block - 1) // block) + extra) * page
    return payload


def received_gbps(sample: dict) -> float:
    if sample.get("error"):
        raise ValueError("iperf3 reported an error; inspect the private sample")
    if sample["start"]["test_start"]["protocol"] != "TCP":
        raise ValueError("this comparison expects an iperf3 TCP sample")
    received = sample["end"]["sum_received"]
    positive(float(received["seconds"]), "sample duration")
    return positive(float(received["bits_per_second"]), "received bitrate") / 1e9


LINK_FIELDS = {
    "source_role",
    "destination_role",
    "source_address",
    "destination_address",
    "source_interface",
    "destination_interface",
    "source_route",
    "destination_route",
    "transport",
    "tool_version",
    "test_parameters",
}


def link_fingerprint(link: dict) -> str:
    """Bind one directed measurement to its route and test conditions."""
    if set(link) != LINK_FIELDS:
        raise ValueError("link record requires role, address, interface, route and test fields")
    for name in LINK_FIELDS - {"test_parameters"}:
        if not isinstance(link[name], str) or not link[name].strip():
            raise ValueError(f"link record requires {name}")
    if (
        not re.fullmatch(r"engine-[1-9][0-9]*", link["source_role"])
        or not re.fullmatch(r"engine-[1-9][0-9]*", link["destination_role"])
        or link["source_role"] == link["destination_role"]
    ):
        raise ValueError("link record requires two distinct engine roles")
    if link["transport"] not in ("ucx_tcp", "ucx_rdma"):
        raise ValueError("link record requires ucx_tcp or ucx_rdma")
    parameters = link["test_parameters"]
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("link record requires test parameters")
    if any(
        not isinstance(key, str) or not key or value is None for key, value in parameters.items()
    ):
        raise ValueError("link test parameters require named values")
    return hashlib.sha256(
        json.dumps(link, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_link(args: argparse.Namespace) -> None:
    link = {
        "source_role": args.source_role,
        "destination_role": args.destination_role,
        "source_address": args.source_address,
        "destination_address": args.destination_address,
        "source_interface": args.source_interface,
        "destination_interface": args.destination_interface,
        "source_route": args.source_route.read_text().strip(),
        "destination_route": args.destination_route.read_text().strip(),
        "transport": args.transport,
        "tool_version": args.tool_version,
        "test_parameters": json.loads(args.test_parameters),
    }
    link_fingerprint(link)
    write_private(args.out, link)


def record_edge(
    link_path: Path, budget_path: Path, sample_path: Path, out: Path, gbps: float | None
) -> bool:
    link = json.loads(link_path.read_text())
    fingerprint = link_fingerprint(link)
    if (link["transport"] == "ucx_tcp") != (gbps is None):
        raise ValueError("TCP uses the iperf JSON rate; RDMA requires --gbps")
    sample_data = sample_path.read_bytes()
    measured = (
        received_gbps(json.loads(sample_data))
        if link["transport"] == "ucx_tcp"
        else positive(gbps, "measured_gbps")
    )
    budget_data = budget_path.read_bytes()
    required = positive(float(json.loads(budget_data)["required_gbps"]), "required_gbps")
    passed = measured >= required
    write_private(
        out,
        {
            "schema": "narwhal.fabric-edge-evidence",
            "schema_version": 1,
            "link_sha256": fingerprint,
            "sample_sha256": hashlib.sha256(sample_data).hexdigest(),
            "budget_sha256": hashlib.sha256(budget_data).hexdigest(),
            "measured_gbps": measured,
            "required_gbps": required,
            "passed": passed,
        },
    )
    return passed


def reuse_edge(
    link_path: Path, evidence_path: Path, sample_path: Path, budget_path: Path, out: Path
) -> bool:
    fingerprint = link_fingerprint(json.loads(link_path.read_text()))
    evidence = json.loads(evidence_path.read_text())
    if (
        evidence.get("schema") != "narwhal.fabric-edge-evidence"
        or evidence.get("schema_version") != 1
    ):
        raise ValueError("unsupported fabric edge evidence")
    if evidence["link_sha256"] != fingerprint:
        raise ValueError("link inputs changed; collect a fresh directed sample")
    if evidence["sample_sha256"] != hashlib.sha256(sample_path.read_bytes()).hexdigest():
        raise ValueError("retained sample differs from the recorded evidence")
    measured = positive(float(evidence["measured_gbps"]), "measured_gbps")
    budget_data = budget_path.read_bytes()
    required = positive(float(json.loads(budget_data)["required_gbps"]), "required_gbps")
    passed = measured >= required
    write_private(
        out,
        {
            "schema": "narwhal.fabric-edge-comparison",
            "schema_version": 1,
            "link_sha256": fingerprint,
            "sample_sha256": evidence["sample_sha256"],
            "source_evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            "budget_sha256": hashlib.sha256(budget_data).hexdigest(),
            "measured_gbps": measured,
            "required_gbps": required,
            "passed": passed,
        },
    )
    return passed


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
    budget.add_argument("--element-bytes", type=int, choices=(1, 2, 4))
    sizing = budget.add_mutually_exclusive_group(required=True)
    sizing.add_argument(
        "--runtime-layout",
        type=Path,
        help="cache-layout.json from a serving capture or measure-cache",
    )
    sizing.add_argument(
        "--uniform-cache", action="store_true", help="explicit uniform analytical estimate"
    )
    sizing.add_argument(
        "--bytes-per-token", type=int, help="measured total across TP ranks for a uniform layout"
    )
    budget.add_argument("--prompt-tokens", type=int, required=True)
    budget.add_argument("--block-tokens", type=int)
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
    link = commands.add_parser("link", help="record current directed link and measurement inputs")
    for field in (
        "source-role",
        "destination-role",
        "source-address",
        "destination-address",
        "source-interface",
        "destination-interface",
        "tool-version",
        "test-parameters",
    ):
        link.add_argument("--" + field, required=True)
    link.add_argument("--source-route", type=Path, required=True)
    link.add_argument("--destination-route", type=Path, required=True)
    link.add_argument("--transport", choices=("ucx_tcp", "ucx_rdma"), required=True)
    link.add_argument("--out", type=Path, required=True)
    record = commands.add_parser("record-edge", help="bind a directed sample to link conditions")
    record.add_argument("--link", type=Path, required=True)
    record.add_argument("--budget", type=Path, required=True)
    record.add_argument("--sample", type=Path, required=True)
    record.add_argument("--gbps", type=float)
    record.add_argument("--out", type=Path, required=True)
    reuse = commands.add_parser(
        "reuse-edge", help="check retained evidence against current link inputs"
    )
    reuse.add_argument("--link", type=Path, required=True)
    reuse.add_argument("--evidence", type=Path, required=True)
    reuse.add_argument("--sample", type=Path, required=True)
    reuse.add_argument("--budget", type=Path, required=True)
    reuse.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "calculate":
            model_data = args.model_config.read_bytes()
            launch_data = args.launch_config.read_bytes()
            launch = json.loads(launch_data)
            tp = launch["tensor_parallel_size"]
            hashes = {
                "model_config_sha256": hashlib.sha256(model_data).hexdigest(),
                "launch_config_sha256": hashlib.sha256(launch_data).hexdigest(),
            }
            if args.runtime_layout:
                if args.block_tokens is not None or args.element_bytes is not None:
                    raise ValueError("runtime sizing supplies its own page sizes and token blocks")
                runtime_data = args.runtime_layout.read_bytes()
                runtime = json.loads(runtime_data)
                if any(runtime[key] != value for key, value in hashes.items()):
                    raise ValueError("runtime cache sizing differs from the model or launch record")
                payload = runtime_payload(runtime, tp, args.prompt_tokens)
                result = workload_budget(
                    payload, args.handoffs_per_s, args.burst, args.transfer_budget_s, args.headroom
                )
                result.update(
                    layout=runtime["sizing"],
                    prompt_tokens=args.prompt_tokens,
                    runtime_layout_sha256=hashlib.sha256(runtime_data).hexdigest(),
                    image=runtime["image"],
                    plan_sha256=runtime["plan_sha256"],
                )
            else:
                if args.element_bytes is None or args.block_tokens is None:
                    raise ValueError("uniform sizing requires --element-bytes and --block-tokens")
                if args.bytes_per_token is None:
                    layout, size = cache_shape(json.loads(model_data), tp, args.element_bytes)
                else:
                    layout, size = "measured_uniform", args.bytes_per_token
                result = calculate(
                    size,
                    args.prompt_tokens,
                    args.block_tokens,
                    args.handoffs_per_s,
                    args.burst,
                    args.transfer_budget_s,
                    args.headroom,
                )
                result.update(layout=layout, element_bytes=args.element_bytes)
            result.update(
                tensor_parallel_size=tp,
                **hashes,
            )
            write_private(args.out, result)
            print(f"Required per directed edge: {result['required_gbps']:.6f} Gbit/s")
        elif args.command == "compare":
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
        elif args.command == "link":
            create_link(args)
            print(
                json.dumps(
                    {
                        "link": str(args.out),
                        "sha256": link_fingerprint(json.loads(args.out.read_text())),
                    }
                )
            )
        elif args.command == "record-edge":
            passed = record_edge(args.link, args.budget, args.sample, args.out, args.gbps)
            print(json.dumps({"evidence": str(args.out), "passed": passed}))
            return 0 if passed else 1
        else:
            passed = reuse_edge(args.link, args.evidence, args.sample, args.budget, args.out)
            print(json.dumps({"comparison": str(args.out), "passed": passed}))
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
