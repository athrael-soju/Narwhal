"""Stubbed hosts and engines for `narwhal deploy run` tests.

`Fleet` replaces each host-facing seam of `deployment.deploy`: the library functions of the
standalone commands, the host steps, the SSH calls the stages make directly, and the systemd
supervisor. It keeps the state those report: each engine's running container, each unit's
invocation, each edge's route, and the calls each stage made.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import functools
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from narwhal.deployment import cli as deploy_cli
from narwhal.deployment import deploy, lease_check, remote, supervision
from narwhal.deployment.hosts import Host, RemoteError, stage_limit, write_private
from narwhal.deployment.role_env import write_environment
from narwhal.deployment.supervision import Observation, Supervisor, SystemdUnit
from narwhal.runtime.lease import WITNESS_NAME, FileLease

REMOTE_DIR = "Narwhal-deploy/" + "b" * 32
SHA = "c" * 64


def environment(root: Path) -> dict[str, str]:
    """Every input `narwhal deploy run` needs, for two engine hosts with the primary router and
    the load balancer on node-1 and the standby router on node-2."""
    wheel = root / "narwhal_inference-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    lock = root / "requirements.txt"
    lock.write_text(f"httpx==0.28.1 --hash=sha256:{SHA}\n")
    known = root / "known_hosts"
    known.write_text("h1 ssh-ed25519 AAAA\n")
    return {
        "NARWHAL_NODE_1_SSH": "deploy@h1",
        "NARWHAL_NODE_2_SSH": "deploy@h2",
        "NARWHAL_SSH_KNOWN_HOSTS": str(known),
        "NARWHAL_WHEEL": str(wheel),
        "NARWHAL_WHEEL_SHA256": hashlib.sha256(b"wheel").hexdigest(),
        "NARWHAL_REQUIREMENTS": str(lock),
        "NARWHAL_REQUIREMENTS_SHA256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "NARWHAL_ENGINE_IMAGE": "sha256:" + "e" * 64,
        "NARWHAL_ENGINE_MODEL_NAME": "served-model",
        "NARWHAL_MODEL_DIR": "/models/checkpoint",
        "NARWHAL_MODEL_TREE_SHA256": "f" * 64,
        "NARWHAL_RUN_DIR": "/srv/runs",
        "NARWHAL_FABRIC_INTERFACE": "fabric0",
        "NARWHAL_FABRIC_PROMPT_TOKENS": "4096",
        "NARWHAL_FABRIC_HANDOFFS_PER_S": "4",
        "NARWHAL_FABRIC_BURST": "2",
        "NARWHAL_FABRIC_TRANSFER_BUDGET_S": "0.5",
        "NARWHAL_FABRIC_HEADROOM": "1.2",
        "NARWHAL_FABRIC_TEST_PORT": "5201",
        "NARWHAL_TTFT_S": "4",
        "NARWHAL_TPOT_S": "0.1",
        "NARWHAL_ROUTER_OVERHEAD_S": "0.05",
        "NARWHAL_FIRST_TOKEN_MARGIN_S": "0.25",
        "NARWHAL_PROFILE_PREFILL_LENS": "256,1024,4096",
        "NARWHAL_PROFILE_DECODE_INPUT_LENS": "512,4096",
        "NARWHAL_PROFILE_WARMUP_REQUESTS": "8",
        "NARWHAL_CALIBRATION_INPUT_TOKENS": "256,4096",
        "NARWHAL_CALIBRATION_SAMPLES": "100",
        "NARWHAL_CALIBRATION_OBSERVATION_TIMEOUT_S": "20",
        "NARWHAL_ROUTER_URL": "http://10.0.0.1:8000",
        "NARWHAL_STANDBY_ROUTER_SSH": "deploy@h2",
        "NARWHAL_STANDBY_ROUTER_URL": "http://10.0.0.2:8000",
        "NARWHAL_LOAD_BALANCER_URL": "http://10.0.0.1:8080",
        "NARWHAL_LEASE_PATH": "/srv/shared/narwhal-lease.json",
        "NARWHAL_HAPROXY_IMAGE": "haproxy@sha256:" + "a" * 64,
        "NARWHAL_FLEET_NAME": "fleet-a",
    }


def _file(name: str, data: bytes) -> str:
    return f"narwhal-file {name} {base64.b64encode(data).decode()}"


@dataclass(frozen=True)
class _Sample:
    name: str


@dataclass(frozen=True)
class _Observation:
    sample: _Sample
    fingerprint: str


@dataclass(frozen=True)
class _Edge:
    source: Host
    destination: Host

    @property
    def name(self) -> str:
        return f"{self.source.id}-to-{self.destination.id}"


class _Plan:
    """The parts of `fabric.Fabric` the deployment stages read."""

    def __init__(self, hosts: list[Host], groups: list[dict]) -> None:
        self.hosts = hosts
        self.groups = [_Group(tuple(group["roles"])) for group in groups]
        self.engines = {
            role: None for host in hosts for role in host.roles if role.startswith("engine-")
        }

    def group(self, role: str) -> _Group:
        return next(group for group in self.groups if role in group.roles)

    def edges(self) -> tuple[_Edge, ...]:
        engines = [host for host in self.hosts if any(r.startswith("engine-") for r in host.roles)]
        return tuple(_Edge(a, b) for a in engines for b in engines if a != b)


@dataclass(frozen=True)
class _Group:
    roles: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.roles[0]


class Fleet:
    """Host and engine state behind every seam of a deployment run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = environment(root)
        # The argv of each unit's last start, by unit name.
        self.argv: dict[str, list[str]] = {}
        self.calls: Counter[str] = Counter()
        # Each engine role's running container: (container ID, start time).
        self.containers: dict[str, tuple[str, str]] = {}
        # Each unit's current invocation: unit name -> (nonce, invocation, restarts).
        self.units: dict[str, tuple[str, str, int]] = {}
        self.routes: dict[str, str] = {}
        self.readmitted: list[list[str]] = []
        self.verified: list[str] = []
        self.fail_stage: str | None = None
        self.fail_status = 1
        self.fail_verify = False
        self.package_dir: Path | None = None
        self.run_directory = root / "run"
        # Each engine role's launch directory its running container started from.
        self.started_from: dict[str, str] = {}
        # The container each launch directory started.
        self.used: dict[str, str] = {}
        self.uploads: dict[str, bytes] = {}
        # Whether the standby router's host reads the probe lease the primary's host claimed.
        self.lease_coherent = True
        self.lease = ProbeLease(root / "shared")
        # Each router host's lease mount type, its clock offset and bound, and the hosts whose
        # lease directory is not mounted.
        self.mounts: dict[str, str] = {}
        self.clocks: dict[str, tuple[float, float]] = {}
        self.unmounted: set[str] = set()
        # Each host's lease mount options, where they differ from a mount the lease accepts.
        self.mount_options: dict[str, list[str]] = {}
        # Seconds since the pair's last lease expired, when no router runs and a lease file
        # is left; None for no lease file.
        self.lease_expired_ago: float | None = None
        # The overhead measurement scripts, in order.
        self.measured: list[str] = []
        self.monitoring_up = False
        self.monitoring_command = ""
        # What `docker compose version` reports missing on every host, or None.
        self.compose_problem: str | None = None
        self.overhead_problem: str | None = None
        # The router that controls the fleet, by host ID.
        self.active_router = "node-1"

    def mount(self, host: str) -> str:
        """`lease_check mount` on `host`: the lease directory's mount."""
        # As the kernel lists a mount made with actimeo=1.
        accepted = [
            "rw",
            "vers=4.2",
            "hard",
            "acregmin=1",
            "acregmax=1",
            "acdirmin=1",
            "acdirmax=1",
            "local_lock=none",
        ]
        options = self.mount_options.get(host, accepted)
        return json.dumps(
            {"type": self.mounts.get(host, "nfs4"), "target": "/srv/shared", "options": options}
        )

    def lease_holder(self) -> str | None:
        """The host whose running router holds a live lease: the active router, while its
        router unit runs."""
        running = any(
            name.startswith(f"{self.active_router}:") and "-router." in name for name in self.units
        )
        return self.active_router if running else None

    # Library functions of the standalone commands.

    def discover(self, env: dict[str, str], out: Path) -> Path:
        self.calls["discover"] += 1
        out.mkdir(mode=0o700, parents=True)
        hosts = deploy.discovery.derive_hosts(env)
        config = Path(env["NARWHAL_HOSTS"]).parent
        write_private(
            Path(env["NARWHAL_HOSTS"]),
            json.dumps({"hosts": [asdict(host) for host in hosts]}).encode(),
        )
        fleet = {
            "model": "served-model",
            "engines": [
                {
                    "iid": f"n{n}",
                    "url": f"${{NARWHAL_NODE_{n}_URL}}",
                    "attestation_url": f"${{NARWHAL_NODE_{n}_ATTESTATION_URL}}",
                    "role": "prefill" if n == 1 else "decode",
                }
                for n in (1, 2)
            ],
            "slo": {"ttft_s": 10, "tpot_s": 0.125},
            "profiles": {"path": "runs/profiles.json"},
        }
        write_private(Path(env["NARWHAL_FLEET"]), json.dumps(fleet).encode())
        write_private(Path(env["NARWHAL_LAUNCH_CONFIG"]), b'{"engines": {}}')
        derived = config / "deployment.env"
        write_environment(
            derived,
            {
                **{f"NARWHAL_NODE_{n}_URL": f"http://10.0.0.{n}:8100" for n in (1, 2)},
                **{f"NARWHAL_NODE_{n}_ATTESTATION_URL": f"http://10.0.0.{n}:8200" for n in (1, 2)},
            },
        )
        return derived

    def package(self, hosts: list[Host], env: dict[str, str], output: Path) -> None:
        self.calls["package"] += 1
        output.mkdir(mode=0o700, parents=True)
        for host in hosts:
            directory = output / host.id
            directory.mkdir(mode=0o700)
            for role in host.roles:
                values = {"NARWHAL_ROUTER_URL": env["NARWHAL_ROUTER_URL"]}
                if role == "load-balancer":
                    values = {}
                elif role.startswith("engine-"):
                    n = role.split("-")[1]
                    values[f"NARWHAL_NODE_{n}_ATTESTATION_URL"] = env[
                        f"NARWHAL_NODE_{n}_ATTESTATION_URL"
                    ]
                else:
                    values.update(
                        {f"NARWHAL_NODE_{n}_URL": env[f"NARWHAL_NODE_{n}_URL"] for n in (1, 2)}
                    )
                    values.update(
                        {
                            f"NARWHAL_NODE_{n}_ATTESTATION_URL": env[
                                f"NARWHAL_NODE_{n}_ATTESTATION_URL"
                            ]
                            for n in (1, 2)
                        }
                    )
                write_environment(directory / f".env.{role}", values)
            if {"router", "standby-router"} & set(host.roles):
                write_private(
                    directory / "fleet.local.json", Path(env["NARWHAL_FLEET"]).read_bytes()
                )
        write_private(output / "manifest.json", json.dumps({"remote_dir": REMOTE_DIR}).encode())
        self.package_dir = output

    def load_run(self, run: Path, hosts: list[Host], env: dict[str, str]) -> dict:
        return json.loads((run / "manifest.json").read_text())

    def install(self, hosts: list[Host], manifest: dict, run: Path, ssh: Any) -> None:
        for host in hosts:
            self.calls[f"install[{host.id}]"] += 1

    def derive(self, launch_config, layouts, transfers, package, out) -> dict:
        self.calls["cache-groups"] += 1
        for path in layouts.values():
            json.loads(Path(path).read_text())
        groups = [
            {"id": "group-1", "representative": "engine-1", "roles": ["engine-1", "engine-2"]}
        ]
        document = {"groups": groups}
        write_private(out / "cache-groups.json", json.dumps(document).encode())
        return document

    def load_fabric(self, hosts, manifest, run, env, groups) -> _Plan:
        return _Plan(hosts, json.loads(Path(groups).read_text())["groups"])

    def group_budget(self, plan, ssh, group, directory: Path) -> dict:
        self.calls[f"fabric-budget[{group.name}]"] += 1
        directory.mkdir(mode=0o700, parents=True)
        write_private(directory / "budget.json", b'{"required_gbps": 1.0}')
        return {"required_gbps": 1.0}

    def qualify_edge(self, plan, ssh, edge, budgets, directory: Path, retained=None, results=None):
        self.calls[f"fabric-edge[{edge.name}]"] += 1
        directory.mkdir(mode=0o700, parents=True)
        write_private(directory / "evidence.json", b"{}")
        return [
            {
                "sample": "a-to-b",
                "link_sha256": self.route(edge.name),
                "measured_gbps": 90.0,
                "roles": {"engine-1": {"required_gbps": 1.0, "passed": True}},
            }
        ]

    def route(self, edge: str) -> str:
        return self.routes.setdefault(edge, "route-1")

    def observe(self, plan, ssh, edge) -> list[_Observation]:
        return [_Observation(_Sample("a-to-b"), self.route(edge.name))]

    # Host steps.

    def step(self, ssh: Any, step: remote.Step, directory: Path) -> str:
        stage, role = step.stage, step.role
        self.calls[f"{stage}[{role}]" if role and role.startswith("engine-") else stage] += 1
        if self.fail_stage == stage:
            raise remote.StepFailed(
                step.host.id, step.operation, self.fail_status, "stubbed failure", directory
            )
        files: dict[str, bytes] = {}
        if stage == "engine-inspect":
            assert role is not None
            sidecar = f"-sidecar-{role.split('-')[1]}.service"
            if role in self.containers or any(u.endswith(sidecar) for u in self.units):
                raise remote.StepFailed(
                    step.host.id, step.operation, 1, "ports: the engine port is in use", directory
                )
            files["inspection/host-inspection.json"] = b'{"passed": true}'
        elif stage == "engine-prepare":
            files["launch.json"] = json.dumps({"role": role, "nonce": uuid.uuid4().hex}).encode()
        elif stage == "engine-check":
            files["checked.json"] = b'{"plan": "checked"}'
        elif stage == "engine-start":
            assert role is not None
            cid = uuid.uuid4().hex
            started = f"2026-01-01T00:00:{sum(self.calls.values()):04d}Z"
            self.containers[role] = (cid, started)
            launch = re.search(r"start --run (\S+) --ready-seconds", step.command)
            assert launch is not None
            self.started_from[role] = launch.group(1)
            self.used[launch.group(1)] = cid
            files["container.id"] = cid.encode()
            files["container-start.json"] = json.dumps(
                {"container_id": cid, "started_at": started}
            ).encode()
        elif stage == "load-balancer":
            cid = uuid.uuid4().hex
            started = f"2026-01-01T00:00:{sum(self.calls.values()):04d}Z"
            self.containers["load-balancer"] = (cid, started)
            return f"{cid} {started}"
        elif stage == "monitoring":
            self.monitoring_up = True
            self.monitoring_command = step.command
            return "prometheus ready at http://127.0.0.1:9090 (abc, prom/prometheus)"
        elif stage == "engine-probe":
            files["probe/probe.json"] = b"{}"
        elif stage == "cache-capture":
            files["cache-layout.json"] = b'{"ranks": [{"kv_cache_layout": "HND"}]}'
        elif stage == "attest-capture":
            if "published" in step.operation:
                launch = {"role": role}
                files["attestation/engine-attestation.json"] = json.dumps(
                    {"launch": launch}
                ).encode()
            else:
                files["published-attestation.json"] = b"{}"
        elif stage == "sidecar-verify":
            files["check/attestation.json"] = b"{}"
        elif stage == "finalize-fleet":
            fleet = json.loads((self.package_dir / "node-1" / "fleet.local.json").read_text())  # type: ignore[operator]
            fleet["engine_contract"] = {"engine": "contract"}
            files["host/fleet.json"] = json.dumps(fleet).encode()
        elif stage in ("profile", "calibrate", "preflight"):
            work = str(directory.relative_to(self.run_directory))
            uploaded = next(
                (data for target, data in self.uploads.items() if f"{work}/fleet.json" in target),
                b"{}",
            )
            files["host/fleet.json"] = uploaded
            if stage == "profile":
                files["host/profiles.json"] = b"{}"
                files["host/profiles.samples.json"] = b"{}"
            elif stage == "calibrate":
                files["host/first-token-calibration.json"] = b'{"candidate_deadline_s": 1.5}'
            else:
                files["host/preflight-evidence.json"] = b"{}"
        remote.write(directory, files)
        return ""

    def upload(self, ssh: Any, host: Host, stage: str, source: Path, target: str) -> None:
        # As the real SSH does, a remote command of a stage takes that stage's time limit.
        stage_limit({}, stage)
        self.calls[f"upload {stage}"] += 1
        self.uploads[target] = source.read_bytes()

    # The SSH calls stages make directly.

    def ssh(self, env: Any, logs: Path) -> _SSH:
        return _SSH(self, logs)

    def restart_unit(self, name: str) -> None:
        """Restart a unit as systemd does after its process fails: a new invocation."""
        unit = next(unit for unit in self.units if unit.endswith(f"-{name}.service"))
        nonce, _, restarts = self.units[unit]
        self.units[unit] = (nonce, uuid.uuid4().hex, restarts + 1)

    # The systemd supervisor of each host.

    def supervisor(self, ssh: Any, host: Host, stage: str, run_id: str) -> _Units:
        return _Units(self, run_id, host.id)

    @contextlib.contextmanager
    def patched(self) -> Iterator[None]:
        with contextlib.ExitStack() as stack:
            targets = (
                (deploy.discovery, "discover", self.discover),
                (deploy.install, "package", self.package),
                (deploy.install, "load_run", self.load_run),
                (deploy.install, "install", self.install),
                (deploy.cache_groups, "derive", self.derive),
                (deploy.fabric, "load", self.load_fabric),
                (deploy.fabric, "group_budget", self.group_budget),
                (deploy.fabric, "qualify_edge", self.qualify_edge),
                (deploy.fabric, "observe", self.observe),
                (deploy.remote, "run", self.step),
                (deploy.remote, "upload", self.upload),
                (deploy.remote, "replace", self.upload),
                (deploy, "SSH", self.ssh),
                (deploy_cli, "SSH", self.ssh),
                (deploy, "load_hosts", lambda path, env: deploy.discovery.derive_hosts(env)),
                (deploy.Deployment, "supervisor", lambda _, *args: self.supervisor(*args)),
                (deploy.supervision, "check_systemd_host", lambda runner: []),
                (deploy_cli, "compose_problem", lambda runner: self.compose_problem),
                (deploy.AttestationDocument, "load", _document),
                (deploy, "launch_digest", lambda contract, launch: json.dumps(launch)),
            )
            for owner, name, value in targets:
                stack.enter_context(patch.object(owner, name, value))
            yield


@dataclass
class _Document:
    launch: dict
    contract: Any


class _Contract:
    def fields(self) -> dict:
        return {"engine": "contract"}


def _document(path: Path) -> _Document:
    return _Document(json.loads(Path(path).read_text())["launch"], _Contract())


class _SSH:
    def __init__(self, fleet: Fleet, logs: Path) -> None:
        self.fleet = fleet
        self.logs = logs

    def log(self, host: Host) -> Path:
        return self.logs / f"{host.id}.log"

    def clock_offset(self, host: Host, stage: str, operation: str, script: str, samples: int):
        assert "lease_check clock" in script, script
        stage_limit({}, stage)
        return self.fleet.clocks.get(host.id, (0.0, 0.01))

    def run(self, host: Host, stage: str, operation: str, script: str, payload=None, reason=None):
        fleet = self.fleet
        if operation == "management access":
            return host.id
        if operation == "read the installed fleet":
            assert fleet.package_dir is not None
            data = (fleet.package_dir / host.id / "fleet.local.json").read_bytes()
            return f"{hashlib.sha256(data).hexdigest()}  runs/deployment/fleet.json"
        if operation == "read the launch directory's container":
            for role, launch in fleet.started_from.items():
                if f"cat {launch}/container.id" in script:
                    fleet.calls[f"spent {role}"] += 1
                    return fleet.used.get(launch, "")
            return ""
        if operation == "remove the container an unrecorded start left":
            fleet.calls["removed unrecorded"] += 1
            return ""
        if operation.startswith("remove ") and operation.endswith(" container"):
            for role, (cid, _) in list(fleet.containers.items()):
                if cid in script:
                    del fleet.containers[role]
            return ""
        if operation == "home directory":
            return "/home/deploy"
        if operation == "read the engine container":
            for cid, started in fleet.containers.values():
                if cid in script:
                    return f"true {started}"
            return "gone"
        if operation.startswith("check the engine-") and operation.endswith(" launch"):
            role = operation.split()[2]
            launch = re.search(r"test -f (\S+)/launch\.json", script)
            assert launch is not None
            local = self._local(launch.group(1)) / "launch.json"
            lines = [hashlib.sha256(local.read_bytes()).hexdigest()]
            if fleet.started_from.get(role) != launch.group(1):
                return "\n".join([*lines, "unstarted"])
            state = fleet.containers.get(role)
            if state is None:
                return "\n".join([*lines, "gone", "{}"])
            return "\n".join([*lines, f"true {state[1]}", json.dumps({"started_at": state[1]})])
        if operation == "readmit restarted engines":
            engines = script.split("readmit", 1)[1].split()[2:]
            fleet.readmitted.append([part.strip("'") for part in engines])
            return _file("readmission.json", b"{}")
        if operation == "check the router":
            return "\n".join(_file(name, b"{}") for name in ("health.json", "ready.json"))
        if operation in (
            "ask whether the standby holds the lease",
            "ask whether the router controls the fleet",
        ):
            return "200" if _router_host(script) == fleet.active_router else "503"
        if operation == "measure the router overhead":
            fleet.calls[operation] += 1
            fleet.measured.append(script)
            measured = {"overhead_seconds": 0.01, "requests": 64, "problem": fleet.overhead_problem}
            return _file("overhead.json", json.dumps(measured).encode())
        if operation == "remove the lease":
            fleet.calls[operation] += 1
            return ""
        if operation == "read the lease mount":
            assert "lease_check mount /srv/shared" in script, script
            return fleet.mount(host.id)
        if operation == "check the lease mount":
            assert f"lease_check witness /srv/shared/{WITNESS_NAME} --check" in script, script
            if host.id in fleet.unmounted:
                return "missing"
            return f"present\n{fleet.mount(host.id)}"
        if operation == "read the pair's lease":
            fleet.calls[operation] += 1
            holder = fleet.lease_holder()
            if holder is None and fleet.lease_expired_ago is not None:
                expires = 1000.0 - fleet.lease_expired_ago
                return json.dumps(
                    {"holder": "node-2:old", "epoch": 2, "expires_at": expires, "now": 1000.0}
                )
            if holder is None:
                return json.dumps({"holder": None, "now": 1000.0})
            return json.dumps(
                {"holder": f"{holder}:token", "epoch": 1, "expires_at": 1005.0, "now": 1000.0}
            )
        if operation == "ask which router controls the fleet":
            return "200" if _router_host(script) == fleet.active_router else "503"
        if operation in _LEASE_CHECK:
            if operation == "read the claim" and not fleet.lease_coherent:
                return "null"
            return fleet.lease.run(operation, script)
        if operation == "read the monitoring containers":
            if "docker ps" in script:
                return "p1 prometheus\ng1 grafana\nr1 renderer"
            return "true\ntrue\ntrue" if fleet.monitoring_up else ""
        if operation == "remove the monitoring containers":
            fleet.monitoring_up = False
            return ""
        if operation.startswith("verify the restarted"):
            fleet.verified.append(operation)
            if fleet.fail_verify:
                raise RemoteError(host.id, stage, operation, self.log(host), ": stubbed")
        return ""

    def _local(self, remote_path: str) -> Path:
        """The workstation attempt directory of a host directory under runs/deploy/<run ID>/."""
        parts = Path(remote_path).parts
        return self.fleet.run_directory / Path(*parts[3:-1])


_LEASE_CHECK = (
    "leave the lease witness",
    "read the lease witness",
    "claim the probe lease",
    "read the claim",
    "claim the held lease",
    "hold the lease lock",
    "probe the held lock",
    "claim the expired lease",
    "read the takeover",
)


class ProbeLease:
    """The lease check's host commands, run through `lease_check` on one local directory that
    stands for the shared mount, on a clock that each SSH session advances as on real hosts."""

    # Seconds an SSH session that starts Python takes, as measured on the router hosts.
    STEP_S = 2.0

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.now = 1_000_000.0

    def clock(self) -> float:
        return self.now

    def local(self, path: str) -> str:
        """The local stand-in of a probe lease path on the shared mount."""
        self.directory.mkdir(exist_ok=True)
        return str(self.directory / Path(path).name)

    def hold(self, script: str) -> None:
        """Run the hold command as the host's shell would, from the SSH login directory, with
        the lease helper on this interpreter, so the shell's grouping is what the test checks."""
        home = self.directory.parent / "home"
        remote = re.match(r"cd (\S+) && ", script)
        assert remote is not None, script
        (home / remote[1]).mkdir(parents=True, exist_ok=True)
        probe = shlex.split(script.split(" hold ", 1)[1])[0]
        # The stand-in holds the lock for a fraction of the host's 4 seconds.
        held = f"{shlex.quote(probe)} 4 "
        assert held in script, script
        script = script.replace(".venv/bin/python", shlex.quote(sys.executable)).replace(
            held, f"{shlex.quote(self.local(probe))} 0.2 "
        )
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        done = subprocess.run(
            ["sh", "-c", script], cwd=home, env=environment, check=False, timeout=30
        )
        if done.returncode:
            raise RemoteError("node-1", "lease-check", "hold the lease lock", home, ": exit 1")

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def run(self, operation: str, script: str) -> str:
        self.now += self.STEP_S
        if operation == "hold the lease lock":
            self.hold(script)
            return ""
        if operation == "probe the held lock":
            return "blocked"
        argv = shlex.split(script.split("narwhal.deployment.lease_check", 1)[1])
        argv[1] = self.local(argv[1])
        if operation == "claim the expired lease":
            # On the fake clock the claim cannot wait out the real hold, so wait for it here.
            with open(argv[1] + ".lock", "a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lease = functools.partial(
            FileLease, wall_clock=self.clock, monotonic_clock=self.clock, sleep=self.sleep
        )
        clock = SimpleNamespace(monotonic=self.clock, sleep=self.sleep)
        output = io.StringIO()
        with (
            patch.object(lease_check, "FileLease", lease),
            patch.object(lease_check, "time", clock),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            assert lease_check.main(argv) == 0, (argv, output.getvalue())
        return output.getvalue().strip()


# Each router host's router origin in `environment`.
_ROUTER_HOSTS = {"10.0.0.1:8000": "node-1", "10.0.0.2:8000": "node-2"}


def _router_host(script: str) -> str:
    """The host whose router a status script asks about."""
    return next((host for origin, host in _ROUTER_HOSTS.items() if origin in script), "")


class _Units(Supervisor[SystemdUnit]):
    """Units that run until the test stops or restarts them, keyed by host and unit name."""

    def __init__(self, fleet: Fleet, run_id: str, host: str) -> None:
        self.fleet = fleet
        self.run_id = run_id
        self.host = host
        self.runner = None

    def unit(self, name: str) -> str:
        return f"{self.host}:narwhal-{self.run_id}-{name}.service"

    def start(self, spec: supervision.ProcessSpec) -> SystemdUnit:
        unit = self.unit(spec.name)
        assert unit not in self.fleet.units, f"{unit} is already loaded"
        nonce, invocation = uuid.uuid4().hex, uuid.uuid4().hex
        self.fleet.units[unit] = (nonce, invocation, 0)
        self.fleet.calls[f"start {spec.name}"] += 1
        self.fleet.argv[spec.name] = list(spec.argv)
        return self._identity(unit)

    def _identity(self, unit: str) -> SystemdUnit:
        nonce, invocation, restarts = self.fleet.units[unit]
        return SystemdUnit(unit, nonce, invocation, 100 + restarts, 5, "boot", restarts, "/env")

    def check(self, recorded: SystemdUnit) -> Observation[SystemdUnit]:
        if recorded.unit not in self.fleet.units:
            return Observation("stopped", "stopped")
        nonce, invocation, _ = self.fleet.units[recorded.unit]
        if nonce != recorded.nonce:
            return Observation("mismatch", "another start")
        if invocation == recorded.invocation_id:
            return Observation("running", "running", recorded)
        return Observation("restarted", "restarted", self._identity(recorded.unit))

    def _terminate(self, recorded: SystemdUnit) -> None:
        self.fleet.units.pop(recorded.unit, None)
        self.fleet.calls[f"stop {recorded.unit}"] += 1

    def discard(self, name: str) -> bool:
        return self.fleet.units.pop(self.unit(name), None) is not None
