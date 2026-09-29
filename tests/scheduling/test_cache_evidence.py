"""Check prefix-cache evidence at sizing, the shared prefill estimate and advisory proposals."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from narwhal.engines.prefix import CacheNamespace, block_identities
from narwhal.scheduling.costs import prefill_seconds
from narwhal.serving.app import create_app
from narwhal.types import Request
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
        cached = self.router.prefix_cache_tokens({"prompt": prompt}, prompt)
        self.assertEqual(cached, {self.first: 16, self.second: 8})
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

    def test_advisory_proposal_leaves_cold_placement_unchanged(self):
        scheduler = self.router.scheduler
        for iid in (self.first, self.second):
            scheduler.profiles.put(
                warm(iid, generation_digest=scheduler.profiles.get(iid).generation_digest)
            )
            scheduler.monitor.instances[iid].role = scheduler.monitor.instances[self.first].role
        cold = Request("cold", 40)
        placed = scheduler.schedule(cold)
        self.assertIsNone(cold.cache_proposal)
        other = self.second if placed.iid == self.first else self.first
        evidence = Request("warm", 40, cached_tokens={other: 32})
        self.assertEqual(scheduler.schedule(evidence).iid, placed.iid)
        proposal = evidence.cache_proposal
        self.assertEqual((proposal["proposed_iid"], proposal["placed_iid"]), (other, placed.iid))
        self.assertEqual(proposal["proposed_cached_tokens"], 32)
        self.assertLess(proposal["proposed_prefill_s"], proposal["placed_cold_prefill_s"])
        self.assertEqual(proposal["placed_cold_prefill_s"], proposal["placed_warm_prefill_s"])
