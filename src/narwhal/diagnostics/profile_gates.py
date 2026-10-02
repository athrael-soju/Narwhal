"""Profile validity, generation and SLO gates."""

from __future__ import annotations

import httpx

from ..config import FleetConfig
from ..profiling.generation import generation_problem, read_generation
from ..profiling.model import decode_evidence_problems
from ..profiling.store import ProfileStore
from ..types import Role
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
