"""Engine request headers, overload rejections and predictive refusal responses."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

from ..runtime.standby import control_ready
from ..types import Request
from .admission import PlacementRefused
from .lifecycle import RequestLifecycle
from .outcomes import error_response

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter

# Decode admission checks in the order the router applies them.
DECODE_CHECKS = ("slot_wait", "kv_capacity", "tpot")

log = logging.getLogger("narwhal.request_records")


def forward_headers(headers: dict[str, str]) -> dict[str, str]:
    """Forward client correlation while engine credentials stay deployment-owned."""
    request_id = next(
        (value for name, value in headers.items() if name.lower() == "x-request-id"), None
    )
    return {"x-request-id": request_id} if request_id is not None else {}


def overloaded_response(state: RequestLifecycle, message: str, *, reason: str) -> JSONResponse:
    """Record a capacity rejection and return its 429 response."""
    state.finish(
        "rejected", error=message, status=429, reason=reason, error_type="server_overloaded_error"
    )
    return error_response(429, "server_overloaded_error", message, headers={"retry-after": "1"})


def monitoring_degraded_reason(router: NarwhalRouter) -> str:
    """Render the degraded streak's first failure as `stage class`, endpoint-free."""
    degraded = router.monitoring_degraded
    if not degraded:
        return ""
    klass, _, stage = degraded.partition(":")
    return f"monitoring degraded: {stage} {klass}"


def not_ready_response(
    router: NarwhalRouter, state: RequestLifecycle | None, *, reason: str = ""
) -> JSONResponse:
    """Record and return a retryable refusal while control or backends are unavailable.

    `reason` names the hold that ended a waiting or prefilled request.
    """
    backend_unavailable = (
        not reason
        and control_ready(router)
        and not router.lifecycle_blocked
        and not router.scheduler.live_instances()
    )
    reason = (
        reason
        or router.failover_blocked
        or router.lifecycle_blocked
        or monitoring_degraded_reason(router)
        or ("engine identity validation pending" if not router.lifecycle.identities_ready else "")
        or ("no available engines" if backend_unavailable else "")
        or "standby: the primary router is serving"
    )
    code = "backend_unavailable" if backend_unavailable else "standby"
    if state is not None:
        state.finish(
            "rejected",
            error=f"router not ready: {reason}",
            status=503,
            reason="not_ready",
            error_type=code,
            error_code=code,
            extra={"readiness_reason": reason},
        )
    return error_response(503, code, reason, headers={"retry-after": "1"}, code=code)


def ttft_refusal_cause(router: NarwhalRouter, req: Request, priced_s: float) -> str:
    """Name the TTFT check failure: unpriced aggregate prefill, the prompt alone, or queueing."""
    if math.isinf(priced_s):
        return "aggregate_unpriced"
    budget = router.scheduler.slo.ttft_s * (1.0 + router.cfg.admission_margin)
    floor_s = router.scheduler.cheapest_own_prefill(req)
    return "prompt" if floor_s is not None and floor_s > budget else "queue"


def refuse_request(state: RequestLifecycle, exc: PlacementRefused) -> JSONResponse:
    """Record and explain a predictive refusal before engine dispatch."""
    router, req, rid = state.router, state.request, state.rid
    priced_s, cause = exc.predicted_s, exc.cause
    budget = router.scheduler.slo.ttft_s * (1.0 + router.cfg.admission_margin)
    caveat = (
        ". Aggregate mode uses the prefill curve only on an engine with no resident decode"
        if router.scheduler.prefill_live() == 0
        else ""
    )
    headers: dict[str, str]
    if cause in DECODE_CHECKS:
        detail = f"refused: every live decode engine is at its decode capacity ({cause} check)"
        message = (
            "every live decode engine is at its measured decode concurrency or TPOT "
            "budget; retry as decode work drains"
        )
        headers = {"retry-after": "1"}
        log.info("refused %s: decode %s", rid, cause)
    elif cause == "aggregate_unpriced":
        detail = (
            "refused: aggregate prefill has no calibrated price while every candidate "
            "carries decode work"
        )
        message = (
            "every live engine is serving decode work; aggregate prefill pricing requires "
            "an engine with zero resident decode work; retry after decode work drains"
        )
        headers = {"retry-after": "1"}
        log.info("refused %s: aggregate prefill interference is unpriced", rid)
    elif cause == "prompt":
        floor_s = router.scheduler.cheapest_own_prefill(req) or 0.0
        detail = (
            f"refused: this prompt's own prefill prices TTFT at {floor_s:.2f}s "
            f"against the {budget:.2f}s budget{caveat}"
        )
        message = (
            f"this prompt's own prefill prices TTFT at {floor_s:.2f}s against the "
            f"{budget:.2f}s budget; no queue drains that, so shorten the prompt "
            f"or raise the TTFT budget{caveat}"
        )
        headers = {}
        log.info(
            "refused %s: the prompt alone prices %.2fs vs %.2fs budget",
            rid,
            floor_s,
            budget,
        )
    else:
        detail = (
            f"refused: cheapest placement prices TTFT at {priced_s:.2f}s "
            f"against the {budget:.2f}s budget{caveat}"
        )
        message = (
            f"cheapest placement prices TTFT at {priced_s:.2f}s against "
            f"the {budget:.2f}s budget; retry as the priced queue drains{caveat}"
        )
        # The placement price without the time this request has already waited.
        price = state.admission_price or {}
        placement_s = (
            price["price_s"] - price["elapsed_s"]
            if price.get("price_s") is not None and price.get("elapsed_s") is not None
            else priced_s
        )
        headers = {"retry-after": str(max(1, math.ceil(placement_s - budget)))}
        log.info("refused %s: priced %.2fs vs %.2fs budget", rid, priced_s, budget)
    state.finish(
        "refused",
        error=detail,
        status=429,
        reason=cause,
        error_type="server_overloaded_error",
        extra={"refused_cause": "decode" if cause in DECODE_CHECKS else cause},
    )
    state.outcome["public_error"] = message
    return error_response(429, "server_overloaded_error", message, headers=headers)
