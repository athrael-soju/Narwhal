"""HTTP routes that list, join and remove the router's engines."""

from __future__ import annotations

import contextlib
import logging
import math
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response

from ..config.loading import read_engine_spec
from ..config.serialization import engine_row
from ..contracts import MEMBERSHIP, versioned
from ..runtime import state as handoff_state
from ..runtime.lifecycle.join import JoinOutcome, join_engine
from ..runtime.lifecycle.leave import begin_leave
from ..runtime.lifecycle.records import LifecycleError
from ..runtime.standby import controls_fleet
from .schemas import EngineSpecIn, MembershipOut

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter

log = logging.getLogger("narwhal.app")

NOT_CONTROLLING = "this router does not control the fleet"


def membership_document(
    router: NarwhalRouter, *, error: str = "", join: JoinOutcome | None = None
) -> dict[str, Any]:
    """Return the versioned membership document."""
    membership = router.membership
    return versioned(
        MEMBERSHIP,
        {
            "generation": membership.generation,
            "engines": [engine_row(spec) for spec in membership],
            "leaving": sorted(
                iid for iid, record in router.lifecycle.records.items() if record.leaving
            ),
            "error": error,
            "join": (
                None
                if join is None
                else {
                    "iid": join.iid,
                    "joined": join.joined,
                    "checks": list(join.checks),
                    "failures": list(join.failures),
                }
            ),
        },
    )


def _reply(
    router: NarwhalRouter,
    status_code: int,
    *,
    error: str = "",
    join: JoinOutcome | None = None,
) -> Response:
    return JSONResponse(
        status_code=status_code,
        content=membership_document(router, error=error, join=join),
    )


def _persist(router: NarwhalRouter) -> None:
    with contextlib.suppress(OSError):
        handoff_state.write(router.cfg.state_path, handoff_state.snapshot(router))


def add_engine_routes(app: FastAPI, router: NarwhalRouter) -> None:
    """Register `GET` and `POST /narwhal/engines` and `DELETE /narwhal/engines/{iid}`."""

    @app.get(
        "/narwhal/engines",
        response_model=MembershipOut,
        summary="The engine membership snapshot and its generation",
    )
    async def engines() -> dict[str, Any]:
        return membership_document(router)

    @app.post(
        "/narwhal/engines",
        response_model=MembershipOut,
        summary="Validate an engine and add it to the fleet",
        responses={409: {"model": MembershipOut}, 422: {}, 503: {"model": MembershipOut}},
    )
    async def join(spec: EngineSpecIn) -> Response:
        if not controls_fleet(router):
            return _reply(router, 503, error=NOT_CONTROLLING)
        try:
            candidate = read_engine_spec(spec.model_dump(), "engine")
        except ValueError as exc:
            return _reply(router, 422, error=str(exc))
        async with router.lifecycle.lock:
            outcome = await join_engine(router, candidate, transport=router.lifecycle_transport)
            _persist(router)
        if outcome.joined:
            return _reply(router, 200, join=outcome)
        status = 503 if not controls_fleet(router) else 409
        return _reply(router, status, error=outcome.error, join=outcome)

    @app.delete(
        "/narwhal/engines/{iid}",
        response_model=MembershipOut,
        summary="Drain an engine and remove it from the fleet",
        responses={
            202: {"model": MembershipOut},
            404: {"model": MembershipOut},
            409: {"model": MembershipOut},
            503: {"model": MembershipOut},
        },
    )
    async def leave(iid: str, deadline_s: float = Query(300.0)) -> Response:
        if not controls_fleet(router):
            return _reply(router, 503, error=NOT_CONTROLLING)
        if iid not in router.membership:
            return _reply(router, 404, error=f"unknown engine: {iid}")
        if not math.isfinite(deadline_s) or deadline_s <= 0:
            return _reply(router, 409, error="deadline_s must be positive")
        async with router.lifecycle.lock:
            try:
                record = await begin_leave(
                    router, iid, deadline_s=deadline_s, transport=router.lifecycle_transport
                )
            except LifecycleError as exc:
                return _reply(router, 409, error=str(exc))
            _persist(router)
        if not controls_fleet(router):
            return _reply(router, 503, error="router control was fenced during the leave")
        if iid not in router.membership:
            return _reply(router, 200)
        if record.state == "blocked":
            return _reply(router, 503, error=record.error)
        return _reply(router, 202)
