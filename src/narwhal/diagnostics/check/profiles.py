from __future__ import annotations

import json
from pathlib import Path

import httpx

from ...config import FleetConfig
from ...profiling.generation import generation_problem, read_generation
from ...profiling.model import decode_evidence_problems
from ...profiling.store import ProfileStore
from ...types import Role
from .report import Report


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


def _sequence_limits(samples: Path) -> dict[str, int]:
    """Return each engine's `max_num_seqs` recorded in a sample sidecar or its merge sources.

    An engine whose decode sweep skipped cohorts that exceed its KV cache measured what its KV
    holds, so it is left out.
    """
    try:
        document = json.loads(samples.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    limits: dict[str, int] = {}
    for source in document.get("sources") or ():
        if isinstance(source, dict) and isinstance(source.get("samples"), str):
            limits.update(_sequence_limits(Path(source["samples"])))
    engines = document.get("engines")
    for iid, evidence in engines.items() if isinstance(engines, dict) else ():
        if not isinstance(evidence, dict):
            continue
        value = evidence.get("max_num_seqs")
        if evidence.get("decode_skipped"):
            limits.pop(iid, None)
        elif isinstance(value, int) and not isinstance(value, bool):
            limits[iid] = value
    return limits


def gate_profile(cfg: FleetConfig, rep: Report) -> ProfileStore:
    print("profile")
    store = _bind_configured_mix(ProfileStore(cfg.profiles_path), cfg)
    policy = cfg.profile_validation
    limits = _sequence_limits(cfg.profiles_path.with_suffix(".samples.json"))
    for spec in cfg.engines:
        p = store.get(spec.iid)
        if p is None:
            if spec.shared_device is not None and store.profiles_for_engine(spec.iid):
                group = [
                    peer.role
                    for peer in cfg.engines
                    if peer.shared_device is not None
                    and peer.shared_device.group == spec.shared_device.group
                ]
                rep.fail(
                    f"{spec.iid} has no profile for its GPU group split of "
                    f"{group.count(Role.PREFILL)} prefill and {group.count(Role.DECODE)} decode "
                    "engines; profile that split with narwhal-profile --colocated"
                )
            else:
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
        limit = limits.get(spec.iid)
        measured = p.decode_max_requests
        if limit is not None and measured is not None and measured < limit:
            rep.warn(
                f"{spec.iid} profile measures decode up to {measured} requests "
                f"and the engine runs up to {limit}; re-run narwhal-profile with --limits"
            )
        quadratic = f"{p.ttft_a:.2e}n^2+{p.ttft_b:.2e}n+{p.ttft_c:.4f}" + (
            f"+{p.ttft_split:.4f} past {p.ttft_block_tokens}-token blocks"
            if p.ttft_split is not None
            else ""
        )
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
    unsafe: set[str] = set()
    for spec in cfg.engines:
        profiles = store.profiles_for_engine(spec.iid)
        if not profiles or spec.iid not in live:
            continue
        try:
            generation = await read_generation(cfg, spec, transport=transport)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            rep.fail(
                f"{spec.iid} profile generation unreadable: {exc}; "
                "restart its attestation sidecar if it answers HTTP 503"
            )
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
