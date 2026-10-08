"""One original deadline, terminal outcome and reservation owner per request."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from ..types import Instance, Phase, Request
from .outcomes import RequestExpired, failure_reason
from .retry import transient

if TYPE_CHECKING:
    from ..scheduling.demand import ArrivalObservation
    from .router.routing import NarwhalRouter

T = TypeVar("T")

# Wait stages in the order a request reaches them.
QUEUE_STAGES = ("admission", "prefill", "decode")


class RequestLifecycle:
    """Retain ownership across admission, backoff and fresh KV attempts."""

    def __init__(
        self,
        router: NarwhalRouter,
        request: Request,
        arrived: float,
        *,
        client_rid: str | None = None,
    ) -> None:
        self.router = router
        self.request = request
        self.rid = request.rid
        self.arrived = arrived
        self.client_rid = client_rid
        self.admitted = False
        self.deadline = arrived + router.cfg.request_timeout_s
        self.ingress_timer: asyncio.Timeout | None = None
        self.attempts = 0
        # Whether the current attempt reached prefill dispatch.
        self.dispatched = False
        # Whether a scheduled retry holds a retry credit it spends at dispatch.
        self.retry_reserved = False
        # Engines whose prefill or decode leg failed for this request, by role.
        self.failed_engines: dict[str, set[str]] = {"prefill": set(), "decode": set()}
        self.decode_attempts = 0
        self.attempt_failures: list[dict[str, Any]] = []
        self.output_started = False
        self.phase = "admission"
        # Seconds waited per stage, for each stage the request reached.
        self.queue_waits: dict[str, float] = {}
        # The latest prefill admission price and its parts.
        self.admission_price: dict[str, Any] | None = None
        self.owned: set[str] = set()
        self.terminal: str | None = None
        self.outcome: dict[str, Any] = {"error": None, "status": 200}
        self.prefill_iid: str | None = None
        self.decode_iid: str | None = None
        self.prefilled_at: float | None = None
        self.first_at: float | None = None
        self.last_at: float | None = None
        self.tokens = 0
        self.sized = False
        self.demand_pending = False
        self.demand_observation: ArrivalObservation | None = None
        self.decode_tokens_observed = 0
        self.upstream_seconds = {"prefill": 0.0, "decode": 0.0}

    @classmethod
    def offered(
        cls, router: NarwhalRouter, headers: dict[str, str], *, arrived: float | None = None
    ) -> RequestLifecycle:
        """Create the original owner at HTTP ingress or direct serving entry."""
        seen = router._clock() if arrived is None else arrived
        request = Request(str(uuid.uuid4()), input_len=0, arrived_at=seen)
        state = cls(
            router,
            request,
            seen,
            client_rid=headers.get("x-request-id"),
        )
        router.offered += 1
        router.controller.demand.unsized_pending += 1
        state.demand_pending = True
        return state

    def resolve_demand(self, *, retain_unsized: bool = False) -> None:
        """Settle the pending body exactly once, including early HTTP rejection."""
        if self.demand_pending:
            self.demand_pending = False
            self.router.controller.demand.resolve_unsized(at=self.arrived, retain=retain_unsized)

    async def wait(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Apply the remaining original budget without resetting it per leg."""
        remaining = self.deadline - self.router._clock()
        if remaining <= 0:
            raise RequestExpired("original request deadline expired")
        timer = asyncio.timeout(remaining)
        try:
            async with timer:
                return await operation()
        except TimeoutError as exc:
            if timer.expired():
                raise RequestExpired("original request deadline expired") from exc
            raise

    def admit(self) -> None:
        """Own one active admission seat."""
        self.admitted = True
        self.router.inflight += 1
        self.router._seat_since[self.rid] = self.router._clock()

    def reserve(self, instance: Instance) -> None:
        """Own one router reservation until this attempt releases it."""
        self.router.monitor.dispatched(instance.iid, self.request)
        self.owned.add(instance.iid)

    def release(self) -> None:
        """Release owned engine reservations and unassigned demand once."""
        for iid in self.owned:
            self.router.monitor.finished(iid, self.rid)
        self.owned.clear()
        self.router.monitor.waiting.pop(self.rid, None)

    @property
    def queue_wait_s(self) -> float:
        """Total seconds waited across every stage."""
        return sum(self.queue_waits.values())

    def waited(self, stage: str, seconds: float) -> None:
        """Add one wait at `stage`; the stage counts as reached even at zero seconds."""
        self.queue_waits[stage] = self.queue_waits.get(stage, 0.0) + seconds

    def begin_attempt(self) -> None:
        """Count an actual prefill dispatch, keeping retries out of arrivals."""
        self.attempts += 1
        self.dispatched = True
        if self.retry_reserved:
            self.retry_reserved = False
            self.router.retry_budget.spend()
        self.router.prefill_attempts += 1
        if self.attempts > 1:
            self.router.retry_attempts += 1
        self.tokens = 0
        self.first_at = self.last_at = self.prefilled_at = None
        self.prefill_iid = self.decode_iid = None
        self.request.output_len = 0

    def engine_headers(self, headers: dict[str, str], phase: str) -> dict[str, str]:
        """Give each engine leg a fresh ID while retaining client correlation locally."""
        # vLLM's X-Request-Id overrides body.request_id and names NIXL ownership.
        return {**headers, "x-request-id": f"{self.rid}-a{self.attempts}-{phase}"}

    def record_upstream_time(self, phase: str, began: float) -> None:
        """Count elapsed HTTP leg time including failed and cancelled attempts."""
        elapsed = max(0.0, self.router._clock() - began)
        self.upstream_seconds[phase] += elapsed
        self.router.upstream_seconds[phase] += elapsed

    async def retry(self, exc: BaseException) -> bool:
        """Spend shared quota for a classified failure before visible output."""
        router = self.router
        policy = router.cfg.serving.retry_policy()
        retryable = transient(exc)
        now = router._clock()
        reason = failure_reason(exc, deadline_passed=now >= self.deadline)
        failure: dict[str, Any] = {
            "at": now,
            # A failure before dispatch belongs to the attempt that never started.
            "attempt": self.attempts if self.dispatched else self.attempts + 1,
            "phase": self.phase,
            "reason": reason,
            "prefill_iid": self.prefill_iid,
            "decode_iid": self.decode_iid,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:240],
            "status": getattr(exc, "status", None),
            "transient": retryable,
            "output_started": self.output_started,
            "retry_scheduled": False,
            "backoff_s": None,
        }
        self.attempt_failures.append(failure)
        router.attempt_failures[self.phase, reason] += 1
        if not self.dispatched:
            self.release_retry_credit()
            failure["retry_reason"] = "not_dispatched"
            return False
        if self.output_started:
            failure["retry_reason"] = "output_started"
            return False
        if not 0 < self.attempts < policy.max_attempts:
            failure["retry_reason"] = "attempt_limit"
            return False
        if not retryable:
            failure["retry_reason"] = "non_transient"
            return False
        delay = policy.delay(self.attempts)
        if self.router._clock() + delay >= self.deadline:
            failure["retry_reason"] = "original_deadline"
            return False
        if not self.router.retry_budget.reserve():
            failure["retry_reason"] = "shared_budget"
            return False
        self.retry_reserved = True
        failure.update(retry_scheduled=True, retry_reason="allowed", backoff_s=delay)
        self.release()
        self.dispatched = False
        self.phase = "backoff"
        self.request.phase = Phase.PREFILL
        # A request in backoff counts as waiting demand.
        self.router.monitor.waiting[self.rid] = self.request
        await self.wait(lambda: asyncio.sleep(delay))
        self.phase = "admission"
        return True

    def release_retry_credit(self) -> None:
        """Return the credit of a retry that ends before it dispatches."""
        if self.retry_reserved:
            self.retry_reserved = False
            self.router.retry_budget.release()

    def finish(
        self,
        terminal: str,
        *,
        error: str | None = None,
        status: int = 200,
        reason: str | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Settle terminal counters, reservations and one journal row.

        `reason` labels failed, rejected, refused and expired outcomes. `error_type` and
        `error_code` repeat the client error body's `type` and `code`.
        """
        if self.terminal is not None:
            return
        if terminal == "cancelled" and self.router._clock() >= self.deadline:
            terminal, error, status = "expired", "original request deadline expired", 504
            reason, error_type, error_code = "deadline", self.phase, "expired"
        self.terminal = terminal
        router = self.router
        req = self.request
        self.release_retry_credit()
        if not self.sized:
            router.unsized_offered += 1
        # Unread bodies other than invalid ones stay visible as unsized demand.
        self.resolve_demand(retain_unsized=terminal != "invalid")
        self.release()
        measured = self.tokens if router.engines.dialect.token_ids else None
        self.outcome.update(error=error, status=status, tokens=measured, terminal=terminal)
        if terminal == "cancelled":
            self.outcome["cancelled"] = True
        counter = {
            "completed": "served",
            "failed": "failed",
            "refused": "refused",
            "rejected": "rejected",
            "expired": "expired",
            "cancelled": "cancelled",
            "invalid": "invalid_requests",
        }[terminal]
        setattr(router, counter, getattr(router, counter) + 1)
        if terminal in router.outcome_reasons:
            reason = reason or "unclassified"
            router.outcome_reasons[terminal][reason] += 1
        else:
            reason = None
        if completed := terminal == "completed":
            error_type = error_code = None
            if self.attempts > 1:
                router.served_after_retry += 1
        if self.admitted:
            self.admitted = False
            router._release_seat(self.rid)
        for stage, waited in self.queue_waits.items():
            router.queue_wait[stage].observe(waited)
        prefill_s = None if self.prefilled_at is None else self.prefilled_at - self.arrived
        first_s = None if self.first_at is None else self.first_at - self.arrived
        tpot_s = (
            (self.last_at - self.prefilled_at) / (self.tokens - 1)
            if measured is not None
            and self.last_at is not None
            and self.prefilled_at is not None
            and self.tokens > 1
            else None
        )
        decode_tpot_s = (
            (self.last_at - self.first_at) / (self.tokens - 1)
            if measured is not None
            and self.last_at is not None
            and self.first_at is not None
            and self.tokens > 1
            else None
        )
        if completed:
            router.retry_budget.succeeded()
            if measured is not None:
                router.controller.saw_completion(req.input_len, req.wanted_len, measured)
        ttft_ok = completed and prefill_s is not None and prefill_s <= router.cfg.slo.ttft_s
        tpot_ok = (
            (completed and (tpot_s is None or tpot_s <= router.cfg.slo.tpot_s))
            if self.prefilled_at is not None
            else True
        )
        if terminal not in ("cancelled", "rejected", "invalid"):
            router.scheduler.note_outcome(ttft_ok, tpot_ok)
        if ttft_ok and tpot_ok:
            router.slo_met += 1
        if prefill_s is not None:
            router.ttft.observe(prefill_s)
        if tpot_s is not None:
            router.tpot.observe(tpot_s)
        row: dict[str, Any] = {
            "rid": self.rid,
            "client_rid": self.client_rid,
            "arrived": self.arrived,
            "input_len": req.input_len,
            "input_sized": self.sized,
            "wanted_len": req.wanted_len,
            "output_len": measured,
            "ttft_s": prefill_s,
            "tpot_s": tpot_s,
            "first_byte_s": first_s,
            "decode_tpot_s": decode_tpot_s,
            "queue_wait_s": self.queue_wait_s,
            "queue_waits": {stage: self.queue_waits.get(stage) for stage in QUEUE_STAGES},
            "admission_price": self.admission_price,
            "duration_s": router._clock() - self.arrived,
            "attempts": self.attempts,
            "decode_attempts": self.decode_attempts,
            "attempt_failures": self.attempt_failures.copy(),
            "decode_tokens_observed": self.decode_tokens_observed if measured is not None else None,
            "upstream_seconds": self.upstream_seconds.copy(),
            "prefill_iid": self.prefill_iid,
            "decode_iid": self.decode_iid,
            "crossed": self.decode_iid is not None and self.decode_iid != self.prefill_iid,
            "cached_tokens": dict(req.cached_tokens),
            "cache_placement": req.cache_placement,
            "token_accounting": router._token_accounting(),
            "error": error,
            # Response headers carry 200 once output starts; a later error ends the stream.
            "status": 200 if self.output_started else (None if terminal == "cancelled" else status),
            "error_type": error_type,
            "error_code": error_code,
            "reason": reason,
            "terminal": terminal,
        }
        if terminal == "cancelled":
            row.update(cancelled=True, cancelled_phase=self.phase)
        elif terminal in ("refused", "rejected", "expired"):
            row[terminal] = True
        if extra:
            row.update(extra)
        router.journal.write(row)
