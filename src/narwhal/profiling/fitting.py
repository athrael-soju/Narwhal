"""Fit and cross-validate per-engine cost curves."""

from __future__ import annotations

import math


def _solve3(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting on a 3x3 system."""
    m = [[*row, rhs] for row, rhs in zip(a, b, strict=True)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("singular system: profiling samples are degenerate")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(3):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            for c in range(col, 4):
                m[r][c] -= f * m[col][c]
    return [m[i][3] / m[i][i] for i in range(3)]


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
            coefficients = _solve3(matrix, [rhs[i] if active[i] else 0.0 for i in range(3)])
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
    _solve3(gram, rhs)
    best = [0.0, 0.0, 0.0]
    best_error = sum(y * y for y in ys)
    for mask in range(1, 8):
        active = [bool(mask & (1 << i)) for i in range(3)]
        matrix = [
            [gram[i][j] if active[i] and active[j] else float(i == j) for j in range(3)]
            for i in range(3)
        ]
        coefficients = _solve3(matrix, [rhs[i] if active[i] else 0.0 for i in range(3)])
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
