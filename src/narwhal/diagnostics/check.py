"""Run fleet preflight gates from cheap health checks through KV transfer."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
from dataclasses import dataclass, field
from importlib import resources

import httpx

from ..config import FleetConfig
from ..contracts import manifest
from ..engines.attestation import fetch_engine_identity, verify_attestation
from ..engines.client import EngineClient, EngineError
from ..engines.connector import PrefillResult
from ..engines.connector import lookup as lookup_connector
from ..engines.dialect import lookup as lookup_dialect
from ..engines.validation import can_consume, can_produce, validation_pairs
from ..profiling.model import decode_evidence_problems
from ..profiling.store import ProfileStore

PROBE_PROMPT = "benchmark " * 64


@dataclass
class Report:
    """Print gate outcomes and collect failures and skips."""

    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

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
    body = {
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


async def gate_consume(
    cfg: FleetConfig,
    live: set[str],
    handoffs: dict[str, PrefillResult],
    client: EngineClient,
    rep: Report,
    mesh: bool,
    repeats: int = 1,
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
        try:
            params = await client.prefill(by_id[src].url, "/v1/completions", body, {})
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
        except EngineError as exc:
            rep.fail(f"{src} -> {dst}: {exc}")
            continue
        except Exception as exc:
            rep.fail(f"{src} -> {dst}: {type(exc).__name__}: {exc}")
            continue
        if not tokens:
            rep.fail(f"{src} -> {dst} accepted the handoff and produced no tokens")
        elif (src, dst) not in seen:
            seen.add((src, dst))
            rep.ok(f"{src} -> {dst} moved KV and produced {tokens} tokens")


def gate_profile(cfg: FleetConfig, rep: Report) -> ProfileStore:
    """Check the store binds the fleet and meets the decode error policy.

    Every row must span a measured decode domain on both axes and stay
    inside the fleet's profile-validation error limits.
    """
    print("profile")
    store = ProfileStore(cfg.profiles_path)
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
        interval = f"{p.tpot_slope:.2e}b+{p.tpot_intercept:.4f}"
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


def gate_slo(cfg: FleetConfig, store: ProfileStore, rep: Report) -> None:
    """Check that each profile can meet the configured latency targets.

    The TPOT target must exceed each engine's fitted `tpot_intercept`.
    """
    print("slo")
    for spec in cfg.engines:
        p = store.get(spec.iid)
        if p is None:
            rep.skip(f"{spec.iid} slo: no profile")
            continue
        if cfg.slo.tpot_s <= p.tpot_intercept:
            rep.fail(
                f"{spec.iid} tpot floor {p.tpot_intercept * 1000:.1f} ms is at or above the "
                f"{cfg.slo.tpot_s * 1000:.1f} ms target; unreachable at any fleet size"
            )
            continue
        headroom = p.max_tokens(cfg.slo.tpot_s)
        if cfg.slo.ttft_s <= p.prefill_time(1):
            rep.fail(f"{spec.iid} ttft target is below its own single-token prefill time")
            continue
        rep.ok(f"{spec.iid} holds {headroom:.0f} batch tokens at the TPOT target")


async def run(
    cfg: FleetConfig,
    mesh: bool,
    skip_kv: bool,
    repeats: int = 1,
    *,
    report: Report | None = None,
) -> int:
    """Run every preflight gate and return a process exit code."""
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
        incompatible.update(await gate_model(cfg, live, rep))
        pace_store = ProfileStore(cfg.profiles_path) if cfg.profiles_path.exists() else None
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
            await gate_consume(cfg, live, handoffs, client, rep, mesh, repeats)
        store = gate_profile(cfg, rep)
        gate_slo(cfg, store, rep)
    finally:
        await client.aclose()

    print()
    if rep.failed:
        print(f"{len(rep.failed)} gate(s) failed, {len(rep.skipped)} skipped")
        return 1
    if rep.skipped:
        print(f"all gates pass, {len(rep.skipped)} skipped")
        return 0
    print("all gates pass")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the fleet-check CLI."""
    ap = argparse.ArgumentParser(description="Check a fleet before starting the router")
    ap.add_argument("--fleet", help="fleet config JSON")
    ap.add_argument("--ring", action="store_true", help="test rotating producer-consumer pairs")
    ap.add_argument("--repeats", type=int, default=1, help="KV transfer probes per pair")
    ap.add_argument("--no-kv", action="store_true", help="skip the two KV gates")
    ap.add_argument(
        "--print-example-config",
        action="store_true",
        help="print the annotated example fleet config and exit",
    )
    ap.add_argument(
        "--print-contract-versions",
        action="store_true",
        help="print the versioned machine-readable interface registry and exit",
    )
    args = ap.parse_args(argv)

    if args.print_example_config:
        print(resources.files("narwhal").joinpath("fleet.example.json").read_text(), end="")
        return 0
    if args.print_contract_versions:
        print(json.dumps(manifest(), indent=2))
        return 0

    if args.fleet is None:
        ap.error("give --fleet")
        return 2
    cfg = FleetConfig.load(args.fleet)
    return asyncio.run(run(cfg, not args.ring, args.no_kv, args.repeats))


if __name__ == "__main__":
    sys.exit(main())
