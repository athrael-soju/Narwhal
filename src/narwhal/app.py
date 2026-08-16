"""The app uvicorn runs: routes, lifespan, and nothing the router owns.

`create_app` wires one ArrowRouter into FastAPI; the serving logic lives in
`server`, the response shapes in `schemas`, the journal in `journal`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import Request as HTTPRequest
from fastapi.responses import Response

from . import __version__
from . import state as handoff_state
from .config import FleetConfig
from .journal import PayloadLog, RunJournal
from .metrics import render
from .schemas import HealthOut, ModelsOut, StateOut
from .server import ArrowRouter, _monitor_loop
from .standby import PROBE_INTERVAL_S, TAKEOVER_AFTER, standby_loop

log = logging.getLogger("narwhal.app")

# Forwarded to the engine. Everything else the client sent is this router's
# business and stops here. `x-api-key` rides along because the tenant
# ledger's resolver accepts it as the bearer fallback - stripping it
# here would 401 a client presenting a perfectly good key.
_FORWARD_HEADERS = ("authorization", "x-request-id", "x-api-key")


def create_app(
    cfg: FleetConfig | None = None,
    max_concurrent: int | None = None,
    journal_path: Path | str | None = None,
    payloads_path: Path | str | None = None,
    payloads_max_chars: int | None = None,
    payloads_max_mb: int | None = None,
    standby_of: str | None = None,
    standby_probe_interval_s: float | None = None,
    standby_takeover_after: int | None = None,
    standby_transport: object | None = None,
) -> FastAPI:
    """The FastAPI app around one router; the lifespan owns the loop and journal."""
    cfg = cfg or FleetConfig.from_env()
    journal = RunJournal(
        path=Path(journal_path) if journal_path else cfg.profiles_path.parent / "journal.jsonl"
    )
    router = ArrowRouter(cfg, journal, max_concurrent=max_concurrent)
    # CLI overrides the config; either can turn capture on.
    resolved_payloads = payloads_path or (cfg.journal_payloads or None)
    if resolved_payloads:
        router.payloads = PayloadLog(
            resolved_payloads,
            max_chars=payloads_max_chars or cfg.journal_payloads_max_chars,
            max_mb=payloads_max_mb or cfg.journal_payloads_max_mb,
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Everything the process holds for its whole life, opened and closed.

        `@app.on_event` is deprecated in the installed FastAPI, and its removal
        would kill startup while the suite stayed green; the lifespan is what
        the app-level tests exercise. The profile gate runs before the journal
        opens, so a fleet the router cannot schedule leaves no file behind.
        """
        missing = router.unprofiled()
        if missing:
            raise RuntimeError(
                f"no profile for {', '.join(missing)}: run `narwhal-profile` against the "
                f"fleet first. Algorithm 1 prices every instance from its profile and "
                f"cannot schedule without one."
            )
        watch = None
        if standby_of:
            # Warm standby: refuse traffic, shadow the primary's handoff,
            # and open only when it goes silent. The resume flag is ignored -
            # the polled document is fresher than anything on disk.
            router.standby = True
            watch = asyncio.create_task(
                standby_loop(
                    router,
                    standby_of,
                    probe_interval_s=standby_probe_interval_s or PROBE_INTERVAL_S,
                    takeover_after=standby_takeover_after or TAKEOVER_AFTER,
                    transport=standby_transport,  # type: ignore[arg-type]
                )
            )
        elif cfg.resume:
            # Resume: take the late router's actuated picture. A missing or
            # mismatched handoff is an opening decision, not a startup error.
            handoff = handoff_state.apply(router, handoff_state.load(cfg.state_path))
            if not handoff.applied:
                log.info("no control-plane handoff applied: %s", handoff.why)
        journal.open()
        if router.payloads is not None:
            router.payloads.open()
        loop = asyncio.create_task(_monitor_loop(router))
        log.info(
            "narwhal up: %d instances, ttft<=%.3gs tpot<=%.3gs, interval %.2gs, "
            "admitting %d at once",
            len(router.monitor.instances),
            cfg.slo.ttft_s,
            cfg.slo.tpot_s,
            cfg.monitor_interval_s,
            router.max_concurrent,
        )
        # The run id is on every row of that file, so a log and a journal join.
        log.info("journal %s, run %s", journal.path, journal.run)
        try:
            yield
        finally:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop
            if watch is not None:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch
            await router.engines.aclose()
            # The freshest possible handoff: a clean shutdown never hands the
            # replacement anything older than this.
            with contextlib.suppress(OSError):
                handoff_state.write(cfg.state_path, handoff_state.snapshot(router))
            if router.payloads is not None:
                router.payloads.close()
            journal.close()

    app = FastAPI(title="narwhal", version=__version__, lifespan=lifespan)
    app.state.router = router

    @app.get("/v1/models", response_model=ModelsOut, summary="The one model this router fronts")
    async def models() -> dict[str, Any]:
        """OpenAI list shape; clients that list before sending see one entry."""
        return {
            "object": "list",
            "data": [{"id": cfg.model, "object": "model", "owned_by": "narwhal"}],
        }

    @app.get("/health", response_model=HealthOut, summary="Liveness, and the configured fleet size")
    async def health() -> dict[str, Any]:
        status = "standby" if router.standby else "ok"
        return {"status": status, "instances": len(router.monitor.instances)}

    @app.get("/arrow/handoff", summary="The live control-plane handoff document")
    async def handoff() -> dict[str, Any]:
        """What a replacement needs, fresh at request time; a warm standby
        polls this instead of reading the primary's disk."""
        return handoff_state.snapshot(router)

    @app.get("/metrics", summary="Prometheus counters and latency histograms")
    async def metrics() -> Response:
        return Response(
            content=render(router.state(), router.ttft, router.tpot),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/arrow/state", response_model=StateOut, summary="The live scheduler picture")
    async def state() -> dict[str, Any]:
        return router.state()

    @app.post("/v1/completions", summary="Completions, body passed to the engine unchanged")
    async def completions(request: HTTPRequest) -> Response:
        if router.standby:
            return _standby_refusal()
        return await router.serve("/v1/completions", await request.json(), _headers(request))

    @app.post("/v1/chat/completions", summary="Chat, body passed to the engine unchanged")
    async def chat_completions(request: HTTPRequest) -> Response:
        if router.standby:
            return _standby_refusal()
        return await router.serve("/v1/chat/completions", await request.json(), _headers(request))

    return app


def _standby_refusal() -> Response:
    """503 with Retry-After: a standby holds the door until the primary dies."""
    return Response(
        content='{"error": {"message": "standby: the primary router is serving", '
        '"type": "standby", "code": "standby"}}',
        status_code=503,
        media_type="application/json",
        headers={"retry-after": "1"},
    )


def _headers(request: HTTPRequest) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_HEADERS}
