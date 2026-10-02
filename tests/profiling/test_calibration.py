"""Check measured-deadline selection and diagnostic handoff outcomes."""

import asyncio
import io
import json
import random
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.config import EngineSpec, FleetConfig
from narwhal.config.model import SharedDeviceAllocation
from narwhal.engines.client import FIRST_OUTPUT_DETAIL, EngineClient, EngineError
from narwhal.engines.validation import validation_pairs
from narwhal.profiling import calibration
from narwhal.profiling.generation import GenerationEvidence
from narwhal.profiling.probe.live import device_key
from narwhal.types import Role
from tests.fixtures import ROOT, calibration_document

TOKEN = 'data: {"choices":[{"text":"x","token_ids":[1]}]}'


def engines(count, shared=()):
    """Return unpinned engines; the engines named in `shared` use one device."""
    allocation = SharedDeviceAllocation("g0", "GPU-0", 0.5, 0.4)
    return [
        EngineSpec(
            f"n{index}",
            f"http://n{index}.invalid",
            shared_device=allocation if f"n{index}" in shared else None,
        )
        for index in range(count)
    ]


class RecordingClient:
    """Fake engine client that records every prefill and decode call per device slot."""

    def __init__(
        self,
        delay=lambda leg, src, dst: 0.001,
        fail=lambda src, dst: None,
        fail_prefill=lambda src: None,
    ):
        self.delay = delay
        self.fail = fail
        self.fail_prefill = fail_prefill
        self.slots = {}
        self.flight = {"prefill": Counter(), "decode": Counter()}
        self.calls = []
        self.violations = []

    def __call__(self, **kwargs):
        return self

    def _enter(self, leg, src, dst, target):
        slot = self.slots[src if leg == "prefill" else dst]
        self.flight[leg][slot] += 1
        prefills = self.flight["prefill"].total()
        decodes = self.flight["decode"].total()
        self.calls.append((leg, src, dst, target, prefills, decodes))
        if self.flight[leg][slot] > 1:
            self.violations.append(f"{slot} runs two {leg} calls")
        if prefills and decodes:
            self.violations.append("prefill and decode overlap")
        return slot

    async def prefill(self, url, endpoint, body, headers):
        slot = self._enter("prefill", url, None, int(body["prompt"]))
        try:
            await asyncio.sleep(self.delay("prefill", url, None))
            if (error := self.fail_prefill(url)) is not None:
                raise error
        finally:
            self.flight["prefill"][slot] -= 1
        return SimpleNamespace(url=url)

    async def decode(self, url, endpoint, body, headers, handoff, first_token_timeout_s=None):
        slot = self._enter("decode", handoff.url, url, int(body["prompt"]))
        try:
            await asyncio.sleep(self.delay("decode", handoff.url, url))
            if (error := self.fail(handoff.url, url)) is not None:
                raise error
            yield TOKEN
            yield "data: [DONE]"
        finally:
            self.flight["decode"][slot] -= 1

    async def aclose(self):
        pass


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        self.cfg.engines = [self.cfg.engines[0], self.cfg.engines[3]]
        self.cfg.first_token_timeout_s = 0.01

    def test_candidate_uses_maximum_and_nearest_rank_p99(self):
        p99, maximum, candidate = calibration.candidate_deadline([1.0] * 98 + [5.0] * 2)
        self.assertEqual((p99, maximum, candidate), (5.0, 5.0, 6.5))

    def _complete_document(self):
        return calibration_document(
            self.cfg,
            {spec.iid: "old" for spec in self.cfg.engines},
            {spec.iid: 100.0 for spec in self.cfg.engines},
            seconds=3.75,
        )

    def _four_engines(self):
        fleet = FleetConfig.load(ROOT / "tests/data/fleet.json")
        self.cfg.engines = [spec for spec in fleet.engines if spec.iid in {"e0", "e1", "e3", "e4"}]

    def _round_numbers(self):
        pairs = validation_pairs(self.cfg.engines, mesh=True)
        slots = {spec.iid: device_key(spec) for spec in self.cfg.engines}
        rounds = calibration.calibration_rounds(pairs, slots)
        return {pair: number for number, members in enumerate(rounds, 1) for pair in members}

    def _canonical(self, input_tokens, samples):
        pairs = validation_pairs(self.cfg.engines, mesh=True)
        return [
            (src, dst, target)
            for source in sorted({src for src, _ in pairs})
            for target in input_tokens
            for src, dst in pairs
            if src == source
        ], [
            (src, dst, target, attempt)
            for source in sorted({src for src, _ in pairs})
            for target in input_tokens
            for src, dst in pairs
            if src == source
            for attempt in range(1, samples + 1)
        ]

    def test_validation_requires_deadline_above_measured_candidate(self):
        document = self._complete_document()
        self.assertIn("is not above", " ".join(calibration.evidence_problems(self.cfg, document)))
        self.cfg.first_token_timeout_s = 5.1
        self.assertEqual(calibration.evidence_problems(self.cfg, document), [])

    def test_validation_recomputes_candidate_from_raw_timings(self):
        self.cfg.first_token_timeout_s = 5.1
        document = self._complete_document()
        document["attempts"][0]["first_token_seconds"] = 9.0
        self.assertIn(
            "incorrect maximum_seconds", " ".join(calibration.evidence_problems(self.cfg, document))
        )

    def test_validation_rejects_duplicated_or_invalid_attempt_indices(self):
        self.cfg.first_token_timeout_s = 5.1
        for index in (1, 0, 101, None, True):
            with self.subTest(index=index):
                document = self._complete_document()
                document["attempts"][1]["attempt"] = index
                self.assertIn(
                    "lacks distinct attempt indices",
                    " ".join(calibration.evidence_problems(self.cfg, document)),
                )

    def test_validation_rejects_missing_or_failed_generation_checks(self):
        self.cfg.first_token_timeout_s = 5.1
        for field in ("changed_generations", "generation_errors"):
            for value in (None, ["e0"], "", False):
                with self.subTest(field=field, value=value):
                    document = self._complete_document()
                    document[field] = value
                    self.assertIn(
                        f"{field} must be an empty list",
                        " ".join(calibration.evidence_problems(self.cfg, document)),
                    )

    def test_validation_requires_process_starts_duration_and_rounds(self):
        self.cfg.first_token_timeout_s = 5.1
        edits = {
            "no process starts": lambda document: document.pop("process_starts"),
            "process starts not an object": lambda document: document.update(process_starts=[1]),
            "engine without a start": lambda document: document["process_starts"].pop("e3"),
            "unknown engine start": lambda document: document["process_starts"].update(e9=1.0),
            "zero start": lambda document: document["process_starts"].update(e3=0.0),
            "boolean start": lambda document: document["process_starts"].update(e3=True),
            "no duration": lambda document: document.pop("duration_s"),
            "negative duration": lambda document: document.update(duration_s=-1.0),
            "no round": lambda document: document["attempts"][5].pop("round"),
            "zero round": lambda document: document["attempts"][5].update(round=0),
            "text round": lambda document: document["attempts"][5].update(round="1"),
            "boolean round": lambda document: document["attempts"][5].update(round=True),
        }
        for name, edit in edits.items():
            with self.subTest(name):
                document = self._complete_document()
                edit(document)
                problems = calibration.evidence_problems(self.cfg, document)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("recalibrate with narwhal-check --calibrate-first-token", problems[0])

    def test_validation_requires_a_capture_time(self):
        self.cfg.first_token_timeout_s = 5.1
        for value in (None, 0, "1000", True):
            with self.subTest(value=value):
                document = self._complete_document()
                document["captured_at_unix"] = value
                self.assertEqual(
                    calibration.evidence_problems(self.cfg, document),
                    ["first-token calibration has no valid capture time"],
                )

    async def test_saved_evidence_rejects_a_changed_engine_generation(self):
        self.cfg.first_token_timeout_s = 5.1
        path = Path(self.folder.name) / "calibration.json"
        self.cfg.first_token_calibration_path = path
        path.write_text(json.dumps(self._complete_document()))
        with patch.object(
            calibration,
            "read_generation",
            new=AsyncMock(return_value=GenerationEvidence("new", {}, 100.0)),
        ):
            check = await calibration.verify_calibration(self.cfg)
        self.assertEqual(check.status, "rejected")
        self.assertEqual(check.engines, {})
        self.assertEqual(len(check.problems), len(self.cfg.engines))
        self.assertTrue(all("process differs" in problem for problem in check.problems))

    async def test_unset_or_unreadable_calibration_has_no_engine_labels(self):
        check = await calibration.verify_calibration(self.cfg)
        self.assertEqual((check.status, check.problems), ("uncalibrated", ()))
        path = Path(self.folder.name) / "missing.json"
        self.cfg.first_token_calibration_path = path
        check = await calibration.verify_calibration(self.cfg)
        self.assertEqual((check.status, check.path, check.engines), ("rejected", path, {}))
        self.assertIn("is unreadable", check.problems[0])

    def test_tracked_process_starts_relabel_engines(self):
        check = calibration.CalibrationCheck(
            "measured",
            engines={"e0": "measured", "e3": "measured"},
            process_starts={"e0": 100.0, "e3": 100.0},
        )
        relaunched = check.at_starts({"e3": 101.0})
        self.assertEqual(relaunched.status, "reused")
        self.assertEqual(relaunched.engines, {"e0": "measured", "e3": "reused"})
        self.assertEqual(check.at_starts({"e0": 100.0, "e3": 100.0}), check)
        uncalibrated = calibration.CalibrationCheck("uncalibrated")
        self.assertIs(uncalibrated.at_starts({"e3": 101.0}), uncalibrated)

    async def _run_with_decode(
        self,
        decode,
        *,
        prefill=None,
        sizing_error=None,
        observation_timeout_s=1.0,
        generations=None,
        samples=2,
    ):
        class FakeClient:
            def __init__(self, **kwargs):
                self.decode = decode

            async def prefill(self, *args):
                return object() if prefill is None else await prefill(*args)

            async def aclose(self):
                pass

        output = Path(self.folder.name) / "calibration.json"
        with (
            patch.object(calibration, "EngineClient", FakeClient),
            patch.object(
                calibration,
                "read_generation",
                new=AsyncMock(
                    return_value=GenerationEvidence("g", {}, 100.0),
                    side_effect=generations,
                ),
            ),
            patch.object(calibration, "engine_context_limit", new=AsyncMock(return_value=1000)),
            patch.object(
                calibration,
                "make_prompt",
                new=AsyncMock(return_value=("hello", 8), side_effect=sizing_error),
            ),
        ):
            code = await calibration.calibrate(
                self.cfg,
                input_tokens=(8,),
                samples_per_group=samples,
                observation_timeout_s=observation_timeout_s,
                out=output,
            )
        return code, json.loads(output.read_text())

    async def _run_recorded(self, client, *, samples, input_tokens=(8,), observation_timeout_s=1.0):
        output = Path(self.folder.name) / "recorded.json"
        client.slots = {spec.url: device_key(spec) for spec in self.cfg.engines}
        with (
            patch.object(calibration, "EngineClient", client),
            patch.object(
                calibration,
                "read_generation",
                new=AsyncMock(return_value=GenerationEvidence("g", {}, 100.0)),
            ),
            patch.object(calibration, "engine_context_limit", new=AsyncMock(return_value=1000)),
            patch.object(
                calibration,
                "make_prompt",
                new=AsyncMock(
                    side_effect=lambda client, url, model, target, *args, **kwargs: (
                        str(target),
                        target,
                    )
                ),
            ),
        ):
            code = await calibration.calibrate(
                self.cfg,
                input_tokens=input_tokens,
                samples_per_group=samples,
                observation_timeout_s=observation_timeout_s,
                out=output,
            )
        return code, json.loads(output.read_text())

    async def test_written_calibration_validates_and_labels_engines(self):
        async def decode(*args, **kwargs):
            yield TOKEN
            yield "data: [DONE]"

        code, document = await self._run_with_decode(decode, samples=100)
        self.assertEqual((code, document["status"]), (0, "complete"))
        self.assertEqual(document["process_starts"], {"e0": 100.0, "e3": 100.0})
        self.assertGreaterEqual(document["duration_s"], 0)
        self.cfg.first_token_timeout_s = document["candidate_deadline_s"] + 0.5
        self.assertEqual(calibration.evidence_problems(self.cfg, document), [])
        self.cfg.first_token_calibration_path = Path(self.folder.name) / "calibration.json"
        for start, status in ((100.0, "measured"), (101.0, "reused")):
            with (
                self.subTest(start=start),
                patch.object(
                    calibration,
                    "read_generation",
                    new=AsyncMock(return_value=GenerationEvidence("g", {}, start)),
                ),
            ):
                check = await calibration.verify_calibration(self.cfg)
                self.assertEqual((check.status, check.problems), (status, ()))
                self.assertEqual(check.engines, {"e0": status, "e3": status})
                self.assertEqual(check.candidate_deadline_s, document["candidate_deadline_s"])
                self.assertEqual(check.captured_at_unix, document["captured_at_unix"])

    async def test_first_sweep_runs_each_pair_alone_in_canonical_order(self):
        self._four_engines()
        iids = {spec.url: spec.iid for spec in self.cfg.engines}
        client = RecordingClient()
        _, document = await self._run_recorded(client, samples=3, input_tokens=(8, 16))
        groups, _ = self._canonical((8, 16), 3)
        first = client.calls[: 2 * len(groups)]
        self.assertTrue(all(prefills + decodes == 1 for *_, prefills, decodes in first))
        self.assertEqual(
            [
                (iids[src], iids[dst], target)
                for leg, src, dst, target, *_ in first
                if leg == "decode"
            ],
            groups,
        )
        number = self._round_numbers()
        for row in document["attempts"]:
            pair = (row["producer"], row["consumer"])
            self.assertEqual(row["round"], None if row["attempt"] == 1 else number[pair])

    async def test_steps_never_overlap_prefill_and_decode(self):
        self._four_engines()
        client = RecordingClient()
        code, document = await self._run_recorded(client, samples=3, input_tokens=(8, 16))
        self.assertEqual(code, 1)
        self.assertTrue(all(row["status"] == "completed" for row in document["attempts"]))
        self.assertEqual(client.violations, [])
        self.assertEqual(max(prefills for *_, prefills, _ in client.calls), 4)
        self.assertEqual(max(decodes for *_, decodes in client.calls), 4)

    async def test_engines_on_one_device_never_share_a_step_leg(self):
        self._four_engines()
        allocation = SharedDeviceAllocation("g0", "GPU-0", 0.5, 0.4)
        self.cfg.engines[0].shared_device = self.cfg.engines[1].shared_device = allocation
        client = RecordingClient()
        _, document = await self._run_recorded(client, samples=2)
        self.assertEqual(client.violations, [])
        number = self._round_numbers()
        rounds = [row for row in document["attempts"] if row["round"] is not None]
        self.assertEqual({row["round"] for row in rounds}, set(range(1, 7)))
        for row in rounds:
            self.assertEqual(row["round"], number[(row["producer"], row["consumer"])])

    async def test_run_prints_schedule_and_duration(self):
        self._four_engines()
        groups, _ = self._canonical((8, 16), 2)
        printed = io.StringIO()
        with redirect_stdout(printed):
            _, document = await self._run_recorded(
                RecordingClient(delay=lambda *args: 0.005), samples=2, input_tokens=(8, 16)
            )
        lines = printed.getvalue().splitlines()
        self.assertEqual(
            lines[0],
            "calibration groups: 24; concurrent rounds per input length: 3; "
            "sweep 1 runs each group alone",
        )
        self.assertRegex(lines[-1], r"^duration \d+s$")
        self.assertGreaterEqual(document["duration_s"], 2 * len(groups) * 0.005)

    async def test_rows_keep_canonical_order_under_shuffled_completion(self):
        self._four_engines()
        timing = random.Random(7)
        client = RecordingClient(delay=lambda *args: timing.uniform(0, 0.004))
        _, document = await self._run_recorded(client, samples=4, input_tokens=(8, 16))
        groups, attempts = self._canonical((8, 16), 4)
        self.assertEqual(
            [
                (row["producer"], row["consumer"], row["target_input_tokens"])
                for row in document["groups"]
            ],
            groups,
        )
        self.assertEqual(
            [
                (row["producer"], row["consumer"], row["target_input_tokens"], row["attempt"])
                for row in document["attempts"]
            ],
            attempts,
        )

    async def test_a_failed_attempt_leaves_concurrent_samples_intact(self):
        self._four_engines()
        urls = {spec.iid: spec.url for spec in self.cfg.engines}

        def fail(src, dst):
            if (src, dst) == (urls["e0"], urls["e1"]):
                return EngineError("decode", dst, 502, "transfer failed")
            return None

        code, document = await self._run_recorded(RecordingClient(fail=fail), samples=3)
        self.assertEqual((code, document["status"]), (1, "incomplete"))
        for group in document["groups"]:
            failed = (group["producer"], group["consumer"]) == ("e0", "e1")
            self.assertEqual((group["completed"], group["failed"]), (0, 3) if failed else (3, 0))
        failures = [row for row in document["attempts"] if row["status"] != "completed"]
        self.assertEqual(len(failures), 3)
        self.assertTrue(all(row["status"] == "failed_transfer" for row in failures))

    async def test_a_failed_prefill_leaves_concurrent_samples_intact(self):
        self._four_engines()
        urls = {spec.iid: spec.url for spec in self.cfg.engines}
        iids = {url: iid for iid, url in urls.items()}

        def fail_prefill(src):
            if src == urls["e0"]:
                return EngineError("prefill", src, 502, "prefill failed")
            return None

        client = RecordingClient(fail_prefill=fail_prefill)
        code, document = await self._run_recorded(client, samples=3)
        self.assertEqual((code, document["status"]), (1, "incomplete"))
        self.assertEqual(client.violations, [])
        self.assertNotIn("e0", {iids[src] for leg, src, *_ in client.calls if leg == "decode"})
        for row in document["attempts"]:
            failed = row["producer"] == "e0"
            self.assertEqual(row["status"], "failed_prefill" if failed else "completed")

    async def test_attempt_budget_excludes_barrier_wait(self):
        self.cfg.request_timeout_s = 0.3
        slow = self.cfg.engines[0].url
        client = RecordingClient(
            delay=lambda leg, src, dst: 0.05 if leg == "decode" else 0.28 if src == slow else 0
        )
        _, document = await self._run_recorded(client, samples=2, observation_timeout_s=0.25)
        statuses = {
            (row["producer"], row["attempt"]): row["status"] for row in document["attempts"]
        }
        self.assertEqual(
            statuses,
            {
                ("e0", 1): "request_expired",
                ("e0", 2): "request_expired",
                ("e3", 1): "completed",
                ("e3", 2): "completed",
            },
        )

    async def test_an_unexpected_error_cancels_the_step_and_writes_no_artifact(self):
        failing, waiting = (spec.url for spec in self.cfg.engines)
        calls = []
        cancelled = []

        async def prefill(url, *args):
            calls.append(url)
            if len(calls) <= 2:
                return object()
            if url == failing:
                await asyncio.sleep(0.01)
                raise KeyError("unexpected")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(url)
                raise

        async def decode(*args, **kwargs):
            yield TOKEN
            yield "data: [DONE]"

        async with asyncio.timeout(2):
            with self.assertRaises(KeyError):
                await self._run_with_decode(decode, prefill=prefill)
        self.assertEqual(cancelled, [waiting])
        self.assertFalse((Path(self.folder.name) / "calibration.json").exists())

    async def test_an_unexpected_decode_error_cancels_the_step_and_writes_no_artifact(self):
        failing, waiting = (spec.url for spec in self.cfg.engines)
        calls = []
        cancelled = []

        async def decode(url, *args, **kwargs):
            calls.append(url)
            if len(calls) > 2 and url == failing:
                await asyncio.sleep(0.01)
                raise KeyError("unexpected")
            if len(calls) > 2:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(url)
                    raise
            yield TOKEN
            yield "data: [DONE]"

        async with asyncio.timeout(2):
            with self.assertRaises(KeyError):
                await self._run_with_decode(decode)
        self.assertEqual(cancelled, [waiting])
        self.assertFalse((Path(self.folder.name) / "calibration.json").exists())

    async def test_slow_working_handoff_is_measured_past_serving_default(self):
        bodies = []

        async def decode(url, endpoint, body, *args, **kwargs):
            bodies.append(body)
            await asyncio.sleep(0.03)
            yield TOKEN
            yield "data: [DONE]"

        code, document = await self._run_with_decode(decode)
        self.assertEqual(code, 1)  # Fewer than 100 samples cannot qualify a fleet.
        self.assertTrue(all(row["status"] == "completed" for row in document["attempts"]))
        self.assertTrue(
            all(
                row["first_token_seconds"] > self.cfg.first_token_timeout_s
                for row in document["attempts"]
            )
        )
        self.assertGreater(document["candidate_deadline_s"], 0.5)
        # An immediate end of text cannot fail calibration.
        self.assertTrue(all(body["ignore_eos"] and body["min_tokens"] >= 1 for body in bodies))

    async def test_a_relaunch_during_calibration_marks_the_generation_changed(self):
        async def decode(*args, **kwargs):
            yield TOKEN
            yield "data: [DONE]"

        starts = {"e0": (100.0, 101.0), "e3": (200.0, 201.0)}
        before = [
            GenerationEvidence("launch", {}, starts[spec.iid][0], f"{spec.iid}-1")
            for spec in self.cfg.engines
        ]
        after = [
            GenerationEvidence("launch", {}, starts[spec.iid][1], f"{spec.iid}-2")
            for spec in self.cfg.engines
        ]
        code, document = await self._run_with_decode(decode, generations=before + after)
        self.assertEqual(code, 1)
        self.assertEqual(document["generations"], {spec.iid: "launch" for spec in self.cfg.engines})
        self.assertEqual(document["process_starts"], {"e0": 100.0, "e3": 200.0})
        self.assertEqual(document["changed_generations"], [spec.iid for spec in self.cfg.engines])

    async def test_stalled_handoff_is_kept_out_of_timing_samples(self):
        async def decode(*args, **kwargs):
            raise EngineError("decode", "http://decode", 504, FIRST_OUTPUT_DETAIL)
            yield ""

        code, document = await self._run_with_decode(decode)
        self.assertEqual(code, 1)
        self.assertIsNone(document["candidate_deadline_s"])
        self.assertTrue(all(row["status"] == "observation_expired" for row in document["attempts"]))

    async def test_stall_after_first_token_obeys_total_request_bound(self):
        self.cfg.request_timeout_s = 0.05
        self.cfg.decode_read_timeout_s = 0

        async def decode(*args, **kwargs):
            yield TOKEN
            await asyncio.Event().wait()

        async with asyncio.timeout(2):
            code, document = await self._run_with_decode(decode, observation_timeout_s=0.04)
        self.assertEqual(code, 1)
        self.assertIsNone(document["candidate_deadline_s"])
        self.assertTrue(all(row["status"] == "request_expired" for row in document["attempts"]))
        self.assertTrue(all("first_token_seconds" in row for row in document["attempts"]))

    async def test_tokenizer_failure_is_retained_in_incomplete_evidence(self):
        decode = AsyncMock()
        code, document = await self._run_with_decode(
            decode, sizing_error=RuntimeError("tokenize returned HTTP 503")
        )
        self.assertEqual(code, 1)
        self.assertIsNone(document["candidate_deadline_s"])
        self.assertTrue(all(row["status"] == "failed_sizing" for row in document["attempts"]))
        self.assertTrue(all("HTTP 503" in row["error"] for row in document["attempts"]))
        decode.assert_not_called()

    async def test_calibration_reserves_available_output_at_context_boundary(self):
        """The real connector and client complete a one-token crossed handoff."""
        self.cfg.engines[1].pin = True
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertLessEqual(int(body["prompt"]) + body["max_tokens"], 1024)
            if not body["stream"]:
                self.assertEqual(body["max_tokens"], 1)
                return httpx.Response(
                    200,
                    json={
                        "kv_transfer_params": {
                            "remote_engine_id": str(request.url.host),
                            "remote_block_ids": [0],
                        }
                    },
                )
            self.assertIn("remote_engine_id", body["kv_transfer_params"])
            return httpx.Response(
                200, text='data: {"choices":[{"text":"x","token_ids":[1]}]}\n\ndata: [DONE]\n\n'
            )

        async def make_prompt(client, url, model, target, dialect, **kwargs):
            return str(target), target

        output = Path(self.folder.name) / "boundary.json"
        with (
            patch.object(
                calibration,
                "EngineClient",
                side_effect=lambda **kwargs: EngineClient(
                    **kwargs, transport=httpx.MockTransport(handle)
                ),
            ),
            patch.object(
                calibration,
                "read_generation",
                new=AsyncMock(return_value=GenerationEvidence("g", {}, 100.0)),
            ),
            patch.object(
                calibration,
                "engine_context_limit",
                new=AsyncMock(
                    side_effect=lambda client, url, *args: (
                        1028 if url == self.cfg.engines[0].url else 1024
                    )
                ),
            ),
            patch.object(calibration, "make_prompt", new=make_prompt),
        ):
            code = await calibration.calibrate(
                self.cfg,
                input_tokens=(128, 1021, 1022, 1023),
                samples_per_group=1,
                observation_timeout_s=1.0,
                out=output,
            )
            with self.assertRaisesRegex(ValueError, "e3: requested 1024 input tokens"):
                await calibration.calibrate(
                    self.cfg,
                    input_tokens=(1024,),
                    samples_per_group=1,
                    observation_timeout_s=1.0,
                    out=Path(self.folder.name) / "oversized.json",
                )
        self.assertEqual(code, 1)  # Boundary behavior does not establish 100-sample qualification.
        document = json.loads(output.read_text())
        self.assertTrue(all(row["status"] == "completed" for row in document["attempts"]))
        expected = {128: 4, 1021: 3, 1022: 2, 1023: 1}
        for row in document["attempts"]:
            self.assertEqual(row["requested_output_tokens"], expected[row["target_input_tokens"]])
        for body in requests:
            if body["stream"]:
                self.assertEqual(body["max_tokens"], expected[int(body["prompt"])])


class CalibrationRoundTests(unittest.TestCase):
    @staticmethod
    def _rounds(specs):
        pairs = validation_pairs(specs, mesh=True)
        slots = {spec.iid: device_key(spec) for spec in specs}
        return pairs, slots, calibration.calibration_rounds(pairs, slots)

    def _assert_rounds(self, pairs, slots, rounds):
        flat = [pair for members in rounds for pair in members]
        self.assertEqual(sorted(flat), sorted(pairs))
        self.assertEqual(len(flat), len(set(flat)))
        for members in rounds:
            producers = [slots[src] for src, _ in members]
            consumers = [slots[dst] for _, dst in members]
            self.assertEqual(len(producers), len(set(producers)))
            self.assertEqual(len(consumers), len(set(consumers)))
            self.assertEqual(members, [pair for pair in pairs if pair in members])

    def test_rounds_cover_every_pair_once_per_slot_role(self):
        for count in range(2, 11):
            with self.subTest(count=count):
                pairs, slots, rounds = self._rounds(engines(count))
                self._assert_rounds(pairs, slots, rounds)
                self.assertEqual(len(rounds), count - 1)

    def test_rounds_treat_a_shared_device_as_one_slot(self):
        pairs, slots, rounds = self._rounds(engines(8, shared={"n0", "n1"}))
        self._assert_rounds(pairs, slots, rounds)
        self.assertEqual(len(rounds), 14)
        for members in rounds:
            self.assertLessEqual(len({"n0", "n1"} & {src for src, _ in members}), 1)
            self.assertLessEqual(len({"n0", "n1"} & {dst for _, dst in members}), 1)

    def test_rounds_for_pinned_roles_match_the_busiest_engine(self):
        specs = [
            EngineSpec(f"p{index}", f"http://p{index}.invalid", Role.PREFILL, pin=True)
            for index in range(4)
        ] + [
            EngineSpec(f"d{index}", f"http://d{index}.invalid", Role.DECODE, pin=True)
            for index in range(2)
        ]
        pairs, slots, rounds = self._rounds(specs)
        self._assert_rounds(pairs, slots, rounds)
        self.assertEqual([len(members) for members in rounds], [2, 2, 2, 2])

    def test_rounds_match_the_busiest_slot_in_mixed_fleets(self):
        choices = random.Random(7)
        groups = [None, None, *(SharedDeviceAllocation(name, name, 0.5, 0.4) for name in "ab")]
        for case in range(300):
            specs = [
                EngineSpec(
                    f"n{index}",
                    f"http://n{index}.invalid",
                    choices.choice(list(Role)),
                    pin=choices.random() < 0.3,
                    shared_device=choices.choice(groups),
                )
                for index in range(choices.randint(2, 9))
            ]
            pairs, slots, rounds = self._rounds(specs)
            with self.subTest(case=case):
                self._assert_rounds(pairs, slots, rounds)
                load = Counter(("out", slots[src]) for src, _ in pairs)
                load.update(("in", slots[dst]) for _, dst in pairs)
                self.assertEqual(len(rounds), max(load.values(), default=0))

    def test_rounds_are_deterministic(self):
        pairs, slots, rounds = self._rounds(engines(8, shared={"n2", "n5"}))
        self.assertEqual(calibration.calibration_rounds(list(pairs), dict(slots)), rounds)


class DeadlineValidationTests(unittest.TestCase):
    def test_rejects_phase_deadlines_that_cannot_take_effect(self):
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        cfg.request_timeout_s = 1.0
        cfg.prefill_timeout_s = 120.0
        cfg.first_token_timeout_s = 2.5
        with self.assertRaisesRegex(ValueError, "request expires before this phase limit"):
            cfg.validate()

    def test_rejects_fractional_graceful_drain(self):
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        cfg.graceful_timeout_s = 0.5
        with self.assertRaisesRegex(ValueError, "must be whole seconds"):
            cfg.validate()

    def test_calibration_path_round_trips_through_fleet_json(self):
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        cfg.first_token_calibration_path = Path("runs/first-token.json")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet.json"
            cfg.save(path)
            loaded = FleetConfig.load(path)
        self.assertEqual(loaded.first_token_calibration_path, cfg.first_token_calibration_path)
