from __future__ import annotations

import asyncio
import time
from hashlib import sha256
from typing import cast

import httpx

from ...backends import load as load_backend
from ...config import FleetConfig
from ...engines.attestation import attested_launches
from ...engines.client import EngineClient, EngineError, first_output_timeout
from ...engines.connector import PrefillResult, RendezvousConnector
from ...engines.stream import sse_token_bearing, sse_token_count
from ...engines.validation import can_consume, can_produce, validation_pairs
from ...runtime.role_switch import engine_side, place_pair
from .engines import PROBE_PROMPT
from .evidence import pair_snapshot, same_generation
from .report import Report


async def gate_produce(
    cfg: FleetConfig, live: set[str], client: EngineClient, rep: Report
) -> dict[str, PrefillResult | None]:
    print("produce")
    handoffs: dict[str, PrefillResult | None] = {}
    if isinstance(client.kv, RendezvousConnector):
        # A rendezvous prefill leg completes only with its decode leg.
        for spec in cfg.engines:
            if spec.iid in live:
                handoffs[spec.iid] = None
                rep.ok(f"{spec.iid} hands off by rendezvous; its consume pairs check the transfer")
            else:
                rep.skip(f"{spec.iid} produce: unreachable")
        return handoffs
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
) -> None:
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
    switcher = engine_side(load_backend(cfg.backend).role_switcher(cfg.connector))
    launches = (
        await attested_launches([by_id[iid] for iid in ids], timeout_s=cfg.health_timeout_s)
        if switcher is not None or isinstance(client.kv, RendezvousConnector)
        else {}
    )
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
            if switcher is not None:
                async with httpx.AsyncClient(
                    timeout=cfg.health_timeout_s, headers=cfg.engine_headers()
                ) as control:
                    await place_pair(
                        switcher,
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
                async for batch in client.decode_handoff(
                    handoff,
                    by_id[dst].url,
                    "/v1/completions",
                    attempt,
                    {},
                    first_token_timeout_s=cfg.first_token_timeout_s,
                ):
                    for event in batch:
                        count = sse_token_count(event)
                        tokens += count
                        if first_token_seconds is None and (
                            count or sse_token_bearing(event, dialect)
                        ):
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
                count_delta = cast(float, after_dst["transfer_count"]) - cast(
                    float, before_dst["transfer_count"]
                )
                transfer_seconds = cast(float, after_dst["transfer_seconds_sum"]) - cast(
                    float, before_dst["transfer_seconds_sum"]
                )
                if count_delta < 1 or transfer_seconds <= 0:
                    raise ValueError(f"{src} -> {dst} produced no observed consumer KV transfer")
                descriptor = handoff.parameters
                record.update(
                    status="passed",
                    connector=handoff.connector,
                    transfer_metric=load_backend(cfg.backend).metrics.transfer_series,
                    transfer_mode=descriptor.get("transfer_mode"),
                    remote_engine_id=descriptor.get("remote_engine_id"),
                    remote_host=descriptor.get("remote_host"),
                    remote_port=descriptor.get("remote_port"),
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
