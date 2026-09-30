"""Check breaker probes, recovery decisions and admission cleanup."""

import asyncio
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.engines.client import (
    FIRST_OUTPUT_DETAIL,
    EngineError,
    InferenceProbe,
    ProbeLeg,
    Tokenization,
)
from narwhal.serving.admission import QueueExpired
from narwhal.serving.app import create_app
from narwhal.serving.execution import _failed_leg
from narwhal.types import Phase, Request
from tests.fixtures import bind_identity_profiles, fleet


class RouterVerificationTests(unittest.IsolatedAsyncioTestCase):
    """Probe outcomes resolve actual scheduler holds and preserve request accounting."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.cfg.engine_contract = None
        self.cfg.eject_after = 1
        self.router = create_app(self.cfg).state.router
        bind_identity_profiles(self.router)
        self.addAsyncCleanup(self.router.engines.aclose)

    async def test_token_id_prompt_uses_its_exact_length_without_tokenization(self):
        """vLLM's string-only tokenizer is unnecessary for a token-ID completion."""
        self.cfg.tokenize = True
        with patch.object(
            self.router.engines,
            "tokenize",
            new=AsyncMock(
                side_effect=EngineError("tokenize", "http://engine", 400, "string required")
            ),
        ) as count:
            self.assertEqual(await self.router.input_length({"prompt": list(range(512))}), 512)
            self.assertEqual(await self.router.input_length({"prompt": [0]}), 1)
            count.assert_not_awaited()

    async def test_text_chat_and_non_token_arrays_keep_strict_tokenization(self):
        """Only a valid flat token-ID prompt bypasses the exact-count route."""
        self.cfg.tokenize = True
        bodies = [
            {"prompt": "hello"},
            {"messages": [{"role": "user", "content": "hello"}]},
            {"prompt": [1], "messages": [{"role": "user", "content": "hello"}]},
            *({"prompt": prompt} for prompt in ([], [True], [-1], [1.5], ["hello"], [[1]])),
        ]
        for body in bodies:
            with (
                self.subTest(body=body),
                patch.object(
                    self.router.engines,
                    "tokenize",
                    new=AsyncMock(
                        side_effect=EngineError("tokenize", "http://engine", 504, "late")
                    ),
                ) as count,
                self.assertRaisesRegex(EngineError, "late"),
            ):
                await self.router.input_length(body)
            self.assertTrue(count.await_args.kwargs["strict"])

    async def test_input_length_skips_a_failed_tokenizer_until_a_count_succeeds(self):
        """Failed exact counting fails this request; the next counts avoid that engine."""
        self.cfg.tokenize = True
        with patch.object(
            self.router.engines,
            "tokenize",
            new=AsyncMock(
                side_effect=[
                    EngineError("tokenize", "http://engine", 504, "late"),
                    Tokenization(9, None),
                    Tokenization(10, None),
                ]
            ),
        ) as count:
            with self.assertRaisesRegex(EngineError, "late"):
                await self.router.input_length({"prompt": "hello"})
            self.assertEqual(await self.router.input_length({"prompt": "hello"}), 9)
            self.assertEqual(await self.router.input_length({"prompt": "hello"}), 10)
        urls = [call.args[0] for call in count.call_args_list]
        self.assertNotEqual(urls[0], urls[1])
        self.assertIsNone(self.router._tokenize_failed)
        self.assertEqual(self.router.estimate_length({"prompt": [1, 2, 3]}), 3)
        self.assertGreaterEqual(
            self.router.estimate_length({"messages": [{"content": "hello"}]}), 1
        )
        for iid in ("e0", "e3"):
            self.router.scheduler.eject(iid)
        with patch.object(self.router.engines, "tokenize", new=AsyncMock()) as count:
            self.assertEqual(await self.router.input_length({"prompt": ""}), 1)
            count.assert_not_awaited()

    async def test_exact_counts_spread_across_the_least_occupied_engines(self):
        """Idle engines share the counts in turn; an occupied engine is left out."""
        self.cfg.tokenize = True
        live = self.router.scheduler.live_instances()
        busy = live[0]
        busy.decode["resident"] = Request("resident", 100, wanted_len=10)
        with patch.object(
            self.router.engines, "tokenize", new=AsyncMock(return_value=Tokenization(5, None))
        ) as count:
            for _ in range(2 * (len(live) - 1)):
                await self.router.input_length({"prompt": "hello"})
        urls = [call.args[0] for call in count.call_args_list]
        self.assertNotIn(busy.url, urls)
        self.assertEqual(set(urls), {i.url for i in live[1:]})
        self.assertEqual(max(urls.count(u) for u in set(urls)), 2)

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
                "/v1/completions", {"model": self.cfg.model, "prompt": "x"}, {}
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "1")
        with patch.object(self.router.admission_queue, "acquire", side_effect=QueueExpired()):
            response = await self.router.serve(
                "/v1/completions", {"model": self.cfg.model, "prompt": "x"}, {}
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
                "/v1/completions", {"model": self.cfg.model, "prompt": "x"}, {}
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
            await self.router.serve("/v1/completions", {"model": self.cfg.model, "prompt": "x"}, {})
        self.assertFalse(self.router.monitor.waiting)
        self.assertEqual(self.router.inflight, 0)
        self.assertEqual(self.router.failed, 1)


class OverloadVerificationTests(unittest.IsolatedAsyncioTestCase):
    """Overload stays out of inference verification; the only engine of a pinned role stays live."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.cfg.engine_contract = None
        self.cfg.eject_after = 1
        self.timeout = EngineError(
            "decode", "http://engine", 504, f"{FIRST_OUTPUT_DETAIL} 1.5s: no frames at all"
        )
        self.failed = InferenceProbe(ProbeLeg(), ProbeLeg(failed="stream"))

    def router(self, *, pinned=False):
        if pinned:
            self.cfg.engines = [replace(spec, pin=True) for spec in self.cfg.engines]
        router = create_app(self.cfg).state.router
        bind_identity_profiles(router)
        self.addAsyncCleanup(router.engines.aclose)
        return router

    def test_first_token_timeouts_count_as_overload_while_the_engine_produces_output(self):
        router = self.router()
        with patch.object(router, "_start_verification") as start:
            router._leg_failed(
                "e3", self.timeout, prefill_iid="e0", decode_leg=True, progressed=True
            )
            start.assert_called_once_with("e3", "verify_health")
            self.assertNotIn("e3", router.scheduler.inference_suspects)
            router._leg_failed(
                "e3", self.timeout, prefill_iid="e0", decode_leg=True, progressed=False
            )
            start.assert_called_with("e3", "verify_inference")
        self.assertIn("e3", router.scheduler.inference_suspects)

    def test_failed_legs_report_output_after_the_leg_started(self):
        router = self.router()
        inst = router.monitor.instances["e3"]
        request = Request("r", 10, phase=Phase.DECODE)
        other = Request("o", 10, phase=Phase.DECODE)
        state = SimpleNamespace(router=router, prefill_iid="e0", request=request)
        router.monitor.dispatched("e3", request)
        router.monitor.dispatched("e3", other)
        started = router._clock()
        with patch.object(router, "_leg_failed") as leg:
            _failed_leg(state, inst, self.timeout, decode=True, started=started)
            self.assertIs(leg.call_args.kwargs["progressed"], False)
            router.monitor.output_token("e3", "o")
            _failed_leg(state, inst, self.timeout, decode=True, started=started)
            self.assertIs(leg.call_args.kwargs["progressed"], True)

    def test_a_hung_engine_with_resident_work_reaches_inference_verification(self):
        router = self.router()
        inst = router.monitor.instances["e3"]
        request = Request("r", 10, phase=Phase.DECODE)
        state = SimpleNamespace(router=router, prefill_iid="e0", request=request)
        router.monitor.dispatched("e3", request)
        router.monitor.dispatched("e3", Request("stuck", 10, phase=Phase.DECODE))
        with patch.object(router, "_start_verification") as start:
            _failed_leg(state, inst, self.timeout, decode=True, started=router._clock())
        start.assert_called_once_with("e3", "verify_inference")

    def test_a_sole_role_engine_leaves_its_hold_after_a_failed_probe(self):
        router = self.router(pinned=True)
        router.scheduler.inference_suspects.add("e3")
        router.scheduler.quarantined["e3"] = math.inf
        self.assertFalse(router._resolve_inference_probe("e3", self.failed))
        self.assertNotIn("e3", router.scheduler.quarantined)
        self.assertNotIn("e3", router.scheduler.ejected)

    async def test_health_verification_and_timed_holds_keep_a_sole_role_engine_live(self):
        for pinned, held in ((True, False), (False, True)):
            with self.subTest(pinned=pinned):
                self.setUp()
                router = self.router(pinned=pinned)
                self.assertIs(router.scheduler.quarantine("e3", 5.0), held)
                router.scheduler.quarantined.pop("e3", None)
                with patch.object(router.engines, "healthy", new=AsyncMock(return_value=False)):
                    await router._verify_health("e3", self.cfg.engines[1].url)
                self.assertEqual("e3" in router.scheduler.ejected, held)

    def test_the_only_engine_for_a_pinned_role_stays_live(self):
        router = self.router(pinned=True)
        with patch.object(router, "_start_verification"):
            router._leg_failed("e3", self.timeout, prefill_iid="e0", decode_leg=True)
        self.assertIn("e3", router.scheduler.inference_suspects)
        self.assertNotIn("e3", router.scheduler.quarantined)
        self.assertFalse(router._resolve_inference_probe("e3", self.failed))
        self.assertNotIn("e3", router.scheduler.ejected)

    def test_unpinned_fleets_hold_and_eject_a_failed_engine(self):
        router = self.router()
        with patch.object(router, "_start_verification"):
            router._leg_failed("e3", self.timeout, prefill_iid="e0", decode_leg=True)
        self.assertEqual(router.scheduler.quarantined["e3"], math.inf)
        self.assertFalse(router._resolve_inference_probe("e3", self.failed))
        self.assertIn("e3", router.scheduler.ejected)

    async def test_inference_probes_use_the_longer_first_token_or_health_budget(self):
        router = self.router()
        router.cfg.first_token_timeout_s = 1.5
        router.cfg.health_timeout_s = 5.0
        passed = InferenceProbe(ProbeLeg(), ProbeLeg())
        with patch.object(
            router.engines, "probe_inference", new=AsyncMock(return_value=passed)
        ) as probe:
            await router._verify_inference("e3", self.cfg.engines[1].url)
        self.assertEqual(probe.call_args.kwargs["deadline_s"], 5.0)

    def test_a_failed_producer_leg_names_the_deferral(self):
        router = self.router()
        probe = InferenceProbe(ProbeLeg(failed="inference_status"), ProbeLeg(inconclusive=True))
        with self.assertLogs("narwhal", level="INFO") as logs:
            self.assertFalse(router._resolve_inference_probe("e3", probe))
        output = "\n".join(logs.output)
        self.assertIn("producer leg failed inference_status", output)
        self.assertNotIn("control pool", output)
