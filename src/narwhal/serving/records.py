"""Engine request headers, overload rejections and predictive refusal responses."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

from ..types import Request
from .admission import PlacementRefused
from .lifecycle import RequestLifecycle

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
    return JSONResponse(
        status_code=429,
        headers={"retry-after": "1"},
        content={"error": {"message": message, "type": "server_overloaded_error"}},
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
        headers = {"retry-after": str(max(1, math.ceil(priced_s - budget)))}
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
    return JSONResponse(
        status_code=429,
        headers=headers,
        content={"error": {"message": message, "type": "server_overloaded_error"}},
    )
