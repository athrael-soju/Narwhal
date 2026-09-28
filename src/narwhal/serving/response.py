"""Close streaming ownership even when ASGI exits before iteration begins."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from .continuation import CommitGroup
from .lifecycle import RequestLifecycle


class RequestStreamResponse(StreamingResponse):
    """Own the upstream iterator and deadline through ASGI response teardown."""

    def __init__(
        self, stream: AsyncGenerator[str | CommitGroup, None], lifecycle: RequestLifecycle
    ) -> None:
        self.lifecycle = lifecycle
        self.upstream = stream
        self.closed = False
        self._asgi_active = False
        self._pending_group: CommitGroup | None = None
        if lifecycle.continuation is not None:
            lifecycle.continuation_response_owned = True
        self.iterator = self._iterate()
        super().__init__(self.iterator, media_type="text/event-stream")

    async def _iterate(self) -> AsyncGenerator[str | bytes, None]:
        try:
            async for line in self.upstream:
                if isinstance(line, CommitGroup):
                    self._pending_group = line
                    yield line.data
                    self._pending_group = None
                else:
                    yield line
                del line
        finally:
            try:
                await self._close_upstream()
            finally:
                if not self._asgi_active:
                    self._release_history()

    async def stream_response(self, send: Send) -> None:
        """Drop each committed group before fetching the next bounded group."""
        if self.lifecycle.continuation is None:
            await super().stream_response(send)
            return
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        async for chunk in self.body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode(self.charset)
            try:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            finally:
                del chunk
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _close_upstream(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()
        finally:
            self.lifecycle.finish("cancelled")

    async def aclose(self) -> None:
        """Close a response abandoned by a direct caller before or during iteration."""
        try:
            await self.iterator.aclose()
        finally:
            try:
                await self._close_upstream()
            finally:
                if not self._asgi_active:
                    self._release_history()

    def _release_history(self) -> None:
        self._pending_group = None
        if self.lifecycle.continuation is not None:
            self.lifecycle.continuation.close()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Hold the deadline through client writes and always close ownership."""
        started = False
        finished = False
        error_sent = False
        self._asgi_active = True

        async def record_send(message: Message) -> None:
            nonlocal started, finished, error_sent
            await send(message)
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body":
                group = self._pending_group
                history = self.lifecycle.continuation
                if group is not None and history is not None:
                    if message.get("body") is not group.data:
                        raise RuntimeError("ASGI body differs from its continuation group")
                    history.commit(group, self.lifecycle.router._clock())
                    self.lifecycle.tokens = history.committed_count
                    self.lifecycle.request.output_len = history.committed_count
                    self.lifecycle.output_started = True
                    self.lifecycle.end_interruption(self.lifecycle.router._clock())
                    self._pending_group = None
                finished = not message.get("more_body", False)
                if message.get("body") and self.lifecycle.terminal == "expired":
                    error_sent = True

        remaining = max(0.0, self.lifecycle.deadline - self.lifecycle.router._clock())
        timer = asyncio.timeout(remaining)
        try:
            async with timer:
                # The response now owns the original deadline. An outer ingress
                # cancellation would bypass this timeout's explicit SSE error.
                if self.lifecycle.ingress_timer is not None:
                    self.lifecycle.ingress_timer.reschedule(None)
                await super().__call__(scope, receive, record_send)
        except TimeoutError:
            if not timer.expired():
                raise
            if finished:
                return
            self.lifecycle.finish("expired", error="original request deadline expired", status=504)
            committed = (
                self.lifecycle.continuation is not None
                and self.lifecycle.continuation.terminal_committed
            )
            if not started or (self.lifecycle.terminal != "expired" and not committed):
                raise
            await self.aclose()
            error = {
                "error": {
                    "message": self.lifecycle.outcome["error"],
                    "type": self.lifecycle.phase,
                    "code": "expired",
                }
            }
            # Send immediately on a writable transport; cancel on backpressure
            # because the request deadline has expired.
            async with asyncio.timeout(0):
                await send(
                    {
                        "type": "http.response.body",
                        "body": b""
                        if error_sent or committed
                        else ("data: " + json.dumps(error) + "\n\n").encode(),
                        "more_body": False,
                    }
                )
        finally:
            try:
                await self.aclose()
            finally:
                self._asgi_active = False
                self._release_history()
