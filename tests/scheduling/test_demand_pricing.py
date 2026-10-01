"""Check that window pricing matches a row-by-row reference on randomized windows."""

import random
import tempfile
import unittest
from pathlib import Path

from narwhal.scheduling.demand import Arrival, Demand
from narwhal.serving.app import create_app
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
    complete = bool(profiles) and not model.unsized_pending and not model.unsized.count(h0)
    for row in model.arrivals.rows(h0):
        length = row.value.input_len
        if profiles and all(p.covers_prefill(length) for p in profiles):
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
        else:
            complete = False
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
                request_cap=model.scheduler.decode_concurrency,
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
    return Demand(
        prefill_engines=prefill,
        decode_engines=max(model.resident_demand(now, window_s), expected_decode),
        arrivals=model.arrival_count(now - window_s),
        output_observations=model.observed_decode.count(now - 4 * window_s),
        complete=complete,
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
        profiles = tuple(scheduler.profiles.get(iid) for iid in iids)
        for trial in range(40):
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
