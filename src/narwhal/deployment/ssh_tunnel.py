"""Own temporary loopback SSH forwards inside a managed measurement helper."""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .management_access import open_input
from .management_records import OperationError


def _ports_owned(pid: int, ports: set[int]) -> bool:
    """Require every selected listening socket to belong to the launched process tree."""
    pending = [pid]
    seen: set[int] = set()
    sockets: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if len(seen) > 128:
            return False
        try:
            with Path(f"/proc/{current}/task/{current}/children").open() as source:
                children = source.read(65_537)
            if len(children) > 65_536:
                return False
            pending.extend(int(value) for value in children.split())
            for descriptor in Path(f"/proc/{current}/fd").iterdir():
                with contextlib.suppress(OSError):
                    target = descriptor.readlink().as_posix()
                    if target.startswith("socket:[") and target.endswith("]"):
                        sockets.add(target[8:-1])
        except (OSError, ValueError):
            continue
    owned = set()
    with Path("/proc/net/tcp").open() as source:
        listeners = source.read(1_048_577)
    if len(listeners) > 1_048_576:
        return False
    for line in listeners.splitlines()[1:]:
        fields = line.split()
        address, port = fields[1].split(":")
        if address == "0100007F" and fields[3] == "0A" and fields[9] in sockets:
            owned.add(int(port, 16))
    return ports <= owned


@contextlib.contextmanager
def forward(
    *,
    destination: str,
    password: str | None,
    known_hosts: Path,
    ports: tuple[tuple[int, int], ...],
    connect_timeout_s: int,
    deadline: float,
) -> Iterator[dict[str, Any]]:
    """Forward fixed loopback ports without user SSH configuration or automatic trust."""
    if (
        not destination
        or destination.startswith("-")
        or any(value.isspace() or ord(value) < 32 for value in destination)
        or not ports
        or any(
            type(value) is not int or not 1 <= value <= 65535 for pair in ports for value in pair
        )
        or len({local for local, _ in ports}) != len(ports)
    ):
        raise OperationError("invalid_input", "Registered tunnel binding is invalid")
    if not math.isfinite(deadline) or time.monotonic() >= deadline:
        raise OperationError("stage_timeout", "SSH tunnel deadline expired before launch")
    executable = shutil.which("ssh", path=os.defpath)
    if executable is None:
        raise OperationError("prerequisite_failed", "OpenSSH client is unavailable")
    with open_input(known_hosts) as (known_fd, trust), tempfile.TemporaryFile() as secret:
        if not trust.strip():
            raise OperationError("prerequisite_failed", "SSH trust store is empty")
        argv = [
            executable,
            "-F",
            os.devnull,
            "-N",
            "-T",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"UserKnownHostsFile=/proc/{os.getpid()}/fd/{known_fd}",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            f"ConnectTimeout={connect_timeout_s}",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=1",
        ]
        passed = []
        if password is None:
            argv += ["-o", "BatchMode=yes"]
        else:
            sshpass = shutil.which("sshpass", path=os.defpath)
            if sshpass is None:
                raise OperationError("prerequisite_failed", "Password SSH requires sshpass")
            secret.write(password.encode() + b"\n")
            secret.seek(0)
            passed.append(secret.fileno())
            argv = [sshpass, "-d", str(secret.fileno()), *argv]
            argv += [
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        for local, remote in ports:
            argv += ["-L", f"127.0.0.1:{local}:127.0.0.1:{remote}"]
        argv.append(destination)
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in {"HOME", "USER", "LOGNAME", "SSH_AUTH_SOCK", "LANG"}
            or name.startswith("LC_")
        }
        environment["PATH"] = os.defpath
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            pass_fds=tuple(passed),
            start_new_session=True,
        )
        started = time.monotonic()
        try:
            while True:
                if process.poll() is not None:
                    raise OperationError("source_unavailable", "SSH tunnel exited before use")
                if time.monotonic() >= deadline:
                    raise OperationError("stage_timeout", "SSH tunnel readiness deadline expired")
                if _ports_owned(process.pid, {local for local, _ in ports}):
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            yield {
                "pid": process.pid,
                "ready_elapsed_s": time.monotonic() - started,
                "ports": list(ports),
            }
            if process.poll() is not None:
                raise OperationError("source_unavailable", "SSH tunnel exited during measurement")
        finally:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=min(1, max(0.01, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=0.2)
