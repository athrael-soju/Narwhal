"""`narwhal deploy plan|run|status|down`: the deployment stage graph on SSH hosts.

`run` runs the stages from host access to a ready router, each as the standalone command for
its gate runs it, with the outputs in the stage's attempt directory under `--run`. Host steps
run in the deployment directory that `install` made, under `runs/deploy/<run ID>/` with the
same attempt path, and their evidence comes back to the attempt directory. Sidecars and the
router run as transient systemd user units; engines run as the containers `narwhal-engine
start` creates.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from narwhal import provenance
from narwhal.engines.attestation import AttestationDocument, launch_digest

from . import cache_groups, discovery, fabric, install, remote, supervision
from .attestation_contract.capture import PUBLISHED
from .deploy_graph import (
    CACHE_CAPTURE,
    DEPLOY_GRAPH,
    EDGE,
    ENGINE,
    GROUP,
    HOST,
    ROUTER,
    TRANSFER_MEASUREMENT,
)
from .hosts import SSH, Host, load_hosts, stage_limit, write_private
from .launch_engine.backend import cache_capture, launcher, transfer_measurement
from .role_env import read_environment
from .run_record import PASSED, key
from .runner import Input, Outcome, Refreshed, Runner, StageContext

# `narwhal deploy run` ends at the router; the monitoring and trial stages come later.
LAST_STAGE = "router-start"
# Seconds a started sidecar or router has to answer its health route, and a router its
# readiness route.
HEALTH_SECONDS = 60
READY_SECONDS = 180
# Seconds a readmitted engine has to become active.
READMIT_SECONDS = 120
# The engine start keeps this much of its stage limit for the commands around the wait.
START_MARGIN_SECONDS = 60
DOCS = "https://athrael-soju.github.io/Narwhal/"
_LIST = re.compile(r"[1-9][0-9]*(?:,[1-9][0-9]*)*")
_COMPLETION_PROMPT = "Return one sentence about narwhals."

# The variables each input fact digests. `discovery` digests every other NARWHAL_ variable of
# the environment, apart from the ones below that no stage reads as an input.
SLO = ("NARWHAL_TTFT_S", "NARWHAL_TPOT_S", "NARWHAL_ROUTER_OVERHEAD_S")
MARGIN = "NARWHAL_FIRST_TOKEN_MARGIN_S"
PROFILE = (
    "NARWHAL_PROFILE_PREFILL_LENS",
    "NARWHAL_PROFILE_DECODE_INPUT_LENS",
    "NARWHAL_PROFILE_DECODE_CONCURRENCY",
    "NARWHAL_PROFILE_WARMUP_REQUESTS",
)
CALIBRATION = (
    "NARWHAL_CALIBRATION_INPUT_TOKENS",
    "NARWHAL_CALIBRATION_SAMPLES",
    "NARWHAL_CALIBRATION_OBSERVATION_TIMEOUT_S",
)
REVISION = (
    "NARWHAL_WHEEL",
    "NARWHAL_WHEEL_SHA256",
    "NARWHAL_REQUIREMENTS",
    "NARWHAL_REQUIREMENTS_SHA256",
    "NARWHAL_TOOLS_COMMIT",
)
ROUTER_INPUTS = ("NARWHAL_ROUTER_URL",)
MONITORING = (
    "NARWHAL_TARGETS_URL",
    "NARWHAL_GRAFANA_BIND_ADDRESS",
    "NARWHAL_PROMETHEUS_LISTEN_ADDRESS",
)
# Written by `run` for each stage, or about how the workstation reaches hosts, not inputs.
_NOT_INPUTS = re.compile(
    r"NARWHAL_(HOSTS|FLEET|LAUNCH_CONFIG|SSH_KNOWN_HOSTS|SSH_JUMP|STAGE_.*_TIMEOUT_SECONDS"
    r"|STAGE_TIMEOUT_SECONDS|STAGE_CLEANUP_GRACE_SECONDS|STAGE_KILL_GRACE_SECONDS"
    r"|NODE_[1-9][0-9]*_SSH(_PASSWORD)?|ROUTER_SSH(_PASSWORD)?|TRIAL_.*)"
)
_FABRIC_TEST = re.compile(r"NARWHAL_(FABRIC_TEST_PORT|(NODE_[1-9][0-9]*_)?RDMA_.*)")


class InputError(ValueError):
    """Deployment inputs are missing or malformed; `problems` names each one."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(problems))


def _number(env: Mapping[str, str], name: str, problems: list[str], *, minimum: float) -> float:
    value = env.get(name, "")
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not math.isfinite(number) or number < minimum:
        problems.append(f"set {name} to a finite number of at least {minimum:g}")
    return number


def _positive(env: Mapping[str, str], name: str, problems: list[str]) -> float:
    value = _number(env, name, problems, minimum=0)
    if value == 0:
        problems.append(f"set {name} to a positive number")
    return value


def _lengths(env: Mapping[str, str], name: str, problems: list[str], *, required: bool) -> str:
    value = env.get(name, "")
    if (value or required) and not _LIST.fullmatch(value):
        problems.append(f"set {name} to comma-separated positive integers")
    return value


def _integer(env: Mapping[str, str], name: str, problems: list[str], minimum: int) -> int:
    value = env.get(name, "")
    if not re.fullmatch(r"[0-9]+", value) or int(value) < minimum:
        problems.append(f"set {name} to an integer of at least {minimum}")
        return minimum
    return int(value)


@dataclass(frozen=True)
class Settings:
    """The values the profiling, calibration, fleet-limits and router stages use."""

    ttft_s: float
    tpot_s: float
    router_overhead_s: float
    margin_s: float
    prefill_lens: str
    decode_input_lens: str
    decode_concurrency: str
    warmup_requests: int
    input_tokens: str
    samples: int
    observation_timeout_s: float
    router_host: str
    router_port: int
    router_url: str


def settings(env: Mapping[str, str]) -> Settings:
    """Read the deployment's settings, naming every missing or malformed variable."""
    problems: list[str] = []
    ttft = _positive(env, "NARWHAL_TTFT_S", problems)
    tpot = _positive(env, "NARWHAL_TPOT_S", problems)
    overhead = _number(env, "NARWHAL_ROUTER_OVERHEAD_S", problems, minimum=0)
    margin = _positive(env, MARGIN, problems)
    prefill = _lengths(env, PROFILE[0], problems, required=True)
    decode = _lengths(env, PROFILE[1], problems, required=True)
    concurrency = _lengths(env, PROFILE[2], problems, required=False)
    warmup = _integer(env, PROFILE[3], problems, 0)
    tokens = _lengths(env, CALIBRATION[0], problems, required=True)
    samples = _integer(env, CALIBRATION[1], problems, 1)
    observation = _positive(env, CALIBRATION[2], problems)
    url = env.get("NARWHAL_ROUTER_URL", "")
    address = urlsplit(url)
    try:
        port = address.port
    except ValueError:
        port = None
    if address.scheme != "http" or not address.hostname or port is None or address.path.strip("/"):
        problems.append("set NARWHAL_ROUTER_URL to the router's http://HOST:PORT")
    if problems:
        raise InputError(problems)
    assert address.hostname is not None and port is not None
    return Settings(
        ttft,
        tpot,
        overhead,
        margin,
        prefill,
        decode,
        concurrency,
        warmup,
        tokens,
        samples,
        observation,
        address.hostname,
        port,
        url.rstrip("/"),
    )


def _values(env: Mapping[str, str], names: Sequence[str]) -> dict[str, str]:
    return {name: env[name] for name in names if name in env}


def inputs(env: Mapping[str, str]) -> dict[str, Input]:
    """The digest of every input fact of the deployment graph, after checking each input.

    Raises `InputError` naming every missing or malformed input, before any host is touched.
    """
    problems: list[str] = []
    try:
        settings(env)
    except InputError as error:
        problems.extend(error.problems)
    hosts: list[Host] = []
    try:
        hosts = discovery.derive_hosts(env)
    except ValueError as error:
        problems.append(str(error))
    known = Path(env.get("NARWHAL_SSH_KNOWN_HOSTS", ""))
    if not env.get("NARWHAL_SSH_KNOWN_HOSTS") or not known.is_file():
        problems.append("set NARWHAL_SSH_KNOWN_HOSTS to the known-hosts file with every host key")
    for check in (install.wheel, install.requirements, fabric.inputs):
        try:
            check(env)
        except ValueError as error:
            problems.append(str(error))
    if not re.fullmatch(r"[0-9a-f]{40}", env.get("NARWHAL_TOOLS_COMMIT", "")):
        problems.append("set NARWHAL_TOOLS_COMMIT to a full 40-hex commit SHA")
    for host in hosts:
        for role in host.roles:
            if role == "router":
                continue
            node = int(role.split("-")[1])
            try:
                discovery.expected_tree(env, node)
            except ValueError as error:
                problems.append(str(error))
            image = discovery.value(env, node, "ENGINE_IMAGE")
            if not re.fullmatch(r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}", image):
                problems.append(
                    f"{role}: pin NARWHAL_ENGINE_IMAGE to an image ID or registry digest"
                )
            problems.extend(
                f"{role}: set NARWHAL_{field} or NARWHAL_NODE_{node}_{field}"
                for field in ("MODEL_DIR", "RUN_DIR", "FABRIC_INTERFACE")
                if not discovery.value(env, node, field)
            )
    if not env.get("NARWHAL_ENGINE_MODEL_NAME"):
        problems.append("set NARWHAL_ENGINE_MODEL_NAME to the served model name")
    if problems:
        raise InputError(list(dict.fromkeys(problems)))
    access = {
        "passwords": sorted(h.password_env for h in hosts if h.password_env),
        "jump": env.get("NARWHAL_SSH_JUMP", ""),
        "known_hosts_sha256": hashlib.sha256(known.read_bytes()).hexdigest(),
    }
    destinations = {h.id: env[h.ssh_env] for h in hosts}
    claimed = {*SLO, MARGIN, *PROFILE, *CALIBRATION, *REVISION, *ROUTER_INPUTS, *MONITORING}
    claimed |= {name for _, name, _ in fabric.WORKLOAD}
    discovered = {
        name: value
        for name, value in env.items()
        if name.startswith("NARWHAL_")
        and name not in claimed
        and not _NOT_INPUTS.fullmatch(name)
        and not _FABRIC_TEST.fullmatch(name)
    }
    source = "environment"
    return {
        "access": Input.value(access, "NARWHAL_SSH_KNOWN_HOSTS and the password variables"),
        "destinations": Input.value(destinations, "the SSH destination variables"),
        "discovery": Input.value(discovered, source),
        "revision": Input.value(_values(env, REVISION), "the wheel, lock and tools commit"),
        "slo": Input.value(_values(env, SLO), source),
        "deadline_margin": Input.value(_values(env, (MARGIN,)), source),
        "profiling": Input.value(_values(env, PROFILE), source),
        "calibration": Input.value(_values(env, CALIBRATION), source),
        "fabric_workload": Input.value(
            _values(env, [name for _, name, _ in fabric.WORKLOAD]), source
        ),
        "fabric_test": Input.value(
            {name: value for name, value in env.items() if _FABRIC_TEST.fullmatch(name)}, source
        ),
        "router": Input.value(_values(env, ROUTER_INPUTS), source),
        "monitoring": Input.value(_values(env, MONITORING), source),
        "trial": Input.value(
            {name: value for name, value in env.items() if name.startswith("NARWHAL_TRIAL_")},
            source,
        ),
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


def _private(path: Path, document: Any) -> None:
    write_private(path, (json.dumps(document, indent=2) + "\n").encode())


def _node(role: str) -> int:
    return int(role.split("-")[1])


def recovery_url(page: str) -> str:
    """The published page of a gate section that `docs/` names as `page.md#anchor`."""
    path, _, anchor = page.partition("#")
    return DOCS + path.removesuffix(".md") + "/" + (f"#{anchor}" if anchor else "")


class Deployment:
    """The actions, live checks and topology of one SSH deployment in one run directory."""

    def __init__(self, directory: Path, env: Mapping[str, str]) -> None:
        self.directory = directory
        self.env = dict(env)
        self._fabric: tuple[tuple[str, str], fabric.Fabric] | None = None
        self.announce: Callable[[str], None] = lambda line: print(line, flush=True)

    # The runner and its inputs.

    def runner(self) -> Runner:
        actions = {
            "access": self.access,
            "discover": self.discover,
            "package": self.package,
            "install": self.install,
            "engine-inspect": self.engine_inspect,
            "engine-prepare": self.engine_prepare,
            "engine-check": self.engine_check,
            "engine-start": self.engine_start,
            "engine-probe": self.engine_probe,
            "cache-capture": self.cache_capture,
            "transfer-measure": self.transfer_measure,
            "cache-groups": self.cache_groups,
            "fabric-budget": self.fabric_budget,
            "fabric-edge": self.fabric_edge,
            "fleet-inventory": self.fleet_inventory,
            "attest-capture": self.attest_capture,
            "sidecar-start": self.sidecar_start,
            "sidecar-verify": self.sidecar_verify,
            "finalize-fleet": self.finalize_fleet,
            "profile": self.profile,
            "calibrate": self.calibrate,
            "fleet-limits": self.fleet_limits,
            "preflight": self.preflight,
            "router-start": self.router_start,
        }
        return Runner(
            DEPLOY_GRAPH,
            self.directory,
            target="ssh",
            revision=provenance.source_digest(),
            actions={name: self._announced(name, action) for name, action in actions.items()},
            topology=Topology(self),
            probes={
                "launch": self.launch_usable,
                "engine_identity": self.engine_running,
                "link": self.link_now,
                "sidecar_identity": self.sidecar_running,
                "router_identity": self.router_running,
            },
        )

    def _announced(
        self, name: str, action: Callable[[StageContext], Outcome | None]
    ) -> Callable[[StageContext], Outcome | None]:
        def announced(context: StageContext) -> Outcome | None:
            self.announce(f"{key(name, context.instance)}: attempt {context.attempt}")
            outcome = action(context)
            self.announce(f"{key(name, context.instance)}: passed")
            return outcome

        return announced

    # Hosts, recorded outputs and the environment of later stages.

    def hosts(self) -> list[Host]:
        return discovery.derive_hosts(self.env)

    def host(self, identifier: str) -> Host:
        return next(host for host in self.hosts() if host.id == identifier)

    def owner(self, role: str) -> Host:
        return next(host for host in self.hosts() if role in host.roles)

    def engines(self) -> list[str]:
        roles = [role for host in self.hosts() for role in host.roles if role != "router"]
        return sorted(roles, key=_node)

    def recorded(self, stage: str, instance: str = "") -> Path:
        """The attempt directory of a stage's last passed attempt, from the run record."""
        from . import run_record

        entry = run_record.read(self.directory)["stages"].get(key(stage, instance))
        if entry is None or entry["status"] != PASSED:
            raise ValueError(f"{key(stage, instance)} has not passed in {self.directory}")
        return self.directory / entry["directory"]

    def stage_env(self, discovered: Path) -> dict[str, str]:
        """The environment with discovery's configuration and derived variables."""
        config = discovered / "config"
        env = {
            **self.env,
            "NARWHAL_HOSTS": str(config / "hosts.local.json"),
            "NARWHAL_FLEET": str(config / "fleet.json"),
            "NARWHAL_LAUNCH_CONFIG": str(config / "engine-launch.local.json"),
        }
        env.update(read_environment(config / "deployment.env"))
        return env

    def _package(self, discovered: Path, packaged: Path) -> tuple[dict[str, str], dict, Path]:
        env = self.stage_env(discovered)
        package = packaged / "package"
        hosts = load_hosts(Path(env["NARWHAL_HOSTS"]), env)
        return env, install.load_run(package, hosts, env), package

    def package_of(self, context: StageContext) -> tuple[dict[str, str], dict, Path]:
        return self._package(context.outputs("discover"), context.outputs("package"))

    def recorded_package(self) -> tuple[dict[str, str], dict, Path]:
        return self._package(self.recorded("discover"), self.recorded("package"))

    def fabric_plan(
        self, env: Mapping[str, str], manifest: dict, package: Path, groups: Path
    ) -> fabric.Fabric:
        cache = (str(package), _digest(groups))
        if self._fabric is None or self._fabric[0] != cache:
            hosts = load_hosts(Path(env["NARWHAL_HOSTS"]), env)
            self._fabric = (cache, fabric.load(hosts, manifest, package, env, groups))
        return self._fabric[1]

    def recorded_fabric(self) -> fabric.Fabric:
        env, manifest, package = self.recorded_package()
        groups = self.recorded("cache-groups") / cache_groups.EVIDENCE
        return self.fabric_plan(env, manifest, package, groups)

    @staticmethod
    def remote(context: StageContext) -> str:
        """The attempt directory's counterpart in a host's deployment directory."""
        relative = context.directory.relative_to(context.run_directory)
        return f"runs/deploy/{context.run_id}/{relative}"

    def ssh(self, context: StageContext, env: Mapping[str, str] | None = None) -> SSH:
        return SSH(env or self.env, context.directory / "logs")

    def live_ssh(self) -> SSH:
        """SSH for live checks, logging beside the stage directories it reads."""
        return SSH(self.env, self.directory / "live")

    def run_id(self) -> str:
        from . import run_record

        return str(run_record.read(self.directory)["run_id"])

    def role_env(self, package: Path, role: str) -> dict[str, str]:
        return read_environment(package / self.owner(role).id / f".env.{role}")

    def launch(self, context: StageContext, role: str) -> str:
        return str(context.product("launch", role)["directory"])

    def step(
        self,
        context: StageContext,
        host: Host,
        role: str | None,
        operation: str,
        command: str,
        *,
        returns: Mapping[str, str] | None = None,
        workdir: bool = True,
        on_failure: str = "",
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Run one host step of this stage in a fresh host directory for its attempt."""
        _, manifest, _ = self.package_of(context)
        return remote.run(
            self.ssh(context, env),
            remote.Step(
                host,
                context.stage,
                operation,
                manifest["remote_dir"],
                role,
                command,
                self.remote(context) if workdir else None,
                returns,
                on_failure,
            ),
            context.directory,
        )

    # Stages up to the installed package.

    def access(self, context: StageContext) -> None:
        host = self.host(context.instance)
        name = SSH(self.env, context.directory / "logs").run(
            host, "access", "management access", "hostname"
        )
        write_private(context.directory / "hostname.txt", (name + "\n").encode())

    def discover(self, context: StageContext) -> None:
        config = context.directory / "config"
        config.mkdir(mode=0o700)
        env = {
            **self.env,
            "NARWHAL_HOSTS": str(config / "hosts.local.json"),
            "NARWHAL_FLEET": str(config / "fleet.json"),
            "NARWHAL_LAUNCH_CONFIG": str(config / "engine-launch.local.json"),
        }
        discovery.discover(env, (context.directory / "discovery").resolve())

    def package(self, context: StageContext) -> None:
        env = self.stage_env(context.outputs("discover"))
        hosts = load_hosts(Path(env["NARWHAL_HOSTS"]), env)
        install.package(hosts, env, (context.directory / "package").resolve())

    def install(self, context: StageContext) -> None:
        env, manifest, package = self.package_of(context)
        hosts = load_hosts(Path(env["NARWHAL_HOSTS"]), env)
        host = next(h for h in hosts if h.id == context.instance)
        install.install([host], manifest, package, self.ssh(context, env))

    # Engine stages.

    def engine_inspect(self, context: StageContext) -> None:
        role = context.instance
        work = self.remote(context)
        self.step(
            context,
            self.owner(role),
            role,
            "inspect the engine host",
            f"narwhal-engine inspect-host --out {shlex.quote(work + '/inspection')}",
            returns={"inspection/": f"{work}/inspection"},
        )

    def engine_prepare(self, context: StageContext) -> Outcome:
        role = context.instance
        launch = f"{self.remote(context)}/launch"
        self.step(
            context,
            self.owner(role),
            role,
            "prepare the launch plan",
            f"narwhal-engine prepare --out {shlex.quote(launch)}",
            returns={"launch.json": f"{launch}/launch.json"},
        )
        return Outcome(
            {
                "launch": {
                    "host": self.owner(role).id,
                    "directory": launch,
                    "plan_sha256": _digest(context.directory / "launch.json"),
                }
            }
        )

    def engine_check(self, context: StageContext) -> None:
        role = context.instance
        launch = self.launch(context, role)
        self.step(
            context,
            self.owner(role),
            role,
            "check the image and plan",
            f"narwhal-engine check --run {shlex.quote(launch)}",
            returns={
                "checked.json": f"{launch}/checked.json",
                "image-check.log": f"{launch}/image-check.log",
            },
        )

    def engine_start(self, context: StageContext) -> Outcome:
        role = context.instance
        host = self.owner(role)
        launch = shlex.quote(self.launch(context, role))
        env, _, _ = self.package_of(context)
        ready = int(stage_limit(env, "engine-start")) - START_MARGIN_SECONDS
        if ready < 1:
            raise ValueError(
                f"the engine-start stage limit leaves no time for readiness; raise it above "
                f"{START_MARGIN_SECONDS} seconds"
            )
        self._stop_previous_engine(context, host)
        work = self.remote(context)
        # A launch directory starts one engine once. An earlier start that this run did not
        # record left its container; it is removed so the next run prepares a fresh directory.
        command = f"""{{
if test -e {launch}/container.id; then
  cid=$(cat {launch}/container.id)
  docker rm --force "$cid" > /dev/null 2>&1 || true
  echo "an earlier start of this launch directory left container $cid, which was removed;" \\
    "run narwhal deploy run again to start the engine from a fresh launch directory"
  exit 4
fi
narwhal-engine start --run {launch} --ready-seconds {ready}
}}"""
        cleanup = (
            f"if test -e {launch}/container.id; then cid=$(cat {launch}/container.id); "
            f'docker logs --tail 200 "$cid" > {shlex.quote(work)}/engine.log 2>&1 || true; '
            f'docker rm --force "$cid" > /dev/null 2>&1 || true; fi'
        )
        self.step(
            context,
            host,
            role,
            "start the engine and wait for its health endpoint",
            command,
            returns={
                "container.id": f"{self.launch(context, role)}/container.id",
                "container-start.json": f"{self.launch(context, role)}/container-start.json",
                "engine.log": f"{work}/engine.log",
            },
            on_failure=cleanup,
        )
        record = _json(context.directory / "container-start.json")
        identity = {
            "host": host.id,
            "container_id": record["container_id"],
            "started_at": record["started_at"],
        }
        return Outcome({"engine_identity": identity}, ({"name": role, **identity},))

    def _stop_previous_engine(self, context: StageContext, host: Host) -> None:
        """Remove the engine an earlier attempt of this stage started, while it still runs."""
        previous = (context.previous or {}).get("products", {}).get("engine_identity")
        if not previous:
            return
        if self._container_state(self.ssh(context), host, context.stage, previous) is None:
            return
        self.announce(f"{context.instance}: removing the engine container of the earlier start")
        ssh = self.ssh(context)
        ssh.run(
            host,
            context.stage,
            "remove the earlier engine container",
            f"docker rm --force {shlex.quote(previous['container_id'])} > /dev/null",
        )

    @staticmethod
    def _container_state(ssh: SSH, host: Host, stage: str, identity: Mapping[str, Any]) -> Any:
        """The recorded container identity while that container runs from the same start."""
        output = ssh.run(
            host,
            stage,
            "read the engine container",
            "docker inspect --format '{{.State.Running}} {{.State.StartedAt}}' "
            f"{shlex.quote(identity['container_id'])} 2> /dev/null || echo 'gone'",
        )
        return identity if output == f"true {identity['started_at']}" else None

    def engine_probe(self, context: StageContext) -> None:
        role = context.instance
        launch = shlex.quote(self.launch(context, role))
        work = self.remote(context)
        self.step(
            context,
            self.owner(role),
            role,
            "probe the live engine",
            f"narwhal-engine probe --run {launch} --out {shlex.quote(work + '/probe')}",
            returns={"probe/": f"{work}/probe"},
        )

    def cache_capture(self, context: StageContext) -> Outcome:
        role = context.instance
        launch = self.launch(context, role)
        self.step(
            context,
            self.owner(role),
            role,
            "capture the live cache layout",
            f"narwhal-engine capture-cache --run {shlex.quote(launch)}",
            returns={"cache-layout.json": f"{launch}/cache-layout.json"},
            workdir=False,
        )
        return Outcome({"cache_geometry": _digest(context.directory / "cache-layout.json")})

    def transfer_measure(self, context: StageContext) -> Outcome:
        env, manifest, package = self.package_of(context)
        hosts = load_hosts(Path(env["NARWHAL_HOSTS"]), env)
        engines, peers, prompt = fabric.measure_handoffs(hosts, manifest, package, env)
        role = context.instance
        digest = fabric.measure_transfer(
            engines[role],
            engines[peers[role]],
            self.ssh(context, env),
            manifest["remote_dir"],
            prompt,
            context.directory / "transfer",
        )
        return Outcome({"handoff_geometry": digest})

    def cache_groups(self, context: StageContext) -> None:
        env, _, package = self.package_of(context)
        backend = launcher(env.get("NARWHAL_ENGINE_BACKEND"))
        layouts, transfers = {}, {}
        for role in self.engines():
            if cache_capture(backend) is not None:
                layouts[role] = context.outputs("cache-capture", role) / "cache-layout.json"
            if transfer_measurement(backend) is not None:
                transfers[role] = (
                    context.outputs("transfer-measure", role) / "transfer" / "transfer.json"
                )
        cache_groups.derive(
            Path(env["NARWHAL_LAUNCH_CONFIG"]), layouts, transfers, package, context.directory
        )

    def _fabric_of(self, context: StageContext) -> tuple[fabric.Fabric, dict[str, str]]:
        env, manifest, package = self.package_of(context)
        groups = context.outputs("cache-groups") / cache_groups.EVIDENCE
        return self.fabric_plan(env, manifest, package, groups), env

    def fabric_budget(self, context: StageContext) -> None:
        plan, env = self._fabric_of(context)
        group = next(group for group in plan.groups if group.name == context.instance)
        fabric.group_budget(plan, self.ssh(context, env), group, context.directory / "budget")

    def fabric_edge(self, context: StageContext) -> Outcome:
        plan, env = self._fabric_of(context)
        edge = next(edge for edge in plan.edges() if edge.name == context.instance)
        budgets = {
            role: context.outputs("fabric-budget", plan.group(role).name) / "budget" / "budget.json"
            for role in plan.engines
        }
        retained = None
        if context.previous is not None:
            earlier = self.directory / context.previous["directory"] / "edge"
            retained = earlier if earlier.is_dir() else None
        results = fabric.qualify_edge(
            plan, self.ssh(context, env), edge, budgets, context.directory / "edge", retained
        )
        return Outcome({"link": {result["sample"]: result["link_sha256"] for result in results}})

    # Attestation.

    def fleet_inventory(self, context: StageContext) -> None:
        """Check that the router's fleet names every engine and resolves each of its URLs."""
        env, manifest, package = self.package_of(context)
        host = self.host(context.instance)
        fleet_path = package / host.id / "fleet.local.json"
        fleet = _json(fleet_path)
        router = read_environment(package / host.id / ".env.router")
        problems = []
        engines = {f"n{_node(role)}" for role in self.engines()}
        listed = {engine["iid"] for engine in fleet["engines"]}
        if listed != engines:
            problems.append(
                f"the fleet lists engines {sorted(listed)}, and the launch records "
                f"{sorted(engines)}"
            )
        for engine in fleet["engines"]:
            for field in ("url", "attestation_url"):
                reference = re.fullmatch(r"\$\{([^}]+)\}", engine.get(field, ""))
                if reference and not router.get(reference[1]):
                    problems.append(
                        f"{engine['iid']} {field} {reference[1]} is unset in .env.router"
                    )
        installed = (
            SSH(env, context.directory / "logs")
            .run(
                host,
                context.stage,
                "read the installed fleet",
                f"cd {shlex.quote(manifest['remote_dir'])} && sha256sum runs/deployment/fleet.json",
            )
            .split()[0]
        )
        if installed != _digest(fleet_path):
            problems.append(
                "runs/deployment/fleet.json on the router host differs from the package's; "
                "restore it or package a new run"
            )
        if problems:
            raise ValueError("; ".join(problems))
        _private(
            context.directory / "inventory.json",
            {
                "engines": sorted(listed),
                "model": fleet["model"],
                "roles": {engine["iid"]: engine.get("role") for engine in fleet["engines"]},
                "fleet_sha256": installed,
            },
        )

    def attest_capture(self, context: StageContext) -> Outcome:
        role = context.instance
        launch = self.launch(context, role)
        quoted = shlex.quote(launch)
        argument = ""
        if (layout := self._representative_layout(context, role)) is not None:
            argument = f" --representative-layout {shlex.quote(layout)}"
        cli = "python -m narwhal.deployment.attestation_contract.cli"
        # A capture published before this run recorded it stands for this launch directory.
        command = f"{{ test -e {quoted}/{PUBLISHED} || {cli} capture --run {quoted}{argument}; }}"
        published = f'"$(python -c {shlex.quote(_PUBLISHED_ATTEMPT)} {quoted})"'
        self.step(
            context,
            self.owner(role),
            role,
            "capture the attestation",
            command,
            returns={PUBLISHED: f"{launch}/{PUBLISHED}"},
            workdir=False,
        )
        self.step(
            context,
            self.owner(role),
            role,
            "read the published attestation",
            f"cp -R {published} {shlex.quote(self.remote(context))}/attestation",
            returns={"attestation/": f"{self.remote(context)}/attestation"},
        )
        document = AttestationDocument.load(
            context.directory / "attestation" / "engine-attestation.json"
        )
        if document.launch is None:
            raise ValueError(f"{role}: the attestation holds no launch evidence")
        contract = document.contract.fields()
        return Outcome(
            {"binding": launch_digest(contract, document.launch), "engine_contract": contract}
        )

    def _representative_layout(self, context: StageContext, role: str) -> str | None:
        groups = _json(context.outputs("cache-groups") / cache_groups.EVIDENCE)["groups"]
        group = next(group for group in groups if role in group["roles"])
        representative = group["representative"]
        if representative == role:
            return None
        try:
            source = context.outputs("cache-capture", representative) / "cache-layout.json"
        except KeyError:
            return None
        names = {rank.get("kv_cache_layout") for rank in _json(source)["ranks"]}
        names.discard(None)
        if len(names) != 1:
            return None
        return str(names.pop())

    def supervisor(self, ssh: SSH, host: Host, stage: str, run_id: str) -> supervision.SystemdUnits:
        return supervision.SystemdUnits(run_id, remote.SSHRunner(ssh, host, stage))

    @staticmethod
    def home(ssh: SSH, host: Host, stage: str) -> str:
        return ssh.run(host, stage, "home directory", "cd && pwd")

    def _systemd_ready(self, supervisor: supervision.SystemdUnits, host: Host) -> None:
        if problems := supervision.check_systemd_host(supervisor.runner):
            raise ValueError(f"{host.id} cannot run supervised units: " + "; ".join(problems))

    def _supervise(
        self,
        context: StageContext,
        host: Host,
        name: str,
        argv: Sequence[str],
        environment: Mapping[str, str],
        checks: Sequence[tuple[str, int]],
    ) -> supervision.SystemdUnit:
        """Start `name` as a unit in place of the earlier attempt's, and wait for its routes."""
        env, manifest, _ = self.package_of(context)
        ssh = self.ssh(context, env)
        units = self.supervisor(ssh, host, context.stage, context.run_id)
        self._systemd_ready(units, host)
        for record in (context.previous or {}).get("processes", []):
            if record.get("name") == name:
                with contextlib.suppress(supervision.IdentityMismatch):
                    units.stop(supervision.identity_from_record(record))  # type: ignore[arg-type]
        # The unit name carries the run ID, so a unit of this name that no record names is
        # one an earlier attempt of this run started before it recorded it.
        units.discard(name)
        root = f"{self.home(ssh, host, context.stage)}/{manifest['remote_dir']}"
        work = f"{root}/{self.remote(context)}"
        ssh.run(
            host, context.stage, "create the attempt directory", f"mkdir -p {shlex.quote(work)}"
        )
        spec = supervision.ProcessSpec(
            name,
            tuple(part.replace("{root}", root) for part in argv),
            Path(root),
            Path(f"{work}/{name}.log"),
            dict(environment),
        )
        unit = units.start(spec)
        try:
            for url, seconds in checks:
                self._wait(ssh, host, context.stage, url, seconds, manifest["remote_dir"])
        except BaseException as error:
            with contextlib.suppress(supervision.SupervisionError, OSError, ValueError):
                units.stop(unit)
            error.add_note(f"{name} log on {host.id}: {work}/{name}.log")
            raise
        return unit

    @staticmethod
    def _wait(ssh: SSH, host: Host, stage: str, url: str, seconds: int, remote_dir: str) -> None:
        """Wait on `host` for `url` to answer HTTP 200."""
        ssh.run(host, stage, f"wait for {url}", _host_http(remote_dir, "wait", url, str(seconds)))

    def sidecar_start(self, context: StageContext) -> Outcome:
        role = context.instance
        host = self.owner(role)
        _, _, package = self.package_of(context)
        environment = self.role_env(package, role)
        url = environment.get(f"NARWHAL_NODE_{_node(role)}_ATTESTATION_URL", "")
        if not url:
            raise ValueError(f"{role}: its role environment names no attestation URL")
        launch = self.launch(context, role)
        unit = self._supervise(
            context,
            host,
            f"sidecar-{_node(role)}",
            (
                "{root}/.venv/bin/python",
                "-m",
                "narwhal.deployment.attestation_contract.cli",
                "serve",
                "--run",
                "{root}/" + launch,
            ),
            environment,
            [(url, HEALTH_SECONDS)],
        )
        identity = {"host": host.id, **unit.record()}
        return Outcome(
            {"sidecar_identity": identity}, ({"name": f"sidecar-{_node(role)}", **identity},)
        )

    def sidecar_verify(self, context: StageContext) -> None:
        role = context.instance
        launch = shlex.quote(self.launch(context, role))
        work = shlex.quote(self.remote(context))
        cli = "python -m narwhal.deployment.attestation_contract.cli"
        self.step(
            context,
            self.owner(role),
            role,
            "verify the sidecar against the live engine",
            f'{{ {cli} verify --run {launch} && cp -R "$(ls -dt {launch}/attestation-check-* | '
            f'head -n 1)" {work}/check; }}',
            returns={"check/": f"{self.remote(context)}/check"},
        )

    # Router stages.

    def _router_step(
        self,
        context: StageContext,
        operation: str,
        command: str,
        upload: Mapping[str, Path] | None = None,
    ) -> None:
        env, manifest, _ = self.package_of(context)
        host = self.host(context.instance)
        work = self.remote(context)
        ssh = self.ssh(context, env)
        ssh.run(
            host,
            context.stage,
            "create the attempt directory",
            f"cd {shlex.quote(manifest['remote_dir'])} && mkdir -p {shlex.quote(work)}",
        )
        for name, source in (upload or {}).items():
            remote.upload(
                ssh, host, context.stage, source, f"{manifest['remote_dir']}/{work}/{name}"
            )
        self.step(
            context,
            host,
            "router",
            operation,
            command,
            returns={"host/": work},
            workdir=False,
            env=env,
        )

    def finalize_fleet(self, context: StageContext) -> None:
        work = shlex.quote(self.remote(context))
        self._router_step(
            context,
            "finalize the fleet contract",
            f"{{ install -m 600 runs/deployment/fleet.json {work}/fleet.json && "
            f"python -m narwhal.deployment.attestation_contract.cli finalize-fleet "
            f"--fleet {work}/fleet.json; }}",
        )

    def profile(self, context: StageContext) -> None:
        values = settings(self.env)
        fleet = _json(context.outputs("finalize-fleet", context.instance) / "host" / "fleet.json")
        work = self.remote(context)
        fleet["profiles"]["path"] = f"{work}/profiles.json"
        draft = context.directory / "fleet.json"
        _private(draft, fleet)
        args = [
            "narwhal-profile",
            "--fleet",
            f"{work}/fleet.json",
            "--limits",
            "runs/deployment/profiling-limits.json",
            "--prefill-lens",
            values.prefill_lens,
            "--decode-input-lens",
            values.decode_input_lens,
            "--warmup-requests",
            str(values.warmup_requests),
        ]
        if values.decode_concurrency:
            args += ["--decode-concurrency", values.decode_concurrency]
        env, _, _ = self.package_of(context)
        if transfer_measurement(launcher(env.get("NARWHAL_ENGINE_BACKEND"))) is not None:
            # The sweep switches each engine's peer; no router serves the engines yet.
            args.append("--no-router")
        self._router_step(
            context, "profile the idle engines", shlex.join(args), {"fleet.json": draft}
        )

    def calibrate(self, context: StageContext) -> None:
        values = settings(self.env)
        source = context.outputs("profile", context.instance) / "host" / "fleet.json"
        work = self.remote(context)
        self._router_step(
            context,
            "calibrate the first-token deadline",
            shlex.join(
                [
                    "narwhal-check",
                    "--fleet",
                    f"{work}/fleet.json",
                    "--calibrate-first-token",
                    "--input-tokens",
                    values.input_tokens,
                    "--samples",
                    str(values.samples),
                    "--observation-timeout-s",
                    f"{values.observation_timeout_s:g}",
                    "--calibration-out",
                    f"{work}/first-token-calibration.json",
                ]
            ),
            {"fleet.json": source},
        )

    def fleet_limits(self, context: StageContext) -> None:
        """Write the SLO targets, deadline and calibration into every router's fleet file."""
        values = settings(self.env)
        env, manifest, _ = self.package_of(context)
        routers = Topology(self).instances(ROUTER)
        first = routers[0]
        profiled = context.outputs("profile", first) / "host"
        calibrated = context.outputs("calibrate", first) / "host"
        artifact = _json(calibrated / "first-token-calibration.json")
        candidate = artifact.get("candidate_deadline_s")
        if not isinstance(candidate, (int, float)) or not math.isfinite(candidate):
            raise ValueError("the first-token calibration names no candidate deadline")
        work = self.remote(context)
        fleet = _json(calibrated / "fleet.json")
        fleet["slo"] = {
            "ttft_s": values.ttft_s,
            "tpot_s": values.tpot_s,
            "router_overhead_s": values.router_overhead_s,
        }
        engine = fleet.setdefault("engine", {})
        engine["first_token_timeout_s"] = round(candidate + values.margin_s, 6)
        engine["first_token_calibration_path"] = f"{work}/first-token-calibration.json"
        fleet["profiles"]["path"] = f"{work}/profiles.json"
        fleet.setdefault("recovery", {})["state_path"] = f"{work}/router-state.json"
        _private(context.directory / "fleet.json", fleet)
        copies = {
            "first-token-calibration.json": calibrated / "first-token-calibration.json",
            "profiles.json": profiled / "profiles.json",
            "profiles.samples.json": profiled / "profiles.samples.json",
        }
        for name, source in copies.items():
            write_private(context.directory / name, source.read_bytes())
        ssh = self.ssh(context, env)
        for router in routers:
            host = self.host(router)
            for name in ("fleet.json", *copies):
                remote.upload(
                    ssh,
                    host,
                    context.stage,
                    context.directory / name,
                    f"{manifest['remote_dir']}/{work}/{name}",
                )
        self.announce(
            f"fleet-limits: first-token deadline {engine['first_token_timeout_s']}s from the "
            f"calibrated candidate {candidate}s"
        )

    def _limits(self, context: StageContext) -> str:
        """The host path of the fleet file that fleet-limits wrote."""
        relative = context.outputs("fleet-limits").relative_to(context.run_directory)
        return f"runs/deploy/{context.run_id}/{relative}/fleet.json"

    def preflight(self, context: StageContext) -> None:
        work = self.remote(context)
        self._router_step(
            context,
            "run preflight",
            shlex.join(
                [
                    "narwhal-check",
                    "--fleet",
                    self._limits(context),
                    "--evidence-out",
                    f"{work}/preflight-evidence.json",
                ]
            ),
        )

    def router_start(self, context: StageContext) -> Outcome:
        values = settings(self.env)
        host = self.host(context.instance)
        env, manifest, package = self.package_of(context)
        ssh = self.ssh(context, env)
        units = self.supervisor(ssh, host, context.stage, context.run_id)
        fleet = context.outputs("fleet-limits") / "fleet.json"
        engines = {role: context.product("engine_identity", role) for role in self.engines()}
        state = {"fleet_sha256": _digest(fleet), "engines": engines}
        previous = context.previous
        if previous is not None and previous["status"] == PASSED:
            kept = self._readmit(context, host, units, previous, state, values)
            if kept is not None:
                return kept
        unit = self._supervise(
            context,
            host,
            "router",
            (
                "{root}/.venv/bin/narwhal-serve",
                "--fleet",
                "{root}/" + self._limits(context),
                "--host",
                values.router_host,
                "--port",
                str(values.router_port),
                "--journal",
                "{root}/" + self.remote(context) + "/journal.jsonl",
            ),
            self.role_env(package, "router"),
            [
                (values.router_url + "/health", HEALTH_SECONDS),
                (values.router_url + "/ready", READY_SECONDS),
            ],
        )
        _private(context.directory / "router.json", state)
        self._router_responses(context, host, env, manifest["remote_dir"], values, completion=True)
        identity = {"host": host.id, **unit.record()}
        return Outcome({"router_identity": identity}, ({"name": "router", **identity},))

    def _readmit(
        self,
        context: StageContext,
        host: Host,
        units: supervision.SystemdUnits,
        previous: Mapping[str, Any],
        state: Mapping[str, Any],
        values: Settings,
    ) -> Outcome | None:
        """Readmit restarted engines to a router that still runs on an unchanged fleet.

        Returns None when the router must start again: it stopped, or the fleet changed.
        """
        earlier = self.directory / previous["directory"] / "router.json"
        recorded = previous["products"].get("router_identity")
        if not earlier.is_file() or recorded is None:
            return None
        before = _json(earlier)
        if before["fleet_sha256"] != state["fleet_sha256"]:
            return None
        identity = supervision.identity_from_record(recorded)
        observation = units.check(identity)  # type: ignore[arg-type]
        if observation.state != "running":
            return None
        restarted = sorted(
            role
            for role, engine in state["engines"].items()
            if before["engines"].get(role) != engine
        )
        env, manifest, _ = self.package_of(context)
        iids = [f"n{_node(role)}" for role in restarted]
        self.announce(f"router-start: readmitting {', '.join(iids) or 'no engines'}")
        output = self.ssh(context, env).run(
            host,
            context.stage,
            "readmit restarted engines",
            _host_http(
                manifest["remote_dir"],
                "readmit",
                values.router_url,
                str(READMIT_SECONDS),
                *iids,
            ),
        )
        remote.write(context.directory, remote.files(output))
        _private(context.directory / "router.json", state)
        self._router_responses(context, host, env, manifest["remote_dir"], values, completion=False)
        current = observation.identity
        assert current is not None
        kept = {"host": host.id, **current.record()}
        return Outcome({"router_identity": kept}, ({"name": "router", **kept},))

    def _router_responses(
        self,
        context: StageContext,
        host: Host,
        env: Mapping[str, str],
        remote_dir: str,
        values: Settings,
        *,
        completion: bool,
    ) -> None:
        """Retain the router's health, readiness, state and metrics, and one completion."""
        fleet = _json(context.outputs("fleet-limits") / "fleet.json")
        body = json.dumps({"model": fleet["model"], "prompt": _COMPLETION_PROMPT, "max_tokens": 32})
        args = ["responses", values.router_url] + (["--completion", body] if completion else [])
        script = _host_http(remote_dir, *args)
        output = SSH(env, context.directory / "logs").run(
            host, context.stage, "check the router", script
        )
        remote.write(context.directory / "router", remote.files(output))

    # Live checks: each returns the recorded value while it still holds, `Refreshed` for a
    # supervised process that restarted and passed its check again, and None once gone.

    def launch_usable(self, instance: str, recorded: Any) -> Any:
        """A launch directory holds until the engine it started stops or starts again."""
        launch = shlex.quote(recorded["directory"])
        _, manifest, _ = self.recorded_package()
        script = f"""cd {shlex.quote(manifest["remote_dir"])}
test -f {launch}/launch.json || {{ echo missing; exit 0; }}
sha256sum {launch}/launch.json | cut -d' ' -f1
test -e {launch}/container.id || {{ echo unstarted; exit 0; }}
cid=$(cat {launch}/container.id)
docker inspect --format '{{{{.State.Running}}}} {{{{.State.StartedAt}}}}' "$cid" 2> /dev/null ||
  echo gone
cat {launch}/container-start.json 2> /dev/null || echo '{{}}'
"""
        output = (
            self.live_ssh()
            .run(
                self.host(recorded["host"]),
                "engine-prepare",
                f"check the {instance} launch",
                script,
            )
            .splitlines()
        )
        if not output or output[0] == "missing" or output[0] != recorded["plan_sha256"]:
            return None
        if output[1:2] == ["unstarted"]:
            return recorded
        try:
            start = json.loads("\n".join(output[2:]))
        except ValueError:
            return None
        return recorded if output[1:2] == [f"true {start.get('started_at')}"] else None

    def engine_running(self, instance: str, recorded: Any) -> Any:
        return self._container_state(
            self.live_ssh(), self.host(recorded["host"]), "engine-start", recorded
        )

    def link_now(self, instance: str, recorded: Any) -> Any:
        plan = self.recorded_fabric()
        edge = next(edge for edge in plan.edges() if edge.name == instance)
        return {
            observation.sample.name: observation.fingerprint
            for observation in fabric.observe(plan, self.live_ssh(), edge)
        }

    def _confirmed(
        self, recorded: Mapping[str, Any], stage: str, verify: Callable[[SSH, Host], None]
    ) -> Any:
        host = self.host(str(recorded["host"]))
        ssh = self.live_ssh()
        units = self.supervisor(ssh, host, stage, self.run_id())
        identity = supervision.identity_from_record(recorded)
        try:
            current = units.confirm(identity, lambda _: verify(ssh, host))  # type: ignore[arg-type]
        except supervision.SupervisionError:
            return None
        if current == identity:
            return dict(recorded)
        return Refreshed({"host": host.id, **current.record()})

    def sidecar_running(self, instance: str, recorded: Any) -> Any:
        """The sidecar's unit, after `verify` passes again for a unit systemd restarted."""
        _, manifest, _ = self.recorded_package()
        entry = self._recorded_entry("engine-prepare", instance)
        launch = shlex.quote(entry["products"]["launch"]["directory"])

        def verify(ssh: SSH, host: Host) -> None:
            ssh.run(
                host,
                "sidecar-verify",
                f"verify the restarted {instance} sidecar",
                remote.prelude(manifest["remote_dir"], instance)
                + f"python -m narwhal.deployment.attestation_contract.cli verify --run {launch}",
            )

        return self._confirmed(recorded, "sidecar-start", verify)

    def router_running(self, instance: str, recorded: Any) -> Any:
        """The router's unit, after `/health` answers again for a unit systemd restarted."""
        values = settings(self.env)
        _, manifest, _ = self.recorded_package()
        return self._confirmed(
            recorded,
            "router-start",
            lambda ssh, host: self._wait(
                ssh,
                host,
                "router-start",
                values.router_url + "/health",
                HEALTH_SECONDS,
                manifest["remote_dir"],
            ),
        )

    def _recorded_entry(self, stage: str, instance: str) -> dict[str, Any]:
        from . import run_record

        entry: dict[str, Any] = run_record.read(self.directory)["stages"][key(stage, instance)]
        return entry

    # `down`: stop what the run started.

    def down(self) -> list[dict[str, Any]]:
        """Stop the router, the sidecars and the engines this run started, newest first.

        Each is stopped only while its recorded identity still matches.
        """
        from . import run_record

        stopped = []
        with run_record.locked(self.directory):
            record = run_record.read(self.directory)
            entries = record["stages"]
            ssh = self.live_ssh()
            for stage in ("router-start", "sidecar-start"):
                for entry in entries.values():
                    if entry["stage"] != stage:
                        continue
                    for process in entry["processes"]:
                        host = self.host(process["host"])
                        units = self.supervisor(ssh, host, stage, record["run_id"])
                        identity = supervision.identity_from_record(process)
                        try:
                            ran = units.stop(identity)  # type: ignore[arg-type]
                        except supervision.IdentityMismatch:
                            # Another start owns the unit now; it is not this run's to stop.
                            ran = False
                        stopped.append({"name": process["name"], "host": host.id, "stopped": ran})
            for entry in entries.values():
                if entry["stage"] != "engine-start":
                    continue
                for process in entry["processes"]:
                    host = self.host(process["host"])
                    ran = self._container_state(ssh, host, "down", process) is not None
                    if ran:
                        ssh.run(
                            host,
                            "down",
                            f"remove the {process['name']} container",
                            f"docker rm --force {shlex.quote(process['container_id'])} > /dev/null",
                        )
                    stopped.append({"name": process["name"], "host": host.id, "stopped": ran})
        return stopped


def _host_http(remote_dir: str, *args: str) -> str:
    """A host script that runs one `host_http` command from the installed wheel."""
    return f"cd {shlex.quote(remote_dir)} && " + shlex.join(
        [".venv/bin/python", "-m", "narwhal.deployment.host_http", *args]
    )


_PUBLISHED_ATTEMPT = (
    "import sys; from pathlib import Path; "
    "from narwhal.deployment.attestation_contract.capture import published_attempt; "
    "print(published_attempt(Path(sys.argv[1])))"
)


class Topology:
    """The deployment's instances, read from the inputs and from the outputs of passed stages."""

    def __init__(self, deployment: Deployment) -> None:
        self.deployment = deployment

    def instances(self, scope: str) -> Sequence[str]:
        hosts = self.deployment.hosts()
        if scope == HOST:
            # The engine 1 host installs first, and a failure there stops the other installs.
            return [host.id for host in sorted(hosts, key=lambda h: "engine-1" not in h.roles)]
        if scope == ENGINE:
            return self.deployment.engines()
        if scope == ROUTER:
            return [host.id for host in hosts if "router" in host.roles]
        if scope == GROUP:
            path = self.deployment.recorded("cache-groups") / cache_groups.EVIDENCE
            return [group["representative"] for group in _json(path)["groups"]]
        if scope == EDGE:
            return [edge.name for edge in self.deployment.recorded_fabric().edges()]
        raise ValueError(f"the deployment topology has no scope {scope}")

    def capable(self, capability: str, instance: str) -> bool:
        backend = launcher(self.deployment.env.get("NARWHAL_ENGINE_BACKEND"))
        if capability == CACHE_CAPTURE:
            return cache_capture(backend) is not None
        if capability == TRANSFER_MEASUREMENT:
            return transfer_measurement(backend) is not None
        raise ValueError(f"the deployment topology has no capability {capability}")

    def related(self, relation: str, instance: str) -> Sequence[str]:
        path = self.deployment.recorded("cache-groups") / cache_groups.EVIDENCE
        group = next(g for g in _json(path)["groups"] if instance in g["roles"])
        if relation == "group":
            return [group["representative"]]
        if relation == "group_edges":
            hosts = {self.deployment.owner(role).id for role in group["roles"]}
            return [
                edge.name
                for edge in self.deployment.recorded_fabric().edges()
                if edge.source.id in hosts
            ]
        raise ValueError(f"the deployment topology has no relation {relation}")
