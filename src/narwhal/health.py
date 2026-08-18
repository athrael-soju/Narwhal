"""Predictive health: per-engine drift tracking against its own profile.

Reactive self-healing answers failures after they arrive: consecutive failed
legs eject, health probes readmit (`GlobalScheduler.record_failure`).
Engines carry residual state for 11-16 minutes after sustained load, so the
degradation is visible in latency drift minutes before it becomes failures.
This tracker is the early instrument.

Residuals are per-engine observed/predicted ratios on two channels:

- TTFT, reported by the server per completed prefill (`note`, observed
  placement latency against what Algorithm 1's own cost predicted for it).
- Decode, sampled once per monitoring pass from the interval readings the
  monitor already publishes (observed inter-token gap against the engine's
  profiled curve at the batch it actually carries, so a full healthy engine
  never drifts).

Values under 1.0 mean faster than profiled; only the over-band direction
drifts. Verdicts are windowed so a busy or idle minute cannot convict: a
window needs `min_samples` samples to speak; an engine over `band` for
`probation_windows` consecutive scored windows goes on probation (a placement
cost penalty drains new work from it); probation held past `evict_windows`
produces an "evict" verdict the scheduler executes through the same ejection
the reactive path uses — never the last live instance, probes readmit — and
`recovery_windows` consecutive under-band windows clear probation, which is
the recover-in-place path.

The verdict band itself is per-engine history, learned: `band` times the
engine's own trailing non-over reading (floored at 1.0x), since hardware,
drivers and batches all carry constant offsets from the static profile. The
surge guard sits across engines: an engine speaks only while a majority of
the other scored engines stay within their *own* bands — if everyone rises
together it is a capacity story, if everyone runs quiet and one drifts, that
one stands out. A one-scored window has no quorum and keeps the personal-band
reading alone.
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
    """One engine's open windows and consecutive-window verdict counters."""

    # Bounded: a tracker whose tick is starved (a stalled loop) must cap at
    # a window's worth of evidence, never grow with uptime.
    residuals: deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    # The TTFT channel is informational only: occupancy terms it would need
    # to equalize are the scheduler's own resident work, and pricing them
    # here would double-count. It still accumulates for observability.
    ttft_residuals: deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    window_since: float = 0.0
    score: float | None = None
    over: int = 0
    under: int = 0
    on_probation: bool = False
    # Trailing own-history baseline, learned from non-over windows: hardware,
    # drivers and batches all carry constant offsets from the static profile
    # (a decode engine in the simulator reads 2.4x its profile from chunking
    # alone), so "against its own profile" has to mean against what this
    # engine reads healthily, not against the measurement's absolute scale.
    baseline: float | None = None


# How fast the own-history baseline adopts non-over windows. Slow enough to
# not chase the drift the loop exists to catch, fast enough to settle after
# a genuinely quiet engine turns busy.
_BASELINE_ALPHA = 0.25


class DriftTracker:
    """Windowed residuals in, probation/evict/recover verdicts out.

    The tracker owns no effectors: `tick` returns verdicts and the scheduler
    applies them, because the last-live-instance guard and the probe cycle
    live there. `clock` is injected so replays can drive it.
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
        min_ttft_s: float = 0.25,
    ) -> None:
        self._clock = clock
        self.window_s = window_s
        self.band = band
        self.min_samples = min_samples
        self.probation_windows = probation_windows
        self.evict_windows = evict_windows
        self.recovery_windows = recovery_windows
        # Additive placement penalty in seconds (prefill leg; the decode leg
        # prices in tokens and converts at the TPOT SLO rate).
        self.penalty_s = penalty_s
        self.relative_band = relative_band
        self.min_ttft_s = min_ttft_s
        self._engines: dict[str, _EngineDrift] = {}

    def note(self, iid: str, residual: float) -> None:
        """One observed/predicted ratio on the verdict channel (decode)."""
        engine = self._engines.get(iid)
        if engine is None:
            engine = _EngineDrift(window_since=self._clock())
            self._engines[iid] = engine
        engine.residuals.append(residual)

    # The TTFT channel additionally refuses ratios under this floor of
    # observed seconds: a twenty-millisecond prefill measured against a
    # monitor interval coarser than itself is voxel noise, not information -
    # and the deep-queue failure this instrument exists to catch lives well
    # above this floor. The channel is informational: verdicts ride the
    # decode channel, whose batch-relative residual is occupancy-fair.
    def note_ttft(self, iid: str, observed_s: float, predicted_s: float) -> None:
        """The prefill channel, with the small-signal floor applied."""
        if observed_s < self.min_ttft_s:
            return
        engine = self._engines.get(iid)
        if engine is None:
            engine = _EngineDrift(window_since=self._clock())
            self._engines[iid] = engine
        engine.ttft_residuals.append(observed_s / max(predicted_s, 1e-9))

    def probation_set(self) -> set[str]:
        """Engines currently on probation: penalized in placement cost."""
        return {iid for iid, e in self._engines.items() if e.on_probation}

    def evicted(self, iid: str) -> None:
        """The scheduler confirmed the ejection: the record dies with the
        engine, and a readmitted engine re-earns its case from fresh
        windows. An evict verdict the scheduler refused never lands here,
        so its probation - and the placement penalty - genuinely stand."""
        self._engines.pop(iid, None)

    def score(self, iid: str) -> float | None:
        """The last scored window's mean residual, for the log line."""
        engine = self._engines.get(iid)
        return engine.score if engine else None

    def tick(self) -> list[tuple[str, str]]:
        """Close any due windows; return (verdict, iid) pairs it adjudicated.

        Verdicts: "probation", "evict", "recover". A window short on samples
        resets without a verdict — silence is not evidence either way.
        """
        now = self._clock()
        scored: dict[str, float] = {}
        for iid, e in self._engines.items():
            if now - e.window_since < self.window_s:
                continue
            e.window_since = now
            if len(e.residuals) < self.min_samples:
                e.residuals.clear()
                continue
            e.score = sum(e.residuals) / len(e.residuals)
            e.residuals.clear()
            scored[iid] = e.score
        # A quorum needs two scored engines; alone, a score reads only
        # against the absolute band.
        quorum_size = len(scored)

        verdicts: list[tuple[str, str]] = []
        for iid, score in scored.items():
            e = self._engines[iid]
            # The verdict band is `band` times the engine's own trailing
            # reading (floored at 1.0x): a twice-as-noisy healthy engine
            # convicts as readily as a clean one.
            personal_band = self.band * max(e.baseline or 1.0, 1.0)
            past_band = score > personal_band
            # The surge guard: with more than one engine scoring, a verdict
            # needs a majority-healthy field - more than half the others
            # inside their own bands. A fleet that surges together vetoes
            # itself; a solo drifter stands alone.
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
                    # The knob's magnitude: an engine this far above even a
                    # surging fleet's median is its own story, not the
                    # fleet's, and speaks through the veto.
                    if median_peer > 0 and score > self.relative_band * median_peer:
                        majority_veto = False
            if e.baseline is None:
                # A cold channel (a fresh role after a flip) starts at the
                # peers' median baseline in this same window: same-channel
                # engines share the measurement's systematic offset, so that
                # offset is the honest seed. No peers scored - it owns itself.
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
                    # The verdict is a request, not the act: the scheduler
                    # may refuse it (last live instance), and then probation
                    # must actually stand. `evicted()` is the confirmation
                    # that retires this engine's record.
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
                    # A vetoed surge window is a capacity story: evidence of
                    # neither drift nor recovery. Both counters hold, so a
                    # still-degraded engine cannot ride a fleet surge out of
                    # probation.
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
