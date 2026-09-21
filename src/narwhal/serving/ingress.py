"""Bound retained HTTP requests before parsing and cancel work on disconnect."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .lifecycle import RequestLifecycle
from .response import RequestStreamResponse

if TYPE_CHECKING:
    from .router import NarwhalRouter

LIFECYCLE = "narwhal.lifecycle"
COMPLETION_PATHS = frozenset(("/v1/completions", "/v1/chat/completions"))


class BodyTooLarge(Exception):
    """The streamed request body exceeded the configured byte limit."""


class ServingIngress:
    """Bound body readers, queued work and response writers as one retained pool."""

    def __init__(self, app: ASGIApp, router: NarwhalRouter) -> None:
        self.app, self.router = app, router

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Create one original owner before a completion endpoint reads its body."""
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] not in COMPLETION_PATHS
        ):
            await self.app(scope, receive, send)
            return
        router = self.router
        state = RequestLifecycle.offered(router, dict(Headers(scope=scope)))
        scope[LIFECYCLE] = state
        limit = router.max_concurrent + router.cfg.serving.queue_capacity
        if router.ingress_inflight >= limit:
            state.finish("rejected", error="HTTP retention limit reached", status=429)
            response = JSONResponse(
                {
                    "error": {
                        "type": "server_overloaded_error",
                        "message": "HTTP retention limit reached",
                    }
                },
                status_code=429,
                headers={"retry-after": "1", "x-request-id": state.rid},
            )
            # No admission seat is available to retain a blocked error writer.
            async with asyncio.timeout(0):
                await response(scope, receive, send)
            return
        router.ingress_inflight += 1
        router.ingress_high_water = max(router.ingress_high_water, router.ingress_inflight)
        started = False

        async def record_start(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["x-request-id"] = state.rid
                status = message["status"]
                if status >= 400 and state.terminal is None:
                    terminal = "invalid" if status in (400, 404, 413, 422) else "rejected"
                    state.finish(terminal, error=f"HTTP {status} before dispatch", status=status)
            await send(message)

        try:
            state.ingress_timer = asyncio.timeout(max(0.0, state.deadline - router._clock()))
            async with state.ingress_timer:
                await self.app(scope, receive, record_start)
        except TimeoutError:
            state.finish("expired", error="original request deadline expired", status=504)
            if started:
                raise
            # Send the timeout response immediately if the transport is writable;
            # cancel the write as soon as it blocks.
            async with asyncio.timeout(0):
                await JSONResponse(
                    {
                        "error": {
                            "type": "request_expired",
                            "message": "original request deadline expired",
                        }
                    },
                    status_code=504,
                    headers={"x-request-id": state.rid},
                )(scope, receive, send)
        except ClientDisconnect:
            state.finish("cancelled")
        except asyncio.CancelledError:
            state.finish("cancelled")
            raise
        except BaseException:
            state.finish("failed", error="HTTP request execution failed", status=500)
            raise
        finally:
            state.finish("cancelled")
            router.ingress_inflight -= 1


async def bounded_body(request: Request, limit: int) -> bytes:
    """Read at most the configured bytes without trusting Content-Length."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise BodyTooLarge
        body.extend(chunk)
    return bytes(body)


async def serve_connected(
    router: NarwhalRouter, request: Request, body: dict, headers: dict[str, str]
) -> Response:
    """Watch disconnects after consuming the body and before ASGI sends the response."""

    async def disconnected() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    serving = asyncio.create_task(
        router.serve(request.url.path, body, headers, lifecycle=request.scope[LIFECYCLE])
    )
    watching = asyncio.create_task(disconnected())
    handed_off = False
    try:
        done, _ = await asyncio.wait((serving, watching), return_when=asyncio.FIRST_COMPLETED)
        if watching in done:
            watching.result()
            raise ClientDisconnect
        response = serving.result()
        handed_off = True
        return response
    finally:
        for task in (watching, serving):
            if not task.done():
                task.cancel()
        await asyncio.gather(watching, serving, return_exceptions=True)
        # A disconnect can arrive between stream preparation and iteration.
        if not handed_off and not serving.cancelled() and serving.exception() is None:
            response = serving.result()
            if isinstance(response, RequestStreamResponse):
                await response.aclose()
