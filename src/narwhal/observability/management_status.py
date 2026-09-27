"""Observe configured monitoring services without changing their resources."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx

from narwhal.deployment.management_access import now
from narwhal.diagnostics.bundle import Redactor

from .management_http import fetch, origin
from .management_targets import StartupError, verify_dashboard_contract, verify_targets
from .management_types import MonitoringBinding


def timestamp(value: str | int | float) -> datetime:
    """Parse a finite source timestamp as an aware UTC instant."""
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timestamp requires an offset")
        return parsed.astimezone(UTC)
    if type(value) not in {int, float}:
        raise ValueError("Timestamp requires a number or RFC3339 string")
    return datetime.fromtimestamp(value, UTC)


def evaluate_monitoring(
    binding: MonitoringBinding,
    observations: list[dict[str, Any]],
    *,
    freshness_s: int,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Apply startup acceptance separately from scrape success and observation availability."""
    at = at or datetime.now(UTC)
    rows = {row["source"]: row for row in observations}
    checks = []

    def check(name: str, sources: tuple[str, ...], callback: Any) -> None:
        if any(source not in rows or rows[source]["status"] != "ok" for source in sources):
            checks.append({"check": name, "status": "unknown", "code": "source_unavailable"})
            return
        try:
            callback(*(rows[source]["data"] for source in sources))
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            OverflowError,
            OSError,
            StartupError,
        ):
            checks.append({"check": name, "status": "fail", "code": "readiness_failed"})
        else:
            checks.append({"check": name, "status": "pass"})

    def require(condition: bool) -> None:
        if not condition:
            raise ValueError("Readiness condition failed")

    for row in observations:
        try:
            age = (at - timestamp(row["observed_at"])).total_seconds()
            if row["status"] == "ok" and not 0 <= age <= freshness_s:
                row.update(status="stale", error_code="source_stale")
        except (ValueError, TypeError, OverflowError, OSError):
            row.update(status="error", error_code="invalid_source")

    check(
        "prometheus_ready",
        ("prometheus_ready",),
        lambda body: require(isinstance(body, str) and "Prometheus Server is Ready" in body),
    )
    check(
        "prometheus_version",
        ("prometheus_build",),
        lambda doc: require(
            doc["status"] == "success" and doc["data"]["version"] == binding.prometheus_version
        ),
    )
    check(
        "grafana_health",
        ("grafana_health",),
        lambda doc: require(doc["database"] == "ok" and doc["version"] == binding.grafana_version),
    )
    check(
        "grafana_datasource",
        ("grafana_datasource",),
        lambda doc: require(doc["type"] == "prometheus" and doc["url"] == binding.datasource_url),
    )
    check("grafana_dashboard", ("grafana_dashboard",), verify_dashboard_contract)

    def configured_targets(targets: dict, metric: dict) -> None:
        require(targets["status"] == metric["status"] == "success")
        verify_targets(binding.targets, targets, metric)
        age = (at - timestamp(metric["data"]["result"][0]["value"][0])).total_seconds()
        require(0 <= age <= freshness_s)

    check(
        "configured_targets",
        ("prometheus_targets", "router_metric"),
        configured_targets,
    )
    check(
        "serving_ready",
        ("router_ready",),
        lambda doc: require(
            rows["router_ready"]["http_status"] == 200 and doc["status"] == "ready"
        ),
    )

    def scrape_freshness(document: dict) -> None:
        active = document["data"]["activeTargets"]
        selected = [row for row in active if row.get("scrapePool") in {"narwhal-router", "engines"}]
        require(bool(selected))
        for target in selected:
            age = (at - timestamp(target["lastScrape"])).total_seconds()
            require(0 <= age <= freshness_s)

    check("scrape_freshness", ("prometheus_targets",), scrape_freshness)
    readiness = (
        "fail"
        if any(row["status"] == "fail" for row in checks)
        else "unknown"
        if any(row["status"] == "unknown" for row in checks)
        else "pass"
    )
    observations.append(
        {
            "source": "monitoring_readiness",
            "observed_at": now(),
            "status": "ok",
            "http_status": None,
            "data": {"checks": checks},
            "artifact_id": None,
        }
    )
    return {"sources": observations, "readiness": readiness}


async def observe_monitoring(
    binding: MonitoringBinding,
    *,
    prometheus_url: str,
    grafana_url: str,
    router_url: str,
    deadline: float,
    freshness_s: int,
    redactor: Redactor,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Collect fixed service routes with five seconds per source and one shared deadline."""
    prometheus, grafana, router = map(origin, (prometheus_url, grafana_url, router_url))
    expression = (
        'narwhal_router_ready{job="narwhal-router",instance='
        + json.dumps(binding.targets.router)
        + "}"
    )
    sources = (
        (prometheus + "/-/ready", "prometheus_ready", None),
        (prometheus + "/api/v1/status/buildinfo", "prometheus_build", None),
        (prometheus + "/api/v1/targets", "prometheus_targets", None),
        (prometheus + "/api/v1/query", "router_metric", {"query": expression}),
        (grafana + "/api/health", "grafana_health", None),
        (grafana + "/api/datasources/name/Prometheus", "grafana_datasource", None),
        (grafana + "/api/dashboards/uid/narwhal-router", "grafana_dashboard", None),
        (router + "/ready", "router_ready", None),
    )
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False
    ) as client:
        observations = await asyncio.gather(
            *(
                fetch(client, url, name, deadline=deadline, redactor=redactor, params=params)
                for url, name, params in sources
            )
        )
    if monotonic() > deadline:
        for row in observations:
            if row["status"] == "ok":
                row.update(status="timeout", error_code="source_unavailable")
    return evaluate_monitoring(binding, observations, freshness_s=freshness_s)
