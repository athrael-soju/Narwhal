"""Check outcome reasons, journal error fields, attempt failures and split queue waits."""

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from narwhal.engines.client import EngineError
from narwhal.serving.admission import QueueExpired
from narwhal.serving.outcomes import OUTCOME_REASONS, error_response, failure_reason
from narwhal.serving.policy import ServingPolicy
from tests.fixtures import fleet
from tests.serving.test_http_accounting import HttpHarness

FAST_RETRY = {"retry_base_s": 0.001, "retry_cap_s": 0.001}
QUEUED = {
    "queue_capacity": 2,
    "queue_timeout_s": 5.0,
}


class OutcomeReasonTests(HttpHarness):
    """Each outcome carries one reason in the journal, `/narwhal/state` and `/metrics`."""

    def setUp(self):
        super().setUp()
        self.cfg.failure_quarantine_s = 0

    async def metrics(self, client):
        return (await client.get("/metrics")).text

    def journal_meta(self):
        lines = (self.root / "journal.jsonl").read_text().splitlines()
        return next(row["meta"] for line in lines if "meta" in (row := json.loads(line)))

    async def test_metrics_list_every_reason_and_the_admission_mode_from_start(self):
        self.cfg.admission_margin = 0.25
        client = self.client()
        text = await self.metrics(client)
        for terminal, reasons in OUTCOME_REASONS.items():
            label = "cause" if terminal == "refused" else "reason"
            for reason in reasons:
                with self.subTest(terminal=terminal, reason=reason):
                    self.assertIn(f'narwhal_{terminal}_total{{{label}="{reason}"}} 0', text)
        self.assertIn('narwhal_admission_info{mode="open"} 1', text)
        self.assertIn("narwhal_admission_margin 0.25", text)
        self.assertIn("narwhal_served_after_retry_total 0", text)
        state = (await client.get("/narwhal/state")).json()
        self.assertEqual((state["admission"]["mode"], state["admission"]["margin"]), ("open", 0.25))
        self.assertEqual(state["outcome_reasons"]["failed"]["engine_error"], 0)
        self.assertEqual(self.journal_meta()["admission"], {"mode": "open", "margin": 0.25})

    async def test_rejections_record_reason_status_and_error_type(self):
        client = self.client()
        router = self.router
        router.loop_lag_s = router.scheduler.slo.ttft_s
        self.assertEqual((await self.post(client)).status_code, 429)
        router.loop_lag_s = 0.0
        # Ingress and the admission queue enforce one in-flight limit with one message.
        router.ingress_inflight = router.max_concurrent
        at_ingress = await self.post(client)
        router.ingress_inflight = 0
        router.inflight = router.max_concurrent
        at_queue = await self.post(client)
        router.inflight = 0
        self.assertEqual((at_ingress.status_code, at_queue.status_code), (429, 429))
        self.assertEqual(at_ingress.json(), at_queue.json())
        self.assertEqual(at_ingress.json()["error"]["message"], "router in-flight limit reached")
        router.lifecycle.identities_ready = False
        not_ready = await self.post(client)
        self.assertEqual(not_ready.status_code, 503)
        rows = self.terminal_rows()
        self.assertEqual(
            [(row["reason"], row["status"], row["error_type"]) for row in rows],
            [
                ("saturated", 429, "server_overloaded_error"),
                ("inflight_limit", 429, "server_overloaded_error"),
                ("inflight_limit", 429, "server_overloaded_error"),
                ("not_ready", 503, "standby"),
            ],
        )
        self.assertEqual(rows[-1]["error_code"], not_ready.json()["error"]["code"])
        self.assertEqual(rows[-1]["readiness_reason"], "engine identity validation pending")
        self.assertEqual(rows[-1]["readiness_reason"], not_ready.json()["error"]["message"])
        self.assertTrue(all(row["terminal"] == "rejected" for row in rows))
        text = await self.metrics(client)
        for reason, count in (("saturated", 1), ("inflight_limit", 2), ("not_ready", 1)):
            self.assertIn(f'narwhal_rejected_total{{reason="{reason}"}} {count}', text)
        self.assertEqual(router.rejected, 4)
        self.assert_released()

    async def test_invalid_bodies_record_the_public_error_type(self):
        client = self.client()
        bad = await client.post(
            "/v1/completions", content="{", headers={"content-type": "application/json"}
        )
        wrong = await client.post("/v1/completions", json={"model": "other", "prompt": "x"})
        self.assertEqual((bad.status_code, wrong.status_code), (400, 404))
        rows = self.terminal_rows()
        self.assertEqual(
            [(row["status"], row["error_type"], row["error_code"], row["reason"]) for row in rows],
            [
                (400, "invalid_request_error", None, None),
                (404, "invalid_request_error", "model_not_found", None),
            ],
        )
        # Requests that never reach admission leave the queue-wait histogram empty.
        self.assertEqual(sum(h.n for h in self.router.queue_wait.values()), 0)
        self.assertTrue(all(row["queue_waits"]["admission"] is None for row in rows))

    async def test_an_engine_failure_records_its_reason_and_one_attempt(self):
        self.prefill_statuses = [500]
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 502)
        row = self.terminal_rows()[-1]
        error = response.json()["error"]
        self.assertEqual(
            (row["terminal"], row["reason"], row["status"]), ("failed", "engine_error", 502)
        )
        self.assertEqual((row["error_type"], row["error_code"]), (error["type"], error["code"]))
        (failure,) = row["attempt_failures"]
        self.assertEqual(
            (failure["attempt"], failure["phase"], failure["reason"], failure["retry_reason"]),
            (1, "prefill", "engine_error", "attempt_limit"),
        )
        text = await self.metrics(client)
        self.assertIn('narwhal_failed_total{reason="engine_error"} 1', text)
        self.assertIn(
            'narwhal_attempt_failures_total{phase="prefill",reason="engine_error"} 1', text
        )
        self.assert_released()

    async def test_an_engine_fault_names_its_phase_as_the_error_type(self):
        """Each fault phase returns its documented status, error type and code."""

        async def slow_count(*args, **kwargs):
            raise EngineError("tokenize", "http://engine", 504, "exact count exceeded 1s")

        for phase, status, upstream in (
            ("admission", 504, None),
            ("prefill", 502, "prefill"),
            ("decode", 502, "decode"),
            ("decode", 504, "decode"),
        ):
            with self.subTest(phase=phase, status=status):
                self.cfg.tokenize = upstream is None
                self.prefill_statuses = [500] if upstream == "prefill" else []
                self.decode_statuses = [status] if upstream == "decode" else []
                client = self.client()
                if upstream is None:
                    self.router.engines.tokenize = slow_count
                response = await self.post(client)
                self.assertEqual(response.status_code, status)
                error = response.json()["error"]
                self.assertEqual((error["type"], error["code"]), (phase, phase))
                row = self.terminal_rows()[-1]
                self.assertEqual((row["status"], row["error_type"]), (status, phase))

    async def test_an_unregistered_error_type_ends_as_an_internal_error(self):
        """A fault in a phase outside the error tables never reaches a client as that type."""

        async def fault_while_waiting(state, *args):
            state.phase = "queue"
            raise RuntimeError("placement state changed")

        client = self.client()
        with (
            patch("narwhal.serving.execution.prepare_attempt", fault_while_waiting),
            self.assertRaisesRegex(ValueError, "unregistered error response: HTTP 502 queue"),
        ):
            await self.post(client)
        row = self.terminal_rows()[-1]
        self.assertEqual(
            (row["terminal"], row["reason"], row["status"], row["error_type"]),
            ("failed", "internal", 500, None),
        )
        self.assert_released()

    async def test_a_failure_after_output_records_the_streamed_status_and_error(self):
        self.decode_frames.append({"error": {"message": "decode failed"}})
        client = self.client()
        response = await client.post(
            "/v1/completions",
            json={"model": self.cfg.model, "prompt": "hello", "max_tokens": 1, "stream": True},
        )
        self.assertEqual(response.status_code, 200)
        event = json.loads(response.text.strip().splitlines()[-1].removeprefix("data: "))
        row = self.terminal_rows()[-1]
        self.assertEqual(row["status"], 200)
        self.assertEqual(
            (row["error_type"], row["error_code"]), (event["error"]["type"], event["error"]["code"])
        )
        self.assertEqual((row["error_type"], row["error_code"]), ("decode", "failed"))
        self.assertEqual(row["reason"], "engine_error")

    async def test_attempt_failures_hold_one_entry_per_attempt(self):
        # Each retry needs an engine whose leg has not failed.
        self.cfg = fleet(self.root, engines=("e0", "e1", "e2", "e3"), pinned=("e3",))
        self.cfg.tokenize, self.cfg.admission, self.cfg.failure_quarantine_s = False, "open", 0
        self.cfg.serving = ServingPolicy(max_attempts=3, **FAST_RETRY)
        self.prefill_statuses = [503, 503, 503]
        client = self.client()
        self.assertEqual((await self.post(client)).status_code, 502)
        failures = self.terminal_rows()[-1]["attempt_failures"]
        self.assertEqual(
            [(f["attempt"], f["retry_reason"]) for f in failures],
            [(1, "allowed"), (2, "allowed"), (3, "attempt_limit")],
        )
        self.assertEqual(self.router.attempt_failures["prefill", "engine_error"], 3)

    async def test_a_refusal_before_dispatch_carries_not_dispatched(self):
        self.cfg.admission = "predictive"
        self.cfg.serving = ServingPolicy(max_attempts=2, **FAST_RETRY)
        client = self.client()
        scheduler = self.router.scheduler
        with (
            patch.object(scheduler, "prefill_admission_price", return_value=20),
            patch.object(scheduler, "cheapest_own_prefill", return_value=1),
        ):
            self.assertEqual((await self.post(client)).status_code, 429)
        (failure,) = self.terminal_rows()[-1]["attempt_failures"]
        self.assertEqual(
            (failure["attempt"], failure["reason"], failure["retry_reason"]),
            (1, "queue", "not_dispatched"),
        )
        # A retry skips the TTFT price and refuses only on its decode check.
        self.prefill_statuses = [503]
        checks = iter((None, "tpot"))
        with (
            patch.object(scheduler, "prefill_admission_price", return_value=0.0) as price,
            patch.object(scheduler, "cheapest_own_prefill", return_value=1),
            patch(
                "narwhal.serving.execution.decode_refusal", side_effect=lambda *a, **k: next(checks)
            ) as decode,
        ):
            self.assertEqual((await self.post(client)).status_code, 429)
        self.assertIsNone(decode.call_args_list[1].kwargs["ttft_s"])
        self.assertEqual(price.call_count, 2)
        row = self.terminal_rows()[-1]
        self.assertEqual(
            [(f["attempt"], f["retry_reason"]) for f in row["attempt_failures"]],
            [(1, "allowed"), (2, "not_dispatched")],
        )
        self.assertEqual(row["attempt_failures"][1]["phase"], "admission")
        self.assertEqual(self.router.outcome_reasons["refused"], {"queue": 1, "tpot": 1})
        self.assert_released()

    async def test_a_request_served_after_a_retry_is_counted(self):
        self.cfg.serving = ServingPolicy(max_attempts=2, **FAST_RETRY)
        self.prefill_statuses = [503, 200]
        client = self.client()
        self.assertEqual((await self.post(client)).status_code, 200)
        row = self.terminal_rows()[-1]
        self.assertEqual((row["status"], row["error_type"], row["reason"]), (200, None, None))
        self.assertEqual(row["attempts"], 2)
        self.assertIn("narwhal_served_after_retry_total 1", await self.metrics(client))

    async def test_expiries_distinguish_the_original_deadline_from_the_queue_wait(self):
        self.cfg.request_timeout_s = 0.05
        self.blocked = asyncio.Event()
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["type"], "request_expired")
        row = self.terminal_rows()[-1]
        self.assertEqual(
            (row["terminal"], row["reason"], row["status"]), ("expired", "deadline", 504)
        )
        self.assertEqual((row["error_type"], row["error_code"]), ("request_expired", None))

        self.cfg.request_timeout_s = 5.0
        self.cfg.serving = ServingPolicy(**{**QUEUED, "queue_timeout_s": 0.01})
        client = self.client()
        self.router.inflight = self.router.max_concurrent
        response = await self.post(client)
        self.router.inflight = 0
        self.assertEqual(response.status_code, 504)
        row = self.terminal_rows()[-1]
        self.assertEqual((row["reason"], row["error_type"]), ("queue_timeout", "queue_expired"))
        self.assertGreater(row["queue_waits"]["admission"], 0.0)
        self.assertIsNone(row["queue_waits"]["prefill"])
        self.assertIn('narwhal_expired_total{reason="queue_timeout"} 1', await self.metrics(client))

    async def test_a_deadline_before_headers_records_the_body_the_client_received(self):
        """Both deadline timers fire before headers; the row repeats the 504 body."""
        for wait in ("prefill_leg", "admission_seat"):
            with self.subTest(wait=wait):
                self.cfg.request_timeout_s = 0.05
                if wait == "prefill_leg":
                    self.blocked = asyncio.Event()
                else:
                    self.blocked = None
                    self.cfg.serving = ServingPolicy(**QUEUED)
                client = self.client()
                if wait == "admission_seat":
                    self.router.inflight = self.router.max_concurrent
                response = await self.post(client)
                self.router.inflight = 0
                self.assertEqual(response.status_code, 504)
                error = response.json()["error"]
                self.assertEqual(
                    error,
                    {"message": "original request deadline expired", "type": "request_expired"},
                )
                row = self.terminal_rows()[-1]
                self.assertEqual(
                    (row["terminal"], row["reason"], row["status"]), ("expired", "deadline", 504)
                )
                self.assertEqual(
                    (row["error_type"], row["error_code"]), (error["type"], error.get("code"))
                )
                text = await self.metrics(client)
                self.assertIn('narwhal_expired_total{reason="deadline"} 1', text)
                self.assertIn('narwhal_expired_total{reason="queue_timeout"} 0', text)
                self.assert_released()

    async def test_queue_waits_split_by_stage(self):
        for policy, reached in (
            (ServingPolicy(), ("admission",)),
            (ServingPolicy(**QUEUED), ("admission", "prefill", "decode")),
        ):
            with self.subTest(queue=policy.queue_capacity):
                self.cfg.serving = policy
                client = self.client()
                self.assertEqual((await self.post(client)).status_code, 200)
                row = self.terminal_rows()[-1]
                for stage, waited in row["queue_waits"].items():
                    self.assertEqual(waited is not None, stage in reached, stage)
                self.assertAlmostEqual(
                    row["queue_wait_s"], sum(w for w in row["queue_waits"].values() if w)
                )
                text = await self.metrics(client)
                for stage in ("admission", "prefill", "decode"):
                    count = int(stage in reached)
                    self.assertRegex(
                        text, rf'narwhal_queue_wait_seconds_count{{stage="{stage}",[^}}]*}} {count}'
                    )

    async def test_the_admission_price_is_recorded_in_both_modes(self):
        for mode in ("open", "predictive"):
            with self.subTest(mode=mode):
                self.cfg.admission = mode
                client = self.client()
                self.assertEqual((await self.post(client)).status_code, 200)
                price = self.terminal_rows()[-1]["admission_price"]
                self.assertEqual(price["attempt"], 1)
                self.assertGreater(price["own_prefill_s"], 0.0)
                self.assertGreaterEqual(price["backlog_s"], 0.0)
                self.assertAlmostEqual(
                    price["price_s"],
                    price["backlog_s"] + price["own_prefill_s"] + price["elapsed_s"],
                )
        with patch.object(
            self.router.scheduler, "prefill_admission_price", return_value=float("inf")
        ):
            self.assertEqual((await self.post(client)).status_code, 429)
        price = self.terminal_rows()[-1]["admission_price"]
        self.assertEqual((price["backlog_s"], price["price_s"]), (None, None))


class FailureReasonTests(unittest.TestCase):
    def test_error_responses_take_only_registered_status_and_type_pairs(self):
        """An error body outside the registered pairs raises before it reaches a client."""
        response = error_response(504, "handoff_expired", "expired", code="handoff_expired")
        self.assertEqual(response.status_code, 504)
        self.assertEqual(
            json.loads(response.body),
            {"error": {"message": "expired", "type": "handoff_expired", "code": "handoff_expired"}},
        )
        for status, error_type in ((502, "queue"), (504, "backoff"), (500, "internal")):
            with self.subTest(status=status, error_type=error_type), self.assertRaises(ValueError):
                error_response(status, error_type, "unregistered")

    def test_a_wrapped_transport_failure_keeps_its_classification(self):
        """A tokenize leg wraps a dropped connection; the outcome names the connection failure."""
        request = httpx.Request("POST", "http://engine/tokenize")
        for cause, reason in (
            (
                httpx.RemoteProtocolError("peer closed connection", request=request),
                "engine_connection",
            ),
            (httpx.ConnectError("refused", request=request), "engine_unreachable"),
            (httpx.ReadTimeout("slow", request=request), "engine_timeout"),
            (ValueError("bad continuation"), "engine_error"),
        ):
            with self.subTest(reason=reason):
                try:
                    raise EngineError("tokenize", "http://engine", 502, str(cause)) from cause
                except EngineError as exc:
                    self.assertEqual(failure_reason(exc, deadline_passed=False), reason)

    def test_a_queue_expiry_at_the_callers_deadline_is_a_deadline_expiry(self):
        """The wait timer can fire before the deadline on the router clock."""
        for exc, deadline_passed, reason in (
            (QueueExpired(at_deadline=True), False, "deadline"),
            (QueueExpired(at_deadline=False), True, "deadline"),
            (QueueExpired(at_deadline=False), False, "queue_timeout"),
        ):
            with self.subTest(at_deadline=exc.at_deadline, deadline_passed=deadline_passed):
                self.assertEqual(failure_reason(exc, deadline_passed=deadline_passed), reason)
