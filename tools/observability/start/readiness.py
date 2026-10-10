"""Wait for the launched services, their contracts and target collection."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

from tools.observability.make_targets import TargetContract

from .services import Container, Service, StartupError
from .stack import Stack

DASHBOARDS = "/apis/dashboard.grafana.app/v2beta1/namespaces/default/dashboards"
DASHBOARD_PATH = f"{DASHBOARDS}/narwhal-router"
DEFAULT_READY_TIMEOUT_S = 60.0

HttpGet = Callable[[str, float], tuple[int, str]]


def http_get(url: str, timeout_s: float) -> tuple[int, str]:
    """Fetch one local health document."""
    request = urllib.request.Request(url, headers={"User-Agent": "narwhal-observe/1"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def _expressions(value: object) -> list[str]:
    """Return every Prometheus expression embedded in a dashboard document."""
    if isinstance(value, dict):
        found = [value["expr"]] if isinstance(value.get("expr"), str) else []
        for child in value.values():
            found.extend(_expressions(child))
        return found
    if isinstance(value, list):
        return [expression for child in value for expression in _expressions(child)]
    return []


def verify_dashboard_contract(document: dict[str, object]) -> None:
    """Require a default router selection that produces populated panel queries."""
    metadata = document.get("metadata")
    spec = document.get("spec")
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "narwhal-router"
        or not isinstance(spec, dict)
    ):
        raise StartupError("Grafana dashboard UID narwhal-router is unavailable")
    variables = spec.get("variables")
    variable_list = variables if isinstance(variables, list) else []
    router = next(
        (
            variable["spec"]
            for variable in variable_list
            if isinstance(variable, dict)
            if isinstance(variable.get("spec"), dict)
            if variable["spec"].get("name") == "router"
        ),
        None,
    )
    if not isinstance(router, dict):
        raise StartupError("Grafana dashboard requires the router variable")
    current = router.get("current")
    if (
        not router.get("includeAll")
        or router.get("allValue") != ".*"
        or not isinstance(current, dict)
        or current.get("value") != "$__all"
    ):
        raise StartupError("Grafana router variable must default to every configured target")
    expressions = _expressions(spec.get("elements"))
    if any('instance="$router"' in expression for expression in expressions):
        raise StartupError("Grafana router panels require regex instance matching")
    if not any('instance=~"$router"' in expression for expression in expressions):
        raise StartupError("Grafana dashboard contains zero router-scoped panel queries")


def _verify_http(service: Service, get: HttpGet, prometheus_url: str) -> None:
    base = service.listener.url
    if service.name == "prometheus":
        status, ready = get(f"{base}/-/ready", 2.0)
        if status != 200 or "Prometheus Server is Ready" not in ready:
            raise StartupError(f"Prometheus readiness returned HTTP {status}")
        status, body = get(f"{base}/api/v1/status/buildinfo", 2.0)
        document = json.loads(body)
        version = document.get("data", {}).get("version")
    else:
        status, body = get(f"{base}/api/health", 2.0)
        document = json.loads(body)
        if document.get("database") != "ok":
            raise StartupError("Grafana health did not report database ok")
        version = document.get("version")
    if status != 200:
        raise StartupError(f"{service.label} health returned HTTP {status}")
    if version != service.version:
        raise StartupError(
            f"{service.label} reported version {version!r}; expected {service.version}"
        )
    if service.name == "grafana":
        status, body = get(f"{base}/api/datasources/name/Prometheus", 2.0)
        datasource = json.loads(body)
        if status != 200 or datasource.get("type") != "prometheus":
            raise StartupError("Grafana Prometheus datasource is unavailable")
        if datasource.get("url") != prometheus_url:
            raise StartupError(
                f"Grafana datasource uses {datasource.get('url')!r}; expected {prometheus_url}"
            )
        status, body = get(f"{base}{DASHBOARD_PATH}", 2.0)
        dashboard = json.loads(body)
        if status != 200:
            raise StartupError(f"Grafana dashboard returned HTTP {status}")
        verify_dashboard_contract(dashboard)


def _verify_targets(
    prometheus: Service,
    contract: TargetContract,
    get: HttpGet,
) -> None:
    """Require the deployed target identities, scrape health and router readiness."""
    base = prometheus.listener.url
    status, body = get(f"{base}/api/v1/targets", 2.0)
    if status != 200:
        raise StartupError(f"Prometheus targets returned HTTP {status}")
    document = json.loads(body)
    data = document.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(active, list):
        raise StartupError("Prometheus targets response requires activeTargets")

    router_rows = [row for row in active if row.get("scrapePool") == "narwhal-router"]
    if len(router_rows) != 1:
        raise StartupError(f"Prometheus discovered {len(router_rows)} router targets; expected 1")
    router = router_rows[0]
    if _scrape_authority(router.get("scrapeUrl")) != contract.router:
        raise StartupError(
            f"Prometheus router target uses {router.get('scrapeUrl')!r}; expected {contract.router}"
        )
    if router.get("health") != "up":
        raise StartupError(
            f"Prometheus router target {contract.router} reports "
            f"{router.get('health')}: {router.get('lastError', '')}"
        )

    expected_engines = dict(contract.engines)
    engine_rows = [row for row in active if row.get("scrapePool") == "engines"]
    actual_engines: dict[str, str] = {}
    for row in engine_rows:
        labels = row.get("labels")
        iid = labels.get("iid") if isinstance(labels, dict) else None
        if not isinstance(iid, str):
            raise StartupError("Prometheus engine target requires iid")
        if iid in actual_engines:
            raise StartupError(f"Prometheus engine target {iid!r} appears more than once")
        actual_engines[iid] = _scrape_authority(row.get("scrapeUrl"))
        if row.get("health") != "up":
            raise StartupError(
                f"Prometheus engine target {iid} reports "
                f"{row.get('health')}: {row.get('lastError', '')}"
            )
    if actual_engines != expected_engines:
        raise StartupError(
            f"Prometheus engine targets use {actual_engines!r}; expected {expected_engines!r}"
        )

    query = f'narwhal_router_ready{{job="narwhal-router",instance="{contract.router}"}}'
    encoded = urllib.parse.urlencode({"query": query})
    status, body = get(f"{base}/api/v1/query?{encoded}", 2.0)
    if status != 200:
        raise StartupError(f"Prometheus router readiness query returned HTTP {status}")
    result_document = json.loads(body)
    result_data = result_document.get("data")
    result = result_data.get("result") if isinstance(result_data, dict) else None
    if not isinstance(result, list) or len(result) != 1:
        size = len(result) if isinstance(result, list) else 0
        raise StartupError(f"Prometheus router readiness returned {size} series; expected 1")
    value = result[0].get("value") if isinstance(result[0], dict) else None
    if not isinstance(value, list) or len(value) != 2 or value[1] != "1":
        raise StartupError("Prometheus router readiness metric reports zero")


def _scrape_authority(value: object) -> str:
    """Return the authority from one Prometheus scrape URL."""
    if not isinstance(value, str):
        raise StartupError("Prometheus target requires scrapeUrl")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StartupError(f"Prometheus target uses invalid scrape URL {value!r}")
    return parsed.netloc


def wait_collection(
    prometheus: Service,
    contract: TargetContract,
    *,
    get: HttpGet = http_get,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    poll_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for every configured target and the router metric contract."""
    deadline = clock() + timeout_s
    last_error = "target collection is starting"
    while clock() < deadline:
        try:
            _verify_targets(prometheus, contract, get)
            return
        except (
            StartupError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
            OSError,
        ) as exc:
            last_error = str(exc)
        sleep(poll_s)
    raise StartupError(f"target collection exceeded {timeout_s:g}s: {last_error}")


def wait_ready(
    services: Sequence[Service],
    stack: Stack,
    *,
    get: HttpGet = http_get,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    poll_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Container]:
    """Wait for the launched containers and their versioned HTTP contracts."""
    prometheus = next(service for service in services if service.name == "prometheus")
    prometheus_url = prometheus.listener.url
    launched: dict[str, Container] = {}
    for service in services:
        container = stack.container(service.name)
        if container is None:
            raise StartupError(f"Compose created zero containers for {service.name}")
        if container.image != service.image:
            raise StartupError(
                f"{service.label} container uses {container.image!r}; expected {service.image}"
            )
        launched[service.name] = container

    deadline = clock() + timeout_s
    last_error = "readiness has not started"
    while clock() < deadline:
        all_ready = True
        for service in services:
            expected = launched[service.name]
            current = stack.container(service.name)
            if current is None:
                raise StartupError(f"{service.label} container {expected.cid[:12]} disappeared")
            if current.cid != expected.cid:
                raise StartupError(
                    f"{service.label} container changed from {expected.cid[:12]} "
                    f"to {current.cid[:12]} during readiness"
                )
            if current.status in {"dead", "exited", "removing"}:
                raise StartupError(
                    f"{service.label} container {current.cid[:12]} entered {current.status}"
                )
            if current.status != "running" or current.restarting:
                all_ready = False
                last_error = f"{service.label} container state is {current.status}"
                continue
            try:
                _verify_http(service, get, prometheus_url)
            except (StartupError, json.JSONDecodeError, urllib.error.URLError, OSError) as exc:
                all_ready = False
                last_error = f"{service.label}: {exc}"
        if all_ready:
            return launched
        sleep(poll_s)
    raise StartupError(f"observability readiness exceeded {timeout_s:g}s: {last_error}")
