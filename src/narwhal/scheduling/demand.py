"""Accounting for offered work, output lengths and decode residency."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from statistics import median

from ..profiling.model import Profile
from ..types import Request, Role
from .monitor import InstanceMonitor
from .prefill import warm_prefill_time
from .scheduler.placement import GlobalScheduler
from .window import Cohort, DemandWindow, weighted_median

OutputEstimates = tuple[dict[tuple[int, int], float], dict[int, float]]
CAPACITY_STEPS_PER_OCTAVE = 16


@dataclass(frozen=True)
class Demand:
    """Phase work observed inside one demand window."""

    prefill_engines: float
    decode_engines: float
    arrivals: int
    output_observations: int
    complete: bool = True
    # Arrivals with a prompt longer than some profile's prefill sweep.
    arrivals_beyond_profile: int = 0
    # Offers turned away before sizing, or still being read, in the span.
    unsized_offers: int = 0


@dataclass(frozen=True)
class Arrival:
    """One offered prompt: its input tokens and the tokens each engine holds cached."""

    input_len: int
    cached: tuple[tuple[str, int], ...] = ()

    @classmethod
    def of(cls, input_len: int, cached_tokens: Mapping[str, int] | None = None) -> Arrival:
        """Build an arrival from cached tokens per engine."""
        return cls(input_len, tuple(sorted((cached_tokens or {}).items())))


ArrivalObservation = tuple[Cohort[Arrival] | None, Cohort[tuple[int, int]] | None]


def _merge_arrivals(a: Arrival, b: Arrival) -> Arrival:
    # A merged overflow cohort prices cold.
    return Arrival(max(a.input_len, b.input_len))


class DemandModel:
    """Offered work, observed outputs and decode-residency samples."""

    def __init__(
        self,
        monitor: InstanceMonitor,
        scheduler: GlobalScheduler,
        clock: Callable[[], float],
        *,
        window_s: float,
        bucket_s: float,
    ) -> None:
        self.monitor = monitor
        self.scheduler = scheduler
        self._clock = clock
        self.window_s = window_s
        self._estimates: tuple[float, OutputEstimates, int] | None = None
        # Output estimates from the last history without shape overflow.
        self._learned: OutputEstimates = ({}, {})
        self.started_at = clock()
        self.unsized_pending = 0
        self.unsized = DemandWindow[bool](
            clock, retained_s=window_s, bucket_s=bucket_s, merge=lambda a, b: a, max_shapes=1
        )
        self.arrivals = DemandWindow[Arrival](
            clock, retained_s=window_s, bucket_s=bucket_s, merge=_merge_arrivals
        )
        self.expected_decode = DemandWindow[tuple[int, int]](
            clock,
            retained_s=window_s,
            bucket_s=bucket_s,
            merge=lambda a, b: (max(a[0], b[0]), max(a[1], b[1]) if a[1] and b[1] else 0),
        )
        self.observed_decode = DemandWindow[tuple[int, int, int]](
            clock,
            retained_s=4 * window_s,
            bucket_s=bucket_s,
            # Overflow keeps the full output budget as the demand estimate.
            merge=lambda a, b: a,
        )
        self.residency = DemandWindow[float](
            clock, retained_s=window_s, bucket_s=bucket_s, merge=max
        )
        self.last_demand = Demand(0.0, 0.0, 0, 0)

    def resolve_unsized(self, *, at: float, retain: bool) -> None:
        """Resolve one pending unsized offer, recording it when `retain` is set."""
        self.unsized_pending -= 1
        if retain:
            self.unsized.add(True, at=at)

    def arrival_count(self, since: float | None = None) -> int:
        """Return arrivals since `since`, including unsized and pending offers."""
        return self.arrivals.count(since) + self.unsized.count(since) + self.unsized_pending

    def saw_arrival(
        self,
        input_len: int,
        *,
        wanted_len: int | None = None,
        at: float | None = None,
    ) -> ArrivalObservation:
        """Record offered prefill work and requested decode work."""
        seen = self._clock() if at is None else at
        arrival = self.arrivals.add(Arrival(input_len), at=seen)
        expected = None
        if wanted_len is not None:
            expected = self.expected_decode.add((input_len, max(0, wanted_len)), at=seen)
        return arrival, expected

    def resize_arrival(
        self,
        observation: ArrivalObservation,
        input_len: int,
        wanted_len: int,
        *,
        at: float,
        cached_tokens: dict[str, int] | None = None,
    ) -> Cohort[Arrival] | None:
        """Replace a local estimate with the admitted request's token counts and cache evidence.

        Return the arrival's cohort.
        """
        arrival, expected = observation
        repriced = self.arrivals.replace(arrival, Arrival.of(input_len, cached_tokens), at=at)
        self.expected_decode.replace(expected, (input_len, max(0, wanted_len)), at=at)
        return repriced

    def reprice_arrival(self, request: Request) -> None:
        """Price the request's offered prefill with its rechecked cache evidence."""
        if request.demand_arrival is None or request.arrived_at is None:
            return
        request.demand_arrival = self.arrivals.replace(
            request.demand_arrival,
            Arrival.of(request.input_len, request.cached_tokens),
            at=request.arrived_at,
        )

    def saw_completion(
        self,
        input_len: int,
        wanted_len: int,
        observed_len: int,
        *,
        at: float | None = None,
    ) -> None:
        """Record output delivered by one successful request.

        The server calls this once after the stream closes.
        """
        self.observed_decode.add((input_len, max(0, wanted_len), max(0, observed_len)), at=at)

    def sample(self) -> None:
        """Add current decode residency, in engine equivalents, to the demand window."""
        resident = 0.0
        for inst in self.monitor.instances.values():
            profile = self.scheduler.profiles.get(inst.iid)
            if profile is None or not inst.decode:
                continue
            correction = self.monitor.decode_correction(inst.iid)
            ceiling = profile.max_tokens(
                self.scheduler.slo.tpot_s / correction,
                len(inst.decode),
            )
            if ceiling > 0:
                share = inst.decode_tokens() / ceiling
                cap = self.scheduler.decode_concurrency
                resident += max(share, len(inst.decode) / cap) if cap > 0 else share
        self.residency.add(resident)

    def _decode_correction(self) -> float:
        """Return the fleet median live/profile decode ratio."""
        values = [
            self.monitor.decode_correction(inst.iid)
            for inst in self.monitor.instances.values()
            if inst.role is Role.DECODE
        ]
        return median(values) if values else 1.0

    def estimate(
        self,
        now: float,
        *,
        window_s: float,
        step_s: float,
        horizon_s: float | None = None,
        estimates: OutputEstimates | None = None,
        correction: float | None = None,
        resident_s: float | None = None,
    ) -> tuple[float, float]:
        """Return prefill and decode demand in engine equivalents.

        `horizon_s` limits offered inputs to a trailing span and skips the `last_demand` update.
        `resident_s` limits decode residency to a trailing span.
        """
        window = horizon_s if horizon_s is not None else window_s
        profiles = tuple(
            row
            for iid in self.monitor.instances
            if (row := self.scheduler.profiles.get(iid)) is not None
        )
        priced = self.price_profiles(
            now,
            window_s=window_s,
            step_s=step_s,
            profiles=profiles,
            horizon_s=window,
            estimates=estimates,
            correction=correction,
            prefill_iids={inst.iid for inst in self.scheduler.live_instances(Role.PREFILL)},
            resident_s=resident_s,
        )
        if horizon_s is None:
            self.last_demand = priced
        return priced.prefill_engines, priced.decode_engines

    def resident_demand(self, now: float, window_s: float) -> float:
        """Return the observed decode residency in engine equivalents."""
        residents = list(self.residency.rows(now - window_s))
        count = sum(row.count for row in residents)
        return sum(row.value * row.count for row in residents) / count if count else 0.0

    def price_profiles(
        self,
        now: float,
        *,
        window_s: float,
        step_s: float,
        profiles: tuple[Profile, ...],
        horizon_s: float | None = None,
        estimates: OutputEstimates | None = None,
        correction: float | None = None,
        prefill_iids: Collection[str] | None = None,
        resident_s: float | None = None,
    ) -> Demand:
        """Price one demand window with the profiles measured for one role mix.

        Cached arrivals take warm prices from engines in `prefill_iids`, or from every engine
        when `prefill_iids` is None.
        Unsized offers take the mean price of the span's parsed offers.
        """
        window = horizon_s if horizon_s is not None else window_s
        span = min(window, max(step_s, now - self.started_at))
        h0 = now - window
        parsed = self.arrivals.count(h0)
        unsized = self.unsized.count(h0) + self.unsized_pending
        demand_complete = bool(profiles) and (parsed > 0 or not unsized)
        prefill = 0.0
        beyond = 0
        # Price per arrival shape, and whether the prompt is longer than some profile's sweep.
        prices: dict[tuple[int, tuple[tuple[str, int], ...]], tuple[float, bool]] = {}
        if profiles:
            for row in self.arrivals.rows(h0):
                shape = (row.value.input_len, row.value.cached)
                if shape not in prices:
                    prices[shape] = (
                        self._arrival_price(profiles, *shape, prefill_iids),
                        not all(p.covers_prefill(shape[0]) for p in profiles),
                    )
                cost, outside = prices[shape]
                prefill += cost * row.count / span
                if outside:
                    beyond += row.count
        expected_decode = 0.0
        estimates = self._output_estimates() if estimates is None else estimates
        correction = self._decode_correction() if correction is None else correction
        capacities: dict[tuple[int, int], float | None] = {}
        exact_capacities: dict[tuple[int, int], float | None] = {}
        # Capacity bucket and exact shape per expected shape; None marks an unknown output.
        buckets: dict[tuple[int, int, bool], tuple[tuple[int, int], tuple[int, int]] | None] = {}
        for expected_row in self.expected_decode.rows(h0):
            input_len, wanted_len = expected_row.value
            expected_shape = (input_len, wanted_len, expected_row.overflow)
            if expected_shape not in buckets:
                # An overflow row may span several output buckets and prices its full cap.
                output_len = (
                    wanted_len
                    if expected_row.overflow
                    else self._expected_output(input_len, wanted_len, estimates)
                )
                buckets[expected_shape] = (
                    (
                        (self._capacity_bucket(input_len), self._capacity_bucket(output_len)),
                        (input_len, output_len),
                    )
                    if output_len
                    else None
                )
            shapes = buckets[expected_shape]
            if shapes is None:
                demand_complete = False
                continue
            key, exact = shapes
            if key not in capacities:
                capacities[key] = self._shape_capacity(profiles, key, correction)
            capacity = capacities[key]
            if not capacity:
                # A bucket without capacity prices the exact shape.
                if exact not in exact_capacities:
                    exact_capacities[exact] = self._shape_capacity(profiles, exact, correction)
                capacity = exact_capacities[exact]
            if not capacity:
                demand_complete = False
                continue
            expected_decode += expected_row.count / (span * capacity)
        scale = (parsed + unsized) / parsed if parsed else 1.0
        return Demand(
            prefill_engines=prefill * scale,
            decode_engines=max(
                self.resident_demand(now, window_s if resident_s is None else resident_s),
                expected_decode * scale,
            ),
            arrivals=self.arrival_count(now - window_s),
            output_observations=self.observed_decode.count(now - 4 * window_s),
            complete=demand_complete,
            arrivals_beyond_profile=beyond,
            unsized_offers=unsized,
        )

    def _shape_capacity(
        self, profiles: tuple[Profile, ...], key: tuple[int, int], correction: float
    ) -> float | None:
        """Return the mean decode request rate for one input and output length."""
        input_len, output_len = key
        context = input_len + output_len / 2.0
        values = [
            p.decode_rps(
                self.scheduler.slo.tpot_s,
                context,
                output_len,
                correction=correction,
                request_cap=self.scheduler.decode_concurrency,
            )
            for p in profiles
        ]
        return sum(values) / len(values) if values else None

    @staticmethod
    def _arrival_price(
        profiles: tuple[Profile, ...],
        length: int,
        cached: tuple[tuple[str, int], ...],
        prefill_iids: Collection[str] | None,
    ) -> float:
        """Price one arrival at the lower of the mean cold and cheapest warm prefill prices."""
        cold = sum(p.prefill_time(length) for p in profiles) / len(profiles)
        tokens = dict(cached)
        warm = (
            warm_prefill_time(p, length, tokens.get(p.iid, 0))
            for p in profiles
            if prefill_iids is None or p.iid in prefill_iids
        )
        return min((cold, *(price for price in warm if price is not None)))

    @staticmethod
    def _shape_bucket(tokens: int) -> int:
        """Place a token length in a power-of-two bucket."""
        return 1 if tokens <= 1 else 1 << (tokens - 1).bit_length()

    @staticmethod
    def _capacity_bucket(tokens: int) -> int:
        """Round a token length up onto a sixteenth-octave grid."""
        if tokens <= 1:
            return 1
        step = math.ceil(math.log2(tokens) * CAPACITY_STEPS_PER_OCTAVE)
        return max(tokens, math.ceil(2 ** (step / CAPACITY_STEPS_PER_OCTAVE)))

    def _output_estimates(
        self,
    ) -> OutputEstimates:
        rows = list(self.observed_decode.rows())
        # Any overflow row keeps the last estimates learned without overflow.
        if any(row.overflow for row in rows):
            return self._learned
        ratios: defaultdict[tuple[int, int], list[tuple[float, int]]] = defaultdict(list)
        outputs: defaultdict[int, list[tuple[float, int]]] = defaultdict(list)
        for row in rows:
            input_len, wanted_len, observed_len = row.value
            input_bucket = self._shape_bucket(input_len)
            if wanted_len > 0:
                key = (input_bucket, self._shape_bucket(wanted_len))
                ratios[key].append((observed_len / wanted_len, row.count))
            if observed_len > 0:
                outputs[input_bucket].append((observed_len, row.count))
        self._learned = (
            {
                key: weighted_median(values)
                for key, values in ratios.items()
                if sum(count for _, count in values) >= 3
            },
            {key: weighted_median(values) for key, values in outputs.items()},
        )
        return self._learned

    def history_summary(self) -> dict[str, dict[str, int | float]]:
        """Return storage and shape-overflow counts for each demand window."""
        return {
            "unsized": {**self.unsized.summary(), "pending": self.unsized_pending},
            "arrivals": self.arrivals.summary(),
            "expected_decode": self.expected_decode.summary(),
            "observed_decode": self.observed_decode.summary(),
            "residency": self.residency.summary(),
        }

    def refresh_output_estimates(self) -> OutputEstimates:
        """Rebuild the output estimates and the fleet-wide delivered-output median."""
        return self._estimate_snapshot()[1]

    def _estimate_snapshot(self) -> tuple[float, OutputEstimates, int]:
        estimates = self._output_estimates()
        rows = list(self.observed_decode.rows())
        delivered = [(float(row.value[2]), row.count) for row in rows if row.value[2] > 0]
        fleet = (
            round(weighted_median(delivered))
            if delivered and not any(row.overflow for row in rows)
            else 0
        )
        self._estimates = (self._clock(), estimates, fleet)
        return self._estimates

    def output_estimator(self) -> Callable[[Request], int]:
        """Return a function giving expected output tokens per request, 0 when unknown.

        A request without a shape estimate takes the fleet-wide delivered-output median.
        """
        _, estimates, fleet = self._current_snapshot()
        return lambda r: self._expected_output(r.input_len, r.wanted_len, estimates) or fleet

    def current_estimates(self) -> OutputEstimates:
        """Return the controller's output estimates, rebuilt when older than the demand window."""
        return self._current_snapshot()[1]

    def _current_snapshot(self) -> tuple[float, OutputEstimates, int]:
        snapshot = self._estimates
        if snapshot is None or self._clock() - snapshot[0] >= self.window_s:
            snapshot = self._estimate_snapshot()
        return snapshot

    def _expected_output(
        self,
        input_len: int,
        wanted_len: int,
        estimates: OutputEstimates | None = None,
    ) -> int:
        """Estimate output length for one input and output bucket.

        The requested cap applies until three matching requests finish; then the
        median delivered fraction applies.
        """
        input_bucket = self._shape_bucket(input_len)
        ratios, outputs = self._output_estimates() if estimates is None else estimates
        if wanted_len > 0:
            fraction = ratios.get((input_bucket, self._shape_bucket(wanted_len)))
            if fraction is None:
                return wanted_len
            return max(1, min(wanted_len, round(wanted_len * fraction)))
        observed = outputs.get(input_bucket)
        if observed is not None:
            return max(1, round(observed))
        return 0


def rounded(value: float, digits: int = 6) -> float | None:
    """Return finite telemetry values rounded for state and journal output."""
    return round(value, digits) if math.isfinite(value) else None
