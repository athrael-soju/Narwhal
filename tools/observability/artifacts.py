"""Stage readable monitoring mounts inside a private deployment directory."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from narwhal.backends import load as load_backend
from narwhal.backends import names as backend_names
from narwhal.observability.tools import METRICS_URL_ENV as TOOL_METRICS_URL_ENV
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
CONSOLE_URL_ENV = "NARWHAL_CONTROL_CONSOLE_URL"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:18020/console"
CONTROL_METRICS_URL_ENV = "NARWHAL_CONTROL_METRICS_URL"
CONTROL_TOKEN_ENV = "NARWHAL_CONTROL_TOKEN"
CONTROL_TARGETS = "prometheus/targets/fleet-control.json"
CONTROL_TOKEN_FILE = "prometheus/fleet-control-token"
TOOL_TARGETS = "prometheus/targets/tools.json"
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
# <<name{selector}>> takes each backend's query for that selector, joined with `or`.
_PLACEHOLDER = re.compile(r"<<(\w+)(?:\{([^<>]*)\})?>>")
# Value mappings that show each registered backend's icon.
BACKEND_BADGES = "<<backend_badges>>"


def _directory(path: Path, mode: int) -> None:
    if path.is_symlink():
        raise ValueError(f"Monitoring mount directory must be a real directory: {path}")
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    path.chmod(mode)


def console_url(env: Mapping[str, str]) -> str:
    """Return the console URL for the dashboard's Fleet control link."""
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


def tool_target(env: Mapping[str, str]) -> str | None:
    """Return the tool metrics origin named by `TOOL_METRICS_URL_ENV`, or None when it is unset."""
    url = env.get(TOOL_METRICS_URL_ENV, "")
    if not url:
        return None
    try:
        return metrics_authority(url)
    except ValueError as exc:
        raise ValueError(f"{TOOL_METRICS_URL_ENV}: {exc}") from None


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


def _console_link(console: str) -> dict[str, Any]:
    """Return the dashboard link that opens the fleet control console in a new tab."""
    return {
        "title": "Fleet control",
        "type": "link",
        "icon": "external link",
        "tooltip": "Open the fleet control console",
        "url": console,
        "tags": [],
        "asDropdown": False,
        "targetBlank": True,
        "includeVars": False,
        "keepTime": False,
    }


def _placeholders(value: object) -> set[str]:
    if isinstance(value, str):
        return {match[0] for match in _PLACEHOLDER.findall(value)}
    if isinstance(value, Mapping):
        value = list(value.values())
    if isinstance(value, list):
        return {name for item in value for name in _placeholders(item)}
    return set()


def _icon(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _backend_badges() -> dict[str, Any]:
    options = {name: {"text": _icon(load_backend(name).icon)} for name in backend_names()}
    return {"type": "value", "options": options}


def render_dashboard(source: Mapping[str, Any]) -> dict[str, Any]:
    mappings = [load_backend(name).metrics.dashboard_series for name in backend_names()]
    series = {key for mapping in mappings for key in mapping}

    def query(name: str, selector: str) -> str:
        queries = [m[name].replace("@sel", selector) for m in mappings if name in m]
        return "(" + " or ".join(f"({q})" for q in queries) + ")"

    def fill(value: Any) -> Any:
        if value == BACKEND_BADGES:
            return _backend_badges()
        if isinstance(value, str):
            return _PLACEHOLDER.sub(
                lambda match: match[0] if match[2] is None else query(match[1], match[2]),
                value,
            )
        if isinstance(value, list):
            return [fill(item) for item in value]
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        return value

    dashboard = copy.deepcopy(dict(source))
    spec = dashboard["spec"]
    # A panel whose queries no backend maps shows nothing, so it is left out.
    dropped = {
        name
        for name, element in spec["elements"].items()
        if _placeholders(element) - series - {"backend_badges"}
    }
    spec["elements"] = {
        name: element for name, element in spec["elements"].items() if name not in dropped
    }
    for row in spec["layout"]["spec"]["rows"]:
        grid = row["spec"]["layout"]["spec"]
        grid["items"] = [
            item for item in grid["items"] if item["spec"]["element"]["name"] not in dropped
        ]
    rendered = fill(dashboard)
    left = _placeholders(rendered)
    if left:
        raise ValueError(f"dashboard leaves fields {', '.join(sorted(left))}")
    return cast(dict[str, Any], rendered)


def narwhal_dashboard(source: Mapping[str, Any], console: str) -> dict[str, Any]:
    """Return the shipped dashboard with the fleet control markers and a link to `console`."""
    dashboard = render_dashboard(source)
    spec = dashboard["spec"]
    spec["annotations"] = [*spec["annotations"], *map(_annotation, CONTROL_ANNOTATIONS)]
    spec["links"] = [*spec["links"], _console_link(console)]
    return dashboard


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


def _write_targets(target: Path, authority: str | None) -> None:
    targets = [] if authority is None else [{"targets": [authority]}]
    _write(target, (json.dumps(targets, indent=2) + "\n").encode())


def stage_artifacts(
    contract: TargetContract,
    console: str = DEFAULT_CONSOLE_URL,
    control: ControlTarget | None = None,
    tools: str | None = None,
    root: Path = MOUNTS,
    source: Path = BASE,
) -> None:
    """Copy the configs and discovery targets with container permissions."""
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
        data = (source / origin).read_bytes()
        if origin == "prometheus.yml":
            data = data.replace(b"<<router>>", contract.router.encode())
        _write(root / destination, data)
    shipped = json.loads((source / "grafana-narwhal.json").read_text(encoding="utf-8"))
    dashboard = narwhal_dashboard(shipped, console)
    _write(root / NARWHAL_DASHBOARD, (json.dumps(dashboard, indent=2) + "\n").encode())
    _write_targets(root / CONTROL_TARGETS, None if control is None else control.authority)
    # Prometheus reads this token.
    _write(root / CONTROL_TOKEN_FILE, ("" if control is None else control.token).encode())
    _write_targets(root / TOOL_TARGETS, tools)
    write_contract(contract, root / "prometheus" / "targets")
