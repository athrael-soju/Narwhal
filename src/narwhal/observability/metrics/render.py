"""Render router metrics in Prometheus text format."""

from __future__ import annotations

from .control import render_controller, render_decode_floor, render_demand
from .engines import render_availability, render_health, render_roles, render_work
from .exposition import Histogram
from .runtime import render_runtime
from .traffic import render_admission, render_outcomes, render_refusals, render_slo


def render(
    state: dict,
    ttft: Histogram,
    tpot: Histogram,
    seat: Histogram | None = None,
    queue_wait: Histogram | None = None,
) -> str:
    """Render a `/narwhal/state` snapshot plus the latency histograms."""
    out: list[str] = []
    out += render_admission(state)
    out += render_runtime(state)
    out += render_roles(state)
    out += render_refusals(state)
    out += render_health(state)
    out += render_outcomes(state)
    out += render_demand(state)
    out += render_availability(state)
    out += render_controller(state)
    out += render_work(state)
    out += render_slo(state)
    out += ttft.render("narwhal_ttft_seconds", "Router arrival to prefill completion")
    out += tpot.render(
        "narwhal_tpot_seconds", "Mean seconds per output token after prefill completion"
    )
    out += render_decode_floor(state)
    if seat is not None:
        out += seat.render(
            "narwhal_seat_seconds",
            "How long an admission seat was held",
        )
    if queue_wait is not None:
        out += queue_wait.render(
            "narwhal_queue_wait_seconds", "Total admission and dispatch wait per original request"
        )
    return "\n".join(out) + "\n"
