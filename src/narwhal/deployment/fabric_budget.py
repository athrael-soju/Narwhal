"""Size a declared KV workload's link budget and bind directed link samples to it.

`narwhal deploy fabric` sizes each cache group's budget from its representative's captured
cache layout or measured handoffs, and records each directed link sample against the budget of
every source role that sends KV over that link.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# The record that a backend's transfer measurement tool writes, as `TransferMeasurement` in
# `launch_engine/backend.py` describes.
TRANSFER_SCHEMA = "narwhal.kv-transfer-measurement"
EVIDENCE_SCHEMA = "narwhal.fabric-edge-evidence"
COMPARISON_SCHEMA = "narwhal.fabric-edge-comparison"
# Version 2 records the source role, because one sample serves every role behind a link.
EVIDENCE_VERSION = 2
TRANSPORTS = ("ucx_tcp", "ucx_rdma")
# The column of a perftest report, run with --report_gbits, that holds the average rate.
RDMA_AVERAGE = "BW average[Gb/sec]"


def positive(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


def integer(document: dict, key: str) -> int:
    value = document.get(key)
    if type(value) is not int or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


@dataclass(frozen=True)
class Workload:
    """The declared workload one directed host edge must carry for each source cache group."""

    prompt_tokens: int
    handoffs_per_s: float
    burst: int
    transfer_budget_s: float
    headroom: float

    def __post_init__(self) -> None:
        integer({"prompt_tokens": self.prompt_tokens}, "prompt_tokens")
        integer({"burst": self.burst}, "burst")
        positive(self.handoffs_per_s, "handoffs_per_s")
        positive(self.transfer_budget_s, "transfer_budget_s")
        if positive(self.headroom, "headroom") < 1:
            raise ValueError("headroom must be at least 1")

    def fields(self) -> dict[str, Any]:
        return asdict(self)


def workload_budget(payload: int, workload: Workload) -> dict:
    integer({"payload": payload}, "payload")
    rate = max(workload.handoffs_per_s, workload.burst / workload.transfer_budget_s)
    required = 8 * payload * rate * workload.headroom / 1e9
    positive(required, "required_gbps")
    return {
        "payload_bytes_per_handoff": payload,
        "peak_handoffs_per_s": workload.handoffs_per_s,
        "burst_handoffs": workload.burst,
        "transfer_budget_s": workload.transfer_budget_s,
        "headroom": workload.headroom,
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


def runtime_layout_name(layout: dict) -> str | None:
    """Return the one KV cache layout every TP rank resolved, or None when no rank names one."""
    names = {rank.get("kv_cache_layout") for rank in layout["ranks"]}
    if names == {None}:
        return None
    if len(names) != 1 or None in names:
        raise ValueError(
            "TP ranks resolved different KV cache layouts; each layout needs its own cache group"
        )
    name = names.pop()
    if not isinstance(name, str) or not name:
        raise ValueError("the runtime cache layout names no KV cache layout")
    return name


def transfer_record(data: bytes, prompt_tokens: int) -> tuple[int, dict]:
    """Return the measured bytes per handoff and the budget evidence of a transfer record."""
    record = json.loads(data)
    if record.get("schema") != TRANSFER_SCHEMA or record.get("schema_version") != 1:
        raise ValueError("unsupported KV transfer measurement record")
    if record.get("prompt_tokens") != prompt_tokens:
        raise ValueError("the transfer record measured another prompt length")
    snapshots = record.get("metrics_sha256")
    if (
        not isinstance(snapshots, dict)
        or set(snapshots) != {"before.prom", "after.prom"}
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in snapshots.values()
        )
    ):
        raise ValueError("the transfer record needs the SHA-256 of both metric snapshots")
    if not isinstance(record.get("series"), str) or not record["series"]:
        raise ValueError("the transfer record names no measured series")
    return integer(record, "payload_bytes_per_handoff"), {
        "layout": "measured_transfer",
        "measured_series": record["series"],
        "measured_handoffs": integer(record, "handoffs"),
        "transfer_record_sha256": hashlib.sha256(data).hexdigest(),
        "transfer_metrics_sha256": [snapshots["before.prom"], snapshots["after.prom"]],
    }


def budget(
    model_data: bytes,
    launch_data: bytes,
    workload: Workload,
    *,
    runtime_layout: bytes | None = None,
    transfer: bytes | None = None,
) -> dict:
    """Return the budget document of one cache group from its representative's cache source.

    The source is the captured `cache-layout.json`, which must describe this model and launch
    record, or the `transfer.json` of measured handoffs.
    """
    if (runtime_layout is None) == (transfer is None):
        raise ValueError("size the budget from one runtime layout or one transfer record")
    tp = json.loads(launch_data)["tensor_parallel_size"]
    hashes = {
        "model_config_sha256": hashlib.sha256(model_data).hexdigest(),
        "launch_config_sha256": hashlib.sha256(launch_data).hexdigest(),
    }
    if runtime_layout is not None:
        runtime = json.loads(runtime_layout)
        if any(runtime[key] != value for key, value in hashes.items()):
            raise ValueError("runtime cache sizing differs from the model or launch record")
        payload = runtime_payload(runtime, tp, workload.prompt_tokens)
        result = workload_budget(payload, workload)
        result.update(
            layout=runtime["sizing"],
            prompt_tokens=workload.prompt_tokens,
            runtime_layout_sha256=hashlib.sha256(runtime_layout).hexdigest(),
            image=runtime["image"],
            plan_sha256=runtime["plan_sha256"],
        )
        if (name := runtime_layout_name(runtime)) is not None:
            result["kv_cache_layout"] = name
    else:
        assert transfer is not None
        payload, evidence = transfer_record(transfer, workload.prompt_tokens)
        result = workload_budget(payload, workload)
        result.update(prompt_tokens=workload.prompt_tokens, **evidence)
    result.update(tensor_parallel_size=tp, **hashes)
    return result


def received_gbps(sample: dict) -> float:
    """Return the receiver's aggregate rate of an iperf3 TCP sample, in decimal Gbit/s."""
    if sample.get("error"):
        raise ValueError(f"iperf3 reported an error: {sample['error']}")
    protocol = sample["start"]["test_start"]["protocol"]
    if protocol != "TCP":
        raise ValueError(f"the iperf3 sample ran over {protocol}, not TCP")
    received = sample["end"]["sum_received"]
    positive(float(received["seconds"]), "sample duration")
    return positive(float(received["bits_per_second"]), "received bitrate") / 1e9


def rdma_gbps(report: str, message_bytes: int) -> float:
    """Return the average rate of an `ib_write_bw --report_gbits` report, in Gbit/s."""
    lines = [line.strip() for line in report.splitlines()]
    headers = [index for index, line in enumerate(lines) if RDMA_AVERAGE in line]
    if len(headers) != 1:
        raise ValueError(f"the perftest report has no single {RDMA_AVERAGE} column")
    columns = re.split(r"\s{2,}", lines[headers[0]])
    rows = [line.split() for line in lines[headers[0] + 1 :] if line and line[0].isdigit()]
    if len(rows) != 1 or len(rows[0]) != len(columns):
        raise ValueError("the perftest report needs one result row under its header")
    values = dict(zip(columns, rows[0], strict=True))
    if values.get("#bytes") != str(message_bytes):
        raise ValueError(f"the perftest report measured another message size than {message_bytes}")
    return positive(float(values[RDMA_AVERAGE]), "measured_gbps")


def sample_gbps(transport: str, data: bytes, message_bytes: int | None = None) -> float:
    """Return the measured rate of a directed sample of `transport`."""
    if transport == "ucx_tcp":
        return received_gbps(json.loads(data))
    if transport == "ucx_rdma" and message_bytes is not None:
        return rdma_gbps(data.decode(), message_bytes)
    raise ValueError(f"no sample rate for transport {transport}")


LINK_FIELDS = {
    "source_host",
    "destination_host",
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
    """Bind one directed measurement to its hosts, routes and test conditions."""
    if set(link) != LINK_FIELDS:
        raise ValueError("link record requires host, address, interface, route and test fields")
    for name in LINK_FIELDS - {"test_parameters"}:
        if not isinstance(link[name], str) or not link[name].strip():
            raise ValueError(f"link record requires {name}")
    if link["source_host"] == link["destination_host"]:
        raise ValueError("link record requires two distinct hosts")
    if link["transport"] not in TRANSPORTS:
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


def _required(budget_data: bytes) -> float:
    return positive(float(json.loads(budget_data)["required_gbps"]), "required_gbps")


def record_edge(
    link_path: Path, budget_path: Path, sample_path: Path, role: str, out: Path
) -> dict:
    """Write the evidence of a fresh sample against one source role's budget, and return it."""
    link = json.loads(link_path.read_text())
    fingerprint = link_fingerprint(link)
    sample_data = sample_path.read_bytes()
    message_bytes = link["test_parameters"].get("message_bytes")
    measured = sample_gbps(link["transport"], sample_data, message_bytes)
    budget_data = budget_path.read_bytes()
    required = _required(budget_data)
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": EVIDENCE_VERSION,
        "source_role": role,
        "link_sha256": fingerprint,
        "sample_sha256": hashlib.sha256(sample_data).hexdigest(),
        "budget_sha256": hashlib.sha256(budget_data).hexdigest(),
        "measured_gbps": measured,
        "required_gbps": required,
        "passed": measured >= required,
    }
    write_private(out, evidence)
    return evidence


def retained_evidence(evidence: dict, fingerprint: str, sample_data: bytes) -> bool:
    """Whether retained evidence holds for the current link record and its retained sample."""
    return (
        evidence.get("schema") == EVIDENCE_SCHEMA
        and evidence.get("schema_version") == EVIDENCE_VERSION
        and evidence.get("link_sha256") == fingerprint
        and evidence.get("sample_sha256") == hashlib.sha256(sample_data).hexdigest()
    )


def reuse_edge(
    link_path: Path,
    evidence_path: Path,
    sample_path: Path,
    budget_path: Path,
    role: str,
    out: Path,
) -> dict:
    """Write a comparison of a retained sample with one source role's budget, and return it."""
    fingerprint = link_fingerprint(json.loads(link_path.read_text()))
    evidence_data = evidence_path.read_bytes()
    evidence = json.loads(evidence_data)
    if not retained_evidence(evidence, fingerprint, sample_path.read_bytes()):
        raise ValueError("the link record or the retained sample differs from the evidence")
    measured = positive(float(evidence["measured_gbps"]), "measured_gbps")
    budget_data = budget_path.read_bytes()
    required = _required(budget_data)
    comparison = {
        "schema": COMPARISON_SCHEMA,
        "schema_version": EVIDENCE_VERSION,
        "source_role": role,
        "link_sha256": fingerprint,
        "sample_sha256": evidence["sample_sha256"],
        "source_evidence_sha256": hashlib.sha256(evidence_data).hexdigest(),
        "budget_sha256": hashlib.sha256(budget_data).hexdigest(),
        "measured_gbps": measured,
        "required_gbps": required,
        "passed": measured >= required,
    }
    write_private(out, comparison)
    return comparison


def write_private(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
