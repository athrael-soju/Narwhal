"""Operator-controlled engine drain, validation, and readmission."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from ..config import EngineSpec, FleetConfig
from ..contracts import LIFECYCLE, versioned
from ..engines.attestation import EngineIdentity, fetch_engine_identity, verify_attestation
from ..engines.client import EngineError
from ..engines.stream import sse_token_count
from ..engines.validation import recovery_pairs, validation_pairs

if TYPE_CHECKING:
    from ..serving.router import NarwhalRouter


class LifecycleError(ValueError):
    """A lifecycle action violates the fleet contract."""


@dataclass
class DrainRecord:
    """Durable operator state for one engine."""

    iid: str
    state: str
    requested_at: float
    deadline_at: float
    restart_required: bool = True
    wave_id: str = ""
    old_process_start: float | None = None
    new_process_start: float | None = None
    error: str = ""
    checks: list[str] = field(default_factory=list)


@dataclass
class ValidationOutcome:
    """Readmission evidence collected before scheduler mutation."""

    starts: dict[str, float] = field(default_factory=dict)
    checks: dict[str, list[str]] = field(default_factory=dict)
    failures: dict[str, list[str]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Return whether every validation gate passed."""
        return not self.failures

    def ok(self, iid: str, message: str) -> None:
        """Record a passing engine gate."""
        self.checks.setdefault(iid, []).append(message)

    def fail(self, iid: str, message: str) -> None:
        """Record a failed engine gate."""
        self.failures.setdefault(iid, []).append(message)


class LifecycleManager:
    """Hold engines out of placement across external restart operations."""

    def __init__(self, router: NarwhalRouter) -> None:
        self.router = router
        self.records: dict[str, DrainRecord] = {}
        self.wave_id = ""
        self.events: list[dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self._wave_stop_ready_emitted = False
        self.process_starts: dict[str, float] = {}
        self.identities_ready = router.cfg.engine_contract is None

    def begin(self, engines: list[str], *, wave: bool, deadline_s: float) -> bool:
        """Start a drain, retry identity capture, or extend holds to a whole wave."""
        ids = list(dict.fromkeys(engines))
        if self.router.cfg.engine_restart_policy == "whole_wave" and not wave:
            raise LifecycleError("engine_restart_policy requires a whole-wave drain")
        configured = set(self.router.monitor.instances)
        unknown = sorted(set(ids) - configured)
        if unknown:
            raise LifecycleError(f"unknown engine(s): {', '.join(unknown)}")
        if not math.isfinite(deadline_s) or deadline_s <= 0:
            raise LifecycleError("deadline_s must be positive")
        if wave:
            if set(ids) != configured:
                raise LifecycleError("a whole-wave drain must name every configured engine")
        elif len(ids) != 1:
            raise LifecycleError("a non-wave drain must name exactly one engine")

        held = {iid for iid, rec in self.records.items() if rec.state != "active"}
        if held:
            promote = wave and not self.wave_id and not self.router.scheduler.live_instances()
            if not promote:
                same_wave_shape = wave == bool(self.wave_id)
                missing_identity = any(self.records[iid].old_process_start is None for iid in held)
                if set(ids) == held and same_wave_shape and missing_identity:
                    return False
                raise LifecycleError(
                    f"lifecycle action already active for {', '.join(sorted(held))}"
                )
        if not wave and not self.router.scheduler.live_instances(exclude=set(ids)):
            raise LifecycleError("the last schedulable engine requires a whole-wave drain")

        now = time.time()
        wave_id = f"wave-{uuid.uuid4().hex[:12]}" if wave else ""
        for iid in ids:
            # A managed engine may already have restarted while its peers
            # failed. Keep the identity captured before that restart.
            old_start = (
                self.records[iid].old_process_start
                if iid in held and self.records[iid].restart_required
                else None
            )
            self.router.scheduler.drain(iid)
            self.records[iid] = DrainRecord(
                iid=iid,
                state="draining",
                requested_at=now,
                deadline_at=now + deadline_s,
                old_process_start=old_start,
                wave_id=wave_id,
            )
            self._emit("drain_started", iid=iid, wave_id=wave_id)
        if wave:
            self.wave_id = wave_id
            self.router.lifecycle_blocked = f"whole-wave drain {wave_id}"
            self._wave_stop_ready_emitted = False
            self._emit("wave_started", engines=sorted(ids), wave_id=wave_id)
        self.refresh()
        return True

    def record_old_identity(self, iid: str, process_start: float | None, error: str = "") -> None:
        """Bind a drain to the process the supervisor is allowed to stop."""
        record = self.records[iid]
        if process_start is None:
            record.state = "blocked"
            record.error = error or "engine process identity could not be recorded"
            self._emit("drain_blocked", iid=iid, error=record.error, wave_id=record.wave_id)
            return
        record.old_process_start = process_start
        record.error = ""
        if record.state == "blocked":
            record.state = "draining"
        self._emit(
            "process_recorded",
            iid=iid,
            process_start=process_start,
            wave_id=record.wave_id,
        )
        self.refresh()

    def start_recovery_validation(self, engines: list[str], *, wave: bool = False) -> bool:
        """Hold ejected engines while the full recovery gate runs."""
        if self.router.cfg.engine_restart_policy == "whole_wave":
            self.require_restart_wave("engine recovery requires a managed whole-wave restart")
            return False
        held = {name for name, rec in self.records.items() if rec.state != "active"}
        configured = set(self.router.monitor.instances)
        if wave and set(engines) != configured:
            raise LifecycleError("whole-wave recovery must name every configured engine")
        if not wave and len(engines) != 1:
            raise LifecycleError("individual recovery must name exactly one engine")
        if held or not set(engines).issubset(self.router.scheduler.ejected):
            return False
        now = time.time()
        wave_id = f"wave-{uuid.uuid4().hex[:12]}" if wave else ""
        for iid in engines:
            self.router.scheduler.drain(iid)
            self.records[iid] = DrainRecord(
                iid=iid,
                state="validating",
                requested_at=now,
                deadline_at=now,
                restart_required=False,
                wave_id=wave_id,
            )
            self._emit("recovery_validation_started", iid=iid, wave_id=wave_id)
        if wave:
            self.wave_id = wave_id
            self.router.lifecycle_blocked = f"whole-wave recovery {wave_id}"
            self._wave_stop_ready_emitted = False
        return True

    def require_restart_wave(self, reason: str, *, reset: bool = False) -> None:
        """Hold the fleet until an operator captures identities and restarts it."""
        if self.wave_id and not reset:
            return
        now = time.time()
        self.wave_id = f"wave-{uuid.uuid4().hex[:12]}"
        for iid in self.router.monitor.instances:
            self.router.scheduler.drain(iid)
            self.records[iid] = DrainRecord(
                iid=iid,
                state="blocked",
                requested_at=now,
                deadline_at=now,
                wave_id=self.wave_id,
                error=reason,
            )
        self.router.lifecycle_blocked = f"whole-wave restart required: {reason}"
        self._wave_stop_ready_emitted = False
        self._emit("restart_required", wave_id=self.wave_id, reason=reason)

    def mark_validating(self, engines: list[str]) -> None:
        """Move drained engines into the pre-readmission validation phase."""
        self.refresh()
        for iid in engines:
            record = self.records.get(iid)
            if record is None or record.state not in {"drained", "blocked"}:
                state = record.state if record is not None else "active"
                raise LifecycleError(f"{iid} is {state}, expected drained")
            if record.restart_required and record.old_process_start is None:
                raise LifecycleError(f"{iid} has no recorded pre-restart process identity")
        for iid in engines:
            record = self.records[iid]
            record.state = "validating"
            record.error = ""
            record.checks = []
            self._emit("validation_started", iid=iid, wave_id=record.wave_id)

    def validation_failed(self, outcome: ValidationOutcome) -> None:
        """Keep every candidate held out when any validation gate fails."""
        for iid, record in self.records.items():
            if record.state != "validating":
                continue
            record.state = "blocked"
            record.checks = list(outcome.checks.get(iid, []))
            failures = outcome.failures.get(iid, [])
            record.error = "; ".join(failures) or "another engine in the wave failed validation"
            self._emit(
                "validation_failed",
                iid=iid,
                error=record.error,
                wave_id=record.wave_id,
            )

    def readmitted(self, engines: list[str], outcome: ValidationOutcome) -> None:
        """Return a fully validated set to placement as one mutation."""
        for iid in engines:
            record = self.records[iid]
            record.state = "active"
            record.new_process_start = outcome.starts[iid]
            self.process_starts[iid] = outcome.starts[iid]
            record.checks = list(outcome.checks.get(iid, []))
            record.error = ""
        for iid in engines:
            self.router.scheduler.finish_drain(iid)
            self._emit(
                "readmitted",
                iid=iid,
                process_start=outcome.starts[iid],
                wave_id=self.records[iid].wave_id,
            )
        if self.wave_id and all(
            rec.state == "active" for rec in self.records.values() if rec.wave_id == self.wave_id
        ):
            completed = self.wave_id
            self.wave_id = ""
            self.router.lifecycle_blocked = ""
            self._emit("wave_readmitted", engines=sorted(engines), wave_id=completed)
            for record in self.records.values():
                if record.wave_id == completed:
                    record.wave_id = ""
        self.router.scheduler.refresh_floor_state()

    def refresh(self) -> None:
        """Set drain completion and deadline states from resident work."""
        now = time.time()
        for iid, record in self.records.items():
            if record.state not in {"draining", "deadline_exceeded"}:
                continue
            instance = self.router.monitor.instances[iid]
            resident = len(instance.prefill) + len(instance.decode)
            if resident == 0 and record.old_process_start is not None:
                record.state = "drained"
                self._emit("drain_complete", iid=iid, wave_id=record.wave_id)
            elif now > record.deadline_at and record.state != "deadline_exceeded":
                record.state = "deadline_exceeded"
                record.error = f"{resident} resident request(s) remain after the drain deadline"
                self._emit(
                    "drain_deadline_exceeded",
                    iid=iid,
                    resident=resident,
                    wave_id=record.wave_id,
                )

        if self.wave_id and not self._wave_stop_ready_emitted:
            members = [rec for rec in self.records.values() if rec.wave_id == self.wave_id]
            if members and all(rec.state == "drained" for rec in members):
                self._wave_stop_ready_emitted = True
                self._emit(
                    "wave_ready_to_stop",
                    engines=sorted(rec.iid for rec in members),
                    wave_id=self.wave_id,
                )

    def document(self, *, error: str = "") -> dict[str, Any]:
        """Return the versioned lifecycle document for operators."""
        return versioned(LIFECYCLE, {**self.view(), "error": error})

    def view(self) -> dict[str, Any]:
        """Return computed lifecycle state without its contract envelope."""
        from .standby import controls_fleet, ready

        self.refresh()
        wave_members = [rec for rec in self.records.values() if rec.wave_id == self.wave_id]
        wave_ready = bool(wave_members) and all(rec.state == "drained" for rec in wave_members)
        eligible = {instance.iid for instance in self.router.scheduler.live_instances()}
        engines: dict[str, dict[str, Any]] = {}
        for iid, instance in sorted(self.router.monitor.instances.items()):
            record = self.records.get(iid)
            prefill = len(instance.prefill)
            decode = len(instance.decode)
            state = record.state if record is not None else "active"
            ready_to_stop = bool(
                record is not None
                and record.old_process_start is not None
                and state == "drained"
                and (not record.wave_id or wave_ready)
            )
            engines[iid] = {
                "state": state,
                "draining": iid in self.router.scheduler.draining,
                "accepts_new": iid in eligible,
                "ready_to_stop": ready_to_stop,
                "resident": {"prefill": prefill, "decode": decode},
                "deadline_at": record.deadline_at if record is not None else None,
                "restart_required": record.restart_required if record is not None else False,
                "wave_id": record.wave_id if record is not None else "",
                "old_process_start": record.old_process_start if record is not None else None,
                "new_process_start": record.new_process_start if record is not None else None,
                "checks": list(record.checks) if record is not None else [],
                "error": record.error if record is not None else "",
            }
        return {
            "router": {
                "controls_fleet": controls_fleet(self.router),
                "ready": ready(self.router),
            },
            "wave": {
                "id": self.wave_id,
                "active": bool(self.wave_id),
                "ready_to_stop": wave_ready,
            },
            "engines": engines,
            "events": list(self.events),
            "engine_restart_policy": self.router.cfg.engine_restart_policy,
            "process_starts": dict(self.process_starts),
        }

    def handoff(self) -> dict[str, Any]:
        """Return durable lifecycle state for resume and warm standby."""
        self.refresh()
        return {
            "wave_id": self.wave_id,
            "records": [asdict(record) for _, record in sorted(self.records.items())],
            "events": list(self.events),
            "engine_restart_policy": self.router.cfg.engine_restart_policy,
            "process_starts": dict(self.process_starts),
        }

    def restore(self, payload: Any) -> None:
        """Restore conservative hold-outs from a handoff document."""
        if not isinstance(payload, dict):
            raise LifecycleError("handoff lifecycle state must be an object")
        policy = payload.get("engine_restart_policy")
        if policy != self.router.cfg.engine_restart_policy:
            raise LifecycleError("handoff engine restart policy does not match this fleet")
        starts = payload.get("process_starts")
        if not isinstance(starts, dict) or any(
            iid not in self.router.monitor.instances
            or isinstance(start, bool)
            or not isinstance(start, int | float)
            or not math.isfinite(start)
            or start <= 0
            for iid, start in starts.items()
        ):
            raise LifecycleError(
                "handoff process_starts must map configured engines to positive times"
            )
        records = payload.get("records")
        if not isinstance(records, list):
            raise LifecycleError("handoff lifecycle records must be a list")
        restored: dict[str, DrainRecord] = {}
        configured = set(self.router.monitor.instances)
        for raw in records:
            if not isinstance(raw, dict):
                raise LifecycleError("handoff lifecycle record must be an object")
            if raw.get("iid") not in configured:
                raise LifecycleError(f"handoff lifecycle names unknown engine {raw.get('iid')!r}")
            try:
                record = DrainRecord(**raw)
            except (TypeError, ValueError) as exc:
                raise LifecycleError(f"invalid lifecycle record: {exc}") from exc
            if record.state not in {
                "active",
                "draining",
                "drained",
                "deadline_exceeded",
                "validating",
                "blocked",
            }:
                raise LifecycleError(f"invalid lifecycle state {record.state!r}")
            for name in ("requested_at", "deadline_at"):
                value = getattr(record, name)
                if (
                    not isinstance(value, int | float)
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    raise LifecycleError(f"lifecycle {name} must be a number")
            for name in ("old_process_start", "new_process_start"):
                value = getattr(record, name)
                if value is not None and (
                    not isinstance(value, int | float)
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    raise LifecycleError(f"lifecycle {name} must be a number or null")
            if not isinstance(record.wave_id, str) or not isinstance(record.error, str):
                raise LifecycleError("lifecycle wave_id and error must be strings")
            if not isinstance(record.restart_required, bool):
                raise LifecycleError("lifecycle restart_required must be a boolean")
            if not isinstance(record.checks, list) or not all(
                isinstance(check, str) for check in record.checks
            ):
                raise LifecycleError("lifecycle checks must be a string list")
            if record.state == "validating":
                record.state = "blocked"
                record.error = "readmission validation was interrupted by router replacement"
            restored[record.iid] = record
        wave_id = payload.get("wave_id")
        if wave_id is not None and not isinstance(wave_id, str):
            raise LifecycleError("handoff lifecycle wave_id must be a string")
        restored_wave_id = wave_id or ""
        record_waves = {record.wave_id for record in restored.values() if record.wave_id}
        if restored_wave_id:
            members = {
                iid for iid, record in restored.items() if record.wave_id == restored_wave_id
            }
            if members != configured or record_waves != {restored_wave_id}:
                raise LifecycleError("whole-wave handoff must hold every configured engine")
        elif record_waves:
            raise LifecycleError("lifecycle record has a wave_id without an active wave")
        self.records = restored
        self.process_starts = dict(starts)
        self.identities_ready = self.router.cfg.engine_contract is None
        self.router.scheduler.draining.clear()
        for iid, record in restored.items():
            if record.state != "active":
                self.router.scheduler.drain(iid)
        self.wave_id = restored_wave_id
        self.router.lifecycle_blocked = f"whole-wave drain {self.wave_id}" if self.wave_id else ""
        events = payload.get("events")
        self.events = (
            [dict(event) for event in events[-200:] if isinstance(event, dict)]
            if isinstance(events, list)
            else []
        )
        self.refresh()

    def _emit(self, action: str, **fields: Any) -> None:
        event = {"event": "engine_lifecycle", "action": action, "at": time.time(), **fields}
        self.events.append(event)
        del self.events[:-200]
        self.router.journal.write(event)


async def check_process_identities(
    router: NarwhalRouter, engines: list[str] | None = None
) -> list[str]:
    """Bind initial identities and exclude changed or unverifiable processes."""
    from .standby import controls_fleet

    cfg: FleetConfig = router.cfg
    manager: LifecycleManager = router.lifecycle
    contract = cfg.engine_contract
    if contract is None:
        manager.identities_ready = True
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
                failures = verify_attestation(response.json(), contract, identity)
                if failures:
                    return None, "attestation: " + "; ".join(failures)
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
                router.scheduler.eject(spec.iid)
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


def _single_pairs(target: EngineSpec, peers: list[EngineSpec]) -> list[tuple[str, str]]:
    try:
        return recovery_pairs(target, peers)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc


async def validate_readmission(
    router: NarwhalRouter,
    engines: list[str],
    *,
    wave: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ValidationOutcome:
    """Run health, attestation, generation, and fabric gates before readmission."""
    outcome = ValidationOutcome()
    cfg: FleetConfig = router.cfg
    contract = cfg.engine_contract
    if cfg.engine_restart_policy == "whole_wave":
        if not wave or set(engines) != set(router.monitor.instances):
            for iid in engines:
                outcome.fail(iid, "engine_restart_policy requires whole-wave readmission")
            return outcome
        if any(
            not router.lifecycle.records[iid].restart_required
            or router.lifecycle.records[iid].old_process_start is None
            for iid in engines
        ):
            for iid in engines:
                outcome.fail(iid, "whole-wave recovery requires recorded pre-restart identities")
            return outcome
    if contract is None or contract.missing():
        for iid in engines:
            outcome.fail(iid, "readmission requires a complete engine_contract")
        return outcome

    by_id = {spec.iid: spec for spec in cfg.engines}
    targets = [by_id[iid] for iid in engines]
    if wave:
        participants = list(cfg.engines)
        pairs = validation_pairs(participants)
    else:
        peers = [
            by_id[instance.iid]
            for instance in router.scheduler.live_instances(exclude=set(engines))
        ]
        try:
            pairs = _single_pairs(targets[0], peers)
        except LifecycleError as exc:
            outcome.fail(targets[0].iid, str(exc))
            return outcome
        peer_ids = {iid for pair in pairs for iid in pair} - set(engines)
        participants = targets + [by_id[iid] for iid in sorted(peer_ids)]

    participant_ids = {spec.iid for spec in participants}
    if len(cfg.engines) == 1:
        outcome.ok(engines[0], "fabric not applicable: single-engine fleet")
    elif len(participant_ids) < 2 or not pairs:
        for iid in engines:
            outcome.fail(iid, "fabric validation requires an eligible peer")
        return outcome

    timeout = cfg.health_timeout_s
    identities: dict[str, EngineIdentity] = {}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for spec in participants:
            if not await router.engines.healthy(spec.url):
                outcome.fail(spec.iid, "health did not answer 200")
                continue
            outcome.ok(spec.iid, "health")
            try:
                identity = await fetch_engine_identity(
                    spec.url,
                    timeout_s=timeout,
                    transport=transport,
                    headers=router.engines._auth(None),
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome.fail(spec.iid, f"process identity unreadable: {type(exc).__name__}")
                continue
            identities[spec.iid] = identity
            previous = router.lifecycle.process_starts.get(spec.iid)
            if (
                spec.iid not in engines
                and previous is not None
                and (identity.process_start_time_seconds != previous)
            ):
                router.scheduler.eject(spec.iid)
                outcome.fail(spec.iid, "peer process changed before readmission")
            if not spec.attestation_url:
                outcome.fail(spec.iid, "attestation_url is not configured")
                continue
            try:
                response = await client.get(spec.attestation_url)
                response.raise_for_status()
                failures = verify_attestation(response.json(), contract, identity)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome.fail(spec.iid, f"attestation unreadable: {type(exc).__name__}")
                continue
            if failures:
                outcome.fail(spec.iid, "attestation: " + "; ".join(failures))
            else:
                outcome.ok(spec.iid, f"attestation {contract.fingerprint()}")
            try:
                models = await client.get(
                    f"{spec.url}/v1/models", headers=router.engines._auth(None)
                )
                models.raise_for_status()
                names = [entry["id"] for entry in models.json().get("data", [])]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome.fail(spec.iid, f"model list unreadable: {type(exc).__name__}")
            else:
                if cfg.model not in names:
                    outcome.fail(spec.iid, f"serves {names}, expected {cfg.model}")
                else:
                    outcome.ok(spec.iid, "model")

        for spec in targets:
            target_identity = identities.get(spec.iid)
            record = router.lifecycle.records[spec.iid]
            if target_identity is None:
                continue
            if record.restart_required and target_identity.process_start_time_seconds <= float(
                record.old_process_start or 0.0
            ):
                outcome.fail(spec.iid, "engine process did not restart after drain")
                continue
            outcome.starts[spec.iid] = target_identity.process_start_time_seconds
            outcome.ok(
                spec.iid,
                "new process identity" if record.restart_required else "process identity",
            )
            try:
                response = await client.post(
                    f"{spec.url}/v1/completions",
                    headers=router.engines._auth(None),
                    json={
                        "model": cfg.model,
                        "prompt": "narwhal lifecycle generation check",
                        "max_tokens": 1,
                        "temperature": 0.0,
                        "stream": False,
                    },
                    timeout=cfg.prefill_timeout_s,
                )
                response.raise_for_status()
                choices = response.json().get("choices")
                if not isinstance(choices, list) or not choices:
                    raise ValueError("completion has no choices")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                outcome.fail(spec.iid, f"generation failed: {type(exc).__name__}")
            else:
                outcome.ok(spec.iid, "generation")

    if outcome.failures:
        return outcome

    async def identities_unchanged() -> bool:
        identity_failed = False
        for spec in participants:
            try:
                live = await fetch_engine_identity(
                    spec.url,
                    timeout_s=timeout,
                    transport=transport,
                    headers=router.engines._auth(None),
                )
                if live != identities[spec.iid]:
                    raise ValueError("process changed during validation")
                async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
                    response = await client.get(spec.attestation_url)
                    response.raise_for_status()
                if verify_attestation(response.json(), contract, live):
                    raise ValueError("attestation changed during validation")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                identity_failed = True
                outcome.fail(
                    spec.iid,
                    f"identity changed or unavailable during validation: {type(exc).__name__}",
                )
                router.scheduler.eject(spec.iid)
        if identity_failed and cfg.engine_restart_policy == "whole_wave":
            router.lifecycle.require_restart_wave(
                "process identity changed during validation", reset=True
            )
        return not outcome.failures

    body = {
        "model": cfg.model,
        "prompt": "narwhal lifecycle fabric check",
        "max_tokens": 2,
        "temperature": 0.0,
    }
    for source, target in pairs:
        if not await identities_unchanged():
            return outcome
        try:
            params = await router.engines.prefill(by_id[source].url, "/v1/completions", body, {})
            if not await identities_unchanged():
                return outcome
            tokens = 0
            async for line in router.engines.decode(
                by_id[target].url,
                "/v1/completions",
                body,
                {},
                params,
                first_token_timeout_s=cfg.first_token_timeout_s,
            ):
                tokens += sse_token_count(line)
            if tokens < 1:
                raise EngineError("decode", by_id[target].url, 502, "no tokens")
        except Exception as exc:
            detail = f"fabric {source}->{target}: {type(exc).__name__}: {exc}"
            outcome.fail(source, detail)
            outcome.fail(target, detail)
        else:
            outcome.ok(source, f"fabric produce to {target}")
            outcome.ok(target, f"fabric consume from {source}")

    if outcome.failures:
        return outcome
    for spec in targets:
        if not await router.engines.healthy(spec.url):
            outcome.fail(spec.iid, "final health failed after fabric validation")
        else:
            outcome.ok(spec.iid, "final health")
    await identities_unchanged()
    return outcome
