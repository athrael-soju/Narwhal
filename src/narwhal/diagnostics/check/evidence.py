"""Process-bound directed KV transfer evidence."""

from __future__ import annotations

import json
import math
import os
import re
import time
from hashlib import sha256
from pathlib import Path

import httpx

from ...config import FleetConfig
from ...engines.attestation import fetch_engine_identity, verify_attestation
from ...engines.validation import validation_pairs
from ...profiling.generation import binding_digest, generation_problem
from ...profiling.store import ProfileStore
from .report import Report

_NIXL_TRANSFER = re.compile(
    r"^vllm:nixl_xfer_time_seconds_(count|sum)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$",
    re.MULTILINE,
)


async def pair_snapshot(cfg: FleetConfig, iid: str) -> dict[str, object]:
    """Verify one current engine and retain the NIXL transfer counter."""
    if cfg.engine_contract is None:
        raise ValueError("directed KV evidence requires a declared engine contract")
    spec = next(spec for spec in cfg.engines if spec.iid == iid)
    if not spec.attestation_url:
        raise ValueError(f"{iid} has no attestation URL")
    identity = await fetch_engine_identity(
        spec.url, timeout_s=cfg.health_timeout_s, headers=cfg.engine_headers()
    )
    async with httpx.AsyncClient(timeout=cfg.health_timeout_s) as client:
        attestation = await client.get(spec.attestation_url)
        attestation.raise_for_status()
        payload = attestation.json()
        failures = verify_attestation(payload, cfg.engine_contract, identity)
        if failures:
            raise ValueError(f"{iid} attestation: {'; '.join(failures)}")
        metrics = await client.get(spec.url.rstrip("/") + "/metrics", headers=cfg.engine_headers())
        metrics.raise_for_status()
    transfer = {"count": 0.0, "sum": 0.0}
    for name, value in _NIXL_TRANSFER.findall(metrics.text):
        transfer[name] += float(value)
    if not math.isfinite(transfer["count"]) or not math.isfinite(transfer["sum"]):
        raise ValueError(f"{iid} returned non-finite NIXL transfer metrics")
    if not _NIXL_TRANSFER.search(metrics.text):
        raise ValueError(f"{iid} exposes no NIXL transfer metrics")
    sources = payload["sources"]
    return {
        "iid": iid,
        "vllm_version": identity.vllm_version,
        "process_start_time_seconds": identity.process_start_time_seconds,
        "attestation_digest": payload["attestation_digest"],
        **({"launch_digest": payload["launch_digest"]} if "launch_digest" in payload else {}),
        "contract_fingerprint": cfg.engine_contract.fingerprint(),
        "cache_layout_sources": {
            name: sources[name] for name in ("cross_layers_blocks", "hybrid_kv_cache_manager")
        },
        "connector_source": sources["nixl_connector_version"],
        "transfer_mode_source": sources["transfer_mode"],
        "nixl_transfer_count": transfer["count"],
        "nixl_transfer_seconds_sum": transfer["sum"],
    }


def same_generation(before: dict[str, object], after: dict[str, object]) -> bool:
    """Return whether two snapshots share vLLM version, process start and attestation."""
    return all(
        name in before and name in after and before[name] == after[name]
        for name in ("vllm_version", "process_start_time_seconds", "attestation_digest")
    )


async def verify_directed_kv_evidence(
    cfg: FleetConfig, fleet_path: Path, evidence_path: Path
) -> list[str]:
    """Reject saved mesh results after a process, profile, or fleet change."""
    try:
        document = json.loads(evidence_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"directed KV evidence unreadable: {exc}"]
    if not isinstance(document, dict):
        return ["directed KV evidence must be an object"]
    problems: list[str] = []
    contract = cfg.engine_contract
    if contract is None:
        problems.append("current fleet has no engine contract")
    if (
        document.get("schema") != "narwhal.directed-kv-evidence"
        or document.get("schema_version") != 1
    ):
        problems.append("directed KV evidence schema differs")
    if document.get("status") != "passed" or document.get("failed") or document.get("skipped"):
        problems.append("directed KV evidence contains failed or skipped gates")
    if document.get("fleet_sha256") != sha256(fleet_path.read_bytes()).hexdigest():
        problems.append("fleet changed since directed KV qualification")
    profile_hash = (
        sha256(cfg.profiles_path.read_bytes()).hexdigest() if cfg.profiles_path.exists() else None
    )
    if document.get("profile_sha256") != profile_hash or profile_hash is None:
        problems.append("profiles changed or are absent since directed KV qualification")
    if contract is not None and document.get("contract_fingerprint") != contract.fingerprint():
        problems.append("engine contract changed since directed KV qualification")
    expected = validation_pairs(cfg.engines, mesh=True)
    if document.get("expected_pairs") != [list(pair) for pair in expected]:
        problems.append("eligible directed KV pairs changed")
    rows = document.get("pairs")
    if not isinstance(rows, list):
        problems.append("directed KV evidence has no pair records")
        return problems
    repeats = document.get("repeats")
    if type(repeats) is not int or repeats < 1:
        problems.append("directed KV evidence has no valid repeat count")
        return problems
    if len(rows) != len(expected) * repeats:
        problems.append("directed KV evidence pair count differs from the eligible mesh")
    saved: dict[str, dict[str, object]] = {}
    for src, dst in expected:
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("producer") == src
            and row.get("consumer") == dst
            and row.get("status") == "passed"
        ]
        if len(matches) != repeats:
            problems.append(f"{src} -> {dst} has no complete passing transfer evidence")
        for row in matches:
            if (
                not isinstance(row.get("output_tokens"), int)
                or row["output_tokens"] < 1
                or not isinstance(row.get("nixl_transfer_count_delta"), (int, float))
                or row["nixl_transfer_count_delta"] < 1
                or not isinstance(row.get("nixl_transfer_seconds"), (int, float))
                or row["nixl_transfer_seconds"] <= 0
            ):
                problems.append(f"{src} -> {dst} lacks observed KV transfer and token evidence")
            for side, iid in (("producer", src), ("consumer", dst)):
                before = row.get(f"{side}_before")
                snapshot = row.get(f"{side}_after")
                if not isinstance(before, dict) or not isinstance(snapshot, dict):
                    problems.append(f"{src} -> {dst} has no {side} process evidence")
                    continue
                if not same_generation(before, snapshot):
                    problems.append(f"{iid} process changed during saved transfer")
                previous = saved.get(iid)
                if previous is not None and not same_generation(previous, snapshot):
                    problems.append(f"{iid} process generation differs across pair records")
                saved[iid] = snapshot
    for iid, before in saved.items():
        try:
            current = await pair_snapshot(cfg, iid)
            if not same_generation(before, current):
                problems.append(f"{iid} process or attestation changed since KV qualification")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"{iid} current identity is unavailable: {exc}")
    return problems


async def check_directed_evidence(
    cfg: FleetConfig,
    store: ProfileStore,
    rep: Report,
    repeats: int,
    fleet_path: Path | None,
    fleet_hash: str | None,
    profile_hash: str | None,
) -> None:
    """Fail `rep` when fresh directed KV evidence misses a pair or its inputs changed."""
    expected = validation_pairs(cfg.engines, mesh=True)
    for src, dst in expected:
        passed = sum(
            row.get("producer") == src
            and row.get("consumer") == dst
            and row.get("status") == "passed"
            for row in rep.pairs
        )
        if passed < max(1, repeats):
            rep.fail(f"{src} -> {dst}: no current passing directed KV evidence")
    first_generation: dict[str, dict[str, object]] = {}
    generation_failures: set[str] = set()
    for row in rep.pairs:
        for side in ("producer", "consumer"):
            iid = str(row[side])
            for phase in ("before", "after"):
                snapshot = row.get(f"{side}_{phase}")
                if isinstance(snapshot, dict):
                    first = first_generation.setdefault(iid, snapshot)
                    if not same_generation(first, snapshot):
                        generation_failures.add(
                            f"{iid} process generation differs across directed KV probes"
                        )
    for iid, before in first_generation.items():
        try:
            current = await pair_snapshot(cfg, iid)
            if not same_generation(before, current):
                generation_failures.add(f"{iid} process changed after directed KV probes")
            for profile in store.profiles_for_engine(iid):
                problem = generation_problem(
                    iid,
                    profile.generation_digest,
                    binding_digest(current),
                )
                if problem:
                    generation_failures.add(problem)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            generation_failures.add(f"{iid} current process verification failed: {exc}")
    for problem in sorted(generation_failures):
        rep.fail(problem)
    if fleet_path is None or sha256(fleet_path.read_bytes()).hexdigest() != fleet_hash:
        rep.fail("fleet changed during directed KV qualification")
    current_profile_hash = (
        sha256(cfg.profiles_path.read_bytes()).hexdigest() if cfg.profiles_path.exists() else None
    )
    if current_profile_hash != profile_hash or profile_hash is None:
        rep.fail("profile store changed during directed KV qualification")


def write_directed_evidence(
    cfg: FleetConfig,
    rep: Report,
    repeats: int,
    evidence_out: Path,
    fleet_path: Path | None,
    fleet_hash: str | None,
    profile_hash: str | None,
) -> None:
    """Write the process-bound full-mesh KV evidence document to a fresh file."""
    contract = cfg.engine_contract
    if fleet_path is None or contract is None:
        raise ValueError("directed KV evidence has no fleet file or engine contract")
    document = {
        "schema": "narwhal.directed-kv-evidence",
        "schema_version": 1,
        "captured_at_unix": time.time(),
        "fleet_sha256": fleet_hash,
        "profile_sha256": profile_hash,
        "model": cfg.model,
        "contract_fingerprint": contract.fingerprint(),
        "expected_pairs": [list(pair) for pair in validation_pairs(cfg.engines, mesh=True)],
        "repeats": max(1, repeats),
        "pairs": rep.pairs,
        "failed": rep.failed,
        "skipped": rep.skipped,
        "status": "passed" if not rep.failed and not rep.skipped else "failed",
    }
    evidence_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(evidence_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(document, output, indent=2)
        output.write("\n")
    print(f"directed KV evidence: {evidence_out}")
