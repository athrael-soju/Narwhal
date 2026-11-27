from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, cast

import httpx

from ...backends import load as load_backend
from ...config import FleetConfig
from ...engines.attestation import attested_launches, read_hca_counters
from ...engines.client import EngineClient, EngineError, first_output_timeout
from ...engines.connector import PrefillResult
from ...engines.stream import sse_token_bearing, sse_token_count
from ...engines.validation import can_consume, can_produce, validation_pairs
from .engines import PROBE_PROMPT
from .evidence import pair_snapshot, same_generation
from .report import Report

# The kernel counts an HCA port's transmitted data in words of four octets.
HCA_WORD_BYTES = 4


async def gate_hca(
    cfg: FleetConfig,
    live: set[str],
    rep: Report,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Mapping[str, Any]]:
    """Read the HCA port of each live engine that sends KV through it, even on its own host.

    The sidecar publishes the port only for a launch with no shared-memory device and no CUDA
    IPC, so the consume gate can check that KV between engines of one host crossed the HCA.
    """
    ports: dict[str, Mapping[str, Any]] = {}
    lines: list[tuple[bool, str]] = []
    for spec in cfg.engines:
        if spec.iid not in live or not spec.attestation_url:
            continue
        try:
            reading = await read_hca_counters(
                spec.attestation_url, timeout_s=cfg.health_timeout_s, transport=transport
            )
        except (httpx.HTTPError, ValueError) as exc:
            lines.append((False, f"{spec.iid} HCA counters: {exc}"))
            continue
        if reading is not None:
            ports[spec.iid] = reading
            lines.append((True, f"{spec.iid} sends KV through HCA {_port(reading)}"))
    if lines:
        print("hca")
    for passed, line in lines:
        if passed:
            rep.ok(line)
        else:
            rep.fail(line)
    return ports


def _port(reading: Mapping[str, Any]) -> str:
    return f"{reading['device']}:{reading['port']}"


def _same_host(hca: Mapping[str, Mapping[str, Any]], src: str, dst: str) -> bool:
    return src in hca and dst in hca and hca[src]["boot_id"] == hca[dst]["boot_id"]


async def gate_produce(
    cfg: FleetConfig, live: set[str], client: EngineClient, rep: Report
) -> dict[str, PrefillResult | None]:
    print("produce")
    handoffs: dict[str, PrefillResult | None] = {}
    if client.kv.concurrent:
        # A concurrent prefill leg completes only with its decode leg.
        for spec in cfg.engines:
            if spec.iid in live:
                handoffs[spec.iid] = None
                rep.ok(f"{spec.iid} hands off by rendezvous; its consume pairs check the transfer")
            else:
                rep.skip(f"{spec.iid} produce: unreachable")
        return handoffs
    # A model may end the probe prompt at once, and a leg that ends there hands off nothing.
    body = {
        "model": cfg.model,
        "prompt": PROBE_PROMPT,
        "max_tokens": 1,
        "temperature": 0.0,
        **load_backend(cfg.backend).dialect.decode_probe_extras(1),
    }
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
        rep.ok(f"{spec.iid} returned handoff parameters ({', '.join(sorted(params.parameters()))})")
    return handoffs


async def gate_consume(
    cfg: FleetConfig,
    live: set[str],
    handoffs: dict[str, PrefillResult | None],
    client: EngineClient,
    rep: Report,
    mesh: bool,
    repeats: int = 1,
    evidence: list[dict[str, object]] | None = None,
    hca: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Probe each selected pair's KV handoff.

    `hca` holds the readings of `gate_hca`. A pair of those engines on one host must raise the
    producer's HCA transmit counter, which shows the KV crossed the HCA.
    """
    hca = hca or {}
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

    dialect = load_backend(cfg.backend).dialect
    # A model may end the probe prompt at once.
    body = {
        "model": cfg.model,
        "prompt": PROBE_PROMPT,
        "max_tokens": 4,
        "temperature": 0.0,
        **dialect.decode_probe_extras(4),
    }
    pairs = [pair for pair in pairs for _ in range(max(1, repeats))]
    switcher = load_backend(cfg.backend).role_switcher(cfg.connector)
    launches = (
        await attested_launches([by_id[iid] for iid in ids], timeout_s=cfg.health_timeout_s)
        if switcher.engine_held or client.kv.concurrent
        else {}
    )
    seen: set[tuple[str, str]] = set()
    hca_checked: set[tuple[str, str]] = set()
    for src, dst in pairs:
        record: dict[str, object] = {"producer": src, "consumer": dst}
        decode_started: float | None = None
        first_token_seconds: float | None = None
        deadline: asyncio.Timeout | None = None
        hca_before: Mapping[str, Any] | None = None
        hca_bytes: int | None = None
        try:
            if _same_host(hca, src, dst):
                hca_checked.add((src, dst))
                hca_before = await _hca_reading(cfg, by_id[src].attestation_url, hca[src])
            if evidence is not None:
                before_src = await pair_snapshot(cfg, src)
                before_dst = await pair_snapshot(cfg, dst)
                record["producer_before"] = before_src
                record["consumer_before"] = before_dst
            if switcher.engine_held:
                async with httpx.AsyncClient(
                    timeout=cfg.health_timeout_s, headers=cfg.engine_headers()
                ) as control:
                    await switcher.place(
                        control,
                        (by_id[src].url, launches.get(src)),
                        (by_id[dst].url, launches.get(dst)),
                    )
            attempt = {**body, **dialect.cold_probe_extras()}
            deadline = asyncio.timeout(cfg.request_timeout_s)
            async with deadline:
                handoff = await client.start_handoff(
                    by_id[src].url, "/v1/completions", attempt, {}, producer=launches.get(src)
                )
                started = decode_started = time.monotonic()
                tokens = 0
                # An engine without a transfer counter reports the leg's transfer in its reply.
                reported = evidence is not None and bool(dialect.transfer_report_headers)
                payloads: list[dict[str, Any]] = []
                async for batch in client.decode_handoff(
                    handoff,
                    by_id[dst].url,
                    "/v1/completions",
                    attempt,
                    dict(dialect.transfer_report_headers) if reported else {},
                    first_token_timeout_s=cfg.first_token_timeout_s,
                    trailer=reported,
                ):
                    for event in batch:
                        if event.data is not None:
                            payloads.append(event.data)
                        count = sse_token_count(event)
                        tokens += count
                        if first_token_seconds is None and (
                            count or sse_token_bearing(event, dialect)
                        ):
                            first_token_seconds = time.monotonic() - started
                decode_seconds = time.monotonic() - started
            record["first_token_seconds"] = first_token_seconds
            if hca_before is not None:
                after = await _hca_reading(cfg, by_id[src].attestation_url, hca_before)
                hca_bytes = HCA_WORD_BYTES * (
                    after["port_xmit_data"] - hca_before["port_xmit_data"]
                )
                record["hca_transmitted_bytes"] = hca_bytes
            if evidence is not None:
                after_src = await pair_snapshot(cfg, src)
                after_dst = await pair_snapshot(cfg, dst)
                record["producer_after"] = after_src
                record["consumer_after"] = after_dst
                if not same_generation(before_src, after_src):
                    raise ValueError(f"{src} process or attestation changed during transfer")
                if not same_generation(before_dst, after_dst):
                    raise ValueError(f"{dst} process or attestation changed during transfer")
                if before_dst["transfer_count"] is None:
                    leg_seconds = dialect.leg_transfer_seconds(payloads)
                    count_delta = 1.0 if leg_seconds is not None else 0.0
                    transfer_seconds = leg_seconds or 0.0
                else:
                    count_delta = cast(float, after_dst["transfer_count"]) - cast(
                        float, before_dst["transfer_count"]
                    )
                    transfer_seconds = cast(float, after_dst["transfer_seconds_sum"]) - cast(
                        float, before_dst["transfer_seconds_sum"]
                    )
                if count_delta < 1 or transfer_seconds <= 0:
                    raise ValueError(f"{src} -> {dst} produced no observed consumer KV transfer")
                record.update(
                    status="passed",
                    connector=handoff.connector,
                    transfer_metric=load_backend(cfg.backend).metrics.transfer_series,
                    **handoff.diagnostics(),
                    descriptor_sha256=sha256(handoff.descriptor_json.encode()).hexdigest(),
                    prefill_seconds=handoff.prefill_seconds,
                    decode_seconds=decode_seconds,
                    first_token_seconds=first_token_seconds,
                    transfer_count_delta=count_delta,
                    transfer_seconds=transfer_seconds,
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
        elif hca_bytes is not None and hca_bytes <= 0:
            problem = (
                f"{src} -> {dst}: same-host KV left no traffic on {src}'s HCA "
                f"{_port(hca[src])}, so UCX moved it through shared memory or CUDA IPC"
            )
            rep.fail(problem)
            record.update(status="failed", error=problem)
        elif (src, dst) not in seen:
            seen.add((src, dst))
            timing = (
                f", first token {first_token_seconds:.3f}s"
                if first_token_seconds is not None
                else ""
            )
            rep.ok(f"{src} -> {dst} moved KV and produced {tokens} tokens{timing}")
            if hca_bytes is not None:
                rep.ok(f"{src} -> {dst} sent {hca_bytes} bytes through {src}'s HCA")
        if evidence is not None:
            evidence.append(record)
    _skip_unprobed_hca(cfg, ids, hca, hca_checked, rep)


async def _hca_reading(
    cfg: FleetConfig, attestation_url: str, expected: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Read an engine's HCA counter again, requiring the port and host of its first reading."""
    reading = await read_hca_counters(attestation_url, timeout_s=cfg.health_timeout_s)
    if reading is None or any(
        reading[name] != expected[name] for name in ("device", "port", "boot_id")
    ):
        raise ValueError(f"{attestation_url} no longer publishes HCA {_port(expected)}")
    return reading


def _skip_unprobed_hca(
    cfg: FleetConfig,
    ids: list[str],
    hca: Mapping[str, Mapping[str, Any]],
    checked: set[tuple[str, str]],
    rep: Report,
) -> None:
    """Skip each engine whose same-host HCA path the selected pairs never carried KV over."""
    by_id = {spec.iid: spec for spec in cfg.engines}
    eligible = validation_pairs([by_id[iid] for iid in ids], mesh=True)
    for src in sorted({src for src, dst in eligible if _same_host(hca, src, dst)}):
        if any(pair[0] == src for pair in checked):
            continue
        peers = ", ".join(dst for s, dst in eligible if s == src and _same_host(hca, src, dst))
        rep.skip(
            f"{src} HCA path: no probe sent KV from {src} to {peers} on its host; "
            "run the full mesh to check it"
        )
