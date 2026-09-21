"""FastAPI routes and process lifespan."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi import Request as HTTPRequest
from fastapi.responses import JSONResponse, Response

from .. import __version__
from ..config import FleetConfig
from ..observability.journal import RunJournal
from ..observability.metrics import render
from ..runtime import state as handoff_state
from ..runtime.lease import FileLease, LeaseError
from ..runtime.lifecycle import (
    LifecycleError,
    ValidationOutcome,
    capture_process_identities,
    check_process_identities,
    validate_readmission,
)
from ..runtime.monitoring import monitor_loop
from ..runtime.standby import (
    MAX_HANDOFF_AGE_S,
    PROBE_INTERVAL_S,
    TAKEOVER_AFTER,
    control_ready,
    controls_fleet,
    lease_renew_loop,
    ready,
    standby_loop,
)
from .completion import completion_body_error
from .ingress import BodyTooLarge, ServingIngress, bounded_body, serve_connected
from .router import NarwhalRouter
from .schemas import DrainIn, HealthOut, ModelsOut, ReadmitIn, StateOut

log = logging.getLogger("narwhal.app")


_FORWARD_HEADERS = ("x-request-id",)


def create_app(
    cfg: FleetConfig | None = None,
    max_concurrent: int | None = None,
    journal_path: Path | str | None = None,
    standby_of: str | None = None,
    standby_probe_interval_s: float | None = None,
    standby_takeover_after: int | None = None,
    standby_max_handoff_age_s: float | None = None,
    standby_transport: httpx.AsyncBaseTransport | None = None,
    lease_path: Path | str | None = None,
    router_id: str = "router",
    lease_ttl_s: float = 5.0,
    lease_renew_interval_s: float = 1.0,
    lease_safety_margin_s: float = 1.0,
    lease: FileLease | None = None,
    lifecycle_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the API and bind router resources to its lifespan."""
    cfg = cfg or FleetConfig.from_env()
    if standby_of and lease is None and lease_path is None:
        raise ValueError("automatic standby takeover requires a shared lease_path")
    if lease_safety_margin_s < 0:
        raise ValueError("lease_safety_margin_s must be nonnegative")
    if lease_renew_interval_s <= 0 or lease_ttl_s <= lease_renew_interval_s + lease_safety_margin_s:
        raise ValueError(
            "lease_ttl_s must exceed the renewal interval plus the clock safety margin"
        )
    if standby_max_handoff_age_s is not None and standby_max_handoff_age_s <= 0:
        raise ValueError("standby_max_handoff_age_s must be positive")
    if lease is None and lease_path is not None:
        holder = f"{router_id}:{uuid.uuid4().hex}"
        lease = FileLease(
            Path(lease_path),
            holder,
            lease_ttl_s,
            safety_margin_s=lease_safety_margin_s,
        )
    journal = RunJournal(
        path=Path(journal_path) if journal_path else cfg.profiles_path.parent / "journal.jsonl"
    )
    router = NarwhalRouter(cfg, journal, max_concurrent=max_concurrent)
    router.lifecycle_transport = lifecycle_transport
    router.lease = lease
    router.lease_epoch = 0
    router.lease_holder = ""
    router.failover_blocked = ""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open and close router resources around the ASGI lifespan."""
        missing, extra = router.profile_set_diff()
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"no profile for {', '.join(missing)}")
            if extra:
                detail.append(f"stale profiles for unconfigured engines: {', '.join(extra)}")
            raise RuntimeError(
                "; ".join(detail)
                + ": run `narwhal-profile` against the fleet first. Placement requires "
                "a profile for exactly the configured engine set."
            )
        watch = None
        lease_watch = None
        if lease is not None:
            if standby_of:
                router.standby = True
            else:
                try:
                    claimed = lease.claim()
                except LeaseError as exc:
                    claimed = False
                    router.failover_blocked = f"lease unavailable: {exc}"
                if claimed:
                    router.lease_epoch = lease.epoch
                    router.lease_holder = lease.holder
                else:
                    router.standby = True
                    router.failover_blocked = (
                        router.failover_blocked or "lease held by another router"
                    )
            lease_watch = asyncio.create_task(lease_renew_loop(router, lease_renew_interval_s))
        if standby_of:
            # A polled handoff is fresher than the local resume file.
            if lease is None:
                raise ValueError("automatic standby takeover requires a shared lease")
            router.standby = True
            watch = asyncio.create_task(
                standby_loop(
                    router,
                    standby_of,
                    lease,
                    probe_interval_s=standby_probe_interval_s or PROBE_INTERVAL_S,
                    takeover_after=standby_takeover_after or TAKEOVER_AFTER,
                    max_handoff_age_s=standby_max_handoff_age_s or MAX_HANDOFF_AGE_S,
                    transport=standby_transport,
                )
            )
        elif cfg.resume:
            handoff = handoff_state.apply(router, handoff_state.load(cfg.state_path))
            if not handoff.applied:
                log.info("no control-plane handoff applied: %s", handoff.why)
                if cfg.engine_contract is not None and cfg.state_path.exists():
                    router.lifecycle.require_restart_wave(f"handoff rejected: {handoff.why}")
        journal.open()
        if controls_fleet(router):
            await check_process_identities(router)
        loop = asyncio.create_task(monitor_loop(router))
        log.info(
            "narwhal up: %d instances, ttft<=%.3gs tpot<=%.3gs, interval %.2gs, "
            "admitting %d at once",
            len(router.monitor.instances),
            cfg.slo.ttft_s,
            cfg.slo.tpot_s,
            cfg.monitor_interval_s,
            router.max_concurrent,
        )
        log.info("journal %s, run %s", journal.path, journal.run)
        try:
            yield
        finally:
            persist_final_state = controls_fleet(router)
            router.standby = True
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop
            if watch is not None:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch
            if lease_watch is not None:
                lease_watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_watch
            probes = list(router._verification_tasks)
            for probe in probes:
                probe.cancel()
            await asyncio.gather(*probes, return_exceptions=True)
            await router.engines.aclose()
            # A standby must retain its polled primary handoff. Recheck the
            # lease after cleanup, which can outlast the ownership window.
            if persist_final_state and (lease is None or lease.valid()):
                with contextlib.suppress(OSError):
                    handoff_state.write(cfg.state_path, handoff_state.snapshot(router))
            if lease is not None:
                lease.release()
            journal.close()

    app = FastAPI(title="narwhal", version=__version__, lifespan=lifespan)
    app.state.router = router
    app.add_middleware(ServingIngress, router=router)

    @app.get("/v1/models", response_model=ModelsOut, summary="The one model this router fronts")
    async def models() -> dict[str, Any]:
        """Return the configured model in OpenAI list format."""
        return {
            "object": "list",
            "data": [{"id": cfg.model, "object": "model", "owned_by": "narwhal"}],
        }

    @app.get("/health", response_model=HealthOut, summary="Liveness, and the configured fleet size")
    async def health() -> dict[str, Any]:
        lease_invalid = not router.standby and router.lease is not None and not router.lease.valid()
        status = (
            "ok"
            if ready(router)
            else (
                "fenced"
                if router.failover_blocked or lease_invalid
                else (
                    "maintenance"
                    if router.lifecycle_blocked
                    else ("standby" if router.standby else "degraded")
                )
            )
        )
        return {
            "status": status,
            "instances": len(router.monitor.instances),
            "available_instances": len(router.scheduler.live_instances()),
        }

    @app.get("/ready", summary="Active-router readiness for external load balancers")
    async def readiness() -> Response:
        content = {
            "status": "ready" if ready(router) else "not_ready",
            "control_ready": control_ready(router),
            "epoch": router.lease_epoch,
            "holder": router.lease_holder,
            "reason": router.failover_blocked
            or router.lifecycle_blocked
            or _monitoring_degraded_reason(router)
            or ("shadowing" if router.standby else "")
            or ("lease expired" if not controls_fleet(router) else "")
            or (
                "engine identity validation pending"
                if not router.lifecycle.identities_ready
                else ""
            )
            or ("no available engines" if not router.scheduler.live_instances() else ""),
        }
        if ready(router):
            return JSONResponse(content=content)
        return JSONResponse(status_code=503, content=content, headers={"retry-after": "1"})

    @app.get("/narwhal/handoff", summary="The live control-plane handoff document")
    async def handoff() -> dict[str, Any]:
        """Return a fresh handoff for resume or standby polling."""
        return handoff_state.snapshot(router)

    @app.get("/metrics", summary="Prometheus counters and latency histograms")
    async def metrics() -> Response:
        return Response(
            content=render(
                router.state(), router.ttft, router.tpot, router.seat, router.queue_wait
            ),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/narwhal/state", response_model=StateOut, summary="The live scheduler picture")
    async def state() -> dict[str, Any]:
        return router.state()

    @app.get("/narwhal/lifecycle", summary="Engine drain and readmission state")
    async def lifecycle() -> dict[str, Any]:
        return router.lifecycle.document()

    @app.post("/narwhal/lifecycle/drain", summary="Remove engines from new placement")
    async def drain(action: DrainIn) -> Response:
        if not controls_fleet(router):
            return _lifecycle_error(router, 503, "this router does not control the fleet")
        engines = action.engines or (sorted(router.monitor.instances) if action.wave else [])
        async with router.lifecycle.lock:
            try:
                router.lifecycle.begin(engines, wave=action.wave, deadline_s=action.deadline_s)
            except LifecycleError as exc:
                return _lifecycle_error(router, 409, str(exc))
            capture = [
                iid for iid in engines if router.lifecycle.records[iid].old_process_start is None
            ]
            starts, failures = await capture_process_identities(
                cfg,
                capture,
                transport=lifecycle_transport,
            )
            for iid in capture:
                router.lifecycle.record_old_identity(iid, starts.get(iid), failures.get(iid, ""))
            _persist_handoff(router)
            if not controls_fleet(router):
                return _lifecycle_error(router, 503, "router control was fenced during drain")
            if failures:
                return _lifecycle_error(
                    router,
                    503,
                    "; ".join(f"{iid}: {detail}" for iid, detail in sorted(failures.items())),
                )
            return JSONResponse(content=router.lifecycle.document())

    @app.post("/narwhal/lifecycle/readmit", summary="Validate and return engines to placement")
    async def readmit(action: ReadmitIn) -> Response:
        if not controls_fleet(router):
            return _lifecycle_error(router, 503, "this router does not control the fleet")
        engines = action.engines or (sorted(router.monitor.instances) if action.wave else [])
        async with router.lifecycle.lock:
            if action.wave:
                expected = {
                    iid
                    for iid, record in router.lifecycle.records.items()
                    if record.wave_id == router.lifecycle.wave_id
                }
                if not router.lifecycle.wave_id or set(engines) != expected:
                    return _lifecycle_error(
                        router,
                        409,
                        "whole-wave readmission must name the active wave's complete engine set",
                    )
            elif router.lifecycle.wave_id:
                return _lifecycle_error(
                    router,
                    409,
                    "an active whole wave must be validated and readmitted as one engine set",
                )
            elif len(engines) != 1:
                return _lifecycle_error(router, 409, "readmission must name exactly one engine")
            try:
                router.lifecycle.mark_validating(engines)
            except LifecycleError as exc:
                return _lifecycle_error(router, 409, str(exc))
            _persist_handoff(router)
            try:
                outcome = await validate_readmission(
                    router,
                    engines,
                    wave=action.wave,
                    transport=lifecycle_transport,
                )
            except Exception as exc:
                log.exception("engine readmission validation failed")
                outcome = ValidationOutcome()
                for iid in engines:
                    outcome.fail(iid, f"validation raised {type(exc).__name__}: {exc}")
            if not controls_fleet(router):
                for iid in engines:
                    outcome.fail(iid, "router control was fenced during validation")
                router.lifecycle.validation_failed(outcome)
                _persist_handoff(router)
                return _lifecycle_error(
                    router,
                    503,
                    "router control was fenced during validation",
                )
            if outcome.passed and set(outcome.starts) == set(engines):
                router.lifecycle.readmitted(engines, outcome)
                _persist_handoff(router)
                return JSONResponse(content=router.lifecycle.document())
            router.lifecycle.validation_failed(outcome)
            _persist_handoff(router)
            return _lifecycle_error(router, 409, "readmission validation failed")

    @app.post("/v1/completions", summary="Completions, body passed to the engine unchanged")
    async def completions(request: HTTPRequest) -> Response:
        if not ready(router):
            return _not_ready_refusal(router)
        body = await _completion_body(router, request)
        if not isinstance(body, dict):
            return body
        return await serve_connected(router, request, body, _headers(request))

    @app.post("/v1/chat/completions", summary="Chat, body passed to the engine unchanged")
    async def chat_completions(request: HTTPRequest) -> Response:
        if not ready(router):
            return _not_ready_refusal(router)
        body = await _completion_body(router, request)
        if not isinstance(body, dict):
            return body
        return await serve_connected(router, request, body, _headers(request))

    return app


async def _completion_body(
    router: NarwhalRouter, request: HTTPRequest
) -> dict[str, Any] | JSONResponse:
    """Parse a bounded body before reserving active or engine capacity."""
    try:
        raw = json.loads(await bounded_body(request, router.cfg.serving.max_request_bytes))
    except BodyTooLarge:
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "message": "request body exceeds max_request_bytes",
                    "type": "request_too_large",
                }
            },
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _invalid_refusal(router, "the request body is not valid JSON", None)
    problem = completion_body_error(raw)
    if problem is not None:
        return _invalid_refusal(router, *problem)
    return raw


def _invalid_refusal(router: NarwhalRouter, message: str, param: str | None) -> JSONResponse:
    """Return the stable malformed-request 400; ingress records its outcome."""
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": param,
                "code": None,
            }
        },
    )


def _monitoring_degraded_reason(router: NarwhalRouter) -> str:
    """Render the degraded streak's first failure as `stage class`, endpoint-free."""
    degraded = router.monitoring_degraded
    if not degraded:
        return ""
    klass, _, stage = degraded.partition(":")
    return f"monitoring degraded: {stage} {klass}"


def _not_ready_refusal(router: NarwhalRouter) -> Response:
    """Return a retryable refusal while control or backends are unavailable."""
    backend_unavailable = (
        control_ready(router)
        and not router.lifecycle_blocked
        and not router.scheduler.live_instances()
    )
    reason = (
        router.failover_blocked
        or router.lifecycle_blocked
        or _monitoring_degraded_reason(router)
        or ("engine identity validation pending" if not router.lifecycle.identities_ready else "")
        or ("no available engines" if backend_unavailable else "")
        or "standby: the primary router is serving"
    )
    code = "backend_unavailable" if backend_unavailable else "standby"
    return JSONResponse(
        content={"error": {"message": reason, "type": code, "code": code}},
        status_code=503,
        headers={"retry-after": "1"},
    )


def _lifecycle_error(router: NarwhalRouter, status_code: int, message: str) -> Response:
    return JSONResponse(
        status_code=status_code,
        content=router.lifecycle.document(error=message),
    )


def _persist_handoff(router: NarwhalRouter) -> None:
    with contextlib.suppress(OSError):
        handoff_state.write(router.cfg.state_path, handoff_state.snapshot(router))


def _headers(request: HTTPRequest) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_HEADERS}
