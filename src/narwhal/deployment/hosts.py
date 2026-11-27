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
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

ROLE = re.compile(r"router|engine-[1-9][0-9]*")
# Seconds one remote command of each stage may run. NARWHAL_STAGE_<STAGE>_TIMEOUT_SECONDS sets
# one stage's limit and NARWHAL_STAGE_TIMEOUT_SECONDS every stage's, as for local stages.
STAGE_LIMITS = {"access": 60.0, "discover": 3600.0, "install": 1800.0}
# Seconds OpenSSH waits for a connection before it gives up, for every command.
CONNECT_TIMEOUT_S = 30
# The host sends SIGKILL to a command this many seconds after its time limit's SIGTERM.
KILL_AFTER_S = 5
# Seconds past a time limit that the workstation waits for the host to stop the command and
# answer, before it ends the connection itself.
HOST_GRACE_S = 30.0
# Exit statuses of coreutils `timeout` when it stopped the command with SIGTERM or SIGKILL.
TIMEOUT_STATUSES = (124, 137)
# The remote scripts start a line with this to name the cause of their failure.
HOST_CAUSE = "narwhal-host: "
# The only variables `ssh` receives. ssh_config's SendEnv adds to the command line's instead of
# yielding to it, so the client environment holds nothing that SendEnv could usefully forward.
# XDG_RUNTIME_DIR locates agent sockets that ssh_config names through ${XDG_RUNTIME_DIR}.
CLIENT_KEYS = frozenset({"PATH", "HOME", "TERM", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR"})
# OpenSSH reports a host key that the known-hosts file lacks or contradicts with these lines.
HOST_KEY_FAILURES = ("Host key verification failed", "REMOTE HOST IDENTIFICATION HAS CHANGED")


@dataclass(frozen=True)
class Host:
    id: str
    ssh_env: str
    password_env: str | None
    roles: tuple[str, ...]


def destination(value: str, name: str) -> str:
    """Return an SSH destination after checking that OpenSSH cannot read it as options."""
    if not value or value.startswith("-") or any(c.isspace() for c in value):
        raise ValueError(f"{name} must be an SSH alias or user@host")
    return value


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
        target = destination(env[host.ssh_env], f"{host.id}: {host.ssh_env}")
        if target in destinations:
            raise ValueError(f"{host.id}: combine roles that use the same management destination")
        for role in host.roles:
            if not ROLE.fullmatch(role) or role in roles:
                raise ValueError(f"{host.id}: each router or numbered engine role needs one owner")
            roles.add(role)
        hosts.append(host)
        ids.add(host.id)
        destinations.add(target)
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


def trust_options(store: Path) -> list[str]:
    """Return the OpenSSH options that hold a connection to the host keys in `store`.

    Options on the command line take precedence over ssh_config, so a user or system
    configuration cannot consult other keys, reuse a connection whose host key nobody checked,
    route through a host whose key `store` lacks, or run local and remote commands of its own.
    """
    values = {
        "StrictHostKeyChecking": "yes",
        "UpdateHostKeys": "no",
        "GlobalKnownHostsFile": "/dev/null",
        "UserKnownHostsFile": str(store),
        "KnownHostsCommand": "none",
        "VerifyHostKeyDNS": "no",
        "ControlMaster": "no",
        "ControlPath": "none",
        "ControlPersist": "no",
        "ProxyJump": "none",
        "ProxyCommand": "none",
        "PermitLocalCommand": "no",
        "RemoteCommand": "none",
        "ForwardAgent": "no",
        "ForwardX11": "no",
        # OpenSSH keeps the first SetEnv it reads, and cannot clear it, so this fixed value
        # displaces any that ssh_config sets.
        "SetEnv": "NARWHAL_DEPLOY=1",
        "ConnectTimeout": str(CONNECT_TIMEOUT_S),
    }
    return [argument for name, value in values.items() for argument in ("-o", f"{name}={value}")]


def bounded(script: str, limit: float) -> str:
    """Return a host command that runs `script` under coreutils `timeout` at `limit`.

    The shell stays the parent of `timeout`, which puts itself and the script in a process
    group of their own. When the script ignores SIGTERM, `timeout` sends SIGKILL to that group
    and dies with it, and the shell still exits with 137, which `ssh` passes back.
    """
    return (
        "command -v timeout > /dev/null 2>&1 || "
        f"{{ echo '{HOST_CAUSE}the host lacks the coreutils timeout command' >&2; exit 127; }}\n"
        f"timeout -k {KILL_AFTER_S} {limit:.3f}s sh -c {shlex.quote(script)}; exit $?"
    )


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
        self.client_env = {name: value for name, value in os.environ.items() if name in CLIENT_KEYS}
        store = env.get("NARWHAL_SSH_KNOWN_HOSTS", "")
        if not store or not Path(store).is_file():
            raise ValueError("NARWHAL_SSH_KNOWN_HOSTS must name the supplied known-hosts file")
        trusted = Path(store).resolve()
        # OpenSSH splits these option values at whitespace and expands % tokens in them.
        if any(c.isspace() or c == "%" for c in str(trusted)):
            raise ValueError(
                f"NARWHAL_SSH_KNOWN_HOSTS path {trusted} contains whitespace or %; "
                "move the file to a path without them"
            )
        self.logs = logs
        self.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.options = trust_options(trusted)
        if jump := env.get("NARWHAL_SSH_JUMP"):
            # The jump connection holds to the same known-hosts file and authenticates with a
            # key, because the jump host has no password input.
            hop = [
                "ssh",
                *self.options,
                "-o",
                "BatchMode=yes",
                "-W",
                "[%h]:%p",
                destination(jump, "NARWHAL_SSH_JUMP"),
            ]
            self.options[self.options.index("ProxyCommand=none")] = "ProxyCommand=" + shlex.join(
                hop
            )

    def log(self, host: Host) -> Path:
        return self.logs / f"{host.id}.log"

    def _arguments(self, host: Host, options: list[str], *, prompts: bool = False) -> list[str]:
        args = ["ssh", *self.options, *options]
        if host.password_env:
            args += [
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        elif not prompts:
            # Key authentication never prompts; a missing key or agent fails the command.
            args += ["-o", "BatchMode=yes"]
        return [*args, self.env[host.ssh_env]]

    @contextmanager
    def _client(self, host: Host) -> Iterator[dict[str, str]]:
        """Yield the client environment, with a private askpass helper for a password host.

        OpenSSH asks the helper for the password, which it reads from a FIFO that this process
        holds open. The password lives only in the pipe until OpenSSH reads it, and never in a
        file, an argument list or an environment, and `ssh` stays in the process group that a
        time limit kills.
        """
        if not host.password_env:
            yield dict(self.client_env)
            return
        with tempfile.TemporaryDirectory(dir=self.logs, prefix=".askpass-") as folder:
            channel = Path(folder) / "password"
            os.mkfifo(channel, 0o600)
            # Opening for reading and writing never blocks, and keeps the pipe's contents until
            # the helper reads them or this process exits.
            pipe = os.open(channel, os.O_RDWR)
            try:
                os.write(pipe, self.env[host.password_env].encode() + b"\n")
                helper = Path(folder) / "askpass"
                write_private(
                    helper, f"#!/bin/sh\nexec head -n 1 {shlex.quote(str(channel))}\n".encode()
                )
                helper.chmod(0o700)
                yield {
                    **self.client_env,
                    "SSH_ASKPASS": str(helper),
                    "SSH_ASKPASS_REQUIRE": "force",
                }
            finally:
                os.close(pipe)

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

        The host stops the script at the limit with coreutils `timeout`, and the workstation
        kills `ssh` when the host has not answered `HOST_GRACE_S` later. The host's log retains
        the script, exit status and output. A failure quotes the last error line that starts
        with `HOST_CAUSE` or with `reason`.
        """
        limit = stage_limit(self.env, stage)
        log_path = self.log(host)
        command = "sh -c " + shlex.quote(bounded(script, limit))
        with self._record(host, f"{stage}: {operation}") as log, self._client(host) as client:
            log.write(script + "\n")
            log.flush()
            args = [*self._arguments(host, ["-T", "-o", "ClearAllForwardings=yes"]), command]
            with payload.open("rb") if payload else open(os.devnull, "rb") as stream:
                started = time.monotonic()
                process = subprocess.Popen(
                    args,
                    stdin=stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=client,
                    start_new_session=True,
                )
                unanswered = False
                try:
                    stdout, stderr = process.communicate(timeout=limit + HOST_GRACE_S)
                except subprocess.TimeoutExpired:
                    unanswered = True
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
            expired = unanswered or (
                process.returncode in TIMEOUT_STATUSES and time.monotonic() - started >= limit
            )
            if unanswered:
                log.write(f"timeout={limit:g}s; the host did not answer\n")
            elif expired:
                log.write(f"timeout={limit:g}s; the host stopped the command\n")
            else:
                log.write(f"exit={process.returncode}\n")
            for output in (stdout, stderr):
                if output:
                    log.write(output.decode(errors="replace"))
        if expired:
            raise RemoteTimeout(host.id, stage, operation, log_path, limit)
        if process.returncode:
            lines = stderr.decode(errors="replace").splitlines()
            causes = [
                line[len(prefix) :]
                for line in lines
                for prefix in (HOST_CAUSE, reason)
                if prefix and line.startswith(prefix)
            ]
            if any(failure in line for line in lines for failure in HOST_KEY_FAILURES):
                causes.append(
                    "the host key differs from NARWHAL_SSH_KNOWN_HOSTS or is missing from it"
                )
            cause = f": {causes[-1]}" if causes else ""
            raise RemoteError(host.id, stage, operation, log_path, cause)
        return stdout.decode(errors="replace").strip()

    def shell(self, host: Host, script: str) -> None:
        """Run an interactive `script` on `host` in this terminal, with no time limit."""
        with self._record(host, "role shell") as log, self._client(host) as client:
            log.write(script + "\n")
            log.flush()
            args = [*self._arguments(host, ["-t"], prompts=True), "sh -c " + shlex.quote(script)]
            result = subprocess.run(args, env=client)
            log.write(f"exit={result.returncode}\n")
        if result.returncode:
            raise RemoteError(host.id, "shell", "role shell", self.log(host))

    def tunnel(self, host: Host, forwards: list[str]) -> None:
        """Hold local port forwards to `host` until SSH exits or the operator interrupts."""
        options = [
            "-N",
            "-T",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        for forward in forwards:
            options += ["-L", forward]
        with self._record(host, "service tunnel") as log, self._client(host) as client:
            log.write(" ".join(forwards) + "\n")
            log.flush()
            result = subprocess.run(
                self._arguments(host, options), stdin=subprocess.DEVNULL, env=client
            )
            log.write(f"exit={result.returncode}\n")
        if result.returncode:
            raise RemoteError(host.id, "tunnel", "service tunnel", self.log(host))
