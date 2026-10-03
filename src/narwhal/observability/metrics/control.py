"""Role change, controller decision, demand evidence and decode floor metrics."""

from __future__ import annotations

from collections.abc import Mapping

from .exposition import metric_lines


def render_demand(state: dict) -> list[str]:
    """Render demand history and consolidation evidence."""
    out: list[str] = []
    for history_field, help_text in (
        ("cells", "Retained demand history cohorts"),
        ("cell_limit", "Maximum retained demand history cohorts"),
        ("observations", "Original observations counted in demand history"),
        ("overflow_observations", "Observations coalesced after shape cardinality overflow"),
    ):
        out += metric_lines(
            f"narwhal_demand_history_{history_field}",
            help_text,
            "gauge",
            [
                ({"window": name}, values[history_field])
                for name, values in (state.get("demand_history") or {}).items()
            ],
        )
    evidence = state.get("demand_evidence") or {}
    out += metric_lines(
        "narwhal_demand_evidence_span_seconds",
        "Elapsed span of arrival evidence retained for decode consolidation",
        "gauge",
        [({}, float(evidence.get("span_s") or 0.0))],
    )
    out += metric_lines(
        "narwhal_demand_evidence_arrivals",
        "Arrival samples retained for decode consolidation",
        "gauge",
        [({}, evidence.get("arrivals") or 0)],
    )
    out += metric_lines(
        "narwhal_demand_evidence_closed",
        "1 while the consolidation evidence window has closed",
        "gauge",
        [({}, 1 if evidence.get("closed") else 0)],
    )
    out += metric_lines(
        "narwhal_demand_evidence_risk_age_seconds",
        "Age of the newest decode risk event (0 while none is recorded)",
        "gauge",
        [({}, float(evidence.get("risk_age_s") or 0.0))],
    )
    out += metric_lines(
        "narwhal_demand_evidence_short_decode_engines",
        "Short-horizon decode demand estimate behind the rising-demand gate",
        "gauge",
        [({}, float(evidence.get("short_decode_engines") or 0.0))],
    )
    out += metric_lines(
        "narwhal_demand_evidence_envelope_decode_engines",
        "Conservative decode demand envelope priced into consolidation candidates",
        "gauge",
        [({}, float(evidence.get("envelope_decode_engines") or 0.0))],
    )
    trend_ratio = evidence.get("trend_ratio")
    if trend_ratio is not None:
        out += metric_lines(
            "narwhal_demand_evidence_trend_ratio",
            "Short/long decode demand ratio; consolidation refuses above 1 plus the tolerance",
            "gauge",
            [({}, float(trend_ratio))],
        )
    gate = evidence.get("blocked_gate") or "none"
    out += metric_lines(
        "narwhal_demand_evidence_refused",
        "1 on the gate currently refusing decode consolidation (risk, evidence, or trend)",
        "gauge",
        [({"gate": name}, 1 if gate == name else 0) for name in ("risk", "evidence", "trend")],
    )
    risk_events = evidence.get("risk_events") or {}
    if any(risk_events.values()):
        out += metric_lines(
            "narwhal_demand_evidence_risk_events_total",
            "Decode risk events re-arming the consolidation evidence window, by kind",
            "counter",
            [({"kind": kind}, n) for kind, n in sorted(risk_events.items())],
        )
    return out


# Each caller and target pair renders at 0 from startup; increase() skips a series' first sample.
FLIP_KEYS = ("decode_floor:decode", "floor_recovery:prefill", "reactive:decode", "reactive:prefill")


def render_controller(state: dict) -> list[str]:
    """Render role changes and controller diagnostics."""
    control = state.get("control") or {}
    flips = dict.fromkeys(FLIP_KEYS, 0) | (control.get("flips") or {})
    flip_samples: list[tuple[Mapping[str, str], float | int]] = []
    for key, count in sorted(flips.items()):
        by, to = key.rsplit(":", 1)
        flip_samples.append(({"to": to, "by": by}, count))

    out: list[str] = []
    out += metric_lines(
        "narwhal_flips_total",
        "Role changes by target pool and caller",
        "counter",
        flip_samples,
    )
    out += metric_lines(
        "narwhal_flip_reversals_total",
        "Role changes that put an instance back where it came from",
        "counter",
        [({}, control.get("flip_reversals", 0))],
    )
    out += metric_lines(
        "narwhal_flips_refused_total",
        "Role-change attempts declined by timing, availability, pins, floors, or advisory mode",
        "counter",
        [({}, control.get("flips_refused", 0))],
    )
    out += metric_lines(
        "narwhal_controller_advisory",
        "1 while controller decisions are recorded without changing roles",
        "gauge",
        [({}, 1.0 if control.get("advisory") else 0.0)],
    )
    decision = control.get("last_decision") or {}
    decision_counts = control.get("decisions") or {}
    decision_samples: list[tuple[Mapping[str, str], float | int]] = []
    for raw_key, value in sorted(decision_counts.items()):
        by, result = str(raw_key).rsplit(":", 1)
        decision_samples.append(({"by": by, "result": result}, float(value)))
    out += metric_lines(
        "narwhal_controller_decisions_total",
        "Controller decisions by caller and result",
        "counter",
        decision_samples,
    )
    if decision:
        out += metric_lines(
            "narwhal_controller_proposed_engines",
            "Engine count in the most recent controller decision",
            "gauge",
            [
                ({"role": "prefill"}, float(decision.get("prefill", 0))),
                ({"role": "decode"}, float(decision.get("decode", 0))),
            ],
        )
        out += metric_lines(
            "narwhal_controller_last_decision",
            "1 for the most recent controller decision and its result",
            "gauge",
            [
                (
                    {
                        "by": str(decision.get("by", "")),
                        "reason": str(decision.get("reason", "")),
                        "result": str(decision.get("result", "")),
                    },
                    1.0,
                )
            ],
        )
        projected = (
            [
                ({"phase": "prefill"}, float(decision["projected_ttft_ratio"])),
                ({"phase": "decode"}, float(decision["projected_tpot_ratio"])),
            ]
            if (
                decision.get("projected_ttft_ratio") is not None
                and decision.get("projected_tpot_ratio") is not None
            )
            else []
        )
        out += metric_lines(
            "narwhal_controller_projected_slo_ratio",
            "Projected SLO ratio after the most recent proposal",
            "gauge",
            projected,
        )
        work = []
        if decision.get("prefill_work") is not None:
            work = [
                ({"phase": "prefill"}, float(decision["prefill_work"])),
                ({"phase": "decode"}, float(decision["decode_work"])),
            ]
        out += metric_lines(
            "narwhal_controller_phase_work_engines",
            "Measured phase work in engine equivalents",
            "gauge",
            work,
        )
        objective = []
        if decision.get("objective") is not None:
            objective.append(({"value": "candidate"}, float(decision["objective"])))
        if decision.get("objective_delta") is not None:
            objective.append(({"value": "improvement"}, float(decision["objective_delta"])))
        out += metric_lines(
            "narwhal_controller_objective",
            "Candidate pressure and improvement over the current split",
            "gauge",
            objective,
        )
        decode_capacity = []
        if decision.get("decode_tokens_per_engine") is not None:
            decode_capacity.append(
                ({"value": "projected"}, float(decision["decode_tokens_per_engine"]))
            )
        if decision.get("decode_slo_capacity_tokens") is not None:
            decode_capacity.append(
                ({"value": "slo_capacity"}, float(decision["decode_slo_capacity_tokens"]))
            )
        if decision.get("decode_kv_capacity_tokens") is not None:
            decode_capacity.append(
                ({"value": "kv_capacity"}, float(decision["decode_kv_capacity_tokens"]))
            )
        out += metric_lines(
            "narwhal_controller_decode_tokens_per_engine",
            "Projected decode tokens per engine and profiled SLO capacity",
            "gauge",
            decode_capacity,
        )
        decode_requests: list[tuple[dict[str, str], float]] = []
        if decision.get("decode_requests_per_engine") is not None:
            decode_requests = [({}, float(decision["decode_requests_per_engine"]))]
        out += metric_lines(
            "narwhal_controller_decode_requests_per_engine",
            "Projected active decode requests per engine",
            "gauge",
            decode_requests,
        )
        decode_model: list[tuple[dict[str, str], float]] = []
        if decision.get("decode_correction") is not None:
            decode_model.append(({"value": "correction"}, float(decision["decode_correction"])))
        if decision.get("decode_profile_covered") is not None:
            decode_model.append(
                (
                    {"value": "profile_covered"},
                    1.0 if decision["decode_profile_covered"] else 0.0,
                )
            )
        out += metric_lines(
            "narwhal_controller_decode_model",
            "Live decode correction and measured-domain coverage",
            "gauge",
            decode_model,
        )
    out += metric_lines(
        "narwhal_flip_inflight_total",
        "Requests resident on an instance at the moment it was flipped",
        "counter",
        [
            ({"phase": phase}, (control.get("flip_inflight") or {}).get(phase, 0))
            for phase in ("prefill", "decode")
        ],
    )
    return out


def render_decode_floor(state: dict) -> list[str]:
    """Render decode floor and recovery counters."""
    out: list[str] = []
    floor = state.get("decode_floor") or {}
    out += metric_lines(
        "narwhal_decode_floor",
        "Configured minimum live decode engines",
        "gauge",
        [({}, float(floor.get("min_decode", 1)))],
    )
    out += metric_lines(
        "narwhal_decode_below_floor",
        "1 while live decode capacity is below min_decode",
        "gauge",
        [({}, 1.0 if floor.get("below_floor") else 0.0)],
    )
    out += metric_lines(
        "narwhal_decode_floor_restorations_total",
        "Moves made to restore the live decode floor",
        "counter",
        [({}, float(floor.get("restoration_moves", 0)))],
    )
    return out
