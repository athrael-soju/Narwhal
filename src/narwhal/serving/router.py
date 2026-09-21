"""Router construction, request admission, recovery verification, and live state."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import FleetConfig
from ..contracts import STATE, versioned
from ..engines.client import EngineClient, EngineError, InferenceProbe, leg_failure_class
from ..engines.connector import lookup as lookup_connector
from ..engines.dialect import lookup as lookup_dialect
from ..observability.journal import RunJournal
from ..observability.metrics import Histogram, buckets_for
from ..profiling.store import ProfileStore
from ..runtime.lifecycle import LifecycleManager
from ..runtime.monitoring import MonitoringLedger
from ..runtime.standby import ready as router_ready
from ..scheduling.controller import ReactiveController
from ..scheduling.health import DriftTracker
from ..scheduling.monitor import InstanceMonitor
from ..scheduling.scheduler import GlobalScheduler
from ..types import Instance, Phase, Role
from .admission import AdmissionQueue, QueueExpired, QueueFull
from .dispatch import Dispatcher
from .execution import request_error, serve_request
from .lifecycle import RequestExpired, RequestLifecycle
from .retry import RetryBudget

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
            # Check admission capacity against the pool before building clients.
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
        # Share one injectable monotonic clock across scheduling and measurement.
        self._clock = clock
        self.profiles = ProfileStore(cfg.profiles_path)
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
            # Attainment buckets align with the monitor cadence and keep four
            # controller windows, the deepest evidence horizon any consumer reads.
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
        self.lifecycle = LifecycleManager(self)
        if cfg.engine_restart_policy == "whole_wave":
            self.scheduler.on_eject = lambda iid: self.lifecycle.require_restart_wave(
                f"engine {iid} was excluded"
            )
        self.lifecycle_transport = transport
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
        # The fleet's dialect fixes the decode token-accounting mode; stamp it
        # into the journal's run metadata when the journal opens.
        journal.extra = {"token_accounting": self._token_accounting()}
        self.served = 0
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
        self.unsized_offered = 0
        self.expired = 0
        self.prefill_attempts = 0
        self.decode_attempts = 0
        self.retry_attempts = 0
        self.decode_tokens_observed = 0
        self.upstream_seconds = {"prefill": 0.0, "decode": 0.0}
        self.ttft = Histogram(buckets_for(cfg.slo.ttft_s))
        self.tpot = Histogram(buckets_for(cfg.slo.tpot_s))
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
        # Prefer the last engine that answered the tokenizer probe.
        self._tokenizer: str | None = None
        self.max_concurrent = max_concurrent if max_concurrent is not None else cfg.max_connections
        self.inflight = 0
        self.admission_queue = AdmissionQueue[bool](
            cfg.serving.queue_capacity, cfg.serving.queue_timeout_s, clock=clock
        )
        self.control_wakeup = asyncio.Event()
        self.dispatcher = Dispatcher(self)
        self.monitor.on_capacity_change = self.dispatcher.notify
        self.retry_budget = RetryBudget(cfg.serving.retry_budget, cfg.serving.retry_replenish)
        self.queue_wait = Histogram(buckets_for(cfg.serving.queue_timeout_s or cfg.slo.ttft_s))
        # Measure how long admitted work occupies capacity.
        self._seat_since: dict[str, float] = {}
        self.seat = Histogram(buckets_for(cfg.slo.ttft_s))
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

    async def input_length(self, body: dict[str, Any]) -> int:
        """Return the exact or estimated input token count.

        One engine is
        queried per request. A failed probe rotates the preferred engine for
        the next request and uses the estimate immediately.
        """
        if self.cfg.tokenize:
            live = self.scheduler.live_instances()
            if live:
                k = next((j for j, i in enumerate(live) if i.iid == self._tokenizer), 0)
                got = await self.engines.token_count(live[k].url, body, self.cfg.tokenize_timeout_s)
                if got is not None:
                    self._tokenizer = live[k].iid
                    return got
                self._tokenizer = live[(k + 1) % len(live)].iid
        return self.estimate_length(body)

    def estimate_length(self, body: dict[str, Any]) -> int:
        """Estimate offered input length locally."""
        raw = body.get("prompt")
        if raw is None:
            raw = "".join(str(m.get("content", "")) for m in body.get("messages", []) or [])
        if isinstance(raw, list):
            return len(raw)
        return max(1, int(len(str(raw)) / self.cfg.chars_per_token))

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
        req.input_len = self.estimate_length(body)
        req.wanted_len = int(body.get("max_tokens") or 0)
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
            response = await serve_request(
                self,
                rid,
                endpoint,
                body,
                headers,
                arrived=arrived,
                offered=True,
                lifecycle=state,
            )
        except QueueFull:
            kind = "server_overloaded_error"
            state.finish("rejected", error=kind, status=429)
            response = JSONResponse(
                status_code=429,
                headers={"retry-after": "1"},
                content={"error": {"message": kind, "type": kind}},
            )
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

    def _leg_failed(
        self,
        iid: str,
        exc: BaseException,
        *,
        prefill_iid: str | None = None,
        decode_leg: bool = False,
    ) -> None:
        """Classify a failed leg and update breaker state.

        An upstream 4xx outside 408/429 confirms that the engine answered and
        stands as transport evidence. Local pool starvation is no evidence at
        all. Every other shape feeds one breaker class; at `eject_after` the
        class decides whether the suspect resolves by a health probe or by
        the two-leg inference probe.
        """
        if isinstance(exc, httpx.PoolTimeout):
            # Preserve breaker state after local connection-pool exhaustion.
            log.info("%s leg waited out the local HTTP pool; no breaker evidence", iid)
            return
        status = exc.status if isinstance(exc, EngineError) else 500
        if 400 <= status < 500 and status not in (408, 429):
            self.scheduler.record_answer(iid, "transport")
            return
        # Route 408/429 responses to the overload class. Decode read timeouts
        # require inference verification, including timeouts before headers.
        klass = (
            "stream"
            if decode_leg and isinstance(exc, httpx.ReadTimeout)
            else leg_failure_class(exc)
        )
        if klass is None:
            # PoolTimeout and caller-side 4xx legs are handled above.
            return
        if klass == "stream":
            self._inference_sources.setdefault(iid, set()).add(prefill_iid or "")
        verdict = self.scheduler.record_failure(iid, klass)
        if verdict == "eject":
            log.warning(
                "ejected %s after %d consecutive %s failures; it takes no "
                "dispatch until readmission succeeds",
                iid,
                self.scheduler.availability.eject_after,
                klass,
            )
        elif verdict in ("verify_health", "verify_inference"):
            if verdict == "verify_inference":
                self.scheduler.inference_suspects.add(iid)
                self.scheduler.quarantined[iid] = math.inf
                self.scheduler.refresh_floor_state()
            # One pending probe per engine and verdict kind; extra failures
            # while it runs only grow the streak it will resolve.
            key = (iid, verdict)
            if key not in self.scheduler.verifying:
                self.scheduler.verifying.add(key)
                self._start_verification(iid, verdict)

    def _start_verification(self, iid: str, kind: str) -> None:
        task = asyncio.create_task(self._verify_suspect(iid, kind))
        self._verification_tasks.add(task)
        task.add_done_callback(self._verification_tasks.discard)

    async def _verify_suspect(self, iid: str, kind: str) -> None:
        """Resolve a suspect engine with a health or inference verification."""
        try:
            inst = self.monitor.instances.get(iid)
            if inst is None:
                return
            if kind == "verify_inference":
                await self._verify_inference(iid, inst.url)
            else:
                await self._verify_health(iid, inst.url)
        except Exception:
            log.exception("verification failed for %s; existing hold retained", iid)
        finally:
            self._verification_at[iid] = self._clock()
            self.scheduler.verifying.discard((iid, kind))

    async def _verify_health(self, iid: str, url: str) -> None:
        """Resolve a health-evidence suspect with an engine health probe."""
        verdict = await self.engines.healthy(url)
        if verdict is None:
            # Preserve the suspect's state after control-pool exhaustion.
            log.info("suspect %s probe waited out the control pool; verdict deferred", iid)
            return
        if verdict:
            self.scheduler.record_answer(iid, "health")
            log.info("suspect %s passed health verification; health failure classes cleared", iid)
            return
        if self.scheduler.eject(iid):
            log.warning("ejected %s: timeout-shaped failures and /health did not answer", iid)

    async def _verify_inference(self, iid: str, url: str) -> None:
        """Verify a suspect engine with a prefill/decode probe."""
        sources = self._inference_sources.get(iid, {""}).copy()
        for source in sorted(sources):
            producer = self.monitor.instances.get(source) if source else None
            if source and producer is None:
                return  # Transfer verification requires the original producer.
            probe = await self.engines.probe_inference(
                url,
                prefill_url=producer.url if producer is not None else None,
                deadline_s=self.cfg.first_token_timeout_s,
            )
            if not self._resolve_inference_probe(iid, probe):
                return
        if sources != self._inference_sources.get(iid, {""}):
            return  # A newly failed path still needs verification.
        self._inference_sources.pop(iid, None)
        self.scheduler.record_answer(iid, "verification")
        log.info("suspect %s passed inference verification; failures cleared", iid)

    def _resolve_inference_probe(self, iid: str, probe: InferenceProbe | None) -> bool:
        """Resolve failure or defer an inconclusive probe without lifting its hold."""
        if probe is None:
            log.warning("inference probe unavailable for %s; verification deferred", iid)
            return False
        legs = {"prefill": probe.prefill, "decode": probe.decode}
        if any(leg.inconclusive for leg in legs.values()):
            # Control-pool starvation: the probe says nothing about the
            # engine, and the streak keeps its verdict pending.
            log.info(
                "suspect %s inference probe waited out the control pool; verdict deferred",
                iid,
            )
            return False
        failed = {name: leg.failed for name, leg in legs.items() if leg.failed is not None}
        if not failed:
            return True
        detail = ", ".join(f"{name} leg failed {klass}" for name, klass in sorted(failed.items()))
        if self.scheduler.eject(iid):
            log.warning("ejected %s: inference probe failed (%s)", iid, detail)
        else:
            log.warning("already ejected %s: inference probe failed (%s)", iid, detail)
        return False

    def state(self) -> dict[str, Any]:
        """Return the live state exposed by `/narwhal/state`."""
        out = {
            "served": self.served,
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
                # Engine-authentication mode.
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
            "resident": {
                iid: {"prefill": len(i.prefill), "decode": len(i.decode)}
                for iid, i in self.monitor.instances.items()
            },
            "pinned": sorted(self.scheduler.pinned),
            "min_prefill": self.scheduler.min_prefill,
            "min_decode": self.scheduler.min_decode,
            "below_floor": self.scheduler.floor_snapshot(),
            "ejected": sorted(self.scheduler.ejected),
            "draining": sorted(self.scheduler.draining),
            "quarantined": self.scheduler.quarantine_list(),
            # Engine failure streaks and active verification probes.
            "breaker": self.scheduler.breaker_snapshot(),
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
