from __future__ import annotations

import asyncio
import json
import math
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from pathlib import Path

import httpx

from ... import command_results as results
from ...backends import load as load_backend
from ...config import EngineSpec, FleetConfig
from ...engines.attestation import attested_launches
from ...engines.connector import RendezvousConnector
from ...provenance import stamp
from ...runtime.role_switch import RoleSwitcher, engine_side, place_pair
from ...types import Role
from ..generation import read_generation
from ..model import decode_evidence_problems
from ..store import ProfileStore
from ..tasks import cancel_tasks
from .engine import engine_context_limit
from .instance import profile_instance
from .neighbours import ColocatedWorkload, NeighbourLoad
from .pairing import PairedTransport, Pairing, origin
from .sweep import Sweep, bounded_sweep, load_sequence_limits


class _Unhealthy(Exception): ...


def device_key(spec: EngineSpec) -> str:
    return spec.shared_device.group if spec.shared_device is not None else f"engine:{spec.iid}"


def _profile_lanes(targets: list[EngineSpec], *, colocated: bool) -> list[list[EngineSpec]]:
    if colocated:
        return [list(targets)]
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


def _pair_roles(
    cfg: FleetConfig,
    pairing: PairedTransport,
    switcher: RoleSwitcher,
    spec: EngineSpec,
    peer: EngineSpec,
    launches: dict[str, dict],
) -> Callable[[Role], Awaitable[None]]:
    async def serve_as(role: Role) -> None:
        own, other = (spec.url, launches.get(spec.iid)), (peer.url, launches.get(peer.iid))
        producer, consumer = (own, other) if role is Role.PREFILL else (other, own)
        async with httpx.AsyncClient(
            timeout=cfg.health_timeout_s, headers=cfg.engine_headers()
        ) as control:
            await place_pair(switcher, control, producer, consumer)
        pairing.pairs[origin(spec.url)] = Pairing(role, origin(peer.url), producer[1] or {})

    return serve_as


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
    backend = load_backend(cfg.backend)
    dialect = backend.dialect
    metrics = backend.metrics
    kv = backend.connector(cfg.connector)
    switcher = engine_side(backend.role_switcher(cfg.connector))
    paired = isinstance(kv, RendezvousConnector)
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
    # A paired engine borrows a peer for its other leg, so engines take turns.
    lanes = _profile_lanes(targets, colocated=colocated_workload is not None or paired)
    connections = _pool_size(sweep or Sweep(), len(lanes), len(cfg.engines), paired=paired)
    pool = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)
    pairing = (
        PairedTransport(httpx.AsyncHTTPTransport(limits=pool), kv, dialect)
        if isinstance(kv, RendezvousConnector)
        else None
    )
    launches = (
        await attested_launches(
            cfg.engines, timeout_s=observation_timeout_s or cfg.health_timeout_s
        )
        if pairing is not None
        else {}
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
                    reader=load_backend(cfg.backend).identity,
                )
                engine_evidence["generation_evidence"] = generation.document
                roles = None
                if pairing is not None and switcher is not None:
                    peer = _peer(spec, cfg.engines)
                    engine_evidence["peer"] = peer.iid
                    roles = _pair_roles(cfg, pairing, switcher, spec, peer, launches)
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
                    spec,
                    cfg.engine_contract,
                    timeout_s=observation_timeout_s or cfg.health_timeout_s,
                    headers=cfg.engine_headers(),
                    reader=load_backend(cfg.backend).identity,
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
            await cancel_tasks(tasks)
            return 1
        except BaseException:
            await cancel_tasks(tasks)
            raise
    print(f"wrote {len(store)} profile(s) to {cfg.profiles_path}")
    return 0
