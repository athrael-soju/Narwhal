"""Pydantic models for the public HTTP responses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthOut(BaseModel):
    """Health response."""

    status: str
    instances: int
    available_instances: int


class ModelOut(BaseModel):
    """OpenAI model entry."""

    id: str
    object: str
    owned_by: str


class ModelsOut(BaseModel):
    """OpenAI model list."""

    object: str
    data: list[ModelOut]


class AdmissionOut(BaseModel):
    """Admission occupancy and refusal counts.

    `rejected` is pool exhaustion, `refused` is the cost model pricing every
    landing over the TTFT budget before the request dispatches.
    """

    inflight: int
    queued: int = 0
    queue_capacity: int = 0
    queue_high_water: int = 0
    waiting_prefill: int = 0
    waiting_decode: int = 0
    limit: int
    rejected: int
    refused: int
    # Engine-authentication mode: boundary or engine-credential.
    engine_auth: str = "boundary"


class ServingOut(BaseModel):
    """HTTP retention and physical attempt accounting across original requests."""

    http_retained: int = 0
    http_retained_limit: int = 0
    http_retained_high_water: int = 0
    prefill_attempts: int = 0
    decode_attempts: int = 0
    retry_attempts: int = 0
    retry_credits: float = 0.0
    retry_credits_spent: int = 0
    retry_denied: int = 0
    decode_tokens_observed: int = 0
    upstream_seconds: dict[str, float] = Field(default_factory=dict)


class HttpPoolsOut(BaseModel):
    """Engine HTTP pool policy: bounded data legs, reserved control probes.

    `data_connections` is the request-leg pool behind the admission limit,
    `control_connections` the reserved health and recovery probe pool, and
    `pool_timeout_s` the maximum wait for a connection slot on either.
    """

    data_connections: int
    control_connections: int
    pool_timeout_s: float


class PoolsOut(BaseModel):
    """Engine IDs grouped by current role."""

    prefill: list[str]
    decode: list[str]


class LoadOut(BaseModel):
    """SLO-relative load for each pool."""

    prefill: float
    decode: float


class ThresholdsOut(BaseModel):
    """Active controller thresholds."""

    expand: float
    shrink: float
    cooldown_s: float
    sustained_intervals: int
    dwell_s: float
    panic_ratio: float


class SLOOut(BaseModel):
    """Latency targets used by placement and control."""

    ttft_s: float
    tpot_s: float


class ResidentOut(BaseModel):
    """In-flight counts for one engine."""

    prefill: int
    decode: int


class FlipOut(BaseModel):
    """Completed role change."""

    at: float
    iid: str
    to: str
    by: str
    prefill_inflight: int
    decode_inflight: int
    drained_s: float | None


class FlipRefusedOut(BaseModel):
    """Role change refused by a controller guard."""

    at: float
    to: str
    why: str


class AttainmentOut(BaseModel):
    """SLO outcome counts in `bucket_s` time buckets retained for `retained_s` seconds.

    Prune counters track expired buckets and their outcomes.
    """

    bucket_s: float = 0.0
    retained_s: float = 0.0
    covered_s: float = 0.0
    buckets: int = 0
    outcomes: int = 0
    pruned_buckets: int = 0
    pruned_outcomes: int = 0


class DemandEvidenceOut(BaseModel):
    """Demand estimates, window coverage and risk events used to decide whether
    a decode engine can move to prefill.
    """

    span_s: float = 0.0
    arrivals: int = 0
    required_span_s: float = 0.0
    required_arrivals: int = 0
    max_span_s: float = 0.0
    closed: bool = False
    risk_kind: str | None = None
    risk_age_s: float | None = None
    risk_events: dict[str, int] = Field(default_factory=dict)
    short_decode_engines: float | None = None
    long_decode_engines: float | None = None
    trend_ratio: float | None = None
    envelope_decode_engines: float | None = None
    blocked_gate: str = "none"


class MonitoringStageOut(BaseModel):
    """Failure ledger for one monitoring stage.

    `failures` and `consecutive` are per-stage failure totals and current
    streaks. `last_class` names the most recent exception class with no
    message; `last_at` stamps it on the router's monotonic clock.
    """

    failures: int = 0
    consecutive: int = 0
    last_class: str | None = None
    last_at: float | None = None


class MonitoringOut(BaseModel):
    """Monitoring-loop failure accounting and the degraded admission gate.

    `core_consecutive` counts consecutive monitoring passes in which any
    stage failed; only a pass with zero stage failures resets it.
    `core_failures` is the lifetime total of failed passes. While the
    streak holds at `monitor_failure_limit` or above, `degraded` is true
    and `reason` carries the streak's first failure as `<class>:<stage>`.
    """

    degraded: bool = False
    reason: str | None = None
    core_consecutive: int = 0
    core_failures: int = 0
    event_loop_lag_s: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    event_loop_lag_high_water_s: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    stages: dict[str, MonitoringStageOut] = Field(default_factory=dict)


class DecodeFloorOut(BaseModel):
    """Live decode capacity and floor recovery."""

    min_decode: int = 1
    live_decode: int = 0
    below_floor: bool = False
    restoration_moves: int = 0


class ControllerDecisionOut(BaseModel):
    """Most recent proposed or applied controller split."""

    model_config = ConfigDict(extra="forbid")

    at: float
    prefill: int
    decode: int
    by: str
    reason: str
    result: str
    applied: bool
    current_prefill: int | None = None
    current_decode: int | None = None
    prefill_work: float | None = None
    decode_work: float | None = None
    projected_ttft_ratio: float | None = None
    projected_tpot_ratio: float | None = None
    objective: float | None = None
    objective_delta: float | None = None
    decode_tokens_per_engine: float | None = None
    decode_slo_capacity_tokens: float | None = None
    decode_kv_capacity_tokens: float | None = None
    decode_request_limit: int | None = None
    decode_requests_per_engine: float | None = None
    pending_decode_requests: int | None = None
    pending_decode_tokens: float | None = None
    decode_profile_covered: bool | None = None
    decode_correction: float | None = None
    arrivals: int | None = None
    output_observations: int | None = None
    demand_complete: bool | None = None
    decision_basis: str | None = None
    observed_prefill_ratio: float | None = None
    recovery_prefill_ratio: float | None = None
    observed_decode_ratio: float | None = None
    eligibility_rule: str | None = None
    confirmations: int | None = None
    required_confirmations: int | None = None
    trigger_rid: str | None = None
    projected_ttft_s: float | None = None
    ttft_slo_s: float | None = None
    trigger_projected_ttft_ratio: float | None = None
    resident_prefill_s: float | None = None
    queued_prefill_s: float | None = None
    waiting_prefill: int | None = None
    initial_projected_ttft_s: float | None = None
    urgent_signals: int | None = None
    event_to_evaluation_s: float | None = None
    candidate_projected_ttft_s: float | None = None
    candidate_projected_ttft_ratio: float | None = None
    projected_ttft_improvement_s: float | None = None
    decode_capacity_safe: bool | None = None
    role_floors_safe: bool | None = None
    source_pressure_safe: bool | None = None
    evidence_span_s: float | None = None
    evidence_arrivals: int | None = None
    evidence_required_span_s: float | None = None
    evidence_required_arrivals: int | None = None
    evidence_max_span_s: float | None = None
    evidence_closed: bool | None = None
    risk_kind: str | None = None
    risk_age_s: float | None = None
    evidence_short_decode_engines: float | None = None
    evidence_long_decode_engines: float | None = None
    evidence_trend_ratio: float | None = None
    evidence_envelope_decode_engines: float | None = None
    evidence_blocked_gate: str | None = None


class ControlOut(BaseModel):
    """Controller mode, latest decision, and process-lifetime event totals."""

    advisory: bool = False
    last_decision: ControllerDecisionOut | None = None
    decisions: dict[str, int] = Field(default_factory=dict)
    flips: dict[str, int] = Field(default_factory=dict)
    flip_reversals: int = 0
    flips_refused: int = 0
    flip_inflight: dict[str, int] = Field(default_factory=dict)


class BelowFloorOut(BaseModel):
    """Current and cumulative prefill-floor breach state."""

    active: bool
    live_prefill: int
    since: float | None = None
    breaches: int = 0
    cumulative_s: float = 0.0


class HAOut(BaseModel):
    """Router lease and readiness state."""

    ready: bool
    standby: bool
    epoch: int
    holder: str
    blocked: str


class DrainIn(BaseModel):
    """One lifecycle drain action."""

    engines: list[str] = Field(default_factory=list)
    wave: bool = False
    deadline_s: float = 300.0


class ReadmitIn(BaseModel):
    """One lifecycle validation and readmission action."""

    engines: list[str] = Field(default_factory=list)
    wave: bool = False


class LifecycleWaveOut(BaseModel):
    """Whole-wave lifecycle state."""

    id: str
    active: bool
    ready_to_stop: bool


class LifecycleRouterOut(BaseModel):
    """Router ownership and traffic readiness during lifecycle work."""

    controls_fleet: bool
    ready: bool


class LifecycleEngineOut(BaseModel):
    """One engine's operator lifecycle state."""

    state: str
    draining: bool
    accepts_new: bool
    ready_to_stop: bool
    resident: ResidentOut
    deadline_at: float | None
    restart_required: bool
    wave_id: str
    old_process_start: float | None
    new_process_start: float | None
    checks: list[str]
    error: str


class LifecycleViewOut(BaseModel):
    """Lifecycle state embedded in the router state response."""

    engine_restart_policy: Literal["individual", "whole_wave"] = "individual"
    process_starts: dict[str, float] = Field(default_factory=dict)

    router: LifecycleRouterOut
    wave: LifecycleWaveOut
    engines: dict[str, LifecycleEngineOut]
    events: list[dict[str, Any]]


class HealthEngineOut(BaseModel):
    """Per-engine drift-window evidence accounting."""

    scored: int = 0
    # Windows closed after dropping gathered evidence too sparse to score.
    undersampled: int = 0
    # Seconds since the last scored window; null until one scores.
    last_scored_s_ago: float | None = None
    prefill_paused: bool = False
    prefill_pauses: int = 0


class EngineStreaksOut(BaseModel):
    """One engine's consecutive breaker failure streaks, keyed by class."""

    connection: int = 0
    timeout: int = 0
    overload: int = 0
    inference_status: int = 0
    kv_handoff: int = 0
    stream: int = 0
    liveness: int = 0


class VerifyingOut(BaseModel):
    """One engine with a breaker verification probe in flight."""

    iid: str
    kind: str


class BreakerOut(BaseModel):
    """Breaker failure accounting: per-class streaks and pending probes.

    `failures` holds each engine's consecutive failure streaks keyed by
    class (`connection`, `timeout`, `overload`, `inference_status`,
    `kv_handoff`, `stream`, and `liveness` for sweep misses). `verifying`
    lists the engines with a health or inference verification in flight.
    """

    failures: dict[str, EngineStreaksOut] = Field(default_factory=dict)
    verifying: list[VerifyingOut] = Field(default_factory=list)


class StateOut(BaseModel):
    """Live router and scheduler state."""

    model_config = ConfigDict(serialize_by_alias=True)

    schema_id: Literal["narwhal.state"] = Field(alias="schema")
    schema_version: Literal[1]
    journal_run: str = ""
    served: int
    offered: int = 0
    unsized_offered: int = 0
    expired: int = 0
    failed: int
    # Clients that disconnected before the work finished, separately from
    # engine and controller failures.
    cancelled: int = 0
    # Malformed client bodies rejected before admission, separately from
    # engine and controller failures.
    invalid_requests: int
    controller: str
    # How decode output is accounted: per-token identity or unmeasurable.
    token_accounting: str
    control: ControlOut = Field(default_factory=lambda: ControlOut())
    # Monitoring-stage failures and the degraded admission gate.
    monitoring: MonitoringOut = Field(default_factory=lambda: MonitoringOut())
    ha: HAOut
    lifecycle: LifecycleViewOut
    admission: AdmissionOut
    serving: ServingOut = Field(default_factory=ServingOut)
    # The engine HTTP pool policy: bounded data legs and reserved probes.
    http_pools: HttpPoolsOut
    pools: PoolsOut
    load: LoadOut
    thresholds: ThresholdsOut
    slo: SLOOut
    first_token_timeout_s: float
    resident: dict[str, ResidentOut]
    pinned: list[str] = []
    min_prefill: int = 1
    min_decode: int = 1
    below_floor: BelowFloorOut
    ejected: list[str]
    draining: list[str] = []
    # Probation adds the configured penalty to placement cost.
    probation: list[str] = []
    health: dict[str, HealthEngineOut] = {}
    unserved: int
    panic_bypasses: int
    # Observed SLO outcome evidence retained in fixed-width time buckets.
    attainment: AttainmentOut = Field(default_factory=lambda: AttainmentOut())
    # Consolidation evidence behind every D-to-P gate: span, samples, trend,
    # envelope, armed risk events, and the refusing gate.
    demand_history: dict[str, dict[str, int | float]] = Field(default_factory=dict)
    demand_evidence: DemandEvidenceOut = Field(default_factory=lambda: DemandEvidenceOut())
    flips_refused: list[FlipRefusedOut]
    flips: list[FlipOut]
    # Engines temporarily held out of placement after failure.
    quarantined: list[str] = []
    # Breaker failure accounting: per-engine per-class streaks and the
    # engines with a verification probe in flight.
    breaker: BreakerOut = Field(default_factory=BreakerOut)
    decode_floor: DecodeFloorOut
