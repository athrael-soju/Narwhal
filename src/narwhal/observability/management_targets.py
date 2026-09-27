"""Share monitoring target identities and dashboard acceptance checks."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from urllib.parse import urlsplit

from narwhal.config.environment import resolve_endpoint


@dataclass(frozen=True)
class TargetContract:
    """Expected Prometheus target identities for one deployment."""

    router: str
    engines: tuple[tuple[str, str], ...]


def metrics_authority(url: str) -> str:
    """Return the host and port Prometheus scrapes from an HTTP base URL."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid metrics URL {url!r}: {exc}") from exc
    if parsed.scheme != "http":
        raise ValueError(f"metrics URL {url!r} requires http")
    if parsed.hostname is None or port is None:
        raise ValueError(f"metrics URL {url!r} requires an explicit host and port")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"metrics URL {url!r} contains credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"metrics URL {url!r} must identify an HTTP origin")
    return parsed.netloc


def build_targets(fleet: dict[str, object], router_url: str) -> TargetContract:
    """Build router and engine target identities from deployment inputs."""
    raw_engines = fleet.get("engines")
    if not isinstance(raw_engines, list) or not raw_engines:
        raise ValueError("fleet document requires a populated engines array")
    engines: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, raw_engine in enumerate(raw_engines):
        if not isinstance(raw_engine, dict):
            raise ValueError(f"fleet engine {index} requires an object")
        iid = raw_engine.get("iid")
        url = raw_engine.get("url")
        if not isinstance(iid, str) or not iid:
            raise ValueError(f"fleet engine {index} requires iid")
        if iid in seen:
            raise ValueError(f"fleet engine iid {iid!r} appears more than once")
        if not isinstance(url, str):
            raise ValueError(f"fleet engine {iid!r} requires url")
        seen.add(iid)
        engines.append((iid, metrics_authority(resolve_endpoint(url, f"engines[{index}].url"))))
    return TargetContract(metrics_authority(router_url), tuple(engines))


class StartupError(RuntimeError):
    """Describe an operator-correctable startup failure."""


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
    dashboard = document.get("dashboard")
    if not isinstance(dashboard, dict) or dashboard.get("uid") != "narwhal-router":
        raise StartupError("Grafana dashboard UID narwhal-router is unavailable")
    templating = dashboard.get("templating")
    variables = templating.get("list") if isinstance(templating, dict) else None
    variable_list = variables if isinstance(variables, list) else []
    router = next(
        (
            variable
            for variable in variable_list
            if isinstance(variable, dict)
            if variable.get("name") == "router"
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
    expressions = _expressions(dashboard)
    if any('instance="$router"' in expression for expression in expressions):
        raise StartupError("Grafana router panels require regex instance matching")
    if not any('instance=~"$router"' in expression for expression in expressions):
        raise StartupError("Grafana dashboard contains zero router-scoped panel queries")


def _scrape_authority(value: object) -> str:
    """Return the authority from one Prometheus scrape URL."""
    if not isinstance(value, str):
        raise StartupError("Prometheus target requires scrapeUrl")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StartupError(f"Prometheus target uses invalid scrape URL {value!r}")
    return parsed.netloc


def verify_targets(contract: TargetContract, document: dict, readiness: dict) -> None:
    """Require exact configured target membership, healthy scrapes and serving readiness."""
    data = document.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if document.get("status", "success") != "success" or not isinstance(active, list):
        raise StartupError("Prometheus targets response requires activeTargets")
    if any(not isinstance(row, dict) for row in active):
        raise StartupError("Prometheus target rows must be objects")
    routers = [row for row in active if row.get("scrapePool") == "narwhal-router"]
    if len(routers) != 1:
        raise StartupError("Prometheus requires exactly one configured router target")
    router = routers[0]
    if _scrape_authority(router.get("scrapeUrl")) != contract.router:
        raise StartupError("Prometheus router target differs from the configured router")
    if router.get("health") != "up":
        raise StartupError("Prometheus router scrape is unhealthy")
    engines = [row for row in active if row.get("scrapePool") == "engines"]
    actual = {}
    for row in engines:
        labels = row.get("labels")
        iid = labels.get("iid") if isinstance(labels, dict) else None
        if not isinstance(iid, str) or iid in actual:
            raise StartupError("Prometheus engine target identity is missing or duplicated")
        actual[iid] = _scrape_authority(row.get("scrapeUrl"))
        if row.get("health") != "up":
            raise StartupError(
                f"Prometheus engine target {iid} reports "
                f"{row.get('health')}: {row.get('lastError', '')}"
            )
    if actual != dict(contract.engines):
        raise StartupError("Prometheus engine targets differ from the configured fleet")
    data = readiness.get("data")
    rows = data.get("result") if isinstance(data, dict) else None
    if (
        readiness.get("status", "success") != "success"
        or not isinstance(rows, list)
        or len(rows) != 1
    ):
        size = len(rows) if isinstance(rows, list) else 0
        raise StartupError(f"Prometheus router readiness returned {size} series; expected 1")
    value = rows[0].get("value") if isinstance(rows[0], dict) else None
    if not isinstance(value, list) or len(value) != 2 or value[1] != "1":
        raise StartupError("Prometheus router readiness metric reports zero")
