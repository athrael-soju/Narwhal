from __future__ import annotations

import asyncio
import contextlib
import math
from urllib.parse import urlsplit

import httpx

from ...backends import load as load_backend
from ...config import EngineSpec, FleetConfig
from ...engines.attestation import fetch_engine_identity, verify_attestation
from ...engines.client import EngineClient, EngineError
from ...engines.validation import can_consume, can_produce
from ...profiling.calibration import verify_calibration
from ...profiling.probe.engine import engine_context_limit, make_prompt
from ...profiling.store import ProfileStore
from .report import Report

PROBE_PROMPT = "benchmark " * 64


async def colocated_restart_risk(
    cfg: FleetConfig, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    contract = cfg.engine_contract
    if cfg.engine_restart_policy != "individual" or contract is None or not contract.connector:
        return ""
    hosts: dict[str, list[EngineSpec]] = {}
    for spec in cfg.engines:
        hosts.setdefault(urlsplit(spec.url).hostname or "", []).append(spec)
    shared = [spec for specs in hosts.values() if len(specs) > 1 for spec in specs]
    released: dict[str, bool] = {}
    async with httpx.AsyncClient(timeout=cfg.health_timeout_s, transport=transport) as client:
        for spec in shared:
            release = None
            if spec.attestation_url:
                with contextlib.suppress(httpx.HTTPError, ValueError, AttributeError):
                    response = await client.get(spec.attestation_url)
                    response.raise_for_status()
                    release = response.json().get("launch", {}).get("peer_release")
            released[spec.iid] = release is True
    unreleased: set[str] = set()
    unprobed: set[str] = set()
    for specs in hosts.values():
        for stopped in specs:
            if len(specs) < 2 or not can_produce(stopped):
                continue
            for peer in specs:
                if peer is stopped or not can_consume(peer):
                    continue
                if not released[peer.iid]:
                    unreleased.add(stopped.iid)
                if not any(
                    can_produce(other) and other.iid not in (stopped.iid, peer.iid)
                    for other in cfg.engines
                ):
                    unprobed.add(stopped.iid)
    causes = [
        f"{', '.join(sorted(iids))} {cause}"
        for iids, cause in (
            (unreleased, "share a host with a KV consumer without attested peer release"),
            (unprobed, "share a host with a KV consumer that has no other producer to probe"),
        )
        if iids
    ]
    if not causes:
        return ""
    return (
        f"engines {'; '.join(causes)}; that peer keeps a stopped engine's GPU memory mapped, "
        "so recover a crashed engine with a whole-wave restart"
    )


async def gate_reach(cfg: FleetConfig, client: EngineClient, rep: Report) -> set[str]:
    print("reach")
    live: set[str] = set()
    for spec in cfg.engines:
        if await client.healthy(spec.url):
            rep.ok(f"{spec.iid} /health")
            live.add(spec.iid)
        else:
            rep.fail(f"{spec.iid} /health did not answer 200")
    return live


async def gate_calibration(
    cfg: FleetConfig, rep: Report, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    print("calibration")
    check = await verify_calibration(cfg, transport=transport)
    if check.status == "uncalibrated":
        rep.warn(
            "first-token deadline has no calibration evidence; run "
            "narwhal-check --calibrate-first-token and set "
            "engine.first_token_calibration_path"
        )
    elif check.status == "rejected":
        for problem in check.problems:
            rep.fail(problem)
    else:
        rep.ok(check.summary(cfg.first_token_timeout_s))
    rep.calibration = {**check.view(), "path": None if check.path is None else str(check.path)}


async def gate_contract(
    cfg: FleetConfig,
    live: set[str],
    rep: Report,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
    print("contract")
    backend = load_backend(cfg.backend)
    label = backend.label
    declared = cfg.engine_contract
    if declared is None:
        rep.skip(f"no engine_contract declared; checking live {label} versions only")
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
                        reader=backend.identity,
                    )
                    version = identity.version
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
            rep.ok(f"{spec.iid} {label} {version}")
            if declared is not None and version != declared.engine_version:
                rep.fail(
                    f"{spec.iid} {label} {version}, expected {declared.engine_version} "
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
        rep.fail(f"mixed {label} versions before KV transfer: {version_detail}")
        unsafe.update(observed)
    return unsafe


async def gate_model(
    cfg: FleetConfig,
    live: set[str],
    rep: Report,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
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
    print("pace")
    dialect = load_backend(cfg.backend).dialect
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
                    r = await c.post(
                        f"{spec.url}/v1/completions", json={**body, **dialect.cold_probe_extras()}
                    )
                except httpx.HTTPError as exc:
                    rep.fail(f"{spec.iid} pace probe: {type(exc).__name__}")
                    failed_probes.add(spec.iid)
                    best = None
                    break
                if r.status_code == 400 and body["prompt"] == PACE_PROMPT:
                    try:
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
                        r = await c.post(
                            f"{spec.url}/v1/completions",
                            json={**body, **dialect.cold_probe_extras()},
                        )
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
    print("tokenize")
    if not cfg.tokenize:
        rep.skip("tokenize: engine.tokenize is disabled; prefill cost uses the character estimate")
        return
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
        try:
            n = await client.token_count(spec.url, body, cfg.tokenize_timeout_s, strict=True)
        except EngineError as exc:
            rep.fail(f"{spec.iid} {route} exact count failed: {exc}")
            continue
        if n:
            rep.ok(f"{spec.iid} {route} -> {n} tokens")
        else:
            # Character-ratio error is squared by the quadratic prefill model.
            rep.fail(f"{spec.iid} {route} returned no count")
