"""Keep phase capacity reserved while continuation qualification performs I/O."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.config import ContinuationPolicy
from narwhal.observability.journal import RunJournal
from narwhal.serving.lifecycle import RequestLifecycle
from narwhal.serving.policy import ServingPolicy
from narwhal.serving.response import RequestStreamResponse
from narwhal.serving.router import NarwhalRouter
from narwhal.types import Phase, Role
from tests.engines.replay_fixtures import fixture, write_qualification
from tests.fixtures import fleet


class ContinuationReservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        cfg = fleet(self.root)
        cfg.admission = "open"
        cfg.serving = ServingPolicy(
            queue_capacity=2,
            queue_timeout_s=5,
            prefill_concurrency=1,
            decode_concurrency=1,
            handoff_timeout_s=5,
        )
        self.qualification, _, _ = fixture(engine_ids=tuple(spec.iid for spec in cfg.engines))
        path = self.root / "qualification.json"
        pin = write_qualification(path, self.qualification)
        cfg.continuation = ContinuationPolicy(
            enabled=True,
            max_attempts=1,
            recovery_budget=1,
            max_context_tokens=32,
            max_history_bytes=16384,
            max_retained_bytes=32768,
            qualification_path=str(path),
            qualification_sha256=pin,
        )
        journal = RunJournal(self.root / "journal.jsonl")
        journal.open()
        self.addCleanup(journal.close)

        def no_network(request):
            raise AssertionError("reservation tests must not dispatch network requests")

        self.router = NarwhalRouter(cfg, journal, transport=httpx.MockTransport(no_network))
        self.addAsyncCleanup(self.router.engines.aclose)
        self.router.lifecycle.process_starts = dict.fromkeys(self.qualification.engines, 100.0)
        self.router.lifecycle.identities_ready = True
        self.states = [RequestLifecycle.offered(self.router, {}) for _ in range(2)]
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, RequestStreamResponse):
                await result.aclose()
        for state in self.states:
            state.finish("cancelled")

    async def check_blocked_qualification(self, phase):
        role = Role.PREFILL if phase is Phase.PREFILL else Role.DECODE
        target = next(inst for inst in self.router.monitor.instances.values() if inst.role is role)
        first, second = self.states
        entered = asyncio.Event()
        release = asyncio.Event()
        second_placing = asyncio.Event()
        qualifying = []
        residents_at_verification = []
        place = self.router.dispatcher.place

        async def verify(qualification, iid, url, attestation_url):
            if iid == target.iid:
                residents = target.prefill if phase is Phase.PREFILL else target.decode
                qualifying.append(iid)
                residents_at_verification.append(set(residents))
                entered.set()
                await release.wait()
            return qualification.engines[iid]

        async def observe_placement(request, *, deadline):
            if request.rid == second.rid and request.phase is phase:
                second_placing.set()
            return await place(request, deadline=deadline)

        async def prefill(url, endpoint, body, headers, *, redact_errors=False):
            return self.router.engines.kv.prefill_result(
                {"kv_transfer_params": {"remote_engine_id": "producer", "remote_block_ids": [0]}},
                url=url,
                endpoint=endpoint,
                request_id=headers["x-request-id"],
            )

        self.enterContext(patch.object(self.router.engines, "verify_continuation", new=verify))
        self.enterContext(patch.object(self.router.engines, "prefill", new=prefill))
        self.enterContext(patch.object(self.router.dispatcher, "place", new=observe_placement))
        body = {
            "prompt": [3],
            "max_tokens": 4,
            "stream": True,
            "narwhal_continuation": True,
        }

        def start(state):
            task = asyncio.create_task(
                self.router.serve("/v1/completions", body, {}, lifecycle=state)
            )
            self.tasks.append(task)
            return task

        first_task = start(first)
        await asyncio.wait_for(entered.wait(), 2)
        second_task = start(second)
        await asyncio.wait_for(second_placing.wait(), 2)

        self.assertEqual(qualifying, [target.iid])
        self.assertEqual(residents_at_verification, [{first.rid}])
        residents = target.prefill if phase is Phase.PREFILL else target.decode
        self.assertEqual(set(residents), {first.rid})
        self.assertEqual(len(self.router.dispatcher.queues[phase]), 1)
        self.assertFalse(second_task.done())

        release.set()
        first_response = await asyncio.wait_for(first_task, 2)
        self.assertIsInstance(first_response, RequestStreamResponse)
        await first_response.aclose()
        second_response = await asyncio.wait_for(second_task, 2)
        self.assertIsInstance(second_response, RequestStreamResponse)
        self.assertEqual(qualifying, [target.iid, target.iid])
        self.assertEqual(residents_at_verification, [{first.rid}, {second.rid}])
        await second_response.aclose()

        self.assertEqual(self.router.inflight, 0)
        self.assertEqual(self.router.continuation_memory.used, 0)
        self.assertFalse(self.router.monitor.waiting)
        for inst in self.router.monitor.instances.values():
            self.assertFalse(inst.prefill)
            self.assertFalse(inst.decode)
        for queue in self.router.dispatcher.queues.values():
            self.assertEqual(len(queue), 0)

    async def test_prefill_qualification_holds_the_only_prefill_slot(self):
        await self.check_blocked_qualification(Phase.PREFILL)

    async def test_decode_qualification_holds_the_only_decode_slot(self):
        await self.check_blocked_qualification(Phase.DECODE)
