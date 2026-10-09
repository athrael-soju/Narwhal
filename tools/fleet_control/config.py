"""Private control-service configuration and the bearer token it names."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .workloads import LoadConfig, read_load

DEFAULT_CONFIG = Path("config/fleet-control.local.json")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8020
DEFAULT_TOKEN_ENV = "NARWHAL_CONTROL_TOKEN"
DEFAULT_RUNS_DIR = Path("runs/fleet-control")
DEFAULT_HOOK_TIMEOUT_S = 900.0
DEFAULT_ROUTER_URL = "http://127.0.0.1:8000"
# Lifecycle readmission validates the engine and its KV peers before it answers.
DEFAULT_ROUTER_TIMEOUT_S = 600.0
# The largest router journal extract copied for one session or load job.
DEFAULT_JOURNAL_MAX_BYTES = 256 * 1024 * 1024
# A token from `openssl rand -hex 32` or `secrets.token_urlsafe(32)` clears this floor.
MIN_TOKEN_LENGTH = 32
# The UID of the shipped Narwhal Orchestrator dashboard, tools/observability/grafana-narwhal.json.
DEFAULT_DASHBOARD_UID = "narwhal-router"
# The shipped dashboard's own time range and refresh interval.
DEFAULT_PANEL_FROM = "now-15m"
DEFAULT_PANEL_REFRESH = "5s"
_KEYS = {
    "host",
    "port",
    "token_env",
    "runs_dir",
    "fleet",
    "hooks",
    "router",
    "prometheus_url",
    "load",
    "console",
}
_HOOK_KEYS = {"argv", "timeout_s"}
_ROUTER_KEYS = {"url", "timeout_s", "journal", "journal_max_bytes"}
_CONSOLE_KEYS = {
    "grafana_url",
    "dashboard_uid",
    "panels",
    "from",
    "refresh",
    "embed_in_grafana",
    "auto_connect",
}
_DASHBOARD_UID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_RELATIVE_TIME = re.compile(r"^now(-[1-9][0-9]*[smhdwMy])?$")
_INTERVAL = re.compile(r"^[1-9][0-9]*[smhd]$")


class ConfigError(ValueError):
    """Describe an operator-correctable configuration problem."""


@dataclass(frozen=True)
class Hook:
    """A deployment-specific command the service runs for one named operation."""

    name: str
    argv: tuple[str, ...]
    timeout_s: float = DEFAULT_HOOK_TIMEOUT_S


@dataclass(frozen=True)
class RouterEndpoint:
    """The Narwhal router whose state and lifecycle API the service calls.

    `journal` is the router's request journal on this host, from which sessions and load jobs
    copy the rows they produced; None records no extract.
    """

    url: str = DEFAULT_ROUTER_URL
    timeout_s: float = DEFAULT_ROUTER_TIMEOUT_S
    journal: Path | None = None
    journal_max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES


@dataclass(frozen=True)
class ConsoleConfig:
    """Grafana panels the console embeds, Grafana framing, and token delivery for the console page.

    `grafana_url` is the Grafana address as the operator's browser reaches it, usually the
    workstation end of the operator tunnel rather than the address on the router host. Without
    `panels`, the console shows its controls only. With `embed_in_grafana`, pages from the
    `grafana_url` origin may frame the console. With `auto_connect`, the console page carries the
    bearer token and connects without asking for it.
    """

    grafana_url: str
    panels: tuple[int, ...] = ()
    dashboard_uid: str = DEFAULT_DASHBOARD_UID
    time_from: str = DEFAULT_PANEL_FROM
    refresh: str = DEFAULT_PANEL_REFRESH
    embed_in_grafana: bool = False
    auto_connect: bool = False


@dataclass(frozen=True)
class ControlConfig:
    """Listener, credential, record location, baseline fleet, router, hooks, load and console.

    `prometheus_url` is the Prometheus queried for firing alerts, or None to skip the query.
    """

    fleet: Path
    hooks: Mapping[str, Hook]
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token_env: str = DEFAULT_TOKEN_ENV
    runs_dir: Path = DEFAULT_RUNS_DIR
    router: RouterEndpoint = RouterEndpoint()
    prometheus_url: str | None = None
    load: LoadConfig | None = None
    console: ConsoleConfig | None = None

    def hook(self, name: str) -> Hook:
        """Return the configured hook, or raise when the deployment did not name one."""
        try:
            return self.hooks[name]
        except KeyError:
            raise ConfigError(f"no {name!r} hook is configured") from None


def is_loopback(host: str) -> bool:
    """Return whether `host` names only the local machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def load_config(path: Path, env: Mapping[str, str]) -> ControlConfig:
    """Read and validate the private control configuration, reporting every problem."""
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: the configuration must be a JSON object")
    problems = [f"unknown key {key!r}" for key in sorted(set(raw) - _KEYS)]
    host = raw.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or not is_loopback(host):
        problems.append(f"host must be a loopback address, got {host!r}")
    port = raw.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        problems.append(f"port must be an integer from 1 through 65535, got {port!r}")
    token_env = raw.get("token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(token_env, str) or not token_env:
        problems.append("token_env must name an environment variable")
    runs_dir = raw.get("runs_dir", str(DEFAULT_RUNS_DIR))
    if not isinstance(runs_dir, str) or not runs_dir:
        problems.append("runs_dir must be a path")
    fleet = raw.get("fleet", env.get("NARWHAL_FLEET"))
    if not isinstance(fleet, str) or not fleet:
        problems.append("fleet must name the baseline fleet configuration (or set NARWHAL_FLEET)")
    hooks = _read_hooks(problems, raw.get("hooks"))
    router = _read_router(problems, raw.get("router", {}))
    prometheus = raw.get("prometheus_url")
    if prometheus is not None and (not isinstance(prometheus, str) or not _is_http_url(prometheus)):
        problems.append(f"prometheus_url must be an http or https URL, got {prometheus!r}")
    load = read_load(problems, raw["load"]) if "load" in raw else None
    console = read_console(problems, raw["console"]) if "console" in raw else None
    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))
    return ControlConfig(
        fleet=Path(str(fleet)),
        hooks=hooks,
        host=str(host),
        port=int(port),
        token_env=str(token_env),
        runs_dir=Path(str(runs_dir)),
        router=router,
        prometheus_url=None if prometheus is None else str(prometheus).rstrip("/"),
        load=load,
        console=console,
    )


def _read_hooks(problems: list[str], raw: object) -> dict[str, Hook]:
    if not isinstance(raw, dict):
        problems.append("hooks must be an object")
        return {}
    hooks = {}
    for name, spec in raw.items():
        label = f"hooks.{name}"
        if not isinstance(spec, dict):
            problems.append(f"{label} must be an object")
            continue
        problems.extend(f"unknown key {label}.{key}" for key in sorted(set(spec) - _HOOK_KEYS))
        argv = spec.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(part, str) and part for part in argv)
        ):
            problems.append(f"{label}.argv must be a non-empty list of strings")
            continue
        timeout = spec.get("timeout_s", DEFAULT_HOOK_TIMEOUT_S)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            problems.append(f"{label}.timeout_s must be a positive number of seconds")
            continue
        hooks[name] = Hook(name, tuple(argv), float(timeout))
    return hooks


def _read_router(problems: list[str], raw: object) -> RouterEndpoint:
    if not isinstance(raw, dict):
        problems.append("router must be an object")
        return RouterEndpoint()
    problems.extend(f"unknown key router.{key}" for key in sorted(set(raw) - _ROUTER_KEYS))
    url = raw.get("url", DEFAULT_ROUTER_URL)
    if not isinstance(url, str) or not _is_http_url(url):
        problems.append(f"router.url must be an http or https URL, got {url!r}")
    timeout = raw.get("timeout_s", DEFAULT_ROUTER_TIMEOUT_S)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        problems.append("router.timeout_s must be a positive number of seconds")
        timeout = DEFAULT_ROUTER_TIMEOUT_S
    journal = raw.get("journal")
    # The file may not exist yet: the router creates it when it starts.
    if journal is not None and (not isinstance(journal, str) or not Path(journal).is_absolute()):
        problems.append("router.journal must be the absolute path of the router's journal file")
        journal = None
    max_bytes = raw.get("journal_max_bytes", DEFAULT_JOURNAL_MAX_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        problems.append("router.journal_max_bytes must be a positive number of bytes")
        max_bytes = DEFAULT_JOURNAL_MAX_BYTES
    return RouterEndpoint(
        str(url).rstrip("/"),
        float(timeout),
        None if journal is None else Path(journal),
        max_bytes,
    )


def read_console(problems: list[str], raw: object) -> ConsoleConfig | None:
    """Validate the `console` section, appending each problem to `problems`."""
    if not isinstance(raw, dict):
        problems.append("console must be an object")
        return None
    count = len(problems)
    problems.extend(f"unknown key console.{key}" for key in sorted(set(raw) - _CONSOLE_KEYS))
    url = raw.get("grafana_url")
    if not isinstance(url, str) or not _is_http_url(url) or any(c in url for c in "?#@"):
        problems.append(
            "console.grafana_url must be an http or https URL without credentials, "
            "query or fragment"
        )
    uid = raw.get("dashboard_uid", DEFAULT_DASHBOARD_UID)
    if not isinstance(uid, str) or not _DASHBOARD_UID.match(uid):
        problems.append("console.dashboard_uid must be a Grafana dashboard UID")
    panels = raw.get("panels")
    if "panels" in raw and (
        not isinstance(panels, list)
        or not panels
        or not all(isinstance(p, int) and not isinstance(p, bool) and p >= 1 for p in panels)
        or len(set(panels)) != len(panels)
    ):
        problems.append("console.panels must be a non-empty list of distinct Grafana panel IDs")
    time_from = raw.get("from", DEFAULT_PANEL_FROM)
    if not isinstance(time_from, str) or not _RELATIVE_TIME.match(time_from):
        problems.append("console.from must be a relative Grafana time such as now-15m")
    refresh = raw.get("refresh", DEFAULT_PANEL_REFRESH)
    if not isinstance(refresh, str) or not _INTERVAL.match(refresh):
        problems.append("console.refresh must be an interval such as 5s or 1m")
    embed = raw.get("embed_in_grafana", False)
    if not isinstance(embed, bool):
        problems.append("console.embed_in_grafana must be true or false")
    auto_connect = raw.get("auto_connect", False)
    if not isinstance(auto_connect, bool):
        problems.append("console.auto_connect must be true or false")
    if len(problems) > count:
        return None
    return ConsoleConfig(
        grafana_url=str(url).rstrip("/"),
        panels=tuple(panels or ()),
        dashboard_uid=str(uid),
        time_from=str(time_from),
        refresh=str(refresh),
        embed_in_grafana=embed,
        auto_connect=auto_connect,
    )


def _is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def read_token(config: ControlConfig, env: Mapping[str, str] = os.environ) -> str:
    """Return the bearer token from the configured environment variable."""
    token = env.get(config.token_env, "")
    if not token:
        raise ConfigError(f"{config.token_env} must hold the control bearer token")
    if len(token) < MIN_TOKEN_LENGTH or token != token.strip():
        raise ConfigError(
            f"{config.token_env} must hold at least {MIN_TOKEN_LENGTH} characters "
            "without surrounding whitespace"
        )
    return token
