"""Router readiness, monitoring and engine lifecycle metrics."""

from __future__ import annotations

from .exposition import metric_lines


def render_runtime(state: dict) -> list[str]:
    """Render router readiness and runtime status."""
    out: list[str] = []
    ha = state.get("ha") or {}
    out += metric_lines(
        "narwhal_router_ready",
        "1 while this router may admit new traffic",
        "gauge",
        [({}, 1 if ha.get("ready", True) else 0)],
    )
    out += metric_lines(
        "narwhal_router_lease_epoch",
        "Current router lease epoch, or zero when fencing is disabled",
        "gauge",
        [({}, ha.get("epoch", 0))],
    )
    monitoring = state.get("monitoring") or {}
    out += metric_lines(
        "narwhal_monitoring_degraded",
        "1 while repeated monitoring-pass failures fence new admissions",
        "gauge",
        [({}, 1 if monitoring.get("degraded") else 0)],
    )
    out += metric_lines(
        "narwhal_monitoring_core_consecutive_failures",
        "Consecutive monitoring passes in which any stage failed",
        "gauge",
        [({}, monitoring.get("core_consecutive", 0))],
    )
    out += metric_lines(
        "narwhal_monitoring_core_failures_total",
        "Total monitoring passes in which any stage failed",
        "counter",
        [({}, monitoring.get("core_failures", 0))],
    )
    for field_name, help_text in (
        ("event_loop_lag_s", "Delay beyond the latest scheduled monitoring deadline"),
        (
            "event_loop_lag_high_water_s",
            "Largest monitoring deadline delay observed by this router process",
        ),
    ):
        out += metric_lines(
            f"narwhal_{field_name.removesuffix('_s')}_seconds",
            help_text,
            "gauge",
            [({}, monitoring.get(field_name, 0.0))],
        )
    monitoring_stages = monitoring.get("stages") or {}
    out += metric_lines(
        "narwhal_monitoring_stage_failures_total",
        "Monitoring stage failures, by stage",
        "counter",
        [
            ({"stage": name}, record.get("failures", 0))
            for name, record in sorted(monitoring_stages.items())
        ],
    )
    out += metric_lines(
        "narwhal_monitoring_stage_consecutive_failures",
        "Current consecutive failure streak of one monitoring stage",
        "gauge",
        [
            ({"stage": name}, record.get("consecutive", 0))
            for name, record in sorted(monitoring_stages.items())
        ],
    )
    lifecycle = state.get("lifecycle") or {}
    lifecycle_engines = lifecycle.get("engines") or {}
    out += metric_lines(
        "narwhal_engine_draining",
        "1 while an operator drain excludes the engine from new placement",
        "gauge",
        [
            ({"iid": iid}, 1 if entry.get("draining") else 0)
            for iid, entry in lifecycle_engines.items()
        ],
    )
    out += metric_lines(
        "narwhal_engine_ready_to_stop",
        "1 after a drained engine has no resident work and a recorded process identity",
        "gauge",
        [
            ({"iid": iid}, 1 if entry.get("ready_to_stop") else 0)
            for iid, entry in lifecycle_engines.items()
        ],
    )
    out += metric_lines(
        "narwhal_engine_lifecycle_state",
        "1 for the engine's current lifecycle state",
        "gauge",
        [
            ({"iid": iid, "state": entry.get("state", "active")}, 1)
            for iid, entry in lifecycle_engines.items()
        ],
    )
    return out
