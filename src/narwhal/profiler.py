"""Per-instance capability models, built once at cluster start (Arrow §5.2).

Prefill is quadratic in input length and decode is linear in batch tokens (Arrow §3.1).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


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
    """Least-squares `y = a x^2 + b x + c` over (x, y) samples."""
    if len(samples) < 3:
        raise ValueError("a quadratic needs at least three samples")
    sx = [sum(x**p for x, _ in samples) for p in range(5)]
    sy = [sum(y * x**p for x, y in samples) for p in range(3)]
    a = [
        [sx[4], sx[3], sx[2]],
        [sx[3], sx[2], sx[1]],
        [sx[2], sx[1], sx[0]],
    ]
    return tuple(_solve3(a, [sy[2], sy[1], sy[0]]))  # type: ignore[return-value]


def fit_linear(samples: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares `y = m x + c`."""
    if len(samples) < 2:
        raise ValueError("a line needs at least two samples")
    n = len(samples)
    sx = sum(x for x, _ in samples)
    sy = sum(y for _, y in samples)
    sxx = sum(x * x for x, _ in samples)
    sxy = sum(x * y for x, y in samples)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        raise ValueError("singular system: every sample shares one x")
    m = (n * sxy - sx * sy) / denom
    return m, (sy - m * sx) / n


@dataclass
class Profile:
    """One instance's measured prefill and decode capability.

    `ttft_*` give predicted prefill processing time in seconds for an input of
    N tokens. `tpot_*` give the per-token generation interval in seconds when
    the instance holds N tokens in its batch.
    """

    iid: str
    ttft_a: float
    ttft_b: float
    ttft_c: float
    tpot_slope: float
    tpot_intercept: float

    def prefill_time(self, input_len: int) -> float:
        """`T(r, i)` in §5.3's cost functions."""
        x = float(input_len)
        return max(0.0, self.ttft_a * x * x + self.ttft_b * x + self.ttft_c)

    def token_interval(self, batch_tokens: int) -> float:
        """Predicted seconds between output tokens at this batch size."""
        return max(0.0, self.tpot_slope * float(batch_tokens) + self.tpot_intercept)

    def max_tokens(self, tpot_slo_s: float) -> float:
        """`MT(i, SLO_TPOT)`: the most tokens holdable while still meeting TPOT.

        Inverts `token_interval`. The intercept is the floor, so an instance
        that misses the target at an empty batch has no headroom at any batch.
        """
        if self.tpot_intercept > tpot_slo_s:
            return 0.0
        if self.tpot_slope <= 0:
            return float("inf")
        return max(0.0, (tpot_slo_s - self.tpot_intercept) / self.tpot_slope)


class ProfileStore:
    """Profiles on disk, reusable across cluster restarts (Arrow §5.2)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_id: dict[str, Profile] = {}
        if path.exists():
            for raw in json.loads(path.read_text()):
                p = Profile(**raw)
                self._by_id[p.iid] = p

    def get(self, iid: str) -> Profile | None:
        """The profile for `iid`, or None until it is measured."""
        return self._by_id.get(iid)

    def put(self, profile: Profile) -> None:
        """Re-profiling one instance leaves the others untouched (Arrow §5.2)."""
        self._by_id[profile.iid] = profile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(p) for p in self._by_id.values()], indent=2))

    def any(self) -> Profile | None:
        """One profile, for a caller that prices a homogeneous fleet."""
        return next(iter(self._by_id.values()), None)

    def mean_prefill_time(self, input_len: int) -> float | None:
        """The fleet-mean prefill seconds for `input_len` tokens.

        On a homogeneous fleet this equals any single profile's answer,
        so callers that switch from `any()` keep their golden numbers;
        on a mixed fleet it prices offered work against what the fleet
        as a whole can do rather than whichever engine profiled first.
        """
        if not self._by_id:
            return None
        times = [p.prefill_time(input_len) for p in self._by_id.values()]
        return sum(times) / len(times)

    def mean_max_tokens(self, tpot_slo_s: float) -> float | None:
        """The fleet-mean decode ceiling at the TPOT target."""
        if not self._by_id:
            return None
        caps = [p.max_tokens(tpot_slo_s) for p in self._by_id.values()]
        return sum(caps) / len(caps)

    def __len__(self) -> int:
        return len(self._by_id)
