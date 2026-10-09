"""Check prefix-cache evidence at sizing, the shared prefill estimate and advisory proposals."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from narwhal.engines.prefix import CacheNamespace, block_identities
from narwhal.scheduling.demand import Arrival
from narwhal.scheduling.prefill import prefill_seconds
from narwhal.scheduling.scoring import CACHE_RECHECK_S
from narwhal.serving.app import create_app
from narwhal.serving.router import sizing
from narwhal.types import Request, Role
from tests.fixtures import fleet, hold_prefix, profile, put_warm, warm

BLOCK = 4


class CacheEvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        # Pricing tests use fixed evidence.
        self.router.scheduler.recheck_cache_evidence = None
        self.first, self.second = (spec.iid for spec in self.cfg.engines)
        self.namespace = CacheNamespace(self.cfg.model, self.cfg.engine_contract.fingerprint())

    def hold(self, iid, tokens, *, salt=None):
        """Make one engine's residency view hold every full block of `tokens`."""
        namespace = replace(self.namespace, cache_salt=salt)
        hold_prefix(self.router.residency.view(iid), namespace, tokens, BLOCK)

    def test_sizing_reports_each_engines_cached_prompt_tokens(self):
        prompt = list(range(22))
        self.hold(self.first, prompt[:16])
        self.hold(self.second, prompt[:8])
        self.router.residency.view(self.first).sequence = 41
        cached, sequences, identities = self.router.sizer.prefix_cache_evidence(
            {"prompt": prompt}, prompt
        )
        self.assertEqual(cached, {self.first: 16, self.second: 8})
        self.assertEqual(sequences, {self.first: 41})
        self.assertEqual(identities, {BLOCK: block_identities(self.namespace, prompt[:16], BLOCK)})
        # The final prompt token is always computed.
        self.assertEqual(
            self.router.sizer.prefix_cache_evidence({"prompt": prompt[:16]}, prompt[:16])[0][
                self.first
            ],
            12,
        )

    def test_evidence_needs_the_same_namespace_text_only_and_known_residency(self):
        prompt = list(range(16))
        self.hold(self.first, prompt, salt="salt")
        self.assertEqual(self.router.sizer.prefix_cache_evidence({}, prompt)[0], {})
        self.assertEqual(
            self.router.sizer.prefix_cache_evidence({"cache_salt": "salt"}, prompt)[0],
            {self.first: 12},
        )
        image = {
            "messages": [{"role": "user", "content": [{"type": "image_url"}]}],
            "cache_salt": "salt",
        }
        self.assertEqual(self.router.sizer.prefix_cache_evidence(image, prompt)[0], {})
        self.router.residency.view(self.first).known = False
        self.assertEqual(
            self.router.sizer.prefix_cache_evidence({"cache_salt": "salt"}, prompt)[0], {}
        )
        self.router.cfg.engine_contract = None
        self.router.residency.view(self.first).known = True
        self.assertEqual(
            self.router.sizer.prefix_cache_evidence({"cache_salt": "salt"}, prompt)[0], {}
        )

    def test_fields_that_change_the_prefilled_tokens_carry_no_evidence(self):
        """Truncation and template inputs outside token counting leave the request cold."""
        prompt = list(range(16))
        self.hold(self.first, prompt)
        self.assertEqual(self.router.sizer.prefix_cache_evidence({}, prompt)[0], {self.first: 12})
        for field, value in (
            ("truncate_prompt_tokens", 8),
            ("documents", [{"text": "doc"}]),
            ("reasoning_effort", "low"),
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    self.router.sizer.prefix_cache_evidence({field: value}, prompt)[0], {}
                )

    def test_a_speculative_decoding_contract_prices_cold(self):
        prompt = list(range(16))
        contract = replace(self.cfg.engine_contract, speculative_config='{"method": "eagle"}')
        self.router.cfg.engine_contract = contract
        self.namespace = CacheNamespace(self.cfg.model, contract.fingerprint())
        self.hold(self.first, prompt)
        self.assertEqual(self.router.sizer.prefix_cache_evidence({}, prompt)[0], {})

    def test_boundary_state_must_sit_at_the_reusable_prefix_end(self):
        """A block-aligned prompt reuses at most the blocks before its final token."""
        prompt = list(range(8))
        view = self.router.residency.view(self.first)
        view.known, view.block_size = True, BLOCK
        blocks = block_identities(self.namespace, prompt, BLOCK)
        # Full attention holds both blocks; boundary state exists only after block two.
        view.groups = {
            "0": ("full_attention", None, set(blocks)),
            "1": ("mamba", None, {blocks[1]}),
        }
        self.assertEqual(self.router.sizer.prefix_cache_evidence({}, prompt)[0], {})
        view.groups["1"] = ("mamba", None, {blocks[0]})
        self.assertEqual(self.router.sizer.prefix_cache_evidence({}, prompt)[0], {self.first: 4})

    def test_token_ids_outside_the_identity_range_carry_no_evidence(self):
        prompt = list(range(16))
        self.hold(self.first, prompt)
        self.assertEqual(
            self.router.sizer.prefix_cache_evidence({}, [*prompt[:8], 1 << 64, *prompt[9:]]),
            ({}, {}, {}),
        )

    def test_shared_estimate_falls_back_to_cold_pricing(self):
        fitted = warm("e0")
        request = Request("r", 40, cached_tokens={"e0": 32})
        self.assertAlmostEqual(prefill_seconds(fitted, request), fitted.cached_prefill_time(32, 8))
        self.assertLess(prefill_seconds(fitted, request), fitted.prefill_time(40))
        for row, cached in (
            (profile("e0"), {"e0": 32}),
            (fitted, {"e1": 32}),
            (fitted, {"e0": 128}),
        ):
            with self.subTest(cached=cached):
                req = Request("r", 200, cached_tokens=cached)
                self.assertEqual(prefill_seconds(row, req), row.prefill_time(200))

    def test_placement_follows_cache_evidence_inside_eligibility(self):
        scheduler = self.router.scheduler
        role = scheduler.monitor.instances[self.first].role
        for iid in (self.first, self.second):
            put_warm(scheduler.profiles, iid)
            scheduler.monitor.instances[iid].role = role
        cold = Request("cold", 40)
        placed = scheduler.schedule(cold)
        self.assertIsNone(cold.cache_placement)
        other = self.second if placed.iid == self.first else self.first
        evidence = Request("warm", 40, cached_tokens={other: 32})
        self.assertEqual(scheduler.schedule(evidence).iid, other)
        record = evidence.cache_placement
        self.assertEqual((record["placed_iid"], record["cold_choice_iid"]), (other, placed.iid))
        self.assertEqual(record["placed_cached_tokens"], 32)
        self.assertIsNone(record["evidence_sequence"])
        sequenced = Request("seq", 40, cached_tokens={other: 32}, cache_sequences={other: 7})
        scheduler.schedule(sequenced)
        self.assertEqual(sequenced.cache_placement["evidence_sequence"], 7)
        self.assertLess(record["predicted_prefill_s"], record["cold_prefill_s"])
        # Cache evidence cannot place a request on an ejected or excluded engine.
        scheduler.eject(other, "liveness")
        held = Request("held", 40, cached_tokens={other: 32})
        self.assertEqual(scheduler.schedule(held).iid, placed.iid)
        with self.assertRaisesRegex(RuntimeError, "no schedulable instances"):
            scheduler.schedule(Request("x", 40, cached_tokens={other: 32}), exclude={placed.iid})

    def test_cold_choice_comes_from_the_placement_candidates(self):
        scheduler = self.router.scheduler
        role = scheduler.monitor.instances[self.first].role
        for iid in (self.first, self.second):
            put_warm(scheduler.profiles, iid)
            scheduler.monitor.instances[iid].role = role
        cold_iid = scheduler.schedule(Request("cold", 40)).iid
        other = self.second if cold_iid == self.first else self.first
        retry = Request("retry", 40, cached_tokens={other: 32, cold_iid: 32})
        self.assertEqual(scheduler.schedule(retry, exclude={cold_iid}).iid, other)
        self.assertEqual(retry.cache_placement["cold_choice_iid"], other)


class PlacementRecheckTests(unittest.TestCase):
    """Placement prices the evidence current residency supports, not what sizing saw."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        self.scheduler = self.router.scheduler
        self.first, self.second = (spec.iid for spec in self.cfg.engines)
        role = self.scheduler.monitor.instances[self.first].role
        for iid in (self.first, self.second):
            put_warm(self.scheduler.profiles, iid)
            self.scheduler.monitor.instances[iid].role = role
        self.namespace = CacheNamespace(self.cfg.model, self.cfg.engine_contract.fingerprint())
        self.prompt = list(range(40))
        # Hold the whole prompt on the engine cold pricing would not choose.
        cold_iid = self.scheduler.schedule(Request("cold", 40)).iid
        self.warm_iid = self.second if cold_iid == self.first else self.first
        self.view = self.router.residency.view(self.warm_iid)
        self.blocks = hold_prefix(self.view, self.namespace, self.prompt, BLOCK, sequence=5)

    def sized(self):
        cached, sequences, identities = self.router.sizer.prefix_cache_evidence({}, self.prompt)
        return Request(
            "r",
            len(self.prompt),
            cached_tokens=cached,
            cache_sequences=sequences,
            cache_identities=identities,
        )

    def test_placement_uses_evidence_that_is_still_resident(self):
        request = self.sized()
        self.assertEqual(request.cached_tokens, {self.warm_iid: 36})
        self.view.sequence = 9
        self.assertEqual(self.scheduler.schedule(request).iid, self.warm_iid)
        self.assertEqual(request.cache_placement["evidence_sequence"], 9)

    def test_eviction_after_sizing_shrinks_the_evidence(self):
        request = self.sized()
        self.view.groups["0"][2].difference_update(self.blocks[2:])
        self.scheduler.schedule(request)
        self.assertEqual(request.cached_tokens, {self.warm_iid: 8})

    def test_reset_restart_or_unknown_view_prices_cold(self):
        for stale in (
            lambda: self.view.groups["0"][2].clear(),
            lambda: self.view.forget("engine restarted"),
            lambda: setattr(self.view, "groups", {"0": ("full_attention", None, set())}),
            lambda: setattr(self.view, "block_size", BLOCK * 2),
        ):
            with self.subTest(stale=stale):
                self.setUp()
                request = self.sized()
                stale()
                placed = self.scheduler.schedule(request)
                self.assertEqual(request.cached_tokens, {})
                self.assertEqual(request.cache_sequences, {})
                self.assertIsNone(request.cache_placement)
                self.assertNotEqual(placed.iid, self.warm_iid)

    def test_offered_demand_follows_evidence_that_eviction_removes(self):
        """Offered prefill demand prices cold once placement finds the prefix evicted."""
        demand = self.router.controller.demand
        now = self.router._clock()
        request = self.sized()
        observation = demand.saw_arrival(len(self.prompt), wanted_len=4, at=now)
        request.demand_arrival = demand.resize_arrival(
            observation, len(self.prompt), 4, at=now, cached_tokens=request.cached_tokens
        )
        request.arrived_at = now
        warm = demand.estimate(now, window_s=60.0, step_s=1.0)[0]
        self.view.forget("engine restarted")
        self.scheduler.schedule(request)
        cold = demand.estimate(now, window_s=60.0, step_s=1.0)[0]
        self.assertGreater(cold, warm)
        self.assertEqual([row.value for row in demand.arrivals.rows()], [Arrival(len(self.prompt))])
        self.assertEqual(demand.arrival_count(), 1)
        self.scheduler.schedule(request)
        self.assertEqual(demand.arrival_count(), 1)

    def test_a_retry_placed_cold_clears_the_earlier_placement_record(self):
        request = self.sized()
        self.scheduler.schedule(request)
        self.assertEqual(request.cache_placement["placed_iid"], self.warm_iid)
        self.view.forget("engine restarted")
        self.scheduler.schedule(request)
        self.assertIsNone(request.cache_placement)

    def test_projections_price_waiting_work_with_current_evidence(self):
        controller = self.router.controller
        now = self.router._clock()
        request = self.sized()
        request.arrived_at = now
        self.scheduler.monitor.waiting[request.rid] = request
        warm_price = self.scheduler.profiles.get(self.warm_iid).cached_prefill_time(36, 4)
        cold_price = self.scheduler.profiles.get(self.warm_iid).prefill_time(40)

        def captured():
            controller._demand(now)
            controller.scorer.recheck(self.scheduler.monitor.waiting.values())
            return controller.scorer.capture(
                now,
                controller.last_demand,
                utilization=controller.utilization,
                observed_load=(0.0, 0.0),
                window_s=controller.window_s,
                step_s=controller.step_s,
            )

        self.assertAlmostEqual(controller.scorer.project_prefill(now).queued_prefill_s, warm_price)
        self.assertAlmostEqual(captured().queued_prefill_s, warm_price)
        self.view.forget("engine restarted")
        self.assertAlmostEqual(controller.scorer.project_prefill(now).queued_prefill_s, cold_price)
        self.assertAlmostEqual(captured().queued_prefill_s, cold_price)
        self.assertEqual(request.cached_tokens, {})

    def test_the_reactive_step_captures_rechecked_waiting_evidence(self):
        controller = self.router.controller
        demand = controller.demand
        now = self.router._clock()
        request = self.sized()
        observation = demand.saw_arrival(len(self.prompt), wanted_len=4, at=now)
        request.demand_arrival = demand.resize_arrival(
            observation, len(self.prompt), 4, at=now, cached_tokens=request.cached_tokens
        )
        request.arrived_at = now
        self.scheduler.monitor.waiting[request.rid] = request
        self.view.forget("engine restarted")
        controller._last_step -= controller.step_s
        captured = []
        capture = controller.scorer.capture

        def spy(*args, **kwargs):
            captured.append(capture(*args, **kwargs))
            return captured[-1]

        with patch.object(controller.scorer, "capture", side_effect=spy):
            controller.step()
        cold_price = self.scheduler.profiles.get(self.warm_iid).prefill_time(40)
        self.assertEqual(len(captured), 1)
        self.assertAlmostEqual(captured[0].queued_prefill_s, cold_price)
        self.assertEqual(request.cached_tokens, {})
        self.assertEqual([row.value for row in demand.arrivals.rows()], [Arrival(len(self.prompt))])
        [(_, option)] = captured[0].demand_options
        self.assertAlmostEqual(controller.last_demand.prefill_engines, option.prefill_engines)

    def test_projections_recheck_waiting_evidence_once_per_interval(self):
        """Projections reuse fresh evidence; placement and unknown views recheck at once."""
        clock = [1000.0]
        self.router._clock = lambda: clock[0]
        scorer = self.router.controller.scorer
        warm_price = self.scheduler.profiles.get(self.warm_iid).cached_prefill_time(36, 4)
        cold_price = self.scheduler.profiles.get(self.warm_iid).prefill_time(40)
        request = self.sized()
        request.arrived_at = request.cache_checked_at = clock[0]
        self.scheduler.monitor.waiting[request.rid] = request
        self.view.groups["0"][2].clear()
        self.assertAlmostEqual(scorer.project_prefill(clock[0]).queued_prefill_s, warm_price)
        clock[0] += CACHE_RECHECK_S
        self.assertAlmostEqual(scorer.project_prefill(clock[0]).queued_prefill_s, cold_price)
        self.assertEqual(request.cached_tokens, {})

        self.setUp()
        clock = [1000.0]
        self.router._clock = lambda: clock[0]
        request = self.sized()
        request.arrived_at = request.cache_checked_at = clock[0]
        self.scheduler.monitor.waiting[request.rid] = request
        self.view.forget("engine restarted")
        scorer = self.router.controller.scorer
        self.assertAlmostEqual(scorer.project_prefill(clock[0]).queued_prefill_s, cold_price)

        self.setUp()
        request = self.sized()
        request.cache_checked_at = self.router._clock()
        self.view.groups["0"][2].clear()
        self.scheduler.schedule(request)
        self.assertEqual(request.cached_tokens, {})

    def test_projections_price_resident_work_with_its_placement_evidence(self):
        now = self.router._clock()
        request = self.sized()
        self.scheduler.monitor.dispatched(self.warm_iid, request)
        self.scheduler.monitor.waiting["w"] = Request("w", 40, arrived_at=now)
        projection = self.router.controller.scorer.project_prefill(now)
        warm_price = self.scheduler.profiles.get(self.warm_iid).cached_prefill_time(36, 4)
        self.assertAlmostEqual(projection.resident_prefill_s, warm_price)

    def test_long_prompts_hash_in_a_worker_thread(self):
        long = list(range(sizing.HASH_THREAD_TOKENS + 8))
        hold_prefix(self.view, self.namespace, long, BLOCK)
        for prompt, threaded in ((long, True), (self.prompt, False)):
            with (
                self.subTest(tokens=len(prompt)),
                patch.object(sizing.asyncio, "to_thread", wraps=asyncio.to_thread) as spy,
            ):
                evidence = asyncio.run(self.router.sizer._cache_evidence({}, prompt))
                self.assertEqual(spy.called, threaded)
                self.assertEqual(evidence, self.router.sizer.prefix_cache_evidence({}, prompt))
                self.assertTrue(evidence[0])


class SharedCostContractTests(unittest.TestCase):
    """Placement, resident work, load, refusal pricing and demand agree on one estimate."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        self.scheduler = self.router.scheduler
        self.scheduler.recheck_cache_evidence = None
        self.iid = self.cfg.engines[0].iid
        for spec in self.cfg.engines:
            put_warm(self.scheduler.profiles, spec.iid)
            self.scheduler.monitor.instances[spec.iid].role = self.scheduler.monitor.instances[
                self.iid
            ].role

    def test_resident_work_load_and_refusal_use_the_placed_engines_evidence(self):
        profile = self.scheduler.profiles.get(self.iid)
        request = Request("r", 40, cached_tokens={self.iid: 32})
        expected = prefill_seconds(profile, request)
        self.assertLess(expected, profile.prefill_time(40))
        self.scheduler.monitor.dispatched(self.iid, request)
        inst = self.scheduler.monitor.instances[self.iid]
        self.assertAlmostEqual(self.scheduler.monitor._prices[self.iid].current, expected)
        self.assertAlmostEqual(
            self.scheduler.prefill_load(inst), expected / self.cfg.slo.ttft_s, places=9
        )
        self.assertAlmostEqual(self.scheduler.cheapest_own_prefill(request), expected)
        self.assertAlmostEqual(self.scheduler.cost(request, inst)[1], 2 * expected)
        self.assertAlmostEqual(
            self.scheduler.cost(request, inst, warm=False)[1], 2 * profile.prefill_time(40)
        )

    def test_demand_prices_cached_arrivals_on_their_cheapest_engine(self):
        demand = self.router.controller.demand
        profiles = self.scheduler.profiles.all_profiles()
        now = self.router._clock()

        def offered(cached):
            model = type(demand)(
                demand.monitor, demand.scheduler, demand._clock, window_s=60.0, bucket_s=1.0
            )
            observation = model.saw_arrival(40, wanted_len=4, at=now)
            model.resize_arrival(observation, 40, 4, at=now, cached_tokens=cached)
            return model.price_profiles(
                now, window_s=60.0, step_s=1.0, profiles=profiles
            ).prefill_engines

        self.assertLess(offered({self.iid: 32}), offered({}))
        self.assertAlmostEqual(
            offered({self.iid: 32}) / offered({}),
            prefill_seconds(profiles[0], Request("r", 40, cached_tokens={profiles[0].iid: 32}))
            / (sum(p.prefill_time(40) for p in profiles) / len(profiles)),
        )

    def test_demand_takes_warm_prices_only_from_engines_that_run_the_prefill(self):
        """A prefix cached on a decode engine leaves offered prefill demand at the cold price."""
        demand = self.router.controller.demand
        instances = self.scheduler.monitor.instances
        decode_iid = next(iid for iid in instances if iid != self.iid)
        instances[decode_iid].role = Role.DECODE
        instances[self.iid].role = Role.PREFILL
        now = self.router._clock()
        observation = demand.saw_arrival(40, wanted_len=4, at=now)
        demand.resize_arrival(observation, 40, 4, at=now, cached_tokens={decode_iid: 32})
        cached_on_decode = demand.estimate(now, window_s=60.0, step_s=1.0)[0]
        demand.resize_arrival(
            demand.saw_arrival(40, wanted_len=4, at=now), 40, 4, at=now, cached_tokens={}
        )
        both = demand.estimate(now, window_s=60.0, step_s=1.0)[0]
        self.assertAlmostEqual(both, 2 * cached_on_decode)
        split = self.router.controller.scorer.capture(
            now,
            demand.last_demand,
            utilization={},
            observed_load=(0.0, 0.0),
            window_s=60.0,
            step_s=1.0,
        )
        priced = dict(split.demand_options)
        self.assertAlmostEqual(priced[1].prefill_engines, both)

    def test_demand_keeps_cold_pricing_for_evidence_without_a_warm_price(self):
        """Evidence on an engine without a warm fit, or outside its domain, leaves demand cold."""
        demand = self.router.controller.demand
        now = self.router._clock()
        cold_only = replace(
            self.scheduler.profiles.get(self.iid),
            ttft_a=2e-6,
            **{f"cached_{k}": None for k in ("ttft_a", "ttft_b", "ttft_c", "ttft_d", "cv_mape")},
            **{
                f"cached_{k}_tokens": None
                for k in ("min_prefix", "max_prefix", "min_suffix", "max_suffix")
            },
        )
        other = next(p for p in self.scheduler.profiles.all_profiles() if p.iid != self.iid)
        profiles = (cold_only, other)

        def offered(cached, length=40):
            model = type(demand)(
                demand.monitor, demand.scheduler, demand._clock, window_s=60.0, bucket_s=1.0
            )
            observation = model.saw_arrival(length, wanted_len=4, at=now)
            model.resize_arrival(observation, length, 4, at=now, cached_tokens=cached)
            return model.price_profiles(
                now, window_s=60.0, step_s=1.0, profiles=profiles
            ).prefill_engines

        self.assertAlmostEqual(offered({self.iid: 32}), offered({}))
        self.assertAlmostEqual(offered({other.iid: 128}, 200), offered({}, 200))
        self.assertLess(offered({other.iid: 32}), offered({}))

    def test_released_reservation_returns_the_engine_to_its_idle_price(self):
        request = Request("r", 40, cached_tokens={self.iid: 32})
        self.scheduler.monitor.dispatched(self.iid, request)
        self.assertGreater(self.scheduler.monitor._prices[self.iid].current, 0.0)
        self.scheduler.monitor.finished(self.iid, "r")
        self.assertEqual(self.scheduler.monitor._prices[self.iid].current, 0.0)

    def test_role_change_keeps_resident_evidence_and_retries_use_their_engine(self):
        """Resident work keeps its price across a role change; a retry uses the new engine."""
        scheduler = self.scheduler
        other = next(spec.iid for spec in self.cfg.engines if spec.iid != self.iid)
        request = Request("r", 40, cached_tokens={self.iid: 32, other: 16})
        placed = scheduler.schedule(request)
        self.assertEqual(placed.iid, self.iid)
        scheduler.monitor.dispatched(self.iid, request)
        price = scheduler.monitor._prices[self.iid].current
        scheduler.monitor.instances[self.iid].role = Role.DECODE
        scheduler.monitor._reprice(self.iid)
        self.assertIn("r", scheduler.monitor.instances[self.iid].prefill)
        self.assertAlmostEqual(scheduler.monitor._prices[self.iid].current, price)
        later = Request("later", 40, cached_tokens={self.iid: 32})
        self.assertEqual(scheduler.schedule(later).iid, other)
        self.assertEqual(later.cache_placement["placed_cached_tokens"], 0)
        retry = scheduler.schedule(request, exclude={self.iid})
        self.assertEqual(retry.iid, other)
        self.assertEqual(request.cache_placement["placed_cached_tokens"], 16)
