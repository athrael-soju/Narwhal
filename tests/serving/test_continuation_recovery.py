"""Recover synthetic HTTP streams using committed IDs and fresh survivor KV."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.config import ContinuationPolicy
from narwhal.profiling.store import ProfileStore
from narwhal.runtime.lease import FileLease
from narwhal.serving.app import create_app
from narwhal.serving.continuation import HistoryReservation
from narwhal.serving.router import NarwhalRouter
from narwhal.types import Phase, Request
from tests.engines.replay_fixtures import envelope, fixture, wire, write_qualification
from tests.fixtures import fleet, profile


class RecoveryStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail=False, before_failure=None):
        self.chunks = chunks
        self.fail = fail
        self.before_failure = before_failure
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            if self.before_failure is not None:
                await self.before_failure()
            raise httpx.ReadError("SYNTHETIC_PRIVATE_TRANSPORT_DETAIL")

    async def aclose(self):
        self.closed = True


class ContinuationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.cfg.engines.append(
            replace(
                self.cfg.engines[1],
                iid="e4",
                url="http://engine-4.invalid:8000",
                attestation_url="http://engine-4.invalid:8000/v1/attestation",
            )
        )
        self.cfg.admission = "open"
        self.cfg.tokenize = False
        self.cfg.failure_quarantine_s = 0
        self.qualification, document, self.attestation = fixture(
            model=self.cfg.model, engine_ids=tuple(spec.iid for spec in self.cfg.engines)
        )
        self.cfg.engine_contract = document.contract
        store = ProfileStore(self.cfg.profiles_path)
        for spec in self.cfg.engines:
            store.put(
                profile(
                    spec.iid,
                    generation_digest=self.attestation["attestation_digest"],
                    tpot_intercept=0.002 if spec.iid == "e4" else 0.001,
                )
            )
        qualification_path = self.root / "qualification.json"
        pin = write_qualification(qualification_path, self.qualification)
        self.cfg.continuation = ContinuationPolicy(
            enabled=True,
            max_attempts=1,
            recovery_budget=1,
            max_context_tokens=64,
            max_history_bytes=16384,
            max_retained_bytes=16384,
            qualification_path=str(qualification_path),
            qualification_sha256=pin,
        )
        self.by_host = {httpx.URL(spec.url).host: spec.iid for spec in self.cfg.engines}
        self.starts = dict.fromkeys(self.qualification.engines, 100.0)
        self.calls = []
        self.prefills = []
        self.decodes = []
        self.streams = []
        self.failure_at = None
        self.failure_hook = None
        self.qualification_hook = None
        self.recovery_prefill_hold = None
        self.recovery_prefill_started = asyncio.Event()
        self.recovery_prefill_status = 200
        self.recovery_handoff_missing = False
        self.recovery_prefill_delay = 0
        self.plans = {
            "e3": [
                (
                    [
                        wire(envelope((7,), "A")),
                        wire(envelope((8,), "", prompt=None)),
                        wire(envelope((9, 10), "🦄B", prompt=None)),
                    ],
                    True,
                )
            ],
            "e4": [(self.recovered_chunks(), False)],
        }

    def recovered_chunks(self, *, stop=None):
        terminal = (
            envelope((11,), "C", prompt=None, finish="length")
            if stop is None
            else envelope(
                (stop,), "", prompt=None, finish="stop", stop_reason=None if stop == 0 else stop
            )
        )
        usage = envelope(prompt=None)
        usage["choices"] = []
        usage["usage"] = {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6}
        events = [
            envelope((8,), "", prompt=(3, 7)),
            envelope((9,), "🦄", prompt=None),
            envelope((10,), "B", prompt=None),
            terminal,
            usage,
        ]
        for event in events:
            event.update(id="replacement-response", created=2, system_fingerprint="replacement")
        return [*(wire(event) for event in events), b"data: [DONE]\n\n"]

    async def before_failure(self):
        if self.failure_at is None:
            self.failure_at = self.router._clock()
        if self.failure_hook is not None:
            await self.failure_hook()

    async def engine(self, request):
        iid = self.by_host[request.url.host]
        body = json.loads(request.content) if request.content else None
        self.calls.append((iid, request.method, request.url.path, body))
        if request.method == "GET":
            if self.qualification_hook is not None:
                await self.qualification_hook(iid, request.url.path)
            if request.url.path == "/version":
                return httpx.Response(200, json={"version": "test-version"})
            if request.url.path == "/metrics":
                return httpx.Response(200, text=f"process_start_time_seconds {self.starts[iid]}\n")
            if request.url.path == "/v1/attestation":
                return httpx.Response(200, json=self.attestation)
            if request.url.path == "/v1/attestation/continuation":
                return httpx.Response(200, json=self.qualification.engines[iid].fields())
            raise AssertionError(request.url.path)
        self.assertEqual(request.url.path, "/v1/completions")
        phase = (
            "prefill" if body.get("kv_transfer_params", {}).get("do_remote_decode") else "decode"
        )
        instance = self.router.monitor.instances[iid]
        resident = list(getattr(instance, phase).values())
        self.assertEqual(len(resident), 1)
        shape = (resident[0].input_len, resident[0].wanted_len, resident[0].output_len)
        if phase == "prefill":
            if self.prefills:
                self.assertTrue(all(stream.closed for stream in self.streams))
                self.recovery_prefill_started.set()
                if self.recovery_prefill_hold is not None:
                    await self.recovery_prefill_hold.wait()
                if self.recovery_prefill_delay:
                    await asyncio.sleep(self.recovery_prefill_delay)
                if self.recovery_prefill_status != 200:
                    return httpx.Response(
                        self.recovery_prefill_status, text="SYNTHETIC_PRIVATE_PREFILL_DETAIL"
                    )
                if self.recovery_handoff_missing:
                    return httpx.Response(200, json={})
            self.prefills.append((iid, body, shape))
            return httpx.Response(
                200,
                json={
                    "kv_transfer_params": {
                        "remote_engine_id": iid,
                        "remote_block_ids": [len(self.prefills)],
                    }
                },
            )
        self.decodes.append((iid, body, shape))
        self.assertTrue(self.plans.get(iid), f"unexpected decode dispatch to {iid}")
        chunks, fail = self.plans[iid].pop(0)
        stream = RecoveryStream(chunks, fail=fail, before_failure=self.before_failure)
        self.streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    def client(self):
        def router(*args, **kwargs):
            return NarwhalRouter(*args, transport=httpx.MockTransport(self.engine), **kwargs)

        with patch("narwhal.serving.app.NarwhalRouter", side_effect=router):
            app = create_app(self.cfg, journal_path=self.root / "journal.jsonl")
        self.router = app.state.router
        self.router.lifecycle.process_starts = self.starts.copy()
        self.router.lifecycle.identities_ready = True
        self.router.journal.open()
        self.addCleanup(self.router.journal.close)
        self.addAsyncCleanup(self.router.engines.aclose)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router")
        self.addAsyncCleanup(client.aclose)
        return client

    def body(self, **changes):
        return {
            "model": self.cfg.model,
            "prompt": [3],
            "max_tokens": 5,
            "stream": True,
            "narwhal_continuation": True,
            "return_token_ids": True,
            "stream_options": {"include_usage": True},
            **changes,
        }

    async def post(self, client, **changes):
        return await client.post("/v1/completions", json=self.body(**changes))

    def events(self, response):
        return [
            json.loads(line[5:])
            for line in response.text.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"
        ]

    def row(self):
        rows = [
            row
            for line in (self.root / "journal.jsonl").read_text().splitlines()
            if "terminal" in (row := json.loads(line)) and "rid" in row
        ]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def assert_released(self):
        self.assertEqual(self.router.continuation_memory.used, 0)
        self.assertEqual(self.router.inflight, 0)
        self.assertEqual(self.router.ingress_inflight, 0)
        self.assertFalse(self.router.monitor.waiting)
        self.assertTrue(all(stream.closed for stream in self.streams))
        for instance in self.router.monitor.instances.values():
            self.assertFalse(instance.prefill)
            self.assertFalse(instance.decode)
        for queue in self.router.dispatcher.queues.values():
            self.assertEqual(len(queue), 0)
        self.assertNotIn("SYNTHETIC_PRIVATE", (self.root / "journal.jsonl").read_text())

    def assert_terminal_error(self, response, *, prefix="A", terminal="failed"):
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        choices = [choice for event in events for choice in event.get("choices", [])]
        self.assertEqual("".join(choice["text"] for choice in choices), prefix)
        self.assertEqual(sum("error" in event for event in events), 1)
        self.assertNotIn("[DONE]", response.text)
        self.assertNotIn("SYNTHETIC_PRIVATE", response.text)
        self.assertEqual(self.row()["terminal"], terminal)
        self.assert_released()

    async def test_replay_uses_committed_ids_remaining_output_and_fresh_kv(self):
        response = await self.post(self.client())
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        self.assertFalse(any("error" in event for event in events), response.text)
        choices = [choice for event in events for choice in event["choices"]]
        self.assertEqual("".join(choice["text"] for choice in choices), "A🦄BC")
        self.assertEqual(
            [token for choice in choices for token in choice["token_ids"]], [7, 8, 9, 10, 11]
        )
        self.assertEqual(
            [choice["prompt_token_ids"] for choice in choices if "prompt_token_ids" in choice],
            [[3]],
        )
        self.assertEqual(
            {event["id"] for event in events}, {"cmpl-" + response.headers["x-request-id"]}
        )
        self.assertEqual(len({event["created"] for event in events}), 1)
        self.assertNotIn("replacement", response.text)
        self.assertEqual(
            events[-1]["usage"], {"prompt_tokens": 1, "completion_tokens": 5, "total_tokens": 6}
        )
        self.assertEqual(response.text.count("data: [DONE]\n\n"), 1)
        self.assertEqual([iid for iid, _, _ in self.prefills], ["e0", "e0"])
        self.assertEqual([iid for iid, _, _ in self.decodes], ["e3", "e4"])
        self.assertEqual(self.prefills[1][1]["prompt"], [3, 7])
        self.assertEqual(self.decodes[1][1]["prompt"], [3, 7])
        self.assertEqual(self.decodes[1][1]["max_tokens"], 4)
        self.assertEqual(self.prefills[1][2], (2, 4, 0))
        self.assertEqual(self.decodes[1][2], (2, 4, 0))
        self.assertNotEqual(
            self.decodes[0][1]["kv_transfer_params"]["remote_block_ids"],
            self.decodes[1][1]["kv_transfer_params"]["remote_block_ids"],
        )
        row = self.row()
        self.assertEqual((row["input_len"], row["wanted_len"], row["output_len"]), (1, 5, 5))
        self.assertEqual(row["decode_tokens_observed"], 8)
        self.assertEqual(row["decode_attempts"], 2)
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["terminal"], "completed")
        self.assertLessEqual(row["ttft_s"], self.failure_at - row["arrived"])
        self.assertLessEqual(row["first_byte_s"], self.failure_at - row["arrived"])
        self.assertEqual(row["continuation"]["attempts"], 1)
        self.assertEqual(row["continuation"]["replay_input_tokens"], 2)
        self.assertEqual(row["continuation"]["terminal_reason"], "completed")
        self.assertGreaterEqual(row["continuation"]["prefill_seconds"], 0)
        self.assertGreaterEqual(row["continuation"]["interruption_seconds"], 0)
        self.assertEqual(self.router.offered, 1)
        self.assertEqual(self.router.served, 1)
        self.assertEqual(self.router.controller.demand.arrival_count(), 1)
        self.assertEqual(self.router.controller.demand.observed_decode.count(), 1)
        self.assertEqual(self.router.continuation_budget.spent, 1)
        self.assertEqual(self.router.retry_budget.spent, 0)
        self.assertEqual(self.router.retry_attempts, 0)
        self.assert_released()

    async def test_repeated_failure_exhausts_one_recovery_without_duplicate_prefix(self):
        self.plans["e4"] = [([wire(envelope((8,), "B", prompt=(3, 7)))], True)]
        response = await self.post(self.client())
        self.assert_terminal_error(response, prefix="AB")
        self.assertEqual(len(self.decodes), 2)
        self.assertEqual(self.row()["output_len"], 2)
        self.assertEqual(self.router.continuation_budget.spent, 1)
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "attempt_limit")

    async def test_finish_before_incomplete_done_replays_only_the_committed_prefix(self):
        self.plans["e3"] = [
            (
                [
                    wire(envelope((7,), "A")),
                    wire(envelope((8, 9, 10, 11), "🦄BC", prompt=None, finish="length")),
                    b"data: [DONE]\n",
                ],
                False,
            )
        ]
        response = await self.post(self.client())
        events = self.events(response)
        self.assertFalse(any("error" in event for event in events), response.text)
        choices = [choice for event in events for choice in event["choices"]]
        self.assertEqual("".join(choice["text"] for choice in choices), "A🦄BC")
        self.assertEqual(sum(choice["finish_reason"] is not None for choice in choices), 1)
        self.assertEqual(self.decodes[1][1]["prompt"], [3, 7])
        self.assertEqual(self.decodes[1][1]["max_tokens"], 4)
        self.assertEqual(response.text.count("data: [DONE]\n\n"), 1)
        self.assertEqual(self.row()["decode_tokens_observed"], 9)
        self.assertEqual(self.row()["output_len"], 5)
        self.assert_released()

    async def test_invalid_generated_identity_is_terminal_without_recovery_credit(self):
        self.plans["e3"] = [
            (
                [
                    wire(envelope((7,), "A")),
                    wire(envelope((True,), "SYNTHETIC_PRIVATE_TOKEN_DETAIL", prompt=None)),
                ],
                False,
            )
        ]
        response = await self.post(self.client())
        self.assert_terminal_error(response)
        self.assertEqual(len(self.prefills), 1)
        self.assertEqual(self.router.continuation_budget.spent, 0)
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "non_transient")

    async def test_exhausted_shared_recovery_credit_prevents_fresh_prefill(self):
        client = self.client()
        self.assertTrue(self.router.continuation_budget.acquire())
        response = await self.post(client)
        self.assert_terminal_error(response)
        self.assertEqual(len(self.prefills), 1)
        self.assertEqual(len(self.decodes), 1)
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "shared_budget")

    async def test_replay_prefill_price_must_fit_the_remaining_original_deadline(self):
        self.cfg.admission = "predictive"
        client = self.client()
        priced_shapes = []

        def price(request, instance):
            priced_shapes.append((request.input_len, request.wanted_len))
            return 0.001 if request.recovery_deadline is None else self.cfg.request_timeout_s + 1

        with patch.object(self.router.scheduler, "prefill_admission_price", side_effect=price):
            response = await self.post(client)
        self.assert_terminal_error(response)
        self.assertIn((1, 5), priced_shapes)
        self.assertIn((2, 4), priced_shapes)
        self.assertEqual(len(self.prefills), 1)
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "prediction")
        self.assertEqual(self.row()["continuation"]["replay_input_tokens"], 0)
        self.assertEqual(self.router.controller.demand.arrival_count(), 1)

    async def test_no_survivor_terminates_without_reusing_failed_decoder(self):
        async def remove_survivors():
            self.router.scheduler.drain("e0")
            self.router.scheduler.drain("e4")

        self.failure_hook = remove_survivors
        response = await self.post(self.client())
        self.assert_terminal_error(response)
        self.assertEqual(len(self.prefills), 1)
        self.assertEqual([iid for iid, _, _ in self.decodes], ["e3"])

    async def test_stale_survivor_identity_prevents_replacement_decode(self):
        client = self.client()
        self.starts["e4"] = 101.0
        response = await self.post(client)
        self.assert_terminal_error(response)
        self.assertEqual([iid for iid, _, _ in self.decodes], ["e3"])
        self.assertTrue(any(iid == "e4" and method == "GET" for iid, method, _, _ in self.calls))
        self.assertTrue(
            all(
                value == 0
                for value in self.router.scheduler.breaker_snapshot()["failures"]["e4"].values()
            )
        )

    async def test_stale_profile_excludes_decoder_and_uses_qualified_aggregate_survivor(self):
        self.plans["e0"] = [(self.recovered_chunks(), False)]

        async def replace_profile():
            self.router.profiles.put(
                replace(self.router.profiles.get("e4"), generation_digest="sha256:" + "c" * 64)
            )

        self.failure_hook = replace_profile
        response = await self.post(self.client())
        self.assertFalse(any("error" in event for event in self.events(response)), response.text)
        self.assertEqual([iid for iid, _, _ in self.decodes], ["e3", "e0"])
        self.assertNotIn("kv_transfer_params", self.decodes[-1][1])
        self.assertEqual(self.decodes[-1][1]["prompt"], [3, 7])
        self.assertEqual(self.row()["terminal"], "completed")
        self.assertEqual(self.row()["output_len"], 5)
        self.assert_released()

    async def test_profile_change_during_decode_qualification_prevents_dispatch(self):
        changed = asyncio.Event()

        async def replace_profile(iid, path):
            if iid != "e4" or path != "/metrics" or changed.is_set():
                return
            if not self.router.monitor.instances[iid].decode:
                return
            await asyncio.sleep(0)
            self.router.profiles.put(
                replace(self.router.profiles.get(iid), generation_digest="sha256:" + "c" * 64)
            )
            changed.set()

        self.qualification_hook = replace_profile
        response = await self.post(self.client())
        self.assertTrue(changed.is_set())
        self.assert_terminal_error(response)
        self.assertEqual([iid for iid, _, _ in self.decodes], ["e3"])
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "qualification")
        self.assertTrue(
            all(
                value == 0
                for value in self.router.scheduler.breaker_snapshot()["failures"]["e4"].values()
            )
        )

    async def test_recovery_prefill_rejection_is_private_and_terminal(self):
        self.recovery_prefill_status = 500
        response = await self.post(self.client())
        self.assert_terminal_error(response)
        self.assertTrue(self.recovery_prefill_started.is_set())
        self.assertEqual(len(self.decodes), 1)

    async def test_recovery_requires_fresh_valid_handoff(self):
        self.recovery_handoff_missing = True
        response = await self.post(self.client())
        self.assert_terminal_error(response)
        self.assertTrue(self.recovery_prefill_started.is_set())
        self.assertEqual(len(self.decodes), 1)

    async def test_recovery_prefill_cannot_outlive_handoff_age(self):
        self.cfg.serving = replace(self.cfg.serving, handoff_timeout_s=0.01)
        self.recovery_prefill_delay = 0.03
        response = await self.post(self.client())
        self.assert_terminal_error(response)
        self.assertTrue(self.recovery_prefill_started.is_set())
        self.assertEqual(len(self.decodes), 1)
        self.assertEqual(self.row()["continuation"]["failures"]["handoff"], 1)
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "attempt_limit")

    async def test_whole_wave_hold_prevents_recovery_dispatch(self):
        async def hold_wave():
            self.router.lifecycle_blocked = "synthetic whole-wave hold"
            for iid in self.router.monitor.instances:
                self.router.scheduler.drain(iid)

        self.failure_hook = hold_wave
        response = await self.post(self.client())
        self.assert_terminal_error(response)
        self.assertEqual(len(self.prefills), 1)

    async def test_lost_router_lease_prevents_recovery_dispatch(self):
        client = self.client()
        lease = FileLease(self.root / "lease.json", "synthetic-router", 30)
        self.assertTrue(lease.claim())
        self.addCleanup(lease.release)
        self.router.lease = lease

        async def lose_lease():
            lease.release()

        self.failure_hook = lose_lease
        response = await self.post(client)
        self.assert_terminal_error(response)
        self.assertEqual(len(self.prefills), 1)

    async def test_lease_loss_during_recovery_prefill_qualification_prevents_post(self):
        await self.check_qualification_fence("prefill")

    async def test_wave_hold_during_recovery_decode_qualification_prevents_post(self):
        await self.check_qualification_fence("decode")

    async def check_qualification_fence(self, phase):
        client = self.client()
        lease = FileLease(self.root / "lease.json", "synthetic-router", 30)
        self.assertTrue(lease.claim())
        self.addCleanup(lease.release)
        self.router.lease = lease
        fenced = asyncio.Event()

        async def fence_after_qualification_await(iid, path):
            if self.failure_at is None or path != "/metrics" or fenced.is_set():
                return
            instance = self.router.monitor.instances[iid]
            if not getattr(instance, phase):
                return
            await asyncio.sleep(0)
            if phase == "prefill":
                lease.release()
            else:
                self.router.lifecycle_blocked = "synthetic whole-wave hold"
            fenced.set()

        self.qualification_hook = fence_after_qualification_await
        response = await self.post(client)
        self.assertTrue(fenced.is_set())
        self.assert_terminal_error(response)
        self.assertEqual(len(self.prefills), 1 if phase == "prefill" else 2)
        self.assertEqual([iid for iid, _, _ in self.decodes], ["e3"])
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "fenced")

    async def test_original_deadline_cancels_recovery_prefill_and_releases_history(self):
        self.cfg.request_timeout_s = 0.1
        self.recovery_prefill_hold = asyncio.Event()
        response = await self.post(self.client())
        self.assert_terminal_error(response, terminal="expired")
        self.assertTrue(self.recovery_prefill_started.is_set())
        self.assertEqual(self.row()["output_len"], 1)

    async def test_all_committed_output_prevents_another_replay_generation(self):
        self.plans["e3"] = [([wire(envelope((7,), "A"))], True)]
        response = await self.post(self.client(), max_tokens=1)
        self.assert_terminal_error(response)
        self.assertEqual(self.row()["output_len"], 1)
        self.assertEqual(len(self.prefills), 1)
        self.assertEqual(len(self.decodes), 1)
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "output_limit")

    async def test_cancelled_recovery_send_closes_engine_before_releasing_history(self):
        self.client()
        response = await self.router.serve("/v1/completions", self.body(), {})
        blocked = asyncio.Event()
        bodies = []
        releases = []
        close = HistoryReservation.close

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] != "http.response.body" or not message.get("body"):
                return
            if not bodies:
                bodies.append(message["body"])
                return
            blocked.set()
            await asyncio.Event().wait()

        def release(reservation):
            if not reservation.closed:
                self.assertEqual(len(self.streams), 2)
                self.assertTrue(all(stream.closed for stream in self.streams))
                releases.append(reservation.bytes)
            close(reservation)

        with patch.object(HistoryReservation, "close", new=release):
            task = asyncio.create_task(
                response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            )
            try:
                await asyncio.wait_for(blocked.wait(), 2)
                self.assertEqual(response.lifecycle.tokens, 1)
                self.assertEqual(self.router.continuation_memory.used, 16384)
                self.assertEqual(releases, [])
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await response.aclose()
        self.assertEqual(releases, [16384])
        self.assertEqual(len(self.decodes), 2)
        self.assertEqual(self.row()["terminal"], "cancelled")
        self.assertEqual(self.row()["output_len"], 1)
        self.assert_released()

    async def test_eos_stop_survives_replay_without_duplicate_metadata(self):
        await self.check_stop(0)

    async def test_default_token_stop_survives_replay_without_duplicate_metadata(self):
        await self.check_stop(1)

    async def test_requested_token_stop_survives_replay_without_duplicate_metadata(self):
        await self.check_stop(2)

    async def check_stop(self, stop):
        self.plans["e4"] = [(self.recovered_chunks(stop=stop), False)]
        changes = {"stop_token_ids": [2]} if stop == 2 else {}
        response = await self.post(self.client(), max_tokens=6, **changes)
        events = self.events(response)
        self.assertFalse(any("error" in event for event in events), response.text)
        choices = [choice for event in events for choice in event["choices"]]
        self.assertEqual("".join(choice["text"] for choice in choices), "A🦄B")
        self.assertEqual(choices[-1]["finish_reason"], "stop")
        self.assertEqual(choices[-1]["stop_reason"], None if stop == 0 else stop)
        self.assertEqual(choices[-1]["token_ids"], [stop])
        self.assertEqual(response.text.count("data: [DONE]\n\n"), 1)
        self.assertEqual(self.decodes[-1][1]["stop_token_ids"], [2] if stop == 2 else [])
        self.assertEqual(self.decodes[-1][1]["max_tokens"], 5)
        self.assertEqual(self.row()["terminal"], "completed")
        self.assertEqual(self.row()["wanted_len"], 6)
        self.assertEqual(self.row()["output_len"], 5)
        self.assert_released()

    async def test_cancellation_while_waiting_for_survivor_releases_queue_and_history(self):
        self.cfg.serving = replace(
            self.cfg.serving,
            queue_capacity=2,
            queue_timeout_s=5,
            prefill_concurrency=1,
            decode_concurrency=1,
            handoff_timeout_s=5,
        )
        client = self.client()
        waiting = asyncio.Event()
        queue = self.router.dispatcher.queues[Phase.DECODE]
        acquire = queue.acquire

        async def occupy_survivor():
            self.router.monitor.instances["e4"].decode["other"] = Request("other", 1)

        async def observe(reserve, *, deadline):
            def reserve_or_signal():
                result = reserve()
                if result is None:
                    waiting.set()
                return result

            return await acquire(reserve_or_signal, deadline=deadline)

        self.failure_hook = occupy_survivor
        with patch.object(queue, "acquire", new=observe):
            task = asyncio.create_task(self.post(client))
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                self.assertEqual(self.router.continuation_memory.used, 16384)
                self.assertTrue(self.streams[0].closed)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.router.monitor.instances["e4"].decode.pop("other", None)
        self.assertEqual([iid for iid, _, _ in self.decodes], ["e3"])
        self.assertEqual(self.row()["terminal"], "cancelled")
        self.assertEqual(self.row()["output_len"], 1)
        self.assert_released()
