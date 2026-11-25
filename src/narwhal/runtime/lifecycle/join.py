"""Admit an engine to the fleet after the readmission checks pass."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from ...config import EngineSpec
from ...profiling.generation import AttestationMismatch, read_generation
from ...profiling.membership import check_engine_set
from ...types import RoleModel
from ..standby import controls_fleet
from .readmission import validate_readmission
from .records import ValidationOutcome

if TYPE_CHECKING:
    from ...serving.router.membership import Membership
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


IID = re.compile(r"[A-Za-z0-9._-]+")


def spec_problem(spec: EngineSpec) -> str:
    """Return why `spec` is not a valid engine entry for a join, or ""."""
    if not IID.fullmatch(spec.iid):
        return "iid must be nonempty and hold only letters, digits, '.', '_' and '-'"
    for name, url in (("url", spec.url), ("attestation_url", spec.attestation_url)):
        parts = urlsplit(url)
        # An empty attestation_url is refused later with its own reason.
        if (url or name == "url") and (parts.scheme not in ("http", "https") or not parts.hostname):
            return f"{name} must be an http or https URL with a host"
    return ""


def join_refusal(router: NarwhalRouter, spec: EngineSpec) -> str:
    """Return why the fleet cannot take `spec` now without contacting any engine, or ""."""
    cfg = router.cfg
    if problem := spec_problem(spec):
        return problem
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
    """Drop what validating a refused candidate recorded."""
    # A member that took this id during validation keeps its tables.
    if spec.iid not in router.membership:
        for table in (router.sequence_limits, router.kv_leases, router.launches):
            table.pop(spec.iid, None)
    if all(member.url.rstrip("/") != spec.url.rstrip("/") for member in router.membership):
        router.role_switch.forget(spec.url)
        router.engines.kv.forget(spec.url)


async def _attestation_problem(
    router: NarwhalRouter, spec: EngineSpec, transport: httpx.AsyncBaseTransport | None
) -> str:
    """Return why the candidate's attestation does not match the engine contract, or ""."""
    try:
        await read_generation(router.cfg, spec, transport=transport)
    except AttestationMismatch as exc:
        return f"attestation does not match the engine contract: {exc}"
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return f"attestation unreadable: {type(exc).__name__}: {exc}"
    return ""


async def join_engine(
    router: NarwhalRouter,
    spec: EngineSpec,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> JoinOutcome:
    """Validate `spec` out of placement and add it to a new generation when every check passes.

    The checks are the readmission checks: health, process identity, attestation against the
    engine contract, profile generation, the served model, the model identity of every member
    that is not ejected or draining, one generation and a KV pair handoff with a live peer. The
    reloaded profile store must hold a profile for every engine in the proposed set, and a
    configured first-token calibration must cover every pair the new engine forms. The caller
    holds `router.lifecycle.lock`.
    """
    manager = router.lifecycle
    current = router.membership
    counts = manager.membership_counts.joins
    if refusal := join_refusal(router, spec):
        counts["refused"] += 1
        manager._emit("join_refused", iid=spec.iid, generation=current.generation, error=refusal)
        return JoinOutcome(spec.iid, False, current.generation, refused=refusal)
    manager._emit("join_started", iid=spec.iid, generation=current.generation)
    outcome = ValidationOutcome()
    joined = False
    try:
        joined = await _validate_and_join(router, spec, current, outcome, transport)
    except asyncio.CancelledError:
        outcome.fail(spec.iid, "join cancelled")
        raise
    except Exception as exc:
        log.exception("join validation of %s failed", spec.iid)
        outcome.fail(spec.iid, f"validation raised {type(exc).__name__}: {exc}")
    finally:
        if not joined:
            _forget_candidate(router, spec)
            failures = _failures(spec, outcome)
            counts["refused"] += 1
            manager._emit(
                "join_refused",
                iid=spec.iid,
                generation=router.membership.generation,
                error="; ".join(failures),
            )
    if not joined:
        return JoinOutcome(
            spec.iid,
            False,
            router.membership.generation,
            checks=list(outcome.checks.get(spec.iid, [])),
            failures=_failures(spec, outcome),
        )
    generation = router.membership.generation
    counts["joined"] += 1
    manager._emit(
        "joined",
        iid=spec.iid,
        generation=generation,
        process_start=outcome.starts[spec.iid],
        role=spec.role.value,
    )
    log.info("joined %s in generation %d as %s", spec.iid, generation, spec.role.value)
    return JoinOutcome(spec.iid, True, generation, checks=list(outcome.checks.get(spec.iid, [])))


def _failures(spec: EngineSpec, outcome: ValidationOutcome) -> list[str]:
    """Return the candidate's failures, or every engine's when a peer failed."""
    return list(outcome.failures.get(spec.iid, [])) or [
        "; ".join(f"{iid}: {'; '.join(items)}" for iid, items in outcome.failures.items())
    ]


async def _validate_and_join(
    router: NarwhalRouter,
    spec: EngineSpec,
    current: Membership,
    outcome: ValidationOutcome,
    transport: httpx.AsyncBaseTransport | None,
) -> bool:
    """Run the join checks into `outcome` and apply the new generation when all pass."""
    if problem := await _attestation_problem(router, spec, transport):
        outcome.fail(spec.iid, problem)
        return False
    proposed = current.joined(spec)
    scheduler = router.scheduler
    live = {
        iid for iid in current.ids if iid not in scheduler.ejected and iid not in scheduler.draining
    } | {spec.iid}
    evidence = await check_engine_set(router.cfg, proposed.specs, live=live, transport=transport)
    if evidence.problems:
        for problem in evidence.problems:
            outcome.fail(spec.iid, problem)
        return False
    validated = await validate_readmission(
        router,
        [spec.iid],
        wave=False,
        transport=transport,
        candidate=spec,
        profiles=evidence.profiles,
    )
    outcome.checks.update(validated.checks)
    outcome.starts.update(validated.starts)
    for iid, items in validated.failures.items():
        for item in items:
            outcome.fail(iid, item)
    if outcome.passed and not controls_fleet(router):
        outcome.fail(spec.iid, "router control was fenced during validation")
    if outcome.passed and router.membership is not current:
        outcome.fail(spec.iid, "membership changed during validation")
    if outcome.passed and (refusal := join_refusal(router, spec)):
        outcome.fail(spec.iid, refusal)
    if not outcome.passed or spec.iid not in outcome.starts or evidence.profiles is None:
        if outcome.passed:
            outcome.fail(spec.iid, "validation recorded no process identity")
        return False
    router.profiles.adopt(evidence.profiles)
    if evidence.calibration.status != "uncalibrated":
        router.first_token_calibration = evidence.calibration
    router.apply_membership(proposed)
    router.lifecycle.process_starts[spec.iid] = outcome.starts[spec.iid]
    return True
