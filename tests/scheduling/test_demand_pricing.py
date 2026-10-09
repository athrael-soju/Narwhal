"""Check that window pricing matches a row-by-row reference on randomized windows."""

import random
import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

from narwhal.scheduling.demand import Arrival, Demand, DemandModel
from narwhal.scheduling.prefill import prefill_seconds
from narwhal.serving.app import create_app
from narwhal.types import Request
from tests.fixtures import fleet, profile, put_warm


def reference_price_profiles(
    model,
    now,
    *,
    window_s,
    step_s,
    profiles,
    horizon_s=None,
    estimates=None,
    correction=None,
    prefill_iids=None,
):
    """Price every row on its own, without the per-shape memo."""
    window = horizon_s if horizon_s is not None else window_s
    span = min(window, max(step_s, now - model.started_at))
    h0 = now - window
    prefill = 0.0
    parsed = model.arrivals.count(h0)
    unsized = model.unsized.count(h0) + model.unsized_pending
    complete = bool(profiles) and (parsed > 0 or not unsized)
    beyond = 0
    for row in model.arrivals.rows(h0) if profiles else ():
        length = row.value.input_len
        cost = sum(p.prefill_time(length) for p in profiles) / len(profiles)
        cached = dict(row.value.cached)
        for p in profiles:
            if prefill_iids is not None and p.iid not in prefill_iids:
                continue
            tokens = cached.get(p.iid, 0)
            warm_s = p.cached_prefill_time(tokens, length - tokens) if tokens else None
            if warm_s is not None:
                cost = min(cost, warm_s)
        prefill += cost * row.count / span
        if not all(p.covers_prefill(length) for p in profiles):
            beyond += row.count
    expected_decode = 0.0
    estimates = model._output_estimates() if estimates is None else estimates
    correction = model._decode_correction() if correction is None else correction

    def capacity_at(input_tokens, output_tokens):
        values = [
            p.decode_rps(
                model.scheduler.slo.tpot_s,
                input_tokens + output_tokens / 2.0,
                output_tokens,
                correction=correction,
            )
            for p in profiles
        ]
        return sum(values) / len(values) if values else None

    for row in model.expected_decode.rows(h0):
        input_len, wanted_len = row.value
        output_len = (
            wanted_len if row.overflow else model._expected_output(input_len, wanted_len, estimates)
        )
        if output_len == 0:
            complete = False
            continue
        capacity = capacity_at(
            model._capacity_bucket(input_len), model._capacity_bucket(output_len)
        ) or capacity_at(input_len, output_len)
        if not capacity:
            complete = False
            continue
        expected_decode += row.count / (span * capacity)
    scale = (parsed + unsized) / parsed if parsed else 1.0
    return Demand(
        prefill_engines=prefill * scale,
        decode_engines=max(model.resident_demand(now, window_s), expected_decode * scale),
        arrivals=model.arrival_count(now - window_s),
        output_observations=model.observed_decode.count(now - 4 * window_s),
        complete=complete,
        arrivals_beyond_profile=beyond,
        unsized_offers=unsized,
    )


class WindowPricingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.router = create_app(fleet(Path(folder.name))).state.router
        scheduler = self.router.scheduler
        for iid in scheduler.monitor.instances:
            put_warm(scheduler.profiles, iid, cached_max_prefix_tokens=4096)

    def test_pricing_matches_the_row_by_row_reference(self):
        rng = random.Random(3)
        scheduler = self.router.scheduler
        iids = list(scheduler.monitor.instances)
        for trial in range(40):
            # Some profiles end their prefill sweep below some offered lengths.
            profiles = tuple(
                replace(scheduler.profiles.get(iid), prefill_max_tokens=rng.choice((None, 512)))
                for iid in iids
            )
            demand = type(self.router.controller.demand)(
                scheduler.monitor, scheduler, self.router._clock, window_s=60.0, bucket_s=1.0
            )
            now = self.router._clock()
            lengths = [rng.choice((40, 96, 200, 512, 900, 5000)) for _ in range(12)]
            for _ in range(rng.randrange(20, 400)):
                at = now - rng.uniform(0.0, 59.0)
                length = rng.choice(lengths)
                observation = demand.saw_arrival(length, wanted_len=rng.choice((0, 64, 256)), at=at)
                cached = {
                    iid: rng.choice((0, 16, 32, length // 2)) for iid in iids if rng.random() < 0.4
                }
                wanted = rng.choice((0, 16, 64, 256, 1024))
                demand.resize_arrival(observation, length, wanted, at=at, cached_tokens=cached)
                if rng.random() < 0.3:
                    demand.saw_completion(length, wanted, rng.randrange(1, 300), at=at)
            for _ in range(rng.randrange(0, 6)):
                demand.unsized_pending += 1
                demand.resolve_unsized(at=now - rng.uniform(0.0, 59.0), retain=True)
            demand.unsized_pending += rng.randrange(0, 3)
            for prefill_iids in (None, set(iids[:1]), set()):
                with self.subTest(trial=trial, prefill_iids=prefill_iids):
                    kwargs = {
                        "window_s": 60.0,
                        "step_s": 1.0,
                        "profiles": profiles,
                        "prefill_iids": prefill_iids,
                    }
                    self.assertEqual(
                        demand.price_profiles(now, **kwargs),
                        reference_price_profiles(demand, now, **kwargs),
                    )

    def test_a_decode_shape_whose_bucket_leaves_the_domain_prices_its_exact_length(self):
        scheduler = self.router.scheduler
        profiles = tuple(profile(iid) for iid in scheduler.monitor.instances)
        demand = type(self.router.controller.demand)(
            scheduler.monitor, scheduler, self.router._clock, window_s=60.0, bucket_s=1.0
        )
        bucket_context = demand._capacity_bucket(98_000) + demand._capacity_bucket(2_000) / 2
        self.assertGreater(bucket_context, profiles[0].decode_max_kv_tokens)
        now = self.router._clock()
        demand.saw_arrival(98_000, wanted_len=2_000, at=now)
        kwargs = {"window_s": 60.0, "step_s": 1.0, "profiles": profiles}
        priced = demand.price_profiles(now, **kwargs)
        self.assertTrue(priced.complete)
        self.assertGreater(priced.decode_engines, 0.0)
        self.assertEqual(priced, reference_price_profiles(demand, now, **kwargs))

    def test_merged_overflow_arrivals_price_cold(self):
        scheduler = self.router.scheduler
        iids = list(scheduler.monitor.instances)
        profiles = tuple(scheduler.profiles.get(iid) for iid in iids)
        now = 1000.0
        shapes = self.router.controller.demand.arrivals.max_shapes

        def offered(count):
            demand = type(self.router.controller.demand)(
                scheduler.monitor, scheduler, lambda: now, window_s=60.0, bucket_s=1.0
            )
            for i in range(count):
                observation = demand.saw_arrival(1100, wanted_len=4, at=now)
                cached = {iids[0]: 1050 + i % 40, iids[1]: 1050 + i // 40}
                demand.resize_arrival(observation, 1100, 4, at=now, cached_tokens=cached)
            return demand

        def price(demand):
            return demand.price_profiles(
                now, window_s=60.0, step_s=1.0, profiles=profiles
            ).prefill_engines

        full, overflowed = offered(shapes), offered(shapes + 5)
        [row] = [row for row in overflowed.arrivals.rows() if row.overflow]
        self.assertEqual((row.count, row.value), (5, Arrival(1100)))
        cold = sum(p.prefill_time(1100) for p in profiles) / len(profiles)
        self.assertLess(price(full), shapes * cold)
        self.assertAlmostEqual(price(overflowed) - price(full), 5 * cold)


class DomainAndUnsizedPricingTests(unittest.TestCase):
    """Demand stays complete with prompts beyond the sweep and with unsized offers."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        router = create_app(fleet(Path(folder.name))).state.router
        self.scheduler = router.scheduler
        self.now = 1000.0
        self.demand = self.model()
        self.profiles = tuple(
            profile(iid, ttft_b=0.001 * (index + 1), prefill_max_tokens=1000)
            for index, iid in enumerate(self.scheduler.monitor.instances)
        )

    def model(self) -> DemandModel:
        demand = DemandModel(
            self.scheduler.monitor, self.scheduler, lambda: self.now, window_s=60.0, bucket_s=1.0
        )
        demand.started_at = 0.0
        return demand

    def price(self, horizon_s=None):
        return self.demand.price_profiles(
            self.now, window_s=60.0, step_s=1.0, profiles=self.profiles, horizon_s=horizon_s
        )

    def parsed(self, count: int, *, length: int = 400, at: float | None = None) -> None:
        for _ in range(count):
            self.demand.saw_arrival(length, wanted_len=50, at=self.now if at is None else at)

    def unsized(self, count: int, *, at: float | None = None, retain: bool = True) -> None:
        for _ in range(count):
            self.demand.unsized_pending += 1
            if retain:
                self.demand.resolve_unsized(at=self.now if at is None else at, retain=True)

    def test_prompts_above_the_sweep_take_the_extended_fit(self):
        self.parsed(3, length=4000)
        self.parsed(2, length=400)
        mixed = (replace(self.profiles[0], prefill_max_tokens=None), *self.profiles[1:])
        priced = self.demand.price_profiles(self.now, window_s=60.0, step_s=1.0, profiles=mixed)
        self.assertTrue(priced.complete)
        cold = {n: sum(p.prefill_time(n) for p in mixed) / len(mixed) for n in (4000, 400)}
        self.assertAlmostEqual(priced.prefill_engines, (3 * cold[4000] + 2 * cold[400]) / 60.0)
        self.assertEqual(priced.arrivals_beyond_profile, 3)

    def test_cold_price_above_the_sweep_matches_admission(self):
        row = self.profiles[0]
        self.assertFalse(row.covers_prefill(4000))
        self.assertEqual(
            DemandModel._arrival_price((row,), 4000, (), None),
            prefill_seconds(row, Request("r", 4000)),
        )

    def test_unsized_offers_take_the_mean_parsed_price(self):
        self.parsed(4)
        alone = self.price()
        for retained, pending in ((2, 0), (0, 2), (1, 1)):
            with self.subTest(retained=retained, pending=pending):
                self.demand = self.model()
                self.parsed(4)
                self.unsized(retained)
                self.unsized(pending, retain=False)
                priced = self.price()
                self.assertTrue(priced.complete)
                self.assertEqual(priced.unsized_offers, 2)
                self.assertAlmostEqual(priced.prefill_engines, alone.prefill_engines * 6 / 4)
                self.assertAlmostEqual(priced.decode_engines, alone.decode_engines * 6 / 4)

    def test_resident_decode_demand_is_not_scaled(self):
        self.parsed(4)
        self.unsized(4)
        with unittest.mock.patch.object(self.demand, "resident_demand", return_value=50.0):
            priced = self.price()
        self.assertEqual(priced.decode_engines, 50.0)

    def test_a_horizon_view_scales_by_its_own_offers(self):
        self.parsed(4, at=self.now - 40.0)
        self.unsized(4, at=self.now - 40.0)
        self.parsed(2)
        self.unsized(1)
        window, recent = self.price(), self.price(horizon_s=15.0)
        self.assertEqual((window.unsized_offers, recent.unsized_offers), (5, 1))
        cold = sum(p.prefill_time(400) for p in self.profiles) / len(self.profiles)
        self.assertAlmostEqual(window.prefill_engines, 6 * cold / 60.0 * 11 / 6)
        self.assertAlmostEqual(recent.prefill_engines, 2 * cold / 15.0 * 3 / 2)

    def test_unsized_offers_without_parsed_offers_leave_demand_incomplete(self):
        self.unsized(2)
        self.assertFalse(self.price().complete)

    def test_unsized_offers_older_than_the_window_leave_pricing_unchanged(self):
        self.unsized(3, at=self.now - 70.0)
        self.parsed(4)
        priced = self.price()
        self.assertTrue(priced.complete)
        cold = sum(p.prefill_time(400) for p in self.profiles) / len(self.profiles)
        self.assertAlmostEqual(priced.prefill_engines, 4 * cold / 60.0)
