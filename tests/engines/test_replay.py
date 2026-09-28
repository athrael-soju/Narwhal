"""Verify replay identity pins, complete framing and qualified request boundaries."""

import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.engines.attestation import (
    build_app,
)
from narwhal.engines.client import (
    FIRST_OUTPUT_DETAIL,
    STREAM_CONTINUATION_DETAIL,
    STREAM_SILENCE_DETAIL,
    STREAM_UNTERMINATED_DETAIL,
    EngineClient,
    EngineError,
    leg_failure_class,
)
from narwhal.engines.replay import (
    QUALIFICATION_SCHEMA,
    ReplayCapture,
    ReplayContract,
    ReplayError,
    ReplayEventReader,
    ReplayInterrupted,
    ReplayQualification,
    ReplayUnavailable,
)
from narwhal.serving.retry import transient
from narwhal.types import LEG_STREAM
from tests.engines.replay_fixtures import contract, envelope, fixture, wire


class ReplayContractTests(unittest.TestCase):
    def test_qualification_pin_precedes_parsing_and_captures_bind_contract(self):
        qualification, _, _ = fixture()
        raw = {
            "schema": QUALIFICATION_SCHEMA,
            "schema_version": 1,
            "contract": qualification.contract.fields(),
            "engines": {"e0": qualification.engines["e0"].fields()},
            "evidence_sha256": "b" * 64,
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "qualification.json"
            path.write_text(json.dumps(raw))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(ReplayQualification.load(path, digest), qualification)
            with self.assertRaisesRegex(ReplayError, "digest does not match"):
                ReplayQualification.load(path, "f" * 64)
            raw["contract"]["source_digests"]["tokenizer"] = "c" * 64
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ReplayError, "another contract"):
                ReplayQualification.load(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_qualification_rejects_unknown_profile_settings_and_invalid_bounds(self):
        original = contract().fields()
        mutations = [
            ("profile", "automatic"),
            ("vocab_size", True),
            ("max_context_tokens", 0),
            ("max_output_tokens", -1),
            ("eos_token_ids", []),
            ("allowed_stop_token_ids", [256]),
            ("allowed_stop_token_ids", [2, 2]),
            ("source_digests", {"tokenizer": "a" * 64}),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value), self.assertRaises(ReplayError):
                ReplayContract.parse({**original, field: value})
        changed = copy.deepcopy(original)
        changed["generation"]["temperature"] = 1
        with self.assertRaisesRegex(ReplayError, "generation"):
            ReplayContract.parse(changed)
        self.assertEqual(ReplayContract.parse(original).fields(), original)

    def test_request_rejects_unknown_generation_extensions_and_bounds(self):
        approved = contract()
        body = {"model": "test-model", "prompt": [3], "max_tokens": 3, "stream": True}
        self.assertEqual(approved.validate_request(body), (3,))
        for field, value in [
            ("prompt", [True]),
            ("prompt", [256]),
            ("prompt", []),
            ("prompt", "text"),
            ("max_tokens", 33),
            ("n", True),
            ("stop", ["hidden"]),
            ("stop_token_ids", [5]),
            ("temperature", 1),
            ("min_tokens", 1),
            ("logit_bias", {"3": 100}),
            ("model", "other"),
        ]:
            with self.subTest(field=field), self.assertRaises(ReplayError):
                approved.validate_request({**body, field: value})
        with self.assertRaisesRegex(ReplayError, "context"):
            approved.validate_request({**body, "prompt": [3] * 63})
        self.assertEqual(approved.validate_request({**body, "stop_token_ids": [2]}), (3,))

    def test_capture_rejects_unbound_or_ambiguous_process_identity(self):
        qualification, _, _ = fixture()
        raw = qualification.engines["e0"].fields()
        self.assertEqual(ReplayCapture.parse(raw), qualification.engines["e0"])
        for value in [True, 0, -1, float("inf"), 10**400, "100"]:
            changed = copy.deepcopy(raw)
            changed["engine"]["process_start_time_seconds"] = value
            with self.subTest(value=value), self.assertRaises(ReplayError):
                ReplayCapture.parse(changed)
        with self.assertRaises(ReplayError):
            ReplayCapture.parse({**raw, "qualified": True})


class ReplayReaderTests(unittest.TestCase):
    def reader(self, maximum=3, bound=2048):
        return ReplayEventReader(contract(), [3], maximum, bound)

    def test_no_event_before_delimiter_including_done(self):
        reader = self.reader()
        data = wire(envelope(finish="stop"))
        self.assertEqual(list(reader.feed(data[:-1])), [])
        events = list(reader.feed(data[-1:]))
        self.assertEqual(events[0].generated_ids, (7,))
        self.assertEqual(list(reader.feed(b"data: [DONE]\n")), [])
        with self.assertRaisesRegex(ReplayInterrupted, "complete terminator"):
            reader.finish()
        self.assertEqual(next(reader.feed(b"\n")).kind, "done")
        reader.finish()

    def test_split_utf8_crlf_comments_and_multiline_data(self):
        reader = self.reader()
        data = (
            b": heartbeat\r\n\r\n"
            + wire(envelope(text="日本語🦄", finish="stop"))
            .replace(b'"choices":', b"\n" + b'data: "choices":')
            .replace(b"\n", b"\r\n")
            + b"data: [DONE]\r\n\r\n"
        )
        events = []
        for byte in data:
            events.extend(reader.feed(bytes([byte])))
        self.assertEqual([e.kind for e in events], ["completion", "done"])
        self.assertEqual(events[0].text, "日本語🦄")
        reader.finish()

    def test_malformed_fields_echo_and_unknown_semantics_fail_without_content(self):
        cases = []
        for name, value in [
            ("index", True),
            ("index", 1),
            ("text", None),
            ("token_ids", [True]),
            ("token_ids", [256]),
            ("token_ids", []),
            ("logprobs", {}),
            ("delta", {"reasoning": "sensitive text"}),
            ("finish_reason", "tool_calls"),
            ("stop_reason", "sensitive text"),
            ("prompt_token_ids", [4]),
        ]:
            obj = envelope(text="sensitive text")
            obj["choices"][0][name] = value
            cases.append(obj)
        obj = envelope()
        del obj["choices"][0]["prompt_token_ids"]
        cases.append(obj)
        cases.append({**envelope(), "extension": "sensitive text"})
        cases.append({**envelope(), "choices": [envelope()["choices"][0]] * 2})
        for obj in cases:
            with self.subTest(obj=obj), self.assertRaises(ReplayError) as caught:
                list(self.reader().feed(wire(obj)))
            self.assertNotIn("sensitive text", str(caught.exception))

    def test_empty_and_grouped_events_remain_observable_without_safe_inference(self):
        reader = self.reader()
        events = list(reader.feed(wire(envelope(text="", ids=(7,)))))
        events += list(
            reader.feed(wire(envelope(ids=(8, 9), text="a", prompt=None, finish="length")))
        )
        events += list(reader.feed(b"data: [DONE]\n\n"))
        self.assertEqual(events[0].generated_ids, (7,))
        self.assertEqual(events[0].text, "")
        self.assertEqual(events[1].generated_ids, (8, 9))
        self.assertNotIn("generated_ids", repr(events[1]))

    def test_order_output_cap_and_usage_are_validated(self):
        for extra in [wire(envelope(prompt=None)), b"data: [DONE]\n\n"]:
            reader = self.reader()
            if extra.startswith(b"data: {"):
                list(reader.feed(wire(envelope(finish="stop"))))
            with self.subTest(extra=extra), self.assertRaises(ReplayError):
                list(reader.feed(extra))
        with self.assertRaisesRegex(ReplayError, "token limit"):
            list(self.reader(maximum=1).feed(wire(envelope(ids=(7, 8)))))
        reader = self.reader()
        list(reader.feed(wire(envelope(finish="stop"))))
        usage = {
            **envelope(),
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        self.assertEqual(next(reader.feed(wire(usage))).kind, "usage")
        with self.assertRaises(ReplayError):
            list(reader.feed(wire(usage)))
        usage["usage"]["total_tokens"] = 1
        reader = self.reader()
        list(reader.feed(wire(envelope(finish="stop"))))
        with self.assertRaises(ReplayError):
            list(reader.feed(wire(usage)))

    def test_pending_events_are_bounded_before_json_decode(self):
        reader = self.reader(bound=12)
        list(reader.feed(b"data: "))
        with self.assertRaisesRegex(ReplayError, "byte limit"):
            list(reader.feed(b"x" * 20))
        self.assertLessEqual(len(reader._line), 12)
        for data in [
            b'data: {"x":1,"x":2}\n\n',
            b'data: {"x":NaN}\n\n',
            b"data: \xff\n\n",
            b"event: changed\n\n",
        ]:
            with self.subTest(data=data), self.assertRaises(ReplayError):
                list(self.reader().feed(data))

    def test_terminal_usage_and_stop_fields_follow_the_qualified_budget(self):
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        with self.assertRaisesRegex(ReplayError, "before terminal"):
            list(self.reader().feed(wire({**envelope(), "usage": usage})))
        reader = self.reader()
        valid = list(reader.feed(wire({**envelope(finish="stop"), "usage": usage})))
        with self.assertRaisesRegex(ReplayError, "outside its terminal boundary"):
            list(reader.feed(wire({**envelope(), "choices": [], "usage": usage})))
        self.assertEqual(valid[0].kind, "completion")
        for obj in [
            envelope(finish="length"),
            envelope(finish="stop", stop_reason=7),
            envelope(ids=(7,), finish="stop", stop_reason=1),
        ]:
            with self.subTest(obj=obj), self.assertRaises(ReplayError):
                list(self.reader().feed(wire(obj)))
        stop = envelope(ids=(2,), text="", finish="stop", stop_reason=2)
        with self.assertRaises(ReplayError):
            list(self.reader().feed(wire(stop)))
        reader = ReplayEventReader(contract(), [3], 3, 2048, stop_token_ids=[2])
        self.assertEqual(next(reader.feed(wire(stop))).finish_reason, "stop")


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for delay, data in self.chunks:
            await asyncio.sleep(delay)
            yield data

    async def aclose(self):
        self.closed = True


class ReplayHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.qualification, self.document, self.standard = fixture()
        self.capture = self.qualification.engines["e0"]
        self.start = 100
        self.encoding = None
        self.requests = []
        self.stream = ByteStream([(0, wire(envelope(finish="stop")) + b"data: [DONE]\n\n")])
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "test-version"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=f"process_start_time_seconds {self.start}\n")
        if request.url.path == "/v1/attestation":
            return httpx.Response(200, json=self.standard)
        if request.url.path == "/v1/attestation/continuation":
            return httpx.Response(200, json=self.capture.fields())
        headers = {"content-encoding": self.encoding} if self.encoding else {}
        return httpx.Response(200, stream=self.stream, headers=headers)

    async def test_live_verification_and_authentication_scopes(self):
        async with httpx.AsyncClient(transport=self.transport) as client:
            actual = await self.qualification.verify(
                "e0",
                "http://engine",
                "http://sidecar/v1/attestation",
                client=client,
                headers={"Authorization": "Bearer test"},
            )
            self.assertEqual(actual, self.capture)
            for request in self.requests:
                self.assertEqual("authorization" in request.headers, request.url.host == "engine")
            self.start = 101
            with self.assertRaises(ReplayUnavailable):
                await self.qualification.verify(
                    "e0", "http://engine", "http://sidecar/v1/attestation", client=client
                )
            with self.assertRaises(ReplayUnavailable):
                await self.qualification.verify(
                    "unknown", "http://engine", "http://sidecar/v1/attestation", client=client
                )

    async def test_sidecar_rejects_restart_at_startup_and_during_service(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            build_app(
                self.document,
                "http://engine",
                replace(self.capture.identity, process_start_time_seconds=101),
                continuation=self.capture,
            )
        app = build_app(
            self.document,
            "http://engine",
            self.capture.identity,
            continuation=self.capture,
            transport=self.transport,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sidecar"
        ) as client:
            response = await client.get("/v1/attestation/continuation")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), self.capture.fields())
            self.start = 101
            self.assertEqual((await client.get("/v1/attestation/continuation")).status_code, 503)
        ordinary = build_app(
            self.document, "http://engine", self.capture.identity, transport=self.transport
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=ordinary), base_url="http://sidecar"
        ) as client:
            self.assertEqual((await client.get("/v1/attestation/continuation")).status_code, 404)

    async def test_prefill_error_redaction_preserves_status_and_transport_classes(self):
        sentinel = "private prompt sentinel"
        for status in [400, 429, 503]:
            client = EngineClient(
                transport=httpx.MockTransport(
                    lambda request, code=status: httpx.Response(code, text=sentinel)
                )
            )
            try:
                with self.assertRaises(EngineError) as ordinary:
                    await client.prefill("http://engine", "/v1/completions", {}, {})
                with self.assertRaises(EngineError) as redacted:
                    await client.prefill(
                        "http://engine", "/v1/completions", {}, {}, redact_errors=True
                    )
                self.assertIn(sentinel, str(ordinary.exception))
                self.assertNotIn(sentinel, str(redacted.exception))
                self.assertEqual(redacted.exception.status, status)
                self.assertEqual(transient(ordinary.exception), transient(redacted.exception))
                self.assertEqual(
                    leg_failure_class(ordinary.exception), leg_failure_class(redacted.exception)
                )
            finally:
                await client.aclose()
        for error_type in [
            httpx.PoolTimeout,
            httpx.ConnectTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ]:

            def fail(request, cls=error_type):
                raise cls(sentinel)

            client = EngineClient(transport=httpx.MockTransport(fail))
            try:
                with self.assertRaises(error_type) as caught:
                    await client.prefill(
                        "http://engine", "/v1/completions", {}, {}, redact_errors=True
                    )
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertEqual(transient(error_type(sentinel)), transient(caught.exception))
                self.assertEqual(
                    leg_failure_class(error_type(sentinel)), leg_failure_class(caught.exception)
                )
            finally:
                await client.aclose()

    async def test_descriptor_and_decode_transport_messages_are_content_free(self):
        client = EngineClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        )
        try:
            with (
                patch.object(
                    client.kv, "prefill_result", side_effect=ValueError("private sentinel")
                ),
                self.assertRaises(EngineError) as caught,
            ):
                await client.prefill("http://engine", "/v1/completions", {}, {}, redact_errors=True)
            self.assertNotIn("private sentinel", str(caught.exception))
            self.assertTrue(caught.exception.detail.startswith("no handoff"))
        finally:
            await client.aclose()

        class FailedStream(ByteStream):
            async def __aiter__(self):
                yield wire(envelope())
                raise httpx.RemoteProtocolError("private sentinel")

        self.stream = FailedStream([])
        with self.assertRaises(httpx.RemoteProtocolError) as caught:
            await self.consume()
        self.assertNotIn("private sentinel", str(caught.exception))
        self.assertTrue(transient(caught.exception))

    async def consume(self, budget=0.5, bound=2048):
        client = EngineClient(transport=self.transport, read_timeout_s=0.02)
        try:
            return [
                event
                async for event in client.decode_continuation(
                    "http://engine",
                    "/v1/completions",
                    {"model": "test-model", "prompt": [3], "max_tokens": 3, "stream": True},
                    {},
                    None,
                    first_token_timeout_s=budget,
                    qualification=self.qualification,
                    iid="e0",
                    attestation_url="http://sidecar/v1/attestation",
                    max_event_bytes=bound,
                )
            ]
        finally:
            await client.aclose()

    async def test_client_revalidates_after_open_and_closes_rejected_stream(self):
        self.start = 101
        with self.assertRaises(ReplayUnavailable):
            await self.consume()
        self.assertEqual(self.requests[0].method, "POST")
        self.assertTrue(self.stream.closed)

    async def test_verification_timeout_does_not_blame_inference(self):
        async def delayed(request):
            if request.url.path == "/version":
                await asyncio.sleep(0.08)
            return self.handle(request)

        self.transport = httpx.MockTransport(delayed)
        with self.assertRaises(ReplayUnavailable) as caught:
            await self.consume(budget=0.025)
        self.assertIsNone(leg_failure_class(caught.exception))
        self.assertTrue(self.stream.closed)

    async def test_encoded_and_oversized_transport_chunks_are_rejected(self):
        self.encoding = "gzip"
        with self.assertRaisesRegex(EngineError, STREAM_CONTINUATION_DETAIL):
            await self.consume()
        self.assertEqual(self.requests[0].headers["accept-encoding"], "identity")
        self.assertTrue(self.stream.closed)
        self.encoding = None
        self.stream = ByteStream([(0, b":" + b"x" * (64 * 1024) + b"\n\n")])
        with self.assertRaisesRegex(EngineError, STREAM_CONTINUATION_DETAIL):
            await self.consume(bound=1024 * 1024)
        self.assertTrue(self.stream.closed)

    async def test_client_preserves_partial_frame_and_silence_deadlines(self):
        self.stream = ByteStream([(0, wire(envelope())[:-1]), (0.08, b"\n")])
        with self.assertRaisesRegex(EngineError, FIRST_OUTPUT_DETAIL):
            await self.consume(budget=0.025)
        self.stream = ByteStream([(0, wire(envelope())), (0.08, b"data: [DONE]\n\n")])
        with self.assertRaisesRegex(EngineError, STREAM_SILENCE_DETAIL):
            await self.consume()

    async def test_client_requires_complete_done_and_classifies_protocol_failure(self):
        self.assertEqual([e.kind for e in await self.consume()], ["completion", "done"])
        for suffix in (b"", b"data: [DONE]", b"data: [DONE]\n", b'data: {"choices":'):
            with self.subTest(suffix=suffix):
                self.stream = ByteStream([(0, wire(envelope(finish="stop")) + suffix)])
                with self.assertRaises(EngineError) as caught:
                    await self.consume()
                self.assertEqual(caught.exception.detail, STREAM_UNTERMINATED_DETAIL)
                self.assertEqual(leg_failure_class(caught.exception), LEG_STREAM)
                self.assertTrue(self.stream.closed)
        for invalid in (
            wire(envelope(ids=(True,))),
            b"event: unsupported\n\n",
            b"data: invalid-json\n\n",
        ):
            with self.subTest(invalid=invalid):
                self.stream = ByteStream([(0, invalid)])
                with self.assertRaisesRegex(EngineError, STREAM_CONTINUATION_DETAIL) as caught:
                    await self.consume()
                self.assertEqual(leg_failure_class(caught.exception), LEG_STREAM)
                self.assertTrue(self.stream.closed)
