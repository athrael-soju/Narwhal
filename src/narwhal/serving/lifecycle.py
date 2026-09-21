"""One original deadline, terminal outcome and reservation owner per request."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from ..types import Instance, Phase, Request
from .retry import transient

if TYPE_CHECKING:
    from ..scheduling.demand import ArrivalObservation
    from .router import NarwhalRouter

T = TypeVar("T")


class RequestExpired(Exception):
    """The original request exhausted its end-to-end deadline."""


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
        self.decode_attempts = 0
        self.attempt_failures: list[dict[str, Any]] = []
        self.output_started = False
        self.phase = "admission"
        self.queue_wait_s = 0.0
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

    def begin_attempt(self) -> None:
        """Count an actual prefill dispatch, keeping retries out of arrivals."""
        self.attempts += 1
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
        policy = self.router.cfg.serving.retry_policy()
        retryable = transient(exc)
        failure: dict[str, Any] = {
            "at": self.router._clock(),
            "attempt": self.attempts,
            "phase": self.phase,
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
        # One failure per attempt, including a pre-dispatch failure. Keep the
        # diagnostic bound even if a future caller evaluates the same failure twice.
        if len(self.attempt_failures) < policy.max_attempts:
            self.attempt_failures.append(failure)
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
        if not self.router.retry_budget.acquire():
            failure["retry_reason"] = "shared_budget"
            return False
        failure.update(retry_scheduled=True, retry_reason="allowed", backoff_s=delay)
        self.release()
        self.phase = "backoff"
        self.request.phase = Phase.PREFILL
        # Keep the original request in waiting demand throughout backoff.
        self.router.monitor.waiting[self.rid] = self.request
        await self.wait(lambda: asyncio.sleep(delay))
        return True

    def finish(
        self,
        terminal: str,
        *,
        error: str | None = None,
        status: int = 200,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Settle terminal counters, reservations and one journal row."""
        if self.terminal is not None:
            return
        if terminal == "cancelled" and self.router._clock() >= self.deadline:
            terminal, error, status = "expired", "original request deadline expired", 504
        self.terminal = terminal
        router = self.router
        req = self.request
        if not self.sized:
            router.unsized_offered += 1
        # Invalid bodies carry no workload shape. Other unread bodies remain
        # visible as unsized demand.
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
        if self.admitted:
            self.admitted = False
            router._release_seat(self.rid)
        router.queue_wait.observe(self.queue_wait_s)
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
        completed = terminal == "completed"
        if completed:
            router.retry_budget.succeeded()
            if measured is not None:
                router.controller.saw_completion(req.input_len, req.wanted_len, measured)
        if terminal not in ("cancelled", "rejected", "invalid"):
            router.scheduler.note_outcome(
                completed and prefill_s is not None and prefill_s <= router.cfg.slo.ttft_s,
                (completed and (tpot_s is None or tpot_s <= router.cfg.slo.tpot_s))
                if self.prefilled_at is not None
                else True,
            )
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
            "duration_s": router._clock() - self.arrived,
            "attempts": self.attempts,
            "decode_attempts": self.decode_attempts,
            "attempt_failures": self.attempt_failures.copy(),
            "decode_tokens_observed": self.decode_tokens_observed if measured is not None else None,
            "upstream_seconds": self.upstream_seconds.copy(),
            "prefill_iid": self.prefill_iid,
            "decode_iid": self.decode_iid,
            "crossed": self.decode_iid is not None and self.decode_iid != self.prefill_iid,
            "token_accounting": router._token_accounting(),
            "error": error,
            "terminal": terminal,
        }
        if terminal == "cancelled":
            row.update(cancelled=True, cancelled_phase=self.phase)
        elif terminal in ("refused", "rejected", "expired"):
            row[terminal] = True
        if extra:
            row.update(extra)
        router.journal.write(row)
