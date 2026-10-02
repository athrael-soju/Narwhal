"""Live per-engine profiling and the fleet run."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx

from .. import command_results as results
from ..config import EngineSpec, FleetConfig
from ..engines.dialect import EngineDialect, VllmDialect
from ..engines.dialect import lookup as lookup_dialect
from ..provenance import stamp
from ..types import Role
from .decode import probe_decode
from .engine_io import (
    cache_block_tokens,
    cancel,
    engine_context_limit,
    kv_capacity,
    prefix_cache_hits,
)
from .fitting import (
    CACHED_FIT_MIN_CASES,
    cached_fit_possible,
    decode_cross_validation_mape,
    decode_mape,
    fit_decode_plane,
    fit_prefill_samples,
)
from .generation import read_generation
from .model import Profile, decode_evidence_problems
from .neighbour import ColocatedWorkload, NeighbourLoad
from .prefill import prefill_fields, probe_prefill
from .store import ProfileStore
from .sweep import Sweep, bounded_sweep, load_sequence_limits, warm_cases
from .warm import apply_cached_fit, probe_cached_prefill


async def _require_cold(
    client: httpx.AsyncClient,
    iid: str,
    url: str,
    before: int | None,
    timeout_s: float | None,
    evidence: dict[str, object] | None,
) -> None:
    """Fail when the engine served prompt tokens from its prefix cache since `before`.

    A missing or decreasing counter records no count.
    """
    after = await prefix_cache_hits(client, url, timeout_s or 30.0)
    hit_tokens = None if before is None or after is None or after < before else after - before
    if evidence is not None:
        evidence["prefix_cache_hit_tokens"] = hit_tokens
    if hit_tokens:
        raise RuntimeError(
            f"{iid} served {hit_tokens} prompt tokens from its prefix cache during cold "
            "profiling; reserve the engine for profiling and repeat the sweep"
        )


async def profile_instance(
    client: httpx.AsyncClient,
    iid: str,
    url: str,
    model: str,
    sweep: Sweep | None = None,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
    *,
    evidence: dict[str, object] | None = None,
    max_model_len: int | None = None,
    observation_timeout_s: float | None = None,
) -> Profile:
    """Run both sweeps and fit one engine profile."""
    s = sweep or Sweep()
    dialect = dialect or VllmDialect()
    print(f"  {iid}")
    hits_before = await prefix_cache_hits(client, url, observation_timeout_s or 30.0)
    block_tokens = await cache_block_tokens(client, url, observation_timeout_s or 30.0)
    prefill = await probe_prefill(
        client,
        url,
        model,
        s.prefill_lens,
        s.prefill_repeats,
        dialect,
        chars_per_token,
        max_model_len,
        observation_timeout_s,
    )
    if evidence is not None:
        evidence["prefill"] = prefill
    await _require_cold(client, iid, url, hits_before, observation_timeout_s, evidence)
    prefill_fit, representatives, prefill_fit_mape = fit_prefill_samples(prefill, block_tokens)
    split = prefill_fit[3]
    print(
        f"    prefill median fit MAPE {prefill_fit_mape:.1%}"
        + (f"; split step {split * 1000:.1f} ms past {block_tokens}" if split is not None else "")
    )
    if evidence is not None:
        evidence.update(
            prefill_fit_points=representatives,
            prefill_fit_mape=prefill_fit_mape,
            prefill_block_tokens=block_tokens,
        )
    decode_intervals: list[dict[str, object]] = []
    decode = await probe_decode(
        client,
        url,
        model,
        s.decode_concurrency,
        s.decode_tokens,
        dialect,
        chars_per_token,
        s.decode_input_lens,
        evidence=decode_intervals,
        max_model_len=max_model_len,
        repeats=s.decode_repeats,
        observation_timeout_s=observation_timeout_s,
    )
    decode_tokens_used = [
        used for row in decode_intervals if isinstance(used := row.get("tokens"), int)
    ]
    if evidence is not None:
        evidence.update(decode=decode, decode_intervals=decode_intervals)
    await _require_cold(client, iid, url, hits_before, observation_timeout_s, evidence)
    cached: list[dict[str, Any]] = []
    reason: str | None
    cases = warm_cases(s, max_model_len)
    if hits_before is None:
        reason = "engine exports no prefix-cache hit counter"
    elif not cached_fit_possible(cases):
        reason = (
            f"the warm sweep has {len(cases)} cases within the engine context; a warm fit needs "
            f"two prefix lengths, two suffix lengths and {CACHED_FIT_MIN_CASES} cases"
        )
    else:
        cached, reason = await probe_cached_prefill(
            client,
            url,
            model,
            s,
            dialect,
            observation_timeout_s=observation_timeout_s,
            max_model_len=max_model_len,
            block_tokens=block_tokens,
        )
    slope, request_slope, intercept = fit_decode_plane(decode)
    coefficients = (slope, request_slope, intercept)
    capacity = await kv_capacity(client, url, observation_timeout_s or 30.0)
    profile = Profile(
        iid=iid,
        **prefill_fields(prefill_fit, block_tokens),
        tpot_slope=slope,
        tpot_intercept=intercept,
        kv_capacity_tokens=capacity,
        tpot_request_slope=request_slope,
        decode_min_requests=max(1, math.ceil(min(row[0] for row in decode))),
        decode_max_requests=max(1, math.floor(max(row[0] for row in decode))),
        decode_min_kv_tokens=max(1, math.ceil(min(row[1] for row in decode))),
        decode_max_kv_tokens=max(1, math.floor(max(row[1] for row in decode))),
        decode_fit_mape=decode_mape(decode, coefficients),
        decode_cv_mape=decode_cross_validation_mape(decode),
        prefill_min_tokens=math.floor(min(row[0] for row in prefill)),
        prefill_max_tokens=math.ceil(max(row[0] for row in prefill)),
        decode_min_output_tokens=1,
        decode_max_output_tokens=max(decode_tokens_used, default=s.decode_tokens),
    )
    try:
        if reason is not None:
            raise ValueError(reason)
        profile, fit = apply_cached_fit(profile, cached)
    except ValueError as exc:
        print(f"    cached prefill kept cold: {exc}")
        if evidence is not None:
            evidence["cached_prefill"] = {"samples": cached, "reason": str(exc)}
        return profile
    if evidence is not None:
        evidence["cached_prefill"] = {"samples": cached, **fit}
    print(
        f"    cached prefill held-out MAPE {fit['cv_mape']:.1%}; "
        f"suffix-on-cold-curve MAPE {fit['suffix_on_cold_curve_mape']:.1%}"
    )
    return profile


class _Unhealthy(Exception):
    """An engine failed its health gate before profiling."""


def device_key(spec: EngineSpec) -> str:
    """Return the engine's shared-device group, or `engine:<iid>` for a dedicated device."""
    return spec.shared_device.group if spec.shared_device is not None else f"engine:{spec.iid}"


def _profile_lanes(targets: list[EngineSpec], *, colocated: bool) -> list[list[EngineSpec]]:
    """Group engines that share a device, or all engines under neighbour load, into one lane."""
    if colocated:
        return [list(targets)]
    lanes: dict[str, list[EngineSpec]] = {}
    for spec in targets:
        lanes.setdefault(device_key(spec), []).append(spec)
    return list(lanes.values())


async def run(
    cfg: FleetConfig,
    only: set[str] | None,
    sweep: Sweep | None = None,
    *,
    overwrite: bool = False,
    limits_path: Path | None = None,
    colocated_workload: ColocatedWorkload | None = None,
    observation_timeout_s: float | None = None,
) -> int:
    """Profile selected healthy engines and write the store."""
    if observation_timeout_s is not None and (
        not math.isfinite(observation_timeout_s) or observation_timeout_s <= 0
    ):
        raise ValueError("profile observation timeout must be finite and positive")
    store = ProfileStore(cfg.profiles_path, load=False)
    evidence_path = store.path.with_suffix(".samples.json")
    if store.path == evidence_path or (
        store.path.exists() and evidence_path.exists() and store.path.samefile(evidence_path)
    ):
        raise ValueError("profile store and sample sidecar must have distinct paths")
    for path in (store.path, evidence_path):
        if path.is_symlink():
            raise ValueError(f"refusing symlink output: {path}")
        if path.exists() and not overwrite:
            raise FileExistsError(f"output exists: {path}; use a new path or --overwrite")
    targets = [e for e in cfg.engines if not only or e.iid in only]
    if not targets:
        results.record_error(
            "engine_selection_empty", "Selection matched zero engines", field="only"
        )
        print("no matching instances", file=sys.stderr)
        return 2
    limits = (
        load_sequence_limits(limits_path, {engine.iid for engine in cfg.engines})
        if limits_path is not None
        else {}
    )

    print(f"profiling {len(targets)} instance(s) against model {cfg.model}")
    dialect = lookup_dialect(cfg.dialect)
    evidence_rows: dict[str, object] = {}
    measurement_record = {
        "method_version": 2,
        **stamp(),
        "model": cfg.model,
        "sweep": asdict(sweep or Sweep()),
        "observation_timeout_s": observation_timeout_s,
        "engines": evidence_rows,
    }
    lanes = _profile_lanes(targets, colocated=colocated_workload is not None)
    connections = max((sweep or Sweep()).decode_concurrency) * len(lanes) + len(cfg.engines)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            observation_timeout_s or 300.0,
            connect=min(10.0, observation_timeout_s or 300.0),
        ),
        limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections),
        headers=cfg.engine_headers(),
    ) as client:

        async def profile_engine(spec: EngineSpec) -> None:
            r = await client.get(
                f"{spec.url}{dialect.health_path}", timeout=observation_timeout_s or 10.0
            )
            if r.status_code != 200:
                results.record_error(
                    "engine_unhealthy", "Health gate failed", stage="health", engine=spec.iid
                )
                print(f"  {spec.iid}: not healthy, aborting", file=sys.stderr)
                raise _Unhealthy(spec.iid)
            if dialect.tokenize_path is None:
                raise ValueError(
                    f"{spec.iid}: the {dialect.name} dialect needs a tokenization route "
                    "that reports max_model_len before profiling"
                )
            max_model_len = await engine_context_limit(
                client, spec.url, cfg.model, dialect, observation_timeout_s or 30.0
            )
            max_num_seqs = limits.get(spec.iid)
            engine_sweep = bounded_sweep(sweep or Sweep(), max_model_len, max_num_seqs)
            print(
                f"  {spec.iid}: max_model_len {max_model_len}; "
                f"prefill up to {max(engine_sweep.prefill_lens)}, "
                f"decode input up to {max(engine_sweep.decode_input_lens)}, "
                f"decode concurrency up to {max(engine_sweep.decode_concurrency)}"
            )
            engine_evidence: dict[str, object] = {
                "max_model_len": max_model_len,
                "sweep": asdict(engine_sweep),
            }
            if max_num_seqs is not None:
                engine_evidence["max_num_seqs"] = max_num_seqs
            neighbour_load = None
            group = spec.shared_device.group if spec.shared_device is not None else None
            if colocated_workload is not None:
                if group is None:
                    raise ValueError(f"{spec.iid}: --colocated requires a shared_device group")
                peers = [
                    (peer.iid, peer.url, peer.role)
                    for peer in cfg.engines
                    if peer.iid != spec.iid
                    and peer.shared_device is not None
                    and peer.shared_device.group == group
                ]
                if not peers:
                    raise ValueError(f"{spec.iid}: no neighbours in shared_device group {group}")
                neighbour_load = NeighbourLoad(
                    client,
                    peers,
                    cfg.model,
                    dialect,
                    cfg.chars_per_token,
                    colocated_workload,
                    observation_timeout_s,
                )
            try:
                if neighbour_load is not None:
                    await neighbour_load.start()
                generation = await read_generation(
                    spec,
                    cfg.engine_contract,
                    timeout_s=observation_timeout_s or cfg.health_timeout_s,
                    headers=cfg.engine_headers(),
                )
                engine_evidence["generation_evidence"] = generation.document
                profile = await profile_instance(
                    client,
                    spec.iid,
                    spec.url,
                    cfg.model,
                    engine_sweep,
                    dialect,
                    cfg.chars_per_token,
                    evidence=engine_evidence,
                    max_model_len=max_model_len,
                    observation_timeout_s=observation_timeout_s,
                )
                if neighbour_load is not None:
                    measured = await neighbour_load.stop()
                    prefill_engines = sum(
                        peer.shared_device is not None
                        and peer.shared_device.group == group
                        and peer.role is Role.PREFILL
                        for peer in cfg.engines
                    )
                    decode_engines = sum(
                        peer.shared_device is not None
                        and peer.shared_device.group == group
                        and peer.role is Role.DECODE
                        for peer in cfg.engines
                    )
                    engine_evidence["colocated_load"] = measured
                    profile = replace(
                        profile,
                        colocated_group=group,
                        colocated_target_role=spec.role.value,
                        colocated_prefill_engines=prefill_engines,
                        colocated_decode_engines=decode_engines,
                        colocated_prefill_rps=float(measured["prefill_rps"]),
                        colocated_decode_rps=float(measured["decode_rps"]),
                    )
                current = await read_generation(
                    spec,
                    cfg.engine_contract,
                    timeout_s=observation_timeout_s or cfg.health_timeout_s,
                    headers=cfg.engine_headers(),
                )
                if generation.process_digest != current.process_digest:
                    raise ValueError(f"{spec.iid}: engine generation changed during profiling")
                profile = replace(profile, generation_digest=generation.digest)
                problems = decode_evidence_problems(
                    profile,
                    max_fit_mape=cfg.profile_validation.max_decode_fit_mape,
                    max_cv_mape=cfg.profile_validation.max_decode_cv_mape,
                )
                if problems:
                    raise RuntimeError("profile rejected: " + "; ".join(problems))
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                if neighbour_load is not None and neighbour_load.tasks:
                    await cancel(neighbour_load.tasks)
                    engine_evidence["colocated_load"] = neighbour_load.evidence()
                engine_evidence["error"] = str(exc)
                evidence_rows[spec.iid] = engine_evidence
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                with evidence_path.open(
                    "x" if len(evidence_rows) == 1 and not overwrite else "w", encoding="utf-8"
                ) as output:
                    output.write(json.dumps(measurement_record, indent=2) + "\n")
                raise
            engine_evidence["profile"] = asdict(profile)
            evidence_rows[spec.iid] = engine_evidence
            # Completed engines' observations survive a later engine's failure.
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            first = len(evidence_rows) == 1
            if first and not overwrite:
                # Exclusive creation reserves the store against another default-mode profiler.
                with store.path.open("x", encoding="utf-8"):
                    pass
            with evidence_path.open(
                "w" if overwrite or not first else "x", encoding="utf-8"
            ) as output:
                output.write(json.dumps(measurement_record, indent=2) + "\n")
            store.put(profile)
            print(
                f"  {spec.iid} fit: ttft = {profile.ttft_a:.3e}n^2 + {profile.ttft_b:.3e}n "
                f"+ {profile.ttft_c:.4f}"
            )
            print(
                f"         tpot = {profile.tpot_request_slope:.3e}q + "
                f"{profile.tpot_slope:.3e}b + {profile.tpot_intercept:.4f}"
            )
            cv = (
                f"{profile.decode_cv_mape:.1%}"
                if profile.decode_cv_mape is not None
                else "unavailable"
            )
            fit_error = profile.decode_fit_mape if profile.decode_fit_mape is not None else 0.0
            print(f"         decode fit MAPE {fit_error:.1%}; cross-validation {cv}")

        async def profile_lane(lane: list[EngineSpec]) -> None:
            for spec in lane:
                await profile_engine(spec)

        tasks = [asyncio.create_task(profile_lane(lane)) for lane in lanes]
        try:
            await asyncio.gather(*tasks)
        except _Unhealthy:
            await cancel(tasks)
            return 1
        except BaseException:
            await cancel(tasks)
            raise
    print(f"wrote {len(store)} profile(s) to {cfg.profiles_path}")
    return 0
