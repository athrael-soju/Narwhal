"""Prepare and install a deployment once per physical host in the private inventory."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.deployment.engine_launch import load_launches
from tools.deployment.host_access import SSH, Host, load_hosts, write_private
from tools.deployment.prepare_host_env import select_values, write_environment

FABRIC_BUDGET_SOURCE = Path(__file__).with_name("fabric_budget.py")
ENGINE_LAUNCHER_SOURCE = Path(__file__).with_name("launch_engine.py")
CACHE_CAPTURE_SOURCE = Path(__file__).with_name("cache_capture_hook.py")


def role_files(host: Host) -> list[str]:
    files = [f".env.{role}" for role in host.roles]
    files += [f"engine-launch.{role}.json" for role in host.roles if role.startswith("engine-")]
    if any(role.startswith("engine-") for role in host.roles):
        files.extend(("fabric_budget.py", "launch_engine.py", "cache_capture_hook.py"))
    if "router" in host.roles:
        files.append("fleet.local.json")
    return files


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise ValueError(
            "Source preparation failed; check the approved commit and local Git objects"
        )
    return result.stdout.strip()


def prepare_bundle(source: Path, revision: str, output: Path) -> None:
    """Package an existing commit and prove that a fresh clone resolves it."""
    if local_git("rev-parse", "--verify", f"{revision}^{{commit}}", cwd=source) != revision:
        raise ValueError("Source preparation requires the approved full commit SHA")
    if output.exists():
        raise ValueError("Source bundle already exists; choose a fresh preparation directory")
    with tempfile.TemporaryDirectory(prefix="source-", dir=output.parent) as folder:
        bare = Path(folder)
        local_git("init", "--bare", str(bare), cwd=source)
        local_git("fetch", "--no-tags", str(source), f"{revision}:refs/heads/deployment", cwd=bare)
        local_git("bundle", "create", str(output), "refs/heads/deployment", cwd=bare)
        output.chmod(0o600)
        local_git("bundle", "verify", str(output), cwd=bare)
        clone = bare / "verify"
        local_git("clone", "--branch", "deployment", str(output), str(clone), cwd=bare)
        if local_git("rev-parse", "HEAD", cwd=clone) != revision:
            raise ValueError("Source bundle clone differs from the approved revision")


def snapshot(hosts: list[Host], env: dict[str, str]) -> dict:
    return {
        "hosts": [asdict(host) for host in hosts],
        "destinations": {
            host.id: hashlib.sha256(env[host.ssh_env].encode()).hexdigest() for host in hosts
        },
    }


def prepare(hosts: list[Host], env: dict[str, str], output: Path, source: Path) -> None:
    """Build one source bundle and separate environment files for every assigned role."""
    fleet_path = Path(env["NARWHAL_FLEET"])
    fleet = json.loads(fleet_path.read_text())
    selected = {}
    for host in hosts:
        for role in host.roles:
            node = int(role.split("-")[1]) if role.startswith("engine-") else None
            selected[role] = select_values("engine" if node else "router", node, fleet, env)
    engine_roles = [role for role in selected if role.startswith("engine-")]
    launches = load_launches(
        Path(env.get("NARWHAL_LAUNCH_CONFIG", "config/engine-launch.local.json")),
        engine_roles,
        selected,
    )
    budget_tool = FABRIC_BUDGET_SOURCE.read_bytes()
    budget_hash = hashlib.sha256(budget_tool).hexdigest()
    launcher_tool = ENGINE_LAUNCHER_SOURCE.read_bytes()
    launcher_hash = hashlib.sha256(launcher_tool).hexdigest()
    capture_tool = CACHE_CAPTURE_SOURCE.read_bytes()
    capture_hash = hashlib.sha256(capture_tool).hexdigest()
    for role in engine_roles:
        selected[role]["NARWHAL_FABRIC_BUDGET_SHA256"] = budget_hash
        selected[role]["NARWHAL_ENGINE_LAUNCHER_SHA256"] = launcher_hash
        selected[role]["NARWHAL_CACHE_CAPTURE_HOOK_SHA256"] = capture_hash
    access_names = {name for h in hosts for name in (h.ssh_env, h.password_env) if name}
    if any(access_names.intersection(values) for values in selected.values()):
        raise ValueError(
            "Role environment references a management credential; correct the fleet references"
        )
    output.mkdir(mode=0o700, parents=True)
    revision = env["NARWHAL_DEPLOYMENT_REVISION"]
    prepare_bundle(source.resolve(), revision, output / "source.bundle")
    for host in hosts:
        directory = output / host.id
        directory.mkdir(mode=0o700)
        for role in host.roles:
            write_environment(directory / f".env.{role}", selected[role])
            if role in launches:
                write_private(
                    directory / f"engine-launch.{role}.json",
                    json.dumps(launches[role], indent=2).encode() + b"\n",
                )
        if any(role.startswith("engine-") for role in host.roles):
            write_private(directory / "fabric_budget.py", budget_tool)
            write_private(directory / "launch_engine.py", launcher_tool)
            write_private(directory / "cache_capture_hook.py", capture_tool)
        if "router" in host.roles:
            write_private(directory / "fleet.local.json", fleet_path.read_bytes())
    paths = ["source.bundle", *[f"{h.id}/{name}" for h in hosts for name in role_files(h)]]
    manifest = {
        **snapshot(hosts, env),
        "revision": revision,
        "remote_dir": f"Narwhal-deploy/{uuid.uuid4().hex}",
        "hashes": {name: digest(output / name) for name in paths},
    }
    write_private(output / "manifest.json", json.dumps(manifest, indent=2).encode() + b"\n")


def load_run(run: Path, hosts: list[Host], env: dict[str, str]) -> dict:
    """Verify prepared inputs and host assignments before starting an SSH operation."""
    manifest = json.loads((run / "manifest.json").read_text())
    expected = json.loads(json.dumps(snapshot(hosts, env)))
    if any(manifest[key] != value for key, value in expected.items()):
        raise ValueError(
            "Host assignments changed; use the matching inventory or prepare a new run"
        )
    if not re.fullmatch(r"Narwhal-deploy/[0-9a-f]{32}", manifest["remote_dir"]):
        raise ValueError("Prepared remote directory has an invalid format")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", manifest["revision"]):
        raise ValueError("Prepared revision must contain the full commit SHA")
    paths = {"source.bundle", *[f"{h.id}/{name}" for h in hosts for name in role_files(h)]}
    if set(manifest["hashes"]) != paths:
        raise ValueError("Prepared file list differs from the assigned roles")
    for name in paths:
        if digest(run / name) != manifest["hashes"][name]:
            raise ValueError("Prepared input changed; restore it or prepare a new run")
    return manifest


def send(ssh: SSH, host: Host, source: Path, target: str, expected: str) -> None:
    path = shlex.quote(target)
    probe = f"""set -eu
test ! -L {path}
if test -e {path}; then
  test -f {path}
  test "$(sha256sum {path} | cut -d' ' -f1)" = {expected}
  printf present
else
  printf absent
fi"""
    if ssh.run(host, "verify file", probe) == "present":
        return
    temporary = shlex.quote(f"{target}.part-{uuid.uuid4().hex}")
    script = f"""set -eu
umask 077
temporary={temporary}
trap 'rm -f "$temporary"' EXIT
(set -C; cat > "$temporary")
test "$(sha256sum "$temporary" | cut -d' ' -f1)" = {expected}
ln "$temporary" {path}"""
    ssh.run(host, "transfer file", script, payload=source)


def install_script(host: Host, manifest: dict) -> str:
    root, revision = manifest["remote_dir"], manifest["revision"]
    copies = []
    for name in role_files(host):
        target = f"config/{name}" if name.endswith(".json") else name
        if name in ("fabric_budget.py", "launch_engine.py", "cache_capture_hook.py"):
            target = f"runs/deployment-tools/{name}"
        copies.append(
            f"if test -e {target}; then cmp ../{name} {target}; "
            f"else install -m 600 ../{name} {target}; fi"
        )
    return f"""set -eu
umask 077
cd {root}
mkdir .install-lock
trap 'rmdir .install-lock' EXIT
if test ! -d checkout; then git clone --branch deployment source.bundle checkout; fi
(
  cd checkout
  test "$(git rev-parse HEAD)" = {revision}
  git diff --quiet
  git diff --cached --quiet
  mkdir -p runs/deployment-tools
  {chr(10).join(copies)}
  if test -e ../installed; then
    test "$(cat ../installed)" = {revision}
    test -x .venv/bin/narwhal-check
  else
    python3 -m venv .venv
    .venv/bin/python -m pip install -e '.[dev]' -c constraints-dev.txt
    .venv/bin/narwhal-check --help > /dev/null
    printf '%s\\n' {revision} > ../installed
  fi
)"""


def install(hosts: list[Host], manifest: dict, run: Path, ssh: SSH) -> None:
    """Transfer source and install once for each inventory host, reusing verified artifacts."""
    for host in hosts:
        root = manifest["remote_dir"]
        print(f"{host.id}: source, {', '.join(host.roles)}, installation", flush=True)
        ssh.run(host, "create deployment directory", f"umask 077; mkdir -p {root}")
        for name in ["source.bundle", *role_files(host)]:
            local = name if name == "source.bundle" else f"{host.id}/{name}"
            send(ssh, host, run / local, f"{root}/{name}", manifest["hashes"][local])
        ssh.run(host, "revision and installation", install_script(host, manifest))
        print(f"{host.id}: installation ready", flush=True)


def forward_ports(value: str) -> tuple[int, int]:
    """Accept explicit local and remote ports before constructing SSH arguments."""
    if not re.fullmatch(r"[0-9]{1,5}:[0-9]{1,5}", value):
        raise argparse.ArgumentTypeError("Use LOCAL_PORT:REMOTE_PORT")
    local, remote = map(int, value.split(":"))
    if not all(1 <= port <= 65535 for port in (local, remote)):
        raise argparse.ArgumentTypeError("Ports must be between 1 and 65535")
    return local, remote


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hosts", type=Path, default=os.environ.get("NARWHAL_HOSTS", "config/hosts.local.json")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    commands.add_parser("check-access")
    prep = commands.add_parser("prepare")
    prep.add_argument("--out", required=True, type=Path)
    apply = commands.add_parser("install")
    apply.add_argument("--run", required=True, type=Path)
    selection = apply.add_mutually_exclusive_group()
    selection.add_argument("--host", action="append")
    selection.add_argument("--role", action="append")
    shell = commands.add_parser("shell")
    shell.add_argument("--role", required=True)
    shell.add_argument("--run", type=Path)
    tunnel = commands.add_parser("tunnel", help="Forward workstation loopback ports to a role host")
    tunnel.add_argument("--role", default="router")
    tunnel.add_argument(
        "--forward",
        type=forward_ports,
        action="append",
        required=True,
        metavar="LOCAL_PORT:REMOTE_PORT",
    )
    tunnel.add_argument("--remote-address", type=ipaddress.ip_address, default="127.0.0.1")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    try:
        hosts = load_hosts(args.hosts, env)
        if args.command == "plan":
            for host in hosts:
                print(f"{host.id}: {', '.join(host.roles)}; source and installation once")
        elif args.command == "prepare":
            prepare(hosts, env, args.out.resolve(), Path.cwd())
            print(f"Prepared {len(hosts)} hosts in {args.out}; private manifest and inputs ready")
        else:
            run = getattr(args, "run", None)
            manifest = load_run(run, hosts, env) if run else None
            logs = run / "logs" if run else Path("runs") / f"access-{uuid.uuid4().hex}"
            ssh = SSH(env, logs)
            print(f"Private command logs: {logs}", flush=True)
            if args.command == "check-access":
                for host in hosts:
                    ssh.run(host, "management access", "hostname")
                    print(f"{host.id}: verified SSH login; hostname recorded in private log")
            elif args.command == "install":
                if args.role:
                    owners = {role: h.id for h in hosts for role in h.roles}
                    if set(args.role) - owners.keys():
                        raise ValueError("Select --role values from the inventory plan")
                    chosen = {owners[role] for role in args.role}
                else:
                    chosen = set(args.host or [host.id for host in hosts])
                if chosen - {host.id for host in hosts}:
                    raise ValueError("Select --host values from the inventory plan")
                install([h for h in hosts if h.id in chosen], manifest, run, ssh)
            else:
                host = next((h for h in hosts if args.role in h.roles), None)
                if host is None:
                    raise ValueError("Select a role from the inventory plan")
                if args.command == "tunnel":
                    if len({local for local, _ in args.forward}) != len(args.forward):
                        raise ValueError("Select a distinct local port for each forward")
                    address = str(args.remote_address)
                    if args.remote_address.version == 6:
                        address = f"[{address}]"
                    forwards = [
                        f"127.0.0.1:{local}:{address}:{remote}" for local, remote in args.forward
                    ]
                    print(
                        "Keep this terminal open; Ctrl-C closes its forwards. "
                        "SSH errors appear here.",
                        flush=True,
                    )
                    try:
                        ssh.run(host, "service tunnel", "", forwards=forwards)
                    except KeyboardInterrupt:
                        return 130
                    return 0
                script = "exec bash -l"
                if manifest:
                    inner = (
                        f"set +x; . ./.env.{args.role} && . ./.venv/bin/activate && "
                        "exec bash --noprofile --norc -i"
                    )
                    script = (
                        f"cd {manifest['remote_dir']}/checkout && "
                        "exec bash --noprofile --norc -c " + shlex.quote(inner)
                    )
                ssh.run(host, "role shell", script, interactive=True)
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        if type(error) is ValueError:
            parser.exit(1, f"{error}\n")
        parser.exit(
            1, "Check private input structure, required fields and local file permissions.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
