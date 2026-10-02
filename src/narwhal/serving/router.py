"""Router construction, request admission, and live state."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import FleetConfig
from ..contracts import STATE, versioned
from ..engines.client import (
    EngineClient,
)
from ..engines.connector import lookup as lookup_connector
from ..engines.dialect import lookup as lookup_dialect
from ..observability.journal import RunJournal
from ..observability.metrics import slo_histogram
from ..profiling.calibration import CalibrationCheck
from ..profiling.store import ProfileStore
from ..runtime.lifecycle import LifecycleManager
from ..runtime.monitoring import MonitoringLedger
from ..runtime.release import PeerRelease
from ..runtime.residency import ResidencySubscriptions
from ..runtime.standby import ready as router_ready
from ..scheduling.controller import ReactiveController
from ..scheduling.health import DriftTracker
from ..scheduling.monitor import InstanceMonitor
from ..scheduling.scheduler import GlobalScheduler
from ..types import Instance, Phase, Role
from .admission import AdmissionQueue, QueueExpired, QueueFull
from .completion import output_cap
from .dispatch import Dispatcher
from .execution import request_error, serve_request
from .lifecycle import RequestExpired, RequestLifecycle
from .records import overloaded_response
from .retry import RetryBudget
from .saturation import SIZING_MIN_SAMPLES, SIZING_WINDOW_S, RecentDelays
from .sizing import estimate_length, recheck_cache_evidence

if TYPE_CHECKING:
    from ..runtime.lease import FileLease

log = logging.getLogger("narwhal.server")


class NarwhalRouter:
    """Serve split requests against one configured fleet."""

    def __init__(
        self,
        cfg: FleetConfig,
        journal: RunJournal,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
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
        self.failover_blocked = ""
        self.lifecycle_blocked = ""
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
            self.profiles.bind_role_mix(
                shared_groups,
                lambda group: (
                    sum(
                        inst.role is Role.PREFILL
                        for iid, inst in self.monitor.instances.items()
                        if shared_groups.get(iid) == group
                    ),
                    sum(
                        inst.role is Role.DECODE
                        for iid, inst in self.monitor.instances.items()
                        if shared_groups.get(iid) == group
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
            # Four controller windows cover the deepest evidence horizon any consumer reads.
            outcome_bucket_s=cfg.monitor_interval_s,
            outcome_retained_s=4 * cfg.reactive_window_s,
            decode_concurrency=cfg.serving.decode_concurrency,
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
        self.scheduler.recheck_cache_evidence = partial(recheck_cache_evidence, self)
        self.lifecycle = LifecycleManager(self)
        if cfg.engine_restart_policy == "whole_wave":
            self.scheduler.on_eject = lambda iid: self.lifecycle.require_restart_wave(
                f"engine {iid} was excluded"
            )
        self.lifecycle_transport = transport
        self.residency_client = httpx.AsyncClient(timeout=cfg.health_timeout_s, transport=transport)
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
            kv=lookup_connector(cfg.connector),
            dialect=lookup_dialect(cfg.dialect),
            model=cfg.model,
            engine_api_key=cfg.resolve_engine_key(),
        )
        self._inference_sources: dict[str, set[str]] = {}
        self._verification_at: dict[str, float] = {}
        self._verification_tasks: set[asyncio.Task[None]] = set()
        self.peer_release = PeerRelease(self._clock)
        # The journal writes this into its run metadata when it opens.
        journal.extra = {"token_accounting": self._token_accounting()}
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
        self.unsized_offered = 0
        self.expired = 0
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
        self._tokenize_turn = 0
        # Engine ID to consecutive failed exact counts and the time it may count again.
        self._tokenize_backoff: dict[str, tuple[int, float]] = {}
        self.max_concurrent = max_concurrent if max_concurrent is not None else cfg.max_connections
        self.inflight = 0
        self.admission_queue = AdmissionQueue[bool](
            cfg.serving.queue_capacity, cfg.serving.queue_timeout_s, clock=clock
        )
        self.control_wakeup = asyncio.Event()
        self.dispatcher = Dispatcher(self)
        self.monitor.on_capacity_change = self.dispatcher.notify
        self.retry_budget = RetryBudget(cfg.serving.retry_budget, cfg.serving.retry_replenish)
        self.queue_wait = slo_histogram(cfg.serving.queue_timeout_s or cfg.slo.ttft_s)
        # Request ID to the time it took an admission seat.
        self._seat_since: dict[str, float] = {}
        self.seat = slo_histogram(cfg.slo.ttft_s)
        self.monitoring = MonitoringLedger(clock, on_event=self.journal.write)

    @property
    def monitoring_degraded(self) -> str:
        """`<class>:<stage>` while repeated failed passes fence admissions."""
        return self.monitoring.degraded

    def profile_set_diff(self) -> tuple[list[str], list[str]]:
        """Return (missing, extra) engine IDs between the fleet and profile store."""
        return self.profiles.engine_set_diff(self.monitor.instances)

    def _token_accounting(self) -> str:
        """Return the decode token-accounting mode the fleet's dialect guarantees."""
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
        """Own admission, deadline and terminal accounting for one offered request."""
        state = lifecycle or RequestLifecycle.offered(self, headers, arrived=arrived)
        rid, arrived = state.rid, state.arrived
        req = state.request
        req.input_len = estimate_length(self, body)
        req.wanted_len = output_cap(body)
        state.sized = True
        state.resolve_demand()
        invalid = request_error(self, body)
        if invalid is not None:
            state.finish("invalid", error="unsupported request", status=invalid.status_code)
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

        state.phase = "queue"
        try:
            self.monitor.waiting[rid] = req
            if self.controller.note_prefill_risk(req, at=self._clock()):
                self.control_wakeup.set()
            began = self._clock()
            try:
                await state.wait(
                    lambda: self.admission_queue.acquire(reserve, deadline=state.deadline)
                )
            finally:
                self.monitor.waiting.pop(rid, None)
                state.queue_wait_s += self._clock() - began
            state.phase = "admission"
            response = await serve_request(state, endpoint, body, headers)
        except QueueFull:
            response = overloaded_response(state, "server_overloaded_error")
        except (QueueExpired, RequestExpired):
            state.finish("expired", error="queue deadline expired", status=504)
            response = JSONResponse(
                status_code=504,
                content={"error": {"message": "queue deadline expired", "type": "queue_expired"}},
            )
        except asyncio.CancelledError:
            state.finish("cancelled")
            raise
        except BaseException:
            state.finish("failed", error="request execution failed", status=500)
            raise
        response.headers["x-request-id"] = rid
        return response

    def _release_seat(self, rid: str) -> None:
        """Release admission capacity and measure its occupancy."""
        admitted_at = self._seat_since.pop(rid, None)
        if admitted_at is not None:
            self.inflight -= 1
            self.seat.observe(self._clock() - admitted_at)
            self.admission_queue.notify()

    def state(self) -> dict[str, Any]:
        """Return the live state exposed by `/narwhal/state`."""
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
            "control": self.scheduler.control_snapshot(),
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
                "rejected": self.rejected,
                "refused": self.refused,
                "engine_auth": self.cfg.engine_auth_mode(),
            },
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
            # Engine failure streaks and active verification probes.
            "breaker": self.scheduler.breaker_snapshot(),
            # Prefix residency each sidecar reports; unknown engines are priced cold.
            "residency": self.residency.snapshot(),
            "probation": sorted(
                self.scheduler.health.probation_set() if self.scheduler.health else []
            ),
            # Scored and undersampled drift windows per engine.
            "health": (self.scheduler.health.window_stats() if self.scheduler.health else {}),
            "unserved": self.scheduler.unserved,
            "panic_bypasses": self.scheduler.panic_bypasses,
            # SLO outcome counts grouped into time buckets.
            "attainment": self.scheduler.outcome_summary(),
            "demand_history": self.controller.demand.history_summary(),
            "demand_evidence": self.controller.safety.consolidation_evidence_snapshot(),
            "flips_refused": [
                {"at": at, "to": to, "why": why}
                for at, to, why in self.scheduler.flips_refused[-20:]
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
                for f in self.scheduler.flips
            ],
            "decode_floor": self.scheduler.decode_floor_snapshot(),
        }
        return versioned(STATE, out)
