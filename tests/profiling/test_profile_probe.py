"""Check profiling measurements, token counts and output-file protection."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.profiling import probe
from narwhal.profiling.generation import GenerationEvidence
from narwhal.profiling.store import ProfileStore
from tests.fixtures import fleet, invalid_token_choices, profile


class MeasuredStream(httpx.AsyncByteStream):
    """Advance the probe clock at each delivered SSE event."""

    def __init__(self, frames):
        self.frames = frames
        self.now = 0

    async def __aiter__(self):
        for frame in self.frames:
            self.now += 0.25
            if isinstance(frame, Exception):
                raise frame
            payload = frame if isinstance(frame, str) else json.dumps(frame)
            yield f"data: {payload}\n\n".encode()


def token(index, *, finish=None):
    """Build one exact-token decode event."""
    return {"choices": [{"text": "x", "token_ids": [index], "finish_reason": finish}]}


class ProfileProbeTests(unittest.IsolatedAsyncioTestCase):
    """HTTP fixtures exercise measurement validation with controlled token arrivals."""

    async def test_prompt_uses_the_engine_count_after_resizing(self):
        """Prompt resizing records the measured count used as the fit axis."""
        counts = iter((20, 9))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"count": next(counts)})
            )
        ) as client:
            text, count = await probe.make_prompt(client, "http://e", "stub", 10)
        self.assertEqual(count, 9)
        self.assertEqual(len(text), 50)

    async def test_tokenize_failures_abort_measurement(self):
        """Unavailable or invalid exact counts prevent fitting against an estimated axis."""
        for response in (
            httpx.Response(503),
            httpx.Response(200, text="{"),
            httpx.Response(200, json={}),
            httpx.Response(200, json={"count": 0}),
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request, response=response: response)
            ) as client:
                with self.subTest(status=response.status_code), self.assertRaises(RuntimeError):
                    await probe.make_prompt(client, "http://e", "stub", 10)

    async def test_prefill_requires_matching_usage_and_length_finish(self):
        """Prefill samples require matching prompt usage and a length finish after one token."""
        good = {
            "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            "choices": [{"finish_reason": "length"}],
        }
        for body, accepted in (
            (good, True),
            ({**good, "error": "failure"}, False),
            ({**good, "usage": {"prompt_tokens": 5, "completion_tokens": 1}}, False),
            ({**good, "choices": [{"finish_reason": "stop"}]}, False),
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, body=body: httpx.Response(200, json=body)
                )
            ) as client:
                with (
                    patch.object(probe, "make_prompt", AsyncMock(return_value=("prompt", 4))),
                    redirect_stdout(io.StringIO()),
                ):
                    if accepted:
                        samples = await probe.probe_prefill(
                            client, "http://e", "stub", lens=(4,), repeats=2
                        )
                        self.assertEqual([row[0] for row in samples], [4, 4])
                        self.assertTrue(all(row[1] >= 0 for row in samples))
                    else:
                        with self.assertRaisesRegex(RuntimeError, "exact token usage"):
                            await probe.probe_prefill(client, "http://e", "stub", lens=(4,))

    async def test_live_context_bounds_prefill_before_completion(self):
        """Live tokenizer limits select a safe sweep and reject an oversized exact count."""
        sent = []

        def respond(request):
            if request.url.path == "/tokenize":
                return httpx.Response(200, json={"count": 1, "max_model_len": 16384})
            body = json.loads(request.content)
            prompt_tokens = len(body["prompt"])
            sent.append(prompt_tokens)
            if prompt_tokens + body["max_tokens"] > 16384:
                return httpx.Response(400, text="maximum context length is 16384 tokens")
            return httpx.Response(
                200,
                json={
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1},
                    "choices": [{"finish_reason": "length"}],
                },
            )

        async def prompt(client, url, model, target, dialect, chars_per_token):
            return "x" * target, target

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(probe, "make_prompt", side_effect=prompt):
                limit = await probe.engine_context_limit(
                    client, "http://e", "stub", probe.VllmDialect()
                )
                sweep = probe.bounded_sweep(probe.Sweep(), limit)
                with redirect_stdout(io.StringIO()):
                    samples = await probe.probe_prefill(
                        client,
                        "http://e",
                        "stub",
                        sweep.prefill_lens,
                        repeats=1,
                        max_model_len=limit,
                    )
                self.assertEqual(len(samples), len(sweep.prefill_lens))
                self.assertEqual(max(sent), 12288)
                with self.assertRaisesRegex(ValueError, "exceeds.*max_model_len"):
                    await probe.probe_prefill(
                        client, "http://e", "stub", lens=(16384,), repeats=1, max_model_len=limit
                    )
                self.assertEqual(max(sent), 12288)
        self.assertEqual(max(probe.bounded_sweep(probe.Sweep(), 8192).prefill_lens), 4096)

    async def test_tokenizer_must_report_live_context_limit(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"count": 1}))
        ) as client:
            with self.assertRaisesRegex(RuntimeError, "no valid max_model_len"):
                await probe.engine_context_limit(client, "http://e", "stub", probe.VllmDialect())

    def test_generated_sequence_limits_bound_decode_cohorts_before_measurement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profiling-limits.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "narwhal.profiling-limits",
                        "schema_version": 1,
                        "engines": {"e0": 8},
                    }
                )
            )
            limits = probe.load_sequence_limits(path, {"e0"})
            self.assertEqual(
                probe.bounded_sweep(probe.Sweep(), 16384, limits["e0"]).decode_concurrency,
                (1, 4, 8),
            )
            for invalid in ({"e0": 0}, {"other": 8}, {"e0": True}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    path.write_text(
                        json.dumps(
                            {
                                "schema": "narwhal.profiling-limits",
                                "schema_version": 1,
                                "engines": invalid,
                            }
                        )
                    )
                    probe.load_sequence_limits(path, {"e0"})
        with self.assertRaisesRegex(ValueError, "fewer than two"):
            probe.bounded_sweep(probe.Sweep(), 16384, 1)

    async def measure(self, frames, *, cohort=1, tokens=3):
        """Run one real decode probe and expose its final resident counters."""
        stream = MeasuredStream(frames)
        state = {"resident": 0, "requests": 0, "epoch": 0, "cohort": cohort}
        samples = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        ) as client:
            with patch.object(probe, "time", SimpleNamespace(monotonic=lambda: stream.now)):
                try:
                    await probe._one_decode_stream(
                        client, "http://e", "stub", "prompt", 10, state, samples, tokens=tokens
                    )
                finally:
                    self.assertEqual((state["resident"], state["requests"]), (0, 0))
        return samples

    async def test_decode_samples_exact_gaps_and_complete_cohorts(self):
        """Each token interval records the active request and token counts for a full cohort."""
        frames = [token(0), token(1), token(2, finish="length"), "[DONE]"]
        self.assertEqual(await self.measure(frames), [(1.0, 12.0, 0.25), (1.0, 13.0, 0.25)])
        self.assertEqual(await self.measure(frames, cohort=2), [])

    async def test_invalid_token_identity_aborts_the_profile(self):
        """Profiling rejects unidentified output and releases the active cohort counters."""
        for choice in invalid_token_choices():
            with (
                self.subTest(choice=choice),
                self.assertRaisesRegex(RuntimeError, "exact token IDs"),
            ):
                await self.measure([token(0), {"choices": [choice]}, "[DONE]"])

    async def test_decode_errors_release_resident_counters(self):
        """Every malformed, short or failed stream releases its resident contribution."""
        for frames, message in (
            ([token(0)], "incomplete"),
            ([token(0), "{bad"], "malformed SSE"),
            ([token(0), {"error": "failed"}], "returned an error"),
            ([token(0), {"choices": [1]}], "invalid choices"),
            ([{"choices": [{"text": "x"}]}], "exact token IDs"),
            ([{"choices": [{"text": "xx", "token_ids": [1, 2]}]}], "one SSE event"),
            ([token(0, finish="stop")], "forced token limit"),
            ([token(0, finish="length")], "terminal token count"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                await self.measure(frames)
        with self.assertRaises(httpx.ReadError):
            await self.measure([token(0), httpx.ReadError("lost")])

    async def test_decode_sweep_requires_enough_complete_cohort_intervals(self):
        """A completed short stream still needs two intervals per cohort member."""
        frames = [token(0), token(1, finish="length"), "[DONE]"]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=MeasuredStream(frames))
            )
        ) as client:
            with (
                patch.object(probe, "make_prompt", AsyncMock(return_value=("p", 10))),
                self.assertRaisesRegex(RuntimeError, "insufficient complete-cohort"),
            ):
                await probe.probe_decode(
                    client, "http://e", "stub", concurrency=(1,), input_lens=(10,), tokens=2
                )

    async def test_profile_fits_axes_and_retains_raw_evidence(self):
        """Profile construction carries measured bounds and fit errors into the store row."""
        prefill = [(x, 0.001 * x + 0.01) for x in (1, 10, 100)]
        decode = [
            (r, k, 0.001 * r + 0.000001 * k + 0.01) for r in (1, 4, 16) for k in (100, 1000, 10000)
        ]
        evidence = {}
        with (
            patch.object(probe, "probe_prefill", AsyncMock(return_value=prefill)),
            patch.object(probe, "probe_decode", AsyncMock(return_value=decode)),
            patch.object(probe, "kv_capacity", AsyncMock(return_value=100_000)),
            redirect_stdout(io.StringIO()),
        ):
            row = await probe.profile_instance(None, "e", "http://e", "stub", evidence=evidence)
        self.assertEqual((row.decode_min_requests, row.decode_max_requests), (1, 16))
        self.assertEqual((row.decode_min_kv_tokens, row.decode_max_kv_tokens), (100, 10000))
        self.assertAlmostEqual(row.tpot_request_slope, 0.001)
        self.assertAlmostEqual(row.tpot_slope, 0.000001)
        self.assertAlmostEqual(row.decode_cv_mape, 0)
        self.assertEqual(evidence["prefill_fit_points"], prefill)
        self.assertLess(evidence["prefill_fit_mape"], 0.01)
        self.assertEqual(evidence["decode"], decode)

    async def test_run_protects_existing_and_symlink_outputs(self):
        """Profiling rejects existing output files and symlinks before contacting engines."""
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            with self.assertRaises(FileExistsError):
                await probe.run(cfg, None)
            cfg.profiles_path = Path(folder) / "link.json"
            cfg.profiles_path.symlink_to(Path(folder) / "profiles.json")
            with self.assertRaisesRegex(ValueError, "symlink"):
                await probe.run(cfg, None, overwrite=True)

    async def test_run_writes_profile_and_measurement_sidecar(self):
        """A profiling run writes the selected engine's profile and measurement sidecar."""
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            cfg.profiles_path = Path(folder) / "new.json"
            limits_path = Path(folder) / "profiling-limits.json"
            limits_path.write_text(
                json.dumps(
                    {
                        "schema": "narwhal.profiling-limits",
                        "schema_version": 1,
                        "engines": {engine.iid: 8 for engine in cfg.engines},
                    }
                )
            )
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200))
            )

            async def measured(client, iid, url, model, sweep, *args, evidence, **kwargs):
                self.assertEqual(sweep.decode_concurrency, (1, 4, 8))
                evidence["prefill"] = [[10, 0.1]]
                return profile(iid)

            with (
                patch.object(probe.httpx, "AsyncClient", return_value=client),
                patch.object(probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    probe,
                    "read_generation",
                    AsyncMock(
                        return_value=GenerationEvidence("sha256:" + "a" * 64, {"engine": {}})
                    ),
                ),
                patch.object(probe, "profile_instance", side_effect=measured),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(await probe.run(cfg, {"e0"}, limits_path=limits_path), 0)
            self.assertEqual(len(ProfileStore(cfg.profiles_path)), 1)
            self.assertEqual(
                ProfileStore(cfg.profiles_path).get("e0").generation_digest,
                "sha256:" + "a" * 64,
            )
            saved = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            self.assertEqual(saved["engines"]["e0"]["generation_evidence"], {"engine": {}})
            self.assertEqual(saved["engines"]["e0"]["prefill"], [[10, 0.1]])
            self.assertEqual(saved["engines"]["e0"]["max_model_len"], 16384)
            self.assertEqual(saved["engines"]["e0"]["max_num_seqs"], 8)
            self.assertEqual(max(saved["engines"]["e0"]["sweep"]["prefill_lens"]), 12288)

    async def test_run_retains_prefill_samples_when_fit_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            cfg.profiles_path = Path(folder) / "new.json"
            lengths = (256, 512, 1024, 2048, 4096)
            bad = [
                (length, 3.0 if length == 1024 else 0.25 + length * 0.0001) for length in lengths
            ]
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200))
            )
            with (
                patch.object(probe.httpx, "AsyncClient", return_value=client),
                patch.object(probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    probe,
                    "read_generation",
                    AsyncMock(
                        return_value=GenerationEvidence("sha256:" + "a" * 64, {"engine": {}})
                    ),
                ),
                patch.object(probe, "probe_prefill", AsyncMock(return_value=bad)),
                patch.object(probe, "probe_decode", AsyncMock()) as decode,
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(ValueError, "prefill median fit error"),
            ):
                await probe.run(cfg, {"e0"})
            decode.assert_not_awaited()
            self.assertFalse(cfg.profiles_path.exists())
            saved = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            self.assertEqual(saved["engines"]["e0"]["prefill"], [list(row) for row in bad])
            self.assertIn("prefill median fit error", saved["engines"]["e0"]["error"])

    async def test_run_discards_fit_when_engine_restarts_during_sweep(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            cfg.profiles_path = Path(folder) / "new.json"
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200))
            )
            generations = (
                GenerationEvidence("sha256:" + "a" * 64, {"engine": {"start": 100}}),
                GenerationEvidence("sha256:" + "b" * 64, {"engine": {"start": 101}}),
            )
            with (
                patch.object(probe.httpx, "AsyncClient", return_value=client),
                patch.object(probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(probe, "profile_instance", AsyncMock(return_value=profile("e0"))),
                patch.object(probe, "read_generation", AsyncMock(side_effect=generations)),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(
                    ValueError, "e0: engine generation changed during profiling"
                ),
            ):
                await probe.run(cfg, {"e0"})
            self.assertFalse(cfg.profiles_path.exists())
            saved = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            self.assertIn("generation changed", saved["engines"]["e0"]["error"])

    def test_saved_prefill_refit_preserves_decode_and_original_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "profiles.samples.json"
            output = root / "profiles-refit.json"
            fleet_path = root / "fleet.json"
            fleet(root).save(fleet_path)
            lengths = (256, 512, 1024, 2048, 4096)
            raw = [
                [length, elapsed]
                for length in lengths
                for elapsed in (
                    0.25 + length * 0.0001,
                    0.25 + length * 0.0001,
                    19.0 if length == 256 else 0.25 + length * 0.0001,
                )
            ]
            original = {
                "method_version": 1,
                "engines": {
                    iid: {
                        "prefill": raw,
                        "profile": asdict(profile(iid, generation_digest="sha256:" + "a" * 64)),
                        "generation_evidence": {"engine": {}},
                    }
                    for iid in ("e0", "e3")
                },
            }
            source.write_text(json.dumps(original))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    probe.main(
                        [
                            "--fleet",
                            str(fleet_path),
                            "--refit-samples",
                            str(source),
                            "--out",
                            str(output),
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(source.read_text()), original)
            saved = ProfileStore(output)
            self.assertEqual(saved.get("e0").tpot_slope, profile("e0").tpot_slope)
            self.assertAlmostEqual(saved.get("e0").ttft_b, 0.0001)
            sidecar = json.loads(output.with_suffix(".samples.json").read_text())
            self.assertEqual(sidecar["method_version"], 2)
            self.assertEqual(sidecar["engines"]["e3"]["prefill"], raw)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                probe.refit_saved_prefill(source, output, {"e0", "e3"})

            legacy = root / "legacy.samples.json"
            legacy_record = json.loads(source.read_text())
            for row in legacy_record["engines"].values():
                row["profile"].pop("generation_digest")
                row.pop("generation_evidence")
            legacy.write_text(json.dumps(legacy_record))
            with self.assertRaisesRegex(ValueError, "e0: saved samples lack generation evidence"):
                probe.refit_saved_prefill(legacy, root / "legacy-refit.json", {"e0", "e3"})

    def test_kv_capacity_uses_the_smallest_reported_rank(self):
        """The physical bound follows the smallest rank capacity."""
        self.assertEqual(
            probe.parse_kv_capacity(
                'x{kv_cache_size_tokens="1000"} 1\nx{kv_cache_size_tokens="900.0"} 1'
            ),
            900,
        )
        self.assertIsNone(probe.parse_kv_capacity(""))
