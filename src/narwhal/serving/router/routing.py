from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from ...backends import load as load_backend
from ...config import FleetConfig
from ...contracts import STATE, versioned
from ...engines.client import EngineClient
from ...engines.wire import Dial, dial_tcp
from ...observability.journal import RunJournal
from ...observability.metrics.exposition import Histogram, buckets_for, slo_histogram, slo_label
from ...profiling.calibration import CalibrationCheck
from ...profiling.store import ProfileStore
from ...runtime.lifecycle.manager import LifecycleManager
from ...runtime.monitoring import MonitoringLedger
from ...runtime.release import PeerRelease
from ...runtime.residency import ResidencySubscriptions
from ...runtime.standby import ready as router_ready
from ...scheduling.controller import ReactiveController
from ...scheduling.health import DriftTracker
from ...scheduling.monitor import InstanceMonitor
from ...scheduling.scheduler.placement import GlobalScheduler
from ...types import Instance, Phase, Role
from ..admission import AdmissionQueue, PlacementRefused, QueueExpired, QueueFull
from ..completion import output_cap
from ..dispatch import Dispatcher, placement_hold
from ..execution import price_waiting, request_error, serve_request
from ..handoff import snapshot as handoff_snapshot
from ..lifecycle import QUEUE_STAGES, RequestLifecycle
from ..outcomes import (
    INFLIGHT_LIMIT_MESSAGE,
    OUTCOME_REASONS,
    RequestExpired,
    RouterHeld,
    error_response,
    failure_reason,
)
from ..records import deadline_response, not_ready_response, overloaded_response, refuse_request
from ..retry import RetryBudget
from ..saturation import (
    SIZING_MIN_SAMPLES,
    SIZING_WINDOW_S,
    RecentDelays,
    saturation_threshold,
)
from ..seats import InputLengths
from ..seats import snapshot as seats_snapshot
from .sizing import RequestSizer
from .verification import SuspectVerifier

if TYPE_CHECKING:
    from ...runtime.lease import FileLease


class NarwhalRouter:
    def __init__(
        self,
        cfg: FleetConfig,
        journal: RunJournal,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        dial: Dial = dial_tcp,
        clock: Callable[[], float] = time.monotonic,
        max_concurrent: int | None = None,
    ) -> None:
        self.cfg = cfg
        cfg.serving.validate()
        self.journal = journal
        if max_concurrent is not None and max_concurrent > cfg.max_connections:
            raise ValueError(
                f"max_concurrent {max_concurrent} exceeds max_connections "
                f"{cfg.max_connections}: admission would outrun the dispatch pool"
            )
        # Standby takeover clears this flag after applying the primary's handoff.
        self.standby = False
        self.takeover_gap_s: float | None = None
        self.lease: FileLease | None = None
        self.lease_epoch = 0
        self.lease_holder = ""
        self._failover_blocked = ""
        self._lifecycle_blocked = ""
        self.first_token_calibration = CalibrationCheck("uncalibrated")
        # One injectable monotonic clock for scheduling and measurement.
        self._clock = clock
        self.profiles = ProfileStore(cfg.profiles_path)
        self.residency = ResidencySubscriptions(cfg.engines)
        self.monitor = InstanceMonitor(
            clock=clock,
            profiles=self.profiles,
            decode_correction_min=cfg.reactive_decode_correction_min,
            decode_correction_max=cfg.reactive_decode_correction_max,
            decode_correction_alpha=cfg.reactive_decode_correction_alpha,
            decode_correction_min_samples=cfg.reactive_decode_correction_min_samples,
        )
        for spec in cfg.engines:
            self.monitor.add(Instance(iid=spec.iid, url=spec.url, role=spec.role))
        shared_groups = {
            spec.iid: spec.shared_device.group
            for spec in cfg.engines
            if spec.shared_device is not None
        }
        if shared_groups:
            members: dict[str, list[str]] = {}
            for iid, group in shared_groups.items():
                members.setdefault(group, []).append(iid)
            self.profiles.bind_role_mix(
                shared_groups,
                lambda group: (
                    sum(
                        self.monitor.instances[iid].role is Role.PREFILL
                        for iid in members.get(group, ())
                    ),
                    sum(
                        self.monitor.instances[iid].role is Role.DECODE
                        for iid in members.get(group, ())
                    ),
                ),
                lambda iid: self.monitor.instances[iid].role,
            )
        self.scheduler = GlobalScheduler(
            self.monitor,
            self.profiles,
            cfg.slo,
            cfg.thresholds,
            clock=clock,
            eject_after=cfg.eject_after,
            flip_history=cfg.flip_history,
            pinned=frozenset(e.iid for e in cfg.engines if e.pin),
            min_prefill=cfg.min_prefill,
            min_decode=cfg.min_decode,
            advisory=cfg.advisory,
            on_floor_event=self.journal.write,
            on_control_event=self.journal.write,
            on_availability_event=self.journal.write,
            # Four controller windows cover the deepest evidence horizon any consumer reads.
            outcome_bucket_s=cfg.monitor_interval_s,
            outcome_retained_s=4 * cfg.reactive_window_s,
            health=DriftTracker(
                clock=clock,
                window_s=cfg.health_window_s,
                band=cfg.health_drift_band,
                min_samples=cfg.health_min_samples,
                probation_windows=cfg.health_probation_windows,
                evict_windows=cfg.health_evict_windows,
                recovery_windows=cfg.health_recovery_windows,
                penalty_s=cfg.health_probation_penalty_s,
                relative_band=cfg.health_relative_band,
            ),
        )
        self.sizer = RequestSizer(self)
        self.scheduler.recheck_cache_evidence = self.sizer.recheck_cache_evidence
        self.lifecycle = LifecycleManager(self)
        if cfg.engine_restart_policy == "whole_wave":
            self.scheduler.on_eject = lambda iid: self.lifecycle.require_restart_wave(
                f"engine {iid} was excluded"
            )
        self.lifecycle_transport = transport
        self.residency_client = httpx.AsyncClient(timeout=cfg.health_timeout_s, transport=transport)
        self.backend = load_backend(cfg.backend)
        self.engines = EngineClient(
            timeout_s=cfg.request_timeout_s,
            prefill_timeout_s=cfg.prefill_timeout_s,
            read_timeout_s=cfg.decode_read_timeout_s,
            max_connections=cfg.max_connections,
            control_connections=cfg.resolved_control_connections(),
            pool_timeout_s=cfg.pool_timeout_s,
            connect_timeout_s=cfg.connect_timeout_s,
            health_timeout_s=cfg.health_timeout_s,
            transport=transport,
            dial=dial,
            kv=self.backend.connector(cfg.connector),
            dialect=self.backend.dialect,
            model=cfg.model,
            engine_api_key=cfg.resolve_engine_key(),
        )
        self.verifier = SuspectVerifier(self)
        self.peer_release = PeerRelease(self._clock, self.backend.fabric)
        # The journal writes this into its run metadata when it opens.
        journal.extra = {
            "token_accounting": self._token_accounting(),
            "admission": self._admission_mode(),
        }
        self.served = 0
        self.slo_met = 0
        self.failed = 0
        self.refused = 0
        # `rejected` records capacity limits. `refused` records predictive admission.
        self.rejected = 0
        # Client disconnects settled by RequestLifecycle.finish().
        self.cancelled = 0
        # Malformed request bodies rejected during validation.
        self.invalid_requests = 0
        self.offered = 0
        self.ingress_inflight = 0
        self.ingress_high_water = 0
        self.loop_lag_s = 0.0
        self.sizing_delays = RecentDelays(SIZING_WINDOW_S, clock, min_samples=SIZING_MIN_SAMPLES)
        self.input_lengths = InputLengths(cfg.reactive_window_s, clock)
        # Per-engine values from each verified attestation.
        self.sequence_limits: dict[str, int] = {}
        self.kv_leases: dict[str, int] = {}
        self.launches: dict[str, dict[str, Any]] = {}
        self.unsized_offered = 0
        self.expired = 0
        # Outcome counts by reason for each counted terminal state.
        self.outcome_reasons: dict[str, Counter[str]] = {
            terminal: Counter() for terminal in OUTCOME_REASONS
        }
        # Attempt failures by request phase and failure reason.
        self.attempt_failures: Counter[tuple[str, str]] = Counter()
        self.served_after_retry = 0
        self.prefill_attempts = 0
        self.decode_attempts = 0
        self.retry_attempts = 0
        self.decode_tokens_observed = 0
        self.upstream_seconds = {"prefill": 0.0, "decode": 0.0}
        self.ttft = slo_histogram(cfg.slo.ttft_s)
        self.tpot = slo_histogram(cfg.slo.tpot_s)
        self.controller = ReactiveController(
            self.monitor,
            self.scheduler,
            clock=clock,
            window_s=cfg.reactive_window_s,
            confirmations=cfg.reactive_confirmations,
            utilization=cfg.reactive_utilization,
            min_arrivals=cfg.reactive_min_arrivals,
            demand_floor=cfg.reactive_demand_floor,
            movement_margin=cfg.reactive_movement_margin,
            step_s=cfg.reactive_step_s,
            evidence_span_s=cfg.reactive_evidence_span_s,
            evidence_max_span_s=cfg.reactive_evidence_max_span_s,
            evidence_min_arrivals=cfg.reactive_evidence_min_arrivals,
            demand_rise_tolerance=cfg.reactive_demand_rise_tolerance,
        )
        self.max_concurrent = max_concurrent if max_concurrent is not None else cfg.max_connections
        self.inflight = 0
        self.admission_queue = AdmissionQueue[bool](
            cfg.serving.queue_capacity, cfg.serving.queue_timeout_s, clock=clock
        )
        self.control_wakeup = asyncio.Event()
        self.dispatcher = Dispatcher(self)
        self.monitor.on_capacity_change = self.dispatcher.notify
        self.retry_budget = RetryBudget(cfg.serving.retry_budget, cfg.serving.retry_replenish)
        wait_scale = cfg.serving.queue_timeout_s or cfg.slo.ttft_s
        self.queue_wait = {
            stage: Histogram(
                buckets_for(wait_scale), labels={"stage": stage, "slo": slo_label(wait_scale)}
            )
            for stage in QUEUE_STAGES
        }
        # Request ID to the time it took an admission seat.
        self._seat_since: dict[str, float] = {}
        self.seat = slo_histogram(cfg.slo.ttft_s)
        self.monitoring = MonitoringLedger(
            clock, on_event=self.journal.write, on_degraded=self.wake_waiters
        )

    @property
    def failover_blocked(self) -> str:
        return self._failover_blocked

    @failover_blocked.setter
    def failover_blocked(self, reason: str) -> None:
        self._failover_blocked = reason
        if reason:
            self.wake_waiters()

    @property
    def lifecycle_blocked(self) -> str:
        return self._lifecycle_blocked

    @lifecycle_blocked.setter
    def lifecycle_blocked(self, reason: str) -> None:
        self._lifecycle_blocked = reason
        if reason:
            self.wake_waiters()

    def wake_waiters(self) -> None:
        self.admission_queue.wake_all()
        self.dispatcher.wake_all()

    def _holds(self) -> dict[str, list[dict[str, Any]]]:
        holds = self.scheduler.holds_snapshot()
        for row in holds["inference"]:
            row["recorded_producers"] = sorted(
                peer for peer in self.verifier.sources.get(row["iid"], {""}) if peer
            )
        return holds

    @property
    def monitoring_degraded(self) -> str:
        return self.monitoring.degraded

    def profile_set_diff(self) -> tuple[list[str], list[str]]:
        return self.profiles.engine_set_diff(self.monitor.instances)

    def _admission_mode(self) -> dict[str, Any]:
        return {"mode": self.cfg.admission, "margin": self.cfg.admission_margin}

    def attested(self, iid: str, payload: Any) -> None:
        identity = self.backend.identity
        launch = payload.get("launch") if isinstance(payload, dict) else None
        self.launches[iid] = launch if isinstance(launch, dict) else {}
        for values, value in (
            (self.sequence_limits, identity.sequence_limit(payload)),
            (self.kv_leases, identity.kv_lease(payload)),
        ):
            if value is None:
                values.pop(iid, None)
            else:
                values[iid] = value

    def _token_accounting(self) -> str:
        return "token_ids" if self.engines.dialect.token_ids else "unavailable"

    async def serve(
        self,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        arrived: float | None = None,
        lifecycle: RequestLifecycle | None = None,
    ) -> StreamingResponse | JSONResponse:
        state = lifecycle or RequestLifecycle.offered(self, headers, arrived=arrived)
        rid, arrived = state.rid, state.arrived
        req = state.request
        req.input_len = self.sizer.estimate_length(body)
        req.wanted_len = output_cap(body)
        state.sized = True
        state.resolve_demand()
        invalid = request_error(self, body)
        if invalid is not None:
            error = json.loads(bytes(invalid.body))["error"]
            state.finish(
                "invalid",
                error="unsupported request",
                status=invalid.status_code,
                error_type=error["type"],
                error_code=error.get("code"),
            )
            invalid.headers["x-request-id"] = rid
            return invalid
        # Local sizing keeps rejected traffic visible without issuing probes.
        state.demand_observation = self.controller.saw_arrival(
            req.input_len, wanted_len=req.wanted_len, at=arrived
        )

        def reserve() -> bool | None:
            if self.inflight >= self.max_concurrent:
                return None
            state.admit()
            return True

        def check() -> float | None:
            # A waiting request leaves on a prefill hold and is priced while it waits.
            hold = placement_hold(self, Phase.PREFILL)
            if hold:
                raise RouterHeld(hold)
            return price_waiting(state)

        state.phase = "queue"
        try:
            self.monitor.waiting[rid] = req
            if self.controller.note_prefill_risk(req, at=self._clock()):
                self.control_wakeup.set()
            began = self._clock()
            try:
                await state.wait(
                    lambda: self.admission_queue.acquire(
                        reserve,
                        deadline=state.deadline,
                        wait_s=state.queue_budget_s,
                        check=check,
                    )
                )
            finally:
                self.monitor.waiting.pop(rid, None)
                state.waited("admission", self._clock() - began)
            state.phase = "admission"
            response = await serve_request(state, endpoint, body, headers)
        except QueueFull:
            # A full queue is the same in-flight limit that ingress checks on arrival.
            response = overloaded_response(state, INFLIGHT_LIMIT_MESSAGE, reason="inflight_limit")
        except PlacementRefused as exc:
            response = refuse_request(state, exc)
        except RouterHeld as exc:
            response = not_ready_response(self, state, reason=str(exc))
        except (QueueExpired, RequestExpired) as exc:
            if failure_reason(exc, deadline_passed=self._clock() >= state.deadline) == "deadline":
                # Ingress answers the same deadline; both give the client one body.
                response = deadline_response(state)
            else:
                state.finish(
                    "expired",
                    error="queue deadline expired",
                    status=504,
                    reason="queue_timeout",
                    error_type="queue_expired",
                )
                response = error_response(504, "queue_expired", "queue deadline expired")
        except asyncio.CancelledError:
            state.finish("cancelled")
            raise
        except BaseException:
            state.finish("failed", error="request execution failed", status=500, reason="internal")
            raise
        response.headers["x-request-id"] = rid
        return response

    def _release_seat(self, rid: str) -> None:
        admitted_at = self._seat_since.pop(rid, None)
        if admitted_at is not None:
            self.inflight -= 1
            self.seat.observe(self._clock() - admitted_at)
            self.admission_queue.notify()

    def state(self) -> dict[str, Any]:
        out = {
            "journal_run": self.journal.run,
            "served": self.served,
            "slo_met": self.slo_met,
            "offered": self.offered,
            "unsized_offered": self.unsized_offered,
            "expired": self.expired,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "invalid_requests": self.invalid_requests,
            "controller": "reactive",
            "token_accounting": self._token_accounting(),
            "control": self.scheduler.roles.control_snapshot(),
            # Monitoring failures and the degraded admission gate.
            "monitoring": self.monitoring.snapshot(),
            "ha": {
                "ready": router_ready(self),
                "standby": self.standby,
                "epoch": self.lease_epoch,
                "holder": self.lease_holder,
                "blocked": self.failover_blocked,
            },
            "lifecycle": self.lifecycle.view(),
            "admission": {
                "inflight": self.inflight,
                "queued": len(self.admission_queue),
                "queue_capacity": self.cfg.serving.queue_capacity,
                "queue_high_water": self.admission_queue.high_water,
                "waiting_prefill": len(self.dispatcher.queues[Phase.PREFILL]),
                "waiting_decode": len(self.dispatcher.queues[Phase.DECODE]),
                "limit": self.max_concurrent,
                # The two signals behind the saturation 429 and the value each must stay below.
                "loop_lag_s": self.loop_lag_s,
                "sizing_delay_s": self.sizing_delays.median(),
                "saturation_threshold_s": saturation_threshold(self),
                "rejected": self.rejected,
                "refused": self.refused,
                "engine_auth": self.cfg.engine_auth_mode(),
                **self._admission_mode(),
            },
            # Every known reason, zero until it occurs.
            "outcome_reasons": {
                terminal: {
                    reason: counts[reason]
                    for reason in (*OUTCOME_REASONS[terminal], *sorted(counts))
                }
                for terminal, counts in self.outcome_reasons.items()
            },
            # Per-engine prefill and decode seats and their inputs.
            "seats": seats_snapshot(self),
            # Per-engine attested KV lease and the handoff bound derived from it.
            "handoff": handoff_snapshot(self),
            "serving": {
                "http_retained": self.ingress_inflight,
                "http_retained_limit": self.max_concurrent + self.cfg.serving.queue_capacity,
                "http_retained_high_water": self.ingress_high_water,
                "prefill_attempts": self.prefill_attempts,
                "decode_attempts": self.decode_attempts,
                "retry_attempts": self.retry_attempts,
                "retry_credits": self.retry_budget.available,
                "retry_credits_spent": self.retry_budget.spent,
                "retry_denied": self.retry_budget.denied,
                "served_after_retry": self.served_after_retry,
                "attempt_failures": [
                    {"phase": phase, "reason": reason, "count": count}
                    for (phase, reason), count in sorted(self.attempt_failures.items())
                ],
                "decode_tokens_observed": self.decode_tokens_observed,
                "upstream_seconds": self.upstream_seconds.copy(),
            },
            # Connection limits for engine requests and recovery probes.
            "http_pools": {
                "data_connections": self.cfg.max_connections,
                "control_connections": self.engines.control_connections,
                "pool_timeout_s": self.cfg.pool_timeout_s,
            },
            "pools": {
                "prefill": sorted(i.iid for i in self.monitor.pool(Role.PREFILL)),
                "decode": sorted(i.iid for i in self.monitor.pool(Role.DECODE)),
            },
            "load": {
                "prefill": round(self.scheduler.pool_load(Role.PREFILL), 4),
                "decode": round(self.scheduler.pool_load(Role.DECODE), 4),
            },
            "thresholds": {
                "expand": self.cfg.thresholds.expand,
                "shrink": self.cfg.thresholds.shrink,
                "cooldown_s": self.cfg.thresholds.cooldown_s,
                "sustained_intervals": self.cfg.thresholds.sustained_intervals,
                "dwell_s": self.cfg.thresholds.dwell_s,
                "panic_ratio": self.cfg.thresholds.panic_ratio,
            },
            "slo": {"ttft_s": self.cfg.slo.ttft_s, "tpot_s": self.cfg.slo.tpot_s},
            "first_token_timeout_s": self.cfg.first_token_timeout_s,
            "first_token_calibration": self.first_token_calibration.at_starts(
                self.lifecycle.process_starts
            ).view(),
            "resident": {
                iid: {"prefill": len(i.prefill), "decode": len(i.decode)}
                for iid, i in self.monitor.instances.items()
            },
            "pinned": sorted(self.scheduler.pinned),
            "min_prefill": self.scheduler.min_prefill,
            "min_decode": self.scheduler.min_decode,
            "below_floor": self.scheduler.floor_snapshot(),
            "ejected": sorted(self.scheduler.ejected),
            "peer_release": self.peer_release.snapshot(),
            "draining": sorted(self.scheduler.draining),
            "quarantined": self.scheduler.quarantine_list(),
            # The same holds split into timed quarantines and inference holds.
            "holds": self._holds(),
            # Engine failure streaks, active verification probes and transition counts.
            "breaker": {
                **self.scheduler.breaker_snapshot(),
                "probes": self.verifier.probe_counts(),
            },
            # Prefix residency each sidecar reports; unknown engines are priced cold.
            "residency": self.residency.snapshot(),
            "probation": sorted(
                self.scheduler.health.probation_set() if self.scheduler.health else []
            ),
            # Scored and undersampled drift windows per engine.
            "health": (self.scheduler.health.window_stats() if self.scheduler.health else {}),
            "unserved": self.scheduler.unserved,
            "panic_bypasses": self.scheduler.roles.panic_bypasses,
            # SLO outcome counts grouped into time buckets.
            "attainment": self.scheduler.outcome_summary(),
            "demand_history": self.controller.demand.history_summary(),
            "demand_evidence": self.controller.safety.consolidation_evidence_snapshot(),
            "flips_refused": [
                {"at": at, "to": to, "why": why}
                for at, to, why in self.scheduler.roles.flips_refused[-20:]
            ],
            "flips": [
                {
                    "at": f.at,
                    "iid": f.iid,
                    "to": f.to.value,
                    "by": f.by,
                    "prefill_inflight": f.prefill_inflight,
                    "decode_inflight": f.decode_inflight,
                    "drained_s": f.drained_s,
                }
                for f in self.scheduler.roles.flips
            ],
            "decode_floor": self.scheduler.roles.decode_floor_snapshot(),
        }
        return versioned(STATE, out)
