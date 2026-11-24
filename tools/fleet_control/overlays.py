"""Configuration overlays, cold restarts and baseline restores."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from narwhal.config import FleetConfig
from narwhal.contracts import canonical_digest

from .app import API, action_reply
from .config import ConfigError, Hook
from .hooks import HookResult
from .records import Action, Session, write_private
from .service import UNDO, ActionError, ControlService, refused, session_changes

if TYPE_CHECKING:
    from .engines import EngineActions

ROUTER_RESTART_HOOK = "router_restart"
COLD_RESTART_HOOK = "cold_restart"
# Hook environment variable holding the fleet file to load.
FLEET_ENV = "NARWHAL_CONTROL_FLEET"
# Fleet sections a router restart can change.
OVERLAY_SECTIONS = ("slo", "controller", "serving", "recovery")
OVERLAYS = "overlays"
READY_PATH = "/ready"
READY_POLL_S = 1.0
READY_PROBE_TIMEOUT_S = 5.0
# Router lifecycle states a readmit applies to.
READMITTABLE = frozenset({"draining", "drained", "deadline_exceeded", "blocked"})


def merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Return `base` with `overlay` applied as a JSON merge patch (RFC 7386)."""
    merged = dict(base)
    for key, value in overlay.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, Mapping):
            current = merged.get(key)
            merged[key] = merge(current if isinstance(current, Mapping) else {}, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def overlay_problems(overlay: Mapping[str, Any]) -> list[str]:
    """Return the problems in the overlay's shape."""
    if not overlay:
        return ["the overlay changes no section"]
    allowed = ", ".join(OVERLAY_SECTIONS)
    return [
        f"an overlay may not change {key!r}; it may change only {allowed}"
        for key in overlay
        if key not in OVERLAY_SECTIONS and not key.startswith("_")
    ]


def fleet_problems(path: Path) -> list[str]:
    """Return every error the fleet configuration loader reports for the file at `path`."""
    try:
        FleetConfig.load(path)
    except (TypeError, ValueError) as exc:
        message = str(exc)
        prefix = str(path)
        if message.startswith(prefix + ": "):
            message = message[len(prefix) + 2 :]
        elif message.startswith(prefix):
            message = "the fleet" + message[len(prefix) :]
        # The loader joins its problems with "; ".
        return [problem for problem in message.split("; ") if problem]
    return []


def reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant {name}")


class Overlays:
    """Apply overlays, cold restarts and baseline restores as exclusive actions."""

    def __init__(
        self,
        service: ControlService,
        *,
        engines: EngineActions | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_s: float = READY_POLL_S,
    ) -> None:
        self.service = service
        self.engines = engines
        self._transport = transport
        self._poll_s = poll_s

    async def apply(self, overlay: Mapping[str, Any]) -> Action:
        """Validate the overlay, write the merged fleet file and restart the router with it."""

        async def apply(session: Session | None) -> Mapping[str, Any]:
            assert session is not None
            return await self._apply(session, overlay)

        return await self.service.act("config.overlay", {"overlay": overlay}, apply, exclusive=True)

    async def _apply(self, session: Session, overlay: Mapping[str, Any]) -> Mapping[str, Any]:
        hook = self._hook(ROUTER_RESTART_HOOK)
        current = session.configurations[-1]
        merged = merge(current["document"], overlay)
        directory = session.directory / OVERLAYS
        directory.mkdir(mode=0o700, exist_ok=True)
        candidate = directory / "candidate.json"
        problems = overlay_problems(overlay)
        if not problems:
            write_private(candidate, merged)
            problems = fleet_problems(candidate)
        if problems:
            candidate.unlink(missing_ok=True)
            raise ActionError(
                "the overlay is invalid: " + "; ".join(problems),
                status=422,
                outcome="refused",
                result={"errors": problems},
            )
        index = len(list(directory.glob("*-fleet.json"))) + 1
        fleet = f"{OVERLAYS}/{index:03d}-fleet.json"
        os.replace(candidate, session.directory / fleet)
        effect: dict[str, Any] = {
            "fleet": fleet,
            "digest": canonical_digest(merged),
            "base_digest": current["digest"],
        }
        effect["hook"] = await self._run(hook, session, fleet, effect)
        # The router now runs this file.
        session.apply_configuration(self.service.stamp(), "overlay", merged, fleet=fleet)
        effect["readiness"] = await self._ready(effect)
        return effect

    def check(self, overlay: Mapping[str, Any]) -> dict[str, Any]:
        """Return the overlay's problems and merged document."""
        session = self.service.session
        if session is None:
            raise refused("no session is active; start one with POST /api/session")
        current = session.configurations[-1]
        merged = merge(current["document"], overlay)
        problems = overlay_problems(overlay)
        if not problems:
            directory = session.directory / OVERLAYS
            directory.mkdir(mode=0o700, exist_ok=True)
            # Resolve relative paths beside the applied overlays.
            descriptor, name = tempfile.mkstemp(dir=directory, prefix=".check-", suffix=".json")
            os.close(descriptor)
            candidate = Path(name)
            try:
                write_private(candidate, merged)
                problems = fleet_problems(candidate)
            finally:
                candidate.unlink(missing_ok=True)
        return {
            "errors": problems,
            "base_digest": current["digest"],
            "digest": None if problems else canonical_digest(merged),
            "document": merged,
        }

    async def cold_restart(self) -> Action:
        """Restart every engine and the router from the session's current fleet file."""

        async def restart(session: Session | None) -> Mapping[str, Any]:
            assert session is not None
            hook = self._hook(COLD_RESTART_HOOK)
            current = session.configurations[-1]
            effect: dict[str, Any] = {"fleet": current["fleet"], "digest": current["digest"]}
            effect["hook"] = await self._run(hook, session, current["fleet"], effect)
            effect["readiness"] = await self._ready(effect)
            return effect

        return await self.service.act("config.cold_restart", {}, restart, exclusive=True)

    async def restore(self) -> Action:
        """Undo the session's configuration change, then each engine and membership change.

        With a router that serves its membership, membership changes come from comparing the
        live membership with the baseline, read again after the configuration restart.
        """

        async def restore(session: Session | None) -> Mapping[str, Any]:
            assert session is not None
            steps: list[dict[str, Any]] = []
            effect: dict[str, Any] = {"steps": steps}
            # The session record alone says whether the configuration changed, so a router that
            # does not answer can still restart with the baseline.
            recorded = session_changes(session)
            # A restore that fails before the membership read keeps the recorded changes.
            effect["changes"] = recorded
            configuration = recorded["configuration"]
            if configuration:
                baseline = session.configurations[0]
                hook = self._hook(ROUTER_RESTART_HOOK)
                effect["hook"] = await self._run(hook, session, baseline["fleet"], effect)
                session.apply_configuration(self.service.stamp(), "baseline", baseline["document"])
                effect["readiness"] = await self._ready(effect)
            members = await self._members(effect)
            changes = {**session_changes(session, members), "configuration": configuration}
            effect["changes"] = changes
            undo = [(iid, change) for iid, each in changes["engines"].items() for change in each]
            for iid, change in undo:
                steps.append(await self._undo(iid, UNDO[change], effect))
            # Engines that left return before engines that joined leave, so capacity holds.
            for iid, spec in changes.get("left", {}).items():
                await self._return(iid, spec, effect, steps)
            for iid in changes.get("joined", []):
                steps.append(await self._remove(iid, effect))  # noqa: PERF401 - each step is kept as it completes
            return effect

        return await self.service.act("config.restore", {}, restore, exclusive=True)

    async def _members(self, effect: Mapping[str, Any]) -> dict[str, Mapping[str, Any]] | None:
        """Return the router's member engine entries, or None without a membership route."""
        if self.engines is None:
            return None
        try:
            async with self.engines._client() as client:
                current = await self.engines.membership(client)
        except ActionError as exc:
            raise ActionError(str(exc), status=exc.status, result=effect) from exc
        return None if current is None else current["engines"]

    async def _undo(self, iid: str, action: str, effect: Mapping[str, Any]) -> dict[str, Any]:
        if self.engines is None:
            raise ActionError(f"engine actions are unavailable to {action} {iid}", result=effect)
        if action == "readmit":
            async with self.engines._client() as client:
                state = await self.engines.engine_state(client, iid)
            if "error" in state:
                raise ActionError(f"readmit {iid} failed: {state['error']}", result=effect)
            lifecycle = state.get("lifecycle") or {}
            if not state.get("draining") and lifecycle.get("state") not in READMITTABLE:
                return {"engine": iid, "action": action, "seq": None}
        try:
            done = await self.engines.within(iid, action)
        except ActionError as exc:
            raise ActionError(
                f"{action} {iid} failed: {exc}", status=exc.status, result=effect
            ) from exc
        return {"engine": iid, "action": action, "seq": done.seq}

    async def _return(
        self,
        iid: str,
        spec: Mapping[str, Any],
        effect: Mapping[str, Any],
        steps: list[dict[str, Any]],
    ) -> None:
        """Start an engine that left, when an engine_start hook is configured, and join it.

        Each completed step is added to `steps` at once, so a failed restore records it.
        """
        engines = self._actions(iid, "join", effect)
        current = await self._members(effect)
        if current is not None and iid in current:
            steps.append({"engine": iid, "action": "join", "seq": None})
            return
        try:
            if "engine_start" in self.service.config.hooks:
                started = await engines.within(iid, "start", spec)
                steps.append({"engine": iid, "action": "start", "seq": started.seq})
            done = await engines.join_within(spec)
        except ActionError as exc:
            raise ActionError(
                f"return of {iid} failed: {exc}", status=exc.status, result=effect
            ) from exc
        steps.append({"engine": iid, "action": "join", "seq": done.seq})

    async def _remove(self, iid: str, effect: Mapping[str, Any]) -> dict[str, Any]:
        """Remove an engine that joined during the session from the router's membership."""
        engines = self._actions(iid, "leave", effect)
        current = await self._members(effect)
        if current is not None and iid not in current:
            return {"engine": iid, "action": "leave", "seq": None}
        try:
            done = await engines.leave_within(iid)
        except ActionError as exc:
            raise ActionError(
                f"leave {iid} failed: {exc}", status=exc.status, result=effect
            ) from exc
        return {"engine": iid, "action": "leave", "seq": done.seq}

    def _actions(self, iid: str, action: str, effect: Mapping[str, Any]) -> EngineActions:
        if self.engines is None:
            raise ActionError(f"engine actions are unavailable to {action} {iid}", result=effect)
        return self.engines

    def _hook(self, name: str) -> Hook:
        try:
            return self.service.config.hook(name)
        except ConfigError as exc:
            raise refused(str(exc), 501) from exc

    async def _run(
        self, hook: Hook, session: Session, fleet: str, effect: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        path = (session.directory / fleet).resolve()
        try:
            result = await self.service.run_hook(hook, session, {FLEET_ENV: str(path)})
        except OSError as exc:
            raise ActionError(f"{hook.name} hook could not start: {exc}", result=effect) from exc
        document = result.document(session.directory)
        if not result.ok:
            raise ActionError(
                f"{hook.name} hook {failure(hook, result)}", result={**effect, "hook": document}
            )
        return document

    async def _ready(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        readiness = await self.wait_ready()
        if not readiness["ready"]:
            raise ActionError(
                f"router was not ready after {self.service.config.router.timeout_s:g}s",
                status=504,
                result={**effect, "readiness": readiness},
            )
        return readiness

    async def wait_ready(self) -> dict[str, Any]:
        """Poll the router's readiness route until it answers 200 or `router.timeout_s` passes."""
        router = self.service.config.router
        started = time.monotonic()
        deadline = started + router.timeout_s
        attempts = 0
        async with httpx.AsyncClient(
            base_url=router.url, transport=self._transport, trust_env=False
        ) as client:
            while True:
                attempts += 1
                remaining = deadline - time.monotonic()
                status, reason = await probe(client, min(READY_PROBE_TIMEOUT_S, remaining))
                remaining = deadline - time.monotonic()
                if status == 200 or remaining <= 0:
                    break
                await asyncio.sleep(min(self._poll_s, remaining))
        return {
            "path": READY_PATH,
            "ready": status == 200,
            "attempts": attempts,
            "waited_s": round(time.monotonic() - started, 3),
            "status_code": status,
            "reason": reason,
        }


async def probe(client: httpx.AsyncClient, timeout_s: float) -> tuple[int | None, str]:
    """Return the readiness status code and the router's stated reason."""
    try:
        response = await client.get(READY_PATH, timeout=max(timeout_s, 0.05))
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        body = response.json()
    except ValueError:
        return response.status_code, response.text[:200]
    if not isinstance(body, dict):
        return response.status_code, ""
    return response.status_code, str(body.get("reason") or body.get("status") or "")


def failure(hook: Hook, result: HookResult) -> str:
    """Describe why a hook did not succeed."""
    if result.timed_out:
        return f"timed out after {hook.timeout_s:g}s"
    return f"exited {result.exit_code}"


def overlay_routes(overlays: Overlays) -> APIRouter:
    """Return the configuration overlay, cold restart and baseline restore routes."""
    routes = APIRouter(prefix=f"{API}/config")

    async def read_overlay(request: Request) -> dict[str, Any] | None:
        raw = await request.body()
        try:
            overlay = json.loads(raw, parse_constant=reject_constant) if raw else None
        except ValueError:
            return None
        return overlay if isinstance(overlay, dict) else None

    @routes.post("/overlay")
    async def apply_overlay(request: Request) -> JSONResponse:
        overlay = await read_overlay(request)
        if overlay is None:
            return JSONResponse({"detail": "the overlay must be a JSON object"}, 422)
        return action_reply(await overlays.apply(overlay))

    @routes.post("/overlay/check")
    async def check_overlay(request: Request) -> JSONResponse:
        overlay = await read_overlay(request)
        if overlay is None:
            return JSONResponse({"detail": "the overlay must be a JSON object"}, 422)
        return JSONResponse(overlays.check(overlay))

    @routes.post("/cold-restart")
    async def cold_restart() -> JSONResponse:
        return action_reply(await overlays.cold_restart())

    @routes.post("/restore")
    async def restore() -> JSONResponse:
        return action_reply(await overlays.restore())

    return routes
