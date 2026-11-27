"""`narwhal deploy --target kubernetes`: the stage graph's actions, live checks and topology on
a Kubernetes cluster.

The workstation drives `helm` and `kubectl` as subprocesses. Every stage that checks something
in the cluster runs it as a gate Job of the Helm chart: the runner sets the chart's `gates`
list to that stage's Jobs and upgrades the release, so the engines and routers it already
runs stay as they are. A Job's inputs travel in a ConfigMap the runner makes for it; its
outputs come back in its log, as `kubernetes_gate` prints them, and land in the stage's
attempt directory. Jobs that check the engines read and write the state volume, where the
engine pods keep their records.

The release holds no router until `router-start`, so the engines and Jobs run first. An engine
pod's launch directory lives at `engines/<role>/launches/<pod UID>` on the state volume, where
its attestation sidecar waits for it.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import math
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from narwhal import provenance
from narwhal.engines.attestation import AttestationDocument, launch_digest

from . import cache_groups, discovery, fabric, install, kubernetes_gate, remote, run_record
from .deploy import CALIBRATION, MARGIN, PROFILE, SLO, InputError, settings
from .deploy import recovery_url as recovery_url
from .deploy_graph import CACHE_CAPTURE, EDGE, ENGINE, GROUP, TRANSFER_MEASUREMENT
from .engine_launch import load_launches
from .hosts import SSH, Host, write_private
from .kubernetes_graph import KUBERNETES_GRAPH
from .launch_engine import checkpoint as checkpoint_manifest
from .launch_engine import engine_set
from .launch_engine.accelerators import gpu_inventory
from .launch_engine.backend import (
    CacheRegistration,
    ModelFormats,
    cache_capture,
    engine_backend,
    launcher,
    plan_launcher,
    transfer_measurement,
)
from .launch_engine.kubernetes import LAUNCHES
from .launch_engine.plan import conv_state_layers
from .role_env import HELPER_DIRECTORY, helpers, read_environment, select_values, write_environment
from .run_record import PASSED, key
from .runner import Input, Outcome, Runner, StageContext, Unobserved, check_directory

TARGET = "kubernetes"
LAST_STAGE = "router-start"

NAMESPACE = "NARWHAL_KUBERNETES_NAMESPACE"
RELEASE = "NARWHAL_KUBERNETES_RELEASE"
CONTEXT = "NARWHAL_KUBERNETES_CONTEXT"
KUBECTL = "NARWHAL_KUBECTL"
HELM = "NARWHAL_HELM"
CHART = "NARWHAL_KUBERNETES_CHART"
VALUES = "NARWHAL_KUBERNETES_VALUES"
STATE_CLAIM = "NARWHAL_KUBERNETES_STATE_CLAIM"
GPU_RESOURCE = "NARWHAL_KUBERNETES_GPU_RESOURCE"
RDMA_RESOURCE = "NARWHAL_KUBERNETES_RDMA_RESOURCE"
MODEL_VOLUME = "NARWHAL_KUBERNETES_MODEL_VOLUME"
RUNTIME_CLASS = "NARWHAL_KUBERNETES_RUNTIME_CLASS"
CLUSTER_INPUTS = (CONTEXT, NAMESPACE, RELEASE, KUBECTL, HELM)
# The engine node of engine role n.
NODE = re.compile(r"NARWHAL_NODE_([1-9][0-9]*)_KUBERNETES_NODE")
DNS_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")

# Where the cluster's containers see things.
STATE = "/narwhal-state"
INPUT = "/narwhal-input"
MODEL = "/model"
ROUTER_HOME = "/narwhal-router"
ROUTER_URL = "http://haproxy:8000"
ROUTERS = ("router-0", "router-1")
# Where a host-script Job unpacks its inputs, as an SSH host's deployment directory.
REMOTE_DIR = "/tmp/narwhal-in"
ENGINE_CONTAINER = "engine"
SIDECAR_CONTAINER = "attestation"
GATE_CONTAINER = "gate"
# The scheduling gate the chart holds each engine pod at until engine-start pins it to its node.
PLACEMENT_GATE = "narwhal.io/placement"

# Seconds each Job may run, and how long the runner waits on the cluster around it.
JOB_SECONDS = {
    "discovery": 1800,
    "prepare": 1800,
    "engine-check": 600,
    "state": 600,
    "fabric-probe": 120,
    "transfer-measure": 900,
    "fabric-server": 180,
    "fabric-client": 180,
    "sidecar-verify": 300,
    "finalize-fleet": 600,
    "profile": 7200,
    "calibrate": 3600,
    "preflight": 1800,
    "router-check": 600,
}
GRACE_SECONDS = 120
POLL_SECONDS = 2.0
START_SECONDS = 1800
ROUTER_SECONDS = 600
PUBLISH_SECONDS = 900
HELM_SECONDS = 600

_COMPLETION_PROMPT = "Return one sentence about narwhals."
_FABRIC_TEST = re.compile(r"NARWHAL_(FABRIC_TEST_PORT|(NODE_[1-9][0-9]*_)?RDMA_.*)")
_NOT_INPUTS = re.compile(
    r"NARWHAL_(HOSTS|FLEET|LAUNCH_CONFIG|STAGE_.*_TIMEOUT_SECONDS|STAGE_TIMEOUT_SECONDS"
    r"|STAGE_CLEANUP_GRACE_SECONDS|STAGE_KILL_GRACE_SECONDS|TRIAL_.*)"
)
_ROUTER_INPUTS = ("NARWHAL_ROUTER_URL",)


class ClusterError(RuntimeError):
    """A `kubectl` or `helm` command failed."""

    def __init__(self, argv: Sequence[str], status: int, cause: str, log: Path) -> None:
        self.context = {"command": argv[0], "exit_status": status, "log": str(log)}
        detail = cause.strip().splitlines()[-1] if cause.strip() else "no output"
        super().__init__(f"{shlex.join(argv[:3])} exited {status}: {detail}; inspect {log}")


class GateFailed(remote.StepFailed):
    """A gate Job failed. Exit status 1 is its check's rejection, as on an SSH host."""


class GateOutputError(ValueError):
    """A gate Job's log holds no complete set of returned files."""


# The cluster: `kubectl` and `helm` as subprocesses.


class Cluster:
    """`kubectl` and `helm` against one namespace and release, each call logged."""

    def __init__(self, env: Mapping[str, str], logs: Path) -> None:
        self.namespace = env.get(NAMESPACE, "")
        self.release = env.get(RELEASE) or "narwhal"
        self.context = env.get(CONTEXT, "")
        self.kubectl_path = env.get(KUBECTL) or "kubectl"
        self.helm_path = env.get(HELM) or "helm"
        self.log_dir = logs
        self._count = 0

    def _run(self, argv: list[str], data: str | None, timeout: float) -> str:
        self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._count += 1
        log = self.log_dir / f"{self._count:03d}-{Path(argv[0]).name}-{argv[1]}.log"
        try:
            result = subprocess.run(
                argv, input=data, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            write_private(log, f"$ {shlex.join(argv)}\ntimed out after {timeout:g}s\n".encode())
            raise ClusterError(argv, 124, f"timed out after {timeout:g} seconds", log) from None
        # Logs keep the command and its standard error; standard output is the caller's.
        write_private(
            log, f"$ {shlex.join(argv)}\nexit {result.returncode}\n{result.stderr}".encode()
        )
        if result.returncode:
            raise ClusterError(argv, result.returncode, result.stderr or result.stdout, log)
        return result.stdout

    def kubectl(self, *args: str, data: str | None = None, timeout: float = 120) -> str:
        context = ["--context", self.context] if self.context else []
        argv = [self.kubectl_path, *args[:1], *context, "--namespace", self.namespace, *args[1:]]
        return self._run(argv, data, timeout)

    def helm(self, *args: str, data: str | None = None, timeout: float = HELM_SECONDS) -> str:
        context = ["--kube-context", self.context] if self.context else []
        argv = [self.helm_path, *args[:1], *context, "--namespace", self.namespace, *args[1:]]
        return self._run(argv, data, timeout)

    def get(self, kind: str, name: str) -> dict | None:
        """The object, or None when the namespace has none of that name."""
        output = self.kubectl("get", kind, name, "--ignore-not-found", "--output", "json")
        return json.loads(output) if output.strip() else None

    def items(self, kind: str, selector: str) -> list[dict]:
        output = self.kubectl("get", kind, "--selector", selector, "--output", "json")
        return list(json.loads(output)["items"])

    def apply(self, document: Mapping[str, Any]) -> None:
        self.kubectl("apply", "--filename", "-", data=json.dumps(document))

    def delete(self, kind: str, name: str) -> None:
        self.kubectl("delete", kind, name, "--ignore-not-found", "--wait=false")

    def patch(self, kind: str, name: str, operations: Sequence[Mapping[str, Any]]) -> None:
        self.kubectl("patch", kind, name, "--type", "json", "--patch", json.dumps(operations))

    def logs(self, job: str) -> str:
        return self.kubectl("logs", f"job/{job}", "--container", GATE_CONTAINER, timeout=300)

    def upgrade(self, chart: Path, values: Mapping[str, Any]) -> None:
        self.helm(
            "upgrade",
            self.release,
            str(chart),
            "--install",
            "--values",
            "-",
            data=json.dumps(values),
        )

    def release_values(self) -> dict:
        """The values the release runs now, or {} before the first install."""
        try:
            output = self.helm("get", "values", self.release, "--output", "json")
        except ClusterError as error:
            if "not found" in str(error):
                return {}
            raise
        return dict(json.loads(output) or {})

    def version(self) -> dict:
        return dict(json.loads(self.kubectl("version", "--output", "json")))

    def helm_version(self) -> str:
        return self.helm("version", "--short").strip()

    def can_i(self, verb: str, resource: str) -> bool:
        try:
            return self.kubectl("auth", "can-i", verb, resource).strip() == "yes"
        except ClusterError:
            return False


# Gate Jobs.


@dataclass(frozen=True)
class Gate:
    """One gate Job: its kind and command, where it runs, and the files it receives."""

    kind: str
    label: str
    command: Sequence[str]
    node: str | None = None
    files: Mapping[str, bytes] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    resources: Mapping[str, str] = field(default_factory=dict)
    image: str | None = None
    runtime_class: str | None = None
    host_network: bool = False
    model_volume: Mapping[str, Any] | None = None
    service_account: bool = False
    # A gate whose command prints only plain output, such as a fabric probe, returns its log.
    plain: bool = False


@dataclass(frozen=True)
class GateResult:
    name: str
    log: Path
    output: str
    files: Mapping[str, bytes]
    pod: Mapping[str, Any]


def gate_files(output: str) -> dict[str, bytes]:
    """The files a gate's log returns, checked against its last manifest line."""
    manifests = [line for line in output.splitlines() if line.startswith(kubernetes_gate.MANIFEST)]
    if not manifests:
        raise GateOutputError(
            "the Job's log holds no manifest of its files; the log may have been truncated"
        )
    manifest = json.loads(manifests[-1][len(kubernetes_gate.MANIFEST) :])
    files = remote.files(output)
    if set(files) != set(manifest):
        missing = sorted(set(manifest) - set(files))
        raise GateOutputError(
            f"the Job's log returns other files than its manifest names: missing {missing}"
        )
    for name, data in files.items():
        entry = manifest[name]
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise GateOutputError(f"the Job's log returns {name} other than its manifest records")
    return files


# The release's objects, which helm reads, creates, changes and removes; helm keeps its
# releases in Secrets.
_RELEASE_KINDS = (
    "secrets",
    "configmaps",
    "services",
    "serviceaccounts",
    "persistentvolumeclaims",
    "statefulsets.apps",
    "deployments.apps",
    "jobs.batch",
    "roles.rbac.authorization.k8s.io",
    "rolebindings.rbac.authorization.k8s.io",
)
# Every verb and resource a run needs, as `kubectl auth can-i` takes them.
PERMISSIONS = (
    *(
        (verb, kind)
        for kind in _RELEASE_KINDS
        for verb in ("get", "list", "create", "patch", "delete")
    ),
    ("get", "pods"),
    ("list", "pods"),
    ("patch", "pods"),
    ("delete", "pods"),
    ("get", "pods/log"),
    ("get", "nodes"),
)
# Pod scheduling gates are on by default from this Kubernetes version.
MINIMUM_SERVER = (1, 27)


def server_version(server: Mapping[str, Any]) -> tuple[int, int]:
    """The server's major and minor version, as (0, 0) when it reports none."""
    match = re.match(r"v?(\d+)\.(\d+)", str(server.get("gitVersion", "")))
    return (int(match[1]), int(match[2])) if match else (0, 0)


def _job_name(kind: str, label: str, seed: str) -> str:
    stem = re.sub(r"[^a-z0-9-]+", "-", f"{kind}-{label}".lower()).strip("-")[:48].strip("-")
    return f"{stem}-{hashlib.sha256(seed.encode()).hexdigest()[:10]}"


def _gate_command(*, inputs: bool, returns: Mapping[str, str], command: Sequence[str]) -> list[str]:
    """A Narwhal image's command around `command`: unpack the inputs, return the files."""
    argv = ["python", "-m", "narwhal.deployment.kubernetes_gate", "run"]
    if inputs:
        argv += ["--inputs", f"{INPUT}/{kubernetes_gate.INPUTS}", "--into", "/tmp/narwhal-in"]
    for name, path in returns.items():
        argv += ["--return", f"{name}={path}"]
    return [*argv, "--", *command]


# Every gate Job keeps its output on the state volume, in the directory this variable names,
# and still prints it for the runner: the log, the exit status, and the files it returns.
KEEP_ENV = kubernetes_gate.KEEP_ENV
_KEEP = f"""[ -d "${KEEP_ENV}" ] || exit 4
{{ "$@"; echo "$?" > "${KEEP_ENV}/status"; }} 2>&1 | tee "${KEEP_ENV}/output.log"
exit "$(cat "${KEEP_ENV}/status")"
"""


def _shell(script: str) -> list[str]:
    return ["sh", "-c", "set -eu\n" + script]


# Discovery in the engine image, which holds the engine's Python but not Narwhal.

POD_PROBE = (
    inspect.getsource(conv_state_layers)
    + inspect.getsource(gpu_inventory)
    + r"""
import hashlib, json, os, re, subprocess, sys
from importlib.metadata import distributions
from pathlib import Path

inputs = json.load(sys.stdin)


def command(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Inspection failed: " + args[0])
    return result.stdout


model_path = Path(inputs["model_dir"]) / "config.json"
model_bytes = model_path.read_bytes()
model = json.loads(model_bytes)
tokenizer_path = Path(inputs["model_dir"]) / "tokenizer_config.json"
tokenizer = json.loads(tokenizer_path.read_text()) if tokenizer_path.is_file() else {}


def has_custom_code(value):
    if isinstance(value, dict):
        return bool(value.get("auto_map")) or any(has_custom_code(item) for item in value.values())
    if isinstance(value, list):
        return any(has_custom_code(item) for item in value)
    return False


wanted = re.compile(inputs["packages"])
packages = {
    d.metadata["Name"].lower(): d.version
    for d in distributions()
    if wanted.fullmatch(d.metadata["Name"].lower())
}
runtime, gpus, common_devices = gpu_inventory(command)
allowed = tuple(inputs["environment_prefixes"])
environment = {}
# The Job's own variables are the image's, apart from the ones the runner set.
for name, value in sorted(os.environ.items()):
    if name in inputs["job_environment"] or name in inputs["managed_environment"]:
        continue
    if name.startswith(allowed) or name in ("LD_LIBRARY_PATH", "PYTHONPATH"):
        if not any(
            word in name
            for word in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "SSH", "SKIP_COMPAT")
        ):
            environment[name] = value
text_model = model.get("text_config", model)
print(
    json.dumps(
        {
            "model_sha256": hashlib.sha256(model_bytes).hexdigest(),
            "requires_trust_remote_code": has_custom_code(model) or has_custom_code(tokenizer),
            "conv_state_layers": conv_state_layers(model),
            "multimodal": any(key in model for key in ("vision_config", "audio_config")),
            "model_dtype": text_model.get(
                "dtype", text_model.get("torch_dtype", model.get("torch_dtype"))
            ),
            "max_model_len": text_model.get("max_position_embeddings", 16384),
            "packages": packages,
            "runtime": runtime,
            "gpus": gpus,
            "common_devices": common_devices,
            "image_environment": environment,
        }
    )
)
"""
)

# Read a published attestation attempt of an engine container from the state volume.
_PUBLISHED = """import shutil, sys, time
from pathlib import Path
from narwhal.deployment.attestation_contract.capture import PUBLISHED, published_attempt
home, out, seconds = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
deadline = time.monotonic() + seconds
while not (home / PUBLISHED).exists():
    if time.monotonic() > deadline:
        sys.exit(f"narwhal-gate: the sidecar published no attestation in {home} in {seconds:g}s")
    time.sleep(2)
shutil.copytree(published_attempt(home), out)
"""


# Inputs.


def _node(role: str) -> int:
    return int(role.split("-")[1])


def engine_nodes(env: Mapping[str, str]) -> dict[str, str]:
    """Each engine role and the Kubernetes node its pod runs on, in role order."""
    found = {
        f"engine-{match[1]}": value
        for name, value in env.items()
        if (match := NODE.fullmatch(name))
    }
    return dict(sorted(found.items(), key=lambda item: _node(item[0])))


def target_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment as the cluster's containers see it: the model at /model, the router
    behind HAProxy unless set."""
    return {
        **env,
        "NARWHAL_MODEL_DIR": MODEL,
        "NARWHAL_RUN_DIR": env.get("NARWHAL_RUN_DIR") or STATE,
        "NARWHAL_ROUTER_URL": env.get("NARWHAL_ROUTER_URL") or ROUTER_URL,
    }


def chart_path(env: Mapping[str, str]) -> Path:
    if env.get(CHART):
        return Path(env[CHART])
    return Path(__file__).resolve().parents[3] / "deploy/helm/narwhal"


def base_values(env: Mapping[str, str]) -> dict:
    path = Path(env.get(VALUES, ""))
    if not env.get(VALUES) or not path.is_file():
        raise ValueError(f"set {VALUES} to the chart values file, in JSON, of this fleet")
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError(f"{path} must hold one JSON object of chart values")
    return document


def _tree_digest(root: Path) -> str:
    read = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        read.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return "sha256:" + read.hexdigest()


def _values(env: Mapping[str, str], names: Sequence[str]) -> dict[str, str]:
    return {name: env[name] for name in names if name in env}


def inputs(env: Mapping[str, str]) -> dict[str, Input]:
    """The digest of every input fact of the Kubernetes graph, after checking each input.

    Raises `InputError` naming every missing or malformed input, before the cluster is touched.
    """
    problems: list[str] = []
    env = target_env(env)
    try:
        settings(env)
    except InputError as error:
        problems.extend(error.problems)
    if not DNS_NAME.fullmatch(env.get(NAMESPACE, "")):
        problems.append(f"set {NAMESPACE} to the namespace the fleet runs in")
    values: dict = {}
    try:
        values = base_values(env)
    except (ValueError, OSError) as error:
        problems.append(str(error))
    images = values.get("images", {}) if isinstance(values.get("images"), dict) else {}
    problems.extend(
        f"{VALUES}: set images.{name} to an image by registry digest"
        for name in ("narwhal", "fabric")
        if not DIGEST.fullmatch(str(images.get(name, "")))
    )
    problems.extend(
        f"{VALUES}: leave {name} unset; the runner sets it"
        for name in ("engines", "directEngines", "gates")
        if values.get(name)
    )
    credentials = (
        values.get("credentials", {}) if isinstance(values.get("credentials"), dict) else {}
    )
    named = (credentials.get("engineApiKey") or {}).get("env")
    if env.get("NARWHAL_ENGINE_KEY") and named != "NARWHAL_ENGINE_KEY":
        problems.append(
            f"{VALUES}: set credentials.engineApiKey.env to NARWHAL_ENGINE_KEY, the variable the "
            "fleet reads the engine API key from, so the Jobs and routers receive it"
        )
    chart = chart_path(env)
    if not (chart / "Chart.yaml").is_file():
        problems.append(f"set {CHART} to the Narwhal Helm chart directory")
    nodes = engine_nodes(env)
    if len(nodes) < 2:
        problems.append("set NARWHAL_NODE_<n>_KUBERNETES_NODE for each engine, at least two")
    for role, node in nodes.items():
        n = _node(role)
        if not DNS_NAME.fullmatch(node):
            problems.append(f"NARWHAL_NODE_{n}_KUBERNETES_NODE must name a Kubernetes node")
        try:
            discovery.expected_tree(env, n)
        except ValueError as error:
            problems.append(str(error))
        if not DIGEST.fullmatch(discovery.value(env, n, "ENGINE_IMAGE")):
            problems.append(f"{role}: pin NARWHAL_ENGINE_IMAGE to an image by registry digest")
        problems.extend(
            f"{role}: set NARWHAL_NODE_{n}_{field}"
            for field in ("URL", "ATTESTATION_URL")
            if not env.get(f"NARWHAL_NODE_{n}_{field}")
        )
        if not discovery.value(env, n, "FABRIC_INTERFACE"):
            problems.append(
                f"{role}: set NARWHAL_FABRIC_INTERFACE or NARWHAL_NODE_{n}_FABRIC_INTERFACE"
            )
    problems.extend(
        f"set {name}; the engine pods read it"
        for name in (GPU_RESOURCE, MODEL_VOLUME, STATE_CLAIM)
        if not env.get(name)
    )
    try:
        fabric.inputs(env)
    except ValueError as error:
        problems.append(str(error))
    if not env.get("NARWHAL_ENGINE_MODEL_NAME"):
        problems.append("set NARWHAL_ENGINE_MODEL_NAME to the served model name")
    if problems:
        raise InputError(list(dict.fromkeys(problems)))
    claimed = {*SLO, MARGIN, *PROFILE, *CALIBRATION, *_ROUTER_INPUTS, *CLUSTER_INPUTS}
    claimed |= {name for _, name, _ in fabric.WORKLOAD} | {CHART, VALUES}
    discovered = {
        name: value
        for name, value in env.items()
        if name.startswith("NARWHAL_")
        and name not in claimed
        and not _NOT_INPUTS.fullmatch(name)
        and not _FABRIC_TEST.fullmatch(name)
    }
    discovered["images"] = json.dumps(images, sort_keys=True)
    router = {
        name: values.get(name)
        for name in ("routers", "haproxy", "credentials", "monitoring", "lease", "state")
    }
    source = "environment"
    return {
        "cluster": Input.value(_values(env, CLUSTER_INPUTS), source),
        "discovery": Input.value(discovered, f"environment and {env[VALUES]}"),
        "revision": Input.value(
            {"source": provenance.source_digest(), "chart": _tree_digest(chart)},
            "the installed package and the chart",
        ),
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
        "router": Input.value(
            {"env": _values(env, _ROUTER_INPUTS), "values": router},
            f"environment and {env[VALUES]}",
        ),
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


def _private(path: Path, document: Any) -> None:
    write_private(path, (json.dumps(document, indent=2) + "\n").encode())


def _container(pod: Mapping[str, Any], name: str) -> dict | None:
    for status in pod.get("status", {}).get("containerStatuses", []) or []:
        if status.get("name") == name:
            return dict(status)
    return None


def container_identity(pod: Mapping[str, Any], name: str) -> dict | None:
    """The running container's ID, image ID and start, or None while it does not run."""
    status = _container(pod, name)
    running = (status or {}).get("state", {}).get("running")
    if status is None or not running or not status.get("containerID"):
        return None
    return {
        "container_id": status["containerID"].partition("://")[2] or status["containerID"],
        "image_id": status.get("imageID", ""),
        "started_at": running.get("startedAt", ""),
    }


class KubernetesDeployment:
    """The actions, live checks and topology of one Kubernetes deployment in one run directory."""

    def __init__(
        self,
        directory: Path,
        env: Mapping[str, str],
        *,
        dry: bool = False,
        cluster: Callable[[Mapping[str, str], Path], Cluster] | None = None,
    ) -> None:
        self.directory = directory
        self.env = target_env(env)
        # A dry deployment runs no Job: `plan`, and `status` while another runner holds the run.
        self.dry = dry
        self._cluster = cluster or Cluster
        self.sleep: Callable[[float], None] = time.sleep
        self.clock: Callable[[], float] = time.monotonic
        self.announce: Callable[[str], None] = lambda line: print(line, flush=True)
        self._fabric: tuple[str, fabric.Fabric] | None = None
        # The fleet file router-start gives the routers, while it runs.
        self._started = 0

    # The runner.

    def runner(self) -> Runner:
        actions = {
            "access": self.access,
            "discover": self.discover,
            "engine-prepare": self.engine_prepare,
            "install": self.install,
            "engine-start": self.engine_start,
            "engine-check": self.engine_check,
            "cache-capture": self.cache_capture,
            "transfer-measure": self.transfer_measure,
            "cache-groups": self.cache_groups,
            "fabric-budget": self.fabric_budget,
            "fabric-edge": self.fabric_edge,
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
            KUBERNETES_GRAPH,
            self.directory,
            target=TARGET,
            revision=provenance.source_digest(),
            actions={name: self._announced(name, action) for name, action in actions.items()},
            topology=Topology(self),
            probes={
                "engine_identity": self.engine_running,
                "link": self.link_now,
                "sidecar_identity": self.sidecar_running,
                "router_identity": self.routers_running,
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

    # Recorded outputs.

    def engines(self) -> list[str]:
        return list(engine_nodes(self.env))

    def node(self, role: str) -> str:
        return engine_nodes(self.env)[role]

    def recorded(self, stage: str, instance: str = "") -> Path:
        """The attempt directory of a stage's last passed attempt, from the run record."""
        entry = run_record.read(self.directory)["stages"].get(key(stage, instance))
        if entry is None or entry["status"] != PASSED:
            raise ValueError(f"{key(stage, instance)} has not passed in {self.directory}")
        return self.directory / entry["directory"]

    def stage_env(self) -> dict[str, str]:
        """The environment with the variables discovery derived."""
        config = self.recorded("discover") / "config"
        return {**self.env, **read_environment(config / "deployment.env")}

    def package(self) -> Path:
        return self.recorded("discover") / "package"

    def cluster(self, context: StageContext | None = None) -> Cluster:
        logs = context.directory / "logs" if context is not None else self.directory / "live"
        return self._cluster(self.env, logs)

    @staticmethod
    def state_dir(context: StageContext) -> str:
        """The attempt directory's counterpart on the state volume."""
        relative = context.directory.relative_to(context.run_directory).as_posix()
        return f"{STATE}/deploy/{context.run_id}/{relative}"

    # The chart's values and gate Jobs.

    def launches(self) -> dict[str, Path]:
        """Each engine's prepared launch directory, from the passed engine-prepare stages."""
        return {role: self.recorded("engine-prepare", role) / "launch" for role in self.engines()}

    def placement(self, roles: Sequence[str]) -> dict[str, str]:
        """The node discovery measured each engine role on, where engine-start pins its pod."""
        nodes = engine_nodes(self.env)
        if missing := [role for role in roles if role not in nodes]:
            raise ValueError(
                f"{', '.join(missing)}: no discovered node; set NARWHAL_NODE_<n>_KUBERNETES_NODE "
                "and discover again"
            )
        return {role: nodes[role] for role in roles}

    def values(
        self,
        cluster: Cluster,
        gates: Sequence[Mapping[str, Any]] = (),
        *,
        engines: Mapping[str, Any] | None = None,
        fleet: str | None = None,
    ) -> dict:
        """The release's values for one upgrade: the base values, the gate Jobs, and what the
        release runs now.

        Every upgrade keeps the engines, their placement, the routers and the rendered
        dashboard the release runs, read from the release itself, so a stage that only runs
        Jobs leaves them running. `engines` replaces the engines, and `fleet` starts the
        routers on that fleet file, or stops them when empty.
        """
        current = cluster.release_values()
        values = copy.deepcopy(base_values(self.env))
        values.setdefault("state", {})["claimName"] = self.env[STATE_CLAIM]
        if engines is None:
            values["engines"] = copy.deepcopy(current.get("engines") or {})
            values["placement"] = dict(current.get("placement") or {})
        else:
            values["engines"] = dict(engines)
            roles = [f"engine-{n}" for n in range(1, int(engines.get("count", 0)) + 1)]
            values["placement"] = self.placement(roles) if engines else {}
        routers = values.setdefault("routers", {})
        running = current.get("routers") or {}
        if fleet is None:
            routers["enabled"] = bool(running.get("enabled"))
            if routers["enabled"]:
                routers["fleet"] = running["fleet"]
        else:
            routers["enabled"] = bool(fleet)
            if fleet:
                routers["fleet"] = fleet
        values["gates"] = list(gates)
        monitoring = values.get("monitoring") or {}
        # The chart's schema needs the dashboard whenever monitoring is on, though the chart
        # runs monitoring only with the routers.
        if monitoring.get("enabled"):
            grafana = monitoring.setdefault("grafana", {})
            shown = ((current.get("monitoring") or {}).get("grafana") or {}).get("dashboard")
            if not grafana.get("dashboard"):
                grafana["dashboard"] = shown or self._dashboard()
        return values

    @staticmethod
    def _dashboard() -> str:
        """The Grafana dashboard the chart serves, rendered as `make observe` renders it."""
        try:
            from tools.observability.chart_dashboard import chart_dashboard
        except ImportError:
            raise ValueError(
                "monitoring is enabled and monitoring.grafana.dashboard is empty; render it with "
                "python -m tools.observability.chart_dashboard <file> from a checkout and set it "
                f"in {VALUES}"
            ) from None
        return chart_dashboard()

    def _entry(self, gate: Gate, name: str, inputs: str | None) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "name": name,
            "kind": gate.kind,
            "command": list(gate.command),
            "deadlineSeconds": JOB_SECONDS[gate.kind],
        }
        optional = {
            "node": gate.node,
            "env": dict(gate.env) or None,
            "resources": dict(gate.resources) or None,
            "image": gate.image,
            "runtimeClassName": gate.runtime_class,
            "hostNetwork": gate.host_network or None,
            "inputs": inputs,
            "modelVolume": dict(gate.model_volume) if gate.model_volume else None,
            "serviceAccount": gate.service_account or None,
        }
        entry.update({name: value for name, value in optional.items() if value is not None})
        return entry

    def start_gates(
        self, context: StageContext, gates: Sequence[Gate], running: Sequence[Mapping] = ()
    ) -> list[dict[str, Any]]:
        """Make each gate's inputs ConfigMap and upgrade the release with the gates' Jobs,
        keeping the `running` entries; return the new entries."""
        cluster = self.cluster(context)
        entries = []
        for gate in gates:
            # Each Job of one attempt gets its own name, as Job specs cannot change.
            self._started += 1
            seed = f"{context.run_id}/{context.stage}/{context.instance}/{context.attempt}"
            name = _job_name(gate.kind, gate.label, f"{seed}/{self._started}")
            inputs = None
            if gate.files:
                inputs = name
                cluster.apply(
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {
                            "name": name,
                            "labels": {"narwhal.io/run": context.run_id, "narwhal.io/gate": name},
                        },
                        "binaryData": {
                            file: _b64(data) for file, data in sorted(gate.files.items())
                        },
                    }
                )
            kept = replace(gate, command=["sh", "-c", _KEEP, "sh", *gate.command])
            entry = self._entry(kept, name, inputs)
            # The chart sets the Job's keep directory, the only one a confined Job may write.
            entry["keep"] = self.state_dir(context).removeprefix(f"{STATE}/") + f"/jobs/{name}"
            entries.append(entry)
        cluster.upgrade(chart_path(self.env), self.values(cluster, [*running, *entries]))
        return entries

    def finish_gate(
        self, context: StageContext, gate: Gate, entry: Mapping[str, Any], where: str = ""
    ) -> GateResult:
        """Wait for one gate Job, keep its log, and return its output and files."""
        cluster = self.cluster(context)
        name = str(entry["name"])
        deadline = self.clock() + JOB_SECONDS[gate.kind] + GRACE_SECONDS
        while True:
            job = cluster.get("job", name) or {}
            conditions = {
                c.get("type"): c for c in job.get("status", {}).get("conditions", []) or []
            }
            done = [c for t, c in conditions.items() if t in ("Complete", "Failed")]
            if any(c.get("status") == "True" for c in done):
                break
            if self.clock() > deadline:
                raise GateFailed(
                    where or gate.node or "cluster",
                    f"Job {name}",
                    124,
                    "did not finish within its time limit",
                    context.directory / "logs",
                )
            self.sleep(POLL_SECONDS)
        pods = cluster.items("pod", f"job-name={name}")
        pod = pods[0] if pods else {}
        output = ""
        try:
            output = cluster.logs(name)
        except ClusterError:
            output = ""
        log = context.directory / "logs" / f"{name}.log"
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(log, output.encode())
        if gate.files:
            cluster.delete("configmap", name)
        failed = conditions.get("Failed", {}).get("status") == "True"
        if failed:
            terminated = (_container(pod, GATE_CONTAINER) or {}).get("state", {}).get("terminated")
            status = int((terminated or {}).get("exitCode", 4))
            causes = [
                line.split(": ", 1)[-1]
                for line in output.splitlines()
                if line.startswith(("narwhal-gate: ", "narwhal-host: ", "narwhal: "))
            ]
            reason = conditions["Failed"].get("message") or conditions["Failed"].get("reason", "")
            cause = causes[-1] if causes else (reason or f"exited {status}")
            raise GateFailed(where or gate.node or "cluster", f"Job {name}", status, cause, log)
        files = {} if gate.plain else gate_files(output)
        return GateResult(name, log, output, files, pod)

    def gates(
        self, context: StageContext, gates: Sequence[Gate], into: Path | None = None
    ) -> list[GateResult]:
        """Run gate Jobs together; write each one's returned files under `into`."""
        entries = self.start_gates(context, gates)
        results = [
            self.finish_gate(context, gate, entry)
            for gate, entry in zip(gates, entries, strict=True)
        ]
        for result in results:
            remote.write(into or context.directory, result.files)
        return results

    def gate(self, context: StageContext, gate: Gate, into: Path | None = None) -> GateResult:
        return self.gates(context, [gate], into)[0]

    def narwhal_gate(
        self,
        context: StageContext,
        kind: str,
        script: str,
        returns: Mapping[str, str],
        files: Mapping[str, bytes] | None = None,
        **options: Any,
    ) -> GateResult:
        """A gate Job in the Narwhal image that runs `script` and returns `returns`."""
        archive = {kubernetes_gate.INPUTS: kubernetes_gate.pack(dict(files))} if files else {}
        command = _gate_command(inputs=bool(files), returns=returns, command=_shell(script))
        return self.gate(
            context, Gate(kind, context.instance or kind, command, files=archive, **options)
        )

    # access

    def access(self, context: StageContext) -> None:
        cluster = self.cluster(context)
        version = cluster.version()
        helm = cluster.helm_version()
        namespace = cluster.kubectl("get", "namespace", cluster.namespace, "--output", "name")
        problems = []
        server = version.get("serverVersion", {})
        if server_version(server) < MINIMUM_SERVER:
            problems.append(
                f"the cluster runs Kubernetes {server.get('gitVersion', 'of an unknown version')}; "
                "the chart holds engine pods at a scheduling gate, which needs "
                f"{'.'.join(map(str, MINIMUM_SERVER))} or later"
            )
        denied = [
            f"{verb} {resource}"
            for verb, resource in PERMISSIONS
            if not cluster.can_i(verb, resource)
        ]
        nodes = {}
        resource = self.env[GPU_RESOURCE]
        rdma = self.env.get(RDMA_RESOURCE, "")
        for role, name in engine_nodes(self.env).items():
            node = cluster.kubectl("get", "node", name, "--output", "json")
            allocatable = json.loads(node).get("status", {}).get("allocatable", {})
            nodes[role] = {"node": name, "allocatable": allocatable}
            if (
                not str(allocatable.get(resource, "0")).isdigit()
                or int(allocatable.get(resource, "0")) < 1
            ):
                problems.append(f"node {name} of {role} offers no {resource}")
            if rdma and not str(allocatable.get(rdma, "0")).strip("0"):
                problems.append(f"node {name} of {role} offers no {rdma}")
        _private(
            context.directory / "access.json",
            {
                # down acts only on this context, namespace and release.
                "cluster": self.identity(),
                "server": server,
                "client": version.get("clientVersion", {}),
                "helm": helm,
                "namespace": namespace.strip(),
                "denied": denied,
                "nodes": nodes,
            },
        )
        if denied:
            problems.append(
                f"the Kubernetes identity may not {', '.join(denied)} in {cluster.namespace}"
            )
        if problems:
            raise GateFailed(
                "cluster", "access", 1, "; ".join(problems), context.directory / "access.json"
            )

    # discover

    def _discovery_gates(self, role: str) -> tuple[Gate, Gate, dict[str, Any]]:
        env = self.env
        n = _node(role)
        node = self.node(role)
        engine = engine_backend(env.get("NARWHAL_ENGINE_BACKEND")).engine_launcher()
        packages = engine.image_packages
        if discovery.model_file(env, n, MODEL) is not None:
            if not isinstance(engine, ModelFormats):
                raise ValueError(
                    f"{role}: this engine serves a model directory; unset NARWHAL_MODEL_PATH"
                )
            if engine.model_file_packages:
                packages += "|" + engine.model_file_packages
        values = base_values(env)
        job_env = sorted(
            name
            for name in (values.get("credentials", {}).get("engineApiKey", {}).get("env"),)
            if name
        )
        selected = {
            "image": discovery.value(env, n, "ENGINE_IMAGE"),
            "model_dir": MODEL,
            "run_dir": STATE,
            "interface": discovery.value(env, n, "FABRIC_INTERFACE"),
            "environment_prefixes": engine.environment_prefixes,
            "managed_environment": sorted(engine.managed_environment),
            "job_environment": job_env,
            "packages": packages,
        }
        # Discovery inspects every GPU the node offers, as an SSH host shows them all.
        allocatable = _json(self.recorded("access") / "access.json")["nodes"][role]["allocatable"]
        count = str(allocatable[env[GPU_RESOURCE]])
        script = (
            "mkdir -p /tmp/narwhal-out\n"
            f"python3 {INPUT}/probe.py < {INPUT}/inputs.json > /tmp/narwhal-out/observed.json\n"
            f"python3 {INPUT}/checkpoint.py < {INPUT}/inputs.json "
            "> /tmp/narwhal-out/checkpoint.json\n"
        )
        command = [
            "python3",
            f"{INPUT}/gate.py",
            "run",
            "--return",
            "observed.json=/tmp/narwhal-out/observed.json",
            "--return",
            "checkpoint.json=/tmp/narwhal-out/checkpoint.json",
            "--return",
            f"model-config.json={MODEL}/config.json",
            "--",
            *_shell(script),
        ]
        files = {
            "probe.py": POD_PROBE.encode(),
            "checkpoint.py": Path(checkpoint_manifest.__file__).read_bytes(),
            "gate.py": Path(kubernetes_gate.__file__).read_bytes(),
            "inputs.json": json.dumps(selected).encode(),
        }
        inspect_gate = Gate(
            "discovery",
            role,
            command,
            node=node,
            files=files,
            resources={env[GPU_RESOURCE]: count},
            image=str(selected["image"]),
            runtime_class=env.get(RUNTIME_CLASS) or None,
            model_volume=json.loads(env[MODEL_VOLUME]),
        )
        interface = shlex.quote(str(selected["interface"]))
        interfaces = Gate(
            "fabric-probe",
            f"{role}-interfaces",
            _shell(
                "printf 'narwhal-interfaces %s\\n' "
                f"\"$(ip -json address show dev {interface} | tr -d '\\n')\""
            ),
            node=node,
            host_network=True,
            plain=True,
        )
        return inspect_gate, interfaces, selected

    def discover(self, context: StageContext) -> None:
        env = self.env
        roles = self.engines()
        pending = {role: self._discovery_gates(role) for role in roles}
        gates = [gate for role in roles for gate in pending[role][:2]]
        entries = self.start_gates(context, gates)
        results = [
            self.finish_gate(context, gate, entry)
            for gate, entry in zip(gates, entries, strict=True)
        ]
        for index, role in enumerate(roles):
            remote.write(context.directory / "inspection" / role, results[2 * index].files)
        observations: dict[str, dict] = {}
        checkpoints: dict[str, dict] = {}
        for index, role in enumerate(roles):
            inspected, interfaces = results[2 * index], results[2 * index + 1]
            observed = json.loads(inspected.files["observed.json"])
            checkpoint = json.loads(inspected.files["checkpoint.json"])
            status = _container(inspected.pod, GATE_CONTAINER) or {}
            image = pending[role][2]["image"]
            image_id = str(status.get("imageID", ""))
            if not image_id.endswith("@" + image.rpartition("@")[2]):
                raise GateFailed(
                    self.node(role),
                    f"{role} discovery",
                    1,
                    f"the Job ran image {image_id or 'unknown'}, not {image}",
                    inspected.log,
                )
            lines = dict(
                line.split(" ", 1)
                for line in interfaces.output.splitlines()
                if line.startswith("narwhal-interfaces ")
            )
            observed.update(
                hostname=self.node(role),
                image_id=image,
                interfaces=json.loads(lines.get("narwhal-interfaces", "[]")),
                model_tree_sha256=checkpoint["model_tree_sha256"],
                model_file_count=checkpoint["file_count"],
            )
            relative = discovery.model_file(env, _node(role), MODEL)
            if relative is not None:
                files = {row["path"]: row["sha256"] for row in checkpoint["files"]}
                if relative not in files:
                    raise ValueError(
                        f"{role}: NARWHAL_MODEL_PATH names no file in the model volume"
                    )
                observed.update(model_file=relative, model_file_sha256=files[relative])
            if checkpoint["model_tree_sha256"] != discovery.expected_tree(env, _node(role)):
                raise GateFailed(
                    self.node(role),
                    f"{role} checkpoint",
                    1,
                    f"checkpoint tree digest {checkpoint['model_tree_sha256']} differs from "
                    "NARWHAL_MODEL_TREE_SHA256",
                    inspected.log,
                )
            observations[role] = observed
            checkpoints[role] = checkpoint
            _private(context.directory / "inspection" / f"{role}.json", observed)
            _private(context.directory / "inspection" / f"{role}-checkpoint.json", checkpoint)
        self._write_records(context, observations)

    def hosts(self) -> list[Host]:
        """One host for each engine node, holding its engine roles, as the fabric reads them."""
        grouped: dict[str, list[str]] = {}
        for role, node in engine_nodes(self.env).items():
            grouped.setdefault(node, []).append(role)
        return [Host(node, NAMESPACE, None, tuple(roles)) for node, roles in grouped.items()]

    def _write_records(self, context: StageContext, observations: dict[str, dict]) -> None:
        """Write the fleet, launch records and role files discovery derives, as `package`
        writes them for SSH hosts."""
        env = self.env
        hosts = self.hosts()
        out = context.directory / "inspection"
        fleet, launches, sources, derived = discovery.build_records(hosts, env, observations, out)
        config = context.directory / "config"
        config.mkdir(mode=0o700)
        stage_env = {**env, **derived}
        revision = base_values(env)["images"]["narwhal"].rpartition("@sha256:")[2]
        engine = launcher(fleet.get("engine", {}).get("backend"))
        selected = {}
        for role in launches["engines"]:
            selected[role] = select_values("engine", _node(role), fleet, stage_env)
            selected[role]["NARWHAL_DEPLOYMENT_REVISION"] = revision
        _private(config / "engine-launch.json", launches)
        records = load_launches(config / "engine-launch.json", list(selected), selected)
        install.check_hardware(fleet, records)
        snapshots = {helper.name: helper.source.read_bytes() for helper in helpers(engine)}
        for helper in helpers(engine):
            for role in selected:
                selected[role][helper.sha256_env] = hashlib.sha256(
                    snapshots[helper.name]
                ).hexdigest()
        router = select_values("router", None, fleet, stage_env)
        package = context.directory / "package"
        for host in hosts:
            folder = package / host.id
            folder.mkdir(mode=0o700, parents=True)
            for role in host.roles:
                write_environment(folder / f".env.{role}", selected[role])
                _private(folder / f"engine-launch.{role}.json", records[role])
            for name, content in snapshots.items():
                write_private(folder / name, content)
        _private(package / "profiling-limits.json", install.profiling_limits(records, fleet))
        # The cluster's fleet names each engine's URL itself: no router file resolves them.
        resolved = copy.deepcopy(fleet)
        for entry in resolved["engines"]:
            for name in ("url", "attestation_url"):
                reference = re.fullmatch(r"\$\{([^}]+)\}", entry.get(name, ""))
                if reference:
                    entry[name] = router[reference[1]]
        _private(config / "fleet.json", fleet)
        _private(package / "fleet.json", resolved)
        _private(config / "engine-launch.sources.json", sources)
        write_environment(config / "deployment.env", derived)

    # engine-prepare

    def job_env(self, role_env: Mapping[str, str], *, credential: bool) -> bytes:
        """A role file for a Job's inputs ConfigMap, which holds no secret. With `credential`,
        the engine API key comes from the Job's own variable, which the chart sets from its
        Secret for the gates that need it; without, the file names no key."""
        fleet = _json(self.package() / "fleet.json")
        key = fleet.get("engine", {}).get("engine_api_key_env")
        lines = []
        for name, value in sorted(role_env.items()):
            if key and name in (key, "NARWHAL_ENGINE_API_KEY"):
                if credential:
                    lines.append(f'export {name}="${{{key}:?the Job has no {key}}}"\n')
            else:
                lines.append(f"export {name}={shlex.quote(value)}\n")
        return "".join(lines).encode()

    def engine_prepare(self, context: StageContext) -> Outcome:
        role = context.instance
        package = self.package()
        host = self.node(role)
        role_env = read_environment(package / host / f".env.{role}")
        files = {
            ".env": self.job_env(role_env, credential=True),
            f"config/engine-launch.{role}.json": (
                package / host / f"engine-launch.{role}.json"
            ).read_bytes(),
        }
        engine = launcher(
            json.loads(files[f"config/engine-launch.{role}.json"]).get("runtime", {}).get("backend")
        )
        for helper in helpers(engine):
            files[f"{HELPER_DIRECTORY}/{helper.name}"] = (package / host / helper.name).read_bytes()
        script = (
            "cd /tmp/narwhal-in\nset -a\n. ./.env\nset +a\n"
            "narwhal-engine prepare --backend kubernetes --out /tmp/narwhal-launch\n"
        )
        self.narwhal_gate(
            context,
            "prepare",
            script,
            {"launch/": "/tmp/narwhal-launch"},
            files,
            node=host,
            model_volume=json.loads(self.env[MODEL_VOLUME]),
        )
        launch = context.directory / "launch"
        return Outcome(
            {
                "launch": {
                    "plan_sha256": _digest(launch / "launch.json"),
                    "manifests_sha256": _digest(launch / "kubernetes.json"),
                }
            }
        )

    # install

    def _retained_maps(self, cluster: Cluster, values: Mapping[str, Any]) -> list[dict]:
        """The hook ConfigMaps the running engine pods mount that the new values drop.

        An OnDelete pod keeps the spec it started with until engine-start replaces it, so the
        release keeps the ConfigMaps it mounts until then.
        """
        engines = values.get("engines") or {}
        kept = {item["metadata"]["name"] for item in engines.get("configMaps", [])}
        retained = []
        for pod in cluster.items("pod", "app.kubernetes.io/component=engine"):
            for volume in pod.get("spec", {}).get("volumes", []) or []:
                sources = [volume.get("configMap")] + [
                    source.get("configMap")
                    for source in (volume.get("projected") or {}).get("sources", [])
                ]
                for source in sources:
                    name = (source or {}).get("name")
                    if not name or name in kept or name == "kube-root-ca.crt":
                        continue
                    live = cluster.get("configmap", name)
                    if live is not None:
                        kept.add(name)
                        retained.append(
                            {
                                "apiVersion": "v1",
                                "kind": "ConfigMap",
                                "metadata": {"name": name},
                                **{k: live[k] for k in ("data", "binaryData") if k in live},
                            }
                        )
        return retained

    def install(self, context: StageContext) -> None:
        cluster = self.cluster(context)
        launches = self.launches()
        values = self.values(cluster, engines=engine_set.chart_values(list(launches.values())))
        if retained := self._retained_maps(cluster, values):
            values["engines"]["configMaps"] = [*values["engines"]["configMaps"], *retained]
        _private(context.directory / "values.json", _redacted(values))
        cluster.upgrade(chart_path(self.env), values)
        statefulset = cluster.get("statefulset", engine_set.STATEFULSET) or {}
        _private(context.directory / "statefulset.json", statefulset.get("status", {}))

    # engine-start

    def engine_start(self, context: StageContext) -> Outcome:
        role = context.instance
        cluster = self.cluster(context)
        launch = context.product("launch", role)
        previous = context.previous
        recorded = (previous or {}).get("products", {}).get("engine_identity") if previous else None
        pod = cluster.get("pod", role)
        statefulset = cluster.get("statefulset", engine_set.STATEFULSET) or {}
        revision = statefulset.get("status", {}).get("updateRevision", "")
        if pod is not None:
            current = pod.get("metadata", {}).get("labels", {}).get("controller-revision-hash")
            # A pod that started this plan before keeps running; a pod of an older template
            # that runs another plan is replaced.
            same = (
                recorded is not None
                and recorded.get("pod_uid") == pod["metadata"].get("uid")
                and recorded.get("plan_sha256") == launch["plan_sha256"]
            )
            node = pod.get("spec", {}).get("nodeName")
            placed = node in (None, "", self.node(role))
            if not placed or (not same and current != revision):
                why = f"on node {node}" if not placed else f"of revision {current}"
                self.announce(f"engine-start[{role}]: replacing pod {role} {why}")
                cluster.delete("pod", role)
                old = pod["metadata"].get("uid")
                pod = self._wait_pod(context, role, lambda p: p["metadata"].get("uid") != old)
        self._pin(cluster, self._wait_pod(context, role, lambda p: True), role)
        identity = None
        deadline = self.clock() + START_SECONDS
        while identity is None:
            pod = cluster.get("pod", role)
            if pod is not None:
                running = container_identity(pod, ENGINE_CONTAINER)
                status = _container(pod, ENGINE_CONTAINER) or {}
                if running is not None and status.get("ready"):
                    identity = running
                    break
                waiting = status.get("state", {}).get("waiting") or {}
                if waiting.get("reason") in (
                    "ErrImagePull",
                    "ImagePullBackOff",
                    "CreateContainerConfigError",
                ):
                    raise GateFailed(
                        self.node(role),
                        f"{role} start",
                        4,
                        waiting.get("message", waiting["reason"]),
                        context.directory / "logs",
                    )
            if self.clock() > deadline:
                raise GateFailed(
                    self.node(role),
                    f"{role} start",
                    124,
                    f"the engine container did not become ready within {START_SECONDS}s",
                    context.directory / "logs",
                )
            self.sleep(POLL_SECONDS)
        assert pod is not None
        node = pod.get("spec", {}).get("nodeName", "")
        _private(
            context.directory / "pod.json",
            {"metadata": pod["metadata"], "status": pod.get("status", {})},
        )
        if node != self.node(role):
            raise GateFailed(
                node or "cluster",
                f"{role} placement",
                1,
                f"the scheduler placed {role} on {node}, and discovery inspected "
                f"{self.node(role)}; free {self.node(role)} for it or set "
                f"NARWHAL_NODE_{_node(role)}_KUBERNETES_NODE to {node} and discover again",
                context.directory / "pod.json",
            )
        value = {
            "pod": role,
            "pod_uid": pod["metadata"]["uid"],
            "node": node,
            "plan_sha256": launch["plan_sha256"],
            **identity,
        }
        return Outcome({"engine_identity": value}, ({"name": role, "host": node, **value},))

    def _pin(self, cluster: Cluster, pod: Mapping[str, Any], role: str) -> None:
        """Pin a pod the chart holds at its scheduling gate to the node discovery measured its
        engine on, then release it to the scheduler.

        A gated pod accepts added node affinity, so each pod of the one StatefulSet lands on
        its own node. A pin an earlier attempt half applied, with the affinity already naming
        this node, only needs the gate removed.
        """
        spec = pod.get("spec", {})
        gates = [gate.get("name") for gate in spec.get("schedulingGates", []) or []]
        if PLACEMENT_GATE not in gates:
            return
        node = self.node(role)
        term = {"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}
        affinity = {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [term]}}
        present = (spec.get("affinity") or {}).get("nodeAffinity")
        if present is None:
            value = {**(spec.get("affinity") or {}), "nodeAffinity": affinity}
            operation = "replace" if "affinity" in spec else "add"
            cluster.patch(
                "pod", role, [{"op": operation, "path": "/spec/affinity", "value": value}]
            )
        elif present != affinity:
            raise ValueError(
                f"pod {role} has node affinity for another node than {node}; delete the pod so "
                "the StatefulSet makes a new one"
            )
        index = gates.index(PLACEMENT_GATE)
        path = f"/spec/schedulingGates/{index}"
        cluster.patch(
            "pod",
            role,
            [
                # The gate at this index must still be this one when the API applies the patch.
                {"op": "test", "path": f"{path}/name", "value": PLACEMENT_GATE},
                {"op": "remove", "path": path},
            ],
        )
        self.announce(f"engine-start[{role}]: pinned pod {role} to node {node}")

    def _wait_pod(
        self, context: StageContext, name: str, condition: Callable[[dict], bool]
    ) -> dict:
        cluster = self.cluster(context)
        deadline = self.clock() + START_SECONDS
        while True:
            pod = cluster.get("pod", name)
            if pod is not None and condition(pod):
                return pod
            if self.clock() > deadline:
                raise GateFailed(
                    "cluster", f"pod {name}", 124, "no new pod started", context.directory / "logs"
                )
            self.sleep(POLL_SECONDS)

    # engine-check

    def _pod_dir(self, role: str, uid: str) -> str:
        return f"{STATE}/engines/{role}/{uid}"

    def _launch_dir(self, role: str, uid: str) -> str:
        return f"{STATE}/engines/{role}/{LAUNCHES}/{uid}"

    def engine_check(self, context: StageContext) -> None:
        role = context.instance
        identity = context.product("engine_identity", role)
        uid = identity["pod_uid"]
        launch = self.recorded("engine-prepare", role) / "launch"
        files = {
            path.relative_to(launch).as_posix(): path.read_bytes()
            for path in sorted(launch.rglob("*"))
            if path.is_file()
        }
        target = shlex.quote(self._launch_dir(role, uid))
        evidence = shlex.quote(self._pod_dir(role, uid))
        # The check runs in a staging directory renamed into place once it passes, so the
        # sidecar, which waits for the directory, reads a checked plan.
        script = f"""target={target}
if test -e "$target/checked.json"; then
  test "$(sha256sum < /tmp/narwhal-in/launch.json)" = "$(sha256sum < "$target/launch.json")" ||
    {{ echo "narwhal-gate: $target holds another launch plan" >&2; exit 1; }}
else
  stage="$target.$$"
  mkdir -p "$(dirname "$target")"
  cp -R /tmp/narwhal-in "$stage"
  narwhal-engine check --run "$stage" --pod-evidence {evidence}
  mv "$stage" "$target"
fi
"""
        self.narwhal_gate(
            context,
            "engine-check",
            script,
            {"checked.json": f"{self._launch_dir(role, uid)}/checked.json"},
            files,
        )

    # cache-capture, cache-groups, fabric-budget and fabric-edge

    def _published(self, context: StageContext, role: str, into: str) -> None:
        """Copy the engine container's published attestation attempt into the attempt."""
        identity = context.product("engine_identity", role)
        home = f"{self._pod_dir(role, identity['pod_uid'])}/containers/{identity['container_id']}"
        script = (
            f"python -c {shlex.quote(_PUBLISHED)} {shlex.quote(home)} /tmp/narwhal-published "
            f"{PUBLISH_SECONDS}\n"
        )
        self.narwhal_gate(context, "state", script, {f"{into}/": "/tmp/narwhal-published"})

    def cache_capture(self, context: StageContext) -> Outcome:
        role = context.instance
        self._published(context, role, "published")
        source = context.directory / "published" / "cache-layout.json"
        if not source.is_file():
            raise ValueError(f"{role}: the sidecar's attestation holds no cache-layout.json")
        write_private(context.directory / "cache-layout.json", source.read_bytes())
        return Outcome({"cache_geometry": _digest(context.directory / "cache-layout.json")})

    def transfer_measure(self, context: StageContext) -> Outcome:
        """Measure one engine's KV handoffs to its decode peer, as the SSH target does, in a
        Job on the engine's node that runs the packaged measurement tool."""
        env = self.stage_env()
        package = self.package()
        engines, peers, prompt = fabric.measure_handoffs(
            self.hosts(), {"remote_dir": REMOTE_DIR}, package, env
        )
        role = context.instance
        engine = engines[role]
        node = engine.host.id
        files = {
            f"runs/deployment/.env.{role}": self.job_env(
                read_environment(package / node / f".env.{role}"), credential=False
            ),
            f"config/engine-launch.{role}.json": (
                package / node / f"engine-launch.{role}.json"
            ).read_bytes(),
        }
        backend = launcher(self.env.get("NARWHAL_ENGINE_BACKEND"))
        for helper in helpers(backend):
            files[f"{HELPER_DIRECTORY}/{helper.name}"] = (package / node / helper.name).read_bytes()
        shell = JobShell(self, context, kind="transfer-measure", files=files)
        digest = fabric.measure_transfer(
            engine,
            engines[peers[role]],
            cast(SSH, shell),
            REMOTE_DIR,
            prompt,
            context.directory / "transfer",
        )
        return Outcome({"handoff_geometry": digest})

    def cache_groups(self, context: StageContext) -> None:
        backend = launcher(self.env.get("NARWHAL_ENGINE_BACKEND"))
        layouts, transfers = {}, {}
        for role in self.engines():
            if cache_capture(backend) is not None:
                layouts[role] = context.outputs("cache-capture", role) / "cache-layout.json"
            if transfer_measurement(backend) is not None:
                transfers[role] = (
                    context.outputs("transfer-measure", role) / "transfer" / "transfer.json"
                )
        cache_groups.derive(
            self.recorded("discover") / "config" / "engine-launch.json",
            layouts,
            transfers,
            self.package(),
            context.directory,
        )

    def fabric_plan(self) -> fabric.Fabric:
        groups = self.recorded("cache-groups") / cache_groups.EVIDENCE
        cache = _digest(groups)
        if self._fabric is None or self._fabric[0] != cache:
            env = self.stage_env()
            plan = fabric.load(self.hosts(), {"remote_dir": "/tmp"}, self.package(), env, groups)
            self._fabric = (cache, plan)
        return self._fabric[1]

    def fabric_budget(self, context: StageContext) -> None:
        """Size one cache group's source budget from its representative's model configuration,
        launch record and captured cache layout."""
        plan = self.fabric_plan()
        group = next(group for group in plan.groups if group.name == context.instance)
        launch = plan.engines[group.name].launch.read_bytes()
        model = _model_config(self, group.name)
        layout = plan.layouts.get(group.name)
        transfer = plan.transfers.get(group.name, {})
        try:
            result = fabric.budget(
                model,
                launch,
                plan.workload,
                runtime_layout=layout,
                transfer=transfer.get("transfer.json"),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise fabric.FabricFailure(fabric.FABRIC_BUDGET, group.name, str(error)) from None
        result["cache_group"] = list(group.roles)
        directory = context.directory / "budget"
        directory.mkdir(mode=0o700)
        write_private(directory / "engine-launch.json", launch)
        write_private(directory / "model-config.json", model)
        if layout is not None:
            write_private(directory / "cache-layout.json", layout)
        for name, data in transfer.items():
            write_private(directory / name, data)
        _private(directory / "budget.json", result)

    def fabric_edge(self, context: StageContext) -> Outcome:
        plan = self.fabric_plan()
        edge = next(edge for edge in plan.edges() if edge.name == context.instance)
        budgets = {
            role: context.outputs("fabric-budget", plan.group(role).name) / "budget" / "budget.json"
            for role in plan.engines
        }
        retained = None
        if context.previous is not None:
            earlier = self.directory / context.previous["directory"] / "edge"
            retained = earlier if earlier.is_dir() else None
        # The fabric module runs host scripts through an SSH runner's `run`, as JobShell does.
        shell = cast(SSH, JobShell(self, context))
        results = fabric.qualify_edge(
            plan, shell, edge, budgets, context.directory / "edge", retained
        )
        return Outcome({"link": {result["sample"]: result["link_sha256"] for result in results}})

    # Attestation.

    def attest_capture(self, context: StageContext) -> Outcome:
        role = context.instance
        self._published(context, role, "attestation")
        self._representative_check(context, role)
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
        """The KV cache layout the representative of `role`'s cache group resolved, as the SSH
        target passes it to `capture --representative-layout`."""
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

    def _representative_check(self, context: StageContext, role: str) -> None:
        """Refuse an attestation whose live cache resolved another layout than its group's
        representative: the check an SSH capture runs before it writes the document. The
        sidecar captured on the pod, so the check reads the layout its attestation holds."""
        expected = self._representative_layout(context, role)
        if expected is None:
            return
        plan = _json(self.recorded("engine-prepare", role) / "launch" / "launch.json")
        engine = plan_launcher(plan)
        if not isinstance(engine, CacheRegistration):
            raise ValueError(
                f"{role}: {engine_backend(plan.get('engine')).label} engines register no cache "
                "layout to match a representative's"
            )
        source = context.directory / "attestation" / "cache-layout.json"
        layout = engine.cache_layout(source)
        if layout != expected:
            raise GateFailed(
                self.node(role),
                f"{role} attestation",
                1,
                f"Resolved layout {layout} differs from the representative's {expected}",
                source,
            )

    def sidecar_start(self, context: StageContext) -> Outcome:
        role = context.instance
        engine = context.product("engine_identity", role)
        cluster = self.cluster(context)
        deadline = self.clock() + START_SECONDS
        while True:
            pod = cluster.get("pod", role) or {}
            if pod.get("metadata", {}).get("uid") != engine["pod_uid"]:
                raise GateFailed(
                    engine["node"],
                    f"{role} sidecar",
                    4,
                    f"pod {role} is no longer the checked pod",
                    context.directory / "logs",
                )
            identity = container_identity(pod, SIDECAR_CONTAINER)
            if identity is not None and (_container(pod, SIDECAR_CONTAINER) or {}).get("ready"):
                break
            if self.clock() > deadline:
                raise GateFailed(
                    engine["node"],
                    f"{role} sidecar",
                    124,
                    "the attestation sidecar did not become ready",
                    context.directory / "logs",
                )
            self.sleep(POLL_SECONDS)
        value = {"pod": role, "pod_uid": engine["pod_uid"], "node": engine["node"], **identity}
        return Outcome(
            {"sidecar_identity": value},
            ({"name": f"{role}-sidecar", "host": engine["node"], **value},),
        )

    def sidecar_verify(self, context: StageContext) -> None:
        role = context.instance
        identity = context.product("engine_identity", role)
        launch = shlex.quote(self._launch_dir(role, identity["pod_uid"]))
        evidence = shlex.quote(self._pod_dir(role, identity["pod_uid"]))
        script = (
            "out=$(python -m narwhal.deployment.attestation_contract.cli verify "
            f"--run {launch} --pod-evidence {evidence})\n"
            "printf '%s\\n' \"$out\" >&2\n"
            "mkdir -p /tmp/narwhal-verify\n"
            'cp -R "${out##*captures in }"/. /tmp/narwhal-verify/\n'
        )
        self.narwhal_gate(
            context,
            "sidecar-verify",
            script,
            {"verify/": "/tmp/narwhal-verify"},
            service_account=True,
        )

    # The fleet: finalize, profile, calibrate, limits, preflight.

    def finalize_fleet(self, context: StageContext) -> None:
        work = self.state_dir(context)
        fleet = (self.package() / "fleet.json").read_bytes()
        self.narwhal_gate(
            context,
            "finalize-fleet",
            f"mkdir -p {work}\ninstall -m 600 /tmp/narwhal-in/fleet.json {work}/fleet.json\n"
            "python -m narwhal.deployment.attestation_contract.cli finalize-fleet "
            f"--fleet {work}/fleet.json\n",
            {"host/": work},
            {"fleet.json": fleet},
        )

    def profile(self, context: StageContext) -> None:
        values = settings(self.env)
        work = self.state_dir(context)
        fleet = _json(context.outputs("finalize-fleet") / "host" / "fleet.json")
        fleet["profiles"]["path"] = f"{work}/profiles.json"
        args = [
            "narwhal-profile",
            "--fleet",
            f"{work}/fleet.json",
            "--limits",
            f"{work}/profiling-limits.json",
            "--prefill-lens",
            values.prefill_lens,
            "--decode-input-lens",
            values.decode_input_lens,
            "--warmup-requests",
            str(values.warmup_requests),
        ]
        if values.decode_concurrency:
            args += ["--decode-concurrency", values.decode_concurrency]
        if transfer_measurement(launcher(self.env.get("NARWHAL_ENGINE_BACKEND"))) is not None:
            # The sweep switches each engine's peer; no router serves the engines yet.
            args.append("--no-router")
        self.narwhal_gate(
            context,
            "profile",
            f"mkdir -p {work}\ncp /tmp/narwhal-in/fleet.json /tmp/narwhal-in/profiling-limits.json "
            f"{work}/\n{shlex.join(args)}\n",
            {"host/": work},
            {
                "fleet.json": (json.dumps(fleet, indent=2) + "\n").encode(),
                "profiling-limits.json": (self.package() / "profiling-limits.json").read_bytes(),
            },
        )

    def calibrate(self, context: StageContext) -> None:
        values = settings(self.env)
        work = self.state_dir(context)
        source = context.outputs("profile") / "host" / "fleet.json"
        args = [
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
        self.narwhal_gate(
            context,
            "calibrate",
            f"mkdir -p {work}\ncp /tmp/narwhal-in/fleet.json {work}/\n{shlex.join(args)}\n",
            {"host/": work},
            {"fleet.json": source.read_bytes()},
        )

    def fleet_limits(self, context: StageContext) -> None:
        """Write the SLO targets, deadline and calibration into the routers' fleet file on the
        state volume."""
        values = settings(self.env)
        profiled = context.outputs("profile") / "host"
        calibrated = context.outputs("calibrate") / "host"
        artifact = _json(calibrated / "first-token-calibration.json")
        candidate = artifact.get("candidate_deadline_s")
        if not isinstance(candidate, (int, float)) or not math.isfinite(candidate):
            raise ValueError("the first-token calibration names no candidate deadline")
        work = self.state_dir(context)
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
        # Each router keeps its recovery state on its own volume.
        fleet.setdefault("recovery", {})["state_path"] = f"{ROUTER_HOME}/router-state.json"
        _private(context.directory / "fleet.json", fleet)
        files = {"fleet.json": (context.directory / "fleet.json").read_bytes()}
        for name, folder in (
            ("first-token-calibration.json", calibrated),
            ("profiles.json", profiled),
            ("profiles.samples.json", profiled),
        ):
            files[name] = (folder / name).read_bytes()
            write_private(context.directory / name, files[name])
        self.narwhal_gate(
            context,
            "state",
            f"mkdir -p {work}\ncp /tmp/narwhal-in/* {work}/\nchmod 644 {work}/*\n",
            {"state/fleet.json": f"{work}/fleet.json"},
            files,
        )
        self.announce(
            f"fleet-limits: first-token deadline {engine['first_token_timeout_s']}s from the "
            f"calibrated candidate {candidate}s"
        )

    def _limits(self, context: StageContext) -> str:
        """The fleet file fleet-limits wrote, as a path of the state volume."""
        relative = context.outputs("fleet-limits").relative_to(context.run_directory).as_posix()
        return f"deploy/{context.run_id}/{relative}/fleet.json"

    def preflight(self, context: StageContext) -> None:
        work = self.state_dir(context)
        self.narwhal_gate(
            context,
            "preflight",
            f"mkdir -p {work}\n"
            + shlex.join(
                [
                    "narwhal-check",
                    "--fleet",
                    f"{STATE}/{self._limits(context)}",
                    "--evidence-out",
                    f"{work}/preflight-evidence.json",
                ]
            )
            + "\n",
            {"host/": work},
        )

    # router-start

    def _routers(self, cluster: Cluster) -> dict[str, dict] | None:
        found = {}
        for name in ROUTERS:
            pod = cluster.get("pod", name)
            identity = container_identity(pod or {}, "router")
            if (
                pod is None
                or identity is None
                or not (_container(pod, "router") or {}).get("ready")
            ):
                return None
            found[name] = {"pod_uid": pod["metadata"]["uid"], **identity}
        return found

    def router_start(self, context: StageContext) -> Outcome:
        cluster = self.cluster(context)
        fleet_path = self._limits(context)
        fleet = context.outputs("fleet-limits") / "fleet.json"
        engines = {role: context.product("engine_identity", role) for role in self.engines()}
        state = {"fleet_sha256": _digest(fleet), "fleet": fleet_path, "engines": engines}
        previous = context.previous
        if previous is not None and previous["status"] == PASSED:
            kept = self._readmit(context, cluster, previous, state)
            if kept is not None:
                return kept
        values = self.values(cluster, fleet=fleet_path)
        _private(context.directory / "values.json", _redacted(values))
        cluster.upgrade(chart_path(self.env), values)
        deadline = self.clock() + ROUTER_SECONDS
        while (routers := self._routers(cluster)) is None:
            if self.clock() > deadline:
                raise GateFailed(
                    "cluster",
                    "router pair",
                    124,
                    "the router pods did not become ready",
                    context.directory / "logs",
                )
            self.sleep(POLL_SECONDS)
        _private(context.directory / "router.json", state)
        self._router_responses(context, completion=True)
        return Outcome(
            {"router_identity": routers},
            tuple({"name": name, "host": "cluster", **value} for name, value in routers.items()),
        )

    def _readmit(
        self,
        context: StageContext,
        cluster: Cluster,
        previous: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> Outcome | None:
        earlier = self.directory / previous["directory"] / "router.json"
        recorded = previous["products"].get("router_identity")
        if not earlier.is_file() or recorded is None:
            return None
        before = _json(earlier)
        if before["fleet_sha256"] != state["fleet_sha256"] or self._routers(cluster) != recorded:
            return None
        restarted = sorted(
            role
            for role, engine in state["engines"].items()
            if before["engines"].get(role) != engine
        )
        iids = [f"n{_node(role)}" for role in restarted]
        lifecycle = self.directory / previous["directory"] / "router" / "lifecycle.json"
        starts = _json(lifecycle).get("process_starts", {}) if lifecycle.is_file() else {}
        engines = [f"{iid}={starts[iid]!r}" if iid in starts else iid for iid in iids]
        self.announce(f"router-start: readmitting {', '.join(iids) or 'no engines'}")
        self._host_http(context, "readmit", "readmit", ROUTER_URL, "120", *engines)
        _private(context.directory / "router.json", state)
        self._router_responses(context, completion=False)
        return Outcome(
            {"router_identity": recorded},
            tuple({"name": name, "host": "cluster", **value} for name, value in recorded.items()),
        )

    def _host_http(self, context: StageContext, into: str, *args: str) -> None:
        command = ["python", "-m", "narwhal.deployment.host_http", *args]
        result = self.gate(context, Gate("router-check", context.stage, command, plain=True))
        remote.write(context.directory / into, remote.files(result.output))

    def _router_responses(self, context: StageContext, *, completion: bool) -> None:
        fleet = _json(context.outputs("fleet-limits") / "fleet.json")
        body = json.dumps({"model": fleet["model"], "prompt": _COMPLETION_PROMPT, "max_tokens": 32})
        self._host_http(
            context,
            "router",
            "responses",
            ROUTER_URL,
            *(["--completion", body] if completion else []),
        )

    # Live checks: each returns the recorded value while it still holds, and None once gone.

    def engine_running(self, instance: str, recorded: Any) -> Any:
        pod = self.cluster().get("pod", recorded["pod"])
        if pod is None or pod["metadata"].get("uid") != recorded["pod_uid"]:
            return None
        identity = container_identity(pod, ENGINE_CONTAINER)
        if identity is None or any(identity[k] != recorded[k] for k in identity):
            return None
        return recorded

    def sidecar_running(self, instance: str, recorded: Any) -> Any:
        pod = self.cluster().get("pod", recorded["pod"])
        if pod is None or pod["metadata"].get("uid") != recorded["pod_uid"]:
            return None
        identity = container_identity(pod, SIDECAR_CONTAINER)
        if identity is None or any(identity[k] != recorded[k] for k in identity):
            return None
        return recorded

    def routers_running(self, instance: str, recorded: Any) -> Any:
        return recorded if self._routers(self.cluster()) == recorded else None

    def link_now(self, instance: str, recorded: Any) -> Any:
        if self.dry:
            # Observing a route needs a Job on each node, which a dry pass must not start.
            return Unobserved(recorded)
        plan = self.fabric_plan()
        edge = next(edge for edge in plan.edges() if edge.name == instance)
        shell = JobShell(self, None)
        return {
            observation.sample.name: observation.fingerprint
            for observation in fabric.observe(plan, cast(SSH, shell), edge)
        }

    # down

    def down(self) -> list[dict[str, Any]]:
        """Remove the routers, engines and gate Jobs this run installed, keeping the volumes.

        Only a release this run installed is changed: the record must be a Kubernetes run of
        the same context, namespace and release, and the release must still run the engines
        and fleet this run gave it. Reports each process the run recorded, and whether it ran
        until now.
        """
        check_directory(self.directory)
        stopped = []
        with run_record.locked(self.directory):
            record = run_record.read(self.directory)
            if record["target"] != TARGET:
                raise ValueError(f"{self.directory} holds a {record['target']} run")
            try:
                access = _json(self.recorded("access") / "access.json")
            except ValueError:
                return []
            recorded_cluster = access.get("cluster", {})
            here = self.identity()
            if differing := sorted(k for k in here if recorded_cluster.get(k) != here[k]):
                raise ValueError(
                    f"the run deployed to another {', '.join(differing)} than the environment "
                    f"names: {recorded_cluster}; load the environment the run used"
                )
            cluster = self.cluster()
            current = cluster.release_values()
            self._owned(record, current)
            for entry in record["stages"].values():
                if entry["stage"] not in ("router-start", "sidecar-start", "engine-start"):
                    continue
                for process in entry["processes"]:
                    pod = cluster.get("pod", process.get("pod", process["name"]))
                    uid = process.get("pod_uid")
                    ran = pod is not None and (uid is None or pod["metadata"].get("uid") == uid)
                    host = process.get("host", "cluster")
                    stopped.append({"name": process["name"], "host": host, "stopped": ran})
            if current:
                values = self.values(cluster, engines={}, fleet="")
                cluster.upgrade(chart_path(self.env), values)
        return stopped

    def identity(self) -> dict[str, str]:
        """The cluster, namespace and release the environment names."""
        cluster = self.cluster()
        return {
            "context": cluster.context,
            "namespace": cluster.namespace,
            "release": cluster.release,
        }

    def _owned(self, record: Mapping[str, Any], current: Mapping[str, Any]) -> None:
        """Refuse a release that runs engines or a fleet another run gave it."""
        try:
            installed = _json(self.recorded("install") / "values.json")["engines"]
        except ValueError:
            installed = {}
        names = {item["metadata"]["name"] for item in installed.get("configMaps", [])}
        engines = current.get("engines") or {}
        live = {item["metadata"]["name"] for item in engines.get("configMaps", [])}
        if not live <= names:
            raise ValueError(
                "the release runs engines another run installed; down leaves them to that run"
            )
        routers = current.get("routers") or {}
        if routers.get("enabled") and not str(routers.get("fleet", "")).startswith(
            f"deploy/{record['run_id']}/"
        ):
            raise ValueError(
                f"the release's routers serve {routers.get('fleet')}, another run's fleet; down "
                "leaves them to that run"
            )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _redacted(values: Mapping[str, Any]) -> dict:
    """The chart values the attempt keeps: every ConfigMap's content by its digest only."""
    kept = copy.deepcopy(dict(values))
    for item in (kept.get("engines") or {}).get("configMaps", []):
        for field_name in ("data", "binaryData"):
            if field_name in item:
                item[field_name] = {
                    name: hashlib.sha256(str(value).encode()).hexdigest()
                    for name, value in item[field_name].items()
                }
    if kept.get("monitoring", {}).get("grafana", {}).get("dashboard"):
        kept["monitoring"]["grafana"]["dashboard"] = "<rendered>"
    return kept


def _model_config(deployment: KubernetesDeployment, role: str) -> bytes:
    """The model configuration discovery read from `role`'s model volume."""
    return (
        deployment.recorded("discover") / "inspection" / role / "model-config.json"
    ).read_bytes()


class JobShell:
    """The SSH runner's `run` over gate Jobs, for the fabric stages' host scripts.

    Each script runs in a Job on the host's node, on the host network, in the fabric image.
    `sample` runs a directed sample's server and client as their own Jobs.
    """

    def __init__(
        self,
        deployment: KubernetesDeployment,
        context: StageContext | None,
        *,
        kind: str = "fabric-probe",
        files: Mapping[str, bytes] | None = None,
    ) -> None:
        self.deployment = deployment
        self.context = context
        self.kind = kind
        self.files = dict(files or {})

    def _gate(self, host: Host, script: str) -> Gate:
        """A Job on the host's node and network that runs `script`, after unpacking its files
        where the script expects its host's deployment directory."""
        archive = {}
        if self.files:
            archive = {kubernetes_gate.INPUTS: kubernetes_gate.pack(self.files)}
            unpack = (
                "from pathlib import Path; from narwhal.deployment.kubernetes_gate import unpack; "
                f"unpack(Path('{INPUT}/{kubernetes_gate.INPUTS}'), Path('{REMOTE_DIR}'))"
            )
            script = f"python -c {shlex.quote(unpack)}\n" + script
        return Gate(
            self.kind,
            f"{host.id}-{self.kind}",
            ["sh", "-c", script],
            node=host.id,
            files=archive,
            host_network=True,
            plain=True,
        )

    def _context(self) -> StageContext:
        if self.context is None:
            raise ValueError("a live fabric check runs Jobs only within a stage")
        return self.context

    def run(
        self,
        host: Host,
        stage: str,
        operation: str,
        script: str,
        payload: Path | None = None,
        reason: str | None = None,
    ) -> str:
        del stage, operation, payload, reason
        context = self.context
        if context is None:
            # A probe Job outside a stage, for `status`: its log lands beside the run record.
            context = cast(StageContext, _LiveContext(self.deployment.directory))
        return self.deployment.gate(context, self._gate(host, script)).output

    def sample(self, plan: fabric.Fabric, where: str, sample: fabric.Sample) -> bytes:
        context = self._context()
        deployment = self.deployment
        for host in (sample.source.host, sample.destination.host):
            fabric._listeners(plan, cast(SSH, self), where, host)
        server, client = fabric._client_argv(plan, sample)
        port = plan.test_port
        window = (
            fabric.SERVER_WINDOW_S
            + fabric.TCP_OMIT_S
            + max(fabric.TCP_DURATION_S, fabric.RDMA_DURATION_S)
        )
        server_script = f"""timeout -k 5 {window} {shlex.join(server)} &
pid=$!
until ss -Hltn "sport = :{port}" | grep -q .; do
  kill -0 "$pid" 2> /dev/null ||
    {{ echo "narwhal-host: the {server[0]} server exited" >&2; exit 1; }}
  sleep 0.1
done
echo narwhal-listening
wait "$pid"
"""
        client_script = f"""status=0
{shlex.join(client)} > /tmp/narwhal-report 2> /tmp/narwhal-client.err || status=$?
cat /tmp/narwhal-client.err >&2
printf 'narwhal-file report %s\\n' "$(base64 -w 0 < /tmp/narwhal-report)"
test "$status" -eq 0 ||
  {{ echo "narwhal-host: the {client[0]} client exited with status $status" >&2; exit 1; }}
"""
        server_gate = Gate(
            "fabric-server",
            f"{sample.name}-server",
            _shell(server_script),
            node=sample.destination.host.id,
            host_network=True,
            plain=True,
        )
        client_gate = Gate(
            "fabric-client",
            f"{sample.name}-client",
            _shell(client_script),
            node=sample.source.host.id,
            host_network=True,
            plain=True,
        )
        [server_entry] = deployment.start_gates(context, [server_gate])
        cluster = deployment.cluster(context)
        deadline = deployment.clock() + JOB_SECONDS["fabric-server"]
        while True:
            try:
                if "narwhal-listening" in cluster.logs(str(server_entry["name"])):
                    break
            except ClusterError:
                pass
            job = cluster.get("job", str(server_entry["name"])) or {}
            if job.get("status", {}).get("failed") or deployment.clock() > deadline:
                deployment.finish_gate(context, server_gate, server_entry, where)
                raise GateFailed(
                    sample.destination.host.id,
                    f"{where} {server[0]} server",
                    4,
                    "the server did not listen",
                    context.directory / "logs",
                )
            deployment.sleep(POLL_SECONDS)
        [client_entry] = deployment.start_gates(context, [client_gate], [server_entry])
        try:
            result = deployment.finish_gate(context, client_gate, client_entry, where)
        finally:
            deployment.finish_gate(context, server_gate, server_entry, where)
        report = remote.files(result.output).get("report")
        if report is None:
            raise GateOutputError(f"{where}: the {client[0]} client's log holds no report")
        return report + b"\n"


@dataclass
class _LiveContext:
    """The parts of a stage context a live probe's Job uses."""

    run_directory: Path
    stage: str = "live"
    instance: str = ""
    attempt: int = 0

    def __post_init__(self) -> None:
        self.directory = self.run_directory / "live"
        self.run_id = str(run_record.read(self.run_directory)["run_id"])


class Topology:
    """The deployment's instances, from the inputs and the outputs of passed stages."""

    def __init__(self, deployment: KubernetesDeployment) -> None:
        self.deployment = deployment

    def instances(self, scope: str) -> Sequence[str]:
        if scope == ENGINE:
            return self.deployment.engines()
        if scope == GROUP:
            path = self.deployment.recorded("cache-groups") / cache_groups.EVIDENCE
            return [group["representative"] for group in _json(path)["groups"]]
        if scope == EDGE:
            return [edge.name for edge in self.deployment.fabric_plan().edges()]
        raise ValueError(f"the Kubernetes topology has no scope {scope}")

    def capable(self, capability: str, instance: str) -> bool:
        backend = launcher(self.deployment.env.get("NARWHAL_ENGINE_BACKEND"))
        if capability == CACHE_CAPTURE:
            return cache_capture(backend) is not None
        if capability == TRANSFER_MEASUREMENT:
            return transfer_measurement(backend) is not None
        raise ValueError(f"the Kubernetes topology has no capability {capability}")

    def related(self, relation: str, instance: str) -> Sequence[str]:
        path = self.deployment.recorded("cache-groups") / cache_groups.EVIDENCE
        group = next(g for g in _json(path)["groups"] if instance in g["roles"])
        if relation == "group":
            return [group["representative"]]
        if relation == "group_edges":
            nodes = {self.deployment.node(role) for role in group["roles"]}
            return [
                edge.name
                for edge in self.deployment.fabric_plan().edges()
                if edge.source.id in nodes
            ]
        raise ValueError(f"the Kubernetes topology has no relation {relation}")


# The name `narwhal deploy` uses for each target's deployment.
Deployment = KubernetesDeployment
