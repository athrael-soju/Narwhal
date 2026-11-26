"""Engine request headers, overload rejections and predictive refusal responses."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from fastapi import Header
from fastapi import Request as HTTPRequest
from fastapi.responses import JSONResponse

from ..runtime.standby import control_ready, controls_fleet, lease_role
from ..types import Request
from .admission import PlacementRefused
from .lifecycle import RequestLifecycle
from .outcomes import DEADLINE_MESSAGE, error_response

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter

NOT_CONTROLLING = "this router does not control the fleet"
# The OpenAPI entry of a fleet-state read's refusal.
CONTROLLED_READ: dict[int | str, dict[str, Any]] = {503: {"description": NOT_CONTROLLING}}

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


def deadline_response(
    state: RequestLifecycle, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    """Record and return the 504 of an original deadline that expires before response headers.

    Ingress and every router wait answer the deadline with this one body, so the journal row
    repeats the body the client receives whichever deadline timer fires first.
    """
    state.finish(
        "expired",
        error=DEADLINE_MESSAGE,
        status=504,
        reason="deadline",
        error_type="request_expired",
    )
    return error_response(504, "request_expired", DEADLINE_MESSAGE, headers=headers)


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

    `reason` names the hold that ended a waiting or prefilled request. The error code tells a
    lease holder that is not ready (`not_ready`) from a standby or fenced router (`standby`),
    and the body's `reason` names the hold, as `/ready` does.
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
    if backend_unavailable:
        code = "backend_unavailable"
    else:
        code = "not_ready" if lease_role(router) == "active" else "standby"
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
    message = "No engines are available" if backend_unavailable else "Router is not ready"
    return error_response(
        503, code, message, headers={"retry-after": "1"}, code=code, reason=reason
    )


class FleetNotControlled(Exception):
    """A fleet-state read reached a router that does not hold the active lease."""


async def require_control(request: HTTPRequest) -> None:
    """Refuse a fleet-state read unless this router controls the fleet.

    A standby or fenced router holds the fleet file's state, or state from before it lost the
    lease, so its answer would misdescribe the fleet.
    """
    router = request.app.state.router
    # A stopping router refuses, so the load balancer's fleet-state check takes it out of
    # rotation before it stops listening.
    if router.stopping or not controls_fleet(router):
        raise FleetNotControlled


async def require_control_or_diagnostic(
    request: HTTPRequest,
    x_narwhal_diagnostic: bool = Header(
        False,
        description="`true` answers with this router's own document even when it does not "
        "control the fleet. Diagnostic bundles send it",
    ),
) -> None:
    """Refuse a fleet-state read unless this router controls the fleet or the read is diagnostic.

    A diagnostic read keeps a standby's or a fenced router's state as failure evidence.
    """
    if not x_narwhal_diagnostic:
        await require_control(request)


def control_lost_reason(router: NarwhalRouter) -> str:
    """Name why the router does not control the fleet.

    The failover block comes first, then `shutting down` on a router that has begun to stop,
    then `shadowing` on a standby, then `lease expired`. `/ready`
    can name another cause first, such as a whole-wave hold or degraded monitoring.
    """
    return (
        router.failover_blocked
        or ("shutting down" if router.stopping else "")
        or ("shadowing" if router.standby else "lease expired")
    )


async def not_controlling_response(request: HTTPRequest, exc: Exception) -> JSONResponse:
    """Return the retryable 503 of a fleet-state read on a router without the lease."""
    return error_response(
        503,
        "standby",
        NOT_CONTROLLING,
        headers={"retry-after": "1"},
        code="standby",
        reason=control_lost_reason(request.app.state.router),
    )


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
