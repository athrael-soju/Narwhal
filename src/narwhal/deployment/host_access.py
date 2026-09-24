"""Resolve management hosts and retain private SSH command evidence."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Host:
    id: str
    ssh_env: str
    password_env: str | None
    roles: tuple[str, ...]


def load_hosts(path: Path, env: dict[str, str]) -> list[Host]:
    """Resolve each role to one host and reject duplicate destination entries."""
    hosts, ids, destinations, roles = [], set(), set(), set()
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
            if not re.fullmatch(r"router|engine-[1-9][0-9]*", role) or role in roles:
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


class SSH:
    def __init__(self, env: dict[str, str], logs: Path, *, enroll_hosts: bool = False):
        self.env = env
        client_keys = {
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
        self.client_env = {
            name: value
            for name, value in os.environ.items()
            if name in client_keys or name.startswith("LC_")
        }
        self.logs = logs
        self.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        store = env.get("NARWHAL_SSH_KNOWN_HOSTS", "")
        if not store or not Path(store).is_file():
            raise ValueError("NARWHAL_SSH_KNOWN_HOSTS must select the supplied host-key file")
        self.options = [
            "-o",
            "StrictHostKeyChecking=accept-new" if enroll_hosts else "StrictHostKeyChecking=yes",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"UserKnownHostsFile={Path(store).resolve()}",
        ]

    def run(
        self,
        host: Host,
        gate: str,
        script: str,
        payload: Path | None = None,
        interactive: bool = False,
        forwards: list[str] | None = None,
    ) -> str:
        """Use the host's authentication for each operation, retaining private command logs."""
        args = ["ssh", *self.options]
        if forwards:
            args += [
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
            ]
            for forward in forwards:
                args += ["-L", forward]
        elif interactive:
            args.append("-t")
        if host.password_env:
            args += ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
        args.append(self.env[host.ssh_env])
        if not forwards:
            args.append("sh -c " + shlex.quote(script))
        fd = os.open(self.logs / f"{host.id}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w") as log, tempfile.TemporaryFile() as password:
            log.write(f"\n[{gate}] {forwards if forwards else script}\n")
            log.flush()
            pass_fds: tuple[int, ...] = ()
            if host.password_env:
                password.write(self.env[host.password_env].encode() + b"\n")
                password.seek(0)
                args = ["sshpass", "-d", str(password.fileno()), *args]
                pass_fds = (password.fileno(),)
            with payload.open("rb") if payload else open(os.devnull, "rb") as stream:
                result = subprocess.run(
                    args,
                    stdin=None if interactive else stream,
                    stdout=None if interactive else subprocess.PIPE,
                    stderr=None if interactive or forwards else subprocess.PIPE,
                    pass_fds=pass_fds,
                    env=self.client_env,
                )
            log.write(f"exit={result.returncode}\n")
            for output in (result.stdout, result.stderr):
                if output:
                    log.write(output.decode(errors="replace"))
        if result.returncode:
            raise ValueError(f"{host.id}: blocked at {gate}; inspect its private command log")
        return (result.stdout or b"").decode(errors="replace").strip()
