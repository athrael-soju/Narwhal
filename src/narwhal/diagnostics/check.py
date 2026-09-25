"""Run fleet preflight gates from cheap health checks through KV transfer."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import cast

import httpx

from .. import command_results as results
from ..cli_support import add_version_argument
from ..config import FleetConfig
from ..contracts import manifest
from ..engines.attestation import fetch_engine_identity, verify_attestation
from ..engines.client import EngineClient, EngineError
from ..engines.connector import PrefillResult
from ..engines.connector import lookup as lookup_connector
from ..engines.dialect import lookup as lookup_dialect
from ..engines.validation import can_consume, can_produce, validation_pairs
from ..profiling.generation import generation_problem, read_generation
from ..profiling.model import decode_evidence_problems
from ..profiling.probe import engine_context_limit, make_prompt
from ..profiling.store import ProfileStore
from ..types import Role

PROBE_PROMPT = "benchmark " * 64


@dataclass
class Report:
    """Print gate outcomes and collect failures and skips."""

    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    pairs: list[dict[str, object]] = field(default_factory=list)

    def ok(self, msg: str) -> None:
        """Print a passing gate result."""
        print(f"  ok    {msg}")

    def fail(self, msg: str) -> None:
        """Print and record a failed gate result."""
        print(f"  FAIL  {msg}")
        self.failed.append(msg)

    def skip(self, msg: str) -> None:
        """Print and record a skipped gate result."""
        print(f"  SKIP  {msg}")
        self.skipped.append(msg)


async def gate_reach(cfg: FleetConfig, client: EngineClient, rep: Report) -> set[str]:
    """Return engine IDs whose health endpoint answers successfully."""
    print("reach")
    live: set[str] = set()
    for spec in cfg.engines:
        if await client.healthy(spec.url):
            rep.ok(f"{spec.iid} /health")
            live.add(spec.iid)
        else:
            rep.fail(f"{spec.iid} /health did not answer 200")
    return live


async def gate_contract(
    cfg: FleetConfig,
    live: set[str],
    rep: Report,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
    """Return engines whose live identity violates the fleet contract."""
    print("contract")
    declared = cfg.engine_contract
    if declared is None:
        rep.skip("no engine_contract declared; checking live vLLM versions only")
    else:
        rep.ok(f"declared engine contract {declared.fingerprint()}")
        missing = declared.missing()
        if missing:
            rep.skip(f"contract {declared.fingerprint()} undeclared: {', '.join(missing)}")

    observed: dict[str, str] = {}
    unsafe: set[str] = set()
    async with httpx.AsyncClient(timeout=cfg.health_timeout_s, transport=transport) as client:
        for spec in cfg.engines:
            if spec.iid not in live:
                rep.skip(f"{spec.iid} contract: unreachable")
                continue
            try:
                if declared is None:
                    response = await client.get(f"{spec.url}/version", headers=cfg.engine_headers())
                    response.raise_for_status()
                    version = response.json()["version"]
                    if not isinstance(version, str) or not version.strip():
                        raise ValueError("version is empty")
                    identity = None
                else:
                    identity = await fetch_engine_identity(
                        spec.url,
                        timeout_s=cfg.health_timeout_s,
                        transport=transport,
                        headers=cfg.engine_headers(),
                    )
                    version = identity.vllm_version
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                route = "/version and /metrics" if declared is not None else "/version"
                message = f"{spec.iid} {route} unreadable: {type(exc).__name__}"
                if declared is not None:
                    rep.fail(message)
                    unsafe.add(spec.iid)
                else:
                    rep.skip(message)
                continue
            observed[spec.iid] = version
            rep.ok(f"{spec.iid} vLLM {version}")
            if declared is not None and version != declared.vllm_version:
                rep.fail(
                    f"{spec.iid} vLLM {version}, expected {declared.vllm_version} "
                    f"from contract {declared.fingerprint()}"
                )
                unsafe.add(spec.iid)
                continue
            if declared is None or identity is None:
                continue
            if not spec.attestation_url:
                rep.fail(f"{spec.iid} has no attestation_url")
                unsafe.add(spec.iid)
                continue
            try:
                attestation = await client.get(spec.attestation_url)
                attestation.raise_for_status()
                payload = attestation.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                rep.fail(f"{spec.iid} attestation unreadable: {type(exc).__name__}")
                unsafe.add(spec.iid)
                continue
            failures = verify_attestation(payload, declared, identity)
            if failures:
                rep.fail(f"{spec.iid} attestation: {'; '.join(failures)}")
                unsafe.add(spec.iid)
            else:
                rep.ok(
                    f"{spec.iid} contract {declared.fingerprint()} attested for process "
                    f"{identity.process_start_time_seconds:.6f}"
                )

    versions = set(observed.values())
    if len(versions) > 1:
        version_detail = ", ".join(f"{iid}={version}" for iid, version in sorted(observed.items()))
        rep.fail(f"mixed vLLM versions before KV transfer: {version_detail}")
        unsafe.update(observed)
    return unsafe


async def gate_model(
    cfg: FleetConfig,
    live: set[str],
    rep: Report,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
    """List live engines missing the configured model."""
    print("model")
    incompatible: set[str] = set()
    async with httpx.AsyncClient(
        timeout=15.0, transport=transport, headers=cfg.engine_headers()
    ) as c:
        for spec in cfg.engines:
            if spec.iid not in live:
                rep.skip(f"{spec.iid} model: unreachable")
                continue
            try:
                r = await c.get(f"{spec.url}/v1/models")
                names = [m["id"] for m in r.json().get("data", [])]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                rep.fail(f"{spec.iid} /v1/models unreadable: {type(exc).__name__}")
                incompatible.add(spec.iid)
                continue
            if cfg.model in names:
                rep.ok(f"{spec.iid} serves {cfg.model}")
            else:
                # Cross-model KV reuse can corrupt output without an engine error.
                rep.fail(f"{spec.iid} serves {names}; expected {cfg.model}")
                incompatible.add(spec.iid)
    return incompatible


PACE_PROMPT = "benchmark " * 4096


async def gate_pace(
    cfg: FleetConfig,
    live: set[str],
    rep: Report,
    store: ProfileStore | None = None,
    tolerance: float = 1.5,
    repeats: int = 2,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
    """Find live engines whose prefill pace exceeds the allowed tolerance.

    The minimum of `repeats` filters one scheduling delay. Each engine is
    compared with the fleet median and, when available, its saved profile.
    Fewer than three engines require individual profiles to establish pace.

    KV gates must skip failed engines because a stalled transfer can terminate
    a healthy peer's engine core.
    """
    print("pace")
    base_body = {
        "model": cfg.model,
        "prompt": PACE_PROMPT,
        "max_tokens": 1,
        "temperature": 0.0,
        "stream": False,
    }
    times: dict[str, float] = {}
    lens: dict[str, int] = {}
    failed_probes: set[str] = set()
    async with httpx.AsyncClient(
        timeout=cfg.prefill_timeout_s, transport=transport, headers=cfg.engine_headers()
    ) as c:
        for spec in cfg.engines:
            if spec.iid not in live:
                rep.skip(f"{spec.iid} pace: unreachable")
                continue
            body = base_body.copy()
            best = None
            for _ in range(repeats):
                start = asyncio.get_event_loop().time()
                try:
                    r = await c.post(f"{spec.url}/v1/completions", json=body)
                except httpx.HTTPError as exc:
                    rep.fail(f"{spec.iid} pace probe: {type(exc).__name__}")
                    failed_probes.add(spec.iid)
                    best = None
                    break
                if r.status_code == 400 and body["prompt"] == PACE_PROMPT:
                    try:
                        dialect = lookup_dialect(cfg.dialect)
                        limit = await engine_context_limit(c, spec.url, cfg.model, dialect)
                        if limit < 2:
                            raise ValueError(f"engine context limit {limit} leaves no output token")
                        prompt, count = await make_prompt(
                            c, spec.url, cfg.model, min(4096, limit - 1), dialect
                        )
                        if count + 1 > limit:
                            raise ValueError(
                                f"pace prompt {count} plus one output exceeds context {limit}"
                            )
                        body["prompt"] = prompt
                        start = asyncio.get_event_loop().time()
                        r = await c.post(f"{spec.url}/v1/completions", json=body)
                    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                        rep.fail(f"{spec.iid} pace probe: 400; context adaptation failed: {exc}")
                        failed_probes.add(spec.iid)
                        best = None
                        break
                elapsed = asyncio.get_event_loop().time() - start
                if r.status_code != 200:
                    rep.fail(f"{spec.iid} pace probe: {r.status_code}")
                    failed_probes.add(spec.iid)
                    best = None
                    break
                if best is None or elapsed < best:
                    best = elapsed
                    lens.pop(spec.iid, None)
                    with contextlib.suppress(ValueError, KeyError, TypeError):
                        count = r.json()["usage"]["prompt_tokens"]
                        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                            lens[spec.iid] = count
            if best is not None:
                times[spec.iid] = best

    slow: set[str] = set(failed_probes)
    if len(times) >= 3:
        ordered = sorted(times.values())
        median = ordered[len(ordered) // 2]
        for iid, t in sorted(times.items()):
            if t > tolerance * median:
                slow.add(iid)
                rep.fail(
                    f"{iid} pace: {t:.2f}s against a fleet median of {median:.2f}s "
                    f"(> {tolerance:g}x) - throttle or degradation exceeds observed fleet pace"
                )
            else:
                rep.ok(f"{iid} pace {t:.2f}s (median {median:.2f}s)")

    profiled: set[str] = set()
    if store is not None and len(store):
        for iid, t in sorted(times.items()):
            profile = store.get(iid)
            if profile is None:
                continue
            if iid not in lens:
                slow.add(iid)
                rep.fail(f"{iid} pace: profile comparison requires exact usage.prompt_tokens")
                continue
            want = profile.prefill_time(lens[iid])
            if not math.isfinite(want) or want <= 0:
                slow.add(iid)
                rep.fail(f"{iid} pace: profile prediction must be positive and finite")
                continue
            profiled.add(iid)
            if t > tolerance * want:
                slow.add(iid)
                rep.fail(
                    f"{iid} pace: {t:.2f}s against its own profile's {want:.2f}s "
                    f"(> {tolerance:g}x) - the fit no longer describes this engine"
                )
            else:
                rep.ok(f"{iid} pace {t:.2f}s (own profile {want:.2f}s)")
    if 0 < len(times) < 3 and (missing := set(times) - profiled):
        rep.skip(
            "pace needs 3 engines or individual profiles with exact prompt lengths; "
            f"missing evidence for {', '.join(sorted(missing))}"
        )
    return slow


async def gate_tokenize(
    cfg: FleetConfig, live: set[str], client: EngineClient, rep: Report
) -> None:
    """Check exact token counting with the router's configured timeout."""
    print("tokenize")
    route = client.dialect.tokenize_path
    body = {"model": cfg.model, "prompt": PROBE_PROMPT}
    for spec in cfg.engines:
        if spec.iid not in live:
            rep.skip(f"{spec.iid} tokenize: unreachable")
            continue
        if route is None:
            rep.skip(
                f"{spec.iid} the {client.dialect.name} dialect has no exact-count route; "
                "prefill cost uses the character estimate"
            )
            continue
        n = await client.token_count(spec.url, body, cfg.tokenize_timeout_s)
        if n:
            rep.ok(f"{spec.iid} {route} -> {n} tokens")
        else:
            # Character-ratio error is squared by the quadratic prefill model.
            rep.skip(f"{spec.iid} no {route}; prefill cost falls back to a character estimate")


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


async def _pair_snapshot(cfg: FleetConfig, iid: str) -> dict[str, object]:
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
        "contract_fingerprint": cfg.engine_contract.fingerprint(),
        "cache_layout_sources": {
            name: sources[name] for name in ("cross_layers_blocks", "hybrid_kv_cache_manager")
        },
        "connector_source": sources["nixl_connector_version"],
        "transfer_mode_source": sources["transfer_mode"],
        "nixl_transfer_count": transfer["count"],
        "nixl_transfer_seconds_sum": transfer["sum"],
    }


def _same_generation(before: dict[str, object], after: dict[str, object]) -> bool:
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

    Ring mode covers each eligible producer and consumer with a peer. Mesh mode
    covers every eligible ordered pair. Repeat each pair the requested number of times.
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
        # Probe only transfers that role pins permit in production. A stalled
        # forbidden transfer can kill the healthy peer's engine core.
        excluded = sorted(set(ids) - set(consumers)) + sorted(set(ids) - set(producers))
        rep.ok(f"pairs excluded by role pins: {', '.join(excluded)} (never cross in production)")
    pairs = validation_pairs([by_id[i] for i in ids], mesh)

    body = {"model": cfg.model, "prompt": PROBE_PROMPT, "max_tokens": 4, "temperature": 0.0}
    pairs = [pair for pair in pairs for _ in range(max(1, repeats))]
    seen: set[tuple[str, str]] = set()
    for src, dst in pairs:
        record: dict[str, object] = {"producer": src, "consumer": dst}
        try:
            if evidence is not None:
                before_src = await _pair_snapshot(cfg, src)
                before_dst = await _pair_snapshot(cfg, dst)
                record["producer_before"] = before_src
                record["consumer_before"] = before_dst
            started = time.monotonic()
            params = await client.prefill(by_id[src].url, "/v1/completions", body, {})
            prefill_seconds = time.monotonic() - started
            started = time.monotonic()
            tokens = 0
            async for line in client.decode(
                by_id[dst].url,
                "/v1/completions",
                body,
                {},
                params,
                first_token_timeout_s=cfg.first_token_timeout_s,
            ):
                from ..engines.stream import sse_token_count

                tokens += sse_token_count(line)
            decode_seconds = time.monotonic() - started
            if evidence is not None:
                after_src = await _pair_snapshot(cfg, src)
                after_dst = await _pair_snapshot(cfg, dst)
                record["producer_after"] = after_src
                record["consumer_after"] = after_dst
                if not _same_generation(before_src, after_src):
                    raise ValueError(f"{src} process or attestation changed during transfer")
                if not _same_generation(before_dst, after_dst):
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
                    nixl_transfer_count_delta=count_delta,
                    nixl_transfer_seconds=transfer_seconds,
                    output_tokens=tokens,
                )
        except EngineError as exc:
            rep.fail(f"{src} -> {dst}: {exc}")
            record.update(status="failed", error=str(exc))
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
            rep.ok(f"{src} -> {dst} moved KV and produced {tokens} tokens")
        if evidence is not None:
            evidence.append(record)


def _bind_configured_mix(store: ProfileStore, cfg: FleetConfig) -> ProfileStore:
    groups = {
        spec.iid: spec.shared_device.group for spec in cfg.engines if spec.shared_device is not None
    }
    if groups:
        roles = {spec.iid: spec.role for spec in cfg.engines}
        mixes = {
            group: (
                sum(
                    role is Role.PREFILL for iid, role in roles.items() if groups.get(iid) == group
                ),
                sum(role is Role.DECODE for iid, role in roles.items() if groups.get(iid) == group),
            )
            for group in set(groups.values())
        }
        store.bind_role_mix(groups, mixes.__getitem__, roles.__getitem__)
    return store


def gate_profile(cfg: FleetConfig, rep: Report) -> ProfileStore:
    """Check the store binds the fleet and meets the decode error policy.

    Every row must span a measured decode domain on both axes and stay
    inside the fleet's profile-validation error limits.
    """
    print("profile")
    store = _bind_configured_mix(ProfileStore(cfg.profiles_path), cfg)
    policy = cfg.profile_validation
    for spec in cfg.engines:
        p = store.get(spec.iid)
        if p is None:
            rep.fail(f"{spec.iid} has no profile; run narwhal-profile --fleet <config>")
            continue
        problems = decode_evidence_problems(
            p,
            max_fit_mape=policy.max_decode_fit_mape,
            max_cv_mape=policy.max_decode_cv_mape,
        )
        for problem in problems:
            rep.fail(problem)
        if problems:
            continue
        quadratic = f"{p.ttft_a:.2e}n^2+{p.ttft_b:.2e}n+{p.ttft_c:.4f}"
        interval = f"{p.tpot_request_slope:.2e}q+{p.tpot_slope:.2e}b+{p.tpot_intercept:.4f}"
        rep.ok(
            f"{spec.iid} ttft={quadratic} tpot={interval} "
            f"decode_fit_mape={p.decode_fit_mape:.4f} "
            f"decode_cv_mape={p.decode_cv_mape:.4f} within the "
            f"{policy.max_decode_fit_mape:g}/{policy.max_decode_cv_mape:g} limits"
        )
    for iid in store.engine_set_diff(spec.iid for spec in cfg.engines)[1]:
        rep.fail(
            f"{iid} has a profile but no configured engine; remove the stale row or "
            f"re-run narwhal-profile --fleet <config>"
        )
    return store


async def gate_profile_generation(
    cfg: FleetConfig,
    store: ProfileStore,
    live: set[str],
    rep: Report,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
    """Fence profiles whose measured engine generation differs from the live one."""
    unsafe: set[str] = set()
    for spec in cfg.engines:
        profiles = store.profiles_for_engine(spec.iid)
        if not profiles or spec.iid not in live:
            continue
        try:
            generation = await read_generation(
                spec,
                cfg.engine_contract,
                timeout_s=cfg.health_timeout_s,
                headers=cfg.engine_headers(),
                transport=transport,
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            rep.fail(f"{spec.iid} profile generation unreadable: {exc}; reprofile before admission")
            unsafe.add(spec.iid)
            continue
        problems = [
            problem
            for profile in profiles
            if (
                problem := generation_problem(
                    spec.iid, profile.generation_digest, generation.digest
                )
            )
        ]
        for problem in dict.fromkeys(problems):
            rep.fail(problem)
            unsafe.add(spec.iid)
        if not problems:
            rep.ok(f"{spec.iid} profile generation {generation.digest}")
    return unsafe


def gate_slo(cfg: FleetConfig, store: ProfileStore, rep: Report) -> None:
    """Price the smallest measured decode cohort against the configured targets."""
    print("slo")
    for spec in cfg.engines:
        p = store.get(spec.iid)
        if p is None:
            rep.skip(f"{spec.iid} slo: no profile")
            continue
        requests = p.decode_min_requests
        tokens = p.decode_min_kv_tokens
        if requests is None or tokens is None:
            rep.fail(f"{spec.iid} TPOT qualification requires measured request and KV bounds")
            continue
        interval = p.token_interval(tokens, requests)
        if interval > cfg.slo.tpot_s:
            rep.fail(
                f"{spec.iid} tpot {interval * 1000:.1f} ms at {requests} requests and "
                f"{tokens} KV tokens exceeds the {cfg.slo.tpot_s * 1000:.1f} ms target"
            )
            continue
        headroom = p.max_tokens(cfg.slo.tpot_s, requests)
        if cfg.slo.ttft_s <= p.prefill_time(1):
            rep.fail(f"{spec.iid} ttft target is below its own single-token prefill time")
            continue
        rep.ok(
            f"{spec.iid} holds {headroom:.0f} batch tokens across {requests} requests "
            "at the TPOT target"
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
                if not _same_generation(before, snapshot):
                    problems.append(f"{iid} process changed during saved transfer")
                previous = saved.get(iid)
                if previous is not None and not _same_generation(previous, snapshot):
                    problems.append(f"{iid} process generation differs across pair records")
                saved[iid] = snapshot
    for iid, before in saved.items():
        try:
            current = await _pair_snapshot(cfg, iid)
            if not _same_generation(before, current):
                problems.append(f"{iid} process or attestation changed since KV qualification")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"{iid} current identity is unavailable: {exc}")
    return problems


async def run(
    cfg: FleetConfig,
    mesh: bool,
    skip_kv: bool,
    repeats: int = 1,
    *,
    report: Report | None = None,
    evidence_out: Path | None = None,
    fleet_path: Path | None = None,
) -> int:
    """Run every preflight gate and return a process exit code."""
    if evidence_out is not None:
        if not mesh or skip_kv or cfg.engine_contract is None or fleet_path is None:
            raise ValueError(
                "directed KV evidence requires a fleet file, full mesh, and engine contract"
            )
        if evidence_out.exists():
            raise ValueError(f"directed KV evidence already exists: {evidence_out}")
    fleet_hash = sha256(fleet_path.read_bytes()).hexdigest() if fleet_path is not None else None
    profile_hash = (
        sha256(cfg.profiles_path.read_bytes()).hexdigest()
        if evidence_out is not None and cfg.profiles_path.exists()
        else None
    )
    print(f"fleet: {len(cfg.engines)} engines, model {cfg.model}")
    print(f"slo:   ttft <= {cfg.slo.ttft_s}s, tpot <= {cfg.slo.tpot_s}s")
    rep = report or Report()
    # Use serving timeouts so preflight exercises the recorded configuration.
    client = EngineClient(
        timeout_s=cfg.request_timeout_s,
        prefill_timeout_s=cfg.prefill_timeout_s,
        read_timeout_s=cfg.decode_read_timeout_s,
        max_connections=cfg.max_connections,
        control_connections=cfg.resolved_control_connections(),
        pool_timeout_s=cfg.pool_timeout_s,
        connect_timeout_s=cfg.connect_timeout_s,
        health_timeout_s=cfg.health_timeout_s,
        kv=lookup_connector(cfg.connector),
        dialect=lookup_dialect(cfg.dialect),
        model=cfg.model,
        engine_api_key=cfg.resolve_engine_key(),
    )
    try:
        live = await gate_reach(cfg, client, rep)
        incompatible = await gate_contract(cfg, live, rep)
        store = gate_profile(cfg, rep)
        stale = await gate_profile_generation(cfg, store, live, rep)
        incompatible.update(stale)
        incompatible.update(await gate_model(cfg, live, rep))
        pace_store = store if not stale else None
        slow = await gate_pace(cfg, live, rep, pace_store)
        await gate_tokenize(cfg, live, client, rep)
        if skip_kv:
            rep.skip("produce and consume: skipped by --no-kv")
        elif slow or incompatible:
            # Protect healthy peers from transfers through a degraded engine.
            blocked = slow | incompatible
            names = ", ".join(sorted(blocked))
            rep.skip(f"produce and consume: pre-transfer gate failed on {names}")
        else:
            handoffs = await gate_produce(cfg, live, client, rep)
            if evidence_out is None:
                await gate_consume(cfg, live, handoffs, client, rep, mesh, repeats)
            else:
                await gate_consume(
                    cfg, live, handoffs, client, rep, mesh, repeats, evidence=rep.pairs
                )
        gate_slo(cfg, store, rep)
        if evidence_out is not None:
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
                            if not _same_generation(first, snapshot):
                                generation_failures.add(
                                    f"{iid} process generation differs across directed KV probes"
                                )
            for iid, before in first_generation.items():
                try:
                    current = await _pair_snapshot(cfg, iid)
                    if not _same_generation(before, current):
                        generation_failures.add(f"{iid} process changed after directed KV probes")
                    for profile in store.profiles_for_engine(iid):
                        problem = generation_problem(
                            iid, profile.generation_digest, str(current["attestation_digest"])
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
                sha256(cfg.profiles_path.read_bytes()).hexdigest()
                if cfg.profiles_path.exists()
                else None
            )
            if current_profile_hash != profile_hash or profile_hash is None:
                rep.fail("profile store changed during directed KV qualification")
    finally:
        await client.aclose()

    if evidence_out is not None:
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

    results.set_data({"failed": rep.failed, "skipped": rep.skipped, "pairs": rep.pairs})
    for problem in rep.failed:
        results.record_error("gate_failed", problem, stage="preflight")
    if rep.failed or (rep.skipped and evidence_out is not None):
        results.set_status("failed_gate")
    elif rep.skipped:
        results.set_status("degraded")
        results.record_error(
            "gates_skipped", "Preflight completed its selected gates", stage="preflight"
        )
    print()
    if rep.failed:
        print(f"{len(rep.failed)} gate(s) failed, {len(rep.skipped)} skipped")
        return 1
    if rep.skipped and evidence_out is not None:
        print(f"directed KV evidence incomplete: {len(rep.skipped)} gate(s) skipped")
        return 1
    if rep.skipped:
        print(f"all gates pass, {len(rep.skipped)} skipped")
        return 0
    print("all gates pass")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the fleet-check CLI."""
    return results.invoke("narwhal-check", argv, _main, operation="check")


def _main(argv: list[str]) -> int:
    """Parse a fleet-check operation."""
    ap = argparse.ArgumentParser(
        description="Check running engines and their measured profiles before starting the "
        "router. By default, probe the full eligible directed KV mesh; retain it with "
        "--evidence-out or verify a saved mesh with --verify-evidence.",
    )
    add_version_argument(ap)
    results.add_format(ap)
    ap.add_argument(
        "--fleet", help="fleet config JSON; required for preflight and evidence verification"
    )
    ap.add_argument(
        "--ring",
        action="store_true",
        help="test rotating producer-consumer pairs (default: full eligible directed mesh)",
    )
    ap.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="KV transfer probes per pair, clamped to at least 1 (default: %(default)s)",
    )
    ap.add_argument(
        "--no-kv",
        action="store_true",
        help="run reach, contract, profile, model, pace, tokenize and slo gates "
        "(default: include produce and consume)",
    )
    ap.add_argument(
        "--evidence-out",
        type=Path,
        help="fresh JSON path for process-bound full-mesh KV evidence; requires "
        "engine_contract and full KV mesh; exclusive with --ring, --no-kv and --verify-evidence "
        "(default: gate output only)",
    )
    ap.add_argument(
        "--verify-evidence",
        type=Path,
        help="verify saved KV evidence against the current fleet, profiles and live processes; "
        "requires engine_contract; exclusive with --evidence-out; "
        "--ring, --no-kv and --repeats apply to new probes "
        "(default: run preflight)",
    )
    ap.add_argument(
        "--print-example-config",
        action="store_true",
        help="print the annotated example fleet config and exit before fleet loading; "
        "takes precedence over --print-contract-versions (default: false)",
    )
    ap.add_argument(
        "--print-contract-versions",
        action="store_true",
        help="print the versioned machine-readable interface registry and exit before "
        "fleet loading (default: false)",
    )
    args = ap.parse_args(argv)

    if args.print_example_config:
        results.set_operation("print-example-config")
        results.set_data(
            json.loads(resources.files("narwhal").joinpath("fleet.example.json").read_text())
        )
        print(resources.files("narwhal").joinpath("fleet.example.json").read_text(), end="")
        return 0
    if args.print_contract_versions:
        results.set_operation("print-contract-versions")
        results.set_data(manifest())
        print(json.dumps(manifest(), indent=2))
        return 0

    if args.fleet is None:
        ap.error("give --fleet")
        return 2
    if args.evidence_out is not None and (args.ring or args.no_kv):
        ap.error("--evidence-out requires the full KV mesh")
    if args.evidence_out is not None and args.verify_evidence is not None:
        ap.error("choose --evidence-out or --verify-evidence")
    if args.evidence_out is not None:
        results.add_artifact("directed_kv_evidence", args.evidence_out)
    from ..cli_errors import failure

    try:
        cfg = FleetConfig.load(args.fleet)
    except (OSError, ValueError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-check", f"load fleet {args.fleet}", exc, 2)
    if results.json_mode():
        results.protect_environment(cfg.engine_api_key_env)
    try:
        if args.verify_evidence is not None:
            results.set_operation("verify-evidence")
            results.add_artifact("directed_kv_evidence", args.verify_evidence)
            problems = asyncio.run(
                verify_directed_kv_evidence(cfg, Path(args.fleet), args.verify_evidence)
            )
            results.set_data({"failed": problems})
            for problem in problems:
                results.record_error("evidence_gate_failed", problem, stage="verify-evidence")
                print(f"  FAIL  {problem}")
            if problems:
                return 1
            print("directed KV evidence matches the current fleet, profiles, and processes")
            return 0
        return asyncio.run(
            run(
                cfg,
                not args.ring,
                args.no_kv,
                args.repeats,
                evidence_out=args.evidence_out,
                fleet_path=Path(args.fleet),
            )
        )
    except ValueError as exc:
        if results.json_mode():
            raise
        return failure("narwhal-check", f"check fleet {args.fleet}", exc, 2)
    except (OSError, httpx.HTTPError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-check", f"check fleet {args.fleet}", exc, 1)


if __name__ == "__main__":
    sys.exit(main())
