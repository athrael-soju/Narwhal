"""Engine role, pool, health and availability metrics."""

from __future__ import annotations

from collections.abc import Mapping

from .exposition import metric_lines


def render_roles(state: dict) -> list[str]:
    """Render engine role assignments."""
    out: list[str] = []
    out += metric_lines(
        "narwhal_instance_role",
        "Current pool assignment for each engine (1 = assigned this role)",
        "gauge",
        # Aggregated mode runs both phases on every decode-labelled engine.
        [
            ({"iid": iid, "role": role}, 1)
            for role in ("prefill", "decode")
            for iid in state.get("pools", {}).get(role, [])
        ]
        + (
            [
                ({"iid": iid, "role": "prefill"}, 1)
                for iid in state.get("pools", {}).get("decode", [])
            ]
            if not state.get("pools", {}).get("prefill")
            else []
        ),
    )
    return out


def render_health(state: dict) -> list[str]:
    """Render engine probation and drift-window metrics."""
    out: list[str] = []
    out += metric_lines(
        "narwhal_probation_instances",
        "Engines the drift instrument has deprioritized",
        "gauge",
        [({"iid": iid}, 1) for iid in state.get("probation", [])],
    )
    health = state.get("health") or {}
    out += metric_lines(
        "narwhal_health_windows_scored_total",
        "Drift windows that collected enough observations to reach a verdict",
        "counter",
        [({"iid": iid}, v.get("scored", 0)) for iid, v in sorted(health.items())],
    )
    out += metric_lines(
        "narwhal_health_windows_undersampled_total",
        "Drift windows that dropped gathered evidence too sparse to score",
        "counter",
        [({"iid": iid}, v.get("undersampled", 0)) for iid, v in sorted(health.items())],
    )
    out += metric_lines(
        "narwhal_health_prefill_paused",
        "Decode drift evidence paused until fresh gaps without local prefill interference",
        "gauge",
        [({"iid": iid}, int(v.get("prefill_paused", False))) for iid, v in sorted(health.items())],
    )
    out += metric_lines(
        "narwhal_health_prefill_pauses_total",
        "Times local prefill interference restarted decode drift evidence",
        "counter",
        [({"iid": iid}, v.get("prefill_pauses", 0)) for iid, v in sorted(health.items())],
    )
    return out


def render_availability(state: dict) -> list[str]:
    """Render engine ejections, quarantines, breaker state and prefill-floor breaches."""
    out: list[str] = []
    out += metric_lines(
        "narwhal_ejected_instances",
        "Instances the breaker currently holds out of scheduling and both loads",
        "gauge",
        [({}, len(state.get("ejected", [])))],
    )
    out += metric_lines(
        "narwhal_ejected",
        "1 while this instance is ejected",
        "gauge",
        [({"iid": iid}, 1) for iid in state.get("ejected", [])],
    )
    out += metric_lines(
        "narwhal_engine_quarantined",
        "1 while a failure quarantine or inference-probe hold excludes the engine from placement",
        "gauge",
        [({"iid": iid}, 1) for iid in state.get("quarantined", [])],
    )
    holds = state.get("holds") or {}
    out += metric_lines(
        "narwhal_engine_held",
        "1 while a hold of this kind, timed quarantine or inference, excludes the engine",
        "gauge",
        [
            ({"iid": row.get("iid", ""), "kind": kind}, 1)
            for kind in ("timed", "inference")
            for row in holds.get(kind, [])
        ],
    )
    breaker = state.get("breaker") or {}
    out += _count_lines(
        breaker,
        "ejections",
        "narwhal_engine_ejections_total",
        "Engine ejections since router start, by cause",
        ("iid", "cause"),
    )
    out += _count_lines(
        breaker,
        "hold_starts",
        "narwhal_engine_hold_starts_total",
        "Placement holds started since router start, by hold kind",
        ("iid", "kind"),
    )
    out += _count_lines(
        breaker,
        "hold_ends",
        "narwhal_engine_hold_ends_total",
        "Placement holds ended since router start, by hold kind and cause",
        ("iid", "kind", "cause"),
    )
    out += _count_lines(
        breaker,
        "probes",
        "narwhal_engine_probes_total",
        "Engine verification probe outcomes since router start, by probe kind",
        ("iid", "kind", "outcome"),
    )
    out += _count_lines(
        breaker,
        "readmissions",
        "narwhal_engine_readmissions_total",
        "Ejected engines returned to placement since router start, by recovery evidence",
        ("iid", "evidence"),
    )
    streak_samples: list[tuple[Mapping[str, str], float | int]] = [
        ({"iid": iid, "class": klass}, count)
        for iid, row in sorted((breaker.get("failures") or {}).items())
        for klass, count in sorted(row.items())
    ]
    out += metric_lines(
        "narwhal_engine_breaker_streak",
        "Consecutive engine failures held against one engine, by breaker class",
        "gauge",
        streak_samples,
    )
    out += metric_lines(
        "narwhal_engine_breaker_verifying",
        "1 while this engine has a breaker verification probe in flight, by kind",
        "gauge",
        [
            ({"iid": v.get("iid", ""), "kind": v.get("kind", "")}, 1)
            for v in breaker.get("verifying", [])
        ],
    )
    floor = state.get("below_floor") or {}
    out += metric_lines(
        "narwhal_prefill_below_floor",
        "1 while the live prefill pool is below min_prefill",
        "gauge",
        [({}, 1 if floor.get("active") else 0)],
    )
    out += metric_lines(
        "narwhal_prefill_below_floor_events_total",
        "Prefill floor breaches",
        "counter",
        [({}, floor.get("breaches", 0))],
    )
    out += metric_lines(
        "narwhal_prefill_below_floor_seconds_total",
        "Seconds spent below the prefill floor",
        "counter",
        [({}, floor.get("cumulative_s", 0.0))],
    )
    return out


def _count_lines(
    breaker: Mapping, key: str, name: str, help_text: str, labels: tuple[str, ...]
) -> list[str]:
    """Render one counter family from the breaker's count rows."""
    return metric_lines(
        name,
        help_text,
        "counter",
        [
            ({label: str(row.get(label, "")) for label in labels}, row.get("count", 0))
            for row in breaker.get(key, [])
        ],
    )


def render_work(state: dict) -> list[str]:
    """Render pool sizes, load and resident requests."""
    out: list[str] = []
    out += metric_lines(
        "narwhal_pool_instances",
        "Instances in each pool",
        "gauge",
        [({"role": r}, len(ids)) for r, ids in sorted(state.get("pools", {}).items())],
    )
    out += metric_lines(
        "narwhal_pool_load",
        "Pool load as a ratio against its own SLO target",
        "gauge",
        [({"role": r}, v) for r, v in sorted(state.get("load", {}).items())],
    )
    out += metric_lines(
        "narwhal_resident_requests",
        "Requests resident on each instance",
        "gauge",
        [
            ({"iid": iid, "phase": phase}, v[phase])
            for iid, v in sorted(state.get("resident", {}).items())
            for phase in ("prefill", "decode")
        ],
    )
    return out
