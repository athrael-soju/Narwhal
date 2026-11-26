"""`narwhal deploy`: discover, reach, package and install a fleet on SSH hosts."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .. import command_results as results
from ..cli_errors import failure
from . import discovery, install
from .hosts import SSH, Host, RemoteError, RemoteTimeout, load_hosts

DESCRIPTIONS = {
    "discover": "Log in to every host named in the loaded environment with its pinned host key, "
    "inspect GPUs, the engine image and the checkpoint, and write the host inventory, launch "
    "records, fleet and deployment.env.",
    "access": "Check the inventory, then log in once to each host with its pinned host key and "
    "record its hostname in a private log.",
    "package": "Check the Narwhal wheel against its SHA-256 and write it, the helper snapshots "
    "and each host's role files into a fresh package directory with a private manifest.",
    "install": "Copy a package to each selected host, checking every file's SHA-256, and "
    "install the wheel into the host's deployment directory.",
    "shell": "Open an interactive shell on a role's host, in its deployment directory with the "
    "role environment loaded when --run names a package.",
    "tunnel": "Forward workstation loopback ports to a role's host until interrupted.",
}


def forward_ports(value: str) -> tuple[int, int]:
    """Parse LOCAL_PORT:REMOTE_PORT into a port pair."""
    if not re.fullmatch(r"[0-9]{1,5}:[0-9]{1,5}", value):
        raise argparse.ArgumentTypeError("Use LOCAL_PORT:REMOTE_PORT")
    local, remote = map(int, value.split(":"))
    if not all(1 <= port <= 65535 for port in (local, remote)):
        raise argparse.ArgumentTypeError("Ports must be between 1 and 65535")
    return local, remote


def add_commands(commands: Any) -> None:
    """Register `deploy` beneath the installed root command."""
    deploy = commands.add_parser(
        "deploy",
        help="Discover, package and install a fleet on SSH hosts",
        description="Discover, package and install a fleet on SSH hosts. discover inspects the "
        "hosts and writes the generated configuration, access checks every login, package "
        "writes the wheel and role files, install copies and installs them, shell opens a role "
        "shell and tunnel forwards router ports to the workstation.",
        epilog="Documentation: https://athrael-soju.github.io/Narwhal/cli/Deploy/",
    )
    actions = deploy.add_subparsers(dest="action", required=True)
    for name, description in DESCRIPTIONS.items():
        action = actions.add_parser(name, help=description, description=description)
        if name not in ("shell", "tunnel"):
            results.add_format(action)
        if name != "discover":
            action.add_argument(
                "--hosts",
                type=Path,
                help="host inventory (default: NARWHAL_HOSTS, else config/hosts.local.json)",
            )
        if name in ("discover", "package"):
            action.add_argument(
                "--out", type=Path, required=True, help="fresh private output directory"
            )
        if name == "install":
            action.add_argument(
                "--run", type=Path, required=True, help="package directory that package wrote"
            )
            selection = action.add_mutually_exclusive_group()
            selection.add_argument(
                "--host", action="append", help="install one inventory host; repeatable"
            )
            selection.add_argument(
                "--role",
                action="append",
                help="install the host of a router or engine-<n> role; repeatable",
            )
        if name == "shell":
            action.add_argument(
                "--role", required=True, help="router or engine-<n> role whose host to open"
            )
            action.add_argument(
                "--run", type=Path, help="installed package whose role environment to load"
            )
        if name == "tunnel":
            action.add_argument("--role", default="router", help="role whose host to forward to")
            action.add_argument(
                "--forward",
                type=forward_ports,
                action="append",
                required=True,
                metavar="LOCAL_PORT:REMOTE_PORT",
                help="forward workstation 127.0.0.1:LOCAL_PORT to REMOTE_PORT; repeatable",
            )
            action.add_argument(
                "--remote-address",
                type=ipaddress.ip_address,
                default="127.0.0.1",
                help="address the forwarded ports listen on at the host (default: %(default)s)",
            )


def _hosts(args: argparse.Namespace, env: dict[str, str]) -> list[Host]:
    path = args.hosts or Path(env.get("NARWHAL_HOSTS", "config/hosts.local.json"))
    return load_hosts(path, env)


def _role_host(hosts: list[Host], role: str) -> Host:
    host = next((h for h in hosts if role in h.roles), None)
    if host is None:
        raise ValueError(f"Select a role from the inventory, not {role}")
    return host


def _ssh(env: dict[str, str], logs: Path) -> SSH:
    ssh = SSH(env, logs)
    print(f"Private command logs: {logs}", flush=True)
    return ssh


def _access_logs() -> Path:
    return Path("runs") / f"access-{uuid.uuid4().hex}"


def _discover(args: argparse.Namespace, env: dict[str, str]) -> int:
    fleet = Path(env.get("NARWHAL_FLEET", "config/fleet.json"))
    results.add_artifact("deployment_env", fleet.with_name("deployment.env"))
    derived = discovery.discover(env, args.out)
    results.set_data({"out": str(args.out), "deployment_env": str(derived)})
    print(f"Generated private configuration. Load {derived} before packaging.")
    return 0


def _access(hosts: list[Host], env: dict[str, str]) -> int:
    logs = _access_logs()
    ssh = _ssh(env, logs)
    for host in hosts:
        ssh.run(host, "access", "management access", "hostname")
        print(f"{host.id}: {', '.join(host.roles)}; host key matched, SSH login verified")
    results.set_data(
        {"logs": str(logs), "hosts": [{"id": h.id, "roles": list(h.roles)} for h in hosts]}
    )
    return 0


def _package(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    results.add_artifact("manifest", args.out / "manifest.json")
    install.package(hosts, env, args.out.resolve())
    results.set_data({"run": str(args.out), "hosts": [host.id for host in hosts]})
    print(f"Packaged {len(hosts)} hosts in {args.out}; private manifest and inputs ready")
    return 0


def _install(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    manifest = install.load_run(args.run, hosts, env)
    if args.role:
        owners = {role: h.id for h in hosts for role in h.roles}
        if set(args.role) - owners.keys():
            raise ValueError("Select --role values from the inventory")
        chosen = {owners[role] for role in args.role}
    else:
        chosen = set(args.host or [host.id for host in hosts])
    if chosen - {host.id for host in hosts}:
        raise ValueError("Select --host values from the inventory")
    selected = [h for h in hosts if h.id in chosen]
    install.install(selected, manifest, args.run, _ssh(env, args.run / "logs"))
    results.set_data({"run": str(args.run), "hosts": [host.id for host in selected]})
    return 0


def _shell(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    host = _role_host(hosts, args.role)
    if args.run:
        manifest = install.load_run(args.run, hosts, env)
        _ssh(env, args.run / "logs").shell(host, install.shell_script(manifest, args.role))
    else:
        _ssh(env, _access_logs()).shell(host, "exec bash -l")
    return 0


def _tunnel(args: argparse.Namespace, hosts: list[Host], env: dict[str, str]) -> int:
    host = _role_host(hosts, args.role)
    if len({local for local, _ in args.forward}) != len(args.forward):
        raise ValueError("Select a distinct local port for each forward")
    address = str(args.remote_address)
    if args.remote_address.version == 6:
        address = f"[{address}]"
    forwards = [f"127.0.0.1:{local}:{address}:{remote}" for local, remote in args.forward]
    ssh = _ssh(env, _access_logs())
    print(
        "Keep this terminal open; Ctrl-C closes its forwards. SSH errors appear here.", flush=True
    )
    try:
        ssh.tunnel(host, forwards)
    except KeyboardInterrupt:
        return 130
    return 0


def _dispatch(args: argparse.Namespace, env: dict[str, str]) -> int:
    if args.action in ("shell", "tunnel") and results.json_mode():
        raise ValueError(f"deploy {args.action} is interactive and writes no command result")
    if args.action == "discover":
        return _discover(args, env)
    hosts = _hosts(args, env)
    if args.action == "access":
        return _access(hosts, env)
    actions = {"package": _package, "install": _install, "shell": _shell, "tunnel": _tunnel}
    return actions[args.action](args, hosts, env)


def run(args: argparse.Namespace) -> int:
    """Run one `narwhal deploy` action and return its exit code."""
    results.set_operation("deploy " + args.action)
    context = f"deploy {args.action}"
    try:
        return _dispatch(args, dict(os.environ))
    except (RemoteError, RemoteTimeout) as exc:
        if results.json_mode():
            raise
        return failure("narwhal", context, exc, 1)
    except (ValueError, KeyError, TypeError, FileExistsError, FileNotFoundError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal", context, exc, 2)
    except OSError as exc:
        if results.json_mode():
            raise
        return failure("narwhal", context, exc, 1)
