"""Directed KV transfer gates and their evidence."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from hashlib import sha256
from pathlib import Path
from typing import cast

import httpx

from ..config import FleetConfig
from ..engines.attestation import fetch_engine_identity, verify_attestation
from ..engines.client import EngineClient, EngineError, first_output_timeout
from ..engines.connector import PrefillResult
from ..engines.dialect import lookup as lookup_dialect
from ..engines.stream import sse_token_bearing, sse_token_count
from ..engines.validation import can_consume, can_produce, validation_pairs
from .gates import PROBE_PROMPT
from .report import Report


async def gate_produce(
    cfg: FleetConfig, live: set[str], client: EngineClient, rep: Report
) -> dict[str, PrefillResult]:
    """Check that every live engine produces a KV handoff."""
    print("produce")
    handoffs: dict[str, PrefillResult] = {}
    body = {"model": cfg.model, "prompt": PROBE_PROMPT, "max_tokens": 1, "temperature": 0.0}
    for spec in cfg.engines:
        if spec.iid not in live:
            rep.skip(f"{spec.iid} produce: unreachable")
            continue
        try:
            params = await client.prefill(spec.url, "/v1/completions", body, {})
        except EngineError as exc:
            rep.fail(f"{spec.iid} prefill leg: {exc}")
            continue
        handoffs[spec.iid] = params
        rep.ok(f"{spec.iid} returned kv_transfer_params ({', '.join(sorted(params.parameters()))})")
    return handoffs


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
    """Return whether two snapshots describe the same engine process and attestation."""
    return all(
        name in before and name in after and before[name] == after[name]
        for name in ("vllm_version", "process_start_time_seconds", "attestation_digest")
    )


async def gate_consume(
    cfg: FleetConfig,
    live: set[str],
    handoffs: dict[str, PrefillResult],
    client: EngineClient,
    rep: Report,
    mesh: bool,
    repeats: int = 1,
    evidence: list[dict[str, object]] | None = None,
) -> None:
    """Probe role-permitted transfers between distinct engines.

    Ring mode covers each eligible producer and consumer with a peer; mesh mode covers
    every eligible ordered pair.
    """
    print(f"consume ({'mesh' if mesh else 'ring'}, {repeats}x)")
    ids = [s.iid for s in cfg.engines if s.iid in live and s.iid in handoffs]
    if len(ids) < 2:
        rep.skip("consume: fewer than two engines produced a handoff")
        return
    by_id = {s.iid: s for s in cfg.engines}
    producers = [i for i in ids if can_produce(by_id[i])]
    consumers = [i for i in ids if can_consume(by_id[i])]
    if len(producers) < len(ids) or len(consumers) < len(ids):
        # A stalled forbidden transfer can kill the healthy peer's engine core.
        excluded = sorted(set(ids) - set(consumers)) + sorted(set(ids) - set(producers))
        rep.ok(f"pairs excluded by role pins: {', '.join(excluded)} (never cross in production)")
    pairs = validation_pairs([by_id[i] for i in ids], mesh)

    dialect = lookup_dialect(cfg.dialect)
    # A model may end the probe prompt at once.
    body = {
        "model": cfg.model,
        "prompt": PROBE_PROMPT,
        "max_tokens": 4,
        "temperature": 0.0,
        **dialect.decode_probe_extras(4),
    }
    pairs = [pair for pair in pairs for _ in range(max(1, repeats))]
    seen: set[tuple[str, str]] = set()
    for src, dst in pairs:
        record: dict[str, object] = {"producer": src, "consumer": dst}
        decode_started: float | None = None
        first_token_seconds: float | None = None
        deadline: asyncio.Timeout | None = None
        try:
            if evidence is not None:
                before_src = await pair_snapshot(cfg, src)
                before_dst = await pair_snapshot(cfg, dst)
                record["producer_before"] = before_src
                record["consumer_before"] = before_dst
            attempt = {**body, **dialect.cold_probe_extras()}
            deadline = asyncio.timeout(cfg.request_timeout_s)
            async with deadline:
                started = time.monotonic()
                params = await client.prefill(by_id[src].url, "/v1/completions", attempt, {})
                prefill_seconds = time.monotonic() - started
                started = decode_started = time.monotonic()
                tokens = 0
                async for line in client.decode(
                    by_id[dst].url,
                    "/v1/completions",
                    attempt,
                    {},
                    params,
                    first_token_timeout_s=cfg.first_token_timeout_s,
                ):
                    count = sse_token_count(line)
                    tokens += count
                    if first_token_seconds is None and (count or sse_token_bearing(line, dialect)):
                        first_token_seconds = time.monotonic() - started
                decode_seconds = time.monotonic() - started
            record["first_token_seconds"] = first_token_seconds
            if evidence is not None:
                after_src = await pair_snapshot(cfg, src)
                after_dst = await pair_snapshot(cfg, dst)
                record["producer_after"] = after_src
                record["consumer_after"] = after_dst
                if not same_generation(before_src, after_src):
                    raise ValueError(f"{src} process or attestation changed during transfer")
                if not same_generation(before_dst, after_dst):
                    raise ValueError(f"{dst} process or attestation changed during transfer")
                count_delta = cast(float, after_dst["nixl_transfer_count"]) - cast(
                    float, before_dst["nixl_transfer_count"]
                )
                transfer_seconds = cast(float, after_dst["nixl_transfer_seconds_sum"]) - cast(
                    float, before_dst["nixl_transfer_seconds_sum"]
                )
                if count_delta < 1 or transfer_seconds <= 0:
                    raise ValueError(f"{src} -> {dst} produced no observed consumer NIXL transfer")
                descriptor = params.parameters()
                record.update(
                    status="passed",
                    connector=params.connector,
                    transfer_metric="vllm:nixl_xfer_time_seconds",
                    transfer_mode=descriptor.get("transfer_mode"),
                    remote_engine_id=descriptor.get("remote_engine_id"),
                    remote_host=descriptor.get("remote_host"),
                    remote_port=descriptor.get("remote_port"),
                    descriptor_sha256=sha256(params.descriptor_json.encode()).hexdigest(),
                    prefill_seconds=prefill_seconds,
                    decode_seconds=decode_seconds,
                    first_token_seconds=first_token_seconds,
                    nixl_transfer_count_delta=count_delta,
                    nixl_transfer_seconds=transfer_seconds,
                    output_tokens=tokens,
                )
        except EngineError as exc:
            deadline_exceeded = exc.status == 504 and first_output_timeout(exc)
            if deadline_exceeded:
                rep.fail(
                    f"{src} -> {dst}: first-token deadline {cfg.first_token_timeout_s:g}s "
                    "expired; transfer remains unconfirmed. Run first-token calibration."
                )
            else:
                rep.fail(f"{src} -> {dst}: {exc}")
            record.update(
                status="failed",
                failure_kind="deadline_exceeded" if deadline_exceeded else "transfer_failed",
                error=str(exc),
                decode_seconds=(
                    time.monotonic() - decode_started if decode_started is not None else None
                ),
            )
            if evidence is not None:
                evidence.append(record)
            continue
        except TimeoutError as exc:
            expired = deadline is not None and deadline.expired()
            detail = (
                f"request deadline {cfg.request_timeout_s:g}s expired; transfer remains unconfirmed"
                if expired
                else f"TimeoutError: {exc}"
            )
            rep.fail(f"{src} -> {dst}: {detail}")
            record.update(
                status="failed",
                failure_kind="request_deadline_exceeded" if expired else "transfer_failed",
                error=detail,
                first_token_seconds=first_token_seconds,
                decode_seconds=(
                    time.monotonic() - decode_started if decode_started is not None else None
                ),
            )
            if evidence is not None:
                evidence.append(record)
            continue
        except Exception as exc:
            rep.fail(f"{src} -> {dst}: {type(exc).__name__}: {exc}")
            record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            if evidence is not None:
                evidence.append(record)
            continue
        if not tokens:
            rep.fail(f"{src} -> {dst} accepted the handoff and produced no tokens")
            record.update(status="failed", error="consumer produced no tokens")
        elif (src, dst) not in seen:
            seen.add((src, dst))
            timing = (
                f", first token {first_token_seconds:.3f}s"
                if first_token_seconds is not None
                else ""
            )
            rep.ok(f"{src} -> {dst} moved KV and produced {tokens} tokens{timing}")
        if evidence is not None:
            evidence.append(record)


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
