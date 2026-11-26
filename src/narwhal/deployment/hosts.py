"""Resolve management hosts and run time-limited SSH commands against pinned host keys."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

ROLE = re.compile(r"router|engine-[1-9][0-9]*")
# Seconds one remote command of each stage may run. NARWHAL_STAGE_<STAGE>_TIMEOUT_SECONDS sets
# one stage's limit and NARWHAL_STAGE_TIMEOUT_SECONDS every stage's, as for local stages.
STAGE_LIMITS = {"access": 60.0, "discover": 3600.0, "install": 1800.0}
# Seconds OpenSSH waits for a connection before it gives up, for every command.
CONNECT_TIMEOUT_S = 30
CLIENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
    }
)
# OpenSSH reports a host key that the known-hosts file lacks or contradicts with these lines.
HOST_KEY_FAILURES = ("Host key verification failed", "REMOTE HOST IDENTIFICATION HAS CHANGED")


@dataclass(frozen=True)
class Host:
    id: str
    ssh_env: str
    password_env: str | None
    roles: tuple[str, ...]


def load_hosts(path: Path, env: Mapping[str, str]) -> list[Host]:
    """Resolve each role to one host and reject duplicate destination entries."""
    hosts: list[Host] = []
    ids: set[str] = set()
    destinations: set[str] = set()
    roles: set[str] = set()
    for entry in json.loads(path.read_text())["hosts"]:
        host = Host(entry["id"], entry["ssh_env"], entry.get("password_env"), tuple(entry["roles"]))
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", host.id) or host.id in ids:
            raise ValueError("Inventory host IDs must be unique lowercase names")
        if not host.roles:
            raise ValueError(f"{host.id}: assign at least one role")
        for name in (host.ssh_env, host.password_env):
            if name is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                raise ValueError(f"{host.id}: use environment variable names for access")
            if name is not None and not env.get(name):
                raise ValueError(f"{host.id}: {name} is unset or empty")
        destination = env[host.ssh_env]
        if destination.startswith("-") or any(c.isspace() for c in destination):
            raise ValueError(f"{host.id}: SSH destination must be an alias or user@host")
        if destination in destinations:
            raise ValueError(f"{host.id}: combine roles that use the same management destination")
        for role in host.roles:
            if not ROLE.fullmatch(role) or role in roles:
                raise ValueError(f"{host.id}: each router or numbered engine role needs one owner")
            roles.add(role)
        hosts.append(host)
        ids.add(host.id)
        destinations.add(destination)
    if "router" not in roles or not any(role.startswith("engine-") for role in roles):
        raise ValueError("Inventory needs a router and at least one engine role")
    return hosts


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def stage_limit(env: Mapping[str, str], stage: str) -> float:
    """Return the seconds one remote command of `stage` may run."""
    default = STAGE_LIMITS[stage]
    for name in (
        f"NARWHAL_STAGE_{stage.upper()}_TIMEOUT_SECONDS",
        "NARWHAL_STAGE_TIMEOUT_SECONDS",
    ):
        if name in env:
            try:
                value = float(env[name])
            except ValueError:
                value = math.nan
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            return value
    return default


class RemoteError(RuntimeError):
    """A remote command failed on the named host."""

    def __init__(self, host: str, stage: str, operation: str, log: Path, cause: str = ""):
        self.stage = stage
        self.context = {"host": host, "operation": operation, "log": str(log)}
        super().__init__(f"{host}: blocked at {operation}{cause}; inspect {log}")


class RemoteTimeout(TimeoutError):
    """A remote command exceeded its stage's time limit on the named host."""

    def __init__(self, host: str, stage: str, operation: str, log: Path, limit: float):
        self.stage = stage
        self.context = {
            "host": host,
            "operation": operation,
            "log": str(log),
            "budget_seconds": limit,
        }
        super().__init__(
            f"{host}: {operation} exceeded the {limit:g}s {stage} time limit; inspect {log}"
        )


class SSH:
    """OpenSSH access that accepts only host keys from the supplied known-hosts file."""

    def __init__(self, env: Mapping[str, str], logs: Path):
        self.env = env
        self.client_env = {
            name: value
            for name, value in os.environ.items()
            if name in CLIENT_KEYS or name.startswith("LC_")
        }
        store = env.get("NARWHAL_SSH_KNOWN_HOSTS", "")
        if not store or not Path(store).is_file():
            raise ValueError("NARWHAL_SSH_KNOWN_HOSTS must name the supplied known-hosts file")
        self.logs = logs
        self.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        # OpenSSH checks the server's key against this file alone before authenticating, and
        # never writes to it.
        self.options = [
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"UserKnownHostsFile={Path(store).resolve()}",
            "-o",
            f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        ]

    def log(self, host: Host) -> Path:
        return self.logs / f"{host.id}.log"

    def _arguments(self, host: Host, options: list[str], *, prompts: bool = False) -> list[str]:
        args = ["ssh", *self.options, *options]
        if host.password_env:
            args += ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
        elif not prompts:
            # Key authentication never prompts; a missing key or agent fails the command.
            args += ["-o", "BatchMode=yes"]
        return [*args, self.env[host.ssh_env]]

    def _authenticated(
        self, host: Host, args: list[str], password: IO[bytes]
    ) -> tuple[list[str], tuple[int, ...]]:
        """Return the command and descriptors, passing a password through a private file."""
        if not host.password_env:
            return args, ()
        password.write(self.env[host.password_env].encode() + b"\n")
        password.flush()
        password.seek(0)
        return ["sshpass", "-d", str(password.fileno()), *args], (password.fileno(),)

    def _record(self, host: Host, heading: str) -> IO[str]:
        fd = os.open(self.log(host), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        log = os.fdopen(fd, "w")
        log.write(f"\n[{heading}]\n")
        log.flush()
        return log

    def run(
        self,
        host: Host,
        stage: str,
        operation: str,
        script: str,
        *,
        payload: Path | None = None,
        reason: str | None = None,
    ) -> str:
        """Run `script` on `host` within its stage's time limit and return its output.

        The host's log retains the script, exit status and output. With `reason`, a failure
        quotes the last error output line that starts with it.
        """
        limit = stage_limit(self.env, stage)
        log_path = self.log(host)
        with (
            self._record(host, f"{stage}: {operation}") as log,
            tempfile.TemporaryFile() as password,
        ):
            log.write(script + "\n")
            log.flush()
            args, pass_fds = self._authenticated(
                host, [*self._arguments(host, []), "sh -c " + shlex.quote(script)], password
            )
            with payload.open("rb") if payload else open(os.devnull, "rb") as stream:
                process = subprocess.Popen(
                    args,
                    stdin=stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=pass_fds,
                    env=self.client_env,
                    start_new_session=True,
                )
                expired = False
                try:
                    stdout, stderr = process.communicate(timeout=limit)
                except subprocess.TimeoutExpired:
                    expired = True
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
            log.write(f"timeout={limit:g}s\n" if expired else f"exit={process.returncode}\n")
            for output in (stdout, stderr):
                if output:
                    log.write(output.decode(errors="replace"))
        if expired:
            raise RemoteTimeout(host.id, stage, operation, log_path, limit)
        if process.returncode:
            lines = stderr.decode(errors="replace").splitlines()
            causes = [line[len(reason) :] for line in lines if reason and line.startswith(reason)]
            if any(failure in line for line in lines for failure in HOST_KEY_FAILURES):
                causes.append(
                    "the host key differs from NARWHAL_SSH_KNOWN_HOSTS or is missing from it"
                )
            cause = f": {causes[-1]}" if causes else ""
            raise RemoteError(host.id, stage, operation, log_path, cause)
        return stdout.decode(errors="replace").strip()

    def shell(self, host: Host, script: str) -> None:
        """Run an interactive `script` on `host` in this terminal, with no time limit."""
        with (
            self._record(host, "role shell") as log,
            tempfile.TemporaryFile() as password,
        ):
            log.write(script + "\n")
            log.flush()
            args = self._arguments(host, ["-t"], prompts=True)
            args, pass_fds = self._authenticated(
                host, [*args, "sh -c " + shlex.quote(script)], password
            )
            result = subprocess.run(args, pass_fds=pass_fds, env=self.client_env)
            log.write(f"exit={result.returncode}\n")
        if result.returncode:
            raise RemoteError(host.id, "shell", "role shell", self.log(host))

    def tunnel(self, host: Host, forwards: list[str]) -> None:
        """Hold local port forwards to `host` until SSH exits or the operator interrupts."""
        options = [
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        for forward in forwards:
            options += ["-L", forward]
        with (
            self._record(host, "service tunnel") as log,
            tempfile.TemporaryFile() as password,
        ):
            log.write(" ".join(forwards) + "\n")
            log.flush()
            args, pass_fds = self._authenticated(host, self._arguments(host, options), password)
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL, pass_fds=pass_fds, env=self.client_env
            )
            log.write(f"exit={result.returncode}\n")
        if result.returncode:
            raise RemoteError(host.id, "tunnel", "service tunnel", self.log(host))
