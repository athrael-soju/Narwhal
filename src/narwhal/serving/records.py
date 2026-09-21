"""Engine request headers and predictive refusal responses."""

from __future__ import annotations

import logging
import math

from fastapi.responses import JSONResponse

from .lifecycle import RequestLifecycle

log = logging.getLogger("narwhal.request_records")


def forward_headers(headers: dict[str, str]) -> dict[str, str]:
    """Forward client correlation while engine credentials stay deployment-owned."""
    request_id = next(
        (value for name, value in headers.items() if name.lower() == "x-request-id"), None
    )
    return {"x-request-id": request_id} if request_id is not None else {}


def refuse_request(
    state: RequestLifecycle,
    priced_s: float,
) -> JSONResponse:
    """Record and explain a predictive refusal before engine dispatch."""
    router, req, rid = state.router, state.request, state.rid
    budget = router.scheduler.slo.ttft_s * (1.0 + router.cfg.admission_margin)
    floor_s = router.scheduler.cheapest_own_prefill(req)
    over_alone = floor_s is not None and floor_s > budget
    caveat = (
        ". Aggregate mode uses the prefill curve only on an engine with no resident decode"
        if router.scheduler.prefill_live() == 0
        else ""
    )
    headers: dict[str, str]
    if math.isinf(priced_s):
        detail = (
            "refused: aggregate prefill has no calibrated price while every candidate "
            "carries decode work"
        )
        message = (
            "every live engine is serving decode work; aggregate prefill pricing requires "
            "an engine with zero resident decode work; retry after decode work drains"
        )
        headers = {"retry-after": "1"}
        cause = "aggregate_unpriced"
        log.info("refused %s: aggregate prefill interference is unpriced", rid)
    elif over_alone:
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
        cause = "prompt"
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
        cause = "queue"
        log.info("refused %s: priced %.2fs vs %.2fs budget", rid, priced_s, budget)
    state.finish("refused", error=detail, status=429, extra={"refused_cause": cause})
    return JSONResponse(
        status_code=429,
        headers=headers,
        content={"error": {"message": message, "type": "server_overloaded_error"}},
    )
