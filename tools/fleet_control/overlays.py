"""Configuration overlays, cold fleet restarts and baseline restores, each checked for readiness.

An overlay is a partial fleet document merged onto the session's current fleet configuration
as a JSON merge patch (RFC 7386): an object merges key by key, `null` removes a key so the
field returns to its default, and any other value, arrays included, replaces the current one.
An overlay may change only the router policy sections in `OVERLAY_SECTIONS`, plus `_`
annotation keys. The merged document must pass the fleet configuration loader that
`narwhal config validate` runs before any hook touches the fleet.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from narwhal.config import FleetConfig
from narwhal.contracts import canonical_digest

from .app import API, action_reply
from .config import ConfigError, Hook
from .hooks import HookResult
from .records import Action, Session, write_private
from .service import ActionError, ControlService, refused

ROUTER_RESTART_HOOK = "router_restart"
COLD_RESTART_HOOK = "cold_restart"
# The hook environment variable naming the fleet file the restarted processes must load.
FLEET_ENV = "NARWHAL_CONTROL_FLEET"
# Router policy read at startup. The other sections describe the engines' launch, model and
# measured profiles, which a router restart cannot change.
OVERLAY_SECTIONS = ("slo", "controller", "serving", "recovery")
OVERLAYS = "overlays"
READY_PATH = "/ready"
READY_POLL_S = 1.0
READY_PROBE_TIMEOUT_S = 5.0


def merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Return `base` with `overlay` applied as a JSON merge patch, leaving both unchanged."""
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
    """Return every reason the overlay's shape is refused before it is merged."""
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
    """Apply overlays through the router-restart hook, restart the fleet and restore the baseline.

    Each operation is an exclusive action: the service refuses every other action until the
    hook finishes and the router answers `GET /ready` with 200, or for at most `router.timeout_s`.
    """

    def __init__(
        self,
        service: ControlService,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_s: float = READY_POLL_S,
    ) -> None:
        self.service = service
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
        # The router restarted with this file, so it governs the fleet even if readiness fails.
        session.apply_configuration(self.service.stamp(), "overlay", merged, fleet=fleet)
        effect["readiness"] = await self._ready(effect)
        return effect

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
        """Run the restore hook against the recorded baseline, then wait for the router."""

        async def restore(session: Session | None) -> Mapping[str, Any]:
            assert session is not None
            # The nested restore is recorded as its own action, as when a session ends.
            action = await self.service._perform(
                "baseline.restore", {}, self.service._restore, True
            )
            current = session.configurations[-1]
            effect: dict[str, Any] = {
                "restore_seq": action.seq,
                "fleet": current["fleet"],
                "digest": current["digest"],
            }
            effect["readiness"] = await self._ready(effect)
            return effect

        return await self.service.act("config.restore", {}, restore, exclusive=True)

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

    @routes.post("/overlay")
    async def apply_overlay(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            overlay = json.loads(raw, parse_constant=reject_constant) if raw else None
        except ValueError:
            overlay = None
        if not isinstance(overlay, dict):
            return JSONResponse({"detail": "the overlay must be a JSON object"}, 422)
        return action_reply(await overlays.apply(overlay))

    @routes.post("/cold-restart")
    async def cold_restart() -> JSONResponse:
        return action_reply(await overlays.cold_restart())

    @routes.post("/restore")
    async def restore() -> JSONResponse:
        return action_reply(await overlays.restore())

    return routes
