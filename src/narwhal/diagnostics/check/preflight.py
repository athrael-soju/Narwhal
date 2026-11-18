from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from ... import command_results as results
from ...backends import load as load_backend
from ...config import FleetConfig
from ...engines.attestation import attested_launches
from ...engines.client import EngineClient, client_options
from ...observability.tools import ToolActivity
from ...runtime.role_switch import RoleSwitcher, current_roles, restore_roles
from ...types import Role
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


async def _live_roles(
    cfg: FleetConfig, live: set[str], switcher: RoleSwitcher
) -> dict[str, tuple[Role, Mapping[str, Any] | None]]:
    specs = [spec for spec in cfg.engines if spec.iid in live]
    launches = await attested_launches(specs, timeout_s=cfg.health_timeout_s)
    async with httpx.AsyncClient(
        timeout=cfg.health_timeout_s, headers=cfg.engine_headers()
    ) as control:
        return await current_roles(
            switcher, control, {spec.url: launches.get(spec.iid) for spec in specs}
        )


async def run(
    cfg: FleetConfig,
    mesh: bool,
    skip_kv: bool,
    repeats: int = 1,
    *,
    report: Report | None = None,
    evidence_out: Path | None = None,
    fleet_path: Path | None = None,
    activity: ToolActivity | None = None,
) -> int:
    activity = activity or ToolActivity("preflight")
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
    client = EngineClient(**client_options(cfg))
    # The gates switch engines to place each pair; a running router keeps the roles it set.
    switcher = load_backend(cfg.backend).role_switcher(cfg.connector)
    roles: dict[str, tuple[Role, Mapping[str, Any] | None]] = {}
    # Reach, calibration, contract, profile, generation, model, pace, tokenize, produce,
    # consume and SLO, then the evidence check when evidence is written.
    activity.plan(11 + (evidence_out is not None))
    activity.use(spec.iid for spec in cfg.engines)
    try:
        live = await gate_reach(cfg, client, rep)
        activity.use(live)
        activity.advance()
        if switcher.engine_held:
            roles = await _live_roles(cfg, live, switcher)
        if restart_risk := await colocated_restart_risk(cfg):
            rep.warn(restart_risk)
        await gate_calibration(cfg, rep)
        activity.advance()
        incompatible = await gate_contract(cfg, live, rep)
        activity.advance()
        store = gate_profile(cfg, rep)
        activity.advance()
        stale = await gate_profile_generation(cfg, store, live, rep)
        activity.advance()
        incompatible.update(stale)
        incompatible.update(await gate_model(cfg, live, rep))
        activity.advance()
        pace_store = store if not stale else None
        slow = await gate_pace(cfg, live, rep, pace_store)
        activity.advance()
        await gate_tokenize(cfg, live, client, rep)
        activity.advance()
        if skip_kv:
            rep.skip("produce and consume: skipped by --no-kv")
            activity.advance(2)
        elif slow or incompatible:
            # A stalled transfer can terminate a healthy peer's engine core.
            blocked = slow | incompatible
            names = ", ".join(sorted(blocked))
            rep.skip(f"produce and consume: pre-transfer gate failed on {names}")
            activity.advance(2)
        else:
            handoffs = await gate_produce(cfg, live, client, rep)
            activity.advance()
            if evidence_out is None:
                await gate_consume(cfg, live, handoffs, client, rep, mesh, repeats)
            else:
                await gate_consume(
                    cfg, live, handoffs, client, rep, mesh, repeats, evidence=rep.pairs
                )
            activity.advance()
        gate_slo(cfg, store, rep)
        activity.advance()
        if evidence_out is not None:
            await check_directed_evidence(
                cfg, store, rep, repeats, fleet_path, fleet_hash, profile_hash
            )
            activity.advance()
    finally:
        try:
            if switcher.engine_held and roles:
                async with httpx.AsyncClient(
                    timeout=cfg.health_timeout_s, headers=cfg.engine_headers()
                ) as control:
                    await restore_roles(switcher, control, roles)
        except (httpx.HTTPError, ValueError) as exc:
            rep.fail(f"role restore: {type(exc).__name__}: {exc}")
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
