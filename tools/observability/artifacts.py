"""Stage readable monitoring mounts inside a private deployment directory."""

from __future__ import annotations

import copy
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
}
NARWHAL_DASHBOARD = "grafana-dashboards/narwhal.json"
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
    "results": "Fleet control load metrics",
    "config": "Fleet control configuration",
}
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
    """Return the console URL the Fleet control dashboard frames, from `CONSOLE_URL_ENV`.

    A path, such as `/fleet-control/console`, frames the console from Grafana's own origin.
    """
    value = env.get(CONSOLE_URL_ENV, DEFAULT_CONSOLE_URL)
    parts = urlsplit(value)
    path = not parts.scheme and not parts.netloc and value.startswith("/")
    if not _URL_CHARACTERS.fullmatch(value) or not (
        path or (parts.scheme in {"http", "https"} and parts.hostname)
    ):
        raise ValueError(
            f"{CONSOLE_URL_ENV} must be an http or https URL or a path, "
            "without credentials, query or fragment"
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
        "",
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


def narwhal_dashboard(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return the shipped dashboard with the fleet control action and load-job markers."""
    dashboard = copy.deepcopy(dict(source))
    spec = dashboard["spec"]
    spec["annotations"] = [*spec["annotations"], *map(_annotation, CONTROL_ANNOTATIONS)]
    return dashboard


def fleet_control_dashboard(source: Mapping[str, Any], console: str) -> dict[str, Any]:
    """Return the Fleet control dashboard: the console views, in the shipped dashboard's style."""
    spec = source["spec"]
    elements = {
        f"console-{view}": _console_view(index + 1, console, view, title)
        for index, (view, title) in enumerate(CONSOLE_VIEWS.items())
    }
    items = [
        _grid_item("console-status", 0, 0, 24, 2),
        _grid_item("console-engines", 0, 2, 24, 15),
        _grid_item("console-load", 0, 17, 12, 11),
        _grid_item("console-results", 12, 17, 12, 11),
        _grid_item("console-config", 0, 28, 12, 11),
        _grid_item("console-activity", 12, 28, 12, 11),
    ]
    return {
        "apiVersion": source["apiVersion"],
        "kind": source["kind"],
        "metadata": {"name": FLEET_CONTROL_UID, "namespace": source["metadata"]["namespace"]},
        "spec": {
            "annotations": [],
            "cursorSync": spec["cursorSync"],
            "description": "Fleet control console views. The Narwhal Orchestrator dashboard "
            "marks each action and load job on its charts.",
            "editable": spec["editable"],
            "elements": elements,
            "layout": {"kind": "RowsLayout", "spec": {"rows": [_row("", False, items)]}},
            "links": [],
            "liveNow": spec["liveNow"],
            "preload": spec["preload"],
            "tags": [*spec["tags"], "fleet-control"],
            "timeSettings": spec["timeSettings"],
            "title": "Fleet control",
            "variables": [],
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

    The shipped dashboard gains the fleet control markers. The Fleet control dashboard frames
    `console`.
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
    _write(
        root / NARWHAL_DASHBOARD, (json.dumps(narwhal_dashboard(shipped), indent=2) + "\n").encode()
    )
    dashboard = fleet_control_dashboard(shipped, console)
    _write(root / FLEET_CONTROL_DASHBOARD, (json.dumps(dashboard, indent=2) + "\n").encode())
    targets = [] if control is None else [{"targets": [control.authority]}]
    _write(root / CONTROL_TARGETS, (json.dumps(targets, indent=2) + "\n").encode())
    # The Prometheus container reads the token; the 0700 mount root keeps it from other accounts.
    _write(root / CONTROL_TOKEN_FILE, ("" if control is None else control.token).encode())
    write_contract(contract, root / "prometheus" / "targets")
