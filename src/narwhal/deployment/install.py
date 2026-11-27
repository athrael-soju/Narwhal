"""Package a deployment and install it once per physical host in the private inventory."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from .engine_launch import expose_colocated_gpus, load_launches
from .hosts import HOST_CAUSE, SSH, Host, write_private
from .launch_engine.backend import launcher
from .role_env import HELPER_DIRECTORY, helpers, select_values, write_environment

# A release wheel of the narwhal-inference distribution, as PEP 427 names it. Each release
# version has the repository tag v<version>.
WHEEL_NAME = re.compile(r"narwhal_inference-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl")
# The pip requirement lines of a hash-checking lock: one exact version, an optional environment
# marker, and the SHA-256 of every distribution file that pip may install.
LOCKED_REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+"
    # pip reads each token that starts with `-` as an option, so a marker holds none, and no
    # `#` or `\`.
    r"(?: *;(?:(?!\s-)[^\\#])*?)?"
    r"(?: +--hash=sha256:[0-9a-f]{64})+"
)
# The package's name for the lock that NARWHAL_REQUIREMENTS names.
REQUIREMENTS = "requirements.txt"
# Prints the version of the distribution that the host's virtual environment installed.
INSTALLED_VERSION = "from importlib.metadata import version; print(version('narwhal-inference'))"
REMOTE_DIR = re.compile(r"Narwhal-deploy/[0-9a-f]{32}")
# Each host keeps the transferred files here, below its deployment directory.
PACKAGE_DIRECTORY = "package"
# The installed `narwhal` package, whose layout the wheel's `narwhal/` tree repeats.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def required(env: Mapping[str, str], name: str, value: str) -> str:
    if not env.get(name):
        raise ValueError(f"Set {name} to {value}")
    return env[name]


def role_files(host: Host, helper_names: list[str]) -> list[str]:
    files = [f".env.{role}" for role in host.roles]
    files += [f"engine-launch.{role}.json" for role in host.roles if role.startswith("engine-")]
    if any(role.startswith("engine-") for role in host.roles):
        files.extend(helper_names)
    if "router" in host.roles:
        files.extend(("fleet.local.json", "profiling-limits.json"))
    return files


def profiling_limits(launches: dict[str, dict], fleet: dict) -> dict:
    """Return the profiling-limits document from each engine's sequence limit."""
    limits = {}
    for role, record in launches.items():
        runtime = record.get("runtime", {})
        try:
            limit = launcher(runtime.get("backend")).sequence_limit(runtime)
        except ValueError as error:
            raise ValueError(f"{role}: {error}") from None
        limits[f"n{role.removeprefix('engine-')}"] = limit
    if set(limits) != {engine["iid"] for engine in fleet["engines"]}:
        raise ValueError("Engine launch roles must match the fleet IDs for profiling")
    return {"schema": "narwhal.profiling-limits", "schema_version": 1, "engines": limits}


def check_hardware(fleet: dict, launches: dict[str, dict]) -> None:
    """Refuse a fleet whose `hardware` fields differ from an engine's launch record.

    `narwhal-engine inspect-host` checks each launch record against its host's GPUs, so the
    two checks together tie the fleet's hardware to the hosts.
    """
    hardware = fleet.get("hardware") or {}
    for role, record in launches.items():
        declared = {
            "accelerator": record["accelerator"],
            "accelerators_per_engine": len(record["gpu_ids"]),
            "tensor_parallel": record["tensor_parallel_size"],
        }
        for field, value in declared.items():
            if hardware.get(field) != value:
                raise ValueError(
                    f"{role}: the fleet's hardware.{field} is {hardware.get(field)!r}, but the "
                    f"launch record gives {value!r}; correct the fleet or rerun discovery"
                )


def wheel_version(name: str) -> str:
    match = WHEEL_NAME.fullmatch(name)
    if match is None:
        raise ValueError("Packaged wheel needs a narwhal_inference release wheel name")
    return match[1]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def wheel(env: Mapping[str, str]) -> tuple[Path, str]:
    """Return the Narwhal wheel the inputs name and its SHA-256, after checking both."""
    path = Path(env.get("NARWHAL_WHEEL", ""))
    expected = env.get("NARWHAL_WHEEL_SHA256", "")
    if not env.get("NARWHAL_WHEEL") or not WHEEL_NAME.fullmatch(path.name):
        raise ValueError(
            "Set NARWHAL_WHEEL to a release wheel, "
            "narwhal_inference-<major>.<minor>.<patch>-py3-none-any.whl"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Set NARWHAL_WHEEL_SHA256 to the wheel's SHA-256, 64 lowercase hex digits")
    if not path.is_file() or path.is_symlink():
        raise ValueError("NARWHAL_WHEEL must name a regular file")
    if digest(path) != expected:
        raise ValueError("NARWHAL_WHEEL differs from NARWHAL_WHEEL_SHA256")
    return path, expected


def requirements(env: Mapping[str, str]) -> tuple[bytes, str]:
    """Return the hash-checking lock of the wheel's dependencies and its SHA-256."""
    path = Path(
        required(env, "NARWHAL_REQUIREMENTS", "the hash-pinned lock of the wheel's dependencies")
    )
    expected = env.get("NARWHAL_REQUIREMENTS_SHA256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError(
            "Set NARWHAL_REQUIREMENTS_SHA256 to the lock's SHA-256, 64 lowercase hex digits"
        )
    if not path.is_file() or path.is_symlink():
        raise ValueError("NARWHAL_REQUIREMENTS must name a regular file")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("NARWHAL_REQUIREMENTS differs from NARWHAL_REQUIREMENTS_SHA256")
    names = []
    for number, line in enumerate(data.decode().replace("\\\n", " ").splitlines(), start=1):
        line = re.sub(r"(?:^|\s)#.*", "", line).strip()
        if not line:
            continue
        if not LOCKED_REQUIREMENT.fullmatch(line):
            raise ValueError(
                f"NARWHAL_REQUIREMENTS line {number}: pin one exact version with its "
                "--hash=sha256 values, and no other pip options"
            )
        names.append(re.split(r"[=;\s]", line, maxsplit=1)[0].lower().replace("_", "-"))
    if not names or "narwhal-inference" in names:
        raise ValueError("NARWHAL_REQUIREMENTS must lock the wheel's dependencies, not the wheel")
    return data, expected


def wheel_file(wheel_path: Path, source: Path) -> bytes:
    """Return the wheel's copy of a module of the `narwhal` package."""
    member = "narwhal/" + source.resolve().relative_to(PACKAGE_ROOT).as_posix()
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            return archive.read(member)
    except (zipfile.BadZipFile, KeyError):
        raise ValueError(f"NARWHAL_WHEEL lacks {member}; package a wheel that holds it") from None


def snapshot(hosts: list[Host], env: Mapping[str, str]) -> dict:
    return {
        "hosts": [asdict(host) for host in hosts],
        "destinations": {
            host.id: hashlib.sha256(env[host.ssh_env].encode()).hexdigest() for host in hosts
        },
    }


def package(hosts: list[Host], env: Mapping[str, str], output: Path) -> None:
    """Write the wheel, the helper snapshots and the role files of every host."""
    wheel_path, wheel_sha256 = wheel(env)
    lock, lock_sha256 = requirements(env)
    tools = required(env, "NARWHAL_TOOLS_COMMIT", "the commit of the wheel's release tag")
    if not re.fullmatch(r"[0-9a-f]{40}", tools):
        raise ValueError("Set NARWHAL_TOOLS_COMMIT to a full 40-hex commit SHA")
    fleet_path = Path(required(env, "NARWHAL_FLEET", "the fleet file that discovery wrote"))
    fleet = json.loads(fleet_path.read_text())
    selected = {}
    for host in hosts:
        for role in host.roles:
            node = int(role.split("-")[1]) if role.startswith("engine-") else None
            selected[role] = select_values("engine" if node else "router", node, fleet, env)
            # Launch plans and attestations record the installed wheel as the revision.
            selected[role]["NARWHAL_DEPLOYMENT_REVISION"] = wheel_sha256
    # The router host checks its tools checkout against this commit.
    selected["router"]["NARWHAL_TOOLS_COMMIT"] = tools
    engine_roles = [role for role in selected if role.startswith("engine-")]
    launches = load_launches(
        Path(env.get("NARWHAL_LAUNCH_CONFIG", "config/engine-launch.local.json")),
        engine_roles,
        selected,
    )
    check_hardware(fleet, launches)
    expose_colocated_gpus(launches, [list(host.roles) for host in hosts])
    selected_helpers = helpers(launcher(fleet.get("engine", {}).get("backend")))
    # Engine hosts run the helpers of the release that they install.
    snapshots = {helper.name: wheel_file(wheel_path, helper.source) for helper in selected_helpers}
    for helper in selected_helpers:
        for role in engine_roles:
            selected[role][helper.sha256_env] = hashlib.sha256(snapshots[helper.name]).hexdigest()
    access_names = {name for h in hosts for name in (h.ssh_env, h.password_env) if name}
    if any(access_names.intersection(values) for values in selected.values()):
        raise ValueError(
            "Role environment references a management credential; correct the fleet references"
        )
    limits = profiling_limits(launches, fleet)
    output.mkdir(mode=0o700, parents=True)
    write_private(output / wheel_path.name, wheel_path.read_bytes())
    if digest(output / wheel_path.name) != wheel_sha256:
        raise ValueError("NARWHAL_WHEEL changed while it was copied; package a new run")
    write_private(output / REQUIREMENTS, lock)
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
            for name, content in snapshots.items():
                write_private(directory / name, content)
        if "router" in host.roles:
            write_private(directory / "fleet.local.json", fleet_path.read_bytes())
            write_private(
                directory / "profiling-limits.json",
                json.dumps(limits, indent=2).encode() + b"\n",
            )
    paths = [
        wheel_path.name,
        REQUIREMENTS,
        *[f"{h.id}/{name}" for h in hosts for name in role_files(h, list(snapshots))],
    ]
    manifest = {
        **snapshot(hosts, env),
        "wheel": wheel_path.name,
        "wheel_sha256": wheel_sha256,
        "requirements_sha256": lock_sha256,
        "tools_commit": tools,
        "helpers": list(snapshots),
        "remote_dir": f"Narwhal-deploy/{uuid.uuid4().hex}",
        "hashes": {name: digest(output / name) for name in paths},
    }
    write_private(output / "manifest.json", json.dumps(manifest, indent=2).encode() + b"\n")


def load_run(run: Path, hosts: list[Host], env: Mapping[str, str]) -> dict:
    """Return the packaged manifest after checking its inputs and host assignments."""
    manifest: dict = json.loads((run / "manifest.json").read_text())
    expected = json.loads(json.dumps(snapshot(hosts, env)))
    if any(manifest[key] != value for key, value in expected.items()):
        raise ValueError(
            "Host assignments changed; use the matching inventory or package a new run"
        )
    if not REMOTE_DIR.fullmatch(manifest["remote_dir"]):
        raise ValueError("Packaged remote directory has an invalid format")
    wheel_version(manifest["wheel"])
    if not re.fullmatch(r"[0-9a-f]{64}", manifest["wheel_sha256"]):
        raise ValueError("Packaged wheel needs its SHA-256")
    paths = {
        manifest["wheel"],
        REQUIREMENTS,
        *[f"{h.id}/{name}" for h in hosts for name in role_files(h, manifest["helpers"])],
    }
    if set(manifest["hashes"]) != paths:
        raise ValueError("Packaged file list differs from the assigned roles")
    if manifest["hashes"][manifest["wheel"]] != manifest["wheel_sha256"]:
        raise ValueError("Packaged wheel hash differs from the manifest's wheel SHA-256")
    if manifest["hashes"][REQUIREMENTS] != manifest["requirements_sha256"]:
        raise ValueError("Packaged lock hash differs from the manifest's requirements SHA-256")
    for name in paths:
        if digest(run / name) != manifest["hashes"][name]:
            raise ValueError("Packaged input changed; restore it or package a new run")
    return manifest


def missing_files(ssh: SSH, host: Host, root: str, files: dict[str, str]) -> set[str]:
    """Create the package directory and return the named files it lacks, in one call."""
    checks = "\n".join(
        f"check {shlex.quote(f'{root}/{name}')} {expected} {shlex.quote(name)}"
        for name, expected in files.items()
    )
    script = f"""set -eu
umask 077
mkdir -p {shlex.quote(root)}
check() {{
  test ! -L "$1"
  if test -e "$1"; then
    test -f "$1"
    test "$(sha256sum "$1" | cut -d' ' -f1)" = "$2"
  else
    printf '%s\\n' "$3"
  fi
}}
{checks}"""
    return set(ssh.run(host, "install", "verify files", script).split())


def transfer(ssh: SSH, host: Host, source: Path, target: str, expected: str) -> None:
    path = shlex.quote(target)
    temporary = shlex.quote(f"{target}.part-{uuid.uuid4().hex}")
    script = f"""set -eu
umask 077
temporary={temporary}
trap 'rm -f "$temporary"' EXIT
(set -C; cat > "$temporary")
test "$(sha256sum "$temporary" | cut -d' ' -f1)" = {expected}
ln "$temporary" {path}"""
    ssh.run(host, "install", "transfer file", script, payload=source)


def install_target(name: str) -> str:
    """Return where the deployment directory keeps the working copy of a packaged file."""
    if name.startswith(".env."):
        return f"runs/deployment/{name}"
    if name == "fleet.local.json":
        return "runs/deployment/fleet.json"
    if name == "profiling-limits.json":
        return "runs/deployment/profiling-limits.json"
    if name.endswith(".py"):
        return f"{HELPER_DIRECTORY}/{name}"
    return f"config/{name}"


def install_script(host: Host, manifest: dict) -> str:
    """Return the host script that installs the wheel and its locked dependencies once."""
    root = shlex.quote(manifest["remote_dir"])
    wheel_file = shlex.quote(f"{PACKAGE_DIRECTORY}/{manifest['wheel']}")
    wheel_sha256 = manifest["wheel_sha256"]
    lock_file = shlex.quote(f"{PACKAGE_DIRECTORY}/{REQUIREMENTS}")
    lock_sha256 = manifest["requirements_sha256"]
    version = shlex.quote(wheel_version(manifest["wheel"]))
    copies = []
    for name in role_files(host, manifest["helpers"]):
        source = shlex.quote(f"{PACKAGE_DIRECTORY}/{name}")
        target = shlex.quote(install_target(name))
        # Deployment steps edit the router's fleet, so a later install keeps the edited copy.
        check = (
            f"test -f {target} && test ! -L {target}"
            if name == "fleet.local.json"
            else f"cmp {source} {target}"
        )
        copy = f"install -m 600 {source} {target}"
        copies.append(f"if test -e {target}; then {check}; else {copy}; fi")
    newline = "\n"
    # The lock directory names its owner, so a stale lock left by a killed installer says
    # which installer took it.
    return f"""set -eu
umask 077
cd {root}
fail() {{ printf '{HOST_CAUSE}%s\\n' "$1" >&2; exit 1; }}
if ! mkdir .install-lock 2> /dev/null; then
  owner=$(cat .install-lock/owner 2> /dev/null || echo 'an installer that recorded no owner')
  fail "$PWD/.install-lock exists, taken by $owner; remove it once that installer has exited"
fi
trap 'rm -f .install-lock/owner; rmdir .install-lock' EXIT
trap 'exit 143' TERM HUP INT
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf 'process %s on %s at %s\\n' "$$" "$(hostname)" "$started" > .install-lock/owner
mkdir -p runs/deployment {HELPER_DIRECTORY} config
{newline.join(copies)}
if test -e installed; then
  test "$(cat installed)" = {wheel_sha256} ||
    fail "installed holds another wheel; package a new run"
  test -x .venv/bin/narwhal-check
else
  for checked in {wheel_file}:{wheel_sha256} {lock_file}:{lock_sha256}; do
    file=${{checked%:*}}
    test ! -L "$file" && test "$(sha256sum "$file" | cut -d' ' -f1)" = "${{checked##*:}}" ||
      fail "$file differs from the packaged SHA-256"
  done
  python3 -m venv .venv
  .venv/bin/python -m pip install --no-deps --require-hashes --only-binary :all: -r {lock_file}
  .venv/bin/python -m pip install --no-deps --no-index {wheel_file}
  .venv/bin/python -m pip check
  test "$(.venv/bin/python -c {shlex.quote(INSTALLED_VERSION)})" = {version} ||
    fail "the installed narwhal-inference version differs from the wheel"
  .venv/bin/narwhal-check --help > /dev/null
  printf '%s\\n' {wheel_sha256} > installed
fi"""


def install(hosts: list[Host], manifest: dict, run: Path, ssh: SSH) -> None:
    """Transfer the package and install the wheel once for each host, reusing verified files."""
    root = manifest["remote_dir"]
    for host in hosts:
        print(f"{host.id}: wheel, {', '.join(host.roles)}, installation", flush=True)
        shared = (manifest["wheel"], REQUIREMENTS)
        sources = {
            name: name if name in shared else f"{host.id}/{name}"
            for name in [*shared, *role_files(host, manifest["helpers"])]
        }
        expected = {name: manifest["hashes"][local] for name, local in sources.items()}
        directory = f"{root}/{PACKAGE_DIRECTORY}"
        absent = missing_files(ssh, host, directory, expected)
        for name, local in sources.items():
            if name in absent:
                transfer(ssh, host, run / local, f"{directory}/{name}", expected[name])
        ssh.run(host, "install", "wheel installation", install_script(host, manifest))
        print(f"{host.id}: installation ready", flush=True)


def shell_script(manifest: dict, role: str) -> str:
    """Return the command that opens a role shell in the installed deployment directory."""
    inner = (
        f"set +x; . ./runs/deployment/.env.{role} && "
        ". ./.venv/bin/activate && "
        "exec bash --noprofile --norc -i"
    )
    return (
        f"cd {shlex.quote(manifest['remote_dir'])} && "
        "exec bash --noprofile --norc -c " + shlex.quote(inner)
    )
