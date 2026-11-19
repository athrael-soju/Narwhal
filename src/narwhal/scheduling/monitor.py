"""Derive per-engine load from scheduler events."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice

from ..profiling.store import ProfileStore
from ..types import Instance, Phase, Request, Role
from .prefill import prefill_seconds


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
        # Returns an engine's attested sequence limit, 0 when unknown. The router sets it.
        self.sequence_limit: Callable[[str], int] = lambda iid: 0
        self._windows: dict[str, _Window] = {}
        self._handoffs: dict[str, _Window] = {}
        # Resident decode tokens per engine.
        self._decode_tokens: dict[str, int] = {}
        # Requests and tokens of each engine's decode legs past their first decode token,
        # read by `output_token`.
        self._decoding: dict[str, list[int]] = {}
        # The engine each such leg decodes on, by request ID.
        self._decoding_on: dict[str, str] = {}
        self._prices: dict[str, _PriceWindow] = {}
        self._last_token: dict[str, float] = {}
        # Latest prefill completion or decode token per engine.
        self._last_output: dict[str, float] = {}
        # Requests past their first decode gap, which includes transfer and queue time.
        self._decode_started: set[str] = set()
        # Decode streams whose engine output has ended and await release.
        self._decode_ended: set[str] = set()
        self._last_prefill_activity: dict[str, float] = {}
        # Requests the router has sent to each engine: placed legs and its own probes.
        self.requests_sent: dict[str, int] = {}

    def add(self, instance: Instance) -> None:
        """Register an engine and initialize its interval state."""
        now = self._clock()
        self.instances[instance.iid] = instance
        self._windows[instance.iid] = _Window()
        self._handoffs[instance.iid] = _Window()
        self._prices[instance.iid] = _PriceWindow(anchor=now, window_start=now)
        self._decode_tokens[instance.iid] = instance.decode_tokens()
        self._decoding[instance.iid] = [0, 0]
        self.requests_sent.setdefault(instance.iid, 0)

    def sent(self, *iids: str) -> None:
        """Count one request the router sends to each of `iids`."""
        for iid in iids:
            self.requests_sent[iid] = self.requests_sent.get(iid, 0) + 1

    def pool(self, role: Role) -> list[Instance]:
        """Return engines assigned to a role."""
        return [i for i in self.instances.values() if i.role == role]

    def _reprice(self, iid: str) -> None:
        """Update the engine's resident prefill price at the current time."""
        w = self._prices[iid]
        w.touch(self._clock())
        w.current = self.running_prefill_seconds(iid)

    def seat_legs(self, iid: str) -> tuple[list[Request], list[Request]]:
        """Split the engine's decode legs into those it seats and those waiting for a seat.

        An engine seats its legs in placement order, up to its attested sequence limit. Without
        a limit every leg is seated.
        """
        legs = list(self.instances[iid].decode.values())
        limit = self.sequence_limit(iid)
        if limit <= 0 or len(legs) <= limit:
            return legs, []
        return legs[:limit], legs[limit:]

    def seat_waiting(self) -> set[str]:
        """Return the requests whose decode leg waits for a seat inside its engine."""
        return {
            leg.rid
            for iid, inst in self.instances.items()
            if 0 < self.sequence_limit(iid) < len(inst.decode)
            for leg in self.seat_legs(iid)[1]
        }

    def running_prefill_seconds(self, iid: str, waiting: set[str] | None = None) -> float:
        """Price the prefill work resident on the engine that is free to run.

        A request whose decode leg waits for a seat starts its prefill when the leg is seated,
        so it adds no prefill work. `waiting` is `seat_waiting()`, read when None.
        """
        profile = self.profiles.get(iid) if self.profiles is not None else None
        prefill = self.instances[iid].prefill
        if profile is None or not prefill:
            return 0.0
        waiting = self.seat_waiting() if waiting is None else waiting
        # Resident requests keep their placement-time price.
        return sum(prefill_seconds(profile, r) for r in prefill.values() if r.rid not in waiting)

    def _reprice_prefill(self, request: Request) -> None:
        iid = request.prefill_instance
        if iid in self.instances and request.rid in self.instances[iid].prefill:
            self._reprice(iid)

    def dispatched(self, iid: str, request: Request) -> None:
        """Add `request` to the engine's resident prefill or decode set."""
        inst = self.instances[iid]
        self.waiting.pop(request.rid, None)
        self.sent(iid)
        if request.phase is Phase.PREFILL:
            inst.prefill[request.rid] = request
            request.prefill_instance = iid
            self._prefill_changed(iid)
            self._reprice(iid)
        else:
            replaced = inst.decode.get(request.rid)
            if replaced is not None:
                self._decode_tokens[iid] -= replaced.length
                self._stop_decoding(iid, replaced)
            inst.decode[request.rid] = request
            self._decode_tokens[iid] += request.length
            self._decode_ended.discard(request.rid)
            if 0 < self.sequence_limit(iid) < len(inst.decode):
                # Placed past the engine's seats, the leg holds its request's prefill.
                self._reprice_prefill(request)

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
            # The engine batches the legs that decode. A leg placed past the engine's seats
            # waits inside the engine and adds nothing to the interval.
            requests, tokens = self._decoding[iid]
            expected = (
                profile.token_interval(tokens, requests)
                if profile is not None and profile.decode_max_requests is not None and requests
                else None
            )
            window.add(now - prev, expected)
        elif prev is not None:
            self._handoffs[iid].add(now - prev)
        self._decode_started.add(rid)
        self._last_token[rid] = self._last_output[iid] = now
        req = self.instances[iid].decode.get(rid)
        if req is not None:
            req.output_len += 1
            self._decode_tokens[iid] += 1
            decoding = self._decoding[iid]
            if self._decoding_on.get(rid) == iid:
                decoding[1] += 1
            else:
                self._decoding_on[rid] = iid
                decoding[0] += 1
                decoding[1] += req.length

    def output_ended(self, rid: str) -> None:
        """Record that a decode stream's engine output has ended before its release."""
        self._decode_ended.add(rid)

    def output_since(self, iid: str, since: float) -> bool:
        """Return whether `iid` completed a prefill or emitted a decode token after `since`."""
        return self._last_output.get(iid, float("-inf")) > since

    def finished(self, iid: str, rid: str) -> None:
        """Remove all tracking state for a completed request."""
        inst = self.instances[iid]
        decoding = inst.decode.pop(rid, None)
        if decoding is not None:
            self._decode_tokens[iid] -= decoding.length
            self._stop_decoding(iid, decoding)
            limit = self.sequence_limit(iid)
            if 0 < limit <= len(inst.decode):
                # The freed seat goes to the first waiting leg, whose request then prefills.
                self._reprice_prefill(next(islice(inst.decode.values(), limit - 1, None)))
        had_prefill = inst.prefill.pop(rid, None)
        self._last_token.pop(rid, None)
        self._decode_started.discard(rid)
        self._decode_ended.discard(rid)
        if had_prefill is not None:
            self._prefill_changed(iid)
            self._reprice(iid)
        self.on_capacity_change()

    def _stop_decoding(self, iid: str, leg: Request) -> None:
        if self._decoding_on.get(leg.rid) == iid:
            del self._decoding_on[leg.rid]
            decoding = self._decoding[iid]
            decoding[0] -= 1
            decoding[1] -= leg.length

    def decoding(self, iid: str) -> tuple[int, int]:
        """Return the requests and tokens of the engine's legs past their first decode token."""
        requests, tokens = self._decoding[iid]
        return requests, tokens

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

    def handoff_delay(self, iid: str) -> float:
        """Return the latest interval's mean wait from prefill completion to first decode token."""
        return self._handoffs[iid].published

    def stalled_gap(self, iid: str) -> float:
        """Return the longest open inter-token gap on an engine.

        Requests awaiting their first decode token and streams whose output has ended are
        excluded.
        """
        now = self._clock()
        gaps = [
            now - self._last_token[rid]
            for rid in self.instances[iid].decode
            if rid in self._last_token
            and rid in self._decode_started
            and rid not in self._decode_ended
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
        for h in self._handoffs.values():
            h.published = h.total_s / h.count if h.count else 0.0
            h.total_s, h.count = 0.0, 0
        for p in self._prices.values():
            p.roll(now)
