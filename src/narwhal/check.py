"""Preflight gates for the fleet, all reported at once rather than one per run.

Ordered cheapest first:

  reach     every engine answers /health at its own address
  model     every engine serves the model the config names
  pace      no engine runs conspicuously slower than the fleet it claims to match
  tokenize  exact input length is available
  produce   a prefill leg returns kv_transfer_params
  consume   a real split moves KV from one engine to another (ring shapes with a
            single unpinned engine may probe it against itself)
  profile   every instance has a measured curve
  slo       the TPOT target is above the measured floor

`produce` and `consume` together prove Arrow §5.2's requirement: every engine must run
NixlConnector with `kv_role: kv_both`. A `kv_producer` passes `produce` and
fails `consume`, a `kv_consumer` fails `produce`, and either way flipping
becomes a relabel with no effect.

Engines are addressed by their own host and port, never loopback: a port number
repeats across hosts, so only the host names the engine.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import httpx

from .config import FleetConfig
from .connector import lookup as lookup_connector
from .dialect import lookup as lookup_dialect
from .engine import EngineClient, EngineError
from .profiler import ProfileStore
from .types import Role

PROBE_PROMPT = "benchmark " * 64


@dataclass
class Report:
    """Gate outcomes, printed as they happen and counted into the exit code."""

    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def ok(self, msg: str) -> None:
        """A gate that passed."""
        print(f"  ok    {msg}")

    def fail(self, msg: str) -> None:
        """A gate that failed; it is named in the summary and fails the run."""
        print(f"  FAIL  {msg}")
        self.failed.append(msg)

    def skip(self, msg: str) -> None:
        """A gate that could not run: neither a pass nor a failure.

        Counted and named separately so it is never read as a pass.
        """
        print(f"  SKIP  {msg}")
        self.skipped.append(msg)


async def gate_reach(cfg: FleetConfig, client: EngineClient, rep: Report) -> set[str]:
    """Every engine answers /health at its own address."""
    print("reach")
    live: set[str] = set()
    for spec in cfg.engines:
        if await client.healthy(spec.url):
            rep.ok(f"{spec.iid} /health")
            live.add(spec.iid)
        else:
            rep.fail(f"{spec.iid} /health did not answer 200")
    return live


async def gate_model(cfg: FleetConfig, live: set[str], rep: Report) -> None:
    """Every engine serves the config's model; mixed models corrupt KV silently."""
    print("model")
    async with httpx.AsyncClient(timeout=15.0) as c:
        for spec in cfg.engines:
            if spec.iid not in live:
                rep.skip(f"{spec.iid} model: unreachable")
                continue
            try:
                r = await c.get(f"{spec.url}/v1/models")
                names = [m["id"] for m in r.json().get("data", [])]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                rep.fail(f"{spec.iid} /v1/models unreadable: {type(exc).__name__}")
                continue
            if cfg.model in names:
                rep.ok(f"{spec.iid} serves {cfg.model}")
            else:
                # Mixed models route KV between incompatible caches, which
                # surfaces as corrupt output rather than an error.
                rep.fail(f"{spec.iid} serves {names}, not {cfg.model}")


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
    """No engine runs conspicuously slower than the fleet it claims to match.

    Liveness gates cannot see a throttled engine: a fleet can serve for
    hours with one node's clocks wedged at a fraction of nominal - /health
    answering 200, drift health quiet under light serving - because
    nothing measures pace until something times it. This gate times one
    mid-size prefill on every live engine (min of `repeats`, so a scheduler
    hiccup is not a conviction) and fails any engine slower than `tolerance`
    times the fleet median. With a profile store present, each engine is also
    held to its own stored fit, which catches the whole fleet drifting
    together - a case the median cannot see.

    Returns the engines that failed, because the KV gates must not run
    through them: a transfer probe against a degraded engine stalls, and a
    stalled transfer kills the healthy peer's engine core (the same
    mechanism as vllm#38840) - a ring probe through a throttled engine
    can take down its healthy partner.
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
    async with httpx.AsyncClient(timeout=cfg.prefill_timeout_s, transport=transport) as c:
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
                    best = None
                    break
                elapsed = asyncio.get_event_loop().time() - start
                if r.status_code != 200:
                    rep.fail(f"{spec.iid} pace probe: {r.status_code}")
                    best = None
                    break
                best = elapsed if best is None else min(best, elapsed)
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    lens[spec.iid] = int(r.json()["usage"]["prompt_tokens"])
            if best is not None:
                times[spec.iid] = best

    slow: set[str] = set()
    if len(times) >= 3:
        ordered = sorted(times.values())
        median = ordered[len(ordered) // 2]
        for iid, t in sorted(times.items()):
            if t > tolerance * median:
                slow.add(iid)
                rep.fail(
                    f"{iid} pace: {t:.2f}s against a fleet median of {median:.2f}s "
                    f"(> {tolerance:g}x) - throttled or degraded, not merely busy"
                )
            else:
                rep.ok(f"{iid} pace {t:.2f}s (median {median:.2f}s)")
    elif times:
        rep.skip(f"pace vs median: needs 3 engines, have {len(times)}")

    if store is not None and len(store):
        for iid, t in sorted(times.items()):
            profile = store.get(iid)
            if profile is None:
                continue
            want = profile.prefill_time(lens.get(iid, len(PACE_PROMPT.split())))
            if want > 0 and t > tolerance * want:
                slow.add(iid)
                rep.fail(
                    f"{iid} pace: {t:.2f}s against its own profile's {want:.2f}s "
                    f"(> {tolerance:g}x) - the fit no longer describes this engine"
                )
    return slow


async def gate_tokenize(
    cfg: FleetConfig, live: set[str], client: EngineClient, rep: Report
) -> None:
    """Exact input length is available, probed with the router's own budget."""
    print("tokenize")
    route = client.dialect.tokenize_path
    body = {"model": cfg.model, "prompt": PROBE_PROMPT}
    for spec in cfg.engines:
        if spec.iid not in live:
            rep.skip(f"{spec.iid} tokenize: unreachable")
            continue
        if route is None:
            # The dialect names the failure mode, not the engine: there is
            # nothing on the wire to probe.
            rep.skip(
                f"{spec.iid} the {client.dialect.name} dialect has no exact-count route; "
                "prefill cost uses the character estimate"
            )
            continue
        n = await client.token_count(spec.url, body, cfg.tokenize_timeout_s)
        if n:
            rep.ok(f"{spec.iid} {route} -> {n} tokens")
        else:
            # Warning, not fatal: falls back to a character ratio, whose error
            # is squared because prefill cost is quadratic (Arrow §3.1).
            rep.skip(f"{spec.iid} no {route}; prefill cost falls back to a character estimate")


async def gate_produce(
    cfg: FleetConfig, live: set[str], client: EngineClient, rep: Report
) -> dict[str, dict]:
    """A prefill leg returns kv_transfer_params on every live engine."""
    print("produce")
    handoffs: dict[str, dict] = {}
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
        rep.ok(f"{spec.iid} returned kv_transfer_params ({', '.join(sorted(params))})")
    return handoffs


async def gate_consume(
    cfg: FleetConfig,
    live: set[str],
    handoffs: dict[str, dict],
    client: EngineClient,
    rep: Report,
    mesh: bool,
    repeats: int = 1,
) -> None:
    """Move KV for real, over every ordered pair by default.

    The ring covers each engine once as producer and once as consumer, which is
    what `kv_both` claims, but it is 6 of 30 pairs on a six-engine fleet and the
    router can use any of the 30.

    `repeats` exists because the fault is intermittent. One probe against an
    instance stalling a fraction f of its decode legs finds it with probability
    f; n probes with 1 - (1 - f)^n.
    """
    print(f"consume ({'mesh' if mesh else 'ring'}, {repeats}x)")
    ids = [s.iid for s in cfg.engines if s.iid in live and s.iid in handoffs]
    if len(ids) < 2:
        rep.skip("consume: fewer than two engines produced a handoff")
        return
    by_id = {s.iid: s for s in cfg.engines}
    producers = [i for i in ids if not (by_id[i].pin and by_id[i].role is Role.DECODE)]
    consumers = [i for i in ids if not (by_id[i].pin and by_id[i].role is Role.PREFILL)]
    if len(producers) < len(ids) or len(consumers) < len(ids):
        # A pinned engine never crosses out of its role, so the pairs the
        # config forbids are not part of the operational surface - and
        # probing one can kill a healthy peer: a consume into a
        # role-constrained engine's bad ingress stalls, and the engine core
        # dies with the stall. Production never runs a forbidden pair; the
        # gate must not either.
        excluded = sorted(set(ids) - set(consumers)) + sorted(set(ids) - set(producers))
        rep.ok(f"pairs excluded by role pins: {', '.join(excluded)} (never cross in production)")
    pairs = _pairs_of(producers, consumers, mesh)

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
                from .engine import sse_token_count

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


def _pairs_of(producers: list[str], consumers: list[str], mesh: bool) -> list[tuple[str, str]]:
    """Ordered pairs over the roles each engine can actually hold.

    Mesh is every eligible ordered pair. The ring pairs each producer with a
    rotating consumer and tops up any consumer left uncovered; with nobody
    pinned, every engine is both producer and consumer and both shapes
    cover the whole fleet.
    """
    if mesh:
        return [(a, b) for a in producers for b in consumers if a != b]
    if not producers or not consumers:
        return []
    pairs: list[tuple[str, str]] = []
    k = 0
    for p in producers:
        if consumers[k % len(consumers)] == p:
            k += 1
        pairs.append((p, consumers[k % len(consumers)]))
        k += 1
    covered = {c for _, c in pairs}
    for c in consumers:
        if c not in covered:
            p = next((x for x in producers if x != c), None)
            if p is not None:
                pairs.append((p, c))
    return pairs


def gate_profile(cfg: FleetConfig, rep: Report) -> ProfileStore:
    """Every instance has a measured curve in the store."""
    print("profile")
    store = ProfileStore(cfg.profiles_path)
    for spec in cfg.engines:
        p = store.get(spec.iid)
        if p is None:
            rep.fail(f"{spec.iid} has no profile; run narwhal-profile --fleet <config>")
        else:
            rep.ok(
                f"{spec.iid} ttft={p.ttft_a:.2e}n^2+{p.ttft_b:.2e}n+{p.ttft_c:.4f} "
                f"tpot={p.tpot_slope:.2e}b+{p.tpot_intercept:.4f}"
            )
    return store


def gate_slo(cfg: FleetConfig, store: ProfileStore, rep: Report) -> None:
    """A TPOT target below the zero-contention interval is unreachable.

    `tpot_intercept` is what one instance produces with nothing else resident,
    so a target under it cannot be met by any fleet at any size.
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


async def run(cfg: FleetConfig, mesh: bool, skip_kv: bool, repeats: int = 1) -> int:
    """Every gate against one fleet, cheapest first; returns the exit code."""
    print(f"fleet: {len(cfg.engines)} engines, model {cfg.model}")
    print(f"slo:   ttft <= {cfg.slo.ttft_s}s, tpot <= {cfg.slo.tpot_s}s")
    rep = Report()
    # The gates probe with the same budgets the router will run with, so a
    # pass here is a statement about the config, not about looser limits.
    client = EngineClient(
        timeout_s=cfg.request_timeout_s,
        prefill_timeout_s=cfg.prefill_timeout_s,
        read_timeout_s=cfg.decode_read_timeout_s,
        max_connections=cfg.max_connections,
        pool_timeout_s=cfg.pool_timeout_s,
        connect_timeout_s=cfg.connect_timeout_s,
        health_timeout_s=cfg.health_timeout_s,
        kv=lookup_connector(cfg.connector),
        dialect=lookup_dialect(cfg.dialect),
    )
    try:
        live = await gate_reach(cfg, client, rep)
        await gate_model(cfg, live, rep)
        pace_store = ProfileStore(cfg.profiles_path) if cfg.profiles_path.exists() else None
        slow = await gate_pace(cfg, live, rep, pace_store)
        await gate_tokenize(cfg, live, client, rep)
        if skip_kv:
            rep.skip("produce and consume: skipped by --no-kv")
        elif slow:
            # A transfer probe through a degraded engine stalls, and the
            # stall kills the healthy peer's engine core. Fix the pace
            # failure first; these gates would make the fleet worse.
            rep.skip(f"produce and consume: pace failed on {', '.join(sorted(slow))}")
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


# Presets resolve against the working tree a checkout ships: the
# config, launch scripts and manifest of one (hardware, model) pair under
# presets/<hw>-<model>/. An editable install keeps parents[2] at that root.
def preset_fleet(name: str) -> Path:
    """The fleet config of a named preset, or a refusal listing what exists."""
    if Path(name).name != name or name in (".", ".."):
        raise ValueError(f"a preset is a directory name under presets/, not {name!r}")
    roots = [Path.cwd() / "presets", Path(__file__).resolve().parents[2] / "presets"]
    for root in roots:
        candidate = root / name / "fleet.json"
        if candidate.exists():
            return candidate
    found = sorted(d.name for root in roots if root.is_dir() for d in root.iterdir())
    raise ValueError(
        f"no preset {name!r} (looked in {', '.join(str(r) for r in roots)}); "
        f"available: {', '.join(dict.fromkeys(found)) or 'none'}"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: gate a fleet, convert a launcher fleet.json, or print the example."""
    ap = argparse.ArgumentParser(description="Check a fleet before starting the router")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--fleet", help="fleet config JSON")
    which.add_argument(
        "--preset",
        help="a presets/<hw>-<model>/ directory name, e.g. h100-llama-70b; "
        "resolves to its fleet.json",
    )
    ap.add_argument(
        "--from-fleet-json",
        help="convert a launcher fleet.json: {nodes: {n: {address, port}}, "
        "model.served_name, scenario.slo{ttft_ms,tpot_ms}, opening_split{p,d}}",
    )
    ap.add_argument("--write", help="with --from-fleet-json, save the converted config here")
    ap.add_argument("--ring", action="store_true", help="test a ring, not every ordered pair")
    ap.add_argument(
        "--repeats", type=int, default=1, help="probes per pair; the stall is intermittent"
    )
    ap.add_argument("--no-kv", action="store_true", help="skip the two KV gates")
    ap.add_argument(
        "--print-example-config",
        action="store_true",
        help="print the annotated example fleet config and exit",
    )
    args = ap.parse_args(argv)

    if args.print_example_config:
        print(resources.files("narwhal").joinpath("fleet.example.json").read_text(), end="")
        return 0

    if args.from_fleet_json:
        cfg = FleetConfig.from_fleet_json(args.from_fleet_json)
        if args.write:
            cfg.save(args.write)
            print(f"wrote {args.write}")
        else:
            print(json.dumps(json.loads(_as_json(cfg)), indent=2))
    elif args.fleet or args.preset:
        try:
            path = Path(args.fleet) if args.fleet else preset_fleet(args.preset)
        except ValueError as exc:
            ap.error(str(exc))
            return 2
        cfg = FleetConfig.load(path)
    else:
        ap.error("give --fleet, --preset or --from-fleet-json")
        return 2
    return asyncio.run(run(cfg, not args.ring, args.no_kv, args.repeats))


def _as_json(cfg: FleetConfig) -> str:
    from tempfile import NamedTemporaryFile

    with NamedTemporaryFile("w+", suffix=".json") as fh:
        cfg.save(fh.name)
        return open(fh.name).read()


if __name__ == "__main__":
    sys.exit(main())
