"""HTTP routes of the control service, all behind bearer-token authentication."""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .records import Action
from .service import ActionError, ControlService, session_changes

API = "/api"


class BearerAuth:
    """Refuse HTTP requests without the control token, except GETs for `public` paths."""

    def __init__(self, app: ASGIApp, token: str, public: Iterable[str] = ()) -> None:
        self.app = app
        self._token = token.encode()
        self._public = frozenset(public)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass lifespan events through and admit only HTTP requests carrying the token."""
        if scope["type"] == "lifespan" or (
            scope["type"] == "http" and (self._is_public(scope) or self._admits(scope))
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # The service serves no WebSocket routes.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": "missing or invalid bearer token"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _is_public(self, scope: Scope) -> bool:
        return scope["method"] == "GET" and scope["path"] in self._public

    def _admits(self, scope: Scope) -> bool:
        values = [value for name, value in scope["headers"] if name == b"authorization"]
        if len(values) != 1:
            return False
        scheme, _, credential = values[0].partition(b" ")
        return scheme.lower() == b"bearer" and hmac.compare_digest(credential.strip(), self._token)


def action_reply(action: Action, status: int = 200) -> JSONResponse:
    """Return an action's run-record entry as the response."""
    return JSONResponse(action.document(), status_code=status)


def core_routes(service: ControlService) -> APIRouter:
    """Return the health, session and load-job routes."""
    routes = APIRouter(prefix=API)

    @routes.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", **service.status()}

    @routes.post("/session")
    async def start_session() -> JSONResponse:
        return action_reply(await service.start_session(), 201)

    @routes.get("/session")
    async def session() -> JSONResponse:
        if service.session is None:
            return JSONResponse({"detail": "no session is active"}, status_code=404)
        return JSONResponse(
            {**service.session.document(), "changes": session_changes(service.session)}
        )

    @routes.post("/session/end")
    async def end_session() -> JSONResponse:
        return action_reply(await service.end_session())

    @routes.post("/jobs")
    async def start_job(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            params = json.loads(raw) if raw else {}
        except ValueError:
            params = None
        if not isinstance(params, dict):
            return JSONResponse({"detail": "job parameters must be a JSON object"}, 422)
        return action_reply(await service.start_job(params), 201)

    @routes.get("/jobs/current")
    async def current_job() -> JSONResponse:
        job = None if service.jobs is None else service.jobs.job
        if job is None:
            return JSONResponse({"detail": "no load job has run"}, status_code=404)
        return JSONResponse(job.document())

    @routes.post("/jobs/current/stop")
    async def stop_job() -> JSONResponse:
        return action_reply(await service.stop_job())

    return routes


def create_app(
    service: ControlService,
    token: str,
    routers: Iterable[APIRouter] = (),
    public: Iterable[str] = (),
) -> FastAPI:
    """Build the control app with `routers` behind the token."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await service.close()

    app = FastAPI(
        title="Narwhal fleet control",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.service = service
    app.include_router(core_routes(service))
    for router in routers:
        app.include_router(router)

    @app.exception_handler(ActionError)
    async def action_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ActionError)
        body: dict[str, Any] = {"detail": str(exc)}
        if exc.action is not None:
            body["action"] = exc.action.document()
        return JSONResponse(body, status_code=exc.status)

    app.add_middleware(BearerAuth, token=token, public=tuple(public))
    return app
