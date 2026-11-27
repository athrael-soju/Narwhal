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
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from narwhal import provenance
from narwhal.engines.attestation import AttestationDocument, launch_digest

from ..observability.stack.make_targets import FLEET_NAME, alertmanager_origin
from ..profiling.calibration import MIN_SAMPLES_PER_GROUP
from ..runtime.lease import DEFAULT_SAFETY_MARGIN_S, WITNESS_NAME
from ..runtime.standby import MAX_HANDOFF_AGE_S
from . import (
    balancer,
    cache_groups,
    deployment_status,
    discovery,
    fabric,
    install,
    remote,
    supervision,
)
from .attestation_contract.capture import PUBLISHED
from .deploy_graph import (
    CACHE_CAPTURE,
    DEPLOY_GRAPH,
    EDGE,
    ENGINE,
    GROUP,
    HOST,
    ROUTER,
    ROUTER_PAIR,
    TRANSFER_MEASUREMENT,
)
from .hosts import (
    HOST_CAUSE,
    ROUTER_ROLES,
    SSH,
    Host,
    RemoteError,
    RemoteTimeout,
    load_hosts,
    stage_limit,
    write_private,
)
from .launch_engine.backend import cache_capture, launcher, transfer_measurement
from .launch_engine.evidence import CheckFailed
from .role_env import read_environment
from .run_record import PASSED, key
from .runner import Input, Outcome, Refreshed, Runner, Spent, StageContext, check_directory

# `narwhal deploy run` ends at the router; the monitoring and trial stages come later.
LAST_STAGE = "monitoring"
# Seconds a started sidecar or router has to answer its health route, and a router its
# readiness route.
HEALTH_SECONDS = 60
READY_SECONDS = 180
# Seconds a readmitted engine has to become active.
READMIT_SECONDS = 120
# Seconds between checks for the router that controls the fleet.
READY_POLL_SECONDS = 2
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
)
ROUTER_INPUTS = (
    "NARWHAL_ROUTER_URL",
    "NARWHAL_STANDBY_ROUTER_URL",
    "NARWHAL_LOAD_BALANCER_URL",
    "NARWHAL_LEASE_PATH",
    "NARWHAL_HAPROXY_IMAGE",
)
# The probe lease's TTL in the lease check. Each step between the claim and the standby's
# refused claim is an SSH session that starts Python, so the lease must outlive several.
LEASE_CHECK_TTL_S = 30
# Clock exchanges with each router host that bound their relative skew.
CLOCK_SAMPLES = 8
MONITORING = (
    "NARWHAL_FLEET_NAME",
    "NARWHAL_ALERTMANAGER_URL",
    "NARWHAL_GRAFANA_BIND_ADDRESS",
    "NARWHAL_PROMETHEUS_LISTEN_ADDRESS",
)
# Written by `run` for each stage, about how the workstation reaches hosts, or read only by
# `make observe`, not inputs.
_NOT_INPUTS = re.compile(
    r"NARWHAL_(HOSTS|FLEET|LAUNCH_CONFIG|TARGETS_URL|SSH_KNOWN_HOSTS|SSH_JUMP|STAGE_.*_TIMEOUT_SECONDS"
    r"|STAGE_TIMEOUT_SECONDS|STAGE_CLEANUP_GRACE_SECONDS|STAGE_KILL_GRACE_SECONDS"
    r"|NODE_[1-9][0-9]*_SSH(_PASSWORD)?|(STANDBY_)?ROUTER_SSH(_PASSWORD)?"
    r"|LOAD_BALANCER_SSH(_PASSWORD)?|TRIAL_.*)"
)
_FABRIC_TEST = re.compile(r"NARWHAL_(FABRIC_TEST_PORT|(NODE_[1-9][0-9]*_)?RDMA_.*)")


def _pause(seconds: float) -> None:
    """Wait before a router start; the tests replace it."""
    time.sleep(seconds)


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
    samples = _integer(env, CALIBRATION[1], problems, MIN_SAMPLES_PER_GROUP)
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


# The requests the primary router serves at once, and in all, while router-start measures the
# time it adds before the first token.
OVERHEAD_CONCURRENCY = 8
OVERHEAD_REQUESTS = 64
_LOOPBACK = re.compile(r"localhost|127\.[0-9.]+|::1|\[::1\]")
_IMAGE = re.compile(r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}")
# An absolute lease file path in a directory below the root, of plain path segments.
_LEASE_PATH = re.compile(r"(?:/[A-Za-z0-9_][A-Za-z0-9._-]*){2,}")


@dataclass(frozen=True)
class Pair:
    """The router pair's standby, load balancer, lease and HAProxy image."""

    standby_url: str
    balancer_url: str
    balancer_bind: str
    lease_path: str
    haproxy_image: str


def _origin(env: Mapping[str, str], name: str, what: str, problems: list[str]) -> str:
    """`name`'s `host:port`, or "" after naming the problem."""
    address = urlsplit(env.get(name, ""))
    try:
        port = address.port
    except ValueError:
        port = None
    if address.scheme != "http" or not address.hostname or port is None or address.path.strip("/"):
        problems.append(f"set {name} to {what}'s http://HOST:PORT")
        return ""
    return address.netloc


def paired(env: Mapping[str, str]) -> bool:
    """Whether the fleet runs a router pair: a standby router on another host, a shared lease
    and a load balancer. Without `NARWHAL_STANDBY_ROUTER_SSH` it runs one router, with no
    router failover."""
    return bool(env.get("NARWHAL_STANDBY_ROUTER_SSH"))


SINGLE_ROUTER = (
    "NARWHAL_STANDBY_ROUTER_SSH is unset, so the fleet runs one router with no standby, lease "
    "or load balancer: it has no router failover"
)


def pair(env: Mapping[str, str]) -> Pair:
    """Read the router pair's settings, naming every missing or malformed variable."""
    problems: list[str] = []
    # `settings` names a malformed NARWHAL_ROUTER_URL.
    primary = _origin(env, "NARWHAL_ROUTER_URL", "the primary router", [])
    standby = _origin(env, "NARWHAL_STANDBY_ROUTER_URL", "the standby router", problems)
    for name, origin in (("NARWHAL_ROUTER_URL", primary), ("NARWHAL_STANDBY_ROUTER_URL", standby)):
        if origin and _LOOPBACK.fullmatch(urlsplit(env[name]).hostname or ""):
            problems.append(
                f"set {name} to an address the other router and the load balancer reach, "
                "not a loopback address"
            )
    if primary and primary == standby:
        problems.append("set NARWHAL_STANDBY_ROUTER_URL to another origin than the primary's")
    bind = _origin(env, "NARWHAL_LOAD_BALANCER_URL", "the load balancer", problems)
    lease = env.get("NARWHAL_LEASE_PATH", "")
    if not _LEASE_PATH.fullmatch(lease) or "/." in lease:
        problems.append(
            "set NARWHAL_LEASE_PATH to the absolute path of the lease file, in a directory on "
            "storage that both router hosts mount at that path, of letters, digits, '.', '_' "
            "and '-'"
        )
    image = env.get("NARWHAL_HAPROXY_IMAGE", "")
    if not _IMAGE.fullmatch(image):
        problems.append("pin NARWHAL_HAPROXY_IMAGE to an image ID or registry digest")
    if problems:
        raise InputError(problems)
    return Pair(
        env["NARWHAL_STANDBY_ROUTER_URL"].rstrip("/"),
        env["NARWHAL_LOAD_BALANCER_URL"].rstrip("/"),
        bind,
        lease,
        image,
    )


def _values(env: Mapping[str, str], names: Sequence[str]) -> dict[str, str]:
    return {name: env[name] for name in names if name in env}


def inputs(env: Mapping[str, str]) -> dict[str, Input]:
    """The digest of every input fact of the deployment graph, after checking each input.

    Raises `InputError` naming every missing or malformed input, before any host is touched.
    """
    problems: list[str] = []
    for reader in (settings, pair) if paired(env) else (settings,):
        try:
            reader(env)
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
    for host in hosts:
        for role in host.roles:
            if not role.startswith("engine-"):
                continue
            node = int(role.split("-")[1])
            try:
                discovery.expected_tree(env, node)
            except ValueError as error:
                problems.append(str(error))
            image = discovery.value(env, node, "ENGINE_IMAGE")
            if not _IMAGE.fullmatch(image):
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
    if not FLEET_NAME.fullmatch(env.get("NARWHAL_FLEET_NAME", "")):
        problems.append(
            "set NARWHAL_FLEET_NAME to the fleet's name, lowercase letters, digits, '.', '_' or "
            "'-', which labels every series"
        )
    if env.get("NARWHAL_ALERTMANAGER_URL"):
        try:
            alertmanager_origin(env["NARWHAL_ALERTMANAGER_URL"])
        except ValueError as error:
            problems.append(str(error))
    if problems:
        raise InputError(list(dict.fromkeys(problems)))
    access = {
        "passwords": sorted(h.password_env for h in hosts if h.password_env),
        "jump": env.get("NARWHAL_SSH_JUMP", ""),
        "known_hosts_sha256": hashlib.sha256(known.read_bytes()).hexdigest(),
    }
    # A role that moves between hosts changes the package.
    destinations = {h.id: {"ssh": env[h.ssh_env], "roles": list(h.roles)} for h in hosts}
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
        "revision": Input.value(_values(env, REVISION), "the wheel and lock"),
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

    def __init__(self, directory: Path, env: Mapping[str, str], *, dry: bool = False) -> None:
        self.directory = directory
        self.env = dict(env)
        # A dry deployment's probes change nothing on a host: `plan`, and `status` while
        # another runner holds the run.
        self.dry = dry
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
            "lease-check": self.lease_check,
            "router-start": self.router_start,
            "load-balancer": self.load_balancer,
            "monitoring": self.monitoring,
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
                "load_balancer_identity": self.balancer_running,
                "monitoring_identity": self.monitoring_running,
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
        roles = [role for host in self.hosts() for role in host.roles if role.startswith("engine-")]
        return sorted(roles, key=_node)

    def primary(self) -> str:
        """The primary router's host."""
        return self.owner("router").id

    def router_role(self, identifier: str) -> str:
        """The router role on a router host: `router` for the primary, else `standby-router`."""
        return "router" if "router" in self.host(identifier).roles else "standby-router"

    def router_url(self, identifier: str) -> str:
        """The origin the router on a router host serves."""
        if self.router_role(identifier) == "router":
            return settings(self.env).router_url
        return pair(self.env).standby_url

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
        self._release_role(context, role)
        self.step(
            context,
            self.owner(role),
            role,
            "inspect the engine host",
            f"narwhal-engine inspect-host --out {shlex.quote(work + '/inspection')}",
            returns={"inspection/": f"{work}/inspection"},
        )

    def _release_role(self, context: StageContext, role: str) -> None:
        """Stop this run's sidecar and engine of `role`, which hold the ports inspection checks.

        A rerun of `engine-inspect` reruns the engine's start too, which would replace both.
        Each is stopped only while its recorded identity still matches.
        """
        from . import run_record

        stages = run_record.read(self.directory)["stages"]
        host = self.owner(role)
        ssh = self.ssh(context)
        for process in (stages.get(key("sidecar-start", role)) or {}).get("processes", []):
            units = self.supervisor(ssh, host, context.stage, context.run_id)
            with contextlib.suppress(supervision.IdentityMismatch):
                if units.stop(supervision.identity_from_record(process)):  # type: ignore[arg-type]
                    self.announce(f"{role}: stopped this run's sidecar for the inspection")
        for process in (stages.get(key("engine-start", role)) or {}).get("processes", []):
            if self._container_state(ssh, host, context.stage, process) is not None:
                ssh.run(
                    host,
                    context.stage,
                    f"remove this run's {role} container",
                    f"docker rm --force {shlex.quote(process['container_id'])} > /dev/null",
                )
                self.announce(f"{role}: removed this run's engine container for the inspection")

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
        self._spent_launch(context, host, launch)
        self._stop_previous_engine(context, host)
        work = self.remote(context)
        command = f"narwhal-engine start --run {launch} --ready-seconds {ready}"
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

    def _spent_launch(self, context: StageContext, host: Host, launch: str) -> None:
        """Refuse a launch directory that already started an engine, before stopping any.

        A launch directory starts one engine once, so `engine-prepare` writes a fresh one
        first. A container that an unrecorded start left is removed; the recorded engine runs
        on until the fresh directory's start replaces it.
        """
        cid = self.ssh(context).run(
            host,
            context.stage,
            "read the launch directory's container",
            f"cd {shlex.quote(self.package_of(context)[1]['remote_dir'])} && "
            f"cat {launch}/container.id 2> /dev/null || true",
        )
        if not cid:
            return
        previous = (context.previous or {}).get("products", {}).get("engine_identity") or {}
        if previous.get("container_id") != cid:
            self.ssh(context).run(
                host,
                context.stage,
                "remove the container an unrecorded start left",
                f"docker rm --force {shlex.quote(cid)} > /dev/null 2>&1 || true",
            )
        raise Spent("launch", context.instance, f"it already started container {cid}")

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
        # The routers export each edge's measured bandwidth against its budget from these.
        _private(context.directory / deployment_status.EDGE_RESULTS, results)
        return Outcome({"link": {result["sample"]: result["link_sha256"] for result in results}})

    # Attestation.

    def fleet_inventory(self, context: StageContext) -> None:
        """Check that the router's fleet names every engine and resolves each of its URLs."""
        env, manifest, package = self.package_of(context)
        host = self.host(context.instance)
        fleet_path = package / host.id / "fleet.local.json"
        fleet = _json(fleet_path)
        router = read_environment(package / host.id / f".env.{self.router_role(host.id)}")
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
        before_start: Callable[[], None] | None = None,
    ) -> supervision.SystemdUnit:
        """Start `name` as a unit in place of the earlier attempt's, and wait for its routes.

        `before_start` runs once the earlier unit has stopped.
        """
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
        if before_start is not None:
            before_start()
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
        host_id: str | None = None,
    ) -> None:
        env, manifest, _ = self.package_of(context)
        host = self.host(host_id or context.instance)
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
            self.router_role(host.id),
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
        # Profiling and calibration run once, from the primary router's host.
        fleet = _json(context.outputs("finalize-fleet", self.primary()) / "host" / "fleet.json")
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
            context,
            "profile the idle engines",
            shlex.join(args),
            {"fleet.json": draft},
            self.primary(),
        )

    def calibrate(self, context: StageContext) -> None:
        values = settings(self.env)
        source = context.outputs("profile") / "host" / "fleet.json"
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
            self.primary(),
        )

    def fleet_limits(self, context: StageContext) -> None:
        """Write the SLO targets, deadline and calibration into every router's fleet file."""
        values = settings(self.env)
        env, manifest, _ = self.package_of(context)
        routers = Topology(self).instances(ROUTER)
        profiled = context.outputs("profile") / "host"
        calibrated = context.outputs("calibrate") / "host"
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

    def lease_check(self, context: StageContext) -> None:
        """Check the lease path for the router pair before either router starts.

        Through the router's own lease code, on a probe lease beside the pair's lease, the
        primary router's host claims, the standby's host must read that claim and be refused,
        must find the lock held while the primary's host holds it, and then claims the
        expired lease; the primary's host must read the new claim.
        """
        lease = pair(self.env).lease_path
        directory = lease.rsplit("/", 1)[0] or "/"
        env, manifest, _ = self.package_of(context)
        ssh = self.ssh(context, env)
        primary = self.host(self.primary())
        standby = self.owner("standby-router")
        probe = f"{directory}/.narwhal-lease-check-{context.run_id}-{context.attempt}.json"
        quoted = shlex.quote(probe)
        cli = ".venv/bin/python -m narwhal.deployment.lease_check"
        work = self.remote(context)
        evidence: dict[str, Any] = {"lease_path": lease, "probe": probe, "steps": []}

        def run(host: Host, operation: str, command: str) -> str:
            output = ssh.run(
                host,
                context.stage,
                operation,
                f"cd {shlex.quote(manifest['remote_dir'])} && {command}",
            )
            evidence["steps"].append({"host": host.id, "operation": operation, "output": output})
            return output

        def expect(host: Host, operation: str, command: str, wanted: str, problem: str) -> None:
            output = run(host, operation, command)
            if output != wanted:
                _private(context.directory / "lease-check.json", evidence)
                raise CheckFailed(
                    f"{lease}: {problem}: {host.id} printed {output!r}, not {wanted!r}; give "
                    "both router hosts one shared mount with POSIX flock, as in "
                    + recovery_url("operate/01-Start-Routers.md#starting-a-router-pair")
                )

        missing = shlex.quote(f"{HOST_CAUSE}{directory} is not a directory on {primary.id}")
        run(
            primary,
            "check the lease directory",
            f"test -d {shlex.quote(directory)} || {{ printf '%s\\n' {missing} >&2; exit 4; }}",
        )
        try:
            first, second = f"{primary.id}-lease-check", f"{standby.id}-lease-check"
            expect(
                primary,
                "claim the probe lease",
                f"{cli} claim {quoted} {first} {LEASE_CHECK_TTL_S}",
                "claimed 1",
                "the primary's host could not claim a new lease",
            )
            claimed = json.dumps({"holder": first, "epoch": 1})
            expect(
                standby,
                "read the claim",
                f"{cli} read {quoted}",
                claimed,
                "the standby's host does not read the primary's claim after its atomic rename",
            )
            expect(
                standby,
                "claim the held lease",
                f"{cli} claim {quoted} {second} {LEASE_CHECK_TTL_S}",
                "refused",
                "the standby's host claimed a lease the primary's host holds",
            )
            marker = shlex.quote(f"{work}/lock-held")
            run(
                primary,
                "hold the lease lock",
                # Only the hold runs in the background; the wait runs in the remote directory.
                f"mkdir -p {shlex.quote(work)} && {{ nohup {cli} hold {quoted} 4 {marker} "
                f"> /dev/null 2>&1 < /dev/null & }} && for _ in $(seq 100); do "
                f"test -e {marker} && break; sleep 0.1; done && test -e {marker}",
            )
            expect(
                standby,
                "probe the held lock",
                f"{cli} probe {quoted}",
                "blocked",
                "flock on the standby's host does not see the lock the primary's host holds",
            )
            expect(
                standby,
                "claim the expired lease",
                f"{cli} claim {quoted} {second} {LEASE_CHECK_TTL_S} "
                f"--wait {LEASE_CHECK_TTL_S + 15}",
                "claimed 2",
                "the standby's host could not claim the expired lease",
            )
            expect(
                primary,
                "read the takeover",
                f"{cli} read {quoted}",
                json.dumps({"holder": second, "epoch": 2}),
                "the primary's host does not read the standby's claim",
            )
            witness = shlex.quote(f"{directory}/{WITNESS_NAME}")
            run(primary, "leave the lease witness", f"{cli} witness {witness}")
            expect(
                standby,
                "read the lease witness",
                f"{cli} witness {witness} --check",
                "present",
                "the standby's host does not see the witness the primary's host left",
            )
            mounts = {
                host.id: run(
                    host,
                    "read the lease mount's file system type",
                    f"stat -f -c %T {shlex.quote(directory)}",
                )
                for host in (primary, standby)
            }
            evidence["mounts"] = mounts
            self._check_skew(context, ssh, manifest["remote_dir"], (primary, standby), evidence)
        finally:
            with contextlib.suppress(RemoteError, RemoteTimeout, OSError):
                run(primary, "remove the probe lease", f"rm -f {quoted} {quoted}.lock")
        _private(context.directory / "lease-check.json", evidence)

    def _check_skew(
        self,
        context: StageContext,
        ssh: SSH,
        remote_dir: str,
        hosts: Sequence[Host],
        evidence: dict[str, Any],
    ) -> None:
        """Fail when the router hosts' clocks may differ by more than the lease reserves.

        A lease's expiry is a wall-clock time that the other host compares with its own clock,
        so skew beyond the routers' --lease-safety-margin lets the standby claim a live lease.
        """
        cli = ".venv/bin/python -m narwhal.deployment.lease_check"
        script = f"cd {shlex.quote(remote_dir)} && {cli} clock"
        readings = [
            ssh.clock_offset(host, context.stage, "read the host clock", script, CLOCK_SAMPLES)
            for host in hosts
        ]
        (first, first_bound), (second, second_bound) = readings
        skew, bound = abs(second - first), first_bound + second_bound
        evidence["clock"] = {"skew_s": skew, "bound_s": bound, "margin_s": DEFAULT_SAFETY_MARGIN_S}
        if skew + bound > DEFAULT_SAFETY_MARGIN_S:
            _private(context.directory / "lease-check.json", evidence)
            raise CheckFailed(
                f"the router hosts' clocks differ by {skew:.3f}s, measured to within "
                f"{bound:.3f}s, beyond the {DEFAULT_SAFETY_MARGIN_S:g}s the lease reserves for "
                "skew; synchronise both hosts' clocks, such as with chrony or systemd-timesyncd"
            )
        self.announce(
            f"lease-check: router host clocks differ by {skew:.3f}s, measured to within "
            f"{bound:.3f}s, within {DEFAULT_SAFETY_MARGIN_S:g}s"
        )

    def router_start(self, context: StageContext) -> Outcome:
        """Start this host's router of the pair, the primary first, then its standby.

        The stage first checks that the lease directory on this host is the shared mount that
        `lease-check` saw. The primary starts with `--first-start-primary`, so it claims the
        lease when no lease file exists or when the lease expired too long ago for a standby to
        take it over, and otherwise starts as the standby of its peer. Before the primary
        starts, the stage waits out a lease that expired so recently that a standby may still
        take it over. The stage never removes a lease. Each start ends with
        the router overhead measured on the router that controls the fleet.
        """
        values = settings(self.env)
        host = self.host(context.instance)
        primary = self.router_role(host.id) == "router"
        url = self.router_url(host.id)
        env, manifest, package = self.package_of(context)
        ssh = self.ssh(context, env)
        units = self.supervisor(ssh, host, context.stage, context.run_id)
        if not paired(self.env):
            return self._single_router(context, host, units, values, url)
        routers = pair(self.env)
        peer = self.router_url(self.owner("standby-router" if primary else "router").id)
        witness = self._check_mount(context, ssh, host, manifest["remote_dir"], routers)
        fleet = context.outputs("fleet-limits") / "fleet.json"
        engines = {role: context.product("engine_identity", role) for role in self.engines()}
        state = {"fleet_sha256": _digest(fleet), "engines": engines}
        previous = context.previous
        if previous is not None and previous["status"] == PASSED:
            kept = self._readmit(context, host, units, previous, state, url)
            if kept is not None:
                self._measure(context, host, env, manifest["remote_dir"], (url, peer), values)
                return kept
        lease = self._lease_state(context, ssh, host, manifest["remote_dir"], routers.lease_path)
        for _ in range(3):
            if not primary or lease["live"] or lease.get("holder") is None:
                break
            # A standby may still take over a lease that expired this recently, so the primary
            # would start as its standby; once no standby can, the primary claims the lease.
            expired = lease["now"] - lease["expires_at"]
            wait = MAX_HANDOFF_AGE_S + DEFAULT_SAFETY_MARGIN_S - expired
            if wait <= 0:
                break
            self.announce(
                f"router-start: the lease expired {expired:.0f}s ago and a standby may still "
                f"take it over; {host.id}'s router starts in {wait:.0f}s"
            )
            _pause(wait)
            lease = self._lease_state(
                context, ssh, host, manifest["remote_dir"], routers.lease_path
            )
        address = urlsplit(url)
        argv = [
            "{root}/.venv/bin/narwhal-serve",
            "--fleet",
            "{root}/" + self._limits(context),
            "--host",
            str(address.hostname),
            "--port",
            str(address.port),
            "--journal",
            "{root}/" + self.remote(context) + "/journal.jsonl",
            "--lease-path",
            routers.lease_path,
            "--lease-witness",
            witness,
            "--router-id",
            host.id,
            "--standby-of",
            peer,
            "--deployment-status",
            "{root}/" + deployment_status.HOST_PATH,
        ]
        if primary:
            argv.append("--first-start-primary")
        unit = self._supervise(
            context,
            host,
            "router",
            argv,
            self.role_env(package, self.router_role(host.id)),
            [(url + "/health", HEALTH_SECONDS)],
        )
        _private(context.directory / "router.json", {**state, "url": url, "lease": lease})
        self._measure(context, host, env, manifest["remote_dir"], (url, peer), values)
        identity = {"host": host.id, **unit.record()}
        return Outcome({"router_identity": identity}, ({"name": "router", **identity},))

    def _single_router(
        self,
        context: StageContext,
        host: Host,
        units: supervision.SystemdUnits,
        values: Settings,
        url: str,
    ) -> Outcome:
        """Start a fleet's only router, with no lease and no standby, and measure it."""
        env, manifest, package = self.package_of(context)
        fleet = context.outputs("fleet-limits") / "fleet.json"
        engines = {role: context.product("engine_identity", role) for role in self.engines()}
        state = {"fleet_sha256": _digest(fleet), "engines": engines}
        previous = context.previous
        if previous is not None and previous["status"] == PASSED:
            kept = self._readmit(context, host, units, previous, state, url)
            if kept is not None:
                self._measure(context, host, env, manifest["remote_dir"], (url,), values)
                return kept
        address = urlsplit(url)
        argv = [
            "{root}/.venv/bin/narwhal-serve",
            "--fleet",
            "{root}/" + self._limits(context),
            "--host",
            str(address.hostname),
            "--port",
            str(address.port),
            "--journal",
            "{root}/" + self.remote(context) + "/journal.jsonl",
            "--router-id",
            host.id,
            "--deployment-status",
            "{root}/" + deployment_status.HOST_PATH,
        ]
        unit = self._supervise(
            context,
            host,
            "router",
            argv,
            self.role_env(package, "router"),
            [(url + "/health", HEALTH_SECONDS)],
        )
        _private(context.directory / "router.json", {**state, "url": url, "lease": None})
        self._measure(context, host, env, manifest["remote_dir"], (url,), values)
        identity = {"host": host.id, **unit.record()}
        return Outcome({"router_identity": identity}, ({"name": "router", **identity},))

    def _check_mount(
        self, context: StageContext, ssh: SSH, host: Host, remote_dir: str, routers: Pair
    ) -> str:
        """Check that this host's lease directory is the shared mount `lease-check` saw, and
        return the witness path the router needs.

        After a reboot that left the mount out, the directory is missing, or a local directory
        without the witness, of another file system type.
        """
        directory = routers.lease_path.rsplit("/", 1)[0]
        witness = f"{directory}/{WITNESS_NAME}"
        checked = _json(context.outputs("lease-check") / "lease-check.json")
        cli = ".venv/bin/python -m narwhal.deployment.lease_check"
        quoted = shlex.quote(directory)
        found = ssh.run(
            host,
            context.stage,
            "check the lease mount",
            f"cd {shlex.quote(remote_dir)} && {cli} witness {shlex.quote(witness)} --check && "
            f"{{ stat -f -c %T {quoted} 2> /dev/null || echo missing; }}",
        ).splitlines()
        expected = checked["mounts"][host.id]
        if found != ["present", expected]:
            raise CheckFailed(
                f"{routers.lease_path}: {host.id}'s lease directory is not the shared mount that "
                f"lease-check saw (witness and file system type {found}, not "
                f"{['present', expected]}); mount the shared storage at {directory} and run "
                "again, as in "
                + recovery_url("deploy/07-Serve-and-Measure.md#checking-the-shared-lease-path")
            )
        return witness

    def _lease_state(
        self, context: StageContext, ssh: SSH, host: Host, remote_dir: str, path: str
    ) -> dict[str, Any]:
        """The lease's holder and expiry as this host reads them, recorded before a start."""
        cli = ".venv/bin/python -m narwhal.deployment.lease_check"
        state = json.loads(
            ssh.run(
                host,
                context.stage,
                "read the pair's lease",
                f"cd {shlex.quote(remote_dir)} && {cli} state {shlex.quote(path)}",
            )
        )
        live = state.get("holder") is not None and state["expires_at"] > state["now"]
        holder = state.get("holder") or "no router"
        self.announce(
            f"router-start: the lease is {'held by ' + holder if live else 'free'}; "
            f"{host.id}'s router starts and {'shadows it' if live else 'may claim it'}"
        )
        return {**state, "live": live}

    def _measure(
        self,
        context: StageContext,
        host: Host,
        env: Mapping[str, str],
        remote_dir: str,
        urls: Sequence[str],
        values: Settings,
    ) -> None:
        """Wait for a router of the pair to control the fleet, then check it and measure its
        overhead."""
        ssh = self.ssh(context, env)
        deadline = time.monotonic() + READY_SECONDS
        controlling = None
        while controlling is None:
            for candidate in urls:
                answered = ssh.run(
                    host,
                    context.stage,
                    "ask which router controls the fleet",
                    _host_http(remote_dir, "status", candidate + "/narwhal/targets"),
                )
                if answered == "200":
                    controlling = candidate
                    break
            else:
                if time.monotonic() >= deadline:
                    raise CheckFailed(
                        f"no router of the pair controlled the fleet within {READY_SECONDS}s; "
                        "read each router's /ready reason"
                    )
                time.sleep(READY_POLL_SECONDS)
        self._router_responses(context, host, env, remote_dir, controlling, True)
        self._overhead(context, host, env, remote_dir, controlling, values)

    def _overhead(
        self,
        context: StageContext,
        host: Host,
        env: Mapping[str, str],
        remote_dir: str,
        url: str,
        values: Settings,
    ) -> None:
        """Measure the router overhead under load and fail above NARWHAL_ROUTER_OVERHEAD_S."""
        fleet = _json(context.outputs("fleet-limits") / "fleet.json")
        body = json.dumps({"model": fleet["model"], "prompt": _COMPLETION_PROMPT, "max_tokens": 32})
        script = _host_http(
            remote_dir,
            "overhead",
            url,
            "--completion",
            body,
            "--requests",
            str(OVERHEAD_REQUESTS),
            "--concurrency",
            str(OVERHEAD_CONCURRENCY),
            "--limit",
            f"{values.router_overhead_s:g}",
        )
        # A failure names the measured overhead; the log holds the measurement.
        ssh = SSH(env, context.directory / "logs")
        output = ssh.run(host, context.stage, "measure the router overhead", script)
        remote.write(context.directory, remote.files(output))
        measured = _json(context.directory / "overhead.json")
        if measured.get("problem"):
            raise CheckFailed(f"{measured['problem']}; the terms are in overhead.json")
        self.announce(
            f"router-start: router overhead {measured['overhead_seconds']:.4f}s over "
            f"{measured['requests']:g} requests, within {values.router_overhead_s:g}s"
        )

    def _readmit(
        self,
        context: StageContext,
        host: Host,
        units: supervision.SystemdUnits,
        previous: Mapping[str, Any],
        state: Mapping[str, Any],
        url: str,
    ) -> Outcome | None:
        """Readmit restarted engines to a router that still runs on an unchanged fleet.

        Returns None when the router must start again: it stopped, or the fleet changed. Only
        the router that controls the fleet readmits; its standby follows through handoffs.
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
        ssh = self.ssh(context, env)
        controls = ssh.run(
            host,
            context.stage,
            "ask whether the router controls the fleet",
            _host_http(manifest["remote_dir"], "status", url + "/narwhal/targets"),
        )
        if controls == "200":
            iids = [f"n{_node(role)}" for role in restarted]
            # The process start the router accepted for each engine before it restarted, from
            # the earlier attempt's lifecycle; the router serves the restart once it differs.
            lifecycle = self.directory / previous["directory"] / "router" / "lifecycle.json"
            starts = _json(lifecycle).get("process_starts", {}) if lifecycle.is_file() else {}
            engines = [f"{iid}={starts[iid]!r}" if iid in starts else iid for iid in iids]
            self.announce(f"router-start: readmitting {', '.join(iids) or 'no engines'}")
            output = ssh.run(
                host,
                context.stage,
                "readmit restarted engines",
                _host_http(
                    manifest["remote_dir"],
                    "readmit",
                    url,
                    str(READMIT_SECONDS),
                    *engines,
                ),
            )
            remote.write(context.directory, remote.files(output))
        else:
            self.announce(f"router-start: {host.id}'s router is the standby and readmits none")
        _private(context.directory / "router.json", {**before, **state})
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
        url: str,
        completion: bool,
    ) -> None:
        """Retain the router's health, readiness, state and metrics, and one completion."""
        fleet = _json(context.outputs("fleet-limits") / "fleet.json")
        body = json.dumps({"model": fleet["model"], "prompt": _COMPLETION_PROMPT, "max_tokens": 32})
        args = ["responses", url] + (["--completion", body] if completion else [])
        script = _host_http(remote_dir, *args)
        output = SSH(env, context.directory / "logs").run(
            host, context.stage, "check the router", script
        )
        remote.write(context.directory / "router", remote.files(output))

    def load_balancer(self, context: StageContext) -> Outcome:
        """Start HAProxy from `deploy/ha/haproxy.cfg` in front of the pair, and wait for it."""
        routers = pair(self.env)
        host = self.owner("load-balancer")
        env, manifest, _ = self.package_of(context)
        ssh = self.ssh(context, env)
        config = balancer.render(
            balancer.shipped(),
            routers.balancer_bind,
            (
                urlsplit(self.router_url(self.primary())).netloc,
                urlsplit(routers.standby_url).netloc,
            ),
        )
        write_private(context.directory / "haproxy.cfg", config.encode())
        work = self.remote(context)
        root = manifest["remote_dir"]
        ssh.run(
            host,
            context.stage,
            "create the attempt directory",
            f"cd {shlex.quote(root)} && mkdir -p {shlex.quote(work)}",
        )
        source = context.directory / "haproxy.cfg"
        remote.upload(ssh, host, context.stage, source, f"{root}/{work}/haproxy.cfg")
        name = f"narwhal-{context.run_id}-load-balancer"
        image = shlex.quote(routers.haproxy_image)
        mount = f'"$PWD"/{shlex.quote(work)}/haproxy.cfg:{balancer.CONTAINER_CONFIG}:ro'
        options = f'--user "$(id -u):$(id -g)" --network host -v {mount}'
        # The container's name carries the run ID, so a container of that name is this run's
        # earlier load balancer, which the new one replaces.
        output = self.step(
            context,
            host,
            "load-balancer",
            "start HAProxy",
            f"docker run --rm {options} {image} haproxy -c -f {balancer.CONTAINER_CONFIG} && "
            f"{{ docker rm --force {name} > /dev/null 2>&1 || true; }} && "
            f"docker run --detach --name {name} --restart on-failure {options} {image} "
            "> /dev/null && "
            f"docker inspect --format '{{{{.Id}}}} {{{{.State.StartedAt}}}}' {name}",
            workdir=False,
            on_failure=f"docker logs --tail 200 {name} > {shlex.quote(work)}/haproxy.log 2>&1 || "
            "true",
        )
        container, started = output.splitlines()[-1].split()
        identity = {"host": host.id, "container_id": container, "started_at": started}
        try:
            ready = routers.balancer_url + "/ready"
            self._wait(ssh, host, context.stage, ready, READY_SECONDS, root)
        except RemoteError:
            ssh.run(host, context.stage, "remove HAProxy", f"docker rm --force {name} > /dev/null")
            raise
        self._router_responses(context, host, env, root, routers.balancer_url, True)
        return Outcome(
            {"load_balancer_identity": identity}, ({"name": "load-balancer", **identity},)
        )

    def monitoring(self, context: StageContext) -> Outcome:
        """Start Prometheus and Grafana on the primary router's host, from the installed wheel."""
        values = settings(self.env)
        host = self.host(self.primary())
        args = [
            ".venv/bin/python",
            "-m",
            "narwhal.observability.stack.start.cli",
            "--router-url",
            values.router_url,
        ]
        if paired(self.env):
            routers = pair(self.env)
            args += ["--router-url", routers.standby_url, "--targets-url", routers.balancer_url]
        else:
            # The only router is the one that controls the fleet.
            args += ["--targets-url", values.router_url]
        _, _, package = self.package_of(context)
        for role in self.engines():
            sidecar = self.role_env(package, role).get(
                f"NARWHAL_NODE_{_node(role)}_ATTESTATION_URL"
            )
            if sidecar:
                address = urlsplit(sidecar)
                args += ["--sidecar-url", f"{address.scheme}://{address.netloc}"]
        listeners = " ".join(
            f"{name}={shlex.quote(self.env[name])}" for name in MONITORING if self.env.get(name)
        )
        env, _, _ = self.package_of(context)
        output = self.step(
            context,
            host,
            "router",
            "start the monitoring stack",
            (f"env {listeners} " if listeners else "") + shlex.join(args),
            workdir=False,
        )
        for line in output.splitlines():
            self.announce(f"monitoring: {line}")
        containers = self.ssh(context, env).run(
            host,
            context.stage,
            "read the monitoring containers",
            "docker ps --filter label=com.docker.compose.project=observability "
            "--format '{{.ID}} {{.Label \"com.docker.compose.service\"}}'",
        )
        services = {}
        for line in containers.splitlines():
            if line.strip():
                container, service = line.split()
                services[service] = container
        identity = {"host": host.id, "containers": dict(sorted(services.items()))}
        return Outcome({"monitoring_identity": identity}, ())

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
        if self.dry:
            # The check writes evidence on the host, so a dry pass only reads the unit.
            observation = units.check(identity)  # type: ignore[arg-type]
            if observation.state == "running":
                return dict(recorded)
            if observation.state == "restarted" and observation.identity is not None:
                return Refreshed({"host": host.id, **observation.identity.record()}, checked=False)
            return None
        try:
            current = units.confirm(identity, lambda _: verify(ssh, host))  # type: ignore[arg-type]
        except (supervision.SupervisionError, RemoteError, RemoteTimeout, OSError) as error:
            # A restart that fails its check is not this stage's process any more.
            self.announce(f"{stage}: the restarted {recorded.get('unit', 'unit')} failed: {error}")
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
        url = self.router_url(instance)
        _, manifest, _ = self.recorded_package()
        return self._confirmed(
            recorded,
            "router-start",
            lambda ssh, host: self._wait(
                ssh,
                host,
                "router-start",
                url + "/health",
                HEALTH_SECONDS,
                manifest["remote_dir"],
            ),
        )

    def balancer_running(self, instance: str, recorded: Any) -> Any:
        """The HAProxy container, while it runs from the recorded start."""
        return self._container_state(
            self.live_ssh(), self.host(recorded["host"]), "load-balancer", recorded
        )

    def monitoring_running(self, instance: str, recorded: Any) -> Any:
        """The monitoring stack's containers, while each recorded one still runs."""
        containers = " ".join(shlex.quote(cid) for cid in recorded["containers"].values())
        output = self.live_ssh().run(
            self.host(recorded["host"]),
            "monitoring",
            "read the monitoring containers",
            f"docker inspect --format '{{{{.State.Running}}}}' {containers} 2> /dev/null || true",
        )
        running = output.split()
        if len(running) != len(recorded["containers"]) or set(running) != {"true"}:
            return None
        return recorded

    def _recorded_entry(self, stage: str, instance: str) -> dict[str, Any]:
        from . import run_record

        entry: dict[str, Any] = run_record.read(self.directory)["stages"][key(stage, instance)]
        return entry

    # The deployment status the routers export.

    def publish_status(self) -> list[str]:
        """Write the run's status beside its record and upload it to every router host.

        Best effort: returns a warning for each host that the upload missed, which keeps the
        status of the run before. A run that has not installed the package uploads nothing.
        """
        from . import run_record

        try:
            record = run_record.read(self.directory)
        except (OSError, ValueError):
            return []
        local = self.directory / deployment_status.LOCAL_NAME
        try:
            previous = _json(local) if local.is_file() else None
            status = deployment_status.build(
                record,
                self.directory,
                DEPLOY_GRAPH.order(),
                LAST_STAGE,
                self.env.get("NARWHAL_FLEET_NAME", ""),
                "ssh",
                previous,
            )
        except (OSError, ValueError, LookupError, TypeError) as error:
            return [f"the deployment status could not be built: {error}"]
        staged = local.with_name(local.name + ".tmp")
        staged.unlink(missing_ok=True)
        _private(staged, status)
        staged.replace(local)
        try:
            _, manifest, _ = self.recorded_package()
            install_passed = all(
                record["stages"].get(key("install", host.id), {}).get("status") == PASSED
                for host in self.hosts()
            )
        except (OSError, ValueError, KeyError):
            return []
        if not install_passed:
            return []
        warnings = []
        ssh = self.live_ssh()
        for router in Topology(self).instances(ROUTER):
            host = self.host(router)
            try:
                remote.replace(
                    ssh,
                    host,
                    "deployment-status",
                    local,
                    f"{manifest['remote_dir']}/{deployment_status.HOST_PATH}",
                )
            except (RemoteError, RemoteTimeout, OSError, ValueError) as error:
                warnings.append(f"{host.id}: the deployment status was not uploaded: {error}")
        return warnings

    # `down`: stop what the run started.

    def down(self) -> list[dict[str, Any]]:
        """Stop the monitoring stack, the load balancer, the routers, the sidecars and the engines
        this run started, newest first.

        Each is stopped only while its recorded identity still matches.
        """
        from . import run_record

        stopped = []
        check_directory(self.directory)
        with run_record.locked(self.directory):
            record = run_record.read(self.directory)
            if record["target"] != "ssh":
                raise ValueError(f"{self.directory} holds a {record['target']} run")
            entries = record["stages"]
            ssh = self.live_ssh()
            for entry in entries.values():
                monitored = entry["products"].get("monitoring_identity")
                if entry["stage"] != "monitoring" or not monitored:
                    continue
                host = self.host(monitored["host"])
                ran = self.monitoring_running("", monitored) is not None
                if ran:
                    # The stack's volumes, and with them its metrics, stay for the next start.
                    containers = " ".join(map(shlex.quote, monitored["containers"].values()))
                    ssh.run(
                        host,
                        "down",
                        "remove the monitoring containers",
                        f"docker rm --force {containers} > /dev/null",
                    )
                stopped.append({"name": "monitoring", "host": host.id, "stopped": ran})
            for entry in entries.values():
                if entry["stage"] != "load-balancer":
                    continue
                for process in entry["processes"]:
                    host = self.host(process["host"])
                    ran = self._container_state(ssh, host, "down", process) is not None
                    if ran:
                        ssh.run(
                            host,
                            "down",
                            "remove the HAProxy container",
                            f"docker rm --force {shlex.quote(process['container_id'])} > /dev/null",
                        )
                    stopped.append({"name": process["name"], "host": host.id, "stopped": ran})
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
            return [host.id for role in ROUTER_ROLES for host in hosts if role in host.roles]
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
        if capability == ROUTER_PAIR:
            return paired(self.deployment.env)
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
