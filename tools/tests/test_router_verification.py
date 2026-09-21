"""Check breaker probes, recovery decisions and admission cleanup."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.engines.client import EngineError, InferenceProbe, ProbeLeg
from narwhal.serving.admission import QueueExpired
from narwhal.serving.app import create_app
from tools.tests.fixtures import fleet


class RouterVerificationTests(unittest.IsolatedAsyncioTestCase):
    """Probe outcomes resolve actual scheduler holds and preserve request accounting."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.cfg.engine_contract = None
        self.cfg.eject_after = 1
        self.router = create_app(self.cfg).state.router
        self.addAsyncCleanup(self.router.engines.aclose)

    async def test_input_length_rotates_after_failure_and_reuses_a_successful_tokenizer(self):
        """Failed tokenization rotates the preferred engine and returns the local estimate."""
        self.cfg.tokenize = True
        with patch.object(
            self.router.engines, "token_count", new=AsyncMock(side_effect=[None, 9, 10])
        ) as count:
            self.assertEqual(
                await self.router.input_length({"prompt": "hello"}),
                self.router.estimate_length({"prompt": "hello"}),
            )
            self.assertEqual(await self.router.input_length({"prompt": "hello"}), 9)
            self.assertEqual(await self.router.input_length({"prompt": "hello"}), 10)
        self.assertNotEqual(count.call_args_list[0].args[0], count.call_args_list[1].args[0])
        self.assertEqual(count.call_args_list[1].args[0], count.call_args_list[2].args[0])
        self.assertEqual(self.router.estimate_length({"prompt": [1, 2, 3]}), 3)
        self.assertGreaterEqual(
            self.router.estimate_length({"messages": [{"content": "hello"}]}), 1
        )
        for iid in ("e0", "e3"):
            self.router.scheduler.eject(iid)
        with patch.object(self.router.engines, "token_count", new=AsyncMock()) as count:
            self.assertEqual(await self.router.input_length({"prompt": ""}), 1)
            count.assert_not_awaited()

    async def test_health_verification_keeps_inconclusive_holds_and_resolves_health_evidence(self):
        """Ejecting a suspect engine requires a failed health probe."""
        for answer in (None, True, False):
            with (
                self.subTest(answer=answer),
                patch.object(self.router.engines, "healthy", new=AsyncMock(return_value=answer)),
            ):
                await self.router._verify_health("e0", self.cfg.engines[0].url)
            self.assertEqual("e0" in self.router.scheduler.ejected, answer is False)

    def test_probe_resolution_defers_inconclusive_legs_and_ejects_failed_inference(self):
        """Only two conclusive passing legs permit inference recovery."""
        for probe, result in (
            (None, False),
            (InferenceProbe(ProbeLeg(inconclusive=True), ProbeLeg()), False),
            (InferenceProbe(ProbeLeg(), ProbeLeg()), True),
            (InferenceProbe(ProbeLeg(failed="kv_handoff"), ProbeLeg()), False),
            (InferenceProbe(ProbeLeg(), ProbeLeg(failed="stream")), False),
        ):
            with self.subTest(probe=probe):
                self.assertIs(self.router._resolve_inference_probe("e0", probe), result)
        self.assertIn("e0", self.router.scheduler.ejected)

    async def test_inference_recovery_verifies_every_recorded_producer(self):
        """Recovery clears a suspect after every recorded transfer path passes."""
        self.router._inference_sources["e3"] = {"", "e0"}
        self.router.scheduler.inference_suspects.add("e3")
        with patch.object(
            self.router.engines,
            "probe_inference",
            new=AsyncMock(return_value=InferenceProbe(ProbeLeg(), ProbeLeg())),
        ) as probe:
            await self.router._verify_inference("e3", self.cfg.engines[1].url)
        self.assertEqual(probe.await_count, 2)
        self.assertEqual(
            [call.kwargs["prefill_url"] for call in probe.call_args_list],
            [None, self.cfg.engines[0].url],
        )
        self.assertNotIn("e3", self.router._inference_sources)
        self.assertNotIn("e3", self.router.scheduler.inference_suspects)

    async def test_new_or_missing_transfer_paths_keep_verification_pending(self):
        """Recovery waits for checks of newly added producers."""
        self.router._inference_sources["e3"] = {"unknown"}
        with patch.object(self.router.engines, "probe_inference", new=AsyncMock()) as probe:
            await self.router._verify_inference("e3", self.cfg.engines[1].url)
            probe.assert_not_awaited()
        self.router._inference_sources["e3"] = {""}

        async def changed(*args, **kwargs):
            self.router._inference_sources["e3"].add("e0")
            return InferenceProbe(ProbeLeg(), ProbeLeg())

        with patch.object(self.router.engines, "probe_inference", side_effect=changed):
            await self.router._verify_inference("e3", self.cfg.engines[1].url)
        self.assertEqual(self.router._inference_sources["e3"], {"", "e0"})
        with patch.object(self.router.engines, "probe_inference", new=AsyncMock(return_value=None)):
            await self.router._verify_inference("e3", self.cfg.engines[1].url)
        self.assertIn("e3", self.router._inference_sources)

    async def test_verification_task_cleanup_runs_after_failure_and_missing_engine(self):
        """Every completed verification releases its deduplication key and records cadence."""
        for iid, kind in (
            ("unknown", "verify_health"),
            ("e0", "verify_health"),
            ("e3", "verify_inference"),
        ):
            self.router.scheduler.verifying.add((iid, kind))
            method = "_verify_inference" if kind == "verify_inference" else "_verify_health"
            with (
                self.subTest(iid=iid),
                patch.object(self.router, method, side_effect=ValueError("probe failed")),
            ):
                if iid == "unknown":
                    await self.router._verify_suspect(iid, kind)
                else:
                    with self.assertLogs("narwhal", level="ERROR"):
                        await self.router._verify_suspect(iid, kind)
            self.assertNotIn((iid, kind), self.router.scheduler.verifying)
            self.assertIn(iid, self.router._verification_at)
        with patch.object(self.router, "_verify_suspect", new=AsyncMock()):
            self.router._start_verification("e0", "verify_health")
            await asyncio.gather(*self.router._verification_tasks)
            await asyncio.sleep(0)
        self.assertFalse(self.router._verification_tasks)

    def test_leg_failures_deduplicate_probes_and_preserve_source_identity(self):
        """Repeated stream failures start one probe and record the producer ID."""
        with patch.object(self.router, "_start_verification") as start:
            self.router._leg_failed(
                "e3", httpx.ReadTimeout("decode"), prefill_iid="e0", decode_leg=True
            )
            self.router._leg_failed(
                "e3", httpx.ReadTimeout("decode"), prefill_iid="e0", decode_leg=True
            )
            start.assert_called_once_with("e3", "verify_inference")
        self.assertEqual(self.router._inference_sources["e3"], {"e0"})
        with patch.object(self.router.scheduler, "record_failure") as failure:
            self.router._leg_failed("e0", httpx.PoolTimeout("pool"))
            self.router._leg_failed("e0", EngineError("prefill", "http://engine", 400, "invalid"))
            failure.assert_not_called()

    async def test_admission_rejection_and_queue_expiry_leave_one_terminal_outcome(self):
        """Rejected and expired requests leave the queue before returning 429 and 504."""
        with patch.object(self.router, "max_concurrent", 0):
            response = await self.router.serve(
                "/v1/completions", {"model": "stub", "prompt": "x"}, {}
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "1")
        with patch.object(self.router.admission_queue, "acquire", side_effect=QueueExpired()):
            response = await self.router.serve(
                "/v1/completions", {"model": "stub", "prompt": "x"}, {}
            )
        self.assertEqual(response.status_code, 504)
        self.assertFalse(self.router.monitor.waiting)
        self.assertEqual(self.router.inflight, 0)
        self.assertEqual((self.router.rejected, self.router.expired), (1, 1))

    async def test_bounded_queue_publishes_prefill_risk_before_waiting(self):
        """A sized offer joins live waiting state before it signals control."""

        def signal(request, *, at):
            self.assertIs(self.router.monitor.waiting[request.rid], request)
            return True

        with (
            patch.object(self.router.controller, "note_prefill_risk", side_effect=signal) as note,
            patch.object(self.router.admission_queue, "acquire", side_effect=QueueExpired()),
        ):
            response = await self.router.serve(
                "/v1/completions", {"model": "stub", "prompt": "x"}, {}
            )
        self.assertEqual(response.status_code, 504)
        note.assert_called_once()
        self.assertTrue(self.router.control_wakeup.is_set())
        self.assertFalse(self.router.monitor.waiting)

    async def test_unexpected_admission_failure_releases_request_ownership(self):
        """A queue exception clears waiting state and counts one failed request before reraising."""
        with (
            patch.object(
                self.router.admission_queue, "acquire", side_effect=ValueError("queue failed")
            ),
            self.assertRaisesRegex(ValueError, "queue failed"),
        ):
            await self.router.serve("/v1/completions", {"model": "stub", "prompt": "x"}, {})
        self.assertFalse(self.router.monitor.waiting)
        self.assertEqual(self.router.inflight, 0)
        self.assertEqual(self.router.failed, 1)
