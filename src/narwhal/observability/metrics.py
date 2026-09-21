"""Render router metrics in Prometheus text format."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..contracts import METRICS, current

# Concentrate histogram resolution around the SLO boundary.
_SLO_FRACTIONS = (0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 1.0, 1.5, 3.0, 10.0)


def buckets_for(slo_s: float) -> tuple[float, ...]:
    """Scale histogram edges to an SLO in seconds."""
    return tuple(round(f * slo_s, 6) for f in _SLO_FRACTIONS)


@dataclass
class Histogram:
    """Prometheus histogram with fixed bucket edges."""

    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    n: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        """Record one observation."""
        self.total += value
        self.n += 1
        for i, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[i] += 1

    def render(self, name: str, help_text: str) -> list[str]:
        """Render the histogram's Prometheus exposition lines."""
        out = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
        for edge, count in zip(self.buckets, self.counts, strict=True):
            out.append(f'{name}_bucket{{le="{edge}"}} {count}')
        out.append(f'{name}_bucket{{le="+Inf"}} {self.n}')
        out.append(f"{name}_sum {self.total}")
        out.append(f"{name}_count {self.n}")
        return out


def _lines(
    name: str,
    help_text: str,
    kind: str,
    samples: Sequence[tuple[Mapping[str, str], float | int]],
) -> list[str]:
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        label_s = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else ""
        out.append(f"{name}{label_s} {value}")
    return out


def _render_admission(state: dict) -> list[str]:
    """Render request totals, queue occupancy and retry budgets."""
    out: list[str] = []
    out += _lines(
        "narwhal_contract_info",
        "Machine-readable Narwhal metrics contract version",
        "gauge",
        [({"contract": "metrics", "version": str(current(METRICS))}, 1)],
    )
    out += _lines(
        "narwhal_served_total",
        "Requests completed without error",
        "counter",
        [({}, state.get("served", 0))],
    )
    for field_name, help_text in (
        ("offered", "Original completion requests received, including early refusals"),
        ("unsized_offered", "Original requests terminated before input sizing"),
        ("expired", "Original requests terminated by their admission or total deadline"),
    ):
        out += _lines(
            f"narwhal_{field_name}_total", help_text, "counter", [({}, state.get(field_name, 0))]
        )
    serving = state.get("serving") or {}
    for field_name, help_text in (
        ("prefill_attempts", "Prefill HTTP attempts including retries"),
        ("decode_attempts", "Decode HTTP attempts including retries"),
        ("retry_attempts", "Additional prefill attempts after an original attempt failed"),
        ("retry_credits_spent", "Retry credits consumed, including cancelled backoffs"),
        ("retry_denied", "Retries denied by the shared retry quota"),
        ("decode_tokens_observed", "Exact decode tokens read across all attempts when supported"),
    ):
        out += _lines(
            f"narwhal_{field_name}_total", help_text, "counter", [({}, serving.get(field_name, 0))]
        )
    for field_name, help_text in (
        ("http_retained", "Completion requests holding body, queue or response capacity"),
        ("http_retained_limit", "Maximum retained completion HTTP requests"),
        ("http_retained_high_water", "Largest number of retained completion HTTP requests"),
        ("retry_credits", "Retry credits currently available"),
    ):
        out += _lines(
            f"narwhal_{field_name}", help_text, "gauge", [({}, serving.get(field_name, 0))]
        )
    admission = state.get("admission") or {}
    for field_name in (
        "queued",
        "queue_capacity",
        "queue_high_water",
        "waiting_prefill",
        "waiting_decode",
    ):
        out += _lines(
            f"narwhal_{field_name}",
            "Bounded request queue occupancy or capacity",
            "gauge",
            [({}, admission.get(field_name, 0))],
        )
    out += _lines(
        "narwhal_upstream_seconds_total",
        "Summed HTTP leg duration including transfer and failed attempts; not GPU execution time",
        "counter",
        [
            ({"phase": phase}, value)
            for phase, value in sorted(serving.get("upstream_seconds", {}).items())
        ],
    )
    return out


def _render_runtime(state: dict) -> list[str]:
    """Render router readiness and runtime status."""
    out: list[str] = []
    ha = state.get("ha") or {}
    out += _lines(
        "narwhal_router_ready",
        "1 while this router may admit new traffic",
        "gauge",
        [({}, 1 if ha.get("ready", True) else 0)],
    )
    out += _lines(
        "narwhal_router_lease_epoch",
        "Current router lease epoch, or zero when fencing is disabled",
        "gauge",
        [({}, ha.get("epoch", 0))],
    )
    monitoring = state.get("monitoring") or {}
    out += _lines(
        "narwhal_monitoring_degraded",
        "1 while repeated monitoring-pass failures fence new admissions",
        "gauge",
        [({}, 1 if monitoring.get("degraded") else 0)],
    )
    out += _lines(
        "narwhal_monitoring_core_consecutive_failures",
        "Consecutive monitoring passes in which any stage failed",
        "gauge",
        [({}, monitoring.get("core_consecutive", 0))],
    )
    out += _lines(
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
        out += _lines(
            f"narwhal_{field_name.removesuffix('_s')}_seconds",
            help_text,
            "gauge",
            [({}, monitoring.get(field_name, 0.0))],
        )
    monitoring_stages = monitoring.get("stages") or {}
    out += _lines(
        "narwhal_monitoring_stage_failures_total",
        "Monitoring stage failures, by stage",
        "counter",
        [
            ({"stage": name}, record.get("failures", 0))
            for name, record in sorted(monitoring_stages.items())
        ],
    )
    out += _lines(
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
    out += _lines(
        "narwhal_engine_draining",
        "1 while an operator drain excludes the engine from new placement",
        "gauge",
        [
            ({"iid": iid}, 1 if entry.get("draining") else 0)
            for iid, entry in lifecycle_engines.items()
        ],
    )
    out += _lines(
        "narwhal_engine_ready_to_stop",
        "1 after a drained engine has no resident work and a recorded process identity",
        "gauge",
        [
            ({"iid": iid}, 1 if entry.get("ready_to_stop") else 0)
            for iid, entry in lifecycle_engines.items()
        ],
    )
    return out


def _render_roles(state: dict) -> list[str]:
    """Render engine role assignments."""
    out: list[str] = []
    out += _lines(
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


def _render_refusals(state: dict) -> list[str]:
    """Render admission refusal, rejection and invalid-request counters."""
    out: list[str] = []
    out += _lines(
        "narwhal_refused_total",
        "Requests predictive admission refused above the TTFT budget",
        "counter",
        [({}, state.get("admission", {}).get("refused", 0))],
    )
    out += _lines(
        "narwhal_rejected_total",
        "Requests rejected by authentication or a concurrency limit",
        "counter",
        [({}, state.get("admission", {}).get("rejected", 0))],
    )
    out += _lines(
        "narwhal_invalid_requests_total",
        "Client requests rejected as malformed or unsupported before dispatch",
        "counter",
        [({}, state.get("invalid_requests", 0))],
    )
    return out


def _render_health(state: dict) -> list[str]:
    """Render engine probation and drift-window metrics."""
    out: list[str] = []
    out += _lines(
        "narwhal_probation_instances",
        "Engines the drift instrument has deprioritized",
        "gauge",
        [({"iid": iid}, 1) for iid in state.get("probation", [])],
    )
    health = state.get("health") or {}
    out += _lines(
        "narwhal_health_windows_scored_total",
        "Drift windows that collected enough observations to reach a verdict",
        "counter",
        [({"iid": iid}, v.get("scored", 0)) for iid, v in sorted(health.items())],
    )
    out += _lines(
        "narwhal_health_windows_undersampled_total",
        "Drift windows that dropped gathered evidence too sparse to score",
        "counter",
        [({"iid": iid}, v.get("undersampled", 0)) for iid, v in sorted(health.items())],
    )
    out += _lines(
        "narwhal_health_prefill_paused",
        "Decode drift evidence paused until fresh gaps without local prefill interference",
        "gauge",
        [({"iid": iid}, int(v.get("prefill_paused", False))) for iid, v in sorted(health.items())],
    )
    out += _lines(
        "narwhal_health_prefill_pauses_total",
        "Times local prefill interference restarted decode drift evidence",
        "counter",
        [({"iid": iid}, v.get("prefill_pauses", 0)) for iid, v in sorted(health.items())],
    )
    return out


def _render_outcomes(state: dict) -> list[str]:
    """Render request failures, cancellations and retained SLO outcomes."""
    out: list[str] = []
    out += _lines(
        "narwhal_failed_total",
        "Requests that ended in an error",
        "counter",
        [({}, state.get("failed", 0))],
    )
    out += _lines(
        "narwhal_cancelled_total",
        "Requests abandoned by their client before completion",
        "counter",
        [({}, state.get("cancelled", 0))],
    )
    out += _lines(
        "narwhal_unserved_total",
        "Phase placements with no SLO-eligible candidate",
        "counter",
        [({}, state.get("unserved", 0))],
    )
    # Pruning expired outcome buckets can lower these totals, so use gauges.
    attainment = state.get("attainment") or {}
    out += _lines(
        "narwhal_attainment_evidence_covered_seconds",
        "Observed SLO outcome span, oldest bucket to now, capped at the retained span",
        "gauge",
        [({}, float(attainment.get("covered_s", 0.0)))],
    )
    out += _lines(
        "narwhal_attainment_evidence_outcomes",
        "Completed-request outcomes currently held as SLO outcome diagnostics",
        "gauge",
        [({}, attainment.get("outcomes", 0))],
    )
    out += _lines(
        "narwhal_attainment_evidence_buckets",
        "Time buckets currently holding SLO outcome diagnostics",
        "gauge",
        [({}, attainment.get("buckets", 0))],
    )
    # Emitted only once beyond-span evidence has actually been dropped.
    if attainment.get("pruned_buckets", 0) or attainment.get("pruned_outcomes", 0):
        out += _lines(
            "narwhal_attainment_evidence_pruned_total",
            "Attainment evidence dropped after aging beyond the retained span",
            "counter",
            [
                ({"kind": "buckets"}, attainment.get("pruned_buckets", 0)),
                ({"kind": "outcomes"}, attainment.get("pruned_outcomes", 0)),
            ],
        )
    return out


def _render_demand(state: dict) -> list[str]:
    """Render demand history and consolidation evidence."""
    out: list[str] = []
    for history_field, help_text in (
        ("cells", "Retained demand history cohorts"),
        ("cell_limit", "Maximum retained demand history cohorts"),
        ("observations", "Original observations counted in demand history"),
        ("overflow_observations", "Observations coalesced after shape cardinality overflow"),
    ):
        out += _lines(
            f"narwhal_demand_history_{history_field}",
            help_text,
            "gauge",
            [
                ({"window": name}, values[history_field])
                for name, values in (state.get("demand_history") or {}).items()
            ],
        )
    evidence = state.get("demand_evidence") or {}
    out += _lines(
        "narwhal_demand_evidence_span_seconds",
        "Elapsed span of arrival evidence retained for decode consolidation",
        "gauge",
        [({}, float(evidence.get("span_s") or 0.0))],
    )
    out += _lines(
        "narwhal_demand_evidence_arrivals",
        "Arrival samples retained for decode consolidation",
        "gauge",
        [({}, evidence.get("arrivals") or 0)],
    )
    out += _lines(
        "narwhal_demand_evidence_closed",
        "1 while the consolidation evidence window has closed",
        "gauge",
        [({}, 1 if evidence.get("closed") else 0)],
    )
    out += _lines(
        "narwhal_demand_evidence_risk_age_seconds",
        "Age of the newest decode risk event (0 while none is recorded)",
        "gauge",
        [({}, float(evidence.get("risk_age_s") or 0.0))],
    )
    out += _lines(
        "narwhal_demand_evidence_short_decode_engines",
        "Short-horizon decode demand estimate behind the rising-demand gate",
        "gauge",
        [({}, float(evidence.get("short_decode_engines") or 0.0))],
    )
    out += _lines(
        "narwhal_demand_evidence_envelope_decode_engines",
        "Conservative decode demand envelope priced into consolidation candidates",
        "gauge",
        [({}, float(evidence.get("envelope_decode_engines") or 0.0))],
    )
    trend_ratio = evidence.get("trend_ratio")
    if trend_ratio is not None:
        out += _lines(
            "narwhal_demand_evidence_trend_ratio",
            "Short/long decode demand ratio; consolidation refuses above 1 plus the tolerance",
            "gauge",
            [({}, float(trend_ratio))],
        )
    gate = evidence.get("blocked_gate") or "none"
    out += _lines(
        "narwhal_demand_evidence_refused",
        "1 on the gate currently refusing decode consolidation (risk, evidence, or trend)",
        "gauge",
        [({"gate": name}, 1 if gate == name else 0) for name in ("risk", "evidence", "trend")],
    )
    risk_events = evidence.get("risk_events") or {}
    if any(risk_events.values()):
        out += _lines(
            "narwhal_demand_evidence_risk_events_total",
            "Decode risk events re-arming the consolidation evidence window, by kind",
            "counter",
            [({"kind": kind}, n) for kind, n in sorted(risk_events.items())],
        )
    return out


def _render_availability(state: dict) -> list[str]:
    """Render engine ejections, breaker state and prefill-floor breaches."""
    out: list[str] = []
    out += _lines(
        "narwhal_ejected_instances",
        "Instances the breaker currently holds out of scheduling and both loads",
        "gauge",
        [({}, len(state.get("ejected", [])))],
    )
    out += _lines(
        "narwhal_ejected",
        "1 while this instance is ejected",
        "gauge",
        [({"iid": iid}, 1) for iid in state.get("ejected", [])],
    )
    breaker = state.get("breaker") or {}
    streak_samples: list[tuple[Mapping[str, str], float | int]] = [
        ({"iid": iid, "class": klass}, count)
        for iid, row in sorted((breaker.get("failures") or {}).items())
        for klass, count in sorted(row.items())
    ]
    out += _lines(
        "narwhal_engine_breaker_streak",
        "Consecutive engine failures held against one engine, by breaker class",
        "gauge",
        streak_samples,
    )
    out += _lines(
        "narwhal_engine_breaker_verifying",
        "1 while this engine has a breaker verification probe in flight, by kind",
        "gauge",
        [
            ({"iid": v.get("iid", ""), "kind": v.get("kind", "")}, 1)
            for v in breaker.get("verifying", [])
        ],
    )
    floor = state.get("below_floor") or {}
    out += _lines(
        "narwhal_prefill_below_floor",
        "1 while the live prefill pool is below min_prefill",
        "gauge",
        [({}, 1 if floor.get("active") else 0)],
    )
    out += _lines(
        "narwhal_prefill_below_floor_events_total",
        "Prefill floor breaches",
        "counter",
        [({}, floor.get("breaches", 0))],
    )
    out += _lines(
        "narwhal_prefill_below_floor_seconds_total",
        "Seconds spent below the prefill floor",
        "counter",
        [({}, floor.get("cumulative_s", 0.0))],
    )
    return out


def _render_controller(state: dict) -> list[str]:
    """Render role changes and controller diagnostics."""
    control = state.get("control") or {}
    flip_samples: list[tuple[Mapping[str, str], float | int]] = []
    for key, count in sorted((control.get("flips") or {}).items()):
        by, to = key.rsplit(":", 1)
        flip_samples.append(({"to": to, "by": by}, count))

    out: list[str] = []
    out += _lines(
        "narwhal_flips_total",
        "Role changes by target pool and caller",
        "counter",
        flip_samples,
    )
    out += _lines(
        "narwhal_flip_reversals_total",
        "Role changes that put an instance back where it came from",
        "counter",
        [({}, control.get("flip_reversals", 0))],
    )
    out += _lines(
        "narwhal_flips_refused_total",
        "Role-change attempts declined by timing, availability, pins, floors, or advisory mode",
        "counter",
        [({}, control.get("flips_refused", 0))],
    )
    out += _lines(
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
    out += _lines(
        "narwhal_controller_decisions_total",
        "Controller decisions by caller and result",
        "counter",
        decision_samples,
    )
    if decision:
        out += _lines(
            "narwhal_controller_proposed_engines",
            "Engine count in the most recent controller decision",
            "gauge",
            [
                ({"role": "prefill"}, float(decision.get("prefill", 0))),
                ({"role": "decode"}, float(decision.get("decode", 0))),
            ],
        )
        out += _lines(
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
        out += _lines(
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
        out += _lines(
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
        out += _lines(
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
        out += _lines(
            "narwhal_controller_decode_tokens_per_engine",
            "Projected decode tokens per engine and profiled SLO capacity",
            "gauge",
            decode_capacity,
        )
        decode_requests: list[tuple[dict[str, str], float]] = []
        if decision.get("decode_requests_per_engine") is not None:
            decode_requests = [({}, float(decision["decode_requests_per_engine"]))]
        out += _lines(
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
        out += _lines(
            "narwhal_controller_decode_model",
            "Live decode correction and measured-domain coverage",
            "gauge",
            decode_model,
        )
    out += _lines(
        "narwhal_flip_inflight_total",
        "Requests resident on an instance at the moment it was flipped",
        "counter",
        [
            ({"phase": phase}, (control.get("flip_inflight") or {}).get(phase, 0))
            for phase in ("prefill", "decode")
        ],
    )
    return out


def _render_work(state: dict) -> list[str]:
    """Render pool sizes, load and resident requests."""
    out: list[str] = []
    out += _lines(
        "narwhal_pool_instances",
        "Instances in each pool",
        "gauge",
        [({"role": r}, len(ids)) for r, ids in sorted(state.get("pools", {}).items())],
    )
    out += _lines(
        "narwhal_pool_load",
        "Pool load as a ratio against its own SLO target",
        "gauge",
        [({"role": r}, v) for r, v in sorted(state.get("load", {}).items())],
    )
    out += _lines(
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


def _render_slo(state: dict) -> list[str]:
    """Render the latency budgets that define histogram buckets and admission."""
    slo = state.get("slo") or {}
    return _lines(
        "narwhal_slo_seconds",
        "Configured service-level latency budget in seconds",
        "gauge",
        [
            ({"metric": metric}, float(slo[field]))
            for metric, field in (("ttft", "ttft_s"), ("tpot", "tpot_s"))
            if slo.get(field) is not None
        ],
    )


def _render_decode_floor(state: dict) -> list[str]:
    """Render decode floor and recovery counters."""
    out: list[str] = []
    floor = state.get("decode_floor") or {}
    out += _lines(
        "narwhal_decode_floor",
        "Configured minimum live decode engines",
        "gauge",
        [({}, float(floor.get("min_decode", 1)))],
    )
    out += _lines(
        "narwhal_decode_below_floor",
        "1 while live decode capacity is below min_decode",
        "gauge",
        [({}, 1.0 if floor.get("below_floor") else 0.0)],
    )
    out += _lines(
        "narwhal_decode_floor_restorations_total",
        "Moves made to restore the live decode floor",
        "counter",
        [({}, float(floor.get("restoration_moves", 0)))],
    )
    return out


def render(
    state: dict,
    ttft: Histogram,
    tpot: Histogram,
    seat: Histogram | None = None,
    queue_wait: Histogram | None = None,
) -> str:
    """Render a `/narwhal/state` snapshot plus the latency histograms."""
    out: list[str] = []
    out += _render_admission(state)
    out += _render_runtime(state)
    out += _render_roles(state)
    out += _render_refusals(state)
    out += _render_health(state)
    out += _render_outcomes(state)
    out += _render_demand(state)
    out += _render_availability(state)
    out += _render_controller(state)
    out += _render_work(state)
    out += _render_slo(state)
    out += ttft.render("narwhal_ttft_seconds", "Router arrival to prefill completion")
    out += tpot.render(
        "narwhal_tpot_seconds", "Mean seconds per output token after prefill completion"
    )
    out += _render_decode_floor(state)
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
