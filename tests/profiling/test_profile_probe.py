import asyncio
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends import load as load_backend
from narwhal.config.model import SharedDeviceAllocation
from narwhal.profiling.generation import GenerationEvidence
from narwhal.profiling.model import CACHED_PROFILE_FIELDS
from narwhal.profiling.probe import cli as profile_cli
from narwhal.profiling.probe import decode as decode_probe
from narwhal.profiling.probe import fleet as fleet_probe
from narwhal.profiling.probe import instance as instance_probe
from narwhal.profiling.probe import neighbours as neighbours_probe
from narwhal.profiling.probe import prefill as prefill_probe
from narwhal.profiling.probe.decode import probe_decode
from narwhal.profiling.probe.engine import engine_context_limit, make_prompt
from narwhal.profiling.probe.instance import profile_instance
from narwhal.profiling.probe.neighbours import ColocatedWorkload, NeighbourLoad
from narwhal.profiling.probe.offline import merge_profiles, refit_saved_prefill
from narwhal.profiling.probe.pairing import PairedTransport
from narwhal.profiling.probe.prefill import probe_prefill
from narwhal.profiling.probe.sweep import Sweep, bounded_sweep, load_sequence_limits
from narwhal.profiling.store import ProfileStore
from narwhal.runtime.role_switch import RoleSwitcher
from narwhal.types import Role
from tests.fixtures import fleet, invalid_token_choices, profile
from tests.profiling.fixtures import patched_profile_sweeps
from tests.wire import routed
from tools.measurement.simulated_engine import SimulatedEngine

VLLM_METRICS = load_backend("vllm").metrics
DIALECT = load_backend("vllm").dialect
SGLANG_DIALECT = load_backend("sglang").dialect


def measured_store(path, rows):
    store = ProfileStore(path, load=False)
    for row in rows:
        store.put(row)
    path.with_suffix(".samples.json").write_text(
        json.dumps({"engines": {row.iid: {"profile": asdict(row)} for row in rows}})
    )
    return path


class MeasuredStream(httpx.AsyncByteStream):
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
    return {"choices": [{"text": "x", "token_ids": [index], "finish_reason": finish}]}


class ProfileProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_colocated_sizing_uses_observation_timeout(self):
        timeouts = []

        async def answer(request):
            self.assertEqual(request.url.path, "/tokenize")
            timeouts.append(request.extensions["timeout"])
            return httpx.Response(200, json={"count": 32, "max_model_len": 32})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            load = NeighbourLoad(
                client,
                [("p", "http://prefill", Role.PREFILL)],
                "stub",
                DIALECT,
                3.8,
                ColocatedWorkload(100, 100, 32, 32, 8),
                observation_timeout_s=75.0,
            )
            with self.assertRaisesRegex(ValueError, "exceeds max_model_len"):
                await load.start()
        self.assertEqual(len(timeouts), 2)
        for timeout in timeouts:
            self.assertEqual(set(timeout.values()), {75.0})

    async def test_colocated_load_records_completed_peer_traffic(self):
        calls = []

        async def answer(request):
            if request.url.path == "/v1/completions":
                calls.append(request.url.host)
                body = json.loads(request.content)
                return httpx.Response(
                    200, json={"usage": {"completion_tokens": body["max_tokens"]}}
                )
            raise AssertionError(request.url.path)

        workload = ColocatedWorkload(40.0, 40.0, 256, 512, 8)
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            load = NeighbourLoad(
                client,
                [("p", "http://prefill", Role.PREFILL), ("d", "http://decode", Role.DECODE)],
                "stub",
                DIALECT,
                3.8,
                workload,
            )
            with (
                patch.object(
                    neighbours_probe, "make_prompt", AsyncMock(return_value=("prompt", 32))
                ),
                patch.object(
                    neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096)
                ),
            ):
                await load.start()
            await asyncio.sleep(0.08)
            measured = await load.stop()
        self.assertIn("prefill", calls)
        self.assertIn("decode", calls)
        self.assertGreater(measured["prefill_rps"], 0)
        self.assertGreater(measured["decode_rps"], 0)
        self.assertLess(measured["completed_prefill"] + measured["completed_decode"], len(calls))
        for iid in ("p", "d"):
            self.assertGreater(measured["peers"][iid]["completed"], 0)
            self.assertGreater(measured["peers"][iid]["rps"], 0)
            self.assertIsNone(measured["peers"][iid]["error"])

    async def test_colocated_load_requires_completions_from_each_same_role_peer(self):
        good_completed = asyncio.Event()

        async def answer(request):
            if request.url.host == "stalled":
                await asyncio.Event().wait()
            good_completed.set()
            return httpx.Response(200, json={"usage": {"completion_tokens": 8}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            load = NeighbourLoad(
                client,
                [("d1", "http://good", Role.DECODE), ("d2", "http://stalled", Role.DECODE)],
                "stub",
                DIALECT,
                3.8,
                ColocatedWorkload(100, 100, 32, 32, 8),
            )
            with (
                patch.object(
                    neighbours_probe, "make_prompt", AsyncMock(return_value=("prompt", 32))
                ),
                patch.object(
                    neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096)
                ),
            ):
                await load.start()
            good_completed.clear()
            await asyncio.wait_for(good_completed.wait(), timeout=1)
            with self.assertRaisesRegex(RuntimeError, "d2: completed 0 requests"):
                await load.stop()
        measured = load.evidence()
        self.assertGreater(measured["peers"]["d1"]["completed"], 0)
        self.assertEqual(measured["peers"]["d2"]["completed"], 0)
        self.assertIn("completed 0 requests", measured["peers"]["d2"]["error"])
        self.assertTrue(all(task.done() for task in load.tasks))

    async def test_colocated_load_retains_a_peer_task_exception(self):
        fail = False
        failed = asyncio.Event()

        async def answer(request):
            if fail and request.url.host == "failing":
                failed.set()
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"usage": {"completion_tokens": 8}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            load = NeighbourLoad(
                client,
                [("d1", "http://good", Role.DECODE), ("d2", "http://failing", Role.DECODE)],
                "stub",
                DIALECT,
                3.8,
                ColocatedWorkload(100, 100, 32, 32, 8),
            )
            with (
                patch.object(
                    neighbours_probe, "make_prompt", AsyncMock(return_value=("prompt", 32))
                ),
                patch.object(
                    neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096)
                ),
            ):
                await load.start()
            await asyncio.sleep(0.02)
            fail = True
            await asyncio.wait_for(failed.wait(), timeout=1)
            with self.assertRaisesRegex(RuntimeError, "d2: AttributeError"):
                await load.stop()
        measured = load.evidence()
        self.assertGreater(measured["peers"]["d2"]["completed"], 0)
        self.assertIn("AttributeError", measured["peers"]["d2"]["error"])

    async def test_merge_keeps_both_measured_role_mixes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sources = [root / "one.json", root / "two.json"]
            for path, mix in zip(sources, ((1, 2), (2, 1)), strict=True):
                row = replace(
                    profile("e0"),
                    colocated_group="gpu-0",
                    colocated_target_role="prefill",
                    colocated_prefill_engines=mix[0],
                    colocated_decode_engines=mix[1],
                    colocated_prefill_rps=1.0,
                    colocated_decode_rps=1.0,
                )
                ProfileStore(path, load=False).put(row)
                path.with_suffix(".samples.json").write_text(
                    json.dumps({"engines": {"e0": {"profile": asdict(row)}}})
                )
            output = root / "merged.json"
            self.assertEqual(merge_profiles(sources, output, {"e0"}), 0)
            merged = ProfileStore(output)
            merged.bind_role_mix({"e0": "gpu-0"}, lambda group: (1, 2), lambda iid: Role.PREFILL)
            self.assertEqual(merged.profiles_for_split(["e0"], 1, 2)[0].colocated_group, "gpu-0")
            self.assertEqual(merged.profiles_for_split(["e0"], 2, 1)[0].colocated_group, "gpu-0")
            self.assertEqual(len(merged.all_profiles()), 2)

    async def test_merge_accepts_evidence_saved_before_optional_profile_fields(self):
        newer = ("ttft_block_tokens", "ttft_split", *CACHED_PROFILE_FIELDS)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sources = [root / "one.json", root / "two.json"]
            rows = []
            for path, mix in zip(sources, ((1, 2), (2, 1)), strict=True):
                row = replace(
                    profile("e0"),
                    colocated_group="gpu-0",
                    colocated_target_role="prefill",
                    colocated_prefill_engines=mix[0],
                    colocated_decode_engines=mix[1],
                    colocated_prefill_rps=1.0,
                    colocated_decode_rps=1.0,
                )
                rows.append(row)
                ProfileStore(path, load=False).put(row)
                saved = {k: v for k, v in asdict(row).items() if k not in newer}
                path.with_suffix(".samples.json").write_text(
                    json.dumps({"engines": {"e0": {"profile": saved}}})
                )
            self.assertEqual(merge_profiles(sources, root / "merged.json", {"e0"}), 0)
            # Evidence from another measurement stops the merge.
            stale = {**asdict(rows[0]), "ttft_c": rows[0].ttft_c + 1.0}
            sources[0].with_suffix(".samples.json").write_text(
                json.dumps({"engines": {"e0": {"profile": stale}}})
            )
            with self.assertRaisesRegex(ValueError, "lacks matching measurement evidence"):
                merge_profiles(sources, root / "again.json", {"e0"})

    async def test_merge_replaces_a_remeasured_engine_and_keeps_the_others(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fresh = replace(profile("e3"), ttft_c=profile("e3").ttft_c + 1.0)
            base = measured_store(root / "base.json", [profile("e0"), profile("e3")])
            update = measured_store(root / "e3.json", [fresh])
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(merge_profiles([base, update], root / "one.json", {"e0", "e3"}), 0)
            self.assertIn(f"e3: {update} replaces the profile from {base}", output.getvalue())
            merged = ProfileStore(root / "one.json")
            self.assertEqual((merged.get("e0"), merged.get("e3")), (profile("e0"), fresh))
            again = replace(profile("e0"), ttft_c=profile("e0").ttft_c + 2.0)
            later = measured_store(root / "e0.json", [again])
            with redirect_stdout(io.StringIO()):
                merge_profiles([root / "one.json", later], root / "two.json", {"e0", "e3"})
            merged = ProfileStore(root / "two.json")
            self.assertEqual((merged.get("e0"), merged.get("e3")), (again, fresh))

    async def test_merge_leaves_out_engines_outside_the_fleet(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rows = [profile("e0"), profile("e3"), profile("e4")]
            base = measured_store(root / "base.json", rows)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(merge_profiles([base], root / "out.json", {"e0", "e3"}), 0)
            self.assertIn("left out profiles for engines outside the fleet: e4", output.getvalue())
            self.assertEqual(
                sorted(row.iid for row in ProfileStore(root / "out.json").all_profiles()),
                ["e0", "e3"],
            )

    async def test_merge_refuses_rows_without_their_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = measured_store(root / "base.json", [profile("e0"), profile("e3")])
            update = measured_store(root / "e3.json", [profile("e3")])
            update.with_suffix(".samples.json").write_text(json.dumps({"engines": {}}))
            with self.assertRaisesRegex(ValueError, "profile e3 lacks matching measurement"):
                merge_profiles([base, update], root / "out.json", {"e0", "e3"})
            with redirect_stdout(io.StringIO()):
                merge_profiles([base], root / "merged.json", {"e0", "e3"})
            base.with_suffix(".samples.json").write_text(json.dumps({"engines": {}}))
            with self.assertRaisesRegex(ValueError, "merged source sidecar .* changed"):
                merge_profiles([root / "merged.json"], root / "again.json", {"e0", "e3"})

    def test_cli_merges_one_store_and_writes_a_live_sweep_to_out(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = fleet(root)
            fleet_path = root / "fleet.json"
            cfg.save(fleet_path)
            base = measured_store(root / "base.json", [profile("e0"), profile("e3")])
            with redirect_stdout(io.StringIO()):
                code = profile_cli.main(
                    [
                        "--fleet",
                        str(fleet_path),
                        "--merge",
                        str(base),
                        "--out",
                        str(root / "o.json"),
                    ]
                )
            self.assertEqual(code, 0)
            run = AsyncMock(return_value=0)
            with patch.object(profile_cli, "run", new=run):
                code = profile_cli.main(
                    ["--fleet", str(fleet_path), "--only", "e3", "--out", str(root / "e3.json")]
                )
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[0].profiles_path, root / "e3.json")

    async def test_prompt_uses_the_engine_count_after_resizing(self):
        counts = iter((20, 9))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"count": next(counts)})
            )
        ) as client:
            text, count = await make_prompt(client, "http://e", "stub", 10, DIALECT)
        self.assertEqual(count, 9)
        self.assertEqual(len(text), 50)

    async def test_bounded_prompt_fits_context_with_fixed_prefix_token_cost(self):
        prefix = "0123456789abcdef0123456789abcdef "
        observed_counts = []

        def tokenize(request):
            prompt = json.loads(request.content)["prompt"]
            self.assertTrue(prompt.startswith(prefix))
            count = 20 + (len(prompt) - len(prefix) + 9) // 10
            observed_counts.append(count)
            return httpx.Response(200, json={"count": count})

        async with httpx.AsyncClient(transport=httpx.MockTransport(tokenize)) as client:
            for target in (128, 512, 1020):
                with self.subTest(target=target):
                    observed_counts.clear()
                    text, count = await make_prompt(
                        client,
                        "http://e",
                        "stub",
                        target,
                        DIALECT,
                        prefix=prefix,
                        max_input_tokens=target,
                    )
                    self.assertGreater(observed_counts[1], target)
                    self.assertEqual(count, target)
                    self.assertEqual(count, observed_counts[-1])
                    self.assertTrue(text.startswith(prefix))
                    self.assertLessEqual(count + 4, 1024)

    async def test_bounded_prompt_rejects_prefix_that_cannot_fit(self):
        calls = 0

        def tokenize(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"count": 20})

        async with httpx.AsyncClient(transport=httpx.MockTransport(tokenize)) as client:
            with self.assertRaisesRegex(RuntimeError, "prefix requires 20 tokens"):
                await make_prompt(
                    client, "http://e", "stub", 8, DIALECT, prefix="unique ", max_input_tokens=8
                )
        self.assertLessEqual(calls, 16)

    async def test_tokenize_failures_abort_measurement(self):
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
                    await make_prompt(client, "http://e", "stub", 10, DIALECT)

    async def test_prefill_requires_matching_usage_and_length_finish(self):
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
                    patch.object(
                        prefill_probe, "make_prompt", AsyncMock(return_value=("prompt", 4))
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    if accepted:
                        samples = await probe_prefill(
                            client, "http://e", "stub", DIALECT, lens=(4,), repeats=2
                        )
                        self.assertEqual([row[0] for row in samples], [4, 4])
                        self.assertTrue(all(row[1] >= 0 for row in samples))
                    else:
                        with self.assertRaisesRegex(RuntimeError, "exact token usage"):
                            await probe_prefill(client, "http://e", "stub", DIALECT, lens=(4,))

    async def test_live_context_bounds_prefill_before_completion(self):
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

        async def prompt(client, url, model, target, dialect, chars_per_token, **kwargs):
            return "x" * target, target

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(prefill_probe, "make_prompt", side_effect=prompt):
                limit = await engine_context_limit(client, "http://e", "stub", DIALECT)
                sweep = bounded_sweep(Sweep(), limit)
                with redirect_stdout(io.StringIO()):
                    samples = await probe_prefill(
                        client,
                        "http://e",
                        "stub",
                        DIALECT,
                        sweep.prefill_lens,
                        repeats=1,
                        max_model_len=limit,
                    )
                self.assertEqual(len(samples), len(sweep.prefill_lens))
                self.assertEqual(max(sent), 16300)
                with self.assertRaisesRegex(ValueError, "exceeds.*max_model_len"):
                    await probe_prefill(
                        client,
                        "http://e",
                        "stub",
                        DIALECT,
                        lens=(16384,),
                        repeats=1,
                        max_model_len=limit,
                    )
                self.assertEqual(max(sent), 16300)
        self.assertEqual(max(bounded_sweep(Sweep(), 8192).prefill_lens), 4300)

    async def test_tokenizer_must_report_live_context_limit(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"count": 1}))
        ) as client:
            with self.assertRaisesRegex(RuntimeError, "no valid max_model_len"):
                await engine_context_limit(client, "http://e", "stub", DIALECT)

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
            limits = load_sequence_limits(path, {"e0"})
            self.assertEqual(
                bounded_sweep(Sweep(), 16384, limits["e0"]).decode_concurrency,
                (1, 4, 8),
            )
            for limit, cohorts in ((48, (1, 4, 16, 48)), (64, (1, 4, 16, 48, 64))):
                with self.subTest(limit=limit):
                    self.assertEqual(
                        bounded_sweep(Sweep(), 16384, limit).decode_concurrency, cohorts
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
                    load_sequence_limits(path, {"e0"})
        with self.assertRaisesRegex(ValueError, "fewer than two"):
            bounded_sweep(Sweep(), 16384, 1)

    async def measure(self, frames, *, cohort=1, tokens=3):
        stream = MeasuredStream(frames)
        state = {"resident": 0, "requests": 0, "epoch": 0, "cohort": cohort}
        samples = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        ) as client:
            with patch.object(decode_probe, "time", SimpleNamespace(monotonic=lambda: stream.now)):
                try:
                    await decode_probe._one_decode_stream(
                        client,
                        "http://e",
                        "stub",
                        "prompt",
                        10,
                        state,
                        samples,
                        DIALECT,
                        tokens=tokens,
                    )
                finally:
                    self.assertEqual((state["resident"], state["requests"]), (0, 0))
        return samples

    async def test_decode_samples_exact_gaps_and_complete_cohorts(self):
        frames = [token(0), token(1), token(2, finish="length"), "[DONE]"]
        self.assertEqual(await self.measure(frames), [(1.0, 12.0, 0.25), (1.0, 13.0, 0.25)])
        self.assertEqual(await self.measure(frames, cohort=2), [])

    async def test_bundled_terminal_tokens_keep_counts_without_false_intervals(self):
        frames = [
            token(0),
            token(1),
            {"choices": [{"text": "xx", "token_ids": [2, 3], "finish_reason": "length"}]},
            "[DONE]",
        ]
        self.assertEqual(await self.measure(frames, tokens=4), [(1.0, 12.0, 0.25)])

    async def test_invalid_token_identity_aborts_the_profile(self):
        for choice in invalid_token_choices():
            with (
                self.subTest(choice=choice),
                self.assertRaisesRegex(RuntimeError, "exact token IDs"),
            ):
                await self.measure([token(0), {"choices": [choice]}, "[DONE]"])

    async def test_decode_errors_release_resident_counters(self):
        for frames, message in (
            ([token(0)], "incomplete"),
            ([token(0), "{bad"], "malformed SSE"),
            ([token(0), {"error": "failed"}], "returned an error"),
            ([token(0), {"choices": [1]}], "invalid choices"),
            ([{"choices": [{"text": "x"}]}], "exact token IDs"),
            ([{"choices": [{"text": "xxxx", "token_ids": [1, 2, 3, 4]}]}], "exceeded"),
            ([token(0, finish="stop")], "forced token limit"),
            ([token(0, finish="length")], "terminal token count"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                await self.measure(frames)
        with self.assertRaises(httpx.ReadError):
            await self.measure([token(0), httpx.ReadError("lost")])

    async def test_decode_sweep_requires_enough_complete_cohort_intervals(self):
        frames = [token(0), token(1, finish="length"), "[DONE]"]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=MeasuredStream(frames))
            )
        ) as client:
            with (
                patch.object(decode_probe, "make_prompt", AsyncMock(return_value=("p", 10))),
                self.assertRaisesRegex(RuntimeError, "insufficient complete-cohort"),
            ):
                await probe_decode(
                    client,
                    "http://e",
                    "stub",
                    DIALECT,
                    concurrency=(1,),
                    input_lens=(10,),
                    tokens=2,
                )

    def test_decode_cells_fit_each_cohort_in_the_kv_cache(self):
        cohorts = (1, 4, 16, 48, 256)
        self.assertEqual(decode_probe.decode_cells(8192, cohorts, 64, None), cohorts)
        self.assertEqual(decode_probe.decode_cells(512, cohorts, 64, 600_000), cohorts)
        # 256 streams of 4,160 tokens exceed 600,000; 115 streams fill 80% of it.
        self.assertEqual(decode_probe.decode_cells(4096, cohorts, 64, 600_000), (1, 4, 16, 48, 115))
        # The largest cohort that fits is measured only when it exceeds every cohort that fits.
        self.assertEqual(decode_probe.decode_cells(4096, (1, 4, 64, 256), 64, 70_000), (1, 4, 13))
        self.assertEqual(decode_probe.decode_cells(4096, (1, 16, 256), 64, 70_000), (1, 16))
        # Fewer than two cohorts that fit skip the input length.
        self.assertEqual(decode_probe.decode_cells(8192, cohorts, 64, 10_000), ())

    async def sweep_calls(
        self,
        concurrency,
        capacity,
        *,
        overlap_tokens=64,
        skipped=None,
        dialect=DIALECT,
        block_tokens=None,
        input_lens=(512, 4096, 8192),
    ):
        """Run a three-length decode sweep and return each cohort's (input, size, tokens)."""
        calls: list[tuple[int, int, int]] = []

        async def cohort(client, url, model, prompt, input_len, size, tokens, dialect):
            calls.append((input_len, size, tokens))
            state = {"cohort": size, "first_at": 0.0, "last_join_at": 10.0, "left_at": 5.0}
            if tokens < overlap_tokens:
                return [], state
            return [(float(size), float(size * input_len), 0.01)] * (2 * size), state

        async def prompt(client, url, model, target, *args, **kwargs):
            return "p", target

        with (
            patch.object(decode_probe, "make_prompt", side_effect=prompt),
            patch.object(decode_probe, "_decode_cohort", side_effect=cohort),
            redirect_stdout(io.StringIO()),
        ):
            samples = await probe_decode(
                None,
                "http://e",
                "stub",
                dialect,
                concurrency=concurrency,
                input_lens=input_lens,
                tokens=64,
                max_model_len=32762,
                kv_capacity_tokens=capacity,
                block_tokens=block_tokens,
                skipped=skipped,
            )
        return calls, samples

    async def test_decode_sweep_measures_the_same_cells_when_every_cohort_fits(self):
        """A 64-sequence engine with a large KV cache measures what it measured before."""
        cohorts = (1, 4, 16, 48, 64)
        warmup = [(512, c, 64) for c in cohorts]
        cells = [(n, c, 64) for n in (512, 4096, 8192) for c in cohorts]
        engines = (
            (None, DIALECT, None),
            (1_481_418, DIALECT, 16),
            (967_728, SGLANG_DIALECT, 1),
            (967_728, DIALECT, 16),
        )
        for capacity, dialect, block in engines:
            with self.subTest(capacity=capacity, dialect=dialect.name):
                skipped: list[dict[str, object]] = []
                calls, samples = await self.sweep_calls(
                    cohorts, capacity, skipped=skipped, dialect=dialect, block_tokens=block
                )
                self.assertEqual(calls, warmup + cells)
                self.assertEqual(len(samples), 15)
                self.assertEqual(skipped, [])
        # Overlap retries that reached about 1,500 tokens on a lagged 8,192-token cohort size
        # their streams the same way.
        lagged = [
            await self.sweep_calls(
                cohorts, capacity, overlap_tokens=1500, dialect=dialect, block_tokens=block
            )
            for capacity, dialect, block in engines
        ]
        for run in lagged[1:]:
            self.assertEqual(run, lagged[0])
        self.assertIn((8192, 64, 1728), lagged[0][0])

    async def test_decode_sweep_skips_cohorts_that_exceed_the_kv_cache(self):
        skipped: list[dict[str, object]] = []
        calls, samples = await self.sweep_calls((1, 4, 16, 48, 256), 600_000, skipped=skipped)
        # 115 and 58 streams fill 80% of the cache at 4,096 and 8,192 input tokens, and each is
        # warmed before it is measured.
        self.assertEqual(
            [(n, c) for n, c, _ in calls],
            [(512, c) for c in (1, 4, 16, 48, 256)] * 2
            + [(4096, 115)]
            + [(4096, c) for c in (1, 4, 16, 48, 115)]
            + [(8192, 58)]
            + [(8192, c) for c in (1, 4, 16, 48, 58)],
        )
        self.assertEqual(max(sample[0] for sample in samples), 256)
        self.assertEqual(
            skipped,
            [
                {
                    "input_tokens": n,
                    "concurrency": 256,
                    "kv_tokens": 256 * (n + 64),
                    "kv_capacity_tokens": 600_000,
                }
                for n in (4096, 8192)
            ],
        )
        with self.assertRaisesRegex(ValueError, "fewer than two decode input lengths"):
            await self.sweep_calls((1, 4, 16, 48, 256), 10_000)

    async def test_a_default_limit_engine_measures_what_its_kv_cache_holds(self):
        """An SGLang engine at its default limit of 256 measures its KV-bound cohort per length."""
        for capacity, largest, covered in (
            (967_728, {512: 256, 4096: 168, 8192: 88}, 88),
            (470_000, {512: 256, 4096: 81, 8192: 48}, 48),
        ):
            with self.subTest(capacity=capacity):
                skipped: list[dict[str, object]] = []
                calls, samples = await self.sweep_calls(
                    (1, 4, 16, 48, 256),
                    capacity,
                    skipped=skipped,
                    dialect=SGLANG_DIALECT,
                    block_tokens=1,
                )
                measured: dict[int, int] = {}
                for n, c, _ in calls:
                    measured[n] = max(measured.get(n, 0), c)
                self.assertEqual(measured, largest)
                self.assertEqual(
                    [(cell["input_tokens"], cell["concurrency"]) for cell in skipped],
                    [(4096, 256), (8192, 256)],
                )
                # Decode seats stop at the cohort of the cell that held the most KV tokens.
                self.assertEqual(instance_probe.covered_decode_requests(samples), covered)

    async def test_a_kv_bound_cohort_that_cannot_complete_is_dropped(self):
        """The sweep keeps the cells it measured when its KV-bound cohort cannot overlap."""
        calls: list[tuple[int, int]] = []

        async def cohort(client, url, model, prompt, input_len, size, tokens, dialect):
            calls.append((input_len, size))
            # The 115-stream cohort's second repeat never overlaps, and nothing can lengthen it.
            if (input_len, size) == (4096, 115) and calls.count((4096, 115)) == 3:
                return [], {"cohort": size}
            return [(float(size), float(size * input_len), 0.01)] * (2 * size), {"cohort": size}

        evidence: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        with (
            patch.object(decode_probe, "make_prompt", side_effect=lambda *a, **k: ("p", a[3])),
            patch.object(decode_probe, "_decode_cohort", side_effect=cohort),
            redirect_stdout(io.StringIO()),
        ):
            samples = await probe_decode(
                None,
                "http://e",
                "stub",
                DIALECT,
                concurrency=(1, 4, 16, 48, 256),
                input_lens=(512, 4096, 8192),
                tokens=64,
                evidence=evidence,
                max_model_len=32762,
                repeats=2,
                kv_capacity_tokens=600_000,
                skipped=skipped,
            )
        cells = [(row["input_tokens"], row["concurrency"]) for row in evidence]
        self.assertNotIn((4096, 115), cells)
        self.assertEqual(len(cells), 2 * 14)
        self.assertEqual(len(samples), 14)
        self.assertNotIn(115.0, [sample[0] for sample in samples])
        self.assertIn(58.0, [sample[0] for sample in samples])
        self.assertEqual(
            [(cell["input_tokens"], cell["concurrency"]) for cell in skipped],
            [(4096, 256), (4096, 115), (8192, 256)],
        )
        self.assertEqual(
            skipped[1]["reason"],
            "decode probe has insufficient complete-cohort intervals: got 0, need 230, "
            "isl=4096, c=115, tokens=64",
        )
        self.assertEqual(skipped[1]["kv_tokens"], 115 * (4096 + 64))

    async def test_a_length_left_with_one_cohort_by_a_dropped_cohort_is_dropped(self):
        """A dropped KV-bound cohort that leaves one cohort at its length drops the length."""

        async def cohort(client, url, model, prompt, input_len, size, tokens, dialect):
            # Only the requested cohorts complete; the 2-stream KV-bound cohort never overlaps.
            if size not in (1, 4):
                return [], {"cohort": size}
            return [(float(size), float(size * input_len), 0.01 + size * 1e-4)] * (2 * size), {
                "cohort": size
            }

        # Four 8,192-token streams exceed the cache, so that length measures 1 and 2 streams.
        capacity = 3 * (8192 + 64) + 10
        self.assertEqual(decode_probe.decode_cells(8192, (1, 4), 64, capacity), (1, 2))
        for input_lens, measured in (((512, 4096, 8192), (512, 4096)), ((4096, 8192), None)):
            with self.subTest(input_lens=input_lens):
                evidence: list[dict[str, object]] = []
                skipped: list[dict[str, object]] = []
                with (
                    patch.object(
                        decode_probe, "make_prompt", side_effect=lambda *a, **k: ("p", a[3])
                    ),
                    patch.object(decode_probe, "_decode_cohort", side_effect=cohort),
                    redirect_stdout(io.StringIO()),
                ):
                    sweep = probe_decode(
                        None,
                        "http://e",
                        "stub",
                        DIALECT,
                        concurrency=(1, 4),
                        input_lens=input_lens,
                        tokens=64,
                        evidence=evidence,
                        max_model_len=32768,
                        kv_capacity_tokens=capacity,
                        skipped=skipped,
                    )
                    if measured is None:
                        with self.assertRaisesRegex(
                            ValueError,
                            r"fewer than two decode input lengths after dropping c=2 at "
                            r"isl=8192 \(decode probe has insufficient complete-cohort",
                        ):
                            await sweep
                        continue
                    samples = await sweep
                self.assertEqual(sorted({sample[1] / sample[0] for sample in samples}), [*measured])
                self.assertEqual(sorted({row["input_tokens"] for row in evidence}), [*measured])
                self.assertEqual(
                    [(cell["input_tokens"], cell["concurrency"]) for cell in skipped],
                    [(8192, 4), (8192, 2)],
                )

    def test_decode_cells_count_whole_blocks_and_the_decode_reserve(self):
        # A vLLM stream of 100 input and 64 output tokens takes 11 blocks of 16, and the pool
        # keeps its null block back.
        null = {"block_tokens": 16, "reserved_blocks": DIALECT.kv_reserved_blocks}
        self.assertEqual(
            decode_probe.decode_cells(100, (1, 2, 8), 64, 16 + 8 * 176, **null), (1, 2, 8)
        )
        # Without room for 8 streams, 80% of the cache holds 6.
        self.assertEqual(
            decode_probe.decode_cells(100, (1, 2, 8), 64, 15 + 8 * 176, **null), (1, 2, 6)
        )
        # An SGLang decode engine holds 512 tokens free for each running request, and keeps no
        # block back.
        cohorts = (1, 4, 16, 48, 64)
        reserve = {"reserved": SGLANG_DIALECT.decode_reserved_tokens, "block_tokens": 1}
        self.assertEqual(SGLANG_DIALECT.kv_reserved_blocks, 0)
        self.assertEqual(
            decode_probe.decode_cells(8192, cohorts, 64, 64 * 8704, **reserve), cohorts
        )
        self.assertEqual(
            decode_probe.decode_cells(8192, cohorts, 64, 64 * 8704 - 1, **reserve),
            (1, 4, 16, 48, 51),
        )

    async def test_decode_retry_keeps_lagged_streams_within_whole_kv_blocks(self):
        """A lagged cohort retries only with streams whose blocks still fit in the KV cache."""

        async def cohort(client, url, model, prompt, input_len, size, tokens, dialect):
            # The four-stream cohort at 100 tokens joins late and needs 192 tokens to overlap.
            state = {"cohort": size, "first_at": 0.0, "last_join_at": 10.0, "left_at": 5.0}
            if size == 4 and input_len == 100 and tokens < 192:
                return [], state
            return [(float(size), float(input_len), 0.01)] * (2 * size), state

        async def sweep(block_tokens):
            with (
                patch.object(decode_probe, "make_prompt", side_effect=lambda *a, **k: ("p", a[3])),
                patch.object(decode_probe, "_decode_cohort", side_effect=cohort),
                redirect_stdout(io.StringIO()),
            ):
                return await probe_decode(
                    None,
                    "http://e",
                    "stub",
                    DIALECT,
                    concurrency=(1, 4),
                    input_lens=(100, 50),
                    tokens=64,
                    max_model_len=4096,
                    kv_capacity_tokens=1200,
                    block_tokens=block_tokens,
                )

        # Without blocks, 1,200 tokens hold four streams of 300: 100 input and 192 output fit.
        self.assertEqual(len(await sweep(None)), 4)
        # With 16-token blocks and one block held back, four streams get 288 tokens each.
        with self.assertRaisesRegex(
            RuntimeError, "insufficient complete-cohort.*isl=100, c=4, tokens=64"
        ):
            await sweep(16)

    async def test_decode_sweep_warms_each_cohort_where_it_first_fits(self):
        calls, samples = await self.sweep_calls(
            (1, 4, 16, 48, 256), 600_000, input_lens=(8192, 512)
        )
        small = [(8192, c, 64) for c in (1, 4, 16, 48, 58)]
        self.assertEqual(
            calls,
            small + small + [(512, 256, 64)] + [(512, c, 64) for c in (1, 4, 16, 48, 256)],
        )
        self.assertEqual(len(samples), 10)

    async def test_decode_sweep_rechecks_input_lengths_after_tokenizing(self):
        """A prompt that tokenizes past a KV boundary drops its length, and the sweep says so."""

        async def cohort(client, url, model, prompt, input_len, size, tokens, dialect):
            state = {"cohort": size}
            return [(float(size), float(input_len), 0.01)] * (2 * size), state

        async def prompt(client, url, model, target, *args, **kwargs):
            return "p", target + 2

        with (
            patch.object(decode_probe, "make_prompt", side_effect=prompt),
            patch.object(decode_probe, "_decode_cohort", side_effect=cohort),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "fewer than two decode input lengths"),
        ):
            # Two streams of 200 input and 64 output tokens fit in 528 tokens; of 202, only one.
            await probe_decode(
                None,
                "http://e",
                "stub",
                DIALECT,
                concurrency=(1, 2),
                input_lens=(100, 200),
                tokens=64,
                max_model_len=4096,
                kv_capacity_tokens=528,
            )

    async def test_decode_sweep_fills_a_cohort_of_its_engine_limit_through_the_pool(self):
        """The profiler's pool holds every stream of a cohort at the engine's sequence limit."""
        cohorts = bounded_sweep(Sweep(decode_concurrency=(1, 16)), 131072, 64).decode_concurrency
        self.assertEqual(cohorts, (1, 16, 64))
        connections = fleet_probe._pool_size(
            Sweep(decode_concurrency=(1, 16)),
            1,
            2,
            paired=False,
            limits={"e0": 64},
            targets=[SimpleNamespace(iid="e0")],
        )
        engine = SimulatedEngine("e0", token_interval_s=0.01)
        url = await engine.start("127.0.0.1", 0)
        pool = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0), limits=pool
            ) as client:
                with redirect_stdout(io.StringIO()):
                    samples = await probe_decode(
                        client,
                        url,
                        "m",
                        DIALECT,
                        concurrency=cohorts,
                        tokens=16,
                        input_lens=(32, 64),
                        max_model_len=131072,
                    )
        finally:
            await engine.close()
        self.assertEqual(max(sample[0] for sample in samples), 64)

    def test_overlapping_tokens_cover_the_admission_lag(self):
        lagged = {"cohort": 4, "first_at": 0.0, "last_join_at": 10.0, "left_at": 5.0}
        self.assertEqual(decode_probe._overlapping_tokens(lagged, 64, None), 64 + 128)
        self.assertIsNone(decode_probe._overlapping_tokens(lagged, 64, 150))
        overlapped = {"cohort": 4, "first_at": 0.0, "last_join_at": 2.0, "left_at": 5.0}
        self.assertIsNone(decode_probe._overlapping_tokens(overlapped, 64, None))
        self.assertIsNone(decode_probe._overlapping_tokens({"cohort": 4}, 64, None))

    async def test_decode_sweep_retries_a_lagged_cohort_with_overlapping_tokens(self):
        calls: list[int] = []

        async def lagged(client, url, model, prompt, input_len, state, observed, dialect, tokens):
            calls.append(tokens)
            state.setdefault("first_at", 0.0)
            if tokens < 128:
                state["last_join_at"] = 10.0
                state.setdefault("left_at", 5.0)
                return
            observed.extend((4.0, float(input_len), 0.01) for _ in range(2))

        evidence: list[dict[str, object]] = []
        with (
            patch.object(decode_probe, "make_prompt", AsyncMock(return_value=("p", 100))),
            patch.object(decode_probe, "_one_decode_stream", side_effect=lagged),
        ):
            await probe_decode(
                None,
                "http://e",
                "stub",
                DIALECT,
                concurrency=(4,),
                input_lens=(100,),
                tokens=64,
                evidence=evidence,
                max_model_len=4096,
            )
        # One unmeasured warmup cohort at each concurrency precedes the sweep.
        self.assertEqual(calls, [64] * 4 + [64] * 4 + [192] * 4)
        self.assertEqual([row["tokens"] for row in evidence], [192])
        calls.clear()
        with (
            patch.object(decode_probe, "make_prompt", AsyncMock(return_value=("p", 100))),
            patch.object(decode_probe, "_one_decode_stream", side_effect=lagged),
            self.assertRaisesRegex(RuntimeError, "insufficient complete-cohort.*tokens=64"),
        ):
            await probe_decode(
                None,
                "http://e",
                "stub",
                DIALECT,
                concurrency=(4,),
                input_lens=(100,),
                tokens=64,
                max_model_len=250,
            )
        self.assertEqual(calls, [64] * 4 + [64] * 4)

    async def test_decode_sweep_retries_until_the_cohort_overlaps(self):
        calls: list[int] = []

        async def lagged(client, url, model, prompt, input_len, state, observed, dialect, tokens):
            calls.append(tokens)
            state.setdefault("first_at", 0.0)
            if tokens < 500:
                state["last_join_at"] = 10.0
                state.setdefault("left_at", 5.0)
                return
            observed.extend((4.0, float(input_len), 0.01) for _ in range(2))

        evidence: list[dict[str, object]] = []
        with (
            patch.object(decode_probe, "make_prompt", AsyncMock(return_value=("p", 100))),
            patch.object(decode_probe, "_one_decode_stream", side_effect=lagged),
        ):
            await probe_decode(
                None,
                "http://e",
                "stub",
                DIALECT,
                concurrency=(4,),
                input_lens=(100,),
                tokens=64,
                evidence=evidence,
                max_model_len=4096,
            )
        self.assertEqual(calls, [64] * 4 + [64] * 4 + [192] * 4 + [576] * 4)
        self.assertEqual([row["tokens"] for row in evidence], [576])
        calls.clear()
        with (
            patch.object(decode_probe, "make_prompt", AsyncMock(return_value=("p", 100))),
            patch.object(decode_probe, "_one_decode_stream", side_effect=lagged),
            self.assertRaisesRegex(RuntimeError, "insufficient complete-cohort.*tokens=192"),
        ):
            await probe_decode(
                None,
                "http://e",
                "stub",
                DIALECT,
                concurrency=(4,),
                input_lens=(100,),
                tokens=64,
                max_model_len=400,
            )
        self.assertEqual(calls, [64] * 4 + [64] * 4 + [192] * 4)

    async def test_decode_sweep_uses_total_service_time_when_tokens_burst(self):

        async def burst(client, url, model, prompt, input_len, state, observed, tokens, dialect):
            observed.extend(
                (1.0, float(input_len + index), gap) for index, gap in enumerate((0.01, 0.01, 0.07))
            )

        with (
            patch.object(decode_probe, "make_prompt", AsyncMock(return_value=("p", 128))),
            patch.object(decode_probe, "_one_decode_stream", side_effect=burst),
        ):
            samples = await probe_decode(
                None, "http://e", "stub", DIALECT, concurrency=(1,), input_lens=(128,), tokens=4
            )
        self.assertAlmostEqual(samples[0][2], 0.03)

    async def test_a_first_batch_size_compile_stays_out_of_the_fit(self):
        seen: set[int] = set()

        async def compiling(client, url, model, prompt, input_len, state, observed, *_):
            # A batch size compiles on its first cohort.
            size = int(state["cohort"])
            gap = 0.01 if size in seen else 0.07
            if state.setdefault("streams", 0) == size - 1:
                seen.add(size)
            state["streams"] += 1
            observed.extend((float(size), float(input_len), gap) for _ in range(2))

        with (
            patch.object(decode_probe, "make_prompt", AsyncMock(return_value=("p", 128))),
            patch.object(decode_probe, "_one_decode_stream", side_effect=compiling),
        ):
            samples = await probe_decode(
                None, "http://e", "stub", DIALECT, concurrency=(1, 2), input_lens=(128,), tokens=4
            )
        self.assertEqual([round(sample[2], 3) for sample in samples], [0.01, 0.01])

    async def test_profile_fits_axes_and_retains_raw_evidence(self):
        prefill = [(x, 0.001 * x + 0.01) for x in (1, 10, 100)]
        decode = [
            (r, k, 0.001 * r + 0.000001 * k + 0.01) for r in (1, 4, 16) for k in (100, 1000, 10000)
        ]
        evidence = {}
        with patched_profile_sweeps(prefill, decode, hits=[7, 7, 7]):
            row = await profile_instance(
                None, "e", "http://e", "stub", DIALECT, metrics=VLLM_METRICS, evidence=evidence
            )
        self.assertEqual(evidence["prefix_cache_hit_tokens"], 0)
        self.assertEqual((row.decode_min_requests, row.decode_max_requests), (1, 16))
        self.assertEqual((row.decode_min_kv_tokens, row.decode_max_kv_tokens), (100, 10000))
        self.assertAlmostEqual(row.tpot_request_slope, 0.001)
        self.assertAlmostEqual(row.tpot_slope, 0.000001)
        self.assertAlmostEqual(row.decode_cv_mape, 0)
        self.assertEqual(evidence["prefill_fit_points"], prefill)
        self.assertLess(evidence["prefill_fit_mape"], 0.01)
        self.assertEqual(evidence["decode"], decode)
        self.assertNotIn("decode_skipped", evidence)

    async def test_profile_bounds_decode_by_kv_capacity_and_records_skipped_cells(self):
        prefill = [(x, 0.001 * x + 0.01) for x in (1, 10, 100)]
        decode = [
            (r, k, 0.001 * r + 0.000001 * k + 0.01) for r in (1, 4, 16) for k in (100, 1000, 10000)
        ]
        cell = {"input_tokens": 8192, "concurrency": 256, "kv_tokens": 2, "kv_capacity_tokens": 1}

        async def sweep(*args, skipped, **kwargs):
            skipped.append(cell)
            return decode

        evidence = {}
        with patched_profile_sweeps(prefill, decode, hits=[7, 7, 7], capacity=600_000):
            instance_probe.probe_decode.side_effect = sweep
            row = await profile_instance(
                None, "e", "http://e", "stub", DIALECT, metrics=VLLM_METRICS, evidence=evidence
            )
            self.assertEqual(
                instance_probe.probe_decode.await_args.kwargs["kv_capacity_tokens"], 600_000
            )
        self.assertEqual(row.kv_capacity_tokens, 600_000)
        self.assertEqual(evidence["decode_skipped"], [cell])

    async def test_profile_bounds_decode_requests_by_the_cell_with_the_most_kv(self):
        """With cohorts skipped for KV, the request and KV bounds meet at a measured cell."""
        prefill = [(x, 0.001 * x + 0.01) for x in (1, 10, 100)]
        rows = [(512, c) for c in (1, 4, 16, 48, 256)] + [(8192, c) for c in (1, 4, 16, 48, 58)]
        decode = [(float(c), float(c * n), 0.001 * c + 0.000001 * c * n + 0.01) for n, c in rows]
        with patched_profile_sweeps(prefill, decode, hits=[7, 7, 7], block=16, capacity=600_000):
            row = await profile_instance(
                None, "e", "http://e", "stub", DIALECT, metrics=VLLM_METRICS
            )
            self.assertEqual(instance_probe.probe_decode.await_args.kwargs["block_tokens"], 16)
        self.assertEqual(row.decode_max_requests, 58)
        self.assertEqual(row.decode_max_kv_tokens, 58 * 8192)
        # The cell with the most KV tokens need not sit at the longest input length.
        cells = [(4096, 115), (8192, 48), (512, 256)]
        decode = [(float(c), float(c * n), 0.01) for n, c in cells]
        self.assertEqual(instance_probe.covered_decode_requests(decode), 115)

    async def test_cold_profile_rejects_prefix_cache_hits_and_records_missing_counter(self):
        prefill = [(x, 0.001 * x + 0.01) for x in (1, 10, 100)]
        decode = [
            (r, k, 0.001 * r + 0.000001 * k + 0.01) for r in (1, 4, 16) for k in (100, 1000, 10000)
        ]
        for counters, message in (
            ([7, 71], "served 64 prompt tokens"),
            ([None, None, None], None),
            ([7, 3, 3], None),
        ):
            evidence = {}
            with (
                self.subTest(counters=counters),
                patched_profile_sweeps(prefill, decode, hits=counters),
            ):
                if message:
                    with self.assertRaisesRegex(RuntimeError, message):
                        await profile_instance(
                            None,
                            "e",
                            "http://e",
                            "stub",
                            DIALECT,
                            metrics=VLLM_METRICS,
                            evidence=evidence,
                        )
                    self.assertEqual(evidence["prefix_cache_hit_tokens"], 64)
                    # Cached prefill fails before the decode sweep runs.
                    instance_probe.probe_decode.assert_not_awaited()
                else:
                    await profile_instance(
                        None,
                        "e",
                        "http://e",
                        "stub",
                        DIALECT,
                        metrics=VLLM_METRICS,
                        evidence=evidence,
                    )
                    self.assertIsNone(evidence["prefix_cache_hit_tokens"])

    async def test_cold_probes_salt_every_request(self):
        bodies = []

        def answer(request):
            body = json.loads(request.content)
            bodies.append(body)
            if body.get("stream"):
                frames = [token(0), token(1), token(2, finish="length"), "[DONE]"]
                return httpx.Response(200, stream=MeasuredStream(frames))
            return httpx.Response(
                200,
                json={
                    "usage": {"prompt_tokens": 4, "completion_tokens": body["max_tokens"]},
                    "choices": [{"finish_reason": "length"}],
                },
            )

        workload = ColocatedWorkload(40.0, 40.0, 4, 4, 3)
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            with (
                patch.object(prefill_probe, "make_prompt", AsyncMock(return_value=("prompt", 4))),
                patch.object(
                    neighbours_probe, "make_prompt", AsyncMock(return_value=("prompt", 4))
                ),
                patch.object(
                    neighbours_probe, "engine_context_limit", AsyncMock(return_value=4096)
                ),
                redirect_stdout(io.StringIO()),
            ):
                await probe_prefill(client, "http://e", "stub", DIALECT, lens=(4,), repeats=3)
                state = {"resident": 0, "requests": 0, "epoch": 0, "cohort": 2}
                await asyncio.gather(
                    *(
                        decode_probe._one_decode_stream(
                            client, "http://e", "stub", "prompt", 4, state, [], DIALECT, tokens=3
                        )
                        for _ in range(2)
                    )
                )
                load = NeighbourLoad(
                    client,
                    [("p", "http://p", Role.PREFILL)],
                    "stub",
                    DIALECT,
                    3.8,
                    workload,
                )
                await load.start()
                await asyncio.sleep(0.06)
                await load.stop()
        self.assertGreater(len(bodies), 6)
        salts = [body.get("cache_salt") for body in bodies]
        self.assertTrue(all(isinstance(salt, str) and len(salt) >= 43 for salt in salts))
        self.assertEqual(len(set(salts)), len(salts))
        self.assertEqual({body["prompt"] for body in bodies}, {"prompt"})

    async def test_run_protects_existing_and_symlink_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            with self.assertRaises(FileExistsError):
                await fleet_probe.run(cfg, None)
            cfg.profiles_path = Path(folder) / "link.json"
            cfg.profiles_path.symlink_to(Path(folder) / "profiles.json")
            with self.assertRaisesRegex(ValueError, "symlink"):
                await fleet_probe.run(cfg, None, overwrite=True)

    async def test_run_writes_profile_and_measurement_sidecar(self):
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

            async def measured(client, iid, url, model, dialect, sweep, *args, evidence, **kwargs):
                self.assertEqual(sweep.decode_concurrency, (1, 4, 8))
                evidence["prefill"] = [[10, 0.1]]
                return profile(iid)

            with (
                patch.object(httpx, "AsyncClient", return_value=client),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    fleet_probe,
                    "read_generation",
                    AsyncMock(
                        return_value=GenerationEvidence("sha256:" + "a" * 64, {"engine": {}}, 100.0)
                    ),
                ),
                patch.object(fleet_probe, "profile_instance", side_effect=measured),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(await fleet_probe.run(cfg, {"e0"}, limits_path=limits_path), 0)
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
            self.assertEqual(saved["engines"]["e0"]["limits_path"], str(limits_path))
            self.assertEqual(max(saved["engines"]["e0"]["sweep"]["prefill_lens"]), 16300)

    async def test_run_rejects_decode_fit_outside_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            cfg.profiles_path = Path(folder) / "unstable.json"
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200))
            )
            with (
                patch.object(httpx, "AsyncClient", return_value=client),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    fleet_probe,
                    "read_generation",
                    AsyncMock(
                        return_value=GenerationEvidence("sha256:" + "a" * 64, {"engine": {}}, 100.0)
                    ),
                ),
                patch.object(
                    fleet_probe,
                    "profile_instance",
                    AsyncMock(return_value=replace(profile("e0"), decode_fit_mape=0.5)),
                ),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "profile rejected"),
            ):
                await fleet_probe.run(cfg, {"e0"})
            self.assertFalse(cfg.profiles_path.exists())
            saved = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            self.assertIn("profile rejected", saved["engines"]["e0"]["error"])

    def test_profile_lanes_serialize_shared_devices_and_neighbour_load(self):
        with tempfile.TemporaryDirectory() as folder:
            engines = fleet(Path(folder)).engines
        shared = SharedDeviceAllocation("gpu-0", "uuid", 0.5, 0.1)
        engines[:2] = [replace(spec, shared_device=shared) for spec in engines[:2]]
        lanes = [
            [spec.iid for spec in lane]
            for lane in fleet_probe._profile_lanes(engines, colocated=False)
        ]
        self.assertEqual(lanes[0], [engines[0].iid, engines[1].iid])
        self.assertEqual([len(lane) for lane in lanes[1:]], [1] * (len(engines) - 2))
        colocated = fleet_probe._profile_lanes(engines, colocated=True)
        self.assertEqual(
            [[spec.iid for spec in lane] for lane in colocated], [[s.iid for s in engines]]
        )

    async def test_each_pair_placement_resets_both_engine_caches(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
        spec, peer = cfg.engines[0], cfg.engines[1]
        backend = load_backend("sglang")
        dialect = backend.dialect
        calls = []
        status = 200

        async def answer(request):
            calls.append(f"{request.url.port} {request.url.path}")
            return httpx.Response(status, text="pending requests")

        class Switcher(RoleSwitcher):
            requires_idle = True

            async def switch(self, client, url, role, launch=None):
                calls.append(f"{httpx.URL(url).port} {role.value}")

        pairing = PairedTransport(
            httpx.MockTransport(answer), backend.connector("mooncake"), dialect
        )
        serve_as = fleet_probe._pair_roles(cfg, dialect, pairing, Switcher(), spec, peer, {})
        own, other = httpx.URL(spec.url).port, httpx.URL(peer.url).port
        with patch.object(fleet_probe.httpx, "AsyncClient", routed(answer)):
            await serve_as(Role.DECODE)
            self.assertEqual(
                calls,
                [
                    f"{other} prefill",
                    f"{own} decode",
                    f"{own} /flush_cache",
                    f"{other} /flush_cache",
                ],
            )
            status = 400
            with self.assertRaisesRegex(RuntimeError, r"cache reset failed .*\(400\)"):
                await serve_as(Role.PREFILL)

    def test_a_finished_lane_stands_in_at_later_phase_switches(self):
        phases = threading.Barrier(2)
        stand_in = threading.Thread(target=fleet_probe._stand_in, args=(phases,))
        stand_in.start()
        for _ in range(3):
            phases.wait(5)
        phases.abort()
        stand_in.join(5)
        self.assertFalse(stand_in.is_alive())

    def test_paired_profile_lanes_run_disjoint_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            engines = fleet(Path(folder)).engines
        specs = [replace(engines[0], iid=f"n{i}") for i in range(1, 6)]

        def lanes(targets):
            return [
                [spec.iid for spec in lane]
                for lane in fleet_probe._profile_lanes(targets, colocated=False, paired=True)
            ]

        self.assertEqual(lanes(specs[:4]), [["n1", "n2"], ["n3", "n4"]])
        self.assertEqual(lanes(specs), [["n1", "n2"], ["n3", "n4", "n5"]])
        self.assertEqual(lanes(specs[:1]), [["n1"]])
        pair = lanes(specs[:4])[0]
        lane = [spec for spec in specs if spec.iid in pair]
        self.assertEqual(fleet_probe._lane_peer(lane[0], lane, specs).iid, "n2")
        self.assertEqual(fleet_probe._lane_peer(lane[1], lane, specs).iid, "n1")
        self.assertEqual(fleet_probe._lane_peer(specs[0], specs[:1], specs).iid, "n2")
        shared = SharedDeviceAllocation("gpu-0", "uuid", 0.5, 0.1)
        specs[0] = replace(specs[0], shared_device=shared)
        self.assertEqual(lanes(specs[:4]), [["n1", "n2", "n3", "n4"]])

    async def test_run_profiles_engines_on_separate_devices_concurrently(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            cfg.profiles_path = Path(folder) / "parallel.json"
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200))
            )
            active = {"now": 0, "peak": 0}

            async def measured(client, iid, url, model, dialect, sweep, *args, evidence, **kwargs):
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
                await asyncio.sleep(0.02)
                active["now"] -= 1
                return profile(iid)

            with (
                patch.object(httpx, "AsyncClient", return_value=client),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    fleet_probe,
                    "read_generation",
                    AsyncMock(
                        return_value=GenerationEvidence("sha256:" + "a" * 64, {"engine": {}}, 100.0)
                    ),
                ),
                patch.object(fleet_probe, "profile_instance", side_effect=measured),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(await fleet_probe.run(cfg, None), 0)
            self.assertEqual(active["peak"], len(cfg.engines))
            self.assertEqual(len(ProfileStore(cfg.profiles_path)), len(cfg.engines))

    async def test_run_retains_per_peer_evidence_when_a_neighbour_stalls(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            cfg.profiles_path = Path(folder) / "colocated.json"
            allocation = SharedDeviceAllocation("gpu", "uuid", 0.5, 0.1)
            cfg.engines = [replace(spec, shared_device=allocation) for spec in cfg.engines]
            cfg.engines.append(replace(cfg.engines[1], iid="e4", url="http://stalled"))
            completed = asyncio.Event()

            async def answer(request):
                if request.url.path == "/health":
                    return httpx.Response(200)
                if request.url.host == "stalled":
                    await asyncio.Event().wait()
                completed.set()
                return httpx.Response(200, json={"usage": {"completion_tokens": 8}})

            async def measured(*args, **kwargs):
                completed.clear()
                await asyncio.wait_for(completed.wait(), timeout=1)
                return profile("e0")

            client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
            with (
                patch.object(httpx, "AsyncClient", return_value=client),
                patch.object(
                    neighbours_probe, "make_prompt", AsyncMock(return_value=("prompt", 32))
                ),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    neighbours_probe, "engine_context_limit", AsyncMock(return_value=16384)
                ),
                patch.object(
                    fleet_probe,
                    "read_generation",
                    AsyncMock(return_value=GenerationEvidence("sha256:" + "a" * 64, {}, 100.0)),
                ),
                patch.object(fleet_probe, "profile_instance", side_effect=measured),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "e4: completed 0 requests"),
            ):
                await fleet_probe.run(
                    cfg,
                    {"e0"},
                    colocated_workload=ColocatedWorkload(100, 100, 32, 32, 8),
                )
            self.assertFalse(cfg.profiles_path.exists())
            saved = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            peers = saved["engines"]["e0"]["colocated_load"]["peers"]
            self.assertGreater(peers["e3"]["completed"], 0)
            self.assertEqual(peers["e4"]["completed"], 0)
            self.assertIn("completed 0 requests", peers["e4"]["error"])

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
                patch.object(httpx, "AsyncClient", return_value=client),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    fleet_probe,
                    "read_generation",
                    AsyncMock(
                        return_value=GenerationEvidence("sha256:" + "a" * 64, {"engine": {}}, 100.0)
                    ),
                ),
                patch.object(instance_probe, "probe_prefill", AsyncMock(return_value=bad)),
                patch.object(instance_probe, "probe_decode", AsyncMock()) as decode,
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "e0: .*prefill median fit error"),
            ):
                await fleet_probe.run(cfg, {"e0"})
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
                GenerationEvidence("sha256:" + "a" * 64, {"engine": {"start": 100}}, 100.0),
                GenerationEvidence("sha256:" + "b" * 64, {"engine": {"start": 101}}, 101.0),
            )
            with (
                patch.object(httpx, "AsyncClient", return_value=client),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(
                    fleet_probe, "profile_instance", AsyncMock(return_value=profile("e0"))
                ),
                patch.object(fleet_probe, "read_generation", AsyncMock(side_effect=generations)),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(
                    RuntimeError, "^e0: engine generation changed during profiling"
                ),
            ):
                await fleet_probe.run(cfg, {"e0"})
            self.assertFalse(cfg.profiles_path.exists())
            saved = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            self.assertIn("generation changed", saved["engines"]["e0"]["error"])

    async def test_a_timeout_records_its_type(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = fleet(Path(folder))
            cfg.profiles_path = Path(folder) / "new.json"
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200))
            )
            timeout = httpx.ReadTimeout(
                "", request=httpx.Request("POST", "http://e0/v1/completions")
            )
            with (
                patch.object(httpx, "AsyncClient", return_value=client),
                patch.object(fleet_probe, "engine_context_limit", AsyncMock(return_value=16384)),
                patch.object(fleet_probe, "profile_instance", AsyncMock(side_effect=timeout)),
                patch.object(
                    fleet_probe,
                    "read_generation",
                    AsyncMock(
                        return_value=GenerationEvidence(
                            "sha256:" + "a" * 64, {"engine": {"start": 100}}, 100.0
                        )
                    ),
                ),
                redirect_stdout(io.StringIO()),
                self.assertRaises(httpx.ReadTimeout),
            ):
                await fleet_probe.run(cfg, {"e0"})
            saved = json.loads(cfg.profiles_path.with_suffix(".samples.json").read_text())
            self.assertEqual(saved["engines"]["e0"]["error"], "ReadTimeout: ")

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
                    profile_cli.main(
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
                refit_saved_prefill(source, output, {"e0", "e3"})

            legacy = root / "legacy.samples.json"
            legacy_record = json.loads(source.read_text())
            for row in legacy_record["engines"].values():
                row["profile"].pop("generation_digest")
                row.pop("generation_evidence")
            legacy.write_text(json.dumps(legacy_record))
            with self.assertRaisesRegex(ValueError, "e0: saved samples lack generation evidence"):
                refit_saved_prefill(legacy, root / "legacy-refit.json", {"e0", "e3"})

    def test_kv_capacity_uses_the_smallest_reported_rank(self):
        self.assertEqual(
            VLLM_METRICS.kv_capacity(
                'x{kv_cache_size_tokens="1000"} 1\nx{kv_cache_size_tokens="900.0"} 1'
            ),
            900,
        )
        self.assertIsNone(VLLM_METRICS.kv_capacity(""))

    def test_prefix_cache_hits_sum_engine_counters(self):
        metrics = (
            'vllm:prefix_cache_hits_total{engine="0",model_name="m"} 12.0\n'
            'vllm:prefix_cache_hits_created{engine="0",model_name="m"} 1.7e9\n'
            'vllm:prefix_cache_hits_total{engine="1",model_name="m"} 30.0\n'
            'vllm:prefix_cache_queries_total{engine="0",model_name="m"} 99.0\n'
        )
        self.assertEqual(VLLM_METRICS.prefix_cache_hits(metrics), 42)
        self.assertIsNone(VLLM_METRICS.prefix_cache_hits("vllm:num_requests_running 0\n"))


class PoolSizeTests(unittest.TestCase):
    def test_a_paired_cohort_gets_a_connection_for_each_leg(self):
        sweep = Sweep(decode_concurrency=(1, 4, 16, 48))
        self.assertEqual(fleet_probe._pool_size(sweep, 1, 8, paired=False), 56)
        self.assertEqual(fleet_probe._pool_size(sweep, 1, 8, paired=True), 104)

    def test_a_colocated_run_holds_one_cohort_at_a_time(self):
        """Engines that share a GPU take turns, so the pool holds one cohort at the top limit."""
        sweep = Sweep(decode_concurrency=(1, 4, 16, 48))
        engines = [SimpleNamespace(iid=f"e{i}") for i in range(4)]
        lanes = fleet_probe._profile_lanes(engines, colocated=True)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(
            fleet_probe._pool_size(
                sweep,
                len(lanes),
                len(engines),
                paired=False,
                limits={engine.iid: 64 for engine in engines},
                targets=engines,
            ),
            64 + 4,
        )

    def test_a_cohort_at_an_engine_limit_gets_a_connection_for_each_stream(self):
        sweep = Sweep(decode_concurrency=(1, 4, 16, 48))
        engines = [SimpleNamespace(iid=iid) for iid in ("e0", "e1")]
        for limits, paired, lanes, expected in (
            ({"e0": 64, "e1": 64}, False, 1, 64 + 8),
            ({"e0": 64, "e1": 64}, False, 8, 64 * 8 + 8),
            # A paired SGLang lane at the default request cap.
            ({"e0": 256, "e1": 256}, True, 1, 256 * 2 + 8),
            # An engine limit below the sweep keeps the sweep's largest cohort.
            ({"e0": 32, "e1": 32}, False, 1, 48 + 8),
            ({"e0": 16, "e1": 128}, False, 1, 128 + 8),
        ):
            with self.subTest(limits=limits, paired=paired, lanes=lanes):
                self.assertEqual(
                    fleet_probe._pool_size(
                        sweep, lanes, 8, paired=paired, limits=limits, targets=engines
                    ),
                    expected,
                )
        # Only the engines this run profiles size the pool.
        self.assertEqual(
            fleet_probe._pool_size(
                sweep, 1, 8, paired=False, limits={"e0": 32, "e1": 128}, targets=engines[:1]
            ),
            48 + 8,
        )
