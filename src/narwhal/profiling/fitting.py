"""Fit and cross-validate per-engine cost curves."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

MAX_PREFILL_FIT_MAPE = 0.20
MAX_PREFILL_POINT_ERROR = 0.50
# Lengths each side of the block rule needs for a split fit.
SPLIT_REGIME_LENGTHS = 2
# Lengths a split fit needs: its four terms and two residual degrees of freedom.
SPLIT_MIN_LENGTHS = 6
# Share of the plain curve's error a split fit must stay below.
SPLIT_ERROR_RATIO = 0.5


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting on a square system."""
    n = len(b)
    m = [[*row, rhs] for row, rhs in zip(a, b, strict=True)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("singular system: profiling samples are degenerate")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_quadratic(samples: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Fit `y = a x^2 + b x + c` with nonnegative coefficients."""
    if len(samples) < 3:
        raise ValueError("a quadratic needs at least three samples")
    if any(not math.isfinite(v) or v < 0 for sample in samples for v in sample):
        raise ValueError("prefill samples must be finite and nonnegative")
    if len({x for x, _ in samples}) < 3:
        raise ValueError("singular system: profiling needs three distinct input lengths")
    scale = max(x for x, _ in samples)
    scaled = [(x / scale, y) for x, y in samples]
    sx = [sum(x**p for x, _ in scaled) for p in range(5)]
    sy = [sum(y * x**p for x, y in scaled) for p in range(3)]
    gram = [
        [sx[4], sx[3], sx[2]],
        [sx[3], sx[2], sx[1]],
        [sx[2], sx[1], sx[0]],
    ]
    rhs = [sy[2], sy[1], sy[0]]
    best = [0.0, 0.0, 0.0]
    best_error = sum(y * y for _, y in scaled)
    # The constrained optimum lies on one of eight faces. Refit each face;
    # clipping an unconstrained coefficient would leave the others misfitted.
    for mask in range(1, 8):
        active = [bool(mask & (1 << i)) for i in range(3)]
        matrix = [
            [gram[i][j] if active[i] and active[j] else float(i == j) for j in range(3)]
            for i in range(3)
        ]
        try:
            coefficients = _solve(matrix, [rhs[i] if active[i] else 0.0 for i in range(3)])
        except ValueError:
            continue
        if any(value < 0 for value in coefficients):
            continue
        a, b, c = coefficients
        error = sum((a * x * x + b * x + c - y) ** 2 for x, y in scaled)
        if error < best_error:
            best, best_error = coefficients, error
    a, b, c = best
    return a / scale / scale, b / scale, c


def splits_prefill(length: float, block_tokens: int | None) -> bool:
    """Return whether a prompt of `length` tokens ends inside a cache block past the first."""
    return block_tokens is not None and length > block_tokens and length % block_tokens != 0


def fit_prefill_samples(
    samples: list[tuple[float, float]],
    block_tokens: int | None = None,
) -> tuple[tuple[float, float, float, float | None], list[tuple[float, float]], float]:
    """Fit `a*n*n + b*n + c + split*s` to each input length's median; raise on a poor fit.

    `s` marks a prompt that ends inside a cache block past the first. `split` is None
    unless the sweep has `SPLIT_MIN_LENGTHS` lengths, both sides of that rule have
    `SPLIT_REGIME_LENGTHS` lengths, and the step brings the error below
    `SPLIT_ERROR_RATIO` of the plain curve's.
    """
    if any(not math.isfinite(value) or value < 0 for sample in samples for value in sample):
        raise ValueError("prefill samples must be finite and nonnegative")
    groups: dict[float, list[float]] = {}
    for length, elapsed in samples:
        groups.setdefault(length, []).append(elapsed)
    representatives = [
        (length, statistics.median(times)) for length, times in sorted(groups.items())
    ]
    flags = [splits_prefill(length, block_tokens) for length, _ in representatives]

    def point_errors(a: float, b: float, c: float, step: float) -> list[float]:
        return [
            abs(a * length * length + b * length + c + step * flag - elapsed) / max(elapsed, 1e-9)
            for (length, elapsed), flag in zip(representatives, flags, strict=True)
        ]

    a, b, c = fit_quadratic(representatives)
    split: float | None = None
    errors = point_errors(a, b, c, 0.0)
    if (
        len(representatives) >= SPLIT_MIN_LENGTHS
        and flags.count(True) >= SPLIT_REGIME_LENGTHS
        and flags.count(False) >= SPLIT_REGIME_LENGTHS
    ):
        split_rows = [
            (x * x, x, 1.0, float(flag))
            for (x, _), flag in zip(representatives, flags, strict=True)
        ]
        sa, sb, sc, step = _nonnegative_fit(split_rows, [y for _, y in representatives])
        stepped = point_errors(sa, sb, sc, step)
        if statistics.mean(stepped) < SPLIT_ERROR_RATIO * statistics.mean(errors):
            a, b, c, split, errors = sa, sb, sc, step, stepped
    mape = statistics.mean(errors)
    if mape > MAX_PREFILL_FIT_MAPE or max(errors) > MAX_PREFILL_POINT_ERROR:
        raise ValueError(
            f"prefill median fit error {mape:.1%}, worst point {max(errors):.1%}; "
            "inspect the retained per-length measurements before using this profile"
        )
    return (a, b, c, split), representatives, mape


def fit_decode_plane(samples: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    """Fit ``y = requests*r + kv_tokens*k + c`` with nonnegative coefficients."""
    if len(samples) < 3:
        raise ValueError("a decode plane needs at least three samples")
    if any(not math.isfinite(v) or v <= 0 for row in samples for v in row):
        raise ValueError("decode samples must be finite and positive")
    r_scale = max(row[0] for row in samples)
    k_scale = max(row[1] for row in samples)
    rows = [(r / r_scale, k / k_scale, 1.0) for r, k, _ in samples]
    ys = [y for _, _, y in samples]
    gram = [[sum(row[i] * row[j] for row in rows) for j in range(3)] for i in range(3)]
    rhs = [sum(row[i] * y for row, y in zip(rows, ys, strict=True)) for i in range(3)]
    # Reject unidentifiable axes even if a boundary fit could hide them.
    _solve(gram, rhs)
    best = [0.0, 0.0, 0.0]
    best_error = sum(y * y for y in ys)
    for mask in range(1, 8):
        active = [bool(mask & (1 << i)) for i in range(3)]
        matrix = [
            [gram[i][j] if active[i] and active[j] else float(i == j) for j in range(3)]
            for i in range(3)
        ]
        coefficients = _solve(matrix, [rhs[i] if active[i] else 0.0 for i in range(3)])
        if any(value < 0 for value in coefficients):
            continue
        error = sum(
            (sum(a * x for a, x in zip(coefficients, row, strict=True)) - y) ** 2
            for row, y in zip(rows, ys, strict=True)
        )
        if error < best_error:
            best, best_error = coefficients, error
    request_slope, kv_slope, intercept = best
    return kv_slope / k_scale, request_slope / r_scale, intercept


def decode_mape(
    samples: list[tuple[float, float, float]],
    coefficients: tuple[float, float, float],
) -> float:
    """Return mean absolute percentage error for a decode plane."""
    kv_slope, request_slope, intercept = coefficients
    errors = []
    for requests, kv_tokens, observed in samples:
        predicted = request_slope * requests + kv_slope * kv_tokens + intercept
        errors.append(abs(predicted - observed) / max(observed, 1e-9))
    return sum(errors) / len(errors) if errors else 0.0


def decode_cross_validation_mape(samples: list[tuple[float, float, float]]) -> float | None:
    """Fit around each decode point and score the point left out."""
    if len(samples) < 4:
        return None
    errors = []
    for index, sample in enumerate(samples):
        training = samples[:index] + samples[index + 1 :]
        try:
            coefficients = fit_decode_plane(training)
        except ValueError:
            return None
        errors.append(decode_mape([sample], coefficients))
    return sum(errors) / len(errors)


def _nonnegative_fit(rows: Sequence[Sequence[float]], ys: Sequence[float]) -> list[float]:
    """Fit `y = sum(w_i f_i)` with nonnegative weights by checking every face."""
    n = len(rows[0])
    scales = [max(abs(row[i]) for row in rows) or 1.0 for i in range(n)]
    scaled = [[row[i] / scales[i] for i in range(n)] for row in rows]
    gram = [[sum(r[i] * r[j] for r in scaled) for j in range(n)] for i in range(n)]
    rhs = [sum(r[i] * y for r, y in zip(scaled, ys, strict=True)) for i in range(n)]
    best = [0.0] * n
    best_error = sum(y * y for y in ys)
    for mask in range(1, 1 << n):
        active = [bool(mask & (1 << i)) for i in range(n)]
        matrix = [
            [gram[i][j] if active[i] and active[j] else float(i == j) for j in range(n)]
            for i in range(n)
        ]
        try:
            weights = _solve(matrix, [rhs[i] if active[i] else 0.0 for i in range(n)])
        except ValueError:
            continue
        if any(value < 0 for value in weights):
            continue
        error = sum(
            (sum(w * v for w, v in zip(weights, r, strict=True)) - y) ** 2
            for r, y in zip(scaled, ys, strict=True)
        )
        if error < best_error:
            best, best_error = weights, error
    return [w / s for w, s in zip(best, scales, strict=True)]


def _cached_features(prefix: float, suffix: float) -> tuple[float, float, float, float]:
    # Terms for a, b, c, d: suffix attention, suffix tokens, constant, prefix read.
    return (2 * prefix * suffix + suffix * suffix, suffix, 1.0, prefix)


# A warm fit needs more cases than it has terms.
CACHED_FIT_MIN_CASES = len(_cached_features(1.0, 1.0)) + 1


def cached_fit_possible(cases: Sequence[tuple[float, float]]) -> bool:
    """Return whether (prefix, suffix) cases can form a warm prefill fit."""
    distinct = set(cases)
    return (
        len({p for p, _ in distinct}) >= 2
        and len({s for _, s in distinct}) >= 2
        and len(distinct) >= CACHED_FIT_MIN_CASES
    )


def _fit_cached_groups(
    groups: list[tuple[float, float, float]],
) -> tuple[float, float, float, float]:
    a, b, c, d = _nonnegative_fit(
        [_cached_features(p, s) for p, s, _ in groups], [y for _, _, y in groups]
    )
    return a, b, c, d


def fit_cached_prefill(
    samples: list[tuple[float, float, float]],
    split: float | None = None,
    block_tokens: int | None = None,
) -> tuple[tuple[float, float, float, float], list[tuple[float, float, float]], float]:
    """Fit warm prefill `c + b*S + d*P + a*(2*P*S + S*S)` from (prefix, suffix, seconds) samples.

    Suffixes that split carry the cold `split`. Returns the coefficients, the per-case
    medians and the leave-one-case-out error.
    """
    if any(not math.isfinite(v) or v < 0 for sample in samples for v in sample):
        raise ValueError("cached prefill samples must be finite and nonnegative")
    grouped: dict[tuple[float, float], list[float]] = {}
    for prefix, suffix, elapsed in samples:
        if prefix <= 0 or suffix <= 0:
            raise ValueError("cached prefill samples need a cached prefix and an uncached suffix")
        grouped.setdefault((prefix, suffix), []).append(elapsed)
    groups = [(p, s, statistics.median(times)) for (p, s), times in sorted(grouped.items())]
    if not cached_fit_possible([(p, s) for p, s, _ in groups]):
        raise ValueError(
            "a cached prefill fit needs two prefix lengths, two suffix lengths and five cases"
        )

    def step(suffix: float) -> float:
        return split if split and splits_prefill(suffix, block_tokens) else 0.0

    stepless = [(p, s, y - step(s)) for p, s, y in groups]
    coefficients = _fit_cached_groups(stepless)
    errors = []
    for index, (prefix, suffix, observed) in enumerate(groups):
        weights = _fit_cached_groups(stepless[:index] + stepless[index + 1 :])
        predicted = step(suffix) + sum(
            w * f for w, f in zip(weights, _cached_features(prefix, suffix), strict=True)
        )
        errors.append(abs(predicted - observed) / max(observed, 1e-9))
    return coefficients, groups, sum(errors) / len(errors)
