"""Check that retries and exact counts move away from engines that failed the request."""

import asyncio
import json
from dataclasses import replace

import httpx

from narwhal.serving.policy import ServingPolicy
from tests.fixtures import fleet
from tests.serving.test_http_accounting import HttpHarness
from tests.wire import EngineWire

FAST_RETRY = {"retry_base_s": 0.001, "retry_cap_s": 0.001}


class RetryPlacementTests(HttpHarness):
    """Two prefill and two decode engines; each test injects faults by engine or by leg."""

    def setUp(self):
        super().setUp()
        self.cfg = fleet(self.root, engines=("e0", "e1", "e3", "e4"))
        self.cfg.tokenize = False
        self.cfg.admission = "open"
        self.cfg.failure_quarantine_s = 0
        self.cfg.serving = ServingPolicy(max_attempts=3, **FAST_RETRY)
        self.iids = {httpx.URL(spec.url).host: spec.iid for spec in self.cfg.engines}
        # Engine ID to the fault every leg it serves meets: "connect", "drop", "hang" or a status.
        self.faults: dict[str, object] = {}
        # Leg ("connect", "tokenize", "prefill" or "decode") to the fault its next occurrence meets.
        self.once: dict[str, object] = {}
        # Each leg as (engine, leg); a refused connection is the leg "connect".
        self.legs: list[tuple[str, str]] = []

    def transports(self):
        wire = EngineWire(self.faulty)

        async def dial(host, port):
            iid = self.iids[host]
            if self.faults.get(iid) == "connect" or self.once.pop("connect", None):
                self.legs.append((iid, "connect"))
                raise ConnectionRefusedError("refused")
            return await wire.dial(host, port)

        return {"transport": httpx.MockTransport(self.engine), "dial": dial}

    async def faulty(self, request):
        iid = self.iids[request.url.host]
        if request.url.path == "/tokenize":
            leg = "tokenize"
        elif (json.loads(request.content).get("kv_transfer_params") or {}).get("do_remote_decode"):
            leg = "prefill"
        else:
            leg = "decode"
        self.legs.append((iid, leg))
        fault = self.once.pop(leg, None) or self.faults.get(iid)
        if fault == "drop":
            raise RuntimeError("engine dropped the connection")
        if fault == "hang":
            await asyncio.Event().wait()
        if isinstance(fault, int):
            return httpx.Response(fault, text="engine busy")
        if leg == "tokenize":
            return httpx.Response(200, json={"count": 7, "tokens": list(range(7))})
        return await self.engine(request)

    def inject(self, leg, fault):
        """Fail the next `leg`; a connect fault refuses the next new connection instead."""
        self.once = {"connect": True} if fault == "connect" else {leg: fault}

    def used(self, *legs):
        """Return the engines that served the named legs, in order."""
        return [iid for iid, leg in self.legs if leg in (*legs, "connect")]

    def attempts(self):
        """Return the engines each failed attempt used, as the journal records them."""
        return [
            (f["phase"], f["prefill_iid"], f["decode_iid"], f["retry_reason"])
            for f in self.terminal_rows()[-1]["attempt_failures"]
        ]

    async def test_a_failed_prefill_engine_is_excluded_from_the_retry(self):
        for fault in ("connect", "drop", "hang", 503):
            with self.subTest(fault=fault):
                self.legs.clear()
                self.cfg.prefill_timeout_s = 0.2 if fault == "hang" else 120.0
                client = self.client()
                self.inject("prefill", fault)
                response = await self.post(client)
                self.assertEqual(response.status_code, 200, response.text)
                first, second = self.used("prefill")
                self.assertEqual({first, second}, {"e0", "e1"})
                ((phase, prefill_iid, _, reason),) = self.attempts()
                self.assertEqual((phase, prefill_iid, reason), ("prefill", first, "allowed"))
                self.assertEqual(self.terminal_rows()[-1]["attempts"], 2)
                self.assert_released()

    async def test_a_decode_failure_before_output_excludes_its_engine_and_producer(self):
        for fault in ("drop", 503):
            with self.subTest(fault=fault):
                self.legs.clear()
                client = self.client()
                self.inject("decode", fault)
                response = await self.post(client)
                self.assertEqual(response.status_code, 200, response.text)
                prefill, decode = self.used("prefill"), self.used("decode")
                self.assertEqual((len(set(prefill)), len(set(decode))), (2, 2))
                ((phase, prefill_iid, decode_iid, reason),) = self.attempts()
                self.assertEqual(
                    (phase, prefill_iid, decode_iid, reason),
                    ("decode", prefill[0], decode[0], "allowed"),
                )
                self.assert_released()

    async def test_a_retry_without_a_remaining_engine_ends_with_503(self):
        # Pinned engines keep their roles, so decode engines cannot take the prefill legs.
        self.cfg.engines = [replace(spec, pin=True) for spec in self.cfg.engines]
        client = self.client()
        self.faults = {"e0": 503, "e1": 503}
        response = await self.post(client)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["type"], "backend_unavailable")
        self.assertEqual(sorted(self.used("prefill")), ["e0", "e1"])
        failures = self.terminal_rows()[-1]["attempt_failures"]
        self.assertEqual(
            [(f["attempt"], f["reason"], f["retry_reason"]) for f in failures],
            [
                (1, "engine_error", "allowed"),
                (2, "engine_error", "allowed"),
                (3, "no_engine", "not_dispatched"),
            ],
        )
        # The third retry never dispatched, so its reserved credit returns.
        budget = self.router.retry_budget
        self.assertEqual((budget.spent, budget.reserved), (1, 0))
        self.assert_released()

    async def test_a_retry_credit_is_spent_only_when_the_retry_dispatches(self):
        client = self.client()
        budget = self.router.retry_budget
        budget.succeeded = lambda: None
        self.inject("prefill", 503)
        self.assertEqual((await self.post(client)).status_code, 200)
        self.assertEqual(
            (budget.spent, budget.reserved, budget.available), (1, 0, budget.capacity - 1)
        )

    async def test_a_failed_exact_count_moves_to_another_engine(self):
        self.cfg.tokenize = True
        self.cfg.serving = ServingPolicy(max_attempts=2, **FAST_RETRY)
        for fault, evidence in (
            ("connect", "ConnectError"),
            ("drop", "RemoteProtocolError"),
            (503, "EngineError"),
        ):
            with self.subTest(fault=fault):
                self.legs.clear()
                client = self.client()
                failed = []
                self.router.verifier.leg_failed = lambda iid, exc, failed=failed, **kw: (
                    failed.append((iid, type(exc).__name__))
                )
                self.inject("tokenize", fault)
                response = await self.post(client)
                self.assertEqual(response.status_code, 200, response.text)
                first, second = self.used("tokenize")
                self.assertNotEqual(first, second)
                self.assertEqual(failed, [(first, evidence)])
                self.assertIn(first, self.router.sizer.backoff)
                self.assertEqual(self.terminal_rows()[-1]["input_len"], 7)

    async def test_exact_count_failures_stop_at_max_attempts(self):
        self.cfg.tokenize = True
        self.cfg.serving = ServingPolicy(max_attempts=2, **FAST_RETRY)
        client = self.client()
        self.faults = dict.fromkeys(self.iids.values(), 503)
        response = await self.post(client)
        self.assertEqual(response.status_code, 502, response.text)
        counted = self.used("tokenize")
        self.assertEqual((len(counted), len(set(counted))), (2, 2))
        self.assertEqual(set(self.router.sizer.backoff), set(counted))
        self.assertEqual(self.terminal_rows()[-1]["reason"], "engine_error")

    async def test_a_rejected_prompt_count_does_not_move(self):
        self.cfg.tokenize = True
        client = self.client()
        self.faults = dict.fromkeys(self.iids.values(), 400)
        await self.post(client)
        self.assertEqual(len(self.used("tokenize")), 1)
