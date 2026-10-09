"""Profile coefficients, measured domains, and capacity calculations."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields
from typing import Any

from .fitting import cached_features, split_step

_FLOAT_FIELDS = (
    "ttft_a",
    "ttft_b",
    "ttft_c",
    "tpot_slope",
    "tpot_intercept",
    "tpot_request_slope",
    "decode_fit_mape",
    "decode_cv_mape",
    "colocated_prefill_rps",
    "colocated_decode_rps",
    "ttft_split",
    "cached_ttft_a",
    "cached_ttft_b",
    "cached_ttft_c",
    "cached_ttft_d",
    "cached_cv_mape",
)


_INT_FIELDS = (
    "kv_capacity_tokens",
    "decode_min_requests",
    "decode_max_requests",
    "decode_min_kv_tokens",
    "decode_max_kv_tokens",
    "prefill_min_tokens",
    "prefill_max_tokens",
    "decode_min_output_tokens",
    "decode_max_output_tokens",
    "colocated_prefill_engines",
    "colocated_decode_engines",
    "ttft_block_tokens",
    "cached_min_prefix_tokens",
    "cached_max_prefix_tokens",
    "cached_min_suffix_tokens",
    "cached_max_suffix_tokens",
)


_REQUIRED_FIELDS = ("iid", "ttft_a", "ttft_b", "ttft_c", "tpot_slope", "tpot_intercept")


# Fields a warm prefill fit sets together.
CACHED_PROFILE_FIELDS = (
    "cached_ttft_a",
    "cached_ttft_b",
    "cached_ttft_c",
    "cached_ttft_d",
    "cached_cv_mape",
    "cached_min_prefix_tokens",
    "cached_max_prefix_tokens",
    "cached_min_suffix_tokens",
    "cached_max_suffix_tokens",
)


_DECODE_BOUNDS = (
    "decode_min_requests",
    "decode_max_requests",
    "decode_min_kv_tokens",
    "decode_max_kv_tokens",
)


def _check(raw: Mapping[str, Any], label: str) -> None:
    """Raise on the first contract violation in one profile row.

    Absent optional fields in `raw` take their dataclass defaults.
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
    # A measured flat decode plane is safe only inside explicit request and KV bounds.
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
        ("prefill_min_tokens", "prefill_max_tokens"),
        ("decode_min_output_tokens", "decode_max_output_tokens"),
        ("cached_min_prefix_tokens", "cached_max_prefix_tokens"),
        ("cached_min_suffix_tokens", "cached_max_suffix_tokens"),
    ):
        if lo in values and hi in values and values[lo] > values[hi]:
            raise ValueError(f"{where}: {lo} must not exceed {hi}")
    if (
        "kv_capacity_tokens" in values
        and "decode_max_kv_tokens" in values
        and values["kv_capacity_tokens"] < values["decode_max_kv_tokens"]
    ):
        raise ValueError(f"{where}: kv_capacity_tokens must be at least decode_max_kv_tokens")
    for name in (*_DECODE_BOUNDS, "decode_fit_mape", "decode_cv_mape"):
        if name not in values:
            raise ValueError(f"{where}: {name} is required on a current profile")
    if ("ttft_block_tokens" in values) != ("ttft_split" in values):
        raise ValueError(f"{where}: ttft_block_tokens and ttft_split go together")
    cached = [name for name in CACHED_PROFILE_FIELDS if name in values]
    if cached and len(cached) != len(CACHED_PROFILE_FIELDS):
        missing = sorted(set(CACHED_PROFILE_FIELDS) - set(cached))
        raise ValueError(f"{where}: a cached prefill fit requires {', '.join(missing)}")
    group = raw.get("colocated_group")
    role = raw.get("colocated_target_role")
    mix = (raw.get("colocated_prefill_engines"), raw.get("colocated_decode_engines"))
    loads = (raw.get("colocated_prefill_rps"), raw.get("colocated_decode_rps"))
    if group is not None:
        if not isinstance(group, str) or not group:
            raise ValueError(f"{where}: colocated_group must be a nonempty string")
        if role not in ("prefill", "decode"):
            raise ValueError(f"{where}: colocated_target_role must be prefill or decode")
        if mix[0] is None or mix[1] is None or any(value is None for value in loads):
            raise ValueError(f"{where}: colocated role mix and neighbour load are required")
        if mix[0] + mix[1] < 2:
            raise ValueError(f"{where}: colocated role mix and neighbour load are required")
    elif role is not None or any(value is not None for value in (*mix, *loads)):
        raise ValueError(f"{where}: colocated role mix requires colocated_group")


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
    prefill_min_tokens: int | None = None
    prefill_max_tokens: int | None = None
    decode_min_output_tokens: int | None = None
    decode_max_output_tokens: int | None = None
    colocated_group: str | None = None
    colocated_target_role: str | None = None
    colocated_prefill_engines: int | None = None
    colocated_decode_engines: int | None = None
    colocated_prefill_rps: float | None = None
    colocated_decode_rps: float | None = None
    # Cache block size and the extra prefill step for a prompt ending past the first block.
    ttft_block_tokens: int | None = None
    ttft_split: float | None = None
    # Warm prefill c + b*S + d*P + a*(2*P*S + S*S) for P cached and S uncached tokens.
    cached_ttft_a: float | None = None
    cached_ttft_b: float | None = None
    cached_ttft_c: float | None = None
    cached_ttft_d: float | None = None
    cached_cv_mape: float | None = None
    cached_min_prefix_tokens: int | None = None
    cached_max_prefix_tokens: int | None = None
    cached_min_suffix_tokens: int | None = None
    cached_max_suffix_tokens: int | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Check the row against the profile contract, naming the first violation."""
        label = self.iid if isinstance(self.iid, str) and self.iid else "<unnamed>"
        _check({f.name: getattr(self, f.name) for f in fields(self)}, label)

    def prefill_time(self, input_len: int) -> float:
        """Predict prefill time for an input length.

        A prompt that ends inside a block past the first adds `ttft_split`. A prompt
        shorter than the measured sweep is priced at the sweep's shortest length, and one
        longer than the sweep is priced by extending the fit.
        """
        tokens = max(input_len, self.prefill_min_tokens or 0)
        x = float(tokens)
        return max(
            0.0, self.ttft_a * x * x + self.ttft_b * x + self.ttft_c + self._split_step(tokens)
        )

    def _split_step(self, tokens: int) -> float:
        """Return `ttft_split` when `tokens` computed tokens end inside a block past the first."""
        return split_step(tokens, self.ttft_split, self.ttft_block_tokens)

    def cached_prefill_time(self, prefix_tokens: int, suffix_tokens: int) -> float | None:
        """Predict prefill time with `prefix_tokens` cached; None outside the warm fit's domain.

        A suffix that ends inside a block past its first adds `ttft_split`.
        """
        if prefix_tokens <= 0:
            return self.prefill_time(suffix_tokens)
        if (
            self.cached_ttft_a is None
            or self.cached_ttft_b is None
            or self.cached_ttft_c is None
            or self.cached_ttft_d is None
            or self.cached_min_prefix_tokens is None
            or self.cached_max_prefix_tokens is None
            or self.cached_min_suffix_tokens is None
            or self.cached_max_suffix_tokens is None
            or not self.cached_min_prefix_tokens <= prefix_tokens <= self.cached_max_prefix_tokens
            or not self.cached_min_suffix_tokens <= suffix_tokens <= self.cached_max_suffix_tokens
        ):
            return None
        attention, suffix, _, prefix = cached_features(float(prefix_tokens), float(suffix_tokens))
        return max(
            0.0,
            self.cached_ttft_a * attention
            + self.cached_ttft_b * suffix
            + self.cached_ttft_d * prefix
            + self.cached_ttft_c
            + self._split_step(suffix_tokens),
        )

    def covers_prefill(self, input_len: int) -> bool:
        """Return whether a prompt is no longer than the longest prompt in the measured sweep."""
        return self.prefill_max_tokens is None or input_len <= self.prefill_max_tokens

    def covers_output(self, output_len: float) -> bool:
        """Return whether an output is at least as long as the shortest measured decode output."""
        return self.decode_min_output_tokens is None or output_len >= self.decode_min_output_tokens

    def token_interval(self, batch_tokens: float, batch_requests: float = 0.0) -> float:
        """Predict the decode token interval for active requests and their KV tokens."""
        return max(
            0.0,
            self.tpot_slope * float(batch_tokens)
            + self.tpot_request_slope * float(batch_requests)
            + self.tpot_intercept,
        )

    def max_tokens(self, tpot_slo_s: float, batch_requests: float = 0.0) -> float:
        """Return the largest decode batch, in KV tokens, that meets `tpot_slo_s`."""
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
        """Return whether a decode point is within the measured request and KV token maxima."""
        requests = self.decode_max_requests is None or batch_requests <= self.decode_max_requests
        tokens = self.decode_max_kv_tokens is None or batch_tokens <= self.decode_max_kv_tokens
        return requests and tokens

    @property
    def decode_token_limit(self) -> int | None:
        """Return the smaller of the measured decode KV domain and the physical KV capacity."""
        bounds = [b for b in (self.kv_capacity_tokens, self.decode_max_kv_tokens) if b is not None]
        return min(bounds) if bounds else None

    def decode_request_limit(self, context_tokens: float) -> int:
        """Return the concurrent decode-request limit at the context length, 0 when unmeasured."""
        measured = self.decode_max_requests
        if context_tokens <= 0 or measured is None or measured <= 0:
            return 0
        limits = [measured]
        token_limit = self.decode_token_limit
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
        """Return one decode engine's request capacity inside its measured domain.

        A batch below the smallest measured batch takes that batch's token interval.
        """
        if (
            context_tokens <= 0
            or output_tokens <= 0
            or correction <= 0
            or not self.covers_output(output_tokens)
        ):
            return 0.0
        hi = self.decode_request_limit(context_tokens)
        if hi <= 0:
            return 0.0
        budget = tpot_slo_s / correction
        increment = self.tpot_request_slope + self.tpot_slope * context_tokens
        if increment > 0:
            best = min(hi, math.floor((budget - self.tpot_intercept) / increment))
        else:
            best = hi
        if best < 1:
            return 0.0
        batch_tokens = best * context_tokens
        if not self.covers_decode(best, batch_tokens):
            return 0.0
        interval = (
            self.token_interval(
                max(batch_tokens, self.decode_min_kv_tokens or 0),
                max(best, self.decode_min_requests or 1),
            )
            * correction
        )
        if interval <= 0 or interval > tpot_slo_s:
            return 0.0
        return best / (output_tokens * interval)


_OPTIONAL_DEFAULTS: dict[str, Any] = {
    f.name: f.default for f in fields(Profile) if f.default is not MISSING
}
_OPTIONAL_FLOAT_FIELDS = tuple(
    name for name in _FLOAT_FIELDS if _OPTIONAL_DEFAULTS.get(name, MISSING) is None
)
_FIELD_NAMES = frozenset(f.name for f in fields(Profile))


def decode_evidence_problems(
    profile: Profile,
    *,
    max_fit_mape: float,
    max_cv_mape: float,
) -> list[str]:
    """Return a profile's validation failures, or an empty list when it passes.

    A passing row has a measured domain that spans both axes, and fit and
    cross-validation errors within the given limits.
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
