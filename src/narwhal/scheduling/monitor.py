"""Derive per-engine load from scheduler events."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..profiling.store import ProfileStore
from ..types import Instance, Phase, Request, Role
from .prefill import resident_prefill_seconds


@dataclass
class _Window:
    """Accumulate inter-token gaps for one monitoring interval.

    `published` remains fixed until the next completed interval.
    """

    total_s: float = 0.0
    count: int = 0
    published: float = 0.0
    ratio_total: float = 0.0
    ratio_count: int = 0
    correction: float = 1.0
    prefill_overlap: bool = False

    def add(self, gap: float, expected: float | None = None) -> None:
        self.total_s += gap
        self.count += 1
        if expected is not None and expected > 0.0:
            self.ratio_total += gap / expected
            self.ratio_count += 1

    def mean(self) -> float:
        return self.published

    def reset(
        self,
        *,
        correction_min: float,
        correction_max: float,
        correction_alpha: float,
        correction_min_samples: int,
    ) -> None:
        if self.count:
            self.published = self.total_s / self.count
        if not self.prefill_overlap and self.ratio_count >= correction_min_samples:
            observed = self.ratio_total / self.ratio_count
            bounded = max(correction_min, min(correction_max, observed))
            self.correction += correction_alpha * (bounded - self.correction)
        self.total_s = 0.0
        self.count = 0
        self.ratio_total = 0.0
        self.ratio_count = 0
        self.prefill_overlap = False


@dataclass
class _PriceWindow:
    """Integrate resident prefill cost over one monitoring interval."""

    integral: float = 0.0
    current: float = 0.0
    anchor: float = 0.0
    window_start: float = 0.0
    published: float = 0.0

    def touch(self, now: float) -> None:
        self.integral += self.current * (now - self.anchor)
        self.anchor = now

    def roll(self, now: float) -> None:
        self.touch(now)
        span = now - self.window_start
        if span > 0.0:
            self.published = self.integral / span
        self.integral = 0.0
        self.window_start = now


class InstanceMonitor:
    """Track resident work and interval load for each engine.

    When profiles are unavailable, prefill interval prices remain zero.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        profiles: ProfileStore | None = None,
        *,
        decode_correction_min: float = 0.5,
        decode_correction_max: float = 2.0,
        decode_correction_alpha: float = 0.2,
        decode_correction_min_samples: int = 8,
    ) -> None:
        self._clock = clock
        self.profiles = profiles
        self.decode_correction_min = decode_correction_min
        self.decode_correction_max = decode_correction_max
        self.decode_correction_alpha = decode_correction_alpha
        self.decode_correction_min_samples = decode_correction_min_samples
        self.instances: dict[str, Instance] = {}
        # Accepted work without an engine reservation remains controller demand.
        self.waiting: dict[str, Request] = {}
        self.on_capacity_change: Callable[[], None] = lambda: None
        self._windows: dict[str, _Window] = {}
        # Resident decode tokens per engine, read by `output_token`.
        self._decode_tokens: dict[str, int] = {}
        self._prices: dict[str, _PriceWindow] = {}
        self._last_token: dict[str, float] = {}
        # Latest prefill completion or decode token per engine.
        self._last_output: dict[str, float] = {}
        # Requests past their first decode gap, which includes transfer and queue time.
        self._decode_started: set[str] = set()
        self._last_prefill_activity: dict[str, float] = {}

    def add(self, instance: Instance) -> None:
        """Register an engine and initialize its interval state."""
        now = self._clock()
        self.instances[instance.iid] = instance
        self._windows[instance.iid] = _Window()
        self._prices[instance.iid] = _PriceWindow(anchor=now, window_start=now)
        self._decode_tokens[instance.iid] = instance.decode_tokens()

    def pool(self, role: Role) -> list[Instance]:
        """Return engines assigned to a role."""
        return [i for i in self.instances.values() if i.role == role]

    def _reprice(self, iid: str) -> None:
        """Update the engine's resident prefill price at the current time."""
        w = self._prices[iid]
        w.touch(self._clock())
        profile = self.profiles.get(iid) if self.profiles is not None else None
        if profile is None:
            w.current = 0.0
            return
        inst = self.instances[iid]
        # Resident requests keep their placement-time price.
        w.current = resident_prefill_seconds(profile, inst)

    def dispatched(self, iid: str, request: Request) -> None:
        """Add `request` to the engine's resident prefill or decode set."""
        inst = self.instances[iid]
        self.waiting.pop(request.rid, None)
        if request.phase is Phase.PREFILL:
            inst.prefill[request.rid] = request
            request.prefill_instance = iid
            self._prefill_changed(iid)
            self._reprice(iid)
        else:
            replaced = inst.decode.get(request.rid)
            if replaced is not None:
                self._decode_tokens[iid] -= replaced.length
            inst.decode[request.rid] = request
            self._decode_tokens[iid] += request.length

    def first_token(self, iid: str, rid: str) -> None:
        """Move a request out of prefill and start its token clock."""
        if self.instances[iid].prefill.pop(rid, None) is not None:
            self._prefill_changed(iid)
        self._last_token[rid] = self._last_output[iid] = self._clock()
        self._reprice(iid)
        self.on_capacity_change()

    def output_token(self, iid: str, rid: str) -> None:
        """Record one decode token and its inter-token gap against the engine that served it."""
        now = self._clock()
        prev = self._last_token.get(rid)
        if prev is not None and rid in self._decode_started:
            inst = self.instances[iid]
            window = self._windows[iid]
            if inst.prefill or prev < self._last_prefill_activity.get(iid, float("-inf")):
                # A gap spanning prefill remains mixed even if that prefill
                # completed before this token or the current monitoring pass.
                window.prefill_overlap = True
            profile = self.profiles.get(iid) if self.profiles is not None else None
            expected = (
                profile.token_interval(self._decode_tokens[iid], len(inst.decode))
                if profile is not None and profile.decode_max_requests is not None
                else None
            )
            window.add(now - prev, expected)
        self._decode_started.add(rid)
        self._last_token[rid] = self._last_output[iid] = now
        req = self.instances[iid].decode.get(rid)
        if req is not None:
            req.output_len += 1
            self._decode_tokens[iid] += 1

    def output_since(self, iid: str, since: float) -> bool:
        """Return whether `iid` completed a prefill or emitted a decode token after `since`."""
        return self._last_output.get(iid, float("-inf")) > since

    def finished(self, iid: str, rid: str) -> None:
        """Remove all tracking state for a completed request."""
        inst = self.instances[iid]
        decoding = inst.decode.pop(rid, None)
        if decoding is not None:
            self._decode_tokens[iid] -= decoding.length
        had_prefill = inst.prefill.pop(rid, None)
        self._last_token.pop(rid, None)
        self._decode_started.discard(rid)
        if had_prefill is not None:
            self._prefill_changed(iid)
            self._reprice(iid)
        self.on_capacity_change()

    def _prefill_changed(self, iid: str) -> None:
        self._windows[iid].prefill_overlap = True
        self._last_prefill_activity[iid] = self._clock()

    def decode_profile_eligible(self, iid: str) -> bool:
        """Return whether the engine's decode gaps are free of local prefill overlap.

        An open gap becomes eligible once a token arrives after the last prefill boundary.
        """
        instance = self.instances[iid]
        if instance.prefill or self._windows[iid].prefill_overlap:
            return False
        boundary = self._last_prefill_activity.get(iid, float("-inf"))
        return not any(
            self._last_token[rid] < boundary
            for rid in instance.decode
            if rid in self._last_token and rid in self._decode_started
        )

    def mean_token_interval(self, iid: str) -> float:
        """Return mean inter-token latency from the latest completed window."""
        return self._windows[iid].mean()

    def current_token_interval(self, iid: str) -> float:
        """Return this interval's measured token gap, zero without fresh output."""
        window = self._windows[iid]
        return window.total_s / window.count if window.count else 0.0

    def decode_correction(self, iid: str) -> float:
        """Return the bounded live/profile decode ratio for one engine."""
        return self._windows[iid].correction

    def stalled_gap(self, iid: str) -> float:
        """Return the longest open inter-token gap on an engine.

        Requests awaiting their first decode token are excluded.
        """
        now = self._clock()
        gaps = [
            now - self._last_token[rid]
            for rid in self.instances[iid].decode
            if rid in self._last_token and rid in self._decode_started
        ]
        return max(gaps, default=0.0)

    def mean_prefill_price(self, iid: str) -> float:
        """Return mean resident prefill cost for the previous interval."""
        return self._prices[iid].published

    def roll_interval(self) -> None:
        """Publish the current monitoring interval and open the next one."""
        now = self._clock()
        for w in self._windows.values():
            w.reset(
                correction_min=self.decode_correction_min,
                correction_max=self.decode_correction_max,
                correction_alpha=self.decode_correction_alpha,
                correction_min_samples=self.decode_correction_min_samples,
            )
        for p in self._prices.values():
            p.roll(now)
