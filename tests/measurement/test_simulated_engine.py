import asyncio
import io
import json
import math
import os
import unittest
from contextlib import redirect_stderr

import h11

from narwhal.backends.vllm import NixlConnector
from narwhal.backends.vllm.identity import parse_process_start
from narwhal.engines.stream import event_choices, event_object, token_ids
from tools.measurement import simulated_engine as sim

TOKEN_KEYS = ["id", "object", "created", "model", "choices"]
CHOICE_KEYS = [
    "index",
    "text",
    "logprobs",
    "finish_reason",
    "stop_reason",
    "prompt_token_ids",
    "token_ids",
]
HANDOFF = {"remote_engine_id": "sim-e1", "remote_block_ids": [0, 1]}


class FakeTransport:
    def __init__(self, closing=False):
        self.writes = []
        self.closing = closing

    def write(self, data):
        self.writes.append(bytes(data))

    def is_closing(self):
        return self.closing

    def close(self):
        self.closing = True


def engine(**options):
    return sim.SimulatedEngine("e0", prefill_s=0, **options)


def stream(**options):
    fields = {
        "request_id": "r1",
        "model": "m",
        "created": 1700000000,
        "prompt_ids": [5, 6],
        "max_tokens": 3,
        "return_token_ids": True,
        "include_usage": True,
    }
    return sim.DecodeStream(**(fields | options))


def chunks(data):
    payloads = []
    while data:
        size, _, data = data.partition(b"\r\n")
        length = int(size, 16)
        assert data[length : length + 2] == b"\r\n"
        payloads.append(data[:length])
        data = data[length + 2 :]
    return payloads


def compact(frame):
    obj = json.loads(frame.removeprefix(b"data: ").removesuffix(b"\n\n"))
    return obj, b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


def one_frame(payload):
    return payload.startswith(b"data: ") and payload.count(b"\n\n") == 1 and payload[-2:] == b"\n\n"


async def until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def events(client):
    collected = []
    while True:
        event = client.next_event()
        if event is h11.NEED_DATA:
            return collected
        collected.append(event)
        if isinstance(event, h11.EndOfMessage):
            return collected


def request_bytes(client, method, target, body=b"", headers=()):
    fields = [("host", "sim"), *headers]
    if body:
        fields.append(("content-length", str(len(body))))
    data = client.send(h11.Request(method=method, target=target, headers=fields))
    if body:
        data += client.send(h11.Data(data=body))
    return data + client.send(h11.EndOfMessage())


async def call(target, method, path, body=None, headers=None):
    raw = b"" if body is None else json.dumps(body).encode()
    return await target.handle(method, path, headers or {}, raw)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_version_and_metrics(self):
        target = engine()
        health = await call(target, "GET", "/health")
        self.assertEqual((health.status, health.body, health.stream), (200, b"", None))
        version = await call(target, "GET", "/version")
        self.assertEqual(json.loads(version.body), {"version": "simulated"})
        metrics = await call(target, "GET", "/metrics")
        self.assertEqual(metrics.content_type, "text/plain; version=0.0.4; charset=utf-8")
        text = metrics.body.decode()
        self.assertEqual(parse_process_start(text), target.process_start_time_seconds)
        self.assertEqual(
            text,
            "# HELP process_start_time_seconds Start time of the process since unix epoch in "
            "seconds.\n# TYPE process_start_time_seconds gauge\n"
            f"process_start_time_seconds {target.process_start_time_seconds!r}\n"
            "# HELP simulated_engine_late_ticks_total Pacing ticks that started one tick period "
            "or more after their scheduled time.\n"
            "# TYPE simulated_engine_late_ticks_total counter\n"
            "simulated_engine_late_ticks_total 0\n"
            "# HELP simulated_engine_active_streams Prefill and decode requests in progress.\n"
            "# TYPE simulated_engine_active_streams gauge\n"
            'simulated_engine_active_streams{phase="prefill"} 0\n'
            'simulated_engine_active_streams{phase="decode"} 0\n'
            "# HELP simulated_engine_peak_streams Most prefill and decode requests in progress "
            "at once.\n"
            "# TYPE simulated_engine_peak_streams gauge\n"
            'simulated_engine_peak_streams{phase="prefill"} 0\n'
            'simulated_engine_peak_streams{phase="decode"} 0\n',
        )

    async def test_tokenize_counts_characters_ids_and_message_contents(self):
        target = engine()
        cases = [
            ({"model": "m", "prompt": "abc"}, [97, 98, 99]),
            ({"model": "m", "prompt": [5, 6, 7, 8]}, [5, 6, 7, 8]),
            (
                {
                    "model": "m",
                    "messages": [
                        {"role": "system", "content": "hi"},
                        {"role": "user", "content": "yo"},
                    ],
                },
                [ord(c) for c in "hiyo"],
            ),
        ]
        for body, tokens in cases:
            with self.subTest(body=body):
                reply = await call(target, "POST", "/tokenize", body)
                self.assertEqual(
                    json.loads(reply.body),
                    {
                        "count": len(tokens),
                        "max_model_len": 131072,
                        "tokens": tokens,
                        "token_strs": None,
                    },
                )

    async def test_prefill_returns_a_nixl_handoff_on_remote_decode(self):
        target = engine()
        reply = await call(
            target,
            "POST",
            "/v1/completions",
            {
                "model": "m",
                "prompt": "x" * 33,
                "max_tokens": 1,
                "stream": False,
                "kv_transfer_params": {"do_remote_decode": True},
            },
            {"x-request-id": "r1"},
        )
        payload = json.loads(reply.body)
        params = NixlConnector().extract(payload)
        self.assertEqual(params["remote_block_ids"], list(range(math.ceil(33 / 16))))
        self.assertEqual(params["remote_engine_id"], "sim-e0")
        self.assertEqual(params["remote_request_id"], "r1")
        self.assertEqual(payload["id"], "cmpl-r1")
        self.assertEqual(payload["model"], "m")
        self.assertEqual(payload["choices"][0]["finish_reason"], "length")
        self.assertEqual(
            payload["usage"], {"prompt_tokens": 33, "total_tokens": 34, "completion_tokens": 1}
        )

    async def test_prefill_without_handoff_request_has_no_handoff(self):
        reply = await call(
            engine(), "POST", "/v1/completions", {"model": "m", "prompt": "ab", "max_tokens": 1}
        )
        payload = json.loads(reply.body)
        self.assertNotIn("kv_transfer_params", payload)
        self.assertRegex(payload["id"], r"^cmpl-[0-9a-f]{32}$")
        self.assertEqual(payload["usage"]["prompt_tokens"], 2)

    async def test_decode_returns_a_stream_and_rejects_invalid_input(self):
        target = engine()
        body = {
            "model": "m",
            "prompt": "abcd",
            "max_tokens": 5,
            "stream": True,
            "kv_transfer_params": HANDOFF,
        }
        reply = await call(target, "POST", "/v1/completions", body)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.content_type, "text/event-stream; charset=utf-8")
        self.assertEqual(reply.stream.remaining, 5)
        rejected = [
            ({"remote_engine_id": "sim-e1"}, "invalid kv_transfer_params"),
            ({"remote_engine_id": "", "remote_block_ids": [0]}, "invalid kv_transfer_params"),
            ({"remote_engine_id": "sim-e1", "remote_block_ids": 3}, "invalid kv_transfer_params"),
        ]
        for params, message in rejected:
            with self.subTest(params=params):
                reply = await call(
                    target, "POST", "/v1/completions", body | {"kv_transfer_params": params}
                )
                self.assertEqual(reply.status, 400)
                self.assertEqual(
                    json.loads(reply.body),
                    {"error": {"message": message, "type": "BadRequestError", "code": 400}},
                )
        reply = await call(target, "POST", "/v1/completions", body | {"max_tokens": 0})
        self.assertEqual(reply.status, 400)
        reply = await target.handle("POST", "/v1/completions", {}, b"{")
        self.assertEqual(reply.status, 400)

    async def test_unknown_routes_return_not_found(self):
        target = engine()
        for method, path in (("GET", "/v1/models"), ("GET", "/v1/completions")):
            with self.subTest(method=method, path=path):
                reply = await call(target, method, path)
                self.assertEqual(reply.status, 404)
                self.assertEqual(
                    json.loads(reply.body),
                    {"error": {"message": "not found", "type": "NotFoundError", "code": 404}},
                )

    def test_ready_line_reports_identity(self):
        target = engine()
        self.assertEqual(
            json.loads(target.ready_line()),
            {
                "iid": "e0",
                "url": "",
                "pid": os.getpid(),
                "version": "simulated",
                "process_start_time_seconds": target.process_start_time_seconds,
            },
        )


class FrameTests(unittest.TestCase):
    def test_token_frames_follow_the_vllm_completion_stream_format(self):
        decode = stream(request_id='r"%d', model="m%s")
        frames = decode.take(10)
        self.assertEqual((len(frames), decode.remaining), (3, 0))
        for i, frame in enumerate(frames):
            with self.subTest(token=i):
                obj, redump = compact(frame)
                self.assertEqual(frame, redump)
                self.assertEqual(list(obj), TOKEN_KEYS)
                self.assertEqual(
                    (obj["id"], obj["object"], obj["created"], obj["model"]),
                    ('cmpl-r"%d', "text_completion", 1700000000, "m%s"),
                )
                choice = obj["choices"][0]
                self.assertEqual(list(choice), CHOICE_KEYS)
                self.assertEqual(choice["text"], f" t{i}")
                self.assertEqual(choice["token_ids"], [1000 + i])
                self.assertEqual(choice["prompt_token_ids"], [5, 6] if i == 0 else None)
                self.assertEqual(choice["finish_reason"], "length" if i == 2 else None)
                self.assertIsNone(choice["logprobs"])
                self.assertIsNone(choice["stop_reason"])
                parsed = event_object(frame.decode())
                self.assertEqual(token_ids(event_choices(parsed)), (1000 + i,))

    def test_tail_carries_usage_then_done(self):
        usage, done = stream().tail()
        obj, redump = compact(usage)
        self.assertEqual(usage, redump)
        self.assertEqual(list(obj), [*TOKEN_KEYS, "usage"])
        self.assertEqual(obj["choices"], [])
        self.assertEqual(
            list(obj["usage"].items()),
            [("prompt_tokens", 2), ("total_tokens", 5), ("completion_tokens", 3)],
        )
        self.assertEqual(done, b"data: [DONE]\n\n")
        self.assertEqual(stream(include_usage=False).tail(), [b"data: [DONE]\n\n"])

    def test_frames_without_token_ids_carry_null_identity(self):
        (frame,) = stream(max_tokens=1, return_token_ids=False).take(1)
        obj, redump = compact(frame)
        self.assertEqual(frame, redump)
        choice = obj["choices"][0]
        self.assertEqual(
            (choice["prompt_token_ids"], choice["token_ids"], choice["finish_reason"]),
            (None, None, "length"),
        )

    def test_token_ids_and_text_cycle(self):
        frames = stream(max_tokens=1001).take(1001)
        for i in (999, 1000):
            with self.subTest(token=i):
                choice = compact(frames[i])[0]["choices"][0]
                self.assertEqual(choice["token_ids"], [1000 + i % 1000])
                self.assertEqual(choice["text"], f" t{i % 10}")


class PacingTests(unittest.TestCase):
    def test_each_tick_writes_one_batch_then_the_tail_in_separate_writes(self):
        target = engine(frames_per_write=2)
        transport = FakeTransport()
        target.attach(stream(max_tokens=5), transport)
        twin = stream(max_tokens=5)
        for _ in range(3):
            target.tick()
        usage, done = twin.tail()
        self.assertEqual(
            transport.writes,
            [
                b"".join(sim.chunk(frame) for frame in twin.take(2)),
                b"".join(sim.chunk(frame) for frame in twin.take(2)),
                b"".join(sim.chunk(frame) for frame in twin.take(2)),
                sim.chunk(usage),
                sim.chunk(done),
                sim.LAST_CHUNK,
            ],
        )
        self.assertEqual([len(chunks(write)) for write in transport.writes], [2, 2, 1, 1, 1, 1])
        payloads = [payload for write in transport.writes[:5] for payload in chunks(write)]
        self.assertTrue(all(one_frame(payload) for payload in payloads))
        self.assertEqual(chunks(sim.LAST_CHUNK), [b""])
        self.assertEqual(target.streams, {})
        target.tick()
        self.assertEqual(len(transport.writes), 6)

    def test_one_frame_per_write(self):
        target = engine()
        transport = FakeTransport()
        target.attach(stream(max_tokens=3, include_usage=False), transport)
        for _ in range(3):
            target.tick()
        self.assertEqual([len(chunks(write)) for write in transport.writes], [1, 1, 1, 1, 1])
        self.assertEqual(transport.writes[-1], sim.LAST_CHUNK)

    def test_closing_transport_is_dropped_without_writes(self):
        target = engine()
        transport = FakeTransport(closing=True)
        finished = []
        decode = stream()
        decode.on_finish = lambda: finished.append(True)
        target.attach(decode, transport)
        target.tick()
        self.assertEqual((transport.writes, target.streams, finished), ([], {}, []))

    def test_pace_counts_ticks_one_period_late(self):
        target = engine(token_interval_s=0.02, frames_per_write=2)
        period = 2 * 0.02
        target.next_tick = 10.0
        self.assertEqual(target.pace(10.0), 10.0 + period)
        self.assertEqual(target.late_ticks, 0)
        self.assertAlmostEqual(target.pace(10.0 + 3 * period), 10.0 + 4 * period)
        self.assertEqual(target.late_ticks, 1)
        self.assertAlmostEqual(target.pace(10.0 + 4.5 * period), 10.0 + 5 * period)
        self.assertEqual(target.late_ticks, 1)
        self.assertIn(b"simulated_engine_late_ticks_total 1\n", target.metrics())


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def connect(self, target):
        protocol = sim.EngineProtocol(target)
        transport = FakeTransport()
        protocol.connection_made(transport)
        return protocol, transport

    async def test_decode_reply_parses_as_chunked_sse(self):
        target = engine()
        protocol, transport = self.connect(target)
        client = h11.Connection(our_role=h11.CLIENT)
        body = json.dumps(
            {
                "model": "m",
                "prompt": "abcd",
                "max_tokens": 3,
                "stream": True,
                "return_token_ids": True,
                "stream_options": {"include_usage": True},
                "kv_transfer_params": HANDOFF,
            }
        ).encode()
        raw = request_bytes(client, "POST", "/v1/completions", body, [("x-request-id", "r1")])
        for i in range(len(raw)):
            protocol.data_received(raw[i : i + 1])
        await until(lambda: target.streams)
        self.assertEqual(
            transport.writes,
            [
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream; charset=utf-8\r\n"
                b"transfer-encoding: chunked\r\n\r\n"
            ],
        )
        while target.streams:
            target.tick()
        client.receive_data(b"".join(transport.writes))
        response, *data, end = events(client)
        self.assertEqual(response.status_code, 200)
        self.assertIn((b"transfer-encoding", b"chunked"), response.headers)
        self.assertIsInstance(end, h11.EndOfMessage)
        self.assertEqual(len(data), 5)
        for event in data:
            self.assertIsInstance(event, h11.Data)
            self.assertTrue(event.chunk_start and event.chunk_end)
            self.assertTrue(one_frame(event.data))
        self.assertEqual(compact(data[0].data)[0]["id"], "cmpl-r1")
        self.assertEqual(data[-1].data, b"data: [DONE]\n\n")
        client.start_next_cycle()
        self.assertFalse(transport.closing)
        self.assertFalse(protocol.busy)

    async def test_keep_alive_serves_sequential_requests(self):
        protocol, transport = self.connect(engine())
        client = h11.Connection(our_role=h11.CLIENT)
        for target, expected in (("/version", b'{"version":"simulated"}'), ("/health", b"")):
            with self.subTest(target=target):
                transport.writes.clear()
                protocol.data_received(request_bytes(client, "GET", target))
                await until(lambda: transport.writes)
                client.receive_data(b"".join(transport.writes))
                response, *data, end = events(client)
                self.assertEqual(response.status_code, 200)
                self.assertIn((b"content-length", str(len(expected)).encode()), response.headers)
                self.assertIn((b"content-type", b"application/json"), response.headers)
                self.assertEqual(b"".join(event.data for event in data), expected)
                self.assertIsInstance(end, h11.EndOfMessage)
                client.start_next_cycle()
        self.assertFalse(transport.closing)

    async def test_pipelined_request_waits_for_the_stream_to_finish(self):
        target = engine()
        protocol, transport = self.connect(target)
        client = h11.Connection(our_role=h11.CLIENT)
        body = json.dumps({"model": "m", "prompt": "ab", "max_tokens": 2, "stream": True})
        protocol.data_received(
            request_bytes(client, "POST", "/v1/completions", body.encode())
            + b"GET /health HTTP/1.1\r\nhost: sim\r\n\r\n"
        )
        await until(lambda: target.streams)
        target.tick()
        await asyncio.sleep(0)
        self.assertEqual(len(transport.writes), 2)
        target.tick()
        await until(lambda: len(transport.writes) == 6)
        client.receive_data(b"".join(transport.writes))
        self.assertIsInstance(events(client)[-1], h11.EndOfMessage)
        client.start_next_cycle()
        request_bytes(client, "GET", "/health")
        response, end = events(client)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(end, h11.EndOfMessage)

    async def test_connection_close_closes_after_the_reply(self):
        protocol, transport = self.connect(engine())
        protocol.data_received(b"GET /health HTTP/1.1\r\nhost: sim\r\nconnection: close\r\n\r\n")
        await until(lambda: transport.closing)
        self.assertEqual(len(transport.writes), 1)

    async def test_connection_lost_drops_the_connection_stream(self):
        target = engine()
        protocol, _ = self.connect(target)
        body = json.dumps({"model": "m", "prompt": "ab", "max_tokens": 2, "stream": True})
        protocol.data_received(
            b"POST /v1/completions HTTP/1.1\r\nhost: sim\r\ncontent-length: %d\r\n\r\n%s"
            % (len(body), body.encode())
        )
        await until(lambda: target.streams)
        protocol.connection_lost(None)
        self.assertEqual((target.streams, target.connections), ({}, set()))


class ArgumentTests(unittest.TestCase):
    def test_options_parse_and_reject_invalid_pacing(self):
        args = sim.parse_args(["--iid", "e0"])
        self.assertEqual(
            (args.host, args.port, args.token_interval, args.frames_per_write),
            ("127.0.0.1", 0, 0.02, 1),
        )
        self.assertEqual(args.prefill_seconds, 0.005)
        for argv in (
            ["--token-interval", "0"],
            ["--token-interval", "nan"],
            ["--frames-per-write", "0"],
            ["--prefill-seconds", "-1"],
        ):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    sim.parse_args(["--iid", "e0", *argv])
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
