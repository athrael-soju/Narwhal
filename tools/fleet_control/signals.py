"""Fleet signals for the console status strip and the service's Prometheus metrics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .app import API
from .engines import Availability, EngineActions
from .overlays import READY_PATH, READY_PROBE_TIMEOUT_S, probe
from .records import Action
from .service import ControlService

ALERTS_QUERY = 'ALERTS{alertname=~"Narwhal.+",alertstate="firing",severity!="info"}'
PROMETHEUS_TIMEOUT_S = 5.0
METRICS_PATH = "/metrics"
METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_ALERT_LABELS = frozenset({"__name__", "alertname", "alertstate", "job", "instance"})
_UNMARKED = frozenset({"job.start", "job.stop", "job.complete", "session.start", "session.end"})
_SHORT_NAMES = {
    "config.overlay": "overlay",
    "config.cold_restart": "cold restart",
    "config.restore": "restore",
    "session.start": "session start",
    "session.end": "session end",
}


# Actions that record other actions as their steps.
_COMPOSITE = frozenset({"config.restore"})


def steps(action: Action) -> list[Mapping[str, Any]]:
    """Return the recorded actions an action ran as its steps, in order."""
    result = action.result if isinstance(action.result, Mapping) else {}
    found = result.get("steps")
    return [
        step
        for step in (found if isinstance(found, list) else [])
        if isinstance(step, Mapping) and step.get("seq") is not None
    ]


def short_name(action: Action) -> str:
    """Return the action's name as the console and the dashboard annotations show it."""
    if action.name.startswith("engine."):
        return action.name.removeprefix("engine.")
    return _SHORT_NAMES.get(action.name, action.name)


def action_target(action: Action) -> str:
    """Return what the action changed: an engine, the overlay's sections or nothing."""
    params = action.params
    if "engine" in params:
        return str(params["engine"])
    overlay = params.get("overlay")
    if isinstance(overlay, dict):
        return ",".join(sorted(overlay))
    return ""


def _label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _sample(name: str, labels: Mapping[str, object], value: float | int) -> str:
    inner = ",".join(f'{key}="{_label(item)}"' for key, item in labels.items())
    return f"{name}{{{inner}}} {value}" if inner else f"{name} {value}"


def _milliseconds(stamp: str) -> int:
    return round(datetime.fromisoformat(stamp).timestamp() * 1000)


def metrics(service: ControlService, available: Availability | None = None) -> str:
    """Return the service's metrics in the Prometheus text format.

    `available` lists the baseline engines outside the router's membership.
    """
    session = service.session
    lines = [
        "# HELP narwhal_control_session_active Whether an operator session is active.",
        "# TYPE narwhal_control_session_active gauge",
        _sample("narwhal_control_session_active", {}, int(session is not None)),
        "# HELP narwhal_control_action_started_ms Start of each engine action, overlay, cold "
        "restart and restore that ran in the active session, in Unix milliseconds.",
        "# TYPE narwhal_control_action_started_ms gauge",
    ]
    actions = [] if session is None else session.actions
    # A composite action is one marker; its steps get none.
    nested = {step["seq"] for action in actions for step in steps(action)}
    status = service.status()
    running = status["in_progress"] in _COMPOSITE and status["in_progress_since"]
    for action in actions:
        if action.outcome == "refused" or action.name in _UNMARKED or action.seq in nested:
            continue
        if running and _milliseconds(action.started_at) >= _milliseconds(running):
            # A step of the composite action still running.
            continue
        target = action_target(action)
        title = f"{short_name(action)} {target}".strip()
        if parts := steps(action):
            title += ": " + ", ".join(
                f"{step.get('action')} {step.get('engine')}" for step in parts
            )
        labels = {
            "session": session.id if session is not None else "",
            "seq": action.seq,
            "action": action.name,
            "target": target,
            "outcome": action.outcome,
            "title": title,
        }
        lines.append(
            _sample("narwhal_control_action_started_ms", labels, _milliseconds(action.started_at))
        )
    lines += [
        "# HELP narwhal_control_load_job_running Whether a load job is running.",
        "# TYPE narwhal_control_load_job_running gauge",
    ]
    job = None if service.jobs is None else service.jobs.job
    if job is not None and job.state == "running":
        labels = {"job": job.id, "workload": job.params.get("workload", "")}
        lines.append(_sample("narwhal_control_load_job_running", labels, 1))
    lines += [
        "# HELP narwhal_control_engine_available Whether a baseline engine outside the "
        "router's membership and its attestation sidecar respond.",
        "# TYPE narwhal_control_engine_available gauge",
    ]
    engines = () if available is None else available.engines
    for engine in engines:
        labels = {"iid": engine.iid, "backend": engine.backend}
        lines.append(_sample("narwhal_control_engine_available", labels, int(engine.available)))
    lines += [
        "# HELP narwhal_control_engine_starting Whether fleet control started a baseline engine "
        "outside the router's membership that does not respond yet.",
        "# TYPE narwhal_control_engine_starting gauge",
    ]
    for engine in engines:
        labels = {"iid": engine.iid, "backend": engine.backend}
        lines.append(_sample("narwhal_control_engine_starting", labels, int(engine.starting)))
    return "\n".join(lines) + "\n"


class FleetSignals:
    """Read the router's readiness and the alerts Prometheus reports firing."""

    def __init__(
        self,
        service: ControlService,
        transport: httpx.AsyncBaseTransport | None = None,
        engines: EngineActions | None = None,
    ) -> None:
        self.service = service
        self._transport = transport
        # Reads the baseline engines outside the membership for the metrics.
        self.engines = engines

    async def document(self) -> dict[str, Any]:
        """Return the router's readiness and, when `prometheus_url` is set, the firing alerts."""
        return {"router": await self.router(), "alerts": await self.alerts()}

    async def router(self) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.service.config.router.url, transport=self._transport, trust_env=False
        ) as client:
            status, reason = await probe(client, READY_PROBE_TIMEOUT_S)
        return {"path": READY_PATH, "ready": status == 200, "status_code": status, "reason": reason}

    async def alerts(self) -> dict[str, Any] | None:
        url = self.service.config.prometheus_url
        if url is None:
            return None
        try:
            async with httpx.AsyncClient(
                base_url=url, transport=self._transport, trust_env=False
            ) as client:
                response = await client.get(
                    "/api/v1/query", params={"query": ALERTS_QUERY}, timeout=PROMETHEUS_TIMEOUT_S
                )
            response.raise_for_status()
            series = response.json()["data"]["result"]
            firing = [
                {
                    "alertname": str(item["metric"]["alertname"]),
                    "labels": {
                        str(key): str(value)
                        for key, value in sorted(item["metric"].items())
                        if key not in _ALERT_LABELS
                    },
                }
                for item in series
            ]
        except (httpx.HTTPError, ValueError, LookupError, TypeError, AttributeError) as exc:
            return {"firing": None, "error": f"{type(exc).__name__}: {exc}"}
        firing.sort(key=lambda alert: (alert["alertname"], sorted(alert["labels"].items())))
        return {"firing": firing, "error": None}


def signal_routes(signals: FleetSignals) -> APIRouter:
    """Return the fleet signal route and the metrics route."""
    routes = APIRouter()

    @routes.get(f"{API}/fleet")
    async def fleet() -> dict[str, Any]:
        return await signals.document()

    @routes.get(METRICS_PATH, include_in_schema=False)
    async def service_metrics() -> PlainTextResponse:
        available = None if signals.engines is None else await signals.engines.available()
        return PlainTextResponse(metrics(signals.service, available), media_type=METRICS_TYPE)

    return routes
