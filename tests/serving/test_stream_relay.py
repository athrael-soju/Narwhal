"""Check the streamed relay: frame boundaries, client bytes, writes and journal fields."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.engines import stream as sse
from narwhal.serving.app import create_app
from narwhal.serving.router.routing import NarwhalRouter
from tests.fixtures import fleet
from tests.wire import engine_transports

PROMPT = {"model": "", "prompt": "hello", "max_tokens": 4, "stream": True}


def token(text, ids, finish=None):
    """One completion event carrying text and its token IDs."""
    return {"choices": [{"index": 0, "text": text, "token_ids": ids, "finish_reason": finish}]}


def metadata():
    """One completion event carrying zero tokens."""
    return {"choices": [{"index": 0, "text": "", "token_ids": []}]}


def wire(obj, *, end="\n\n"):
    """Engine wire form of one event, with raw non-ASCII characters."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + end


def client_frame(obj):
    """Client form of one event: compact JSON without token fields."""
    obj = json.loads(json.dumps(obj))
    for choice in obj.get("choices", []):
        choice.pop("token_ids", None)
        choice.pop("prompt_token_ids", None)
    return "data: " + json.dumps(obj, separators=(",", ":")) + "\n\n"


class ChunkStream(httpx.AsyncByteStream):
    """Engine body delivered as the given transport chunks, with optional pauses."""

    def __init__(self, chunks, closed):
        self.chunks = chunks
        self.closed = closed

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, float):
                await asyncio.sleep(chunk)
                continue
            yield chunk.encode() if isinstance(chunk, str) else chunk

    async def aclose(self):
        self.closed.set()


class StreamRelayTests(unittest.IsolatedAsyncioTestCase):
    """Streams cross the real router from chunked engine bodies to ASGI messages."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.cfg.tokenize = False
        self.cfg.admission = "open"
        self.chunks = []
        self.upstream_closed = asyncio.Event()

    async def engine(self, request):
        body = json.loads(request.content)
        if (body.get("kv_transfer_params") or {}).get("do_remote_decode"):
            return httpx.Response(
                200,
                json={"kv_transfer_params": {"remote_engine_id": "e0", "remote_block_ids": [0]}},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(self.chunks, self.upstream_closed),
        )

    def app(self):
        def router(*args, **kwargs):
            return NarwhalRouter(*args, **engine_transports(self.engine), **kwargs)

        with patch("narwhal.serving.app.NarwhalRouter", side_effect=router):
            app = create_app(self.cfg, journal_path=self.root / "journal.jsonl")
        self.router = app.state.router
        self.router.lifecycle.process_starts = {spec.iid: 100 for spec in self.cfg.engines}
        self.router.lifecycle.identities_ready = True
        self.router.journal.open()
        self.addCleanup(self.router.journal.close)
        self.addAsyncCleanup(self.router.engines.aclose)
        return app

    async def call(self, app, *, stream=True, disconnect_after_bodies=None):
        """Drive one completion through ASGI; return the status and each body message."""
        request = json.dumps({**PROMPT, "model": self.cfg.model, "stream": stream}).encode()
        delivered = False
        disconnect = asyncio.Event()
        messages = []

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": request, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            bodies = [m for m in messages if m["type"] == "http.response.body" and m.get("body")]
            if disconnect_after_bodies is not None and len(bodies) >= disconnect_after_bodies:
                disconnect.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/completions",
            "raw_path": b"/v1/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"router"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(request)).encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("router", 80),
        }
        await app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        bodies = [m["body"] for m in messages if m["type"] == "http.response.body" and m["body"]]
        return status, bodies

    def terminal_rows(self):
        return [
            row
            for line in (self.root / "journal.jsonl").read_text().splitlines()
            if "terminal" in (row := json.loads(line)) and "rid" in row
        ]

    # Characterization: these hold at the starting commit and on the chunked relay.

    async def test_frames_split_across_reads_reach_the_client_unchanged(self):
        events = [token("a", [1]), token("b", [2]), token("c", [3], "length")]
        body = "".join(wire(e, end="\r\n\r\n") for e in events) + "data: [DONE]\r\n\r\n"
        self.chunks = [body[:3], body[3:40], body[40:41], body[41:95], body[95:]]
        status, bodies = await self.call(self.app())
        self.assertEqual(status, 200)
        expected = "".join(client_frame(e) for e in events) + "data: [DONE]\n\n"
        self.assertEqual(b"".join(bodies).decode(), expected)
        row = self.terminal_rows()[-1]
        self.assertEqual((row["terminal"], row["output_len"]), ("completed", 3))

    async def test_metadata_and_keepalives_frame_as_before(self):
        events = [metadata(), token("a", [1]), token("b", [2], "length")]
        self.chunks = [
            ": keepalive\n\n",
            wire(events[0]),
            ": keepalive\n\n",
            wire(events[1]),
            ": keepalive\n\n",
            wire(events[2]) + "data: [DONE]\n\n",
        ]
        status, bodies = await self.call(self.app())
        self.assertEqual(status, 200)
        expected = (
            client_frame(events[0])
            + client_frame(events[1])
            + ": keepalive\n\n"
            + client_frame(events[2])
            + "data: [DONE]\n\n"
        )
        self.assertEqual(b"".join(bodies).decode(), expected)

    async def test_pre_output_metadata_above_64_frames_fails_before_output(self):
        self.chunks = ["".join(wire(metadata()) for _ in range(65)), wire(token("a", [1]))]
        status, bodies = await self.call(self.app())
        self.assertEqual(status, 502)
        self.assertEqual(
            json.loads(b"".join(bodies)),
            {
                "error": {
                    "message": "Response exceeds the configured limit",
                    "type": "decode",
                    "code": "decode",
                }
            },
        )
        row = self.terminal_rows()[-1]
        self.assertEqual(row["terminal"], "failed")
        self.assertIn("64-frame limit", row["error"])

    async def test_request_deadline_mid_stream_ends_with_an_expired_event(self):
        self.cfg.request_timeout_s = 0.6
        self.chunks = [wire(token("a", [1])), 2.0, wire(token("b", [2], "length"))]
        status, bodies = await self.call(self.app())
        self.assertEqual(status, 200)
        text = b"".join(bodies).decode()
        self.assertTrue(text.startswith(client_frame(token("a", [1]))))
        last = json.loads(text.rstrip("\n").rsplit("\n\n", 1)[-1][len("data: ") :])
        self.assertEqual(last["error"]["code"], "expired")
        self.assertEqual(self.terminal_rows()[-1]["terminal"], "expired")
        await asyncio.wait_for(self.upstream_closed.wait(), 1.0)

    async def test_client_disconnect_during_decode_cancels_and_closes_upstream(self):
        self.chunks = [wire(token("a", [1])), 5.0, wire(token("b", [2], "length"))]
        app = self.app()
        status, bodies = await asyncio.wait_for(self.call(app, disconnect_after_bodies=1), 3.0)
        self.assertEqual(status, 200)
        self.assertEqual(b"".join(bodies).decode(), client_frame(token("a", [1])))
        self.assertEqual(self.terminal_rows()[-1]["terminal"], "cancelled")
        await asyncio.wait_for(self.upstream_closed.wait(), 1.0)
        self.assertEqual(self.router.inflight, 0)

    async def test_journal_fields_follow_relayed_tokens(self):
        events = [token("a", [1, 2]), token("b", [3]), token("c", [4], "length")]
        self.chunks = [wire(events[0]), 0.05, wire(events[1]), 0.05, wire(events[2])]
        self.chunks.append("data: [DONE]\n\n")
        status, _ = await self.call(self.app())
        self.assertEqual(status, 200)
        row = self.terminal_rows()[-1]
        self.assertEqual(row["output_len"], 4)
        self.assertEqual(row["decode_tokens_observed"], 4)
        self.assertGreater(row["first_byte_s"], 0)
        self.assertGreater(row["decode_tpot_s"], 0)
        self.assertGreater(row["upstream_seconds"]["decode"], 0)

    # The chunked relay.

    async def test_frames_in_one_read_reach_the_client_in_one_write(self):
        events = [token("a", [1]), token("b", [2]), token("c", [3], "length")]
        self.chunks = ["".join(wire(e) for e in events) + "data: [DONE]\n\n"]
        status, bodies = await self.call(self.app())
        self.assertEqual(status, 200)
        expected = "".join(client_frame(e) for e in events) + "data: [DONE]\n\n"
        self.assertEqual([b.decode() for b in bodies], [expected])

    async def test_unicode_line_separators_stay_inside_one_event(self):
        for character in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(character=hex(ord(character))):
                event = token(f"a{character}b", [1], "length")
                self.chunks = [wire(event) + "data: [DONE]\n\n"]
                status, bodies = await self.call(self.app())
                self.assertEqual(status, 200)
                expected = client_frame(event) + "data: [DONE]\n\n"
                self.assertEqual(b"".join(bodies).decode(), expected)
                self.assertEqual(self.terminal_rows()[-1]["output_len"], 1)

    async def test_each_engine_data_frame_is_parsed_once(self):
        events = [token("a", [1]), metadata(), token("b", [2]), token("c", [3], "length")]
        self.chunks = ["".join(wire(e) for e in events[:2]), "".join(wire(e) for e in events[2:])]
        self.chunks.append("data: [DONE]\n\n")
        loads = json.loads
        payloads = {json.dumps(e, ensure_ascii=False) for e in events}
        parsed = []

        def counting(text, *args, **kwargs):
            if text in payloads:
                parsed.append(text)
            return loads(text, *args, **kwargs)

        app = self.app()
        with patch.object(sse.json, "loads", counting):
            status, _ = await self.call(app)
        self.assertEqual(status, 200)
        self.assertEqual(len(parsed), len(events))


if __name__ == "__main__":
    unittest.main()
