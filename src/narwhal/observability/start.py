"""Start the shipped observability stack after proving listener and container identity."""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .artifacts import stage_artifacts
from .make_targets import DEFAULT_DATA_DIR, TargetContract, load_contract

BASE = Path(__file__).resolve().parent
COMPOSE_FILE = BASE / "compose.yml"
GRAFANA_PORT = 3000
DEFAULT_READY_TIMEOUT_S = 60.0
COMMAND_TIMEOUT_S = 10.0
COMPOSE_UP_TIMEOUT_S = 300.0
PROMETHEUS_IMAGE = "prom/prometheus:v3.14.0"
PROMETHEUS_VERSION = "3.14.0"
GRAFANA_IMAGE = "grafana/grafana:13.2.1"
GRAFANA_VERSION = "13.2.1"


class StartupError(RuntimeError):
    """Describe an operator-correctable startup failure."""


@dataclass(frozen=True)
class Listener:
    """Configured host listener for one Compose service."""

    host: str
    port: int

    @property
    def display(self) -> str:
        """Return an unambiguous address and port."""
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{host}:{self.port}"

    @property
    def authority(self) -> str:
        """Return an HTTP authority reachable from the host."""
        host = self.host
        if host in {"", "0.0.0.0"}:  # noqa: S104 - recognising wildcard, not binding
            host = "127.0.0.1"
        elif host in {"::", "[::]"}:
            host = "::1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self.port}"


@dataclass(frozen=True)
class Service:
    """Expected Compose identity and HTTP contract for one service."""

    name: str
    label: str
    listener: Listener
    image: str
    version: str


@dataclass(frozen=True)
class Container:
    """Compose container identity used during readiness polling."""

    cid: str
    image: str
    status: str
    restarting: bool
    listener: str


@dataclass(frozen=True)
class ActivitySample:
    """Scraped request and engine counters at one observation point."""

    served: float
    prompt_tokens: tuple[tuple[str, float], ...]

    def prompt_total(self) -> float:
        """Return the total prompt tokens observed across the fleet."""
        return sum(value for _, value in self.prompt_tokens)


class Stack(Protocol):
    """Compose operations consumed by the startup checks."""

    def up(self) -> None:
        """Create or update the project services."""

    def container(self, service: str) -> Container | None:
        """Return the project's container for one service."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
HttpGet = Callable[[str, float], tuple[int, str]]


class ComposeStack:
    """Run Docker Compose against the packaged, pinned project file."""

    def __init__(
        self, runner: Runner = subprocess.run, env: Mapping[str, str] | None = None
    ) -> None:
        self._runner = runner
        self._compose_env = dict(os.environ if env is None else env)
        data_dir = Path(
            self._compose_env.get("NARWHAL_OBSERVABILITY_DATA_DIR", str(DEFAULT_DATA_DIR))
        ).expanduser()
        if not data_dir.is_absolute():
            raise StartupError("NARWHAL_OBSERVABILITY_DATA_DIR must be absolute")
        self._compose_env["NARWHAL_OBSERVABILITY_DATA_DIR"] = str(data_dir)
        listener = parse_prometheus_listener(
            self._compose_env.get("NARWHAL_PROMETHEUS_LISTEN_ADDRESS", "127.0.0.1:9090")
        )
        self._compose_env["NARWHAL_PROMETHEUS_URL"] = f"http://{listener.authority}"
        self._prefix = [
            "docker",
            "compose",
            "--project-directory",
            str(BASE),
            "-f",
            str(COMPOSE_FILE),
        ]

    def _run(
        self,
        *args: str,
        timeout_s: float = COMMAND_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            [*self._prefix, *args],
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            env=self._compose_env,
        )

    def up(self) -> None:
        """Create or update Prometheus and Grafana."""
        result = self._run("up", "-d", timeout_s=COMPOSE_UP_TIMEOUT_S)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

    def container(self, service: str) -> Container | None:
        """Inspect a service container selected through this Compose project."""
        result = self._run("ps", "--all", "--quiet", service)
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            return None
        if len(ids) != 1:
            raise StartupError(f"Compose returned {len(ids)} containers for {service}")
        cid = ids[0]
        inspected = self._runner(
            ["docker", "inspect", cid],
            check=True,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_S,
        )
        documents = json.loads(inspected.stdout)
        if len(documents) != 1:
            raise StartupError(f"Docker returned an invalid inspection for {service}")
        document = documents[0]
        state = document.get("State", {})
        config = document.get("Config", {})
        return Container(
            cid=cid,
            image=str(config.get("Image", "")),
            status=str(state.get("Status", "unknown")),
            restarting=bool(state.get("Restarting", False)),
            listener=_configured_listener(config, service),
        )


def parse_prometheus_listener(value: str) -> Listener:
    """Parse Prometheus' configured address and port."""
    raw = value.strip()
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0 or end + 1 >= len(raw) or raw[end + 1] != ":":
            raise StartupError(f"invalid Prometheus listener {value!r}")
        host, port_text = raw[1:end], raw[end + 2 :]
    else:
        host, separator, port_text = raw.rpartition(":")
        if not separator:
            raise StartupError(f"Prometheus listener requires address:port, got {value!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise StartupError(f"invalid Prometheus listener port in {value!r}") from exc
    if not host or not 1 <= port <= 65535:
        raise StartupError(f"invalid Prometheus listener {value!r}")
    return Listener(host, port)


def configured_services(env: Mapping[str, str]) -> tuple[Service, Service]:
    """Build the pinned service contracts from operator-selected listeners."""
    prometheus = parse_prometheus_listener(
        env.get("NARWHAL_PROMETHEUS_LISTEN_ADDRESS", "127.0.0.1:9090")
    )
    grafana_host = env.get("NARWHAL_GRAFANA_BIND_ADDRESS", "127.0.0.1").strip()
    if not grafana_host:
        raise StartupError("NARWHAL_GRAFANA_BIND_ADDRESS requires an address")
    if grafana_host.startswith("[") and grafana_host.endswith("]"):
        grafana_host = grafana_host[1:-1]
    return (
        Service(
            "prometheus",
            "Prometheus",
            prometheus,
            PROMETHEUS_IMAGE,
            PROMETHEUS_VERSION,
        ),
        Service(
            "grafana",
            "Grafana",
            Listener(grafana_host, GRAFANA_PORT),
            GRAFANA_IMAGE,
            GRAFANA_VERSION,
        ),
    )


def listener_available(listener: Listener) -> bool:
    """Return whether every resolved endpoint can accept the configured listener."""
    try:
        addresses = socket.getaddrinfo(
            listener.host,
            listener.port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except socket.gaierror as exc:
        raise StartupError(f"cannot resolve listener {listener.display}: {exc}") from exc
    if not addresses:
        raise StartupError(f"listener {listener.display} resolved to zero addresses")
    seen: set[tuple[int, tuple[object, ...]]] = set()
    for family, socktype, proto, _, address in addresses:
        key = family, tuple(address)
        if key in seen:
            continue
        seen.add(key)
        probe = socket.socket(family, socktype, proto)
        try:
            probe.bind(address)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            raise StartupError(f"cannot probe listener {listener.display}: {exc}") from exc
        finally:
            probe.close()
    return True


def listener_owner(listener: Listener, runner: Runner = subprocess.run) -> str:
    """Describe the processes or socket units listening on the configured port."""
    try:
        result = runner(
            ["ss", "-H", "-ltnp", f"sport = :{listener.port}"],
            check=False,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        lines: list[str] = []
        lookup_error = f"owner lookup failed: {exc}"
    else:
        lines = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and _ss_line_matches_listener(line, listener)
        ]
        lookup_error = ""
    pids = sorted({int(value) for line in lines for value in re.findall(r"pid=(\d+)", line)})
    processes: list[str] = []
    for pid in pids:
        try:
            process = runner(
                ["ps", "-p", str(pid), "-o", "pid=,comm=,args="],
                check=False,
                text=True,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_S,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            process = ""
        if process:
            processes.append(process)
    if processes:
        return "; ".join(processes)
    for command in (
        ["systemctl", "list-sockets", "--all", "--no-legend", "--no-pager"],
        ["systemctl", "--user", "list-sockets", "--all", "--no-legend", "--no-pager"],
    ):
        try:
            sockets = runner(
                command,
                check=False,
                text=True,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in sockets.stdout.splitlines():
            fields = line.split()
            units = [field for field in fields if field.endswith(".socket")]
            if units and _line_mentions_listener(line, listener):
                return f"socket unit {units[0]}"
    container = _docker_listener_owner(listener, runner)
    if container:
        return container
    if lines:
        return "; ".join(lines[:3])
    return lookup_error or "owner unavailable from ss"


def _line_mentions_listener(line: str, listener: Listener) -> bool:
    return any(_address_matches_listener(field.rstrip(",;"), listener) for field in line.split())


def _listener_hosts(listener: Listener) -> set[str]:
    """Return configured and resolved addresses used for owner diagnostics."""
    host = listener.host.strip("[]")
    hosts = {host}
    try:
        addresses = socket.getaddrinfo(
            host,
            listener.port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return hosts
    hosts.update(str(address[0]).split("%", 1)[0] for *_, address in addresses)
    return hosts


def _ss_line_matches_listener(line: str, listener: Listener) -> bool:
    """Match an ss row to the configured address as well as its port."""
    fields = line.split()
    return len(fields) >= 4 and _address_matches_listener(fields[3], listener)


def _address_matches_listener(value: str, listener: Listener) -> bool:
    """Match one socket address to a configured listener."""
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or value[end + 1 : end + 2] != ":":
            return False
        host, port_text = value[1:end], value[end + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            return False
    if port_text != str(listener.port):
        return False
    host = host.strip("[]").split("%", 1)[0]
    expected = _listener_hosts(listener)
    wildcards = {"*", "0.0.0.0", "::"}  # noqa: S104 - address comparison only
    return host in expected or host in wildcards or bool(expected & wildcards)


def _docker_listener_owner(listener: Listener, runner: Runner) -> str:
    try:
        listed = runner(
            ["docker", "ps", "--quiet"],
            check=False,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not ids:
        return ""
    try:
        inspected = runner(
            ["docker", "inspect", *ids],
            check=False,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_S,
        )
        documents = json.loads(inspected.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return ""
    for document in documents:
        if document.get("HostConfig", {}).get("NetworkMode") != "host":
            continue
        config = document.get("Config", {})
        if listener.display in {
            _configured_listener(config, "prometheus"),
            _configured_listener(config, "grafana"),
        }:
            name = str(document.get("Name", "")).lstrip("/") or "unnamed"
            image = str(config.get("Image", "unknown"))
            cid = str(document.get("Id", ""))[:12]
            return f"container {name} ({image}, {cid})"
    return ""


def _configured_listener(config: Mapping[str, object], service: str) -> str:
    if service == "prometheus":
        command = config.get("Cmd")
        for value in command if isinstance(command, list) else []:
            argument = str(value)
            if argument.startswith("--web.listen-address="):
                return argument.split("=", 1)[1]
        return ""
    configured_environment = config.get("Env")
    environment = {
        key: value
        for item in (configured_environment if isinstance(configured_environment, list) else [])
        if "=" in str(item)
        for key, value in [str(item).split("=", 1)]
    }
    host = environment.get("GF_SERVER_HTTP_ADDR", "127.0.0.1")
    try:
        port = int(environment.get("GF_SERVER_HTTP_PORT", GRAFANA_PORT))
    except ValueError:
        return ""
    return Listener(host, port).display


def _current_project_owns(service: Service, stack: Stack) -> bool:
    container = stack.container(service.name)
    return bool(
        container
        and container.status == "running"
        and not container.restarting
        and container.listener == service.listener.display
    )


def check_listeners(
    services: Sequence[Service],
    stack: Stack,
    *,
    available: Callable[[Listener], bool] = listener_available,
    owner: Callable[[Listener], str] = listener_owner,
) -> None:
    """Reject listeners held outside the expected running Compose project."""
    for service in services:
        if available(service.listener):
            continue
        if _current_project_owns(service, stack):
            continue
        detail = owner(service.listener)
        raise StartupError(
            f"{service.label} listener {service.listener.display} is occupied by {detail}"
        )


def http_get(url: str, timeout_s: float) -> tuple[int, str]:
    """Fetch one local health document."""
    if urllib.parse.urlsplit(url).scheme != "http":
        raise StartupError("monitoring readiness requires an HTTP URL")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"User-Agent": "narwhal-observe/1"}
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
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


def _verify_http(service: Service, get: HttpGet, prometheus_url: str) -> None:
    base = f"http://{service.listener.authority}"
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
        status, body = get(f"{base}/api/dashboards/uid/narwhal-router", 2.0)
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
    base = f"http://{prometheus.listener.authority}"
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


def _query_vector(prometheus: Service, expression: str, get: HttpGet) -> list[dict]:
    """Read one Prometheus instant vector and reject malformed samples."""
    base = f"http://{prometheus.listener.authority}"
    encoded = urllib.parse.urlencode({"query": expression})
    status, body = get(f"{base}/api/v1/query?{encoded}", 2.0)
    if status != 200:
        raise StartupError(f"Prometheus query returned HTTP {status}: {expression}")
    document = json.loads(body)
    if document.get("status") != "success":
        raise StartupError(f"Prometheus query failed: {expression}")
    data = document.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("resultType") != "vector"
        or not isinstance(result, list)
    ):
        raise StartupError(f"Prometheus query returned no instant vector: {expression}")
    if not all(isinstance(row, dict) for row in result):
        raise StartupError(f"Prometheus query returned an invalid sample: {expression}")
    return result


def _sample_value(row: dict, expression: str) -> float:
    value = row.get("value")
    if not isinstance(value, list) or len(value) != 2:
        raise StartupError(f"Prometheus sample lacks a value: {expression}")
    try:
        number = float(value[1])
    except (TypeError, ValueError) as exc:
        raise StartupError(f"Prometheus sample is not numeric: {expression}") from exc
    if not math.isfinite(number) or number < 0:
        raise StartupError(f"Prometheus sample is invalid: {expression}")
    return number


def activity_snapshot(
    prometheus: Service, contract: TargetContract, *, get: HttpGet = http_get
) -> ActivitySample:
    """Capture live router and engine telemetry from the canonical scrape targets."""
    served_query = (
        f'sum(narwhal_served_total{{job="narwhal-router",instance={json.dumps(contract.router)}}})'
    )
    served_rows = _query_vector(prometheus, served_query, get)
    if len(served_rows) != 1:
        raise StartupError("Narwhal served counter is absent from the router scrape")
    served = _sample_value(served_rows[0], served_query)

    cache_query = 'max by (iid) (vllm:kv_cache_usage_perc{job="engines"})'
    cache_rows = _query_vector(prometheus, cache_query, get)
    present = {
        row["metric"].get("iid")
        for row in cache_rows
        if isinstance(row.get("metric"), dict) and isinstance(row["metric"].get("iid"), str)
    }
    expected = {iid for iid, _ in contract.engines}
    if present != expected:
        raise StartupError(
            f"vLLM cache telemetry covers {sorted(present)!r}; expected {sorted(expected)!r}"
        )
    for row in cache_rows:
        _sample_value(row, cache_query)

    prompt_query = 'sum by (iid) (vllm:prompt_tokens_total{job="engines"})'
    prompt_rows = _query_vector(prometheus, prompt_query, get)
    counts: dict[str, float] = dict.fromkeys(expected, 0.0)
    seen: set[str] = set()
    for row in prompt_rows:
        metric = row.get("metric")
        iid = metric.get("iid") if isinstance(metric, dict) else None
        if iid not in expected or iid in seen:
            raise StartupError(f"vLLM prompt telemetry has an unexpected engine: {iid!r}")
        seen.add(iid)
        counts[iid] = _sample_value(row, prompt_query)
    return ActivitySample(served, tuple(sorted(counts.items())))


def wait_activity(
    prometheus: Service,
    contract: TargetContract,
    before: ActivitySample,
    *,
    get: HttpGet = http_get,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    poll_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ActivitySample:
    """Require new routed completions and engine prompt work after a request."""
    deadline = clock() + timeout_s
    last_error = "router and engine counters have not advanced"
    while clock() < deadline:
        try:
            after = activity_snapshot(prometheus, contract, get=get)
            if after.served > before.served and after.prompt_total() > before.prompt_total():
                return after
        except (StartupError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            last_error = str(exc)
        sleep(poll_s)
    raise StartupError(f"request telemetry did not appear within {timeout_s:g}s: {last_error}")


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
    prometheus_url = f"http://{prometheus.listener.authority}"
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


def start(
    env: Mapping[str, str],
    stack: Stack,
    contract: TargetContract,
    *,
    get: HttpGet = http_get,
    timeout_s: float = DEFAULT_READY_TIMEOUT_S,
    target_writer: Callable[[TargetContract, Path], None] = stage_artifacts,
) -> dict[str, Container]:
    """Check listeners, launch Compose and verify the resulting services."""
    services = configured_services(env)
    check_listeners(services, stack)
    data_dir = Path(env.get("NARWHAL_OBSERVABILITY_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    if not data_dir.is_absolute():
        raise StartupError("NARWHAL_OBSERVABILITY_DATA_DIR must be absolute")
    target_writer(contract, data_dir / "mounts")
    stack.up()
    launched = wait_ready(services, stack, get=get, timeout_s=timeout_s)
    prometheus = next(service for service in services if service.name == "prometheus")
    wait_collection(prometheus, contract, get=get, timeout_s=timeout_s)
    for service in services:
        container = launched[service.name]
        print(
            f"{service.label} ready at http://{service.listener.authority} "
            f"({container.cid[:12]}, {container.image})"
        )
    return launched


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checked observability startup command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=DEFAULT_READY_TIMEOUT_S,
        help="seconds to wait for both versioned health contracts",
    )
    parser.add_argument(
        "--fleet",
        type=Path,
        default=os.environ.get("NARWHAL_FLEET"),
        help="deployed fleet document (default: NARWHAL_FLEET)",
    )
    parser.add_argument(
        "--router-url",
        default=os.environ.get("NARWHAL_ROUTER_URL"),
        help="router HTTP origin scraped by Prometheus (default: NARWHAL_ROUTER_URL)",
    )
    args = parser.parse_args(argv)
    if args.ready_timeout <= 0:
        parser.error("--ready-timeout must be positive")
    if args.fleet is None:
        parser.error("--fleet or NARWHAL_FLEET is required")
    if args.router_url is None:
        parser.error("--router-url or NARWHAL_ROUTER_URL is required")
    try:
        contract = load_contract(args.fleet, args.router_url)
        start(os.environ, ComposeStack(env=os.environ), contract, timeout_s=args.ready_timeout)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"observability startup failed: {detail}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        command = " ".join(str(part) for part in exc.cmd)
        print(
            f"observability startup failed: command exceeded {exc.timeout:g}s: {command}",
            file=sys.stderr,
        )
        return 1
    except (StartupError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"observability startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
