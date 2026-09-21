"""Close streaming ownership even when ASGI exits before iteration begins."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from .lifecycle import RequestLifecycle


class RequestStreamResponse(StreamingResponse):
    """Own the upstream iterator and deadline through ASGI response teardown."""

    def __init__(self, stream: AsyncGenerator[str, None], lifecycle: RequestLifecycle) -> None:
        self.lifecycle = lifecycle
        self.upstream = stream
        self.closed = False
        self.iterator = self._iterate()
        super().__init__(self.iterator, media_type="text/event-stream")

    async def _iterate(self) -> AsyncGenerator[str, None]:
        try:
            async for line in self.upstream:
                yield line
        finally:
            await self._close_upstream()

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
            await self._close_upstream()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Hold the deadline through client writes and always close ownership."""
        started = False
        finished = False
        error_sent = False

        async def record_send(message: Message) -> None:
            nonlocal started, finished, error_sent
            await send(message)
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body":
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
            if not started or self.lifecycle.terminal != "expired":
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
                        if error_sent
                        else ("data: " + json.dumps(error) + "\n\n").encode(),
                        "more_body": False,
                    }
                )
        finally:
            await self.aclose()
