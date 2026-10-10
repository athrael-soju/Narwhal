from __future__ import annotations

import asyncio
import time
from hashlib import sha256
from typing import cast

from ...backends import load as load_backend
from ...config import FleetConfig
from ...engines.client import EngineClient, EngineError, first_output_timeout
from ...engines.connector import PrefillResult
from ...engines.stream import sse_token_bearing, sse_token_count
from ...engines.validation import can_consume, can_produce, validation_pairs
from .engines import PROBE_PROMPT
from .evidence import pair_snapshot, same_generation
from .report import Report


async def gate_produce(
    cfg: FleetConfig, live: set[str], client: EngineClient, rep: Report
) -> dict[str, PrefillResult]:
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
                async for batch in client.decode(
                    by_id[dst].url,
                    "/v1/completions",
                    attempt,
                    {},
                    params,
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
                    transfer_metric=load_backend(cfg.backend).metrics.transfer_series,
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
