"""Check warm prefill measurement, fitting and its cold fallback."""

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.profiling import probe
from narwhal.profiling.fitting import fit_cached_prefill
from tests.fixtures import profile

A, B, C = 2e-9, 1e-5, 0.07


def warm_time(prefix, suffix):
    return A * (2 * prefix * suffix + suffix * suffix) + B * suffix + C


def warm_samples(prefixes=(1024, 4096), suffixes=(256, 2048), repeats=3):
    return [
        {
            "target_prefix": p,
            "target_suffix": s,
            "repeat": r,
            "state": state,
            "cache_evidence": "prefix_cache_hits",
            "prefix_tokens": p if state == "warm" else 0,
            "suffix_tokens": s if state == "warm" else p + s,
            "seconds": warm_time(p, s) if state == "warm" else warm_time(0, p + s),
        }
        for p in prefixes
        for s in suffixes
        for r in range(repeats)
        for state in ("warm", "cold")
    ]


class CachedPrefillFitTests(unittest.TestCase):
    def test_fit_recovers_attention_to_the_cached_prefix(self):
        points = [(p, s, warm_time(p, s)) for p in (1024, 4096) for s in (256, 2048)]
        (a, b, c), groups, cv_mape = fit_cached_prefill(points)
        self.assertAlmostEqual(a / A, 1, places=4)
        self.assertAlmostEqual(b / B, 1, places=4)
        self.assertAlmostEqual(c, C, places=6)
        self.assertEqual(len(groups), 4)
        self.assertLess(cv_mape, 1e-6)

    def test_fit_needs_two_prefix_and_two_suffix_lengths(self):
        for points in (
            [(1024, s, 0.1) for s in (256, 2048)],
            [(p, 256, 0.1) for p in (1024, 4096)],
            [(0, 256, 0.1), (1024, 256, 0.1), (4096, 2048, 0.2), (1024, 2048, 0.2)],
        ):
            with self.subTest(points=points), self.assertRaises(ValueError):
                fit_cached_prefill(points)

    def test_warm_estimate_stays_inside_its_measured_domain(self):
        cold = profile("e0", ttft_a=1e-9, ttft_b=2e-5, ttft_c=0.08)
        fitted, fit = probe.apply_cached_fit(cold, warm_samples())
        self.assertAlmostEqual(fitted.cached_prefill_time(2048, 1024), warm_time(2048, 1024))
        self.assertIsNone(fitted.cached_prefill_time(8192, 1024))
        self.assertIsNone(fitted.cached_prefill_time(2048, 4096))
        self.assertIsNone(cold.cached_prefill_time(2048, 1024))
        self.assertEqual(fitted.cached_prefill_time(0, 1024), fitted.prefill_time(1024))
        self.assertLess(fit["cv_mape"], 1e-6)
        # Pricing only the suffix on the cold curve misses attention to the cached prefix.
        self.assertGreater(fit["suffix_on_cold_curve_mape"], 0.05)
        with self.assertRaisesRegex(ValueError, "cached prefill fit requires"):
            replace(cold, cached_ttft_a=A)


class CachedPrefillProbeTests(unittest.IsolatedAsyncioTestCase):
    """A fake engine caches whole 16-token blocks per salt and counts reused tokens."""

    def engine(self, *, reuse=True, hybrid=False):
        cache: dict[str, int] = {}
        hits = [0]

        def cached(tokens):
            # A hybrid engine keeps boundary state only inside the prompt's final block.
            if hybrid and tokens % 16 == 0:
                return 0
            return tokens // 16 * 16

        def handle(request):
            if request.url.path == "/metrics":
                return httpx.Response(
                    200, text=f'vllm:prefix_cache_hits_total{{engine="0"}} {hits[0]}\n'
                )
            body = json.loads(request.content)
            tokens = len(body["prompt"].split())
            salt = body["cache_salt"]
            if reuse and salt in cache:
                hits[0] += min(cache[salt], (tokens - 1) // 16 * 16)
            cache.setdefault(salt, cached(tokens))
            return httpx.Response(
                200, json={"usage": {"prompt_tokens": tokens, "completion_tokens": 1}}
            )

        return httpx.MockTransport(handle)

    async def run_probe(self, transport, prefix_lens=(100, 400)):
        sweep = replace(
            probe.Sweep(),
            cached_prefix_lens=prefix_lens,
            cached_suffix_lens=(20, 60),
            cached_repeats=2,
        )

        async def prompt(client, url, model, target, *args, **kwargs):
            return ("w " * target).strip(), target

        async with httpx.AsyncClient(transport=transport) as client:
            with (
                patch.object(probe, "make_prompt", AsyncMock(side_effect=prompt)),
                redirect_stdout(io.StringIO()),
            ):
                return await probe.probe_cached_prefill(
                    client, "http://e", "stub", sweep, probe.VllmDialect()
                )

    async def test_samples_record_observed_cache_state_and_cold_controls(self):
        samples = await self.run_probe(self.engine())
        warm = [s for s in samples if s["state"] == "warm"]
        cold = [s for s in samples if s["state"] == "cold"]
        self.assertEqual((len(warm), len(cold)), (8, 8))
        self.assertEqual({s["prefix_tokens"] for s in warm}, {96, 400})
        self.assertTrue(
            all(
                s["prefix_tokens"] + s["suffix_tokens"] == s["target_prefix"] + s["target_suffix"]
                for s in warm
            )
        )
        self.assertTrue(all(s["prefix_tokens"] == 0 for s in cold))
        self.assertTrue(all(s["cache_evidence"] == "prefix_cache_hits" for s in samples))

    async def test_a_block_aligned_prefix_stays_reusable_on_a_hybrid_engine(self):
        samples = await self.run_probe(self.engine(hybrid=True), prefix_lens=(96, 400))
        warm = {s["prefix_tokens"] for s in samples if s["state"] == "warm"}
        self.assertEqual(warm, {96, 400})

    async def test_an_engine_that_reuses_nothing_keeps_cold_pricing(self):
        self.assertIsNone(await self.run_probe(self.engine(reuse=False)))


class CachedPrefillRefitTests(unittest.TestCase):
    def test_saved_samples_reproduce_the_warm_fit_offline(self):
        base = profile("e0", ttft_a=1e-9, ttft_b=2e-5, ttft_c=0.08)
        base = replace(base, generation_digest="sha256:" + "a" * 64)
        fitted, fit = probe.apply_cached_fit(base, warm_samples())
        prefill = [(float(n), 1e-9 * n * n + 2e-5 * n + 0.08) for n in (256, 1024, 4096)]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "profiles.samples.json"
            source.write_text(
                json.dumps(
                    {
                        "engines": {
                            "e0": {
                                "prefill": prefill,
                                "profile": asdict(fitted),
                                "generation_evidence": {},
                                "cached_prefill": {"samples": warm_samples(), **fit},
                            }
                        }
                    }
                )
            )
            out = Path(folder) / "refit.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(probe.refit_saved_prefill(source, out, {"e0"}), 0)
            row = json.loads(out.read_text())["profiles"][0]
        for name in ("cached_ttft_a", "cached_ttft_b", "cached_ttft_c", "cached_max_prefix_tokens"):
            self.assertAlmostEqual(row[name], getattr(fitted, name), places=12)
        self.assertTrue(re.fullmatch(r"sha256:a{64}", row["generation_digest"]))
