"""Bind, capture and verify engine process identities."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from ...config import EngineSpec, FleetConfig
from ...engines.attestation import fetch_engine_identity, verify_attestation
from ...profiling.generation import binding_digest, profile_generation_problems, read_generation
from .manager import LifecycleManager

if TYPE_CHECKING:
    from ...serving.router.routing import NarwhalRouter


async def check_process_identities(
    router: NarwhalRouter, engines: list[str] | None = None
) -> list[str]:
    """Bind initial identities and exclude changed or unverifiable processes."""
    from ..standby import controls_fleet

    cfg: FleetConfig = router.cfg
    manager: LifecycleManager = router.lifecycle
    contract = cfg.engine_contract
    if contract is None:
        # For contract-free fleets, only startup and takeover release the profile gate.
        return []
    async with manager.lock:
        if not controls_fleet(router):
            return []
        if cfg.engine_restart_policy == "whole_wave" and router.scheduler.ejected:
            manager.require_restart_wave("an engine was excluded")
        if manager.wave_id:
            manager.identities_ready = True
            return []
        selected = set(engines) if engines is not None else set(router.monitor.instances)
        specs = [
            spec
            for spec in cfg.engines
            if spec.iid in selected
            and spec.iid not in router.scheduler.ejected
            and spec.iid not in router.scheduler.draining
        ]

        async def inspect(spec: EngineSpec) -> tuple[float | None, str]:
            try:
                identity = await fetch_engine_identity(
                    spec.url,
                    timeout_s=cfg.health_timeout_s,
                    transport=router.lifecycle_transport,
                    headers=router.engines._auth(None),
                )
                if not spec.attestation_url:
                    return None, "attestation_url is not configured"
                async with httpx.AsyncClient(
                    timeout=cfg.health_timeout_s,
                    transport=router.lifecycle_transport,
                ) as client:
                    response = await client.get(spec.attestation_url)
                    response.raise_for_status()
                payload = response.json()
                failures = verify_attestation(payload, contract, identity)
                if failures:
                    return None, "attestation: " + "; ".join(failures)
                problems = profile_generation_problems(
                    router.profiles, spec.iid, binding_digest(payload)
                )
                if problems:
                    return None, "; ".join(problems)
                router.attested(spec.iid, payload)
                return identity.process_start_time_seconds, ""
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                return None, f"process identity unavailable: {type(exc).__name__}"

        results = await asyncio.gather(*(inspect(spec) for spec in specs))
        if not controls_fleet(router):
            return []
        excluded: list[str] = []
        for spec, (start, error) in zip(specs, results, strict=True):
            previous = manager.process_starts.get(spec.iid)
            changed = start is not None and previous is not None and start != previous
            if error or changed:
                router.scheduler.eject(spec.iid, "process_identity")
                excluded.append(spec.iid)
                manager._emit(
                    "process_excluded",
                    iid=spec.iid,
                    reason=error or "engine process changed",
                    previous_process_start=previous,
                    observed_process_start=start,
                )
            elif start is not None:
                manager.process_starts[spec.iid] = start
        if excluded and cfg.engine_restart_policy == "whole_wave":
            manager.require_restart_wave("engine identity changed or could not be verified")
        manager.identities_ready = True
        return excluded


async def capture_process_identities(
    cfg: FleetConfig,
    engines: list[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """Read the live process identity before an external supervisor stops it."""
    specs = {spec.iid: spec for spec in cfg.engines}
    starts: dict[str, float] = {}
    failures: dict[str, str] = {}
    if cfg.engine_contract is None or cfg.engine_contract.missing():
        detail = "lifecycle drain requires a complete engine_contract"
        return {}, dict.fromkeys(engines, detail)
    for iid in engines:
        try:
            identity = await fetch_engine_identity(
                specs[iid].url,
                timeout_s=cfg.health_timeout_s,
                transport=transport,
                headers=cfg.engine_headers(),
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            failures[iid] = f"process identity unreadable: {type(exc).__name__}"
        else:
            starts[iid] = identity.process_start_time_seconds
    return starts, failures


async def allow_profile_recovery(router: NarwhalRouter, iid: str) -> bool:
    """Keep health and inference probes from restoring stale or operator-held engines."""
    from ..standby import controls_fleet

    manager = router.lifecycle
    async with manager.lock:
        if (
            not controls_fleet(router)
            or router.lifecycle_blocked
            or iid in router.scheduler.draining
        ):
            return False
        if router.cfg.engine_restart_policy == "whole_wave" and iid in router.scheduler.ejected:
            manager.require_restart_wave("engine recovery requires a managed whole-wave restart")
            return False
        spec = next(spec for spec in router.cfg.engines if spec.iid == iid)
        try:
            generation = await read_generation(
                spec,
                router.cfg.engine_contract,
                timeout_s=router.cfg.health_timeout_s,
                headers=router.cfg.engine_headers(),
                transport=router.lifecycle_transport,
            )
            problems = profile_generation_problems(router.profiles, iid, generation.digest)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            problems = [f"{iid} profile generation unreadable: {type(exc).__name__}"]
        if (
            not controls_fleet(router)
            or router.lifecycle_blocked
            or iid in router.scheduler.draining
        ):
            return False
        if not problems:
            return True
        router.scheduler.eject(iid, "profile_generation")
        manager._emit("profile_recovery_blocked", iid=iid, error="; ".join(problems))
        if router.cfg.engine_restart_policy == "whole_wave":
            manager.require_restart_wave("profile generation could not be verified")
        return False
