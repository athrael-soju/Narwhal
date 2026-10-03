"""Check that each configured listener is free or held by this Compose project."""

from __future__ import annotations

import errno
import json
import re
import socket
import subprocess
from collections.abc import Callable, Sequence

from .services import (
    COMMAND_TIMEOUT_S,
    Listener,
    Service,
    StartupError,
    configured_listener,
)
from .stack import Runner, Stack


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
    """Return whether a socket address matches the listener; wildcard hosts match any host."""
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
    wildcards = {"*", "0.0.0.0", "::"}
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
            configured_listener(config, "prometheus"),
            configured_listener(config, "grafana"),
        }:
            name = str(document.get("Name", "")).lstrip("/") or "unnamed"
            image = str(config.get("Image", "unknown"))
            cid = str(document.get("Id", ""))[:12]
            return f"container {name} ({image}, {cid})"
    return ""


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
