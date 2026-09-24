"""Profile coefficients, measured domains, and capacity calculations."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

_FLOAT_FIELDS = (
    "ttft_a",
    "ttft_b",
    "ttft_c",
    "tpot_slope",
    "tpot_intercept",
    "tpot_request_slope",
    "decode_fit_mape",
    "decode_cv_mape",
)


_INT_FIELDS = (
    "kv_capacity_tokens",
    "decode_min_requests",
    "decode_max_requests",
    "decode_min_kv_tokens",
    "decode_max_kv_tokens",
)


_REQUIRED_FIELDS = ("iid", "ttft_a", "ttft_b", "ttft_c", "tpot_slope", "tpot_intercept")


_OPTIONAL_FLOAT_FIELDS = ("decode_fit_mape", "decode_cv_mape")


_DECODE_BOUNDS = (
    "decode_min_requests",
    "decode_max_requests",
    "decode_min_kv_tokens",
    "decode_max_kv_tokens",
)


_OPTIONAL_DEFAULTS: dict[str, Any] = {
    "generation_digest": None,
    "kv_capacity_tokens": None,
    "tpot_request_slope": 0.0,
    "decode_min_requests": None,
    "decode_max_requests": None,
    "decode_min_kv_tokens": None,
    "decode_max_kv_tokens": None,
    "decode_fit_mape": None,
    "decode_cv_mape": None,
}


_FIELD_NAMES = frozenset(_REQUIRED_FIELDS) | frozenset(_OPTIONAL_DEFAULTS)


def _check(raw: Mapping[str, Any], label: str) -> None:
    """Raise on the first contract violation in one profile row.

    `raw` maps field name to value; absent optional fields take their dataclass
    defaults. `label` names the engine when `iid` is valid, or the row's position otherwise.
    """
    where = f"profile {label}"
    iid = raw.get("iid")
    if not isinstance(iid, str) or not iid:
        raise ValueError(f"{where}: iid must be a nonempty string")
    generation = raw.get("generation_digest")
    if generation is not None and (
        not isinstance(generation, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", generation) is None
    ):
        raise ValueError(f"{where}: generation_digest must be a sha256 digest")
    values: dict[str, float] = {}
    for name in _FLOAT_FIELDS:
        value = raw.get(name, _OPTIONAL_DEFAULTS.get(name))
        if value is None and name in _OPTIONAL_FLOAT_FIELDS:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}: {name} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{where}: {name} must be finite")
        values[name] = float(value)
    for name in _INT_FIELDS:
        value = raw.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}: {name} must be an integer")
        if value < 1:
            raise ValueError(f"{where}: {name} must be positive")
        values[name] = float(value)
    for name in ("ttft_a", "ttft_b", "ttft_c", "tpot_intercept", "tpot_request_slope"):
        if values[name] < 0:
            raise ValueError(f"{where}: {name} must be nonnegative")
    if values["tpot_slope"] < 0:
        raise ValueError(f"{where}: tpot_slope must be nonnegative")
    # A measured flat decode plane is safe only inside an explicit request/KV domain.
    if values["tpot_slope"] == 0 and not all(
        name in values for name in ("decode_max_requests", "decode_max_kv_tokens")
    ):
        raise ValueError(f"{where}: zero tpot_slope requires measured decode bounds")
    for name in _OPTIONAL_FLOAT_FIELDS:
        if name in values and values[name] < 0:
            raise ValueError(f"{where}: {name} must be nonnegative")
    for lo, hi in (
        ("decode_min_requests", "decode_max_requests"),
        ("decode_min_kv_tokens", "decode_max_kv_tokens"),
    ):
        if lo in values and hi in values and values[lo] > values[hi]:
            raise ValueError(f"{where}: {lo} must not exceed {hi}")
    if (
        "kv_capacity_tokens" in values
        and "decode_max_kv_tokens" in values
        and values["kv_capacity_tokens"] < values["decode_max_kv_tokens"]
    ):
        raise ValueError(f"{where}: kv_capacity_tokens must be at least decode_max_kv_tokens")
    for name in (*_DECODE_BOUNDS, *_OPTIONAL_FLOAT_FIELDS):
        if name not in values:
            raise ValueError(f"{where}: {name} is required on a current profile")


@dataclass(frozen=True)
class Profile:
    """Per-engine latency fits: quadratic prefill time by input length and
    linear decode interval by resident KV tokens and active requests.
    """

    iid: str
    ttft_a: float
    ttft_b: float
    ttft_c: float
    tpot_slope: float
    tpot_intercept: float
    generation_digest: str | None = None
    kv_capacity_tokens: int | None = None
    tpot_request_slope: float = 0.0
    decode_min_requests: int | None = None
    decode_max_requests: int | None = None
    decode_min_kv_tokens: int | None = None
    decode_max_kv_tokens: int | None = None
    decode_fit_mape: float | None = None
    decode_cv_mape: float | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Check the row against the profile contract, naming the first violation."""
        label = self.iid if isinstance(self.iid, str) and self.iid else "<unnamed>"
        _check({f.name: getattr(self, f.name) for f in fields(self)}, label)

    def prefill_time(self, input_len: int) -> float:
        """Predict prefill time for an input length."""
        x = float(input_len)
        return max(0.0, self.ttft_a * x * x + self.ttft_b * x + self.ttft_c)

    def token_interval(self, batch_tokens: float, batch_requests: float = 0.0) -> float:
        """Predict one decode iteration from active requests and their KV tokens."""
        return max(
            0.0,
            self.tpot_slope * float(batch_tokens)
            + self.tpot_request_slope * float(batch_requests)
            + self.tpot_intercept,
        )

    def max_tokens(self, tpot_slo_s: float, batch_requests: float = 0.0) -> float:
        """Return the largest decode batch that meets `tpot_slo_s`.

        Inverts `token_interval` after charging the active-request term.
        """
        if self.decode_max_requests is not None and batch_requests > self.decode_max_requests:
            return 0.0
        fixed = self.tpot_intercept + self.tpot_request_slope * float(batch_requests)
        if fixed > tpot_slo_s:
            return 0.0
        capacity = (
            float("inf")
            if self.tpot_slope <= 0
            else max(0.0, (tpot_slo_s - fixed) / self.tpot_slope)
        )
        if self.decode_max_kv_tokens is not None:
            capacity = min(capacity, self.decode_max_kv_tokens)
        return capacity

    def covers_decode(self, batch_requests: float, batch_tokens: float) -> bool:
        """Return whether a decode point is inside the measured profile domain."""
        if batch_requests <= 0 or batch_tokens <= 0:
            return True
        bounds = (
            (self.decode_min_requests, self.decode_max_requests, batch_requests),
            (self.decode_min_kv_tokens, self.decode_max_kv_tokens, batch_tokens),
        )
        return all(lo is None or lo <= value for lo, _, value in bounds) and all(
            hi is None or value <= hi for _, hi, value in bounds
        )

    def decode_request_limit(self, context_tokens: float) -> int:
        """Return the concurrent decode-request limit for the context length.

        Return 0 for an invalid context or missing measured request bound.
        """
        measured = self.decode_max_requests
        if context_tokens <= 0 or measured is None or measured <= 0:
            return 0
        limits = [measured]
        token_limit = self.kv_capacity_tokens
        if self.decode_max_kv_tokens is not None:
            token_limit = (
                self.decode_max_kv_tokens
                if token_limit is None
                else min(token_limit, self.decode_max_kv_tokens)
            )
        if token_limit is not None:
            limits.append(max(1, int(token_limit / context_tokens)))
        return max(1, min(limits))

    def decode_rps(
        self,
        tpot_slo_s: float,
        context_tokens: float,
        output_tokens: float,
        *,
        correction: float = 1.0,
    ) -> float:
        """Return measured-domain request capacity for one decode engine."""
        if context_tokens <= 0 or output_tokens <= 0 or correction <= 0:
            return 0.0
        hi = self.decode_request_limit(context_tokens)
        if hi <= 0:
            return 0.0
        lo = 1
        if self.decode_min_requests is not None:
            lo = max(lo, self.decode_min_requests)
        if self.decode_min_kv_tokens is not None:
            lo = max(lo, math.ceil(self.decode_min_kv_tokens / context_tokens))
        if lo > hi:
            return 0.0
        budget = tpot_slo_s / correction
        increment = self.tpot_request_slope + self.tpot_slope * context_tokens
        if increment > 0:
            best = min(hi, math.floor((budget - self.tpot_intercept) / increment))
        else:
            best = hi
        if best < lo:
            return 0.0
        batch_tokens = best * context_tokens
        interval = self.token_interval(batch_tokens, best) * correction
        if interval <= 0 or interval > tpot_slo_s or not self.covers_decode(best, batch_tokens):
            return 0.0
        return best / (output_tokens * interval)


def decode_evidence_problems(
    profile: Profile,
    *,
    max_fit_mape: float,
    max_cv_mape: float,
) -> list[str]:
    """Name a profile's validation failures, returning an empty list when it passes.

    Every row needs a measured domain spanning both axes and fit and
    cross-validation errors inside the given limits.
    """
    iid = profile.iid
    problems = []
    if profile.decode_min_requests == profile.decode_max_requests:
        problems.append(
            f"{iid} decode request domain is a single point "
            f"({profile.decode_min_requests}); the sweep must span both axes independently"
        )
    if profile.decode_min_kv_tokens == profile.decode_max_kv_tokens:
        problems.append(
            f"{iid} decode KV token domain is a single point "
            f"({profile.decode_min_kv_tokens}); the sweep must span both axes independently"
        )
    if profile.decode_fit_mape is None:
        problems.append(f"{iid} carries no decode_fit_mape evidence")
    elif profile.decode_fit_mape > max_fit_mape:
        problems.append(
            f"{iid} decode_fit_mape {profile.decode_fit_mape:.4f} exceeds the "
            f"profile-validation limit {max_fit_mape:g}"
        )
    if profile.decode_cv_mape is None:
        problems.append(f"{iid} carries no decode_cv_mape evidence")
    elif profile.decode_cv_mape > max_cv_mape:
        problems.append(
            f"{iid} decode_cv_mape {profile.decode_cv_mape:.4f} exceeds the "
            f"profile-validation limit {max_cv_mape:g}"
        )
    return problems
