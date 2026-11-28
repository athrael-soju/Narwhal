from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from ...config import EngineSpec, FleetConfig
from ...engines.attestation import (
    AttestedIdentity,
    IdentityRead,
    attest_identity,
    fetch_spec_identity,
    read_attestation,
    read_process_start_of,
)
from ...profiling.generation import (
    AttestationMismatch,
    binding_digest,
    bound_profiles,
    profile_generation_problems,
    read_generation,
)
from .manager import LifecycleManager

if TYPE_CHECKING:
    from ...serving.router.routing import NarwhalRouter


async def attest_member(
    router: NarwhalRouter,
    spec: EngineSpec,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    payload: Any = None,
    identify: IdentityRead | None = None,
) -> AttestedIdentity:
    """Read an engine's attestation, unless `payload` holds it, then its identity in that order.

    A trusted endpoint that now runs another process, or whose attestation fails, loses the
    router's trust before any request of this check could carry the credential. The caller
    grants trust again once the attestation verifies.
    """
    transport = router.lifecycle_transport if transport is None else transport
    if payload is None:
        payload = await read_attestation(
            spec, timeout_s=router.cfg.health_timeout_s, transport=transport
        )
    attested = await attest_identity(
        router.cfg,
        spec,
        payload,
        transport=transport,
        trusted_start=router.trust.start(spec.url),
        identify=fetch_spec_identity if identify is None else identify,
    )
    start = attested.identity.process_start_time_seconds
    if attested.failures or not router.trust.observe(spec.url, start):
        router.trust.revoke(spec.url)
    return attested


async def check_process_identities(
    router: NarwhalRouter, engines: list[str] | None = None
) -> list[str]:
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
        selected = set(engines) if engines is not None else set(router.membership.ids)
        specs = [
            spec
            for spec in router.membership
            if spec.iid in selected
            and spec.iid not in router.scheduler.ejected
            and spec.iid not in router.scheduler.draining
        ]

        async def inspect(spec: EngineSpec) -> tuple[float | None, str]:
            try:
                attested = await attest_member(router, spec)
                if attested.failures:
                    return None, "attestation: " + "; ".join(attested.failures)
                payload = attested.payload
                problems = profile_generation_problems(
                    router.profiles, spec.iid, binding_digest(payload), contract=True
                )
                if problems:
                    return None, "; ".join(problems)
                start = attested.identity.process_start_time_seconds
                router.trust.grant(spec.url, start)
                router.attested(spec.iid, payload)
                if manager.process_starts.get(spec.iid) != start:
                    # A new engine process starts in its launch role. A known process keeps
                    # its role, and a switch would hold it out of placement.
                    switched = await router.resume_role(spec.iid)
                    if switched is not None:
                        return None, f"role switch failed: {switched}"
                return start, ""
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                router.trust.revoke(spec.url)
                return None, f"process identity unavailable: {type(exc).__name__}"

        results = await asyncio.gather(*(inspect(spec) for spec in specs))
        if not controls_fleet(router):
            return []
        excluded: list[str] = []
        for spec, (start, error) in zip(specs, results, strict=True):
            previous = manager.process_starts.get(spec.iid)
            changed = start is not None and previous is not None and start != previous
            if error or changed:
                router.trust.revoke(spec.url)
                router.scheduler.eject(spec.iid, "process_identity")
                excluded.append(spec.iid)
                manager._emit(
                    "process_excluded",
                    iid=spec.iid,
                    reason=error or "engine process changed",
                    previous_process_start=previous,
                    observed_process_start=start,
                )
            elif start is not None and spec.iid in router.membership:
                manager.process_starts[spec.iid] = start
        if excluded and cfg.engine_restart_policy == "whole_wave":
            manager.require_restart_wave("engine identity changed or could not be verified")
        manager.identities_ready = True
        return excluded


async def capture_process_identities(
    cfg: FleetConfig,
    engines: list[str],
    *,
    specs: dict[str, EngineSpec],
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """Read each engine's process start for its drain record, with no credential."""
    starts: dict[str, float] = {}
    failures: dict[str, str] = {}
    if cfg.engine_contract is None or cfg.contract_missing():
        detail = "lifecycle drain requires a complete engine_contract"
        return {}, dict.fromkeys(engines, detail)
    for iid in engines:
        try:
            start = await read_process_start_of(
                cfg, specs[iid], transport=transport, identify=fetch_spec_identity
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            failures[iid] = f"process identity unreadable: {type(exc).__name__}"
        else:
            starts[iid] = start
    return starts, failures


async def allow_profile_recovery(router: NarwhalRouter, iid: str) -> bool:
    from ..standby import controls_fleet

    manager = router.lifecycle
    async with manager.lock:
        if (
            not controls_fleet(router)
            or router.lifecycle_blocked
            or iid in router.scheduler.draining
            or iid not in router.membership
        ):
            return False
        if router.cfg.engine_restart_policy == "whole_wave" and iid in router.scheduler.ejected:
            manager.require_restart_wave("engine recovery requires a managed whole-wave restart")
            return False
        spec = router.membership.spec(iid)
        try:
            live = ""
            if bound_profiles(router.profiles, iid) and router.cfg.engine_contract is not None:
                attested = await attest_member(router, spec)
                if attested.failures:
                    raise AttestationMismatch("; ".join(attested.failures))
                router.trust.grant(spec.url, attested.identity.process_start_time_seconds)
                live = binding_digest(attested.payload)
            elif bound_profiles(router.profiles, iid):
                generation = await read_generation(
                    router.cfg, spec, transport=router.lifecycle_transport
                )
                live = generation.digest
            problems = profile_generation_problems(
                router.profiles, iid, live, contract=router.cfg.engine_contract is not None
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            problems = [
                f"{iid} profile generation unreadable: {type(exc).__name__}; "
                "restart its attestation sidecar if it answers HTTP 503"
            ]
        if (
            not controls_fleet(router)
            or router.lifecycle_blocked
            or iid in router.scheduler.draining
            or iid not in router.membership
        ):
            return False
        if not problems:
            return True
        router.scheduler.eject(iid, "profile_generation")
        manager._emit("profile_recovery_blocked", iid=iid, error="; ".join(problems))
        if router.cfg.engine_restart_policy == "whole_wave":
            manager.require_restart_wave("profile generation could not be verified")
        return False
