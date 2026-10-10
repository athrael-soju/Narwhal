from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from ... import command_results as results
from ...backends import load as load_backend
from ...config import FleetConfig
from ...engines.client import EngineClient
from .engines import (
    colocated_restart_risk,
    gate_calibration,
    gate_contract,
    gate_model,
    gate_pace,
    gate_reach,
    gate_tokenize,
)
from .evidence import check_directed_evidence, write_directed_evidence
from .profiles import gate_profile, gate_profile_generation, gate_slo
from .report import Report
from .transfer import gate_consume, gate_produce


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
    # Preflight uses the serving timeouts.
    client = EngineClient(
        timeout_s=cfg.request_timeout_s,
        prefill_timeout_s=cfg.prefill_timeout_s,
        read_timeout_s=cfg.decode_read_timeout_s,
        max_connections=cfg.max_connections,
        control_connections=cfg.resolved_control_connections(),
        pool_timeout_s=cfg.pool_timeout_s,
        connect_timeout_s=cfg.connect_timeout_s,
        health_timeout_s=cfg.health_timeout_s,
        kv=load_backend(cfg.backend).connector(cfg.connector),
        dialect=load_backend(cfg.backend).dialect,
        model=cfg.model,
        engine_api_key=cfg.resolve_engine_key(),
    )
    try:
        live = await gate_reach(cfg, client, rep)
        if restart_risk := await colocated_restart_risk(cfg):
            rep.warn(restart_risk)
        await gate_calibration(cfg, rep)
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
            # A stalled transfer can terminate a healthy peer's engine core.
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
            await check_directed_evidence(
                cfg, store, rep, repeats, fleet_path, fleet_hash, profile_hash
            )
    finally:
        await client.aclose()

    if evidence_out is not None:
        write_directed_evidence(
            cfg, rep, repeats, evidence_out, fleet_path, fleet_hash, profile_hash
        )

    results.set_data(
        {
            "failed": rep.failed,
            "skipped": rep.skipped,
            "warnings": rep.warnings,
            "pairs": rep.pairs,
            "first_token_calibration": rep.calibration,
        }
    )
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
