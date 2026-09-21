"""Check exact-output canary results, request digests and event windows."""

import asyncio
import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.contracts import CANARY_CASES, STATE, versioned
from narwhal.diagnostics import canary
from narwhal.diagnostics.canary import (
    CanaryCase,
    CanaryOutcome,
    ControlEvent,
    StreamEvidence,
    classify,
    load_cases,
    parse_sse,
    request_case,
    state_transitions,
    summarize,
)
from tools.tests.fixtures import ROOT


class CanaryTests(unittest.IsolatedAsyncioTestCase):
    """One exact completion distinguishes truncation, corruption and terminal errors."""

    def setUp(self):
        self.case = CanaryCase("case", "private prompt", "ab", (1, 2), (1, 2, 3))

    def test_classification_distinguishes_each_failure(self):
        """Text, IDs, terminator and error flags jointly determine the result."""
        good = StreamEvidence("ab", (1, 2), True, False, False)
        for evidence, expected in (
            (good, "correct"),
            (replace(good, done=False), "truncated"),
            (replace(good, text="a", token_ids=(1,)), "truncated"),
            (replace(good, text="xx"), "wrong"),
            (replace(good, token_ids=(1, 3)), "wrong"),
            (replace(good, malformed=True), "malformed"),
            (replace(good, terminal_error=True, malformed=True), "terminal_error"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(classify(self.case, evidence), expected)

    def test_sse_parser_retains_metadata_and_payload_failures(self):
        """Completion and chat frames produce the same text and token counts."""
        lines = [
            ": keepalive",
            'data: {"choices":[]}',
            'data: {"choices":[{"text":"a","token_ids":[1]}]}',
            'data: {"choices":[{"delta":{"content":"b"},"token_ids":[2]}]}',
            "data: [DONE]",
        ]
        self.assertEqual(classify(self.case, parse_sse(lines)), "correct")
        for line in (
            "data: {bad",
            "data: null",
            'data: {"choices":{}}',
            'data: {"choices":[1]}',
            'data: {"choices":[{"text":"x"}]}',
        ):
            with self.subTest(line=line):
                self.assertTrue(parse_sse([line]).malformed)
        self.assertTrue(parse_sse(['data: {"error":"failed"}']).terminal_error)

    def test_invalid_token_identity_marks_canary_evidence_malformed(self):
        """Canary evidence rejects the same invalid identities as measurement clients."""
        from tools.tests.fixtures import invalid_token_choices

        for choice in invalid_token_choices():
            with self.subTest(choice=choice):
                evidence = parse_sse(["data: " + json.dumps({"choices": [choice]}), "data: [DONE]"])
                self.assertTrue(evidence.malformed)
                self.assertEqual(classify(self.case, evidence), "malformed")

    def test_case_documents_require_allowed_distractors_and_unique_ids(self):
        """Case construction checks the expected tokens against a wider allowed set."""
        row = {
            "id": "c",
            "prompt": "p",
            "expected": "a",
            "expected_token_ids": [0],
            "allowed_token_ids": [0, 1],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            path.write_text(json.dumps(versioned(CANARY_CASES, {"model": "stub", "cases": [row]})))
            model, cases = load_cases(path)
            self.assertEqual((model, cases[0].expected_token_ids), ("stub", (0,)))
            for changes in (
                {"allowed_token_ids": [0]},
                {"allowed_token_ids": [1, 2]},
                {"allowed_token_ids": [0, 1, 1]},
                {"expected_token_ids": [True]},
                {"prompt": ""},
                {"surprise": 1},
            ):
                path.write_text(
                    json.dumps(versioned(CANARY_CASES, {"cases": [{**row, **changes}]}))
                )
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    load_cases(path)
            path.write_text(json.dumps(versioned(CANARY_CASES, {"cases": [row, row]})))
            with self.assertRaisesRegex(ValueError, "ids must be unique"):
                load_cases(path)

    def test_shipped_canary_example_is_constructible(self):
        """The tracked case template passes the same loader used by the CLI."""
        model, cases = load_cases(ROOT / "config/canary-cases.example.json")

        self.assertEqual(model, "replace-with-served-model")
        self.assertEqual(len(cases), 1)
        self.assertGreater(
            len(set(cases[0].allowed_token_ids) - set(cases[0].expected_token_ids)), 0
        )

    async def test_request_retains_digest_and_counters_without_content(self):
        """Completed canary results include keyed digests and token counts."""

        def handle(request):
            body = json.loads(request.content)
            self.assertEqual(body["allowed_token_ids"], [1, 2, 3])
            return httpx.Response(
                200, text='data: {"choices":[{"text":"ab","token_ids":[1,2]}]}\n\ndata: [DONE]\n\n'
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await request_case(
                client,
                base="http://e",
                model="stub",
                case=self.case,
                seq=1,
                scheduled_at=0,
                timeout_s=1,
                digest_key=b"test-key",
            )
        self.assertTrue(result.correct)
        self.assertEqual(result.observed_tokens, 2)
        self.assertTrue(result.observed_digest)
        self.assertNotIn("private prompt", json.dumps(asdict(result)))
        self.assertNotIn('"ab"', json.dumps(asdict(result)))

    async def test_http_and_transport_failures_remain_distinct(self):
        """Client-visible refusal and transport failure keep separate canary statuses."""
        for response, expected in (
            (httpx.Response(429), "http_error"),
            (httpx.ReadError("lost"), "transport_error"),
        ):

            def handle(request, response=response):
                if isinstance(response, Exception):
                    raise response
                return response

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                result = await request_case(
                    client,
                    base="http://e",
                    model="stub",
                    case=self.case,
                    seq=1,
                    scheduled_at=0,
                    timeout_s=1,
                    digest_key=None,
                )
            self.assertEqual(result.status, expected)
            self.assertFalse(result.correct)

    def test_control_transitions_and_event_windows_preserve_failed_denominator(self):
        """Event windows include failed requests at both inclusive boundaries."""
        events = state_transitions(
            {"pools": {"prefill": ["e"], "decode": []}, "ejected": ["old"]},
            {"pools": {"prefill": [], "decode": ["e"]}, "quarantined": ["e"]},
            10,
        )
        self.assertEqual(
            {event.event for event in events},
            {"role_change", "engine_readmitted", "failure_quarantine"},
        )
        base = CanaryOutcome(0, "c", 9, 9, 10, 0, 0.1, 1, 2, 2, True, "correct")
        outcomes = [base, replace(base, seq=1, started_at=11, correct=False, status="timeout")]
        result = summarize(outcomes, [ControlEvent("role_change", 10, "router_state")], 1)
        self.assertEqual(
            (result["requests"], result["correct"], result["terminal_errors"]), (2, 1, 1)
        )
        self.assertEqual(result["event_windows"][0]["failures"], 1)
        self.assertIsNone(summarize([], [], 1)["ttft_p50_s"])

    async def test_driver_cycles_cases_and_joins_the_state_watcher(self):
        """The driver dispatches the ceiling of offered requests and joins its watcher."""

        async def watcher(client, base, interval, stop, events):
            await stop.wait()
            events.append(ControlEvent("observed", 0, "fixture"))

        def handler(request):
            return httpx.Response(
                200, text='data: {"choices":[{"text":"ab","token_ids":[1,2]}]}\n\ndata: [DONE]\n\n'
            )

        for digest, interval in ((False, 0), (True, 0.001)):
            with (
                self.subTest(digest=digest),
                patch.object(canary, "watch_state", side_effect=watcher) as watch,
            ):
                outcomes, events, started, completed = await canary.drive(
                    base="http://router",
                    model="stub",
                    cases=[self.case, replace(self.case, cid="second")],
                    rate=1000,
                    duration_s=0.0021,
                    timeout_s=1,
                    state_poll_s=interval,
                    digest=digest,
                    transport=httpx.MockTransport(handler),
                )
            self.assertEqual([row.canary_id for row in outcomes], ["case", "second", "case"])
            self.assertEqual([row.seq for row in outcomes], [0, 1, 2])
            self.assertTrue(all(row.correct for row in outcomes))
            self.assertTrue(all(bool(row.observed_digest) == digest for row in outcomes))
            self.assertGreaterEqual(completed, started)
            self.assertEqual(watch.call_count, int(interval > 0))
            self.assertEqual(len(events), int(interval > 0))

    async def test_state_watcher_retains_the_last_valid_snapshot_after_bad_responses(self):
        """HTTP and schema failures leave the last valid state available for comparison."""
        stop = asyncio.Event()
        replies = iter(
            [
                httpx.Response(200, json=versioned(STATE, {"pools": {"prefill": ["e"]}})),
                httpx.Response(503),
                httpx.Response(200, json=[]),
                httpx.Response(200, json={}),
                httpx.Response(200, json=versioned(STATE, {"pools": {"decode": ["e"]}})),
            ]
        )
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 5:
                stop.set()
            return next(replies)

        events = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await asyncio.wait_for(
                canary.watch_state(client, "http://router", 0.001, stop, events), 1
            )
        self.assertEqual(
            [(event.event, event.iid, event.to) for event in events],
            [("role_change", "e", "decode")],
        )

    async def test_request_timeout_retains_partial_token_evidence(self):
        """A timed-out stream keeps the tokens and digest observed before the deadline."""

        class PartialStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"text":"a","token_ids":[1]}]}\n\n'
                await asyncio.Event().wait()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=PartialStream())
            )
        ) as client:
            result = await request_case(
                client,
                base="http://engine",
                model="stub",
                case=self.case,
                seq=0,
                scheduled_at=0,
                timeout_s=0.01,
                digest_key=b"test-key",
            )
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.observed_tokens, 1)
        self.assertIsNotNone(result.ttft_s)
        self.assertTrue(result.observed_digest)

    def test_operator_markers_use_inclusive_bounds_and_ignore_malformed_rows(self):
        """Only valid markers inside the run's timestamps contribute event windows."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "markers.jsonl"
            self.assertEqual(canary.load_markers(path, 10, 20), [])
            rows = [
                {"event": "start", "at": 10},
                {"event": "end", "at": 20, "iid": 1},
                {"event": "before", "at": 9},
                {"event": "after", "at": 21},
                {"event": "", "at": 15},
                {"event": 1, "at": 15},
                {},
                {"at": "bad"},
            ]
            path.write_text("{\n" + "\n".join(json.dumps(row) for row in rows))
            events = canary.load_markers(path, 10, 20)
            self.assertEqual(
                [(row.event, row.iid) for row in events], [("start", None), ("end", "1")]
            )

    def test_case_file_shapes_and_ids_fail_with_named_diagnostics(self):
        """Case loading rejects malformed envelopes and empty identity fields."""
        row = {
            "id": "c",
            "prompt": "p",
            "expected": "a",
            "expected_token_ids": [1],
            "allowed_token_ids": [1, 2],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            for doc in (
                [],
                versioned(CANARY_CASES, {"unknown": True}),
                versioned(CANARY_CASES, {"model": 1}),
                versioned(CANARY_CASES, {"cases": []}),
                versioned(CANARY_CASES, {"cases": [None]}),
                *(
                    versioned(CANARY_CASES, {"cases": [{**row, field: value}]})
                    for field, value in (("id", ""), ("expected", ""), ("allowed_token_ids", []))
                ),
            ):
                path.write_text(json.dumps(doc))
                with self.subTest(doc=doc), self.assertRaises(ValueError):
                    load_cases(path)


class CanaryCliTests(unittest.TestCase):
    """The CLI writes content-free artifacts from explicit driver results."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cases = self.root / "cases.json"
        self.out = self.root / "canary.jsonl"
        self.doc = versioned(
            CANARY_CASES,
            {
                "model": "stub",
                "cases": [
                    {
                        "id": "c",
                        "prompt": "private fixture prompt",
                        "expected": "a",
                        "expected_token_ids": [1],
                        "allowed_token_ids": [1, 2],
                    }
                ],
            },
        )
        self.cases.write_text(json.dumps(self.doc))
        self.args = ["--cases", str(self.cases), "--duration", "1", "--out", str(self.out)]
        self.stdout = io.StringIO()
        self.enterContext(patch("sys.stdout", self.stdout))
        self.enterContext(patch("sys.stderr", io.StringIO()))
        self.row = CanaryOutcome(0, "c", 10, 10, 11, 0, 0.1, 1, 1, 1, True, "correct")
        self.driver = self.enterContext(
            patch.object(canary, "drive", new=AsyncMock(return_value=([self.row], [], 10, 11)))
        )

    def test_cli_artifact_retains_case_hash_and_operator_events(self):
        """The artifact records case IDs, digests and operator events and omits prompt text."""
        markers = self.root / "markers.jsonl"
        markers.write_text(json.dumps({"at": 10, "event": "restart"}))
        self.assertEqual(canary.main([*self.args, "--markers", str(markers), "--digest"]), 0)
        rows = [json.loads(line) for line in self.out.read_text().splitlines()]
        self.assertEqual(rows[1]["meta"]["cases"]["ids"], ["c"])
        self.assertEqual(rows[-1]["correct"], 1)
        self.assertEqual(rows[-1]["event_windows"][0]["event"], "restart")
        self.assertNotIn("private fixture prompt", self.out.read_text())
        self.assertTrue(self.driver.call_args.kwargs["digest"])

    def test_cli_failure_status_and_missing_latency_are_reported(self):
        """A timed-out canary exits with status 1 and reports missing TTFT as unseen."""
        self.driver.return_value = (
            [replace(self.row, correct=False, status="timeout", ttft_s=None)],
            [],
            10,
            11,
        )
        self.assertEqual(canary.main(self.args), 1)
        self.assertIn("TTFT p95 unseen", self.stdout.getvalue())
        self.assertEqual(json.loads(self.out.read_text().splitlines()[-1])["terminal_errors"], 1)

    def test_cli_setup_errors_precede_driver_dispatch(self):
        """Invalid bounds and case documents fail before load is dispatched."""
        for option in ("--rate", "--duration", "--timeout", "--event-window"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                canary.main([*self.args, option, "0"])
        with self.assertRaises(SystemExit):
            canary.main([*self.args, "--state-poll", "-1"])
        self.cases.write_text("{")
        self.assertEqual(canary.main(self.args), 2)
        del self.doc["model"]
        self.cases.write_text(json.dumps(self.doc))
        with self.assertRaises(SystemExit):
            canary.main(self.args)
        self.driver.assert_not_awaited()
