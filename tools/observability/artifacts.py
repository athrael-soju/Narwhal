"""Stage readable monitoring mounts inside a private deployment directory."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tools.observability.make_targets import TargetContract, metrics_authority, write_contract

BASE = Path(__file__).resolve().parent
MOUNTS = BASE.parents[1] / "runs" / "observability" / "mounts"
FILES = {
    "prometheus.yml": "prometheus/prometheus.yml",
    "prometheus-alerts.yml": "prometheus/prometheus-alerts.yml",
    "grafana/provisioning/dashboards/narwhal.yml": "grafana-provisioning/dashboards/narwhal.yml",
    "grafana/provisioning/datasources/prometheus.yml": (
        "grafana-provisioning/datasources/prometheus.yml"
    ),
    "grafana-narwhal.json": "grafana-dashboards/narwhal.json",
}
FLEET_CONTROL_DASHBOARD = "grafana-dashboards/fleet-control.json"
FLEET_CONTROL_UID = "narwhal-fleet-control"
CONSOLE_URL_ENV = "NARWHAL_CONTROL_CONSOLE_URL"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:18020/console"
CONTROL_METRICS_URL_ENV = "NARWHAL_CONTROL_METRICS_URL"
CONTROL_TOKEN_ENV = "NARWHAL_CONTROL_TOKEN"
CONTROL_TARGETS = "prometheus/targets/fleet-control.json"
CONTROL_TOKEN_FILE = "prometheus/fleet-control-token"
CONSOLE_THEME = "theme=dark"
# The console is another origin, so `allow-same-origin` grants it no access to Grafana.
CONSOLE_SANDBOX = (
    "allow-scripts allow-same-origin allow-forms allow-downloads "
    "allow-top-navigation-by-user-activation"
)
CONSOLE_VIEWS = {
    "status": "Fleet control session",
    "engines": "Fleet control engines",
    "activity": "Fleet control activity",
    "load": "Fleet control load job",
    "config": "Fleet control configuration",
}
_ROUTER = 'job="narwhal-router",instance=~"$router"'
DIAGNOSTIC_ROWS = (
    ("Controller", (10, 9)),
    ("Request & Recovery", (52, 12, 57, 58, 54, 55, 56, 59)),
)
REQUEST_STATS = (
    ("A", "Within SLO"),
    ("B", "Offered"),
    ("C", "Completed"),
    ("E", "Dropped"),
    ("F", "Refused"),
    ("G", "Rejected"),
    ("H", "Failed"),
    ("I", "Expired"),
)
CONTROL_ANNOTATIONS = (
    {
        "name": "Fleet control actions",
        "iconColor": "#B877D9",
        "expr": "narwhal_control_action_started_ms",
        "step": "15s",
        "titleFormat": "{{title}}",
        "textFormat": "{{action}} {{outcome}} · session {{session}} #{{seq}}",
        "useValueForTime": True,
    },
    {
        "name": "Load jobs",
        "iconColor": "#5794F2",
        "expr": "max by (job, workload) (narwhal_control_load_job_running)",
        "step": "5s",
        "titleFormat": "{{job}}",
        "textFormat": "{{workload}}",
        "useValueForTime": False,
    },
)
_URL_CHARACTERS = re.compile(r"[A-Za-z0-9._~%:/\[\]-]+")


def _directory(path: Path, mode: int) -> None:
    if path.is_symlink():
        raise ValueError(f"Monitoring mount directory must be a real directory: {path}")
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    path.chmod(mode)


def console_url(env: Mapping[str, str]) -> str:
    """Return the console URL the Fleet control dashboard frames, from `CONSOLE_URL_ENV`."""
    value = env.get(CONSOLE_URL_ENV, DEFAULT_CONSOLE_URL)
    parts = urlsplit(value)
    if (
        not _URL_CHARACTERS.fullmatch(value)
        or parts.scheme not in {"http", "https"}
        or not parts.hostname
    ):
        raise ValueError(
            f"{CONSOLE_URL_ENV} must be an http or https URL without credentials, query or fragment"
        )
    return value


@dataclass(frozen=True)
class ControlTarget:
    """The fleet control service Prometheus scrapes for the dashboard's annotations."""

    authority: str
    token: str


def control_target(env: Mapping[str, str]) -> ControlTarget | None:
    """Return the control service named by `CONTROL_METRICS_URL_ENV`, or None when it is unset."""
    url = env.get(CONTROL_METRICS_URL_ENV, "")
    if not url:
        return None
    try:
        authority = metrics_authority(url)
    except ValueError as exc:
        raise ValueError(f"{CONTROL_METRICS_URL_ENV}: {exc}") from None
    token = env.get(CONTROL_TOKEN_ENV, "")
    if not token or token != token.strip() or any(c.isspace() for c in token):
        raise ValueError(
            f"{CONTROL_METRICS_URL_ENV} requires the control bearer token in {CONTROL_TOKEN_ENV}"
        )
    return ControlTarget(authority, token)


def _grid_item(name: str, x: int, y: int, width: int, height: int) -> dict[str, Any]:
    element = {"kind": "ElementReference", "name": name}
    spec = {"x": x, "y": y, "width": width, "height": height, "element": element}
    return {"kind": "GridLayoutItem", "spec": spec}


def _panel(
    panel_id: int,
    title: str,
    description: str,
    queries: list[dict[str, Any]],
    kind: str,
    options: Mapping[str, Any],
    defaults: Mapping[str, Any],
    overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "Panel",
        "spec": {
            "id": panel_id,
            "title": title,
            "description": description,
            "data": {
                "kind": "QueryGroup",
                "spec": {"queries": queries, "transformations": [], "queryOptions": {}},
            },
            "vizConfig": {
                "kind": kind,
                "spec": {
                    "options": dict(options),
                    "fieldConfig": {"defaults": dict(defaults), "overrides": overrides or []},
                },
            },
        },
    }


def _query(ref: str, expr: str, legend: str, *, instant: bool = False) -> dict[str, Any]:
    spec: dict[str, Any] = {"expr": expr, "legendFormat": legend}
    if instant:
        spec["instant"] = True
    return {
        "kind": "PanelQuery",
        "spec": {
            "query": {"kind": "prometheus", "spec": spec},
            "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
            "refId": ref,
        },
    }


def _queries(panel: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    queries = panel["spec"]["data"]["spec"]["queries"]
    return {query["spec"]["refId"]: query["spec"]["query"]["spec"] for query in queries}


def _named(name: str, *properties: tuple[str, Any]) -> dict[str, Any]:
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": key, "value": value} for key, value in properties],
    }


def _thresholds(*steps: tuple[str, float | None]) -> dict[str, Any]:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def _requests_stat(source: Mapping[str, Any], panel_id: int) -> dict[str, Any]:
    """Return the Requests table of the shipped dashboard as one stat per column."""
    shipped = _queries(source["panel-105"])
    queries = [_query(ref, shipped[ref]["expr"], name, instant=True) for ref, name in REQUEST_STATS]
    share = (("unit", "percentunit"), ("decimals", 1), ("color", {"mode": "thresholds"}))
    overrides = [
        _named(
            "Within SLO",
            *share,
            ("thresholds", _thresholds(("#D44A3A", None), ("#F2CC0C", 0.9), ("#56A64B", 0.95))),
        ),
        _named(
            "Dropped",
            *share,
            ("thresholds", _thresholds(("#56A64B", None), ("#F2CC0C", 0.01), ("#D44A3A", 0.05))),
        ),
    ]
    return _panel(
        panel_id,
        "Requests",
        source["panel-105"]["spec"]["description"],
        queries,
        "stat",
        {
            "colorMode": "value",
            "graphMode": "none",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "text": {"titleSize": 12, "valueSize": 26},
            "textMode": "value_and_name",
            "wideLayout": True,
        },
        {
            "unit": "short",
            "decimals": 0,
            "noValue": "N/A",
            "color": {"mode": "fixed", "fixedColor": "text"},
        },
        overrides,
    )


def _latency_bars(source: Mapping[str, Any], panel_id: int) -> dict[str, Any]:
    """Return the p95 latency of the shipped Latency table as bars against the SLO."""
    shipped = _queries(source["panel-106"])
    queries = [
        _query("A", shipped["A"]["expr"], "TTFT p95", instant=True),
        _query("B", shipped["B"]["expr"], "TPOT p95", instant=True),
    ]
    return _panel(
        panel_id,
        "Latency against SLO",
        "The p95 time to first token and time per output token over the displayed interval, "
        "as a share of each SLO target. A bar turns yellow at 80% of the target and red at 100%. "
        "The bar scale "
        "runs from 0 to 150% of the target.",
        queries,
        "bargauge",
        {
            "displayMode": "basic",
            "orientation": "horizontal",
            "namePlacement": "left",
            "showUnfilled": True,
            "sizing": "auto",
            "valueMode": "color",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        {
            "unit": "percentunit",
            "decimals": 0,
            "min": 0,
            "max": 1.5,
            "noValue": "N/A",
            "color": {"mode": "thresholds"},
            "thresholds": _thresholds(("#56A64B", None), ("#F2CC0C", 0.8), ("#D44A3A", 1.0)),
        },
    )


def _request_outcomes(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return offered, completed and dropped requests per second.

    The panel keeps the shipped panel ID, which the shipped alert annotations name.
    """
    shipped = source["panel-11"]
    queries = _queries(shipped)
    dropped = " + ".join(
        f"sum(rate(narwhal_{name}_total{{{_ROUTER}}}[$__rate_interval]))"
        for name in ("refused", "rejected", "failed", "expired")
    )
    fixed = (("custom.fillOpacity", 0),)
    return _panel(
        shipped["spec"]["id"],
        "Request outcomes",
        "Requests offered, completed and dropped per second. Dropped requests were refused, "
        "rejected, failed or expired. Shaded regions are load jobs; dashed lines are fleet "
        "control actions.",
        [
            _query("A", queries["A"]["expr"], "offered"),
            _query("B", queries["B"]["expr"], "completed"),
            _query("C", dropped, "dropped"),
        ],
        "timeseries",
        {"tooltip": {"mode": "multi", "sort": "desc"}},
        {
            "unit": "reqps",
            "min": 0,
            "custom": {"fillOpacity": 7, "gradientMode": "opacity", "lineWidth": 2},
        },
        [
            _named("offered", *fixed, ("color", {"mode": "fixed", "fixedColor": "#5794F2"})),
            _named("completed", ("color", {"mode": "fixed", "fixedColor": "#56A64B"})),
            _named("dropped", ("color", {"mode": "fixed", "fixedColor": "#FF9830"})),
        ],
    )


def _console_view(panel_id: int, console: str, view: str, title: str) -> dict[str, Any]:
    """Return a text panel that frames one console view."""
    frame = (
        f'<iframe src="{html.escape(f"{console}?view={view}&{CONSOLE_THEME}")}" '
        f'title="{html.escape(title)}" sandbox="{CONSOLE_SANDBOX}" referrerpolicy="no-referrer" '
        'style="display:block;width:100%;height:100%;border:0"></iframe>'
    )
    return _panel(
        panel_id,
        "",
        f"{title}. The fleet control console refuses this frame unless console.embed_in_grafana "
        "is true in the control configuration.",
        [],
        "text",
        {"content": frame, "mode": "html"},
        {},
    )


def _row(title: str, collapse: bool, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a dashboard row; an untitled row has no header and stays expanded."""
    spec: dict[str, Any] = {
        "title": title,
        "collapse": collapse,
        "layout": {"kind": "GridLayout", "spec": {"items": items}},
    }
    if not title:
        spec["hideHeader"] = True
    return {"kind": "RowsLayoutRow", "spec": spec}


def _annotation(spec: Mapping[str, Any]) -> dict[str, Any]:
    query = {key: spec[key] for key in ("expr", "step", "titleFormat", "textFormat")}
    query["useValueForTime"] = spec["useValueForTime"]
    return {
        "kind": "AnnotationQuery",
        "spec": {
            "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
            "query": {"kind": "prometheus", "spec": query},
            "enable": True,
            "hide": True,
            "iconColor": spec["iconColor"],
            "name": spec["name"],
            "legacyOptions": dict(query),
        },
    }


def fleet_control_dashboard(source: Mapping[str, Any], console: str) -> dict[str, Any]:
    """Return the Fleet control dashboard: console views among panels of `source`."""
    spec = source["spec"]
    shipped = spec["elements"]
    next_id = max(element["spec"]["id"] for element in shipped.values()) + 1
    elements: dict[str, Any] = {}
    for offset, (view, title) in enumerate(CONSOLE_VIEWS.items()):
        elements[f"console-{view}"] = _console_view(next_id + offset, console, view, title)
    next_id += len(CONSOLE_VIEWS)
    elements["panel-requests"] = _requests_stat(shipped, next_id)
    elements["panel-latency"] = _latency_bars(shipped, next_id + 1)
    elements["panel-11"] = _request_outcomes(shipped)
    for name in ("panel-8", "panel-37", "panel-50", "panel-51"):
        elements[name] = shipped[name]
    items = [
        _grid_item("console-status", 0, 0, 24, 2),
        _grid_item("panel-requests", 0, 2, 15, 4),
        _grid_item("panel-latency", 15, 2, 9, 4),
        _grid_item("console-engines", 0, 6, 12, 13),
        _grid_item("panel-8", 12, 6, 12, 13),
        _grid_item("panel-11", 0, 19, 14, 8),
        _grid_item("panel-37", 14, 19, 10, 8),
        _grid_item("panel-50", 0, 27, 12, 8),
        _grid_item("panel-51", 12, 27, 12, 8),
        _grid_item("console-activity", 0, 35, 24, 8),
    ]
    rows = [
        _row("", False, items),
        _row(
            "Load and configuration",
            False,
            [
                _grid_item("console-load", 0, 0, 12, 9),
                _grid_item("console-config", 12, 0, 12, 9),
            ],
        ),
    ]
    for title, panels in DIAGNOSTIC_ROWS:
        names = [f"panel-{panel}" for panel in panels if f"panel-{panel}" in shipped]
        for name in names:
            elements[name] = shipped[name]
        if names:
            grid = [
                _grid_item(name, 12 * (index % 2), 8 * (index // 2), 12, 8)
                for index, name in enumerate(names)
            ]
            rows.append(_row(title, True, grid))
    return {
        "apiVersion": source["apiVersion"],
        "kind": source["kind"],
        "metadata": {"name": FLEET_CONTROL_UID, "namespace": source["metadata"]["namespace"]},
        "spec": {
            "annotations": [*spec["annotations"], *map(_annotation, CONTROL_ANNOTATIONS)],
            "cursorSync": spec["cursorSync"],
            "description": "Fleet control console views beside panels from the Narwhal "
            "Orchestrator dashboard.",
            "editable": spec["editable"],
            "elements": elements,
            "layout": {"kind": "RowsLayout", "spec": {"rows": rows}},
            "links": [],
            "liveNow": spec["liveNow"],
            "preload": spec["preload"],
            "tags": [*spec["tags"], "fleet-control"],
            "timeSettings": spec["timeSettings"],
            "title": "Fleet control",
            "variables": spec["variables"],
        },
    }


def _write(target: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage_artifacts(
    contract: TargetContract,
    console: str = DEFAULT_CONSOLE_URL,
    control: ControlTarget | None = None,
    root: Path = MOUNTS,
    source: Path = BASE,
) -> None:
    """Copy the named configs and discovery targets with explicit container permissions.

    The Fleet control dashboard is derived from the shipped dashboard and frames `console`.
    Prometheus scrapes `control` with its token. When `control` is None, the fleet-control target
    list and token file are empty.
    """
    _directory(root, 0o700)
    for relative in (
        "prometheus",
        "prometheus/targets",
        "grafana-provisioning",
        "grafana-provisioning/dashboards",
        "grafana-provisioning/datasources",
        "grafana-dashboards",
    ):
        _directory(root / relative, 0o755)
    for origin, destination in FILES.items():
        _write(root / destination, (source / origin).read_bytes())
    shipped = json.loads((source / "grafana-narwhal.json").read_text(encoding="utf-8"))
    dashboard = fleet_control_dashboard(shipped, console)
    _write(root / FLEET_CONTROL_DASHBOARD, (json.dumps(dashboard, indent=2) + "\n").encode())
    targets = [] if control is None else [{"targets": [control.authority]}]
    _write(root / CONTROL_TARGETS, (json.dumps(targets, indent=2) + "\n").encode())
    # The Prometheus container reads the token; the 0700 mount root keeps it from other accounts.
    _write(root / CONTROL_TOKEN_FILE, ("" if control is None else control.token).encode())
    write_contract(contract, root / "prometheus" / "targets")
