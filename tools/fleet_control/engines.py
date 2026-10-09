"""Engine actions: hook-driven pause, resume, stop and start, and router drain and readmit.

Each action names one engine from the session's baseline fleet configuration and records
that engine's router state before and after the action.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .app import API, action_reply
from .config import Hook
from .hooks import HookResult
from .records import Action, Session
from .service import ActionError, ControlService, refused

# The private configuration names the deployment's command for each hook action.
HOOK_ACTIONS = {
    "pause": "engine_pause",
    "resume": "engine_resume",
    "stop": "engine_stop",
    "start": "engine_start",
}
LIFECYCLE_ACTIONS = {
    "drain": "/narwhal/lifecycle/drain",
    "readmit": "/narwhal/lifecycle/readmit",
}
ACTIONS = (*HOOK_ACTIONS, *LIFECYCLE_ACTIONS)
# Optional request fields each action accepts beyond the engine it names.
ACTION_PARAMS: dict[str, frozenset[str]] = {"drain": frozenset({"deadline_s"})}
STATE_PATH = "/narwhal/state"
# State reads use this shorter limit so a hung router cannot hold an action for router.timeout_s.
STATE_TIMEOUT_S = 10.0


class StateUnavailable(Exception):
    """The router's state document could not be read."""


class EngineActions:
    """Run engine actions through the service so each one is recorded."""

    def __init__(
        self, service: ControlService, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.service = service
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        router = self.service.config.router
        return httpx.AsyncClient(
            base_url=router.url, timeout=router.timeout_s, transport=self._transport
        )

    async def act(self, iid: str, action: str, params: Mapping[str, Any]) -> Action:
        """Run `action` on engine `iid` and record it with the engine's state around it."""

        async def operate(session: Session | None) -> Mapping[str, Any]:
            assert session is not None
            engine = baseline_engine(session, iid)
            _check_params(action, params)
            hook = self._hook(action) if action in HOOK_ACTIONS else None
            async with self._client() as client:
                before = await self.engine_state(client, iid)
                try:
                    if hook is not None:
                        effect = await self._run_hook(hook, session, engine, action)
                    else:
                        effect = await self._lifecycle(client, iid, action, params)
                except ActionError as exc:
                    after = await self.engine_state(client, iid)
                    exc.result = {
                        "engine": iid,
                        "before": before,
                        **(exc.result or {}),
                        "after": after,
                    }
                    raise
                after = await self.engine_state(client, iid)
            return {"engine": iid, "before": before, **effect, "after": after}

        return await self.service.act(f"engine.{action}", {"engine": iid, **params}, operate)

    def _hook(self, action: str) -> Hook:
        name = HOOK_ACTIONS[action]
        hook = self.service.config.hooks.get(name)
        if hook is None:
            raise refused(f"no {name!r} hook is configured", 501)
        return hook

    async def _run_hook(
        self, hook: Hook, session: Session, engine: Mapping[str, Any], action: str
    ) -> dict[str, Any]:
        env = {
            "NARWHAL_CONTROL_ACTION": action,
            "NARWHAL_CONTROL_ENGINE": str(engine["iid"]),
            "NARWHAL_CONTROL_ENGINE_URL": str(engine.get("url", "")),
        }
        try:
            result = await self.service.run_hook(hook, session, env)
        except OSError as exc:
            raise ActionError(f"{hook.name} hook could not start: {exc}") from exc
        effect = {"hook": result.document(session.directory)}
        if not result.ok:
            raise ActionError(f"{hook.name} hook {hook_failure(hook, result)}", result=effect)
        return effect

    async def _lifecycle(
        self, client: httpx.AsyncClient, iid: str, action: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        path = LIFECYCLE_ACTIONS[action]
        body = {"engines": [iid], **params}
        try:
            response = await client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise ActionError(f"router {path} failed: {type(exc).__name__}: {exc}") from exc
        error = _lifecycle_error(response)
        effect = {
            "router": {"path": path, "body": body, "status": response.status_code, "error": error}
        }
        if response.status_code != 200:
            raise ActionError(
                f"router refused {action} with HTTP {response.status_code}: {error}", result=effect
            )
        return effect

    async def engine_state(self, client: httpx.AsyncClient, iid: str) -> dict[str, Any]:
        """Return the router's view of one engine, or the reason it could not be read.

        An unreadable state does not stop the action; the record carries the reason instead.
        """
        try:
            return (await self._slices(client, [iid]))[iid]
        except StateUnavailable as exc:
            return {"error": str(exc)}

    async def fleet_state(self, iids: list[str]) -> dict[str, Any]:
        """Return the router's view of each named engine."""
        async with self._client() as client:
            try:
                return await self._slices(client, iids)
            except StateUnavailable as exc:
                raise ActionError(str(exc)) from exc

    async def _slices(self, client: httpx.AsyncClient, iids: list[str]) -> dict[str, Any]:
        timeout = min(STATE_TIMEOUT_S, self.service.config.router.timeout_s)
        try:
            response = await client.get(STATE_PATH, timeout=timeout)
            response.raise_for_status()
            state = response.json()
            return {iid: engine_slice(state, iid) for iid in iids}
        except (httpx.HTTPError, ValueError, LookupError, TypeError, AttributeError) as exc:
            raise StateUnavailable(
                f"router state unavailable: {type(exc).__name__}: {exc}"
            ) from exc


def baseline_engines(session: Session) -> dict[str, Mapping[str, Any]]:
    """Return the engines of the session's baseline fleet configuration, keyed by ID."""
    return {str(engine["iid"]): engine for engine in session.baseline["engines"]}


def configured_engines(fleet: Path) -> list[str]:
    """Return the engine IDs of the configured baseline fleet file, for reads outside a session."""
    document = json.loads(fleet.read_text(encoding="utf-8"))
    return [str(engine["iid"]) for engine in document["engines"]]


def baseline_engine(session: Session, iid: str) -> Mapping[str, Any]:
    """Return one baseline engine, refusing an ID the baseline does not configure."""
    engine = baseline_engines(session).get(iid)
    if engine is None:
        raise refused(f"engine {iid!r} is not in the baseline fleet configuration", 404)
    return engine


def engine_slice(state: Mapping[str, Any], iid: str) -> dict[str, Any]:
    """Return one engine's part of a `narwhal.state` document.

    The slice holds the engine's pool, its availability flags, its resident work, its breaker
    streaks, its lifecycle record and process start, and the router's readiness and wave.
    """
    pools = state["pools"]
    lifecycle = state["lifecycle"]
    return {
        "known": iid in state["resident"],
        "role": next((role for role in ("prefill", "decode") if iid in pools[role]), None),
        "ejected": iid in state["ejected"],
        "draining": iid in state.get("draining", []),
        "quarantined": iid in state.get("quarantined", []),
        "probation": iid in state.get("probation", []),
        "pinned": iid in state.get("pinned", []),
        "resident": state["resident"].get(iid),
        "breaker": state.get("breaker", {}).get("failures", {}).get(iid),
        "lifecycle": lifecycle["engines"].get(iid),
        "process_start": lifecycle.get("process_starts", {}).get(iid),
        "router": lifecycle["router"],
        "wave": lifecycle["wave"],
    }


def hook_failure(hook: Hook, result: HookResult) -> str:
    """Describe why a hook run did not succeed."""
    if result.timed_out:
        return f"timed out after {hook.timeout_s:g}s"
    return f"exited {result.exit_code}"


def _check_params(action: str, params: Mapping[str, Any]) -> None:
    unknown = sorted(set(params) - ACTION_PARAMS.get(action, frozenset()))
    if unknown:
        raise refused(f"{action} does not accept {', '.join(unknown)}", 422)
    deadline = params.get("deadline_s")
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, int | float)
        or not math.isfinite(deadline)
        or deadline <= 0
    ):
        raise refused("deadline_s must be a positive number of seconds", 422)


def _lifecycle_error(response: httpx.Response) -> str:
    try:
        document = response.json()
    except ValueError:
        return response.text[:500]
    error = document.get("error") if isinstance(document, dict) else None
    return error if isinstance(error, str) else ""


def engine_routes(actions: EngineActions) -> APIRouter:
    """Return the engine state route and one POST route per engine action."""
    routes = APIRouter(prefix=f"{API}/engines")
    service = actions.service

    @routes.get("")
    async def engines() -> JSONResponse:
        # Outside a session, the engines come from the configured baseline fleet file.
        if service.session is not None:
            iids = list(baseline_engines(service.session))
        else:
            try:
                iids = configured_engines(service.config.fleet)
            except (OSError, ValueError, LookupError, TypeError) as exc:
                detail = f"baseline fleet configuration is unreadable: {exc}"
                return JSONResponse({"detail": detail}, status_code=500)
        return JSONResponse({"engines": await actions.fleet_state(iids)})

    def add(action: str) -> None:
        async def run(iid: str, request: Request) -> JSONResponse:
            raw = await request.body()
            try:
                params = json.loads(raw) if raw else {}
            except ValueError:
                params = None
            if not isinstance(params, dict):
                return JSONResponse({"detail": f"{action} parameters must be a JSON object"}, 422)
            return action_reply(await actions.act(iid, action, params))

        routes.add_api_route(f"/{{iid}}/{action}", run, methods=["POST"], name=f"engine_{action}")

    for action in ACTIONS:
        add(action)
    return routes
