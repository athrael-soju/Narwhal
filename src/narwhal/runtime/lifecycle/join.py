"""Admit an engine to the fleet after the readmission checks pass."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from ...config import EngineSpec
from ...profiling.membership import check_engine_set
from ...types import RoleModel
from .readmission import validate_readmission
from .records import ValidationOutcome

if TYPE_CHECKING:
    from ...serving.router.routing import NarwhalRouter

log = logging.getLogger("narwhal.lifecycle")


@dataclass
class JoinOutcome:
    """The result of one join request."""

    iid: str
    joined: bool
    generation: int
    # The reason a precondition refused the join before any engine was contacted.
    refused: str = ""
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def error(self) -> str:
        return self.refused or "; ".join(self.failures)


def join_refusal(router: NarwhalRouter, spec: EngineSpec) -> str:
    """Return why the fleet cannot take `spec` now without contacting any engine, or ""."""
    cfg = router.cfg
    if cfg.engine_restart_policy == "whole_wave":
        return "engine_restart_policy whole_wave changes engines only as one managed wave"
    if cfg.engine_contract is None or cfg.contract_missing():
        return "joining an engine requires a complete engine_contract"
    if spec.iid in router.membership:
        return f"engine {spec.iid} is already a member"
    if any(member.url.rstrip("/") == spec.url.rstrip("/") for member in router.membership):
        return f"another member already serves {spec.url}"
    if spec.shared_device is not None:
        return "an engine in a shared-device group cannot join"
    if not spec.attestation_url:
        return "attestation_url is not configured"
    if router.role_switch.model is RoleModel.FIXED and not spec.pin:
        return "engines with fixed roles must set pin"
    held = sorted(iid for iid, rec in router.lifecycle.records.items() if rec.state != "active")
    if held:
        return f"lifecycle action active for {', '.join(held)}"
    return ""


def _forget_candidate(router: NarwhalRouter, spec: EngineSpec) -> None:
    for table in (router.sequence_limits, router.kv_leases, router.launches):
        table.pop(spec.iid, None)
    router.role_switch.forget(spec.url)


async def join_engine(
    router: NarwhalRouter,
    spec: EngineSpec,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> JoinOutcome:
    """Validate `spec` out of placement and add it to a new generation when every check passes.

    The checks are the readmission checks: health, process identity, attestation against the
    engine contract, profile generation, the served model, one generation and a KV pair
    handoff with a live peer. The reloaded profile store must hold a profile for exactly the
    proposed engine set, and a configured first-token calibration must cover every pair the new
    engine forms. The caller holds `router.lifecycle.lock`.
    """
    manager = router.lifecycle
    current = router.membership
    if refusal := join_refusal(router, spec):
        manager._emit("join_refused", iid=spec.iid, generation=current.generation, error=refusal)
        return JoinOutcome(spec.iid, False, current.generation, refused=refusal)
    manager._emit("join_started", iid=spec.iid, generation=current.generation)
    proposed = current.joined(spec)
    scheduler = router.scheduler
    live = {
        iid for iid in current.ids if iid not in scheduler.ejected and iid not in scheduler.draining
    } | {spec.iid}
    evidence = await check_engine_set(router.cfg, proposed.specs, live=live, transport=transport)
    outcome = ValidationOutcome()
    if evidence.problems:
        for problem in evidence.problems:
            outcome.fail(spec.iid, problem)
    else:
        try:
            outcome = await validate_readmission(
                router,
                [spec.iid],
                wave=False,
                transport=transport,
                candidate=spec,
                profiles=evidence.profiles,
            )
        except Exception as exc:
            log.exception("join validation of %s failed", spec.iid)
            outcome = ValidationOutcome()
            outcome.fail(spec.iid, f"validation raised {type(exc).__name__}: {exc}")
    if outcome.passed and router.membership is not current:
        outcome.fail(spec.iid, "membership changed during validation")
    if outcome.passed and (refusal := join_refusal(router, spec)):
        outcome.fail(spec.iid, refusal)
    if not outcome.passed or spec.iid not in outcome.starts or evidence.profiles is None:
        if outcome.passed:
            outcome.fail(spec.iid, "validation recorded no process identity")
        _forget_candidate(router, spec)
        failures = list(outcome.failures.get(spec.iid, [])) or [
            "; ".join(f"{iid}: {'; '.join(items)}" for iid, items in outcome.failures.items())
        ]
        manager._emit(
            "join_refused",
            iid=spec.iid,
            generation=router.membership.generation,
            error="; ".join(failures),
        )
        return JoinOutcome(
            spec.iid,
            False,
            router.membership.generation,
            checks=list(outcome.checks.get(spec.iid, [])),
            failures=failures,
        )
    router.profiles.adopt(evidence.profiles)
    if evidence.calibration.status != "uncalibrated":
        router.first_token_calibration = evidence.calibration
    router.apply_membership(proposed)
    manager.process_starts[spec.iid] = outcome.starts[spec.iid]
    manager._emit(
        "joined",
        iid=spec.iid,
        generation=proposed.generation,
        process_start=outcome.starts[spec.iid],
        role=spec.role.value,
    )
    log.info("joined %s in generation %d as %s", spec.iid, proposed.generation, spec.role.value)
    return JoinOutcome(
        spec.iid, True, proposed.generation, checks=list(outcome.checks.get(spec.iid, []))
    )
