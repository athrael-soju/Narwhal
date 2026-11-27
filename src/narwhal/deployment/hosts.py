"""Resolve management hosts and run time-limited SSH commands against pinned host keys."""

from __future__ import annotations

import json
import math
import os
import re
import select
import shlex
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TypeVar

ROLE = re.compile(r"router|standby-router|load-balancer|engine-[1-9][0-9]*")
# The primary router of a pair first, then its standby.
ROUTER_ROLES = ("router", "standby-router")
# Seconds one remote command of each stage may run. NARWHAL_STAGE_<STAGE>_TIMEOUT_SECONDS sets
# one stage's limit and NARWHAL_STAGE_TIMEOUT_SECONDS every stage's, as for local stages.
STAGE_LIMITS = {
    "access": 60.0,
    "discover": 3600.0,
    "install": 1800.0,
    "engine-inspect": 300.0,
    "engine-prepare": 300.0,
    "engine-check": 1800.0,
    "engine-start": 1800.0,
    "engine-probe": 300.0,
    "cache-capture": 600.0,
    "transfer-measure": 900.0,
    "fabric-budget": 300.0,
    "fabric-edge": 300.0,
    "fleet-inventory": 60.0,
    "attest-capture": 900.0,
    "sidecar-start": 300.0,
    "sidecar-verify": 300.0,
    "finalize-fleet": 300.0,
    "profile": 7200.0,
    "calibrate": 7200.0,
    "fleet-limits": 300.0,
    "preflight": 3600.0,
    "lease-check": 120.0,
    "router-start": 600.0,
    "load-balancer": 300.0,
    "monitoring": 600.0,
    # Each load client run, journal read and the KV ring of the capacity trial.
    "trial": 3600.0,
    "down": 300.0,
    # The upload of the deployment status after a run.
    "deployment-status": 60.0,
}
# The shortest stage limit: `bounded` passes limits to `timeout` in milliseconds, and `timeout`
# reads a zero duration as no limit at all.
MIN_STAGE_LIMIT_S = 0.001
# Seconds OpenSSH waits for a connection before it gives up, for every command.
CONNECT_TIMEOUT_S = 30
# The host sends SIGKILL to a command this many seconds after its time limit's SIGTERM.
KILL_AFTER_S = 5
# Seconds past a time limit that the workstation waits for the host to stop the command and
# answer, before it ends the connection itself.
HOST_GRACE_S = 30.0
# Seconds the workstation gives the host to stop an interrupted command.
STOP_LIMIT_S = 30.0
# Where each host command records the process ID of its `timeout`, relative to the SSH user's
# home, so an interrupted command can be stopped from a second connection.
COMMANDS_DIRECTORY = ".cache/narwhal-commands"
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
                raise ValueError(
                    f"{host.id}: each router, load balancer or numbered engine role needs one owner"
                )
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
        "NARWHAL_STAGE_" + re.sub(r"[^A-Z0-9]", "_", stage.upper()) + "_TIMEOUT_SECONDS",
        "NARWHAL_STAGE_TIMEOUT_SECONDS",
    ):
        if name in env:
            try:
                value = float(env[name])
            except ValueError:
                value = math.nan
            if not math.isfinite(value) or value < MIN_STAGE_LIMIT_S:
                raise ValueError(f"{name} must be finite and at least {MIN_STAGE_LIMIT_S:g}")
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


# The only process IDs a command record may hold: no leading zero, since dash reads
# `kill -- -01` as `kill -- -1`, and not 1, whose group is every process of the user.
PID_PATTERN = '"" | *[!0-9]* | 0* | 1'


def _marker(token: str) -> str:
    return f'"$HOME"/{COMMANDS_DIRECTORY}/{token}'


def _read_line(fd: int, deadline: float) -> str:
    """One line from `fd` before the monotonic `deadline`."""
    data = b""
    while not data.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError("the host did not answer in time")
        chunk = os.read(fd, 1)
        if not chunk:
            raise ValueError("the host closed its output")
        data += chunk
    return data.decode().strip()


def bounded(script: str, limit: float, token: str) -> str:
    """Return a host command that runs `script` under coreutils `timeout` at `limit`.

    `timeout` puts itself and the script in a process group of their own and sends SIGTERM to
    that group at the limit. When the script outlives SIGTERM, GNU `timeout` sends the group
    SIGKILL `KILL_AFTER_S` later, but the uutils `timeout` of Ubuntu 25.10 and later never does.
    So after `timeout` reports a time limit with 124 or 137, the shell sends SIGKILL to the
    group itself, and exits with that status, which `ssh` passes back.

    `timeout` runs in the foreground, so the script keeps the channel's standard input and the
    default SIGINT and SIGQUIT, which a background job of a shell would lose. Before it starts
    the script, `timeout`'s child checks that its parent is this `timeout`, whose arguments name
    `token`, and leads its process group, and records that process ID under `token` for the
    group kill and for `stop_script`. It writes the record to a new file, which no symbolic
    link can redirect, and renames it into place. The record directory must belong to the SSH
    user and be writable by no one else. When any of that fails, the script does not run, so no
    command runs that the limit could not stop.
    """
    marker = _marker(token)
    directory = f'"$HOME"/{COMMANDS_DIRECTORY}'
    shown = f"$HOME/{COMMANDS_DIRECTORY}"
    pending = f'"$HOME"/{COMMANDS_DIRECTORY}/.{token}.$$'
    refuse = f'{{ echo "{HOST_CAUSE}$1" >&2; exit 125; }}'
    start = (
        f"fail() {refuse}\n"
        f'case "$PPID" in {PID_PATTERN}) fail "timeout has no usable process ID" ;; esac\n'
        f'tr "\\000" " " < /proc/"$PPID"/cmdline 2> /dev/null | grep -q -- narwhal-{token} ||\n'
        '  fail "the parent of the command is not its timeout"\n'
        'group=$(sed "s/^.*) //" /proc/$$/stat | cut -d" " -f3)\n'
        'test "$group" = "$PPID" || fail "timeout did not give the command a process group"\n'
        f"(set -C && umask 077 && printf '%s\\n' \"$PPID\" > {pending}) 2> /dev/null &&\n"
        f"  mv -f {pending} {marker} ||\n"
        f'  {{ rm -f {pending}; fail "cannot record the command in {shown}; make it writable"; }}\n'
        f'exec sh -c "$1" narwhal-{token}'
    )
    return (
        "command -v timeout > /dev/null 2>&1 || "
        f"{{ echo '{HOST_CAUSE}the host lacks the coreutils timeout command' >&2; exit 127; }}\n"
        f"(umask 077 && mkdir -p {directory}) 2> /dev/null\n"
        f'test -d {directory} && test -w {directory} || {{ echo "{HOST_CAUSE}cannot create '
        f'{shown} to record the command; make it writable" >&2; exit 125; }}\n'
        f"! test -L {directory} && test -O {directory} || "
        f'{{ echo "{HOST_CAUSE}{shown} is a symbolic link or belongs to another user" >&2; '
        "exit 125; }\n"
        f'case "$(stat -c %a {directory})" in *[2367][0-7] | *[2367])\n'
        f'  echo "{HOST_CAUSE}{shown} is writable by other users; run chmod go-w on it" >&2\n'
        "  exit 125 ;;\n"
        "esac\n"
        f"timeout -k {KILL_AFTER_S} {limit:.3f}s sh -c {shlex.quote(start)} narwhal-{token} "
        f"{shlex.quote(script)}\n"
        "status=$?\n"
        'case "$status" in\n'
        "  124 | 137)\n"
        f"    pid=$(cat {marker} 2> /dev/null)\n"
        "    # Only a process ID that the command's own timeout recorded names its group.\n"
        '    case "$pid" in\n'
        f"      {PID_PATTERN}) ;;\n"
        '      *) kill -s KILL -- "-$pid" 2> /dev/null ;;\n'
        "    esac\n"
        "    ;;\n"
        "esac\n"
        f"rm -f {marker}\n"
        'exit "$status"'
    )


def stop_script(token: str) -> str:
    """Return a host command that stops the command `bounded` started under `token`.

    It sends SIGTERM to the process group of the command's `timeout`, and SIGKILL to what is
    left of the group `KILL_AFTER_S` later, as at the time limit. A recorded process ID that no
    longer names this command's `timeout` is left alone.
    """
    marker = _marker(token)
    return (
        f"test -f {marker} || {{ echo 'no command is running'; exit 0; }}\n"
        f"pid=$(cat {marker})\n"
        f"rm -f {marker}\n"
        f'case "$pid" in {PID_PATTERN}) exit 0 ;; esac\n'
        f'tr "\\000" " " < /proc/"$pid"/cmdline 2> /dev/null | grep -q -- narwhal-{token} ||\n'
        "  { echo 'already ended'; exit 0; }\n"
        'kill -s TERM -- "-$pid" 2> /dev/null\n'
        "tries=0\n"
        f'while kill -s 0 -- "-$pid" 2> /dev/null && test "$tries" -lt {KILL_AFTER_S * 10}; do\n'
        "  sleep 0.1; tries=$((tries + 1))\n"
        "done\n"
        'kill -s KILL -- "-$pid" 2> /dev/null\n'
        'echo "stopped $pid"'
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
        # The host commands in flight, by token, so `cancel` can stop them from any thread.
        self._running: dict[str, tuple[Host, str, str, subprocess.Popen]] = {}
        self._lock = threading.Lock()
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
        token = uuid.uuid4().hex
        command = "sh -c " + shlex.quote(bounded(script, limit, token))
        # An interrupted or unanswered command may still run on the host, since sshd signals
        # no command that has no terminal. A second connection stops it before this one is
        # read to its end.
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
                with self._lock:
                    self._running[token] = (host, stage, operation, process)
                unanswered = False
                try:
                    stdout, stderr = process.communicate(timeout=limit + HOST_GRACE_S)
                except subprocess.TimeoutExpired:
                    unanswered = True
                    os.killpg(process.pid, signal.SIGKILL)
                    self.stop(host, stage, operation, token)
                    stdout, stderr = process.communicate()
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    self.stop(host, stage, operation, token)
                    process.communicate()
                    raise
                finally:
                    with self._lock:
                        self._running.pop(token, None)
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

    def stop(self, host: Host, stage: str, operation: str, token: str) -> None:
        """Stop the host command started under `token`, within `STOP_LIMIT_S`.

        A failure to reach the host is logged, not raised, so it never hides the error that
        led here. The host's own time limit still ends the command.
        """
        with self._record(host, f"{stage}: stop {operation}") as log:
            try:
                with self._client(host) as client:
                    args = [
                        *self._arguments(host, ["-T", "-o", "ClearAllForwardings=yes"]),
                        "sh -c " + shlex.quote(stop_script(token)),
                    ]
                    process = subprocess.Popen(
                        args,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        env=client,
                        start_new_session=True,
                    )
                    try:
                        output, _ = process.communicate(timeout=STOP_LIMIT_S)
                    finally:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                log.write(f"exit={process.returncode}\n" + output.decode(errors="replace"))
            except (OSError, subprocess.SubprocessError) as error:
                log.write(f"the command may still run until its time limit: {error}\n")

    def cancel(self) -> None:
        """Stop every host command in flight, locally and on its host.

        Their `run` calls return in their own threads with a failed command. Call this from the
        thread that received an interruption, so commands that other threads run end as well.
        """
        with self._lock:
            running = dict(self._running)
        for _, _, _, process in running.values():
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        # Each stop takes up to STOP_LIMIT_S, so the hosts are stopped at once.
        stops = [
            threading.Thread(target=self.stop, args=(host, stage, operation, token))
            for token, (host, stage, operation, _) in running.items()
        ]
        for stop in stops:
            stop.start()
        for stop in stops:
            stop.join()

    def clock_offset(
        self, host: Host, stage: str, operation: str, script: str, samples: int = 8
    ) -> tuple[float, float]:
        """The host's wall clock minus this workstation's, and the bound on that estimate.

        `script` prints the host's `time.time()` for each line it reads. Each exchange brackets
        the host's reading between two workstation readings, and the tightest of `samples`
        exchanges gives the offset; half its round trip bounds the error.
        """
        limit = stage_limit(self.env, stage)
        log_path = self.log(host)
        token = uuid.uuid4().hex
        command = "sh -c " + shlex.quote(bounded(script, limit, token))
        best: tuple[float, float] | None = None
        with self._record(host, f"{stage}: {operation}") as log, self._client(host) as client:
            log.write(script + "\n")
            log.flush()
            args = [*self._arguments(host, ["-T", "-o", "ClearAllForwardings=yes"]), command]
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=client,
                start_new_session=True,
            )
            assert process.stdin is not None and process.stdout is not None
            with self._lock:
                self._running[token] = (host, stage, operation, process)
            deadline = time.monotonic() + limit + HOST_GRACE_S
            try:
                for _ in range(samples):
                    sent = time.time()
                    process.stdin.write(b"\n")
                    process.stdin.flush()
                    line = _read_line(process.stdout.fileno(), deadline)
                    received = time.time()
                    half = (received - sent) / 2
                    offset = float(line) - (sent + received) / 2
                    if best is None or half < best[1]:
                        best = (offset, half)
                process.stdin.close()
                process.wait(timeout=max(deadline - time.monotonic(), 0.1))
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                os.killpg(process.pid, signal.SIGKILL)
                self.stop(host, stage, operation, token)
                _, stderr = process.communicate()
                log.write(f"failed: {error}\n{stderr.decode(errors='replace')}")
                raise RemoteError(host.id, stage, operation, log_path, f": {error}") from None
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                self.stop(host, stage, operation, token)
                process.communicate()
                raise
            finally:
                with self._lock:
                    self._running.pop(token, None)
            assert best is not None
            log.write(f"offset={best[0]:.6f}s bound={best[1]:.6f}s\n")
        return best

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

    @contextmanager
    def forward(
        self, host: Host, stage: str, forwards: Mapping[int, tuple[str, int]]
    ) -> Iterator[None]:
        """Hold workstation loopback ports forwarded to `host` while the block runs.

        `forwards` maps each local port to the address and port it reaches from the host. The
        block starts once every local port accepts a connection; SSH failing first, or no port
        accepting within the connection timeout, raises RemoteError. The block's end stops the
        `ssh` process this call started, by its process handle.
        """
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
        specs = []
        for local, (address, port) in forwards.items():
            target = f"[{address}]" if ":" in address else address
            specs.append(f"127.0.0.1:{local}:{target}:{port}")
            options += ["-L", specs[-1]]
        operation = "forward " + ", ".join(specs)
        with self._record(host, f"forwards of {stage}") as log, self._client(host) as client:
            log.write(" ".join(specs) + "\n")
            log.flush()
            process = subprocess.Popen(
                self._arguments(host, options),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=client,
            )
            try:
                pending = set(forwards)
                deadline = time.monotonic() + CONNECT_TIMEOUT_S
                while pending:
                    if process.poll() is not None:
                        raise RemoteError(
                            host.id,
                            stage,
                            operation,
                            self.log(host),
                            f": ssh exited {process.returncode}",
                        )
                    for local in sorted(pending):
                        with (
                            suppress(OSError),
                            socket.create_connection(("127.0.0.1", local), timeout=0.5),
                        ):
                            pending.discard(local)
                    if pending and time.monotonic() >= deadline:
                        raise RemoteError(
                            host.id,
                            stage,
                            operation,
                            self.log(host),
                            f": no forward listened within {CONNECT_TIMEOUT_S}s",
                        )
                    if pending:
                        time.sleep(0.1)
                yield
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=KILL_AFTER_S)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                log.write(f"exit={process.returncode}\n")


def free_port() -> int:
    """A loopback port no process listens on now, for a forward to take."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


Item = TypeVar("Item")
Result = TypeVar("Result")


def parallel(
    ssh: SSH, function: Callable[[Item], Result], items: Iterable[Item], workers: int
) -> Iterator[tuple[Item, Result]]:
    """Run `function` over `items` in up to `workers` threads, yielding results in order.

    When the caller is interrupted or one call fails, the calls not yet started are dropped and
    the host commands in flight are stopped through `ssh.cancel`, so the threads end at once
    instead of running on to their time limits.
    """
    jobs = list(items)
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs))))
    futures = [pool.submit(function, job) for job in jobs]
    try:
        for job, future in zip(jobs, futures, strict=True):
            yield job, future.result()
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        # Stand-ins for SSH in tests may run nothing that needs stopping.
        cancel = getattr(ssh, "cancel", None)
        while not all(future.done() for future in futures):
            if cancel is not None:
                cancel()
            wait(futures, timeout=0.1)
        raise
    finally:
        pool.shutdown()
