"""Request totals, admission, refusals, outcomes and SLO budget metrics."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from ...contracts import METRICS, current
from .exposition import metric_lines, slo_label

# Request totals a state handoff carries, by their `/narwhal/state` field.
_TOP_LEVEL = (
    "offered",
    "unsized_offered",
    "served",
    "slo_met",
    "expired",
    "failed",
    "cancelled",
    "invalid_requests",
    "unserved",
)
_ADMISSION = ("rejected", "refused")


@dataclass
class AdoptedCounts:
    """Counts that state handoffs added to this router process's own counts.

    `/narwhal/state` and the next handoff report totals that include them. `/metrics` leaves
    them out, so each counter counts what this process observed and changes only by its own
    increments and its restart.
    """

    # The reasons each terminal outcome exports from process start, at 0 until one occurs.
    exported: Mapping[str, Collection[str]] = field(default_factory=dict)
    totals: Counter[str] = field(default_factory=Counter)
    reasons: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    # Decode risk events behind narwhal_demand_evidence_risk_events_total, by kind.
    risk_events: Counter[str] = field(default_factory=Counter)

    def add(
        self,
        before: dict[str, int],
        after: dict[str, int],
        reasons_before: dict[str, Counter[str]],
        reasons_after: dict[str, Counter[str]],
    ) -> None:
        """Record the change one handoff made to the totals and their reason counts."""
        for name, value in after.items():
            self.totals[name] += value - before.get(name, 0)
        for terminal, counts in reasons_after.items():
            previous = reasons_before.get(terminal, Counter())
            for reason in set(counts) | set(previous):
                self.reasons[terminal][reason] += counts[reason] - previous[reason]

    def local(self, state: dict) -> dict:
        """Return `state` with the adopted counts taken out."""
        out = dict(state)
        for name in _TOP_LEVEL:
            if name in out:
                out[name] = out[name] - self.totals[name]
        if "admission" in out:
            admission = dict(out["admission"])
            for name in _ADMISSION:
                if name in admission:
                    admission[name] = admission[name] - self.totals[name]
            out["admission"] = admission
        if "outcome_reasons" in out:
            # Another reason appears once this process counts one.
            out["outcome_reasons"] = {
                terminal: {
                    reason: local
                    for reason, count in counts.items()
                    if (local := count - self.reasons[terminal][reason])
                    or reason in self.exported.get(terminal, ())
                }
                for terminal, counts in out["outcome_reasons"].items()
            }
        evidence = out.get("demand_evidence")
        if isinstance(evidence, dict) and "risk_events" in evidence:
            events = evidence["risk_events"]
            out["demand_evidence"] = {
                **evidence,
                "risk_events": {
                    kind: count - self.risk_events[kind] for kind, count in events.items()
                },
            }
        return out


def _by_reason(state: dict, terminal: str, label: str) -> list:
    """Return one sample per reason of a terminal outcome."""
    return [
        ({label: reason}, count) for reason, count in state["outcome_reasons"][terminal].items()
    ]


def render_admission(state: dict) -> list[str]:
    """Render request totals, queue occupancy and retry budgets."""
    out: list[str] = []
    out += metric_lines(
        "narwhal_contract_info",
        "Machine-readable Narwhal metrics contract version",
        "gauge",
        [({"contract": "metrics", "version": str(current(METRICS))}, 1)],
    )
    out += metric_lines(
        "narwhal_served_total",
        "Requests completed without error",
        "counter",
        [({}, state.get("served", 0))],
    )
    out += metric_lines(
        "narwhal_slo_met_total",
        "Requests completed within the TTFT and TPOT SLOs",
        "counter",
        [({}, state.get("slo_met", 0))],
    )
    for field_name, help_text in (
        ("offered", "Original completion requests received, including early refusals"),
        ("unsized_offered", "Original requests terminated before input sizing"),
    ):
        out += metric_lines(
            f"narwhal_{field_name}_total", help_text, "counter", [({}, state.get(field_name, 0))]
        )
    out += metric_lines(
        "narwhal_expired_total",
        "Original requests terminated by their admission or total deadline, by reason",
        "counter",
        _by_reason(state, "expired", "reason"),
    )
    serving = state.get("serving") or {}
    for field_name, help_text in (
        ("prefill_attempts", "Prefill HTTP attempts including retries"),
        ("decode_attempts", "Decode HTTP attempts including retries"),
        ("retry_attempts", "Additional prefill attempts after an original attempt failed"),
        ("retry_credits_spent", "Retry credits spent by retries that dispatched"),
        ("retry_denied", "Retries denied by the shared retry quota"),
        ("served_after_retry", "Requests completed by an attempt after the first"),
        ("decode_tokens_observed", "Exact decode tokens read across all attempts when supported"),
        ("output_tokens", "Measured output tokens of completed requests"),
    ):
        out += metric_lines(
            f"narwhal_{field_name}_total", help_text, "counter", [({}, serving.get(field_name, 0))]
        )
    out += metric_lines(
        "narwhal_attempt_failures_total",
        "Failed attempts by request phase and failure reason",
        "counter",
        [
            ({"phase": row["phase"], "reason": row["reason"]}, row["count"])
            for row in serving.get("attempt_failures", [])
        ],
    )
    for field_name, help_text in (
        ("http_retained", "Completion requests holding body, queue or response capacity"),
        ("http_retained_limit", "Maximum retained completion HTTP requests"),
        ("http_retained_high_water", "Largest number of retained completion HTTP requests"),
        ("retry_credits", "Retry credits currently available"),
    ):
        out += metric_lines(
            f"narwhal_{field_name}", help_text, "gauge", [({}, serving.get(field_name, 0))]
        )
    admission = state.get("admission") or {}
    for name, field_name, help_text in (
        ("admission_inflight", "inflight", "Requests holding an admission seat"),
        (
            "admission_inflight_limit",
            "limit",
            "Router in-flight limit: --max-concurrent, else serving.max_connections",
        ),
        ("router_loop_lag_seconds", "loop_lag_s", "Latest router event-loop wake-up lateness"),
        (
            "request_sizing_delay_seconds",
            "sizing_delay_s",
            "Median request sizing delay over the trailing window",
        ),
        (
            "saturation_threshold_seconds",
            "saturation_threshold_s",
            "Loop lag or sizing delay at which ingress answers a saturation 429",
        ),
    ):
        out += metric_lines(f"narwhal_{name}", help_text, "gauge", [({}, admission[field_name])])
    out += metric_lines(
        "narwhal_admission_info",
        "Admission mode set by serving.admission",
        "gauge",
        [({"mode": admission["mode"]}, 1)],
    )
    out += metric_lines(
        "narwhal_admission_margin",
        "Fraction serving.admission_margin adds to the TTFT budget",
        "gauge",
        [({}, admission["margin"])],
    )
    for field_name in (
        "queued",
        "queue_capacity",
        "queue_high_water",
        "waiting_prefill",
        "waiting_decode",
    ):
        out += metric_lines(
            f"narwhal_{field_name}",
            "Bounded request queue occupancy or capacity",
            "gauge",
            [({}, admission.get(field_name, 0))],
        )
    seats = (state.get("seats") or {}).get("engines") or {}
    out += metric_lines(
        "narwhal_engine_seats",
        "Engine seats by phase; 0 means no seat limit",
        "gauge",
        [
            ({"iid": iid, "phase": phase}, entry.get(phase, 0))
            for iid, entry in sorted(seats.items())
            for phase in ("prefill", "decode")
        ],
    )
    handoff = state.get("handoff") or {}
    for name, field_name, help_text in (
        ("kv_lease_seconds", "kv_lease_s", "Producer KV lease from the engine's attestation"),
        (
            "handoff_bound_seconds",
            "bound_s",
            "Time after prefill completion by which decode must reach its engine",
        ),
    ):
        out += metric_lines(
            f"narwhal_{name}",
            help_text,
            "gauge",
            [
                ({"iid": iid}, entry[field_name])
                for iid, entry in sorted(handoff.items())
                if entry.get(field_name) is not None
            ],
        )
    out += metric_lines(
        "narwhal_upstream_seconds_total",
        "Summed HTTP leg duration including transfer and failed attempts; not GPU execution time",
        "counter",
        [
            ({"phase": phase}, value)
            for phase, value in sorted(serving.get("upstream_seconds", {}).items())
        ],
    )
    return out


def render_refusals(state: dict) -> list[str]:
    """Render admission refusal, rejection and invalid-request counters."""
    out: list[str] = []
    out += metric_lines(
        "narwhal_refused_total",
        "Requests predictive admission refused for a projected TTFT or decode SLO miss, by cause",
        "counter",
        _by_reason(state, "refused", "cause"),
    )
    out += metric_lines(
        "narwhal_rejected_total",
        "Requests refused with HTTP 429 for capacity or HTTP 503 for router readiness, by reason",
        "counter",
        _by_reason(state, "rejected", "reason"),
    )
    out += metric_lines(
        "narwhal_invalid_requests_total",
        "Client requests rejected as malformed or unsupported before dispatch",
        "counter",
        [({}, state.get("invalid_requests", 0))],
    )
    return out


def render_outcomes(state: dict) -> list[str]:
    """Render request failures, cancellations and retained SLO outcomes."""
    out: list[str] = []
    out += metric_lines(
        "narwhal_failed_total",
        "Requests that ended in an error, by reason",
        "counter",
        _by_reason(state, "failed", "reason"),
    )
    out += metric_lines(
        "narwhal_cancelled_total",
        "Requests abandoned by their client before completion",
        "counter",
        [({}, state.get("cancelled", 0))],
    )
    out += metric_lines(
        "narwhal_unserved_total",
        "Phase placements with no SLO-eligible candidate",
        "counter",
        [({}, state.get("unserved", 0))],
    )
    # Pruning expired outcome buckets can lower these totals.
    attainment = state.get("attainment") or {}
    out += metric_lines(
        "narwhal_attainment_evidence_covered_seconds",
        "Observed SLO outcome span, oldest bucket to now, capped at the retained span",
        "gauge",
        [({}, float(attainment.get("covered_s", 0.0)))],
    )
    out += metric_lines(
        "narwhal_attainment_evidence_outcomes",
        "Completed-request outcomes currently held as SLO outcome diagnostics",
        "gauge",
        [({}, attainment.get("outcomes", 0))],
    )
    out += metric_lines(
        "narwhal_attainment_evidence_buckets",
        "Time buckets currently holding SLO outcome diagnostics",
        "gauge",
        [({}, attainment.get("buckets", 0))],
    )
    # Present after beyond-span evidence has been dropped.
    if attainment.get("pruned_buckets", 0) or attainment.get("pruned_outcomes", 0):
        out += metric_lines(
            "narwhal_attainment_evidence_pruned_total",
            "Attainment evidence dropped after aging beyond the retained span",
            "counter",
            [
                ({"kind": "buckets"}, attainment.get("pruned_buckets", 0)),
                ({"kind": "outcomes"}, attainment.get("pruned_outcomes", 0)),
            ],
        )
    return out


def render_slo(state: dict) -> list[str]:
    """Render the latency budgets that define histogram buckets and admission."""
    slo = state.get("slo") or {}
    return metric_lines(
        "narwhal_slo_seconds",
        "Configured service-level latency budget in seconds",
        "gauge",
        [
            ({"metric": metric, "slo": slo_label(float(slo[field]))}, float(slo[field]))
            for metric, field in (("ttft", "ttft_s"), ("tpot", "tpot_s"))
            if slo.get(field) is not None
        ],
    )
