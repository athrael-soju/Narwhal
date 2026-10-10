"""Serve one simulated engine for router-only benchmarks."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, NamedTuple

import httptools

SIMULATED_VERSION = "simulated"
LATE_TICKS_METRIC = "simulated_engine_late_ticks_total"
ACTIVE_METRIC = "simulated_engine_active_streams"
PEAK_METRIC = "simulated_engine_peak_streams"
MAX_MODEL_LEN = 131072
BLOCK_TOKENS = 16
LAST_CHUNK = b"0\r\n\r\n"
DONE_FRAME = b"data: [DONE]\n\n"
EVENT_STREAM = "text/event-stream; charset=utf-8"
METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
BACKLOG = 4096
# Token i carries the ID 1000 + i % 1000.
TOKEN_IDS = tuple(b"[%d]" % (1000 + i) for i in range(1000))


def compact(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def chunk(frame: bytes) -> bytes:
    return b"%x\r\n" % len(frame) + frame + b"\r\n"


def tokenize(body: Mapping[str, Any]) -> list[int]:
    """Return one token per prompt character, or the prompt's token IDs."""
    if "messages" in body:
        text = "".join(
            message["content"]
            for message in body["messages"]
            if isinstance(message.get("content"), str)
        )
        return [ord(c) for c in text]
    prompt = body.get("prompt", "")
    if isinstance(prompt, str):
        return [ord(c) for c in prompt]
    return list(prompt)


def error(status: int, message: str, kind: str) -> Reply:
    return Reply(
        status, body=compact({"error": {"message": message, "type": kind, "code": status}})
    )


def head(status: int, content_type: str, framing: bytes) -> bytes:
    return b"HTTP/1.1 %d %s\r\ncontent-type: %s\r\n%s\r\n" % (
        status,
        HTTPStatus(status).phrase.encode(),
        content_type.encode(),
        framing,
    )


def _ignore() -> None:
    return None


class DecodeStream:
    """Token frames and tail of one decode reply in vLLM completion stream format."""

    def __init__(
        self,
        *,
        request_id: str,
        model: str,
        created: int,
        prompt_ids: list[int],
        max_tokens: int,
        return_token_ids: bool,
        include_usage: bool,
    ) -> None:
        identity = b'data: {"id":%s,"object":"text_completion","created":%d,"model":%s' % (
            compact("cmpl-" + request_id),
            created,
            compact(model),
        )
        self._frame = identity.replace(b"%", b"%%") + (
            b',"choices":[{"index":0,"text":" t%d","logprobs":null,"finish_reason":%s,'
            b'"stop_reason":null,"prompt_token_ids":%s,"token_ids":%s}]}\n\n'
        )
        self._prompt = compact(prompt_ids) if return_token_ids else b"null"
        self._ids = return_token_ids
        self._next = 0
        self._count = max_tokens
        usage = compact(
            {
                "prompt_tokens": len(prompt_ids),
                "total_tokens": len(prompt_ids) + max_tokens,
                "completion_tokens": max_tokens,
            }
        )
        self._tail = [identity + b',"choices":[],"usage":%s}\n\n' % usage] if include_usage else []
        self._tail.append(DONE_FRAME)
        self.on_finish: Callable[[], None] = _ignore

    @property
    def remaining(self) -> int:
        return self._count - self._next

    def take(self, count: int) -> list[bytes]:
        start = self._next
        self._next = stop = min(start + count, self._count)
        return [self._token(i) for i in range(start, stop)]

    def tail(self) -> list[bytes]:
        return list(self._tail)

    def _token(self, i: int) -> bytes:
        return self._frame % (
            i % 10,
            b'"length"' if i == self._count - 1 else b"null",
            self._prompt if i == 0 else b"null",
            TOKEN_IDS[i % 1000] if self._ids else b"null",
        )


@dataclass
class Reply:
    status: int
    content_type: str = "application/json"
    body: bytes = b""
    stream: DecodeStream | None = None


def handoff(params: object) -> bool:
    return (
        isinstance(params, dict)
        and isinstance(params.get("remote_engine_id"), str)
        and params["remote_engine_id"] != ""
        and isinstance(params.get("remote_block_ids"), list)
    )


class VllmWire:
    def route(self, engine: SimulatedEngine, method: str, path: str, body: bytes) -> Reply | None:
        if (method, path) == ("GET", "/version"):
            return Reply(200, body=compact({"version": SIMULATED_VERSION}))
        return None

    def identity_metrics(self, engine: SimulatedEngine) -> str:
        return (
            "# HELP process_start_time_seconds Start time of the process since unix epoch in "
            "seconds.\n"
            "# TYPE process_start_time_seconds gauge\n"
            f"process_start_time_seconds {engine.process_start_time_seconds!r}\n"
        )

    def request_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str | None:
        return headers.get("x-request-id")

    def decode_error(self, payload: dict[str, Any]) -> Reply | None:
        params = payload.get("kv_transfer_params")
        if params is not None and not handoff(params):
            return error(400, "invalid kv_transfer_params", "BadRequestError")
        return None

    def prefill_fields(
        self, engine: SimulatedEngine, rid: str, payload: dict[str, Any], prompt: int
    ) -> dict[str, Any]:
        params = payload.get("kv_transfer_params")
        if not isinstance(params, dict) or not params.get("do_remote_decode"):
            return {}
        return {
            "kv_transfer_params": {
                "do_remote_prefill": True,
                "do_remote_decode": False,
                "remote_block_ids": list(range(math.ceil(prompt / BLOCK_TOKENS))),
                "remote_engine_id": f"sim-{engine.iid}",
                "remote_request_id": rid,
                "remote_host": "127.0.0.1",
                "remote_port": engine.port,
                "tp_size": 1,
            }
        }


class SglangWire:
    def route(self, engine: SimulatedEngine, method: str, path: str, body: bytes) -> Reply | None:
        if (method, path) == ("GET", "/server_info"):
            state = {"disaggregation_mode": engine.role, "decode_cuda_graph_memory_gb": 0.0}
            return Reply(
                200,
                body=compact(
                    {
                        "version": SIMULATED_VERSION,
                        "disaggregation_mode": "prefill",
                        "api_key": None,
                        "internal_states": [state],
                    }
                ),
            )
        if (method, path) in (("GET", "/flush_cache"), ("POST", "/flush_cache")):
            return Reply(200, "text/plain", b"Cache flushed.")
        if (method, path) == ("POST", "/pd_role_switch"):
            try:
                role = json.loads(body).get("new_role")
            except (ValueError, AttributeError):
                role = None
            if role not in ("prefill", "decode"):
                return error(400, "new_role must be prefill or decode", "BadRequestError")
            old, engine.role = engine.role, role
            return Reply(
                200,
                body=compact({"success": True, "message": "ok", "old_role": old, "new_role": role}),
            )
        return None

    def identity_metrics(self, engine: SimulatedEngine) -> str:
        # The engine's startup phases stand in for a process start time.
        return (
            "# HELP sglang:startup_time_seconds Engine startup duration by phase in seconds.\n"
            "# TYPE sglang:startup_time_seconds gauge\n"
            f'sglang:startup_time_seconds{{phase="load_weight"}} '
            f"{engine.process_start_time_seconds!r}\n"
        )

    def request_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str | None:
        rid = payload.get("rid")
        return rid if isinstance(rid, str) else None

    def decode_error(self, payload: dict[str, Any]) -> Reply | None:
        if type(payload.get("bootstrap_room")) is not int:
            return error(
                400, "Disaggregated request received without bootstrap room id", "BadRequestError"
            )
        return None

    def prefill_fields(
        self, engine: SimulatedEngine, rid: str, payload: dict[str, Any], prompt: int
    ) -> dict[str, Any]:
        return {}


# One wire protocol per engine backend name.
PROTOCOLS = {"sglang": SglangWire(), "vllm": VllmWire()}


class SimulatedEngine:
    """Answer engine routes and pace decode frames over the active streams."""

    def __init__(
        self,
        iid: str,
        *,
        token_interval_s: float = 0.02,
        frames_per_write: int = 1,
        prefill_s: float = 0.005,
        backend: str = "vllm",
    ) -> None:
        self.iid = iid
        self.wire = PROTOCOLS[backend]
        self.token_interval_s = token_interval_s
        self.frames_per_write = frames_per_write
        self.prefill_s = prefill_s
        self.process_start_time_seconds = time.time()
        self.role = "prefill"
        self.late_ticks = 0
        self.prefilling = 0
        self.peak = {"prefill": 0, "decode": 0}
        self.next_tick = 0.0
        self.url = ""
        self.port = 0
        self.streams: dict[DecodeStream, asyncio.WriteTransport] = {}
        self.connections: set[asyncio.BaseTransport] = set()
        self._server: asyncio.Server | None = None
        self._ticker: asyncio.TimerHandle | None = None

    @property
    def period(self) -> float:
        return self.frames_per_write * self.token_interval_s

    async def handle(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> Reply:
        route = (method, path)
        if route == ("GET", "/health"):
            return Reply(200)
        identity = self.wire.route(self, method, path, body)
        if identity is not None:
            return identity
        if route == ("GET", "/metrics"):
            return Reply(200, METRICS_TYPE, self.metrics())
        if route not in (("POST", "/tokenize"), ("POST", "/v1/completions")):
            return error(404, "not found", "NotFoundError")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return error(400, "request body must be a JSON object", "BadRequestError")
        if path == "/tokenize":
            tokens = tokenize(payload)
            return Reply(
                200,
                body=compact(
                    {
                        "count": len(tokens),
                        "max_model_len": MAX_MODEL_LEN,
                        "tokens": tokens,
                        "token_strs": None,
                    }
                ),
            )
        rid = self.wire.request_id(headers, payload) or uuid.uuid4().hex
        if payload.get("stream"):
            return self.decode(rid, payload)
        return await self.prefill(rid, payload)

    def decode(self, rid: str, payload: dict[str, Any]) -> Reply:
        refused = self.wire.decode_error(payload)
        if refused is not None:
            return refused
        max_tokens = payload.get("max_tokens", 16)
        if type(max_tokens) is not int or max_tokens < 1:
            return error(400, "max_tokens must be a positive integer", "BadRequestError")
        return Reply(
            200,
            EVENT_STREAM,
            stream=DecodeStream(
                request_id=rid,
                model=payload.get("model", ""),
                created=int(time.time()),
                prompt_ids=tokenize(payload),
                max_tokens=max_tokens,
                return_token_ids=bool(payload.get("return_token_ids")),
                include_usage=bool((payload.get("stream_options") or {}).get("include_usage")),
            ),
        )

    async def prefill(self, rid: str, payload: dict[str, Any]) -> Reply:
        self.prefilling += 1
        self.peak["prefill"] = max(self.peak["prefill"], self.prefilling)
        try:
            await asyncio.sleep(self.prefill_s)
        finally:
            self.prefilling -= 1
        prompt = len(tokenize(payload))
        reply: dict[str, Any] = {
            "id": f"cmpl-{rid}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": payload.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "text": " t0",
                    "logprobs": None,
                    "finish_reason": "length",
                    "stop_reason": None,
                    "prompt_token_ids": None,
                    "token_ids": None,
                }
            ],
            "usage": {"prompt_tokens": prompt, "total_tokens": prompt + 1, "completion_tokens": 1},
            **self.wire.prefill_fields(self, rid, payload, prompt),
        }
        return Reply(200, body=compact(reply))

    def metrics(self) -> bytes:
        return (
            self.wire.identity_metrics(self)
            + f"# HELP {LATE_TICKS_METRIC} Pacing ticks that started one tick period or more "
            "after their scheduled time.\n"
            f"# TYPE {LATE_TICKS_METRIC} counter\n"
            f"{LATE_TICKS_METRIC} {self.late_ticks}\n"
            f"# HELP {ACTIVE_METRIC} Prefill and decode requests in progress.\n"
            f"# TYPE {ACTIVE_METRIC} gauge\n"
            f'{ACTIVE_METRIC}{{phase="prefill"}} {self.prefilling}\n'
            f'{ACTIVE_METRIC}{{phase="decode"}} {len(self.streams)}\n'
            f"# HELP {PEAK_METRIC} Most prefill and decode requests in progress at once.\n"
            f"# TYPE {PEAK_METRIC} gauge\n"
            f'{PEAK_METRIC}{{phase="prefill"}} {self.peak["prefill"]}\n'
            f'{PEAK_METRIC}{{phase="decode"}} {self.peak["decode"]}\n'
        ).encode()

    def attach(self, stream: DecodeStream, transport: asyncio.WriteTransport) -> None:
        self.streams[stream] = transport
        self.peak["decode"] = max(self.peak["decode"], len(self.streams))

    def drop(self, stream: DecodeStream) -> None:
        self.streams.pop(stream, None)

    def tick(self) -> None:
        count = self.frames_per_write
        closed: list[DecodeStream] = []
        finished: list[DecodeStream] = []
        for stream, transport in self.streams.items():
            if transport.is_closing():
                closed.append(stream)
                continue
            transport.write(b"".join([chunk(frame) for frame in stream.take(count)]))
            if not stream.remaining:
                for frame in stream.tail():
                    transport.write(chunk(frame))
                transport.write(LAST_CHUNK)
                finished.append(stream)
        for stream in closed:
            del self.streams[stream]
        for stream in finished:
            del self.streams[stream]
            stream.on_finish()

    def pace(self, now: float) -> float:
        period = self.period
        if now - self.next_tick >= period:
            self.late_ticks += 1
            self.next_tick = now
        self.tick()
        self.next_tick += period
        return self.next_tick

    def ready_line(self) -> str:
        return json.dumps(
            {
                "iid": self.iid,
                "url": self.url,
                "pid": os.getpid(),
                "version": SIMULATED_VERSION,
                "process_start_time_seconds": self.process_start_time_seconds,
            }
        )

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(
            lambda: EngineProtocol(self), host, port, backlog=BACKLOG
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://{host}:{self.port}"
        self.next_tick = loop.time() + self.period
        self._ticker = loop.call_at(self.next_tick, self._advance, loop)
        return self.url

    def _advance(self, loop: asyncio.AbstractEventLoop) -> None:
        self._ticker = loop.call_at(self.pace(loop.time()), self._advance, loop)

    async def close(self) -> None:
        self._ticker.cancel()
        self._server.close()
        for transport in list(self.connections):
            transport.close()
        await self._server.wait_closed()


class Request(NamedTuple):
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    keep_alive: bool


class EngineProtocol(asyncio.Protocol):
    """Serve HTTP/1.1 requests on one connection, one at a time."""

    def __init__(self, engine: SimulatedEngine) -> None:
        self.engine = engine
        self.parser = httptools.HttpRequestParser(self)
        self.transport: asyncio.Transport | None = None
        self.requests: deque[Request] = deque()
        self.busy = False
        self.close_after = False
        self.task: asyncio.Task[None] | None = None
        self.stream: DecodeStream | None = None
        self._url = b""
        self._headers: dict[str, str] = {}
        self._body: list[bytes] = []
        self._keep_alive = True

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        self.engine.connections.add(transport)

    def connection_lost(self, exc: Exception | None) -> None:
        self.engine.connections.discard(self.transport)
        if self.task is not None:
            self.task.cancel()
        if self.stream is not None:
            self.engine.drop(self.stream)

    def data_received(self, data: bytes) -> None:
        try:
            self.parser.feed_data(data)
        except (httptools.HttpParserError, httptools.HttpParserUpgrade):
            self.transport.close()

    def on_message_begin(self) -> None:
        self._url = b""
        self._headers = {}
        self._body = []

    def on_url(self, url: bytes) -> None:
        self._url += url

    def on_header(self, name: bytes, value: bytes) -> None:
        self._headers[name.decode("latin-1").lower()] = value.decode("latin-1")

    def on_headers_complete(self) -> None:
        self._keep_alive = self.parser.should_keep_alive()

    def on_body(self, body: bytes) -> None:
        self._body.append(body)

    def on_message_complete(self) -> None:
        self.requests.append(
            Request(
                self.parser.get_method().decode(),
                httptools.parse_url(self._url).path.decode(),
                self._headers,
                b"".join(self._body),
                self._keep_alive,
            )
        )
        if not self.busy:
            self._next()

    def _next(self) -> None:
        self.busy = True
        request = self.requests.popleft()
        self.task = asyncio.get_running_loop().create_task(self._respond(request))

    async def _respond(self, request: Request) -> None:
        reply = await self.engine.handle(
            request.method, request.path, request.headers, request.body
        )
        self.close_after = not request.keep_alive
        if reply.stream is None:
            self.transport.write(
                head(reply.status, reply.content_type, b"content-length: %d\r\n" % len(reply.body))
                + reply.body
            )
            self._finish()
            return
        self.transport.write(
            head(reply.status, reply.content_type, b"transfer-encoding: chunked\r\n")
        )
        self.stream = reply.stream
        reply.stream.on_finish = self._finish
        self.engine.attach(reply.stream, self.transport)

    def _finish(self) -> None:
        self.task = None
        self.stream = None
        if self.close_after:
            self.transport.close()
            return
        self.busy = False
        if self.requests:
            self._next()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--iid", required=True)
    options.add_argument("--backend", choices=sorted(PROTOCOLS), default="vllm")
    options.add_argument("--host", default="127.0.0.1")
    options.add_argument("--port", type=int, default=0)
    options.add_argument("--token-interval", type=float, default=0.02)
    options.add_argument("--frames-per-write", type=int, default=1)
    options.add_argument("--prefill-seconds", type=float, default=0.005)
    args = options.parse_args(argv)
    if not (
        0 < args.token_interval < math.inf
        and args.frames_per_write >= 1
        and 0 <= args.prefill_seconds < math.inf
    ):
        options.error(
            "--token-interval must be positive and finite, --frames-per-write at least 1, "
            "and --prefill-seconds nonnegative and finite"
        )
    return args


async def serve(args: argparse.Namespace) -> int:
    engine = SimulatedEngine(
        args.iid,
        token_interval_s=args.token_interval,
        frames_per_write=args.frames_per_write,
        prefill_s=args.prefill_seconds,
        backend=args.backend,
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    await engine.start(args.host, args.port)
    print(engine.ready_line(), flush=True)
    await stop.wait()
    await engine.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import uvloop

    return uvloop.run(serve(args))


if __name__ == "__main__":
    raise SystemExit(main())
