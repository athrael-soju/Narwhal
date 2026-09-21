"""Detect sustained per-engine decode-latency drift.

Windows are time-delimited. The first observation of an engine opens its
window, and the first tick that finds the window at least `window_s` old
closes it. Each monitoring pass contributes at most one residual per engine.
The config bounds `min_samples` by the nominal monitoring cadence. Slow
passes or missing observations can leave a window below that bound; such
windows close without a health verdict.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("narwhal.health")


@dataclass
class _EngineDrift:
    """Residual windows and verdict state for one engine."""

    # Cap retained evidence if the monitoring loop stalls.
    residuals: deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    window_since: float = 0.0
    score: float | None = None
    over: int = 0
    under: int = 0
    on_probation: bool = False
    # Per-engine baselines absorb stable hardware and batch-shape offsets.
    baseline: float | None = None
    # Count closed windows that contained too few observations to score.
    undersampled_windows: int = 0
    scored_windows: int = 0
    last_scored_at: float | None = None
    prefill_paused: bool = False
    prefill_pauses: int = 0


# Baseline adaptation stays slower than the drift window.
_BASELINE_ALPHA = 0.25


class DriftTracker:
    """Convert latency residual windows into health verdicts.

    The scheduler applies returned verdicts because it owns ejection guards and
    readmission probes. `clock` supports deterministic replay.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window_s: float = 30.0,
        band: float = 2.0,
        min_samples: int = 3,
        probation_windows: int = 3,
        evict_windows: int = 5,
        recovery_windows: int = 3,
        penalty_s: float = 1.5,
        relative_band: float = 1.5,
    ) -> None:
        self._clock = clock
        self.window_s = window_s
        self.band = band
        self.min_samples = min_samples
        self.probation_windows = probation_windows
        self.evict_windows = evict_windows
        self.recovery_windows = recovery_windows
        # Placement converts this seconds penalty for decode cost comparisons.
        self.penalty_s = penalty_s
        self.relative_band = relative_band
        self._engines: dict[str, _EngineDrift] = {}

    def note(self, iid: str, residual: float) -> None:
        """Record one decode observed-to-predicted ratio."""
        engine = self._engines.get(iid)
        if engine is None:
            engine = _EngineDrift(window_since=self._clock())
            self._engines[iid] = engine
        if engine.prefill_paused:
            engine.window_since = self._clock()
            engine.prefill_paused = False
        engine.residuals.append(residual)

    def pause_for_prefill(self, iid: str) -> None:
        """Start a fresh drift window while preserving the baseline and probation."""
        engine = self._engines.setdefault(iid, _EngineDrift(window_since=self._clock()))
        if not engine.prefill_paused:
            engine.prefill_pauses += 1
        engine.prefill_paused = True
        engine.residuals.clear()
        engine.window_since = self._clock()
        engine.over = engine.under = 0

    def probation_set(self) -> set[str]:
        """Return engines currently penalized by placement."""
        return {iid for iid, e in self._engines.items() if e.on_probation}

    def evicted(self, iid: str) -> None:
        """Drop drift history after the scheduler confirms ejection."""
        self._engines.pop(iid, None)

    def score(self, iid: str) -> float | None:
        """Return the engine's latest mean residual."""
        engine = self._engines.get(iid)
        return engine.score if engine else None

    def window_stats(self) -> dict[str, dict[str, float | int | None]]:
        """Return per-engine window counts and the age of the last scored window.

        The undersampled count is the record of evidence dropped without a
        verdict. Both counts grow for the record's lifetime; a confirmed
        ejection drops the record, so a readmitted engine restarts at zero.
        """
        now = self._clock()
        return {
            iid: {
                "scored": e.scored_windows,
                "undersampled": e.undersampled_windows,
                "last_scored_s_ago": (
                    now - e.last_scored_at if e.last_scored_at is not None else None
                ),
                "prefill_paused": e.prefill_paused,
                "prefill_pauses": e.prefill_pauses,
            }
            for iid, e in self._engines.items()
        }

    def tick(self) -> list[tuple[str, str]]:
        """Close due windows and return `(verdict, iid)` pairs.

        Supported verdicts are `probation`, `evict`, and `recover`. Sparse
        windows produce no verdict; a due window holding evidence closes with
        that evidence dropped and the engine's undersampled count raised,
        while an empty window closes without counting.
        """
        now = self._clock()
        scored: dict[str, float] = {}
        for iid, e in self._engines.items():
            if now - e.window_since < self.window_s:
                continue
            e.window_since = now
            if len(e.residuals) < self.min_samples:
                # The window is time-delimited: holding the residuals would
                # merge unlike latencies into the next verdict. Only a window
                # that held evidence on close drops any.
                if e.residuals:
                    e.undersampled_windows += 1
                    e.residuals.clear()
                continue
            e.score = sum(e.residuals) / len(e.residuals)
            e.residuals.clear()
            e.scored_windows += 1
            e.last_scored_at = now
            scored[iid] = e.score
        # Three scored engines are required for a majority peer veto.
        quorum_size = len(scored)

        verdicts: list[tuple[str, str]] = []
        for iid, score in scored.items():
            e = self._engines[iid]
            # Scale the threshold from the engine's own healthy history.
            personal_band = self.band * max(e.baseline or 1.0, 1.0)
            past_band = score > personal_band
            # Suppress verdicts when a majority of peers cross their own bands.
            others_over = 0
            for other_id, other_score in scored.items():
                if other_id == iid:
                    continue
                other = self._engines[other_id]
                if other_score > self.band * max(other.baseline or 1.0, 1.0):
                    others_over += 1
            if self.relative_band <= 0:
                majority_veto = False
            else:
                majority_veto = quorum_size >= 3 and others_over * 2 >= quorum_size - 1
                if majority_veto:
                    others = sorted(v for k, v in scored.items() if k != iid)
                    median_peer = others[len(others) // 2]
                    # Preserve an outlier verdict during a fleet-wide surge.
                    if median_peer > 0 and score > self.relative_band * median_peer:
                        majority_veto = False
            if e.baseline is None:
                # Seed a cold channel from peer baselines when available.
                peers = sorted(
                    b
                    for other_id in scored
                    if other_id != iid and (b := self._engines[other_id].baseline) is not None
                )
                e.baseline = peers[len(peers) // 2] if peers else max(score, 1.0)
            elif not e.on_probation and not past_band:
                e.baseline = _BASELINE_ALPHA * score + (1 - _BASELINE_ALPHA) * e.baseline
            if past_band and not majority_veto:
                e.over += 1
                e.under = 0
                if e.on_probation and e.over >= self.evict_windows:
                    log.warning(
                        "health: evict %s - drift %.1fx over its own %.1fx band, %d windows",
                        iid,
                        score,
                        personal_band,
                        e.over,
                    )
                    # Keep probation until the scheduler confirms ejection.
                    verdicts.append(("evict", iid))
                elif not e.on_probation and e.over >= self.probation_windows:
                    log.warning(
                        "health: probation %s - drift %.1fx over its own %.1fx band, %d windows",
                        iid,
                        score,
                        personal_band,
                        e.over,
                    )
                    e.on_probation = True
                    verdicts.append(("probation", iid))
            else:
                if past_band:
                    # Hold both counters through fleet-wide surge windows.
                    continue
                e.over = 0
                e.under += 1
                if e.on_probation and e.under >= self.recovery_windows:
                    log.info(
                        "health: %s recovered - %.1fx within its own %.1fx band for %d windows",
                        iid,
                        score,
                        personal_band,
                        e.under,
                    )
                    e.on_probation = False
                    e.over = 0
                    verdicts.append(("recover", iid))
        return verdicts
