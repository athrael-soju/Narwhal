from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import multiprocessing
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx

from ... import command_results as results
from ...backends import load as load_backend
from ...config import EngineSpec, FleetConfig
from ...engines.dialect import EngineDialect
from ...provenance import stamp
from ...runtime.role_switch import RoleSwitcher, engine_side
from ...types import Role
from ..generation import read_generation
from ..model import Profile, decode_evidence_problems
from ..store import ProfileStore
from ..tasks import cancel_tasks
from .engine import engine_context_limit
from .instance import profile_instance
from .neighbours import ColocatedWorkload, NeighbourLoad
from .pairing import PairedTransport, paired_transport
from .sweep import Sweep, bounded_sweep, load_sequence_limits


class _Unhealthy(Exception): ...


# The longest profile phase, a full decode sweep, ends well inside this wait.
PHASE_WAIT_S = 3600.0


def device_key(spec: EngineSpec) -> str:
    return spec.shared_device.group if spec.shared_device is not None else f"engine:{spec.iid}"


def _profile_lanes(
    targets: list[EngineSpec], *, colocated: bool, paired: bool = False
) -> list[list[EngineSpec]]:
    # Engines that share a device, or a pair across one, take turns.
    if colocated or (paired and any(spec.shared_device is not None for spec in targets)):
        return [list(targets)]
    if paired:
        # Each pair lends its engines to each other, so disjoint pairs run side by side.
        pairs = [list(targets[i : i + 2]) for i in range(0, len(targets), 2)]
        if len(pairs) > 1 and len(pairs[-1]) == 1:
            pairs[-2].extend(pairs.pop())
        return pairs
    lanes: dict[str, list[EngineSpec]] = {}
    for spec in targets:
        lanes.setdefault(device_key(spec), []).append(spec)
    return list(lanes.values())


def _pool_size(sweep: Sweep, lanes: int, engines: int, *, paired: bool) -> int:
    # A paired request holds a connection for each leg until both finish.
    legs = 2 if paired else 1
    return max(sweep.decode_concurrency) * legs * lanes + engines


def _peer(spec: EngineSpec, engines: list[EngineSpec]) -> EngineSpec:
    index = next(i for i, engine in enumerate(engines) if engine.iid == spec.iid)
    return engines[(index + 1) % len(engines)]


def _lane_peer(spec: EngineSpec, lane: list[EngineSpec], engines: list[EngineSpec]) -> EngineSpec:
    return _peer(spec, lane) if len(lane) > 1 else _peer(spec, engines)


async def _reset_caches(
    client: httpx.AsyncClient, dialect: EngineDialect, urls: tuple[str, ...]
) -> None:
    # A peer still holding the previous sweep's cache evicts it to receive KV, which slows the
    # measured prefill, so each placement starts both engines with empty caches.
    path = dialect.cache_reset_path
    if path is None:
        return
    for url in urls:
        try:
            r = await client.post(f"{url}{path}")
        except httpx.HTTPError as exc:
            raise RuntimeError(f"cache reset failed on {url}: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise RuntimeError(f"cache reset failed on {url} ({r.status_code}): {r.text[:200]}")


def _pair_roles(
    cfg: FleetConfig,
    dialect: EngineDialect,
    pairing: PairedTransport,
    switcher: RoleSwitcher,
    spec: EngineSpec,
    peer: EngineSpec,
    launches: dict[str, dict],
    phases: Any = None,
) -> Callable[[Role], Awaitable[None]]:
    async def serve_as(role: Role) -> None:
        if phases is not None:
            # Lanes switch phase together; a lane's KV transfers slow another lane's prefill.
            await asyncio.to_thread(phases.wait, PHASE_WAIT_S)
        own, other = (spec.url, launches.get(spec.iid)), (peer.url, launches.get(peer.iid))
        producer, consumer = (own, other) if role is Role.PREFILL else (other, own)
        async with httpx.AsyncClient(
            timeout=cfg.health_timeout_s, headers=cfg.engine_headers()
        ) as control:
            await switcher.place(control, producer, consumer)
            await _reset_caches(control, dialect, (spec.url, peer.url))
        pairing.pair(spec.url, role, peer.url, producer[1])

    return serve_as


def _lane_worker(
    cfg: FleetConfig,
    only: set[str],
    sweep: Sweep | None,
    limits_path: Path | None,
    observation_timeout_s: float | None,
    phases: Any,
) -> int:
    return asyncio.run(
        run(
            cfg,
            only,
            sweep,
            overwrite=True,
            limits_path=limits_path,
            observation_timeout_s=observation_timeout_s,
            split_lanes=False,
            phases=phases,
        )
    )


def _stand_in(phases: Any) -> None:
    # A finished lane keeps arriving at phase switches, so a lane with more switches goes on.
    with contextlib.suppress(threading.BrokenBarrierError):
        while True:
            phases.wait(PHASE_WAIT_S)


async def _run_lanes_apart(
    cfg: FleetConfig,
    lanes: list[list[EngineSpec]],
    sweep: Sweep | None,
    store: ProfileStore,
    evidence_path: Path,
    measurement_record: dict[str, object],
    evidence_rows: dict[str, object],
    *,
    overwrite: bool,
    limits_path: Path | None,
    observation_timeout_s: float | None,
) -> int:
    # One event loop cannot stream every lane's decode cohort and dispatch its paired legs on
    # time, so each lane measures in its own process.
    loop = asyncio.get_running_loop()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=store.path.parent) as folder:
        lane_cfgs = []
        for index, _ in enumerate(lanes):
            lane_cfg = copy.copy(cfg)
            lane_cfg.profiles_path = Path(folder) / f"lane-{index}.json"
            lane_cfgs.append(lane_cfg)
        context = multiprocessing.get_context("spawn")
        with (
            context.Manager() as manager,
            ProcessPoolExecutor(max_workers=len(lanes), mp_context=context) as pool,
        ):
            # Lanes of one length pass the same phase switches, so they can keep in step.
            phases = (
                manager.Barrier(len(lanes)) if len({len(lane) for lane in lanes}) == 1 else None
            )
            futures = [
                loop.run_in_executor(
                    pool,
                    _lane_worker,
                    lane_cfg,
                    {spec.iid for spec in lane},
                    sweep,
                    limits_path,
                    observation_timeout_s,
                    phases,
                )
                for lane_cfg, lane in zip(lane_cfgs, lanes, strict=True)
            ]
            outcomes: list[BaseException | int] = []
            stand_ins: list[asyncio.Future[None]] = []
            for finished in asyncio.as_completed(futures):
                try:
                    outcomes.append(await finished)
                except Exception as error:
                    outcomes.append(error)
                if phases is None:
                    continue
                if outcomes[-1] != 0 or len(outcomes) == len(futures):
                    # The first failure stops the lanes waiting for it at a phase switch, and
                    # the last lane to finish stops the stand-ins.
                    phases.abort()
                else:
                    stand_ins.append(loop.run_in_executor(None, _stand_in, phases))
            await asyncio.gather(*stand_ins)
        profiles: list[Profile] = []
        for lane_cfg in lane_cfgs:
            lane_evidence = lane_cfg.profiles_path.with_suffix(".samples.json")
            if lane_evidence.is_file():
                evidence_rows.update(json.loads(lane_evidence.read_text())["engines"])
            if lane_cfg.profiles_path.is_file():
                profiles.extend(ProfileStore(lane_cfg.profiles_path).all_profiles())
    if evidence_rows:
        with evidence_path.open("w" if overwrite else "x", encoding="utf-8") as output:
            output.write(json.dumps(measurement_record, indent=2) + "\n")
    if profiles and not overwrite:
        with store.path.open("x", encoding="utf-8"):
            pass
    for profile in profiles:
        store.put(profile)
    codes = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
        codes.append(outcome)
    failed = next((code for code in codes if code), 0)
    if failed:
        return failed
    print(f"wrote {len(store)} profile(s) to {cfg.profiles_path}")
    return 0


async def run(
    cfg: FleetConfig,
    only: set[str] | None,
    sweep: Sweep | None = None,
    *,
    overwrite: bool = False,
    limits_path: Path | None = None,
    colocated_workload: ColocatedWorkload | None = None,
    observation_timeout_s: float | None = None,
    split_lanes: bool = True,
    phases: Any = None,
) -> int:
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
    if cfg.engine_contract is not None and (
        missing := [spec.iid for spec in targets if not spec.attestation_url]
    ):
        raise ValueError(f"profile generation needs an attestation_url: {', '.join(missing)}")
    limits = (
        load_sequence_limits(limits_path, {engine.iid for engine in cfg.engines})
        if limits_path is not None
        else {}
    )

    print(f"profiling {len(targets)} instance(s) against model {cfg.model}")
    backend = load_backend(cfg.backend)
    dialect = backend.dialect
    metrics = backend.metrics
    kv = backend.connector(cfg.connector)
    switcher = engine_side(backend.role_switcher(cfg.connector))
    paired = kv.concurrent
    if paired and switcher is None:
        raise ValueError(
            f"profiling measures both roles of each engine, and engine.connector "
            f"{cfg.connector} keeps {backend.label} engine roles fixed"
        )
    if paired and colocated_workload is not None:
        raise ValueError(f"--colocated profiling needs a {backend.label} engine to serve alone")
    if paired and len(cfg.engines) < 2:
        raise ValueError(f"profiling a {backend.label} engine needs a peer engine")
    evidence_rows: dict[str, object] = {}
    measurement_record = {
        "method_version": 2,
        **stamp(),
        "model": cfg.model,
        "sweep": asdict(sweep or Sweep()),
        "observation_timeout_s": observation_timeout_s,
        "engines": evidence_rows,
    }
    # A paired engine borrows a peer from its lane for its other leg.
    lanes = _profile_lanes(targets, colocated=colocated_workload is not None, paired=paired)
    if paired and split_lanes and len(lanes) > 1:
        return await _run_lanes_apart(
            cfg,
            lanes,
            sweep,
            store,
            evidence_path,
            measurement_record,
            evidence_rows,
            overwrite=overwrite,
            limits_path=limits_path,
            observation_timeout_s=observation_timeout_s,
        )
    connections = _pool_size(sweep or Sweep(), len(lanes), len(cfg.engines), paired=paired)
    pool = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)
    pairing, launches = await paired_transport(
        lambda: httpx.AsyncHTTPTransport(limits=pool),
        kv,
        dialect,
        cfg.engines,
        timeout_s=observation_timeout_s or cfg.health_timeout_s,
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            observation_timeout_s or 300.0,
            connect=min(10.0, observation_timeout_s or 300.0),
        ),
        limits=pool,
        headers=cfg.engine_headers(),
        transport=pairing,
    ) as client:

        async def profile_engine(spec: EngineSpec, lane: list[EngineSpec]) -> None:
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
                    cfg, spec, timeout_s=observation_timeout_s or cfg.health_timeout_s
                )
                engine_evidence["generation_evidence"] = generation.document
                roles = None
                if pairing is not None and switcher is not None:
                    peer = (
                        _peer(spec, cfg.engines)
                        if colocated_workload is not None
                        else _lane_peer(spec, lane, cfg.engines)
                    )
                    engine_evidence["peer"] = peer.iid
                    roles = _pair_roles(
                        cfg, dialect, pairing, switcher, spec, peer, launches, phases
                    )
                profile = await profile_instance(
                    client,
                    spec.iid,
                    spec.url,
                    cfg.model,
                    dialect,
                    engine_sweep,
                    cfg.chars_per_token,
                    metrics=metrics,
                    evidence=engine_evidence,
                    max_model_len=max_model_len,
                    observation_timeout_s=observation_timeout_s,
                    roles=roles,
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
                    cfg, spec, timeout_s=observation_timeout_s or cfg.health_timeout_s
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
                    await cancel_tasks(neighbour_load.tasks)
                    engine_evidence["colocated_load"] = neighbour_load.evidence()
                engine_evidence["error"] = f"{type(exc).__name__}: {exc}"
                evidence_rows[spec.iid] = engine_evidence
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                with evidence_path.open(
                    "x" if len(evidence_rows) == 1 and not overwrite else "w", encoding="utf-8"
                ) as output:
                    output.write(json.dumps(measurement_record, indent=2) + "\n")
                if isinstance(exc, ValueError):
                    # The inputs passed validation, so a measurement that fails here is not invalid
                    # input.
                    detail = str(exc)
                    if not detail.startswith(f"{spec.iid}:"):
                        detail = f"{spec.iid}: {detail}"
                    raise RuntimeError(detail) from exc
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
                await profile_engine(spec, lane)

        tasks = [asyncio.create_task(profile_lane(lane)) for lane in lanes]
        try:
            await asyncio.gather(*tasks)
        except _Unhealthy:
            await cancel_tasks(tasks)
            return 1
        except BaseException:
            await cancel_tasks(tasks)
            raise
    print(f"wrote {len(store)} profile(s) to {cfg.profiles_path}")
    return 0
