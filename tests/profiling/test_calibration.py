"""Check measured-deadline selection and diagnostic handoff outcomes."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from narwhal.config import FleetConfig
from narwhal.engines.client import FIRST_OUTPUT_DETAIL, EngineError
from narwhal.engines.validation import validation_pairs
from narwhal.profiling import calibration
from tests.fixtures import ROOT


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
        pairs = validation_pairs(self.cfg.engines, mesh=True)
        return {
            "schema": calibration.SCHEMA,
            "schema_version": 1,
            "status": "complete",
            "model": self.cfg.model,
            "contract_fingerprint": self.cfg.engine_contract.fingerprint(),
            "engine_urls": {spec.iid: spec.url for spec in self.cfg.engines},
            "input_tokens": [8],
            "samples_per_group": 100,
            "groups": [
                {
                    "producer": src,
                    "consumer": dst,
                    "target_input_tokens": 8,
                    "completed": 100,
                    "failed": 0,
                    "p99_seconds": 3.75,
                    "maximum_seconds": 3.75,
                    "candidate_deadline_s": 5.0,
                }
                for src, dst in pairs
            ],
            "attempts": [
                {
                    "producer": src,
                    "consumer": dst,
                    "target_input_tokens": 8,
                    "status": "completed",
                    "first_token_seconds": 3.75,
                }
                for src, dst in pairs
                for _ in range(100)
            ],
            "candidate_deadline_s": 5.0,
            "generations": {spec.iid: "old" for spec in self.cfg.engines},
        }

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

    async def test_saved_evidence_rejects_a_changed_engine_generation(self):
        self.cfg.first_token_timeout_s = 5.1
        path = Path(self.folder.name) / "calibration.json"
        self.cfg.first_token_calibration_path = path
        path.write_text(json.dumps(self._complete_document()))
        with patch.object(
            calibration,
            "read_generation",
            new=AsyncMock(return_value=SimpleNamespace(digest="new")),
        ):
            problems = await calibration.verify_calibration(self.cfg)
        self.assertEqual(len(problems), len(self.cfg.engines))
        self.assertTrue(all("process differs" in problem for problem in problems))

    async def _run_with_decode(self, decode, *, sizing_error=None, observation_timeout_s=1.0):
        class FakeClient:
            def __init__(self, **kwargs):
                self.decode = decode

            async def prefill(self, *args):
                return object()

            async def aclose(self):
                pass

        output = Path(self.folder.name) / "calibration.json"
        with (
            patch.object(calibration, "EngineClient", FakeClient),
            patch.object(
                calibration,
                "read_generation",
                new=AsyncMock(return_value=SimpleNamespace(digest="g")),
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
                samples_per_group=2,
                observation_timeout_s=observation_timeout_s,
                out=output,
            )
        return code, json.loads(output.read_text())

    async def test_slow_working_handoff_is_measured_past_serving_default(self):
        async def decode(*args, **kwargs):
            await asyncio.sleep(0.03)
            yield 'data: {"choices":[{"text":"x","token_ids":[1]}]}'
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
            yield 'data: {"choices":[{"text":"x","token_ids":[1]}]}'
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
