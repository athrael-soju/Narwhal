from __future__ import annotations

from dataclasses import dataclass

import httpx

from ...backends import load as load_backend
from ...config import FleetConfig
from ...profiling.generation import generation_problem, read_generation
from ...profiling.model import Profile, decode_evidence_problems, evidence_matches
from ...profiling.store import ProfileStore, measured_evidence
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


@dataclass(frozen=True)
class _Recorded:
    """The sequence limit a profile's evidence records, the limits file it read it from, and
    whether its sweep skipped cohorts that exceed the engine's KV cache.
    """

    limit: int | None
    limits_path: str | None
    kv_bound: bool


def _recorded(entries: list[dict[str, object]], row: Profile) -> _Recorded | None:
    """Return what the evidence that measured `row` records, or None when none matches it."""
    for evidence in entries:
        if evidence_matches(evidence.get("profile"), row):
            value = evidence.get("max_num_seqs")
            limit = value if isinstance(value, int) and not isinstance(value, bool) else None
            path = evidence.get("limits_path")
            return _Recorded(
                limit,
                path if isinstance(path, str) else None,
                bool(evidence.get("decode_skipped")),
            )
    return None


def _below_limit(
    iid: str, row: Profile, limit: int, source: str, recorded: _Recorded | None
) -> str | None:
    """Describe a profile whose decode coverage stops below `limit`, with the fix that applies.

    `recorded` is None when no evidence says how the sweep ran.
    """
    measured = row.decode_max_requests
    if measured is None or measured >= limit:
        return None
    if recorded is not None and recorded.limit is not None and recorded.limit < limit:
        limits_file = (
            f"the limits file {recorded.limits_path}"
            if recorded.limits_path is not None
            else "the limits file it was profiled with"
        )
        remedy = (
            f"{limits_file} records {recorded.limit}; regenerate that file and re-run "
            "narwhal-profile with --limits"
        )
    elif recorded is not None and recorded.kv_bound:
        remedy = (
            f"its sweep skipped cohorts that exceed its {row.kv_capacity_tokens} KV tokens; "
            "pass shorter --decode-input-lens, or lower the engine's sequence limit and "
            "regenerate the limits file, then re-run narwhal-profile with --limits"
        )
    elif recorded is None:
        remedy = (
            "re-run narwhal-profile with --limits, and with shorter --decode-input-lens if its "
            "KV cache holds fewer streams"
        )
    else:
        remedy = "re-run narwhal-profile with --limits"
    return (
        f"{iid} profile measures decode up to {measured} requests and its {source} allows "
        f"{limit}; {remedy}"
    )


def gate_profile(cfg: FleetConfig, rep: Report) -> ProfileStore:
    print("profile")
    store = _bind_configured_mix(ProfileStore(cfg.profiles_path), cfg)
    policy = cfg.profile_validation
    evidence, _ = measured_evidence(cfg.profiles_path.with_suffix(".samples.json"))
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
        recorded = _recorded(evidence.get(spec.iid, []), p)
        # Decode seats stop at the profiled maximum, whatever stopped the sweep.
        if (
            recorded is not None
            and recorded.limit is not None
            and (warning := _below_limit(spec.iid, p, recorded.limit, "limits file", recorded))
        ):
            rep.warn(warning)
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
    identity = load_backend(cfg.backend).identity
    evidence, _ = measured_evidence(cfg.profiles_path.with_suffix(".samples.json"))
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
        row = store.get(spec.iid)
        # The router seats a contract fleet's engines up to the attested limit.
        if cfg.engine_contract is None or row is None or spec.iid in unsafe:
            continue
        attested = identity.sequence_limit(generation.document)
        if attested is None:
            continue
        recorded = _recorded(evidence.get(spec.iid, []), row)
        # The profile gate already compared the profile with a recorded limit this high.
        if recorded is not None and recorded.limit is not None and recorded.limit >= attested:
            continue
        if warning := _below_limit(spec.iid, row, attested, "attested launch", recorded):
            rep.warn(warning)
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
