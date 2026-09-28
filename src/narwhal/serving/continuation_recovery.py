"""Admit bounded replay after a qualified upstream interruption."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..engines.client import (
    NO_HANDOFF_DETAIL,
    STREAM_CONTINUATION_DETAIL,
    STREAM_UNTERMINATED_DETAIL,
    EngineError,
)
from ..engines.connector import HandoffExpired
from ..engines.replay import ReplayError, ReplayUnavailable
from ..profiling.generation import identity_generation, profile_generation_problems
from ..runtime.standby import control_ready
from ..types import Request
from .admission import QueueExpired
from .continuation import HistoryLimitExceeded
from .lifecycle import RequestExpired, RequestLifecycle
from .retry import transient

_STOP_MESSAGES = {
    "attempt_limit": "Continuation recovery attempt limit reached",
    "shared_budget": "Continuation recovery credits are exhausted",
    "no_survivor": "No qualified continuation survivor is available",
    "qualification": "Qualified continuation capacity is unavailable",
    "original_deadline": "Original request deadline expired during continuation",
    "output_limit": "No output tokens remain for continuation",
    "fenced": "Router control does not permit continuation",
    "prediction": "Continuation prefill estimate exceeds the remaining deadline",
}


class ContinuationStopped(RuntimeError):
    """A local continuation admission gate ended the original stream."""

    def __init__(self, reason: str) -> None:
        super().__init__(_STOP_MESSAGES[reason])
        self.reason = reason


def failure_reason(exc: BaseException) -> str:
    """Return a fixed diagnostic label without retaining exception content."""
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, RequestExpired | QueueExpired):
        return "original_deadline"
    if isinstance(exc, HistoryLimitExceeded):
        return "history_limit"
    if isinstance(exc, ReplayUnavailable):
        return "qualification"
    if isinstance(exc, ReplayError):
        return "invalid_stream"
    if isinstance(exc, HandoffExpired):
        return "handoff"
    if isinstance(exc, httpx.PoolTimeout):
        return "local_pool"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection"
    if isinstance(exc, httpx.NetworkError | httpx.RemoteProtocolError):
        return "stream_interrupted"
    if isinstance(exc, EngineError):
        if exc.detail.startswith(STREAM_CONTINUATION_DETAIL):
            return "invalid_stream"
        if exc.detail.startswith(STREAM_UNTERMINATED_DETAIL):
            return "stream_interrupted"
        if exc.status == 200 and exc.detail.startswith(NO_HANDOFF_DETAIL):
            return "handoff"
        return "upstream_status"
    return "other"


def recoverable(exc: BaseException, *, already_recovering: bool) -> bool:
    """Allow worker interruptions while rejecting local and semantic failures."""
    if failure_reason(exc) in {
        "cancelled",
        "original_deadline",
        "history_limit",
        "qualification",
        "invalid_stream",
        "local_pool",
    }:
        return False
    if isinstance(exc, HandoffExpired) or (
        isinstance(exc, EngineError)
        and exc.status == 200
        and exc.detail.startswith(NO_HANDOFF_DETAIL)
    ):
        return already_recovering
    return transient(exc)


def profile_matches(state: RequestLifecycle, iid: str) -> bool:
    """Require every loaded profile variant to match the qualified process."""
    qualification = state.router.replay_qualification
    capture = qualification.engines.get(iid) if qualification is not None else None
    if capture is None:
        return False
    generation = (
        capture.attestation_digest
        if state.router.cfg.engine_contract is not None
        else identity_generation(capture.identity).digest
    )
    return not profile_generation_problems(state.router.profiles, iid, generation)


def begin_recovery(
    state: RequestLifecycle, original_body: dict[str, Any], exc: Exception
) -> dict[str, Any]:
    """Spend one independent credit and construct exact replay work synchronously."""
    if isinstance(exc, ContinuationStopped):
        raise exc
    history = state.continuation
    if history is None or not state.output_started or history.terminal_committed:
        raise RuntimeError("continuation recovery has no unfinished committed prefix")
    reason = failure_reason(exc)
    state.recovery_failed(reason)
    now = state.router._clock()
    if state.recovery_started_at is None:
        state.recovery_started_at = now
    if not recoverable(exc, already_recovering=state.recovering):
        state.recovery_terminal_reason = (
            reason
            if reason in {"original_deadline", "history_limit", "qualification", "local_pool"}
            else "non_transient"
        )
        raise exc
    router = state.router
    if now >= state.deadline:
        raise ContinuationStopped("original_deadline")
    if not history.remaining:
        raise ContinuationStopped("output_limit")
    if (
        not control_ready(router)
        or router.lifecycle_blocked
        or router.failover_blocked
        or not router.lifecycle.identities_ready
    ):
        raise ContinuationStopped("fenced")
    if state.recovery_attempts >= router.cfg.continuation.max_attempts:
        raise ContinuationStopped("attempt_limit")
    qualification = router.replay_qualification
    if qualification is None:
        raise ContinuationStopped("qualification")

    # An expired handoff supplies no evidence that either selected engine died.
    if not isinstance(exc, HandoffExpired):
        failed = state.decode_iid if state.phase == "decode" else state.prefill_iid
        if failed is not None:
            state.recovery_excluded.add(failed)
    qualified = {
        iid
        for iid, capture in qualification.engines.items()
        if router.lifecycle.process_starts.get(iid) == capture.identity.process_start_time_seconds
        and profile_matches(state, iid)
    }
    state.recovery_excluded.update(set(router.monitor.instances) - qualified)
    if not router.scheduler.live_instances(exclude=state.recovery_excluded):
        raise ContinuationStopped("no_survivor")
    if not router.continuation_budget.acquire():
        raise ContinuationStopped("shared_budget")

    history.discard_pending()
    state.recovering = True
    state.recovery_attempts += 1
    router.continuation_attempts += 1
    state.attempt_request = Request(
        rid=state.rid,
        input_len=history.prompt_count + history.committed_count,
        wanted_len=history.remaining,
        arrived_at=state.request.arrived_at,
        recovery_deadline=state.deadline,
    )
    return {
        **original_body,
        "prompt": history.replay_prompt(),
        "max_tokens": history.remaining,
    }
