"""Listener and service contracts for the observability stack."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

GRAFANA_PORT = 3000
COMMAND_TIMEOUT_S = 10.0
PROMETHEUS_IMAGE = "prom/prometheus:v3.14.0"
PROMETHEUS_VERSION = "3.14.0"
GRAFANA_IMAGE = "grafana/grafana:13.2.1"
GRAFANA_VERSION = "13.2.1"
RENDERER_IMAGE = "grafana/grafana-image-renderer:v5.12.5"


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
        if host in {"", "0.0.0.0"}:
            host = "127.0.0.1"
        elif host in {"::", "[::]"}:
            host = "::1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self.port}"

    @property
    def url(self) -> str:
        """Return an HTTP URL reachable from the host."""
        return f"http://{self.authority}"


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


def parse_prometheus_listener(value: str) -> Listener:
    """Return the listener for an address:port or [address]:port value."""
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


def configured_listener(config: Mapping[str, object], service: str) -> str:
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
