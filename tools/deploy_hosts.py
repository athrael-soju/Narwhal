"""Prepare and install a deployment once per physical host in the private inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.engine_launch import load_launches
from tools.prepare_host_env import select_values, write_environment

FABRIC_BUDGET_SOURCE = Path(__file__).with_name("fabric_budget.py")


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


def role_files(host: Host) -> list[str]:
    files = [f".env.{role}" for role in host.roles]
    files += [f"engine-launch.{role}.json" for role in host.roles if role.startswith("engine-")]
    if any(role.startswith("engine-") for role in host.roles):
        files.append("fabric_budget.py")
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


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


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
    for role in engine_roles:
        selected[role]["NARWHAL_FABRIC_BUDGET_SHA256"] = budget_hash
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


class SSH:
    def __init__(self, env: dict[str, str], logs: Path):
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
            "StrictHostKeyChecking=yes",
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
    ) -> str:
        """Use the host's authentication for each operation, retaining private command logs."""
        args = ["ssh", *self.options]
        if interactive:
            args.append("-t")
        if host.password_env:
            args += ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
        args += [self.env[host.ssh_env], "sh -c " + shlex.quote(script)]
        fd = os.open(self.logs / f"{host.id}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w") as log, tempfile.TemporaryFile() as password:
            log.write(f"\n[{gate}] {script}\n")
            log.flush()
            pass_fds = ()
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
                    stderr=None if interactive else subprocess.PIPE,
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
        if name == "fabric_budget.py":
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
