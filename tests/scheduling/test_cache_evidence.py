"""Check prefix-cache evidence at sizing, the shared prefill estimate and advisory proposals."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from narwhal.engines.prefix import CacheNamespace, block_identities
from narwhal.scheduling.costs import prefill_seconds
from narwhal.serving.app import create_app
from narwhal.types import Request, Role
from tests.fixtures import fleet, profile

BLOCK = 4


def warm(iid, **changes):
    """Return a profile with a warm fit that makes cached prefill cheap."""
    return replace(
        profile(iid, ttft_a=1e-6, ttft_b=0.002, ttft_c=0.01),
        cached_ttft_a=1e-8,
        cached_ttft_b=0.0001,
        cached_ttft_c=0.01,
        cached_ttft_d=0.0,
        cached_cv_mape=0.05,
        cached_min_prefix_tokens=4,
        cached_max_prefix_tokens=64,
        cached_min_suffix_tokens=1,
        cached_max_suffix_tokens=64,
        **changes,
    )


class CacheEvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        self.first, self.second = (spec.iid for spec in self.cfg.engines)
        self.namespace = CacheNamespace(self.cfg.model, self.cfg.engine_contract.fingerprint())

    def hold(self, iid, tokens, *, salt=None):
        """Make one engine's residency view hold every full block of `tokens`."""
        namespace = replace(self.namespace, cache_salt=salt)
        view = self.router.residency.view(iid)
        view.known, view.block_size = True, BLOCK
        view.groups = {
            "0": ("full_attention", None, set(block_identities(namespace, tokens, BLOCK)))
        }

    def test_sizing_reports_each_engines_cached_prompt_tokens(self):
        prompt = list(range(22))
        self.hold(self.first, prompt[:16])
        self.hold(self.second, prompt[:8])
        self.router.residency.view(self.first).sequence = 41
        cached, sequences = self.router.prefix_cache_evidence({"prompt": prompt}, prompt)
        self.assertEqual(cached, {self.first: 16, self.second: 8})
        self.assertEqual(sequences, {self.first: 41})
        # vLLM computes the final prompt token, so a fully cached prompt reuses one block less.
        self.assertEqual(
            self.router.prefix_cache_tokens({"prompt": prompt[:16]}, prompt[:16])[self.first], 12
        )

    def test_evidence_needs_the_same_namespace_text_only_and_known_residency(self):
        prompt = list(range(16))
        self.hold(self.first, prompt, salt="salt")
        self.assertEqual(self.router.prefix_cache_tokens({}, prompt), {})
        self.assertEqual(
            self.router.prefix_cache_tokens({"cache_salt": "salt"}, prompt), {self.first: 12}
        )
        image = {
            "messages": [{"role": "user", "content": [{"type": "image_url"}]}],
            "cache_salt": "salt",
        }
        self.assertEqual(self.router.prefix_cache_tokens(image, prompt), {})
        self.router.residency.view(self.first).known = False
        self.assertEqual(self.router.prefix_cache_tokens({"cache_salt": "salt"}, prompt), {})
        self.router.cfg.engine_contract = None
        self.router.residency.view(self.first).known = True
        self.assertEqual(self.router.prefix_cache_tokens({"cache_salt": "salt"}, prompt), {})

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
            digest = scheduler.profiles.get(iid).generation_digest
            scheduler.profiles.put(warm(iid, generation_digest=digest))
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
        # Cache evidence cannot place work on an engine the scheduler excludes.
        scheduler.eject(other)
        held = Request("held", 40, cached_tokens={other: 32})
        self.assertEqual(scheduler.schedule(held).iid, placed.iid)
        with self.assertRaisesRegex(RuntimeError, "no schedulable instances"):
            scheduler.schedule(Request("x", 40, cached_tokens={other: 32}), exclude={placed.iid})


class SharedCostContractTests(unittest.TestCase):
    """Placement, resident work, load, refusal pricing and demand agree on one estimate."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        self.scheduler = self.router.scheduler
        self.iid = self.cfg.engines[0].iid
        for spec in self.cfg.engines:
            digest = self.scheduler.profiles.get(spec.iid).generation_digest
            self.scheduler.profiles.put(warm(spec.iid, generation_digest=digest))
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

    def test_role_change_keeps_resident_evidence_and_retries_use_their_engine(self):
        """Resident work keeps its price across a role change; a retry uses the new engine."""
        scheduler = self.scheduler
        other = next(spec.iid for spec in self.cfg.engines if spec.iid != self.iid)
        request = Request("r", 40, cached_tokens={self.iid: 32, other: 16})
        placed = scheduler.schedule(request)
        self.assertEqual(placed.iid, self.iid)
        scheduler.monitor.dispatched(self.iid, request)
        price = scheduler.monitor._prices[self.iid].current
        # A role change moves new placement away without dropping resident work or its price.
        scheduler.monitor.instances[self.iid].role = Role.DECODE
        scheduler.monitor._reprice(self.iid)
        self.assertIn("r", scheduler.monitor.instances[self.iid].prefill)
        self.assertAlmostEqual(scheduler.monitor._prices[self.iid].current, price)
        later = Request("later", 40, cached_tokens={self.iid: 32})
        self.assertEqual(scheduler.schedule(later).iid, other)
        self.assertEqual(later.cache_placement["placed_cached_tokens"], 0)
        # A retry excluding the failed engine is priced with its new engine's evidence.
        retry = scheduler.schedule(request, exclude={self.iid})
        self.assertEqual(retry.iid, other)
        self.assertEqual(request.cache_placement["placed_cached_tokens"], 16)
