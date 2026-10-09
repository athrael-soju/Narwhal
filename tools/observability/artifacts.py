"""Stage readable monitoring mounts inside a private deployment directory."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tools.observability.make_targets import TargetContract, write_contract

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
# The fleet control console as the operator's browser reaches it through the operator tunnel.
CONSOLE_URL_ENV = "NARWHAL_CONTROL_CONSOLE_URL"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:18020/console"
# Panels of the shipped dashboard shown beside the console, in display order: Requests, Latency,
# Time to first token, Time per output token, Engine role history and Fleet events.
CONSOLE_PANELS = (105, 106, 50, 51, 8, 37)
# The console matches Grafana's default dark theme when framed with this query.
CONSOLE_THEME = "?theme=dark"
# Grid columns the console occupies of Grafana's 24.
CONSOLE_WIDTH = 10
# The console runs its own script, keeps its token in session storage, submits its forms through
# script and asks for confirmation with browser dialogs. It is another origin, so
# `allow-same-origin` keeps it in its own origin and grants no access to Grafana.
CONSOLE_SANDBOX = "allow-scripts allow-same-origin allow-forms allow-modals"
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


def _grid_item(name: str, x: int, y: int, width: int, height: int) -> dict[str, Any]:
    element = {"kind": "ElementReference", "name": name}
    spec = {"x": x, "y": y, "width": width, "height": height, "element": element}
    return {"kind": "GridLayoutItem", "spec": spec}


def fleet_control_dashboard(source: Mapping[str, Any], console: str) -> dict[str, Any]:
    """Return the Fleet control dashboard: the console framed beside panels of `source`.

    The panels, variables, annotations and time settings are copies from the shipped dashboard,
    so the two dashboards show the same series.
    """
    spec = source["spec"]
    heights = {
        item["spec"]["element"]["name"]: item["spec"]["height"]
        for item in spec["layout"]["spec"]["items"]
    }
    names = [f"panel-{panel}" for panel in CONSOLE_PANELS]
    items = []
    y = 0
    for name in names:
        items.append(_grid_item(name, CONSOLE_WIDTH, y, 24 - CONSOLE_WIDTH, heights[name]))
        y += heights[name]
    panel_id = max(element["spec"]["id"] for element in spec["elements"].values()) + 1
    frame = (
        f'<iframe src="{html.escape(console + CONSOLE_THEME)}" title="Fleet control console" '
        f'sandbox="{CONSOLE_SANDBOX}" referrerpolicy="no-referrer" '
        'style="display:block;width:100%;height:100%;border:0"></iframe>'
    )
    console_panel = {
        "kind": "Panel",
        "spec": {
            "id": panel_id,
            "title": "Fleet control",
            "description": (
                "The fleet control console. Connect with the control token. The console "
                "refuses this frame unless console.embed_in_grafana is true in the control "
                "configuration."
            ),
            "data": {
                "kind": "QueryGroup",
                "spec": {"queries": [], "transformations": [], "queryOptions": {}},
            },
            "vizConfig": {
                "kind": "text",
                "spec": {
                    "options": {"content": frame, "mode": "html"},
                    "fieldConfig": {"defaults": {}, "overrides": []},
                },
            },
        },
    }
    return {
        "apiVersion": source["apiVersion"],
        "kind": source["kind"],
        "metadata": {"name": FLEET_CONTROL_UID, "namespace": source["metadata"]["namespace"]},
        "spec": {
            "annotations": spec["annotations"],
            "cursorSync": spec["cursorSync"],
            "description": "The fleet control console beside the live Narwhal Orchestrator panels.",
            "editable": spec["editable"],
            "elements": {"panel-console": console_panel}
            | {name: spec["elements"][name] for name in names},
            "layout": {
                "kind": "GridLayout",
                "spec": {"items": [_grid_item("panel-console", 0, 0, CONSOLE_WIDTH, y), *items]},
            },
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
    root: Path = MOUNTS,
    source: Path = BASE,
) -> None:
    """Copy the named configs and discovery targets with explicit container permissions.

    The Fleet control dashboard is derived from the shipped dashboard and frames `console`.
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
    write_contract(contract, root / "prometheus" / "targets")
