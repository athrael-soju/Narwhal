"""Response shapes, pinned as Pydantic models.

The served /docs page renders these, and a field added to `state()` without
a model update fails the schema test instead of silently vanishing from the
response: `response_model` filters unknown fields. Every harness that polls
/arrow/state (the W&B watcher, the walk scorers) parses these shapes.
"""

from __future__ import annotations

from pydantic import BaseModel


class HealthOut(BaseModel):
    """GET /health."""

    status: str
    instances: int


class ModelOut(BaseModel):
    """One entry of /v1/models."""

    id: str
    object: str
    owned_by: str


class ModelsOut(BaseModel):
    """GET /v1/models: the OpenAI list shape."""

    object: str
    data: list[ModelOut]


class AdmissionOut(BaseModel):
    """In-flight against the limit, and the two refusal counts.

    `rejected` is pool exhaustion, `refused` is the cost model pricing every
    landing over the TTFT budget before the request dispatches.
    """

    inflight: int
    limit: int
    rejected: int
    refused: int


class TenantOut(BaseModel):
    """One customer's honest slice."""

    served: int
    failed: int
    rejected: int
    inflight: int
    weight: float
    cap: int


class PoolsOut(BaseModel):
    """Instance ids by current role."""

    prefill: list[str]
    decode: list[str]


class LoadOut(BaseModel):
    """Arrow §5.5 pool loads, SLO-relative."""

    prefill: float
    decode: float


class ThresholdsOut(BaseModel):
    """The thresholds the run is holding."""

    expand: float
    shrink: float
    cooldown_s: float
    sustained_intervals: int
    dwell_s: float
    panic_ratio: float


class SLOOut(BaseModel):
    """The two targets everything is priced against."""

    ttft_s: float
    tpot_s: float


class ResidentOut(BaseModel):
    """Per-instance in-flight counts."""

    prefill: int
    decode: int


class FlipOut(BaseModel):
    """One role change and what it was carrying."""

    at: float
    iid: str
    to: str
    by: str
    prefill_inflight: int
    decode_inflight: int
    drained_s: float | None


class FlipRefusedOut(BaseModel):
    """A flip that was refused, and why."""

    at: float
    to: str
    why: str


class PoAOut(BaseModel):
    """The observation-only efficiency gauge and regime."""

    regret: float | None
    regime: str
    samples: int


class StateOut(BaseModel):
    """The live scheduler picture: the record that an adaptive run actuated."""

    served: int
    failed: int
    controller: str
    admission: AdmissionOut
    tenants: dict[str, TenantOut] = {}
    pools: PoolsOut
    load: LoadOut
    thresholds: ThresholdsOut
    slo: SLOOut
    first_token_timeout_s: float
    resident: dict[str, ResidentOut]
    # Config-pinned roles and the prefill floor: the standing answer to
    # "why does this engine never flip". Empty and 1 when the feature is off.
    pinned: list[str] = []
    min_prefill: int = 1
    ejected: list[str]
    # Engines the drift tracker has deprioritized; healthy enough to
    # serve, slow enough to stop offering new work to.
    probation: list[str] = []
    unserved: int
    panic_bypasses: int
    poa: PoAOut
    flips_refused: list[FlipRefusedOut]
    flips: list[FlipOut]
