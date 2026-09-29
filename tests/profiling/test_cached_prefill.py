"""Check warm prefill measurement, fitting, the cold split step and their cold fallback."""

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
from narwhal.profiling.fitting import fit_cached_prefill, fit_prefill_samples, splits_prefill
from narwhal.profiling.model import Profile
from tests.fixtures import profile

A, B, C, D = 2e-9, 1e-5, 0.07, 4e-6


def warm_time(prefix, suffix):
    return A * (2 * prefix * suffix + suffix * suffix) + B * suffix + D * prefix + C


def warm_samples(prefixes=(1024, 2048, 4096), suffixes=(256, 1024, 2048), repeats=3, noise=None):
    """Samples shaped like the profiler's, optionally scaled by `noise(prefix, suffix)`."""
    scale = noise or (lambda p, s: 1.0)
    return [
        {
            "target_prefix": p,
            "target_suffix": s,
            "repeat": r,
            "state": state,
            "cache_evidence": "prefix_cache_hits",
            "prefix_tokens": p if state == "warm" else 0,
            "suffix_tokens": s if state == "warm" else p + s,
            "seconds": warm_time(p, s) * scale(p, s) if state == "warm" else warm_time(0, p + s),
        }
        for p in prefixes
        for s in suffixes
        for r in range(repeats)
        for state in ("warm", "cold")
    ]


def noisy(p, s):
    return 1.0 + 0.03 * ((p // 1024 + 2 * s // 256) % 3 - 1)


class CachedPrefillFitTests(unittest.TestCase):
    def test_fit_recovers_prefix_reads_and_attention_to_the_cached_prefix(self):
        points = [(p, s, warm_time(p, s)) for p in (1024, 2048, 4096) for s in (256, 1024, 2048)]
        (a, b, c, d), groups, cv_mape = fit_cached_prefill(points)
        self.assertAlmostEqual(a / A, 1, places=4)
        self.assertAlmostEqual(b / B, 1, places=4)
        self.assertAlmostEqual(d / D, 1, places=4)
        self.assertAlmostEqual(c, C, places=6)
        self.assertEqual(len(groups), 9)
        self.assertLess(cv_mape, 1e-6)

    def test_held_out_error_predicts_each_case_from_the_others(self):
        points = [
            (p, s, warm_time(p, s) * noisy(p, s)) for p in (1024, 2048, 4096) for s in (256, 2048)
        ]
        _, _, cv_mape = fit_cached_prefill(points)
        errors = []
        for index, (p, s, y) in enumerate(points):
            (a, b, c, d), _, _ = fit_cached_prefill(points[:index] + points[index + 1 :])
            predicted = a * (2 * p * s + s * s) + b * s + c + d * p
            errors.append(abs(predicted - y) / y)
        self.assertAlmostEqual(cv_mape, sum(errors) / len(errors))
        self.assertGreater(cv_mape, 0.001)

    def test_fit_needs_two_prefix_and_two_suffix_lengths_and_five_cases(self):
        for points in (
            [(1024, s, 0.1) for s in (256, 2048)],
            [(p, 256, 0.1) for p in (1024, 4096)],
            [(p, s, 0.1) for p in (1024, 4096) for s in (256, 2048)],
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
        self.assertGreater(fit["suffix_on_cold_curve_mape"], 0.05)
        self.assertIsNotNone(fit["cold_control_curve_mape"])
        with self.assertRaisesRegex(ValueError, "cached prefill fit requires"):
            replace(cold, cached_ttft_a=A)

    def test_a_suffix_past_a_block_carries_the_measured_split_step(self):
        split, block = 0.06, 512
        points = [
            (p, s, warm_time(p, s) + (split if splits_prefill(s, block) else 0.0))
            for p in (4096, 8192, 11776)
            for s in (128, 300, 600)
        ]
        (_, _, c, d), _, cv_mape = fit_cached_prefill(points, split, block)
        self.assertLess(cv_mape, 1e-6)
        self.assertAlmostEqual(c, C, places=6)
        self.assertAlmostEqual(d / D, 1, places=4)
        self.assertGreater(fit_cached_prefill(points)[2], 0.05)
        cold = profile(
            "e0", ttft_a=1e-9, ttft_b=2e-5, ttft_c=0.08, ttft_block_tokens=block, ttft_split=split
        )
        samples = warm_samples(prefixes=(4096, 8192, 11776), suffixes=(128, 300, 600))
        for sample in samples:
            if sample["state"] == "warm" and splits_prefill(sample["suffix_tokens"], block):
                sample["seconds"] += split
        fitted, fit = probe.apply_cached_fit(cold, samples)
        self.assertLess(fit["cv_mape"], 1e-6)
        self.assertAlmostEqual(fitted.cached_prefill_time(8192, 224), warm_time(8192, 224))
        self.assertAlmostEqual(fitted.cached_prefill_time(8192, 600), warm_time(8192, 600) + split)

    def test_repeats_group_by_target_case_and_a_poor_fit_stays_cold(self):
        cold = profile("e0")
        grid = warm_samples(prefixes=(1024, 4096), suffixes=(256, 2048))
        # Hit counts vary between repeats of one case.
        for index, sample in enumerate(grid):
            if sample["state"] == "warm":
                sample["prefix_tokens"] += 16 * (index % 2)
        with self.assertRaisesRegex(ValueError, "five cases"):
            probe.apply_cached_fit(cold, grid)
        wild = warm_samples(noise=lambda p, s: 3.0 if (p, s) == (2048, 1024) else 1.0)
        with self.assertRaisesRegex(ValueError, "held-out error"):
            probe.apply_cached_fit(cold, wild)


class ColdSplitStepTests(unittest.TestCase):
    def test_fit_measures_the_extra_step_past_the_first_block(self):
        def cold(n):
            return 1e-9 * n * n + 2e-5 * n + 0.07 + (0.06 if n > 512 and n % 512 else 0.0)

        lengths = (256, 700, 1300, 2300, 4096, 4300, 8300)
        (a, b, c, split), _, mape = fit_prefill_samples([(n, cold(n)) for n in lengths], 512)
        self.assertAlmostEqual(split, 0.06, places=6)
        self.assertLess(mape, 1e-6)
        row = replace(profile("e0"), ttft_a=a, ttft_b=b, ttft_c=c)
        row = replace(row, ttft_block_tokens=512, ttft_split=split)
        for n in (300, 512, 513, 1024, 1500):
            self.assertAlmostEqual(row.prefill_time(n), cold(n), places=6)
        one_short = (256, 700, 1300, 2300, 4300, 8300)
        for kept in (one_short, one_short[1:]):
            with self.subTest(lengths=kept):
                self.assertIsNone(fit_prefill_samples([(n, cold(n)) for n in kept], 512)[0][3])
        plain = fit_prefill_samples([(n, cold(n)) for n in one_short])
        self.assertIsNone(plain[0][3])
        self.assertGreater(plain[2], 0.05)
        with self.assertRaisesRegex(ValueError, "go together"):
            replace(profile("e0"), ttft_split=0.06)

    def test_a_one_step_engine_keeps_the_plain_curve(self):
        def cold(n):
            return 1e-9 * n * n + 2e-5 * n + 0.05

        # The short point sits off the curve.
        for offset in (0.95, 1.05):
            samples = [(n, cold(n) * (offset if n == 256 else 1.0)) for n in probe.PREFILL_LENS]
            for block in (16, 512):
                with self.subTest(offset=offset, block=block):
                    (a, b, c, split), _, _ = fit_prefill_samples(samples, block)
                    self.assertIsNone(split)
                    row = replace(profile("e0"), ttft_a=a, ttft_b=b, ttft_c=c)
                    for n in (1024, 4096, 8192):
                        self.assertLess(abs(row.prefill_time(n) - cold(n)) / cold(n), 0.015)

    def test_default_lengths_measure_both_regimes_for_common_block_sizes(self):
        for block in (16, 512):
            with self.subTest(block=block):
                one_step = [n for n in probe.PREFILL_LENS if not splits_prefill(n, block)]
                self.assertGreaterEqual(len(one_step), 2)
        self.assertEqual(list(probe.PREFILL_LENS), sorted(probe.PREFILL_LENS))
        # Two one-step lengths remain in a 4096-token context.
        short = probe.bounded_sweep(replace(probe.Sweep(), decode_input_lens=(512, 1024)), 4096)
        for block in (16, 512):
            with self.subTest(block=block, context=4096):
                one_step = [n for n in short.prefill_lens if not splits_prefill(n, block)]
                self.assertGreaterEqual(len(one_step), 2)

    def test_block_size_comes_from_the_engine_cache_metric(self):
        metrics = 'vllm:cache_config_info{block_size="512",engine="0"} 1.0\n'
        self.assertEqual(probe.parse_cache_block_tokens(metrics), 512)
        self.assertIsNone(probe.parse_cache_block_tokens("vllm:num_requests_running 0\n"))


class WarmSweepBoundsTests(unittest.TestCase):
    def test_bounded_sweep_trims_warm_lengths_and_accepts_empty_lists(self):
        empty = replace(probe.Sweep(), cached_prefix_lens=(), cached_suffix_lens=())
        bounded = probe.bounded_sweep(empty, 16384)
        self.assertEqual((bounded.cached_prefix_lens, bounded.cached_suffix_lens), ((), ()))
        trimmed = probe.bounded_sweep(replace(probe.Sweep(), decode_input_lens=(512, 1024)), 4096)
        self.assertEqual(trimmed.cached_prefix_lens, (2048,))
        self.assertEqual(probe.warm_cases(trimmed, 4096), [(2048, 700), (2048, 1300)])
        self.assertEqual(len(probe.warm_cases(probe.Sweep())), 9)


class CachedPrefillProbeTests(unittest.IsolatedAsyncioTestCase):
    """A fake engine caches whole 16-token blocks per salt and counts reused tokens."""

    def engine(self, *, reuse=True, hybrid=False, leak=False, sent=None, blind_after_cold=False):
        cache: dict[str, int] = {}
        hits = [0]
        # `blind_after_cold`: /metrics fails after the first cold control.
        blind, reused = [False], [False]

        def cached(tokens):
            # Hybrid: boundary state only inside the prompt's final block.
            if hybrid and tokens % 16 == 0:
                return 0
            return tokens // 16 * 16

        def handle(request):
            if request.url.path == "/metrics":
                if blind[0]:
                    return httpx.Response(503)
                return httpx.Response(
                    200, text=f'vllm:prefix_cache_hits_total{{engine="0"}} {hits[0]}\n'
                )
            body = json.loads(request.content)
            tokens = len(body["prompt"].split())
            if request.url.path == "/tokenize":
                return httpx.Response(200, json={"count": tokens, "max_model_len": 1024})
            if sent is not None:
                sent.append(tokens)
            salt = body["cache_salt"]
            if blind_after_cold and reused[0] and salt not in cache:
                blind[0] = True
            reused[0] = salt in cache
            if reuse and salt in cache:
                hits[0] += min(cache[salt], (tokens - 1) // 16 * 16)
            elif leak and cache:
                hits[0] += 16
            cache.setdefault(salt, cached(tokens))
            return httpx.Response(
                200,
                json={
                    "choices": [{"text": "x"}],
                    "usage": {"prompt_tokens": tokens, "completion_tokens": 1},
                },
            )

        return httpx.MockTransport(handle)

    async def run_probe(self, transport, prefix_lens=(100, 400), short=0, **kwargs):
        sweep = replace(
            probe.Sweep(),
            cached_prefix_lens=prefix_lens,
            cached_suffix_lens=(20, 60),
            cached_repeats=2,
        )

        async def prompt(client, url, model, target, *args, **kwargs):
            # `short` models a tokenizer whose resized prompt misses its target.
            return ("w " * (target - short)).strip(), target

        async with httpx.AsyncClient(transport=transport) as client:
            with (
                patch.object(probe, "make_prompt", AsyncMock(side_effect=prompt)),
                redirect_stdout(io.StringIO()),
            ):
                return await probe.probe_cached_prefill(
                    client, "http://e", "stub", sweep, probe.VllmDialect(), **kwargs
                )

    async def test_samples_record_observed_cache_state_and_cold_controls(self):
        samples, reason = await self.run_probe(self.engine())
        self.assertIsNone(reason)
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
        samples, _ = await self.run_probe(self.engine(hybrid=True), prefix_lens=(96, 400))
        warm = {s["prefix_tokens"] for s in samples if s["state"] == "warm"}
        self.assertEqual(warm, {96, 400})

    async def test_a_short_prefix_primer_still_ends_past_its_block_boundary(self):
        for short in (1, 3):
            with self.subTest(short=short):
                samples, reason = await self.run_probe(
                    self.engine(hybrid=True), prefix_lens=(96, 400), short=short, block_tokens=16
                )
                self.assertIsNone(reason)
                warm = {s["prefix_tokens"] for s in samples if s["state"] == "warm"}
                self.assertEqual(warm, {96, 400})

    async def test_a_primer_that_stays_on_a_block_boundary_stops_the_sweep(self):
        samples, reason = await self.run_probe(
            self.engine(hybrid=True), prefix_lens=(96,), short=20, block_tokens=16
        )
        self.assertEqual(samples, [])
        self.assertIn("block boundary", reason)

    async def test_an_engine_that_reuses_nothing_keeps_cold_pricing(self):
        samples, reason = await self.run_probe(self.engine(reuse=False))
        self.assertEqual(samples, [])
        self.assertIn("reused no cached prefix", reason)

    async def test_an_unreadable_counter_after_a_cold_control_ends_the_warm_sweep(self):
        samples, reason = await self.run_probe(self.engine(blind_after_cold=True))
        self.assertEqual(reason, "the prefix-cache hit counter became unreadable")
        self.assertEqual(samples, [])

    async def test_a_cold_control_that_hits_the_cache_fails_the_sweep(self):
        with self.assertRaisesRegex(RuntimeError, "cold control reused"):
            await self.run_probe(self.engine(leak=True))

    async def test_cases_longer_than_the_context_are_skipped(self):
        sent = []
        samples, reason = await self.run_probe(self.engine(sent=sent), max_model_len=440)
        self.assertIsNone(reason)
        self.assertEqual(
            {(s["target_prefix"], s["target_suffix"]) for s in samples},
            {(100, 20), (100, 60), (400, 20)},
        )
        self.assertLessEqual(max(sent), 439)
        samples, reason = await self.run_probe(self.engine(), max_model_len=50)
        self.assertEqual((samples, reason), ([], "every warm case exceeds the engine context"))


class ProfileInstanceWarmTests(unittest.IsolatedAsyncioTestCase):
    async def profile_with(self, sweep_result, *, block=None, prefill=None, **kwargs):
        prefill = prefill or [(float(n), 1e-9 * n * n + 2e-5 * n + 0.08) for n in (256, 1024, 4096)]
        decode = [
            (r, k, 0.001 * r + 0.000001 * k + 0.01) for r in (1, 4, 16) for k in (100, 1000, 10000)
        ]
        evidence = {}
        with (
            patch.object(probe, "probe_prefill", AsyncMock(return_value=prefill)),
            patch.object(probe, "probe_decode", AsyncMock(return_value=decode)),
            patch.object(probe, "kv_capacity", AsyncMock(return_value=100_000)),
            patch.object(probe, "cache_block_tokens", AsyncMock(return_value=block)),
            patch.object(probe, "prefix_cache_hits", AsyncMock(return_value=7)),
            patch.object(
                probe, "probe_cached_prefill", AsyncMock(return_value=sweep_result)
            ) as sweep,
            redirect_stdout(io.StringIO()),
        ):
            row = await probe.profile_instance(
                None, "e0", "http://e", "stub", evidence=evidence, **kwargs
            )
        self.warm_sweeps = sweep.await_count
        return row, evidence

    async def test_a_warm_fit_enters_the_profile_with_its_evidence(self):
        row, evidence = await self.profile_with((warm_samples(), None))
        self.assertAlmostEqual(row.cached_prefill_time(2048, 1024), warm_time(2048, 1024))
        self.assertEqual(len(evidence["cached_prefill"]["samples"]), 54)
        self.assertLess(evidence["cached_prefill"]["cv_mape"], 1e-6)

    async def test_an_engine_without_a_warm_fit_keeps_cold_pricing_with_the_reason(self):
        grid = warm_samples(prefixes=(1024, 4096), suffixes=(256, 2048))
        for result, reason in (
            (([], "case prefix~2048 suffix~700 reused no cached prefix"), "reused no cached"),
            ((grid, None), "five cases"),
        ):
            with self.subTest(reason=reason):
                row, evidence = await self.profile_with(result)
                self.assertIsNone(row.cached_ttft_a)
                self.assertIn(reason, evidence["cached_prefill"]["reason"])
                self.assertEqual(evidence["cached_prefill"]["samples"], result[0])

    async def test_too_few_warm_cases_keep_cold_pricing_before_the_sweep(self):
        for sweep, limit in (
            (replace(probe.Sweep(), cached_prefix_lens=(2048,)), None),
            (replace(probe.Sweep(), cached_prefix_lens=(), cached_suffix_lens=()), None),
            (probe.Sweep(), 4096),
        ):
            with self.subTest(sweep=sweep.cached_prefix_lens, limit=limit):
                row, evidence = await self.profile_with(
                    (warm_samples(), None), sweep=sweep, max_model_len=limit
                )
                self.assertEqual(self.warm_sweeps, 0)
                self.assertIsNone(row.cached_ttft_a)
                self.assertIn("a warm fit needs", evidence["cached_prefill"]["reason"])

    async def test_the_cold_curve_records_the_engine_block_and_split_step(self):
        def cold(n):
            return 1e-9 * n * n + 2e-5 * n + 0.08 + (0.06 if n > 512 and n % 512 else 0.0)

        prefill = [(float(n), cold(n)) for n in (256, 700, 1300, 2300, 4096, 4300)]
        row, evidence = await self.profile_with(([], "none"), block=512, prefill=prefill)
        self.assertEqual(row.ttft_block_tokens, 512)
        self.assertAlmostEqual(row.ttft_split, 0.06, places=6)
        self.assertEqual(evidence["prefill_block_tokens"], 512)
        row, evidence = await self.profile_with(([], "none"), block=512, prefill=prefill[:-2])
        self.assertIsNone(row.ttft_block_tokens)
        self.assertIsNone(row.ttft_split)
        self.assertEqual(evidence["prefill_block_tokens"], 512)


class CachedPrefillRefitTests(unittest.TestCase):
    def refit(self, cached_prefill, base, prefill=None, block=None):
        if prefill is None:
            prefill = [(float(n), 1e-9 * n * n + 2e-5 * n + 0.08) for n in (256, 1024, 4096)]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "profiles.samples.json"
            source.write_text(
                json.dumps(
                    {
                        "engines": {
                            "e0": {
                                "prefill": prefill,
                                "profile": asdict(base),
                                "generation_evidence": {},
                                "cached_prefill": cached_prefill,
                                "prefill_block_tokens": block,
                            }
                        }
                    }
                )
            )
            out = Path(folder) / "refit.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(probe.refit_saved_prefill(source, out, {"e0"}), 0)
            sidecar = json.loads(out.with_suffix(".samples.json").read_text())
            return json.loads(out.read_text())["profiles"][0], sidecar["engines"]["e0"]

    def base(self):
        row = profile("e0", ttft_a=1e-9, ttft_b=2e-5, ttft_c=0.08)
        return replace(row, generation_digest="sha256:" + "a" * 64)

    def test_saved_samples_reproduce_the_warm_fit_offline(self):
        fitted, fit = probe.apply_cached_fit(self.base(), warm_samples())
        # The saved profile has no warm fit.
        row, evidence = self.refit({"samples": warm_samples(), **fit}, self.base())
        for name in (
            "cached_ttft_a",
            "cached_ttft_b",
            "cached_ttft_c",
            "cached_ttft_d",
            "cached_max_prefix_tokens",
        ):
            self.assertAlmostEqual(row[name], getattr(fitted, name), places=12)
        self.assertAlmostEqual(evidence["cached_prefill"]["cv_mape"], fit["cv_mape"])
        self.assertTrue(re.fullmatch(r"sha256:a{64}", row["generation_digest"]))

    def test_refit_uses_the_saved_block_size_for_cold_and_warm_splits(self):
        split, block = 0.06, 512

        def cold(n):
            return 1e-9 * n * n + 2e-5 * n + 0.08 + (split if splits_prefill(n, block) else 0.0)

        prefill = [(float(n), cold(n)) for n in (256, 700, 1024, 1300, 2300, 4096, 4300, 8300)]
        samples = warm_samples(prefixes=(4096, 8192, 11776), suffixes=(128, 300, 600))
        for sample in samples:
            if sample["state"] == "warm" and splits_prefill(sample["suffix_tokens"], block):
                sample["seconds"] += split
        row, evidence = self.refit({"samples": samples}, self.base(), prefill, block)
        self.assertEqual(row["ttft_block_tokens"], block)
        self.assertAlmostEqual(row["ttft_split"], split, places=6)
        refit = Profile(**row)
        self.assertAlmostEqual(refit.prefill_time(4300), cold(4300), places=6)
        self.assertAlmostEqual(
            refit.cached_prefill_time(8192, 600), warm_time(8192, 600) + split, places=6
        )
        self.assertLess(evidence["cached_prefill"]["cv_mape"], 1e-4)

    def test_malformed_saved_warm_samples_are_reported_as_invalid(self):
        good = warm_samples()
        for field, value in (("state", "stale"), ("seconds", "fast")):
            bad = [dict(good[0], **{field: value}), *good[1:]]
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ValueError, "cached prefill samples are invalid"),
            ):
                self.refit({"samples": bad}, self.base())

    def test_refit_keeps_an_engine_cold_when_its_live_warm_sweep_stopped(self):
        # A stopped sweep's samples would still form a fit.
        reason = "case prefix~8192 suffix~700 reused no cached prefix"
        row, evidence = self.refit({"samples": warm_samples(), "reason": reason}, self.base())
        self.assertIsNone(row["cached_ttft_a"])
        self.assertEqual(evidence["cached_prefill"], {"samples": warm_samples(), "reason": reason})

    def test_refit_clears_a_warm_fit_its_samples_no_longer_form(self):
        fitted, _ = probe.apply_cached_fit(self.base(), warm_samples())
        grid = warm_samples(prefixes=(2048,), suffixes=(256, 1024, 2048))
        row, evidence = self.refit({"samples": grid}, fitted)
        for name in probe.CACHED_PROFILE_FIELDS:
            self.assertIsNone(row[name], name)
        self.assertIn("five cases", evidence["cached_prefill"]["reason"])

    def test_refit_keeps_an_engine_cold_when_its_samples_form_no_warm_fit(self):
        grid = warm_samples(prefixes=(2048,), suffixes=(256, 1024, 2048))
        row, evidence = self.refit({"samples": grid, "reason": "fit needs five cases"}, self.base())
        self.assertIsNone(row["cached_ttft_a"])
        self.assertIn("five cases", evidence["cached_prefill"]["reason"])
        with self.assertRaisesRegex(ValueError, "cached prefill samples are invalid"):
            self.refit({"samples": [{"state": "warm"}]}, self.base())
