"""Request totals, admission, refusals, outcomes and SLO budget metrics."""

from __future__ import annotations

from ...contracts import METRICS, current
from .exposition import metric_lines, slo_label


def _by_reason(state: dict, terminal: str, label: str, total: int) -> list:
    """Return one sample per reason, or the unlabelled total when reasons are absent."""
    reasons = (state.get("outcome_reasons") or {}).get(terminal)
    if reasons is None:
        return [({}, total)]
    return [({label: reason}, count) for reason, count in reasons.items()]


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
        _by_reason(state, "expired", "reason", state.get("expired", 0)),
    )
    serving = state.get("serving") or {}
    for field_name, help_text in (
        ("prefill_attempts", "Prefill HTTP attempts including retries"),
        ("decode_attempts", "Decode HTTP attempts including retries"),
        ("retry_attempts", "Additional prefill attempts after an original attempt failed"),
        ("retry_credits_spent", "Retry credits consumed, including cancelled backoffs"),
        ("retry_denied", "Retries denied by the shared retry quota"),
        ("served_after_retry", "Requests completed by an attempt after the first"),
        ("decode_tokens_observed", "Exact decode tokens read across all attempts when supported"),
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
    if "mode" in admission:
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
            [({}, admission.get("margin", 0.0))],
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
    admission = state.get("admission") or {}
    out += metric_lines(
        "narwhal_refused_total",
        "Requests predictive admission refused for a projected TTFT or decode SLO miss, by cause",
        "counter",
        _by_reason(state, "refused", "cause", admission.get("refused", 0)),
    )
    out += metric_lines(
        "narwhal_rejected_total",
        "Requests refused with HTTP 429 for capacity or HTTP 503 for router readiness, by reason",
        "counter",
        _by_reason(state, "rejected", "reason", admission.get("rejected", 0)),
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
        _by_reason(state, "failed", "reason", state.get("failed", 0)),
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
