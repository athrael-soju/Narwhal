"""Start labelled monitoring services and retain only verified owned containers."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from narwhal.diagnostics.bundle import Redactor
from narwhal.observability.management_status import observe_monitoring
from narwhal.observability.management_targets import build_targets
from narwhal.observability.management_types import MonitoringBinding

from .management_records import OperationError
from .ssh_worker import _command, _labels

if TYPE_CHECKING:
    from .ssh_adapter import Session


def owned_override(owner: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Give each service and retained data volume the supervisor's exact owner labels."""
    project = "narwhal-" + owner["launch_token"].replace("-", "")
    labels = _labels(owner)
    return project, {
        "services": {name: {"labels": labels} for name in ("prometheus", "grafana")},
        "volumes": {name: {"labels": labels} for name in ("prom-data", "grafana-data")},
    }


def _get(url: str, timeout_s: float) -> tuple[int, str]:
    try:
        return _get_body(url, timeout_s)
    except httpx.HTTPError:
        raise OSError("Monitoring HTTP source is unavailable") from None


def _get_body(url: str, timeout_s: float) -> tuple[int, str]:
    with (
        httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout_s) as client,
        client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response,
    ):
        if response.headers.get("content-encoding", "identity") not in {"", "identity"}:
            raise ValueError("Monitoring response is compressed")
        body = bytearray()
        for block in response.iter_bytes(chunk_size=65_536):
            if len(body) + len(block) > 262_144:
                raise ValueError("Monitoring response exceeds its byte limit")
            body.extend(block)
        return response.status_code, body.decode("utf-8")


def start_remote(request: dict[str, Any]) -> dict[str, Any]:
    """Run shipped startup checks under the remote supervisor's retained-container boundary."""
    from .ssh_gates import write_result

    startup = importlib.import_module("tools.observability.start")
    environment = dict(os.environ)
    project, override = owned_override(request["owner"])
    path = Path(request["result_path"]).with_name("compose.owner.json")
    write_result(path, override)
    stack = startup.ComposeStack(env=environment)
    stack._prefix.extend(["-p", project, "-f", str(path)])
    fleet = json.loads(Path(request["fleet"]).read_bytes())
    contract = build_targets(fleet, request["router_url"])
    seconds = float(request["ready_seconds"])
    deadline = time.monotonic() + seconds
    containers = startup.start(
        environment, stack, contract, get=_get, timeout_s=max(0.1, seconds / 2)
    )
    services = {service.name: service for service in startup.configured_services(environment)}
    prometheus = "http://" + services["prometheus"].listener.authority
    grafana = "http://" + services["grafana"].listener.authority
    binding = MonitoringBinding(contract, prometheus, request["host_id"])
    observation = asyncio.run(
        observe_monitoring(
            binding,
            prometheus_url=prometheus,
            grafana_url=grafana,
            router_url=request["router_url"],
            deadline=deadline,
            freshness_s=request["freshness_s"],
            redactor=Redactor(False),
        )
    )
    if observation["readiness"] != "pass":
        for row in observation["sources"]:
            if raw := row.pop("raw_body", None):
                row["retained_prefix"] = raw.decode("utf-8", errors="replace")
        write_result(path.with_name("monitoring.failed.json"), observation)
        raise ValueError("Monitoring scrape, dashboard or serving readiness did not pass")
    volume_names = [project + "_" + name for name in ("prom-data", "grafana-data")]
    volumes = json.loads(_command(["docker", "volume", "inspect", *volume_names], deadline))
    expected = _labels(request["owner"])
    if (
        not isinstance(volumes, list)
        or len(volumes) != 2
        or {row.get("Name") for row in volumes} != set(volume_names)
        or any(
            any((row.get("Labels") or {}).get(key) != value for key, value in expected.items())
            for row in volumes
        )
    ):
        raise ValueError("Monitoring data volume ownership does not match the operation")
    return {
        "host_id": request["host_id"],
        "project": project,
        "labels": expected,
        "containers": {name: asdict(value) for name, value in containers.items()},
        "volumes": [
            {"name": row["Name"], "driver": row["Driver"], "labels": row["Labels"]}
            for row in volumes
        ],
        "prometheus_url": prometheus,
        "grafana_url": grafana,
        "binding": asdict(binding),
        "observation": observation,
    }


def start(session: Session) -> dict[str, Any]:
    """Retain monitoring only after HTTP checks and matching live container receipts."""
    router = session.state["router"]
    effect, result = session.gate(
        session.router_host,
        "monitoring",
        {
            "operation": "monitoring_start",
            "host_id": session.router_host,
            "fleet": router["fleet_path"],
            "router_url": router["url"],
            "freshness_s": session.context.target.freshness_s,
            "ready_seconds": max(0.1, session.context.deadline - time.monotonic() - 5),
        },
        kind="monitoring",
        containers=True,
    )
    if result is None or result["observation"]["readiness"] != "pass":
        raise OperationError("prerequisite_failed", "Monitoring startup has no passing evidence")
    expected = {row["cid"] for row in result["containers"].values()}
    observed = session.transport.inspect_containers(
        session.router_host, effect["owner"]["launch_token"]
    )["containers"]
    labels = _labels(effect["owner"])
    if (
        len(expected) != 2
        or {row["container_id"] for row in observed} != expected
        or any(not row["running"] or row["labels"] != labels for row in observed)
    ):
        raise OperationError("ownership_conflict", "Monitoring containers differ from startup")
    result["effect"] = effect
    session.state["monitoring"] = result
    session.retain(effect)
    session.save()
    return result
