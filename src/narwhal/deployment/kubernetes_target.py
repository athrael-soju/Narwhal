"""`narwhal deploy --target kubernetes`: the stage graph's actions, live checks and topology on
a Kubernetes cluster, for one `NarwhalFleet`.

The actions reach the cluster through a `Cluster`: `ApiCluster` over the API client in the
fleet controller, or `KubectlCluster` from a workstation. Every object they make is built by
`kubernetes_objects` with the fleet as its controlling owner and applied server-side, so
deleting the fleet leaves the cleanup to the garbage collector.

Every stage that checks something in the cluster runs it as a gate Job. A Job's name follows
from the fleet's uid, the stage instance, the attempt and the Job's place in the attempt, and
the run record names it before the Job is created. A replayed attempt therefore meets each Job
it had started under the same name: the create conflicts, and the action adopts the Job, waits
for it and reads its log. A Job's inputs travel in a ConfigMap; its outputs come back in its
log, as `kubernetes_gate` prints them, and land in the stage's attempt directory, which stays
on the machine that runs the stage. Jobs that check the engines read and write the state
volume, where the engine pods keep their records. Every Job delete propagates in the
background, so the Job's pods go with it.

A Job or pod that cannot run fails its stage at once: a container whose configuration the
kubelet cannot build, a pod the kubelet refuses at admission, and an engine or router pod no
node can schedule.

The engine API key never reaches the runner. The fleet names the Secret that holds it, and the
pods and Jobs that call the engines read it from there.

The fleet holds no router until `router-start`, so the engines and Jobs run first. An engine
pod's launch directory lives at `engines/<role>/launches/<pod UID>` on the state volume, where
its attestation sidecar waits for it.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import json
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from narwhal import provenance
from narwhal.contracts import canonical_digest
from narwhal.engines.attestation import AttestationDocument, launch_digest
from narwhal.runtime import kubernetes_api
from narwhal.runtime.kubernetes_api import (
    CONFIG_MAP,
    DEPLOYMENT,
    JOB,
    NODE,
    POD,
    SERVICE,
    STATEFUL_SET,
    ApiClient,
    Conflict,
    Kind,
    KubernetesError,
    LogTooLarge,
    NotFound,
    OutcomeUnknown,
    Unavailable,
)

from . import (
    cache_groups,
    discovery,
    fabric,
    fleet_resource,
    install,
    kubernetes_gate,
    kubernetes_objects,
    remote,
    run_record,
)
from .deploy import InputError, settings
from .deploy import recovery_url as recovery_url
from .deploy_graph import CACHE_CAPTURE, EDGE, ENGINE, GROUP, TRANSFER_MEASUREMENT
from .engine_launch import load_launches
from .fleet_resource import (
    FABRIC_IMAGE_ENV,
    FLEET_UID_ENV,
    HAPROXY_ENV,
    HAPROXY_IMAGE_ENV,
    ROUTERS_ENV,
    VolumePolicy,
    check_model_volume,
    spec_env,
    variable_fact,
)
from .graph import INPUT as INPUT_FACT
from .hosts import HOST_CAUSE, SSH, Host, write_private
from .kubernetes_graph import KUBERNETES_GRAPH
from .kubernetes_objects import GateJob, Owner, controlled
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
from .launch_engine.kubernetes import (
    API_KEY_SECRET_ENV,
    LAUNCHES,
    SERVICE_ACCOUNT_ENV,
    SIDECAR_IMAGE_ENV,
    SIDECAR_USER_ENV,
    api_key_secret,
    sidecar_user,
)
from .launch_engine.plan import API_KEY_ENV, conv_state_layers
from .role_env import HELPER_DIRECTORY, helpers, read_environment, select_values, write_environment
from .run_record import PASSED, key
from .runner import (
    Cancelled,
    Input,
    Interrupted,
    Outcome,
    Runner,
    RunResult,
    StageContext,
    Stop,
    Unobserved,
    check_directory,
)

TARGET = "kubernetes"
LAST_STAGE = "router-start"

NAMESPACE = fleet_resource.NAMESPACE_ENV
FLEET_UID = FLEET_UID_ENV
CONTEXT = "NARWHAL_KUBERNETES_CONTEXT"
KUBECTL = "NARWHAL_KUBECTL"
STATE_CLAIM = "NARWHAL_KUBERNETES_STATE_CLAIM"
GPU_RESOURCE = "NARWHAL_KUBERNETES_GPU_RESOURCE"
RDMA_RESOURCE = "NARWHAL_KUBERNETES_RDMA_RESOURCE"
MODEL_VOLUME = "NARWHAL_KUBERNETES_MODEL_VOLUME"
RUNTIME_CLASS = "NARWHAL_KUBERNETES_RUNTIME_CLASS"
# The engines' ServiceAccount, which the chart makes as `<release>-engine` and names in the
# controller's settings; no fleet sets it.
ENGINE_ACCOUNT = SERVICE_ACCOUNT_ENV
# The user the engine pods' attestation sidecars run as, from the controller's settings.
SIDECAR_USER = SIDECAR_USER_ENV
# How the workstation reaches the cluster; the controller in the cluster needs neither.
WORKSTATION_INPUTS = (CONTEXT, KUBECTL)
# The inputs `narwhal-engine prepare --backend kubernetes` reads from its Job's role file.
POD_INPUTS = (
    GPU_RESOURCE,
    RDMA_RESOURCE,
    MODEL_VOLUME,
    STATE_CLAIM,
    RUNTIME_CLASS,
    API_KEY_SECRET_ENV,
    ENGINE_ACCOUNT,
    SIDECAR_IMAGE_ENV,
    SIDECAR_USER,
)
# The engine node of engine role n.
NODE_NAME = re.compile(r"NARWHAL_NODE_([1-9][0-9]*)_KUBERNETES_NODE")
DNS_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# The fleet, as the API serves it.
FLEET = Kind(
    fleet_resource.GROUP, fleet_resource.VERSION, fleet_resource.KIND, fleet_resource.PLURAL
)

# Where the cluster's containers see things.
STATE = "/narwhal-state"
INPUT = "/narwhal-input"
MODEL = "/model"
ROUTER_HOME = "/narwhal-router"
ROUTERS = kubernetes_objects.ROUTER_PODS
# The first router pod, by its name in the routers' headless Service of the fleet's namespace.
# HAProxy forwards only the serving routes, so a router's own address stands for the router.
ROUTER_URL = (
    f"http://{ROUTERS[0]}.{kubernetes_objects.ROUTER_SERVICE}:{kubernetes_objects.ROUTER_PORT}"
)
ROUTER_COMPONENT = "router"
# Where a host-script Job unpacks its inputs, as an SSH host's deployment directory.
REMOTE_DIR = "/tmp/narwhal-in"
ENGINE_CONTAINER = "engine"
SIDECAR_CONTAINER = "attestation"
GATE_CONTAINER = kubernetes_objects.GATE
# The scheduling gate each engine pod waits at until engine-start pins it to its node.
PLACEMENT_GATE = fleet_resource.PLACEMENT_GATE
# The label the Job controller gives each pod of a Job.
JOB_NAME_LABEL = "batch.kubernetes.io/job-name"

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
    "key-check": 300,
}
GRACE_SECONDS = 120
POLL_SECONDS = 2.0
START_SECONDS = 1800
ROUTER_SECONDS = 600
# A router pod may wait this long for a node, as when a cluster autoscaler adds one.
ROUTER_UNSCHEDULABLE_SECONDS = 300
# How long a Job being deleted may take to go before its name is free again.
GONE_SECONDS = 120
# Reads of a finished Job's log, and the longest wait between reads the API refused.
LOG_TRIES = 5
BACKOFF_SECONDS = 30.0
PUBLISH_SECONDS = 900
# A gate's log is read whole; the kubelet rotates a container log at 10 MiB by default.
LOG_BYTES = kubernetes_api.LOG_MAX_BYTES
# A write whose outcome is unknown is read back, and made again at most this many times.
WRITE_TRIES = 3

# A container that waits for one of these never starts by itself.
# CreateContainerError is left out: the runtime gives it for passing causes too.
NEVER_STARTS = ("CreateContainerConfigError", "InvalidImageName")
# An engine pod that waits for its image this way runs no other image.
IMAGE_FAILURES = ("ErrImagePull", "ImagePullBackOff")

_COMPLETION_PROMPT = "Return one sentence about narwhals."
_FABRIC_TEST = re.compile(r"NARWHAL_(FABRIC_TEST_PORT|(NODE_[1-9][0-9]*_)?RDMA_.*)")
_NOT_INPUTS = re.compile(
    r"NARWHAL_(HOSTS|FLEET|LAUNCH_CONFIG|STAGE_.*_TIMEOUT_SECONDS|STAGE_TIMEOUT_SECONDS"
    r"|STAGE_CLEANUP_GRACE_SECONDS|STAGE_KILL_GRACE_SECONDS|TRIAL_.*)"
)
_ROUTER_INPUTS = ("NARWHAL_ROUTER_URL",)
# The Helm inputs the target read before the NarwhalFleet held its settings.
RETIRED = (
    "NARWHAL_KUBERNETES_VALUES",
    "NARWHAL_KUBERNETES_CHART",
    "NARWHAL_KUBERNETES_RELEASE",
    "NARWHAL_HELM",
)

# The key check: its Job reports the HMAC of the engine API key under the fleet's salt, which
# the run directory keeps in CREDENTIAL_FILE with the last HMAC.
KEY_CHECK = "key-check"
CREDENTIAL_FILE = "credential.json"
HEX64 = re.compile(r"[0-9a-f]{64}")
_KEY_HMAC = f"""import hashlib, hmac, json, os
salt = bytes.fromhex(open('/tmp/narwhal-in/salt').read().strip())
key = os.environ[{API_KEY_ENV!r}].encode()
digest = hmac.new(salt, key, hashlib.sha256).hexdigest()
with open('/tmp/narwhal-key.json', 'w') as out:
    json.dump({{"hmac_sha256": digest}}, out)
"""

# The digest of a gate Job's spec and inputs, which adoption matches exactly.
SPEC_ANNOTATION = fleet_resource.qualified("spec-sha256")

# The time functions the deployment waits with.
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


# A failed request to the cluster: the API's own errors, or a kubectl that failed.
ClusterError = KubernetesError


class GateFailed(remote.StepFailed):
    """A gate Job failed. Exit status 1 is its check's rejection, as on an SSH host."""


class GateOutputError(ValueError):
    """A gate Job's log holds no complete set of returned files."""


# What the runner may do in the fleet's namespace, and in the cluster: each verb a `Cluster`
# call below sends, as a Role's rules name them. The narwhal chart grants the controller these.
RULES: tuple[dict[str, Any], ...] = (
    {"apiGroups": [fleet_resource.GROUP], "resources": [FLEET.plural], "verbs": ["get"]},
    {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "list", "create", "delete"]},
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "patch", "delete"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    {
        "apiGroups": [""],
        "resources": ["configmaps", "services"],
        "verbs": ["get", "list", "create", "patch", "delete"],
    },
    {
        "apiGroups": ["apps"],
        "resources": ["statefulsets", "deployments"],
        "verbs": ["get", "list", "create", "patch", "delete"],
    },
)
CLUSTER_RULES: tuple[dict[str, Any], ...] = (
    {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get"]},
)
# The RBAC verbs each `Cluster` method needs. A server-side apply that makes an object needs
# `create` as well as `patch`.
VERBS = {
    "get": ("get",),
    "list": ("list",),
    "create": ("create",),
    "apply": ("create", "patch"),
    "patch": ("patch",),
    "delete": ("delete",),
    "log": ("get",),
}


# The cluster.


class Cluster(Protocol):
    """The API verbs the actions use, on plain dictionaries, in the fleet's namespace.

    `get` returns None for an object that does not exist, and `delete` False. A write raises
    `Conflict` for a name that exists and `OutcomeUnknown` when it may have taken effect;
    the caller reads the object again before it decides.
    """

    namespace: str

    def get(self, kind: Kind, name: str) -> dict[str, Any] | None: ...

    def list(self, kind: Kind, labels: Mapping[str, str] | None = None) -> list[dict[str, Any]]: ...

    def create(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]: ...

    def apply(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]: ...

    def patch(
        self, kind: Kind, name: str, operations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]: ...

    def delete(self, kind: Kind, name: str, *, uid: str | None = None) -> bool: ...

    def log(self, pod: str, container: str) -> bytes: ...


def _typed(kind: Kind, items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """List items with their apiVersion and kind, which the API leaves off them."""
    return [{"apiVersion": kind.api_version, "kind": kind.kind, **dict(item)} for item in items]


class ApiCluster:
    """A `Cluster` over the API client, as the fleet controller reaches its own namespace.

    Applies take every field they set, as `kubernetes_objects.MANAGER`, and every delete
    propagates in the background.
    """

    def __init__(self, client: ApiClient) -> None:
        self.client = client
        self.namespace = client.namespace

    def get(self, kind: Kind, name: str) -> dict[str, Any] | None:
        return self.client.get(kind, name)

    def list(self, kind: Kind, labels: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        return _typed(kind, self.client.list(kind, labels=labels).items)

    def create(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]:
        return self.client.create(kind, obj)

    def apply(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]:
        return self.client.apply(kind, obj, force=True)

    def patch(
        self, kind: Kind, name: str, operations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return self.client.patch(kind, name, operations)

    def delete(self, kind: Kind, name: str, *, uid: str | None = None) -> bool:
        return self.client.delete(kind, name, propagation=kubernetes_objects.BACKGROUND, uid=uid)

    def log(self, pod: str, container: str) -> bytes:
        return self.client.log(pod, container=container, limit_bytes=LOG_BYTES)


def in_cluster(env: Mapping[str, str], logs: Path) -> Cluster:
    """The cluster as the pod this runs in reaches it, through its ServiceAccount."""
    del env, logs
    connection = kubernetes_api.in_cluster()
    return ApiCluster(ApiClient(connection, field_manager=kubernetes_objects.MANAGER))


class KubectlCluster:
    """A `Cluster` through `kubectl` on a workstation, each call logged under `logs`."""

    def __init__(self, env: Mapping[str, str], logs: Path) -> None:
        self.namespace = env.get(NAMESPACE, "")
        self.context = env.get(CONTEXT, "")
        self.kubectl_path = env.get(KUBECTL) or "kubectl"
        self.log_dir = logs
        self._count = 0

    def _run(self, argv: list[str], data: str | None, timeout: float, *, write: bool) -> str:
        self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._count += 1
        log = self.log_dir / f"{self._count:03d}-kubectl-{argv[1]}.log"
        try:
            # kubectl reads standard input only for a write; it never reads the runner's.
            result = subprocess.run(
                argv,
                input=data,
                stdin=None if data is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            write_private(log, f"$ {shlex.join(argv)}\ntimed out after {timeout:g}s\n".encode())
            raise _kubectl_error(argv, 124, "timed out", log, write=write) from None
        # Logs keep the command and its standard error; standard output is the caller's.
        write_private(
            log, f"$ {shlex.join(argv)}\nexit {result.returncode}\n{result.stderr}".encode()
        )
        if result.returncode:
            raise _kubectl_error(
                argv, result.returncode, result.stderr or result.stdout, log, write=write
            )
        return result.stdout

    def kubectl(
        self, *args: str, data: str | None = None, timeout: float = 120, write: bool = False
    ) -> str:
        context = ["--context", self.context] if self.context else []
        argv = [self.kubectl_path, *args[:1], *context, "--namespace", self.namespace, *args[1:]]
        return self._run(argv, data, timeout, write=write)

    @staticmethod
    def _resource(kind: Kind) -> str:
        return f"{kind.plural}.{kind.group}" if kind.group else kind.plural

    def get(self, kind: Kind, name: str) -> dict[str, Any] | None:
        output = self.kubectl(
            "get", self._resource(kind), name, "--ignore-not-found", "--output", "json"
        )
        return json.loads(output) if output.strip() else None

    def list(self, kind: Kind, labels: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        selector = ",".join(f"{name}={value}" for name, value in sorted((labels or {}).items()))
        args = ["get", self._resource(kind), "--output", "json"]
        if selector:
            args += ["--selector", selector]
        return _typed(kind, json.loads(self.kubectl(*args))["items"])

    def create(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]:
        output = self.kubectl(
            "create", "--filename", "-", "--output", "json", data=json.dumps(obj), write=True
        )
        return dict(json.loads(output))

    def apply(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]:
        output = self.kubectl(
            "apply",
            "--server-side",
            "--force-conflicts",
            f"--field-manager={kubernetes_objects.MANAGER}",
            "--filename",
            "-",
            "--output",
            "json",
            data=json.dumps(obj),
            write=True,
        )
        return dict(json.loads(output))

    def patch(
        self, kind: Kind, name: str, operations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        output = self.kubectl(
            "patch",
            self._resource(kind),
            name,
            "--type",
            "json",
            "--patch",
            json.dumps(list(operations)),
            "--output",
            "json",
            write=True,
        )
        return dict(json.loads(output))

    def delete(self, kind: Kind, name: str, *, uid: str | None = None) -> bool:
        if uid is not None:
            # kubectl names no precondition, so the delete goes to the object's own path with
            # DeleteOptions that hold the uid.
            options = {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Background",
                "preconditions": {"uid": uid},
            }
            try:
                self.kubectl(
                    "delete",
                    "--raw",
                    f"{kind.path(self.namespace)}/{name}",
                    "-f",
                    "-",
                    data=json.dumps(options),
                    write=True,
                )
            except NotFound:
                return False
            return True
        output = self.kubectl(
            "delete",
            self._resource(kind),
            name,
            "--ignore-not-found",
            "--wait=false",
            "--cascade=background",
            write=True,
        )
        return bool(output.strip())

    def log(self, pod: str, container: str) -> bytes:
        output = self.kubectl(
            "logs", f"pod/{pod}", "--container", container, f"--limit-bytes={LOG_BYTES + 1}"
        )
        data = output.encode()
        if len(data) > LOG_BYTES:
            raise LogTooLarge(0, "LogTooLarge", f"pod {pod} logged more than {LOG_BYTES} bytes")
        return data


def _kubectl_error(
    argv: Sequence[str], status: int, cause: str, log: Path, *, write: bool
) -> KubernetesError:
    """The API error a failed kubectl reports, by the reason it prints."""
    detail = cause.strip().splitlines()[-1] if cause.strip() else "no output"
    message = f"{shlex.join(argv[:3])} exited {status}: {detail}; inspect {log}"
    if "(AlreadyExists)" in cause or "(Conflict)" in cause:
        return Conflict(409, "Conflict", message)
    if "(NotFound)" in cause:
        return NotFound(404, "NotFound", message)
    lost = status == 124 or any(
        text in cause for text in ("Unable to connect", "i/o timeout", "connection refused")
    )
    if lost:
        if write:
            return OutcomeUnknown(0, "OutcomeUnknown", f"{message}; read the object again")
        return Unavailable(0, "Unavailable", message)
    return KubernetesError(status, "KubectlFailed", message)


# The kinds of the objects the fleet owns, by name.
KINDS = {kind.kind: kind for kind in (CONFIG_MAP, SERVICE, STATEFUL_SET, DEPLOYMENT, JOB, POD)}
# What `stop` deletes, and the order of the components it deletes.
STOP_KINDS = (DEPLOYMENT, STATEFUL_SET, JOB, SERVICE, CONFIG_MAP)
STOP_ORDER = (
    ROUTER_COMPONENT,
    kubernetes_objects.HAPROXY,
    kubernetes_objects.ENGINE,
    kubernetes_objects.GATE,
)


def kind_of(document: Mapping[str, Any]) -> Kind:
    """The kind of an object `kubernetes_objects` built."""
    found = KINDS.get(str(document.get("kind")))
    if found is None or document.get("apiVersion") != found.api_version:
        raise ValueError(f"{document.get('apiVersion')} {document.get('kind')} is no fleet object")
    return found


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


def _conditions(obj: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {c.get("type"): c for c in (obj.get("status") or {}).get("conditions") or []}


def never_runs(pod: Mapping[str, Any], *, scheduled: bool) -> str | None:
    """Why a pod will never run by itself, or None: a container whose configuration the
    kubelet cannot build, a pod the kubelet refused at admission, such as `OutOfnvidia.com/gpu`
    on a node a Job is pinned to, or, with `scheduled`, a pod no node can schedule."""
    status = pod.get("status") or {}
    if status.get("phase") == "Failed" and status.get("reason"):
        node = (pod.get("spec") or {}).get("nodeName") or "its node"
        return f"the kubelet on {node} refused the pod: {status['reason']}: " + str(
            status.get("message") or "no message"
        )
    containers = [
        *(status.get("initContainerStatuses") or []),
        *(status.get("containerStatuses") or []),
    ]
    for container in containers:
        waiting = (container.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in NEVER_STARTS:
            return f"container {container.get('name')}: {waiting['reason']}: " + str(
                waiting.get("message") or "no message"
            )
    if scheduled and (cause := unschedulable(pod)):
        return cause
    return None


def unschedulable(pod: Mapping[str, Any]) -> str | None:
    """Why the scheduler finds no node for the pod, or None. A pod that waits for its
    PersistentVolumeClaims to bind is not unschedulable: the claim binds by itself."""
    placed = _conditions(pod).get("PodScheduled") or {}
    if placed.get("status") != "False" or placed.get("reason") != "Unschedulable":
        return None
    message = str(placed.get("message") or "Unschedulable")
    if "unbound" in message and "PersistentVolumeClaim" in message:
        return None
    return f"no node can schedule the pod: {message}"


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
        if (match := NODE_NAME.fullmatch(name))
    }
    return dict(sorted(found.items(), key=lambda item: _node(item[0])))


def target_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment as the cluster's containers see it: the model at /model, and the first
    router pod's address unless the router URL is set."""
    return {
        **env,
        "NARWHAL_MODEL_DIR": MODEL,
        "NARWHAL_RUN_DIR": env.get("NARWHAL_RUN_DIR") or STATE,
        "NARWHAL_ROUTER_URL": env.get("NARWHAL_ROUTER_URL") or ROUTER_URL,
    }


def _object(env: Mapping[str, str], name: str) -> dict[str, Any]:
    """A variable that holds one JSON object, as `spec_env` writes a fleet's blocks."""
    try:
        value = json.loads(env.get(name) or "{}")
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must hold one JSON object")
    return value


def fact_of(name: str) -> str | None:
    """The input fact a variable belongs to, or None for a variable that is no input."""
    if not name.startswith("NARWHAL_") or _NOT_INPUTS.fullmatch(name):
        return None
    if name in WORKSTATION_INPUTS:
        return "cluster"
    if name in _ROUTER_INPUTS:
        return "router"
    if _FABRIC_TEST.fullmatch(name):
        return "fabric_test"
    return variable_fact(name)


def settings_env(fleet: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, str]:
    """The variables `inputs()` reads, from a NarwhalFleet and the controller's settings.

    The narwhal chart writes the settings to controller.json: `stateClaim`, the engines'
    ServiceAccount in `engineServiceAccount`, the sidecars' user in `sidecarUser` (the launch's
    default when unset) and the cluster admin's model volume allow-lists.
    """
    policy = VolumePolicy.from_values(settings)
    env = spec_env(fleet, state_claim=str(settings["stateClaim"]), policy=policy)
    env[ENGINE_ACCOUNT] = str(settings["engineServiceAccount"])
    if "sidecarUser" in settings:
        env[SIDECAR_USER] = str(settings["sidecarUser"])
    return env


def _check_model_volume(env: Mapping[str, str], policy: VolumePolicy) -> None:
    source = json.loads(env[MODEL_VOLUME])
    check_model_volume({"spec": {"engines": {"modelVolume": source}}}, policy)


def inputs(env: Mapping[str, str], *, policy: VolumePolicy | None = None) -> dict[str, Input]:
    """The digest of every input fact of the Kubernetes graph, after checking each input.

    `policy` is the cluster admin's allow-list of model volumes, the narwhal chart's by default.
    Raises `InputError` naming every missing or malformed input, before the cluster is touched.
    """
    policy = policy or VolumePolicy()
    problems: list[str] = []
    env = target_env(env)
    try:
        settings(env)
    except InputError as error:
        problems.extend(error.problems)
    if not DNS_LABEL.fullmatch(env.get(NAMESPACE, "")):
        problems.append(f"set {NAMESPACE} to the namespace the fleet runs in")
    problems.extend(
        f"unset {name}; the NarwhalFleet now holds the settings it gave"
        for name in RETIRED
        if name in env
    )
    if not UID.fullmatch(env.get(FLEET_UID, "")):
        problems.append(f"set {FLEET_UID} to the uid of the NarwhalFleet this run deploys")
    problems.extend(
        f"set {name} to the {what} image by registry digest"
        for name, what in (
            (SIDECAR_IMAGE_ENV, "Narwhal"),
            (FABRIC_IMAGE_ENV, "fabric"),
            (HAPROXY_IMAGE_ENV, "HAProxy"),
        )
        if not DIGEST.fullmatch(env.get(name, ""))
    )
    if not DNS_NAME.fullmatch(env.get(ENGINE_ACCOUNT, "")):
        problems.append(
            f"set {ENGINE_ACCOUNT} to the engines' ServiceAccount, which the chart makes"
        )
    for name in (ROUTERS_ENV, HAPROXY_ENV):
        try:
            block = _object(env, name)
        except ValueError as error:
            problems.append(str(error))
            continue
        if name == ROUTERS_ENV and not {"storageClassName", "size"} <= set(block):
            problems.append(f"set {ROUTERS_ENV} to the routers block, with its storage")
    for check in (api_key_secret, sidecar_user):
        try:
            check(env)
        except ValueError as error:
            problems.append(str(error))
    if env.get(API_KEY_ENV):
        problems.append(
            f"{API_KEY_ENV} holds the engine API key; the fleet reads it from the Secret "
            f"{API_KEY_SECRET_ENV} names, so unset it"
        )
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
        for name in (GPU_RESOURCE, RDMA_RESOURCE, MODEL_VOLUME, STATE_CLAIM)
        if not env.get(name)
    )
    if env.get(MODEL_VOLUME):
        try:
            _check_model_volume(env, policy)
        except ValueError as error:
            problems.append(f"{MODEL_VOLUME}: {error}")
    try:
        fabric.inputs(env)
    except ValueError as error:
        problems.append(str(error))
    if not env.get("NARWHAL_ENGINE_MODEL_NAME"):
        problems.append("set NARWHAL_ENGINE_MODEL_NAME to the served model name")
    if problems:
        raise InputError(list(dict.fromkeys(problems)))
    facts: dict[str, dict[str, str]] = {
        name: {} for name, fact in KUBERNETES_GRAPH.facts.items() if fact.kind == INPUT_FACT
    }
    for name, value in env.items():
        fact = fact_of(name)
        if fact is not None:
            facts[fact][name] = value
    facts["revision"] = {"source": provenance.source_digest()}
    sources = {"revision": "the installed package"}
    return {
        name: Input.value(values, sources.get(name, "the fleet")) for name, values in facts.items()
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


def stamp(job: dict[str, Any], inputs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Annotate a gate Job with the digest of everything it runs: its labels, which name the
    run, its whole pod template, with image, env, command, volumes and keep subPath, and its
    inputs ConfigMap's content."""
    content = (inputs or {}).get("binaryData") or {}, (inputs or {}).get("data") or {}
    digest = canonical_digest(
        {
            "labels": job["metadata"].get("labels") or {},
            "spec": job["spec"],
            "inputs": content,
        }
    )
    job["metadata"].setdefault("annotations", {})[SPEC_ANNOTATION] = digest
    return job


def _same_gate(live: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    """Whether a Job found under a name is the one `desired` describes, to the digest of its
    spec and inputs. A Job another run, attempt or older Narwhal made under the name is not."""

    def digest(job: Mapping[str, Any]) -> str | None:
        return (job["metadata"].get("annotations") or {}).get(SPEC_ANNOTATION)

    return digest(desired) is not None and digest(live) == digest(desired)


def _finished(job: Mapping[str, Any] | None) -> bool:
    """Whether a Job has completed or failed; a missing Job has finished too."""
    if job is None:
        return True
    return any(
        _conditions(job).get(kind, {}).get("status") == "True" for kind in ("Complete", "Failed")
    )


def _empty(directory: Path) -> None:
    """Remove what an interrupted attempt wrote in its own directory, without following links."""
    for path in directory.iterdir():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


class _Action:
    """A stage action as the runner calls it, with the Jobs of its attempt kept in order.

    A new attempt first deletes the Jobs earlier attempts left, which the record names in
    `stale_jobs`. An attempt that passes deletes its own Jobs, and one that fails keeps them
    for inspection until the next attempt. `adopt` tells the runner whether an interrupted
    attempt's Jobs still exist, so it resumes that attempt. A resumed attempt starts from an
    empty directory and reads each adopted Job's evidence again from its log.
    """

    def __init__(
        self,
        deployment: KubernetesDeployment,
        name: str,
        action: Callable[[StageContext], Outcome | None],
    ) -> None:
        self.deployment = deployment
        self.name = name
        self.action = action

    def __call__(self, context: StageContext) -> Outcome | None:
        deployment = self.deployment
        where = key(self.name, context.instance)
        resumed = " (resumed)" if context.resumed else ""
        deployment.announce(f"{where}: attempt {context.attempt}{resumed}")
        deployment.begin(context)
        deployment.save_credential()
        if context.resumed:
            _empty(context.directory)
        else:
            deployment.remove_stale(context)
        outcome = self.action(context)
        deployment.clean_up(context)
        deployment.announce(f"{where}: passed")
        return outcome

    def adopt(self, previous: Mapping[str, Any]) -> bool:
        return self.deployment.adopt(previous)


class KeyedRunner(Runner):
    """The deployment's runner, whose `credential` input also holds the HMAC of the engine API
    key itself, so an edit of the Secret in place reruns the engines and the routers.

    `run` reads the key's HMAC through a key-check Job first. `plan` takes the HMAC the last
    run recorded, and starts no Job.
    """

    def __init__(self, deployment: KubernetesDeployment, *args: Any, **options: Any) -> None:
        super().__init__(*args, **options)
        self.deployment = deployment

    def run(
        self, inputs: Mapping[str, Input], *, until: str | None = None, stop: Stop | None = None
    ) -> RunResult:
        keyed = self.deployment.keyed(inputs, observe=True)
        return super().run(keyed, until=until, stop=stop)

    def plan(self, inputs: Mapping[str, Input], *, until: str | None = None) -> RunResult:
        return super().plan(self.deployment.keyed(inputs, observe=False), until=until)


class KubernetesDeployment:
    """The actions, live checks and topology of one fleet's deployment in one run directory."""

    def __init__(
        self,
        directory: Path,
        env: Mapping[str, str],
        *,
        dry: bool = False,
        cluster: Callable[[Mapping[str, str], Path], Cluster] | None = None,
        max_age: Mapping[str, float] | None = None,
    ) -> None:
        self.directory = directory
        self.env = target_env(env)
        # A dry deployment runs no Job: `plan`, and `status` while another runner holds the run.
        self.dry = dry
        self._cluster = cluster or KubectlCluster
        self.max_age = dict(max_age or {})
        self.sleep: Callable[[float], None] = _sleep
        self.clock: Callable[[], float] = _clock
        self.announce: Callable[[str], None] = lambda line: print(line, flush=True)
        self._fabric: tuple[str, fabric.Fabric] | None = None
        self._fleet: kubernetes_objects.Fleet | None = None
        # The Jobs each running attempt started, in order, by stage instance and attempt.
        self._jobs: dict[tuple[str, str, int], list[str]] = {}
        # The key check's salt and HMAC, until the run directory can keep them.
        self._credential: dict[str, str] | None = None

    # The runner.

    def runner(self) -> KeyedRunner:
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
        return KeyedRunner(
            self,
            KUBERNETES_GRAPH,
            self.directory,
            target=TARGET,
            revision=provenance.source_digest(),
            actions={name: _Action(self, name, action) for name, action in actions.items()},
            topology=Topology(self),
            probes={
                "engine_identity": self.engine_running,
                "link": self.link_now,
                "sidecar_identity": self.sidecar_running,
                "router_identity": self.routers_running,
            },
            max_age=self.max_age,
        )

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

    # The fleet.

    # The engine API key, seen only through its HMAC.

    def keyed(self, inputs: Mapping[str, Input], *, observe: bool) -> dict[str, Input]:
        """`inputs` with the key's HMAC added to the `credential` fact. With `observe`, a
        key-check Job reads it; otherwise it is the one the last run recorded."""
        found = dict(inputs)
        if "credential" not in found or api_key_secret(self.env) is None:
            return found
        if observe and not self._fleet_holds():
            # Its Job would go with the fleet it names; `access` says why the run stops.
            return found
        digest = self.key_hmac(observe=observe)
        if digest is not None:
            reference = found["credential"]
            found["credential"] = Input.value(
                {"reference": reference.digest, "key_hmac_sha256": digest}, reference.source
            )
        return found

    def _fleet_holds(self) -> bool:
        """Whether the namespace holds the fleet of this run's uid."""
        live = self.cluster().get(FLEET, fleet_resource.NAME)
        return live is not None and live["metadata"].get("uid") == self.env[FLEET_UID]

    def recorded_credential(self) -> dict[str, str] | None:
        """The salt and the HMAC the last key check left in the run directory, or None."""
        path = self.directory / CREDENTIAL_FILE
        if not path.is_file():
            return None
        found = _json(path)
        if not isinstance(found, dict) or not all(
            isinstance(found.get(field), str) and HEX64.fullmatch(found[field])
            for field in ("salt", "hmac")
        ):
            raise ValueError(f"{path} holds no salt and HMAC; remove it and run again")
        return {"salt": found["salt"], "hmac": found["hmac"]}

    def key_hmac(self, *, observe: bool) -> str | None:
        """The HMAC-SHA256 of the engine API key under the fleet's salt.

        The salt is drawn once per run directory and kept there, so the digest shows a change
        of the key and nothing about it: never the key, and no plain hash of it.
        """
        recorded = self.recorded_credential()
        if not observe:
            return recorded["hmac"] if recorded else None
        salt = recorded["salt"] if recorded else secrets.token_hex(32)
        digest = self._key_check(salt)
        self._credential = {"salt": salt, "hmac": digest}
        self.save_credential()
        return digest

    def save_credential(self) -> None:
        """Keep the key check's salt and HMAC once the run directory holds its record."""
        if self._credential is None or not (self.directory / run_record.RECORD).is_file():
            return
        path = self.directory / CREDENTIAL_FILE
        if path.is_file() and _json(path) == self._credential:
            return
        pending = path.with_name(f".{CREDENTIAL_FILE}.{os.getpid()}")
        write_private(pending, (json.dumps(self._credential) + "\n").encode())
        os.replace(pending, path)

    def _key_check(self, salt: str) -> str:
        """Run a key-check Job, which holds the key from its Secret, and return its HMAC."""
        cluster = self.cluster()
        name = kubernetes_objects.job_name(
            KEY_CHECK, self.env[FLEET_UID], KEY_CHECK, "", secrets.randbelow(2**31), 0
        )
        command = _gate_command(
            inputs=True,
            returns={"key.json": "/tmp/narwhal-key.json"},
            command=["python", "-c", _KEY_HMAC],
        )
        files = {kubernetes_gate.INPUTS: kubernetes_gate.pack({"salt": salt.encode()})}
        gate = Gate(KEY_CHECK, KEY_CHECK, command, files=files)
        job = self._gate_job_at(gate, name, f"deploy/{KEY_CHECK}/{name}", None)
        fleet = self.fleet()
        inputs = kubernetes_objects.inputs(fleet, job, gate.files)
        with tempfile.TemporaryDirectory() as folder:
            logs = Path(folder)
            if (self.directory / run_record.RECORD).is_file():
                logs = self.directory / "live" / KEY_CHECK
            context = cast(StageContext, _KeyContext(logs))
            try:
                self.apply(cluster, inputs)
                self.create_job(cluster, stamp(kubernetes_objects.job(fleet, job), inputs))
                result = self.finish_gate(context, gate, name)
            finally:
                self.remove(cluster, JOB, name)
                self.remove(cluster, CONFIG_MAP, name)
        reported = json.loads(result.files["key.json"])
        digest = reported.get("hmac_sha256") if isinstance(reported, dict) else None
        if not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise GateOutputError(f"Job {name} reported no HMAC of the engine API key")
        return digest

    def owner(self) -> Owner:
        return Owner(fleet_resource.NAME, self.env[FLEET_UID], self.env[NAMESPACE])

    def fleet(self) -> kubernetes_objects.Fleet:
        """What every object this deployment builds shares, from the fleet's inputs."""
        if self._fleet is None:
            env = self.env
            secret = api_key_secret(env)
            haproxy = _object(env, HAPROXY_ENV)
            self._fleet = kubernetes_objects.Fleet(
                self.owner(),
                narwhal_image=env[SIDECAR_IMAGE_ENV],
                state_claim=env[STATE_CLAIM],
                haproxy_image=env.get(HAPROXY_IMAGE_ENV) or None,
                fabric_image=env.get(FABRIC_IMAGE_ENV) or None,
                api_key=(
                    kubernetes_objects.ApiKey(API_KEY_ENV, secret["name"], secret["key"])
                    if secret
                    else None
                ),
                engine_account=env.get(ENGINE_ACCOUNT) or None,
                rdma_resource=env.get(RDMA_RESOURCE) or None,
                model_volume=json.loads(env[MODEL_VOLUME]),
                cluster_domain=str(haproxy.get("clusterDomain", "cluster.local")),
                haproxy_replicas=int(haproxy.get("replicas", 1)),
            )
        return self._fleet

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

    # Writes. Each reads the object back when its outcome is unknown.

    def apply(self, cluster: Cluster, document: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one object the fleet owns. An apply is idempotent, so one whose outcome is
        unknown is made again."""
        kind = kind_of(document)
        for attempt in range(WRITE_TRIES):
            try:
                return cluster.apply(kind, document)
            except OutcomeUnknown:
                if attempt + 1 == WRITE_TRIES:
                    raise
        raise AssertionError("unreachable")

    def controls(self, cluster: Cluster, live: Mapping[str, Any]) -> bool:
        """Whether the fleet controls an object: directly, or for a pod through the
        StatefulSet or Job that controls it."""
        owner = self.owner()
        if controlled(owner, live):
            return True
        if live.get("kind") != POD.kind:
            return False
        for reference in live["metadata"].get("ownerReferences") or []:
            kind = {"StatefulSet": STATEFUL_SET, "Job": JOB}.get(reference.get("kind", ""))
            if reference.get("controller") is True and kind is not None:
                parent = cluster.get(kind, reference.get("name", ""))
                return (
                    parent is not None
                    and parent["metadata"].get("uid") == reference.get("uid")
                    and controlled(owner, parent)
                )
        return False

    def remove(self, cluster: Cluster, kind: Kind, name: str) -> None:
        """Delete one object the fleet controls, in the background; one another owner controls
        stays. The delete names the uid that was read, so an object made again under the name
        in between stays. A delete whose outcome is unknown is made again until the object is
        gone."""
        for attempt in range(WRITE_TRIES):
            live = cluster.get(kind, name)
            if (
                live is None
                or (live.get("metadata") or {}).get("deletionTimestamp")
                or not self.controls(cluster, live)
            ):
                return
            try:
                cluster.delete(kind, name, uid=live["metadata"]["uid"])
                return
            except Conflict:
                # Another object holds the name now; the next read decides on it.
                continue
            except OutcomeUnknown:
                if attempt + 1 == WRITE_TRIES:
                    raise

    def create_job(self, cluster: Cluster, job: Mapping[str, Any]) -> bool:
        """Create a gate Job, or adopt the one of its name: returns whether it was adopted.

        The name follows from the fleet and the attempt, so a Job of that name that the fleet
        controls and whose spec digest matches is this attempt's own, started before the runner
        restarted. One another owner controls is refused, as its log is not this gate's. One
        the fleet controls with another digest, which another run directory, a replay that took
        another path or an older Narwhal made, is deleted with its uid as precondition and the
        Job created again, as is one already being deleted once it is gone.
        """
        name = job["metadata"]["name"]
        for attempt in range(WRITE_TRIES):
            try:
                cluster.create(JOB, job)
                return False
            except (Conflict, OutcomeUnknown) as error:
                live = cluster.get(JOB, name)
                if live is None:
                    if isinstance(error, Conflict) or attempt + 1 == WRITE_TRIES:
                        raise
                    continue
                if not controlled(self.owner(), live):
                    raise ValueError(
                        f"Job {name} exists and another owner controls it; this fleet's gate "
                        "cannot run under its name, so remove it"
                    ) from None
                if (live.get("metadata") or {}).get("deletionTimestamp"):
                    self._gone(cluster, name)
                    continue
                if not _same_gate(live, job):
                    self.announce(f"Job {name} is another run's or attempt's; replacing it")
                    self.remove(cluster, JOB, name)
                    self._gone(cluster, name)
                    continue
                if isinstance(error, Conflict):
                    self.announce(f"adopted Job {name}")
                    return True
                return False
        raise ValueError(f"Job {name} could not be created after {WRITE_TRIES} tries")

    def _gone(self, cluster: Cluster, name: str) -> None:
        """Wait until the Job of `name` and its pods are deleted."""
        deadline = self.clock() + GONE_SECONDS
        while cluster.get(JOB, name) is not None:
            if self.clock() > deadline:
                raise ValueError(f"Job {name} was not deleted within {GONE_SECONDS}s")
            self.sleep(POLL_SECONDS)

    # Jobs across restarts.

    @staticmethod
    def _attempt(context: StageContext) -> tuple[str, str, int]:
        return (context.stage, context.instance, context.attempt)

    def begin(self, context: StageContext) -> None:
        """Number this attempt's Jobs from the first again: a resumed attempt replays its
        action and meets each Job it started under the same name."""
        self._jobs[self._attempt(context)] = []

    def adopt(self, previous: Mapping[str, Any]) -> bool:
        """Whether any Job an interrupted attempt recorded still exists, for the runner to
        resume that attempt and adopt it. A recorded name another owner holds is refused."""
        cluster = self.cluster()
        found = False
        for name in previous.get("jobs") or []:
            live = cluster.get(JOB, name)
            if live is None or (live.get("metadata") or {}).get("deletionTimestamp"):
                continue
            if not controlled(self.owner(), live):
                raise ValueError(
                    f"Job {name}, which {key(previous['stage'], previous['instance'])} recorded, "
                    "exists and another owner controls it; remove it"
                )
            found = True
        return found

    def remove_stale(self, context: StageContext) -> None:
        """Delete the Jobs, and their inputs, that earlier attempts of this stage left."""
        stale = list(context.stale_jobs)
        if not stale:
            return
        cluster = self.cluster(context)
        for name in stale:
            self.remove(cluster, JOB, name)
            self.remove(cluster, CONFIG_MAP, name)
        context.jobs_deleted(stale)

    def clean_up(self, context: StageContext) -> None:
        """Delete the Jobs of an attempt that passed, with those a resumed attempt recorded
        before it restarted and did not meet again. A Job left behind goes with the next
        attempt, or with the fleet."""
        cluster = self.cluster(context)
        started = self._jobs.pop(self._attempt(context), [])
        recorded = list(context.previous["jobs"]) if context.resumed and context.previous else []
        for name in dict.fromkeys([*started, *recorded]):
            try:
                self.remove(cluster, JOB, name)
            except KubernetesError as error:
                self.announce(f"{key(context.stage, context.instance)}: Job {name} stays: {error}")

    # Gate Jobs.

    def _job_name(self, context: StageContext, kind: str) -> str:
        started = self._jobs.setdefault(self._attempt(context), [])
        name = kubernetes_objects.job_name(
            kind,
            self.env[FLEET_UID],
            context.stage,
            context.instance,
            context.attempt,
            len(started),
        )
        started.append(name)
        return name

    def next_job_name(self, context: StageContext, kind: str) -> str:
        """The name the attempt's next Job takes, without taking it."""
        started = self._jobs.get(self._attempt(context), [])
        return kubernetes_objects.job_name(
            kind,
            self.env[FLEET_UID],
            context.stage,
            context.instance,
            context.attempt,
            len(started),
        )

    def _gate_job(self, context: StageContext, gate: Gate, name: str) -> GateJob:
        keep = self.state_dir(context).removeprefix(f"{STATE}/") + f"/jobs/{name}"
        return self._gate_job_at(gate, name, keep, context.run_id)

    @staticmethod
    def _gate_job_at(gate: Gate, name: str, keep: str, run: str | None) -> GateJob:
        return GateJob(
            name=name,
            kind=gate.kind,
            command=["sh", "-c", _KEEP, "sh", *gate.command],
            deadline_seconds=JOB_SECONDS[gate.kind],
            keep=keep,
            node=gate.node,
            env=dict(gate.env),
            resources=dict(gate.resources),
            image=gate.image,
            runtime_class=gate.runtime_class,
            host_network=gate.host_network,
            inputs=name if gate.files else None,
            model_volume=gate.model_volume,
            service_account=gate.service_account,
            run=run,
        )

    def start_gates(self, context: StageContext, gates: Sequence[Gate]) -> list[str]:
        """Start each gate's Job, with its inputs ConfigMap, and return their names.

        The run record names every Job before the first is created, so a runner that restarts
        finds them. A Job that already exists under its name is adopted.
        """
        cluster = self.cluster(context)
        fleet = self.fleet()
        names = [self._job_name(context, gate.kind) for gate in gates]
        record = getattr(context, "record_jobs", None)
        if record is not None:
            record(names)
        for gate, name in zip(gates, names, strict=True):
            job = self._gate_job(context, gate, name)
            inputs = kubernetes_objects.inputs(fleet, job, gate.files) if gate.files else None
            if inputs is not None:
                self.apply(cluster, inputs)
            self.create_job(cluster, stamp(kubernetes_objects.job(fleet, job), inputs))
        return names

    def job_pods(self, cluster: Cluster, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The Job's own pods, newest last."""
        uid = job["metadata"].get("uid")
        pods = [
            pod
            for pod in cluster.list(POD, {JOB_NAME_LABEL: job["metadata"]["name"]})
            if any(r.get("uid") == uid for r in pod["metadata"].get("ownerReferences") or [])
        ]
        return sorted(pods, key=lambda pod: pod["metadata"].get("creationTimestamp", ""))

    def job_log(self, cluster: Cluster, job: Mapping[str, Any], *, finished: bool = True) -> str:
        """The log of the Job's newest pod.

        For a finished Job the log is its evidence: a read the API refuses is tried again with
        backoff, and one that still fails interrupts the attempt, whose next pass adopts the
        Job and reads it again. While the Job runs, as a sample waits for its server, a pod
        with no log to read yet gives "".
        """
        name = job["metadata"]["name"]
        backoff = POLL_SECONDS
        for attempt in range(LOG_TRIES):
            try:
                pods = self.job_pods(cluster, job)
                if not pods:
                    if finished:
                        raise Interrupted(f"Job {name} finished and no pod of it is left to read")
                    return ""
                return cluster.log(pods[-1]["metadata"]["name"], GATE_CONTAINER).decode(
                    errors="replace"
                )
            except LogTooLarge as error:
                raise GateOutputError(f"Job {name}: {error}") from None
            except KubernetesError as error:
                if not finished:
                    return ""
                if isinstance(error, Unavailable):
                    # An outage is the caller's to wait out.
                    raise
                if attempt + 1 == LOG_TRIES:
                    raise Interrupted(
                        f"Job {name}: its log could not be read ({error}); the next pass "
                        "adopts the Job and reads it again"
                    ) from error
                self.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_SECONDS)
        raise AssertionError("unreachable")

    def _patient(self, name: str, limit: float, call: Callable[[], Any]) -> Any:
        """`call`, tried again with backoff while the API is unavailable, for up to `limit`
        seconds; past that the attempt is interrupted, for the next pass to adopt its Job."""
        started = self.clock()
        backoff = POLL_SECONDS
        while True:
            try:
                return call()
            except Unavailable as error:
                if self.clock() - started > limit:
                    raise Interrupted(
                        f"Job {name}: the API was unavailable for {self.clock() - started:.0f}s; "
                        "the next pass adopts the Job"
                    ) from error
                self.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_SECONDS)

    def _cancel(self, context: StageContext, cluster: Cluster) -> None:
        """Delete every Job the cancelled attempt started, then end it."""
        for name in self._jobs.get(self._attempt(context), []):
            self.remove(cluster, JOB, name)
            self.remove(cluster, CONFIG_MAP, name)
        raise Cancelled(f"{key(context.stage, context.instance)} was cancelled; its Jobs are gone")

    def finish_gate(
        self, context: StageContext, gate: Gate, name: str, where: str = ""
    ) -> GateResult:
        """Wait for one gate Job, keep its log, and return its output and files.

        A cancelled stage deletes its Jobs. A Job whose pod will never run fails at once and
        is deleted, so its pod holds nothing on its node.
        """
        cluster = self.cluster(context)
        here = where or gate.node or "cluster"
        limit = JOB_SECONDS[gate.kind] + GRACE_SECONDS
        deadline = self.clock() + limit
        while True:
            if getattr(context, "cancelled", False):
                self._cancel(context, cluster)
            # An API outage while the Job runs is waited out for as long as the Job may run.
            job = self._patient(name, limit, lambda: cluster.get(JOB, name))
            if job is None:
                raise GateFailed(
                    here, f"Job {name}", 4, "the Job is gone before it finished", context.directory
                )
            conditions = _conditions(job)
            done = [c for t, c in conditions.items() if t in ("Complete", "Failed")]
            if any(c.get("status") == "True" for c in done):
                break
            pods = self._patient(name, limit, functools.partial(self.job_pods, cluster, job))
            for pod in pods:
                if cause := never_runs(pod, scheduled=False):
                    self.remove(cluster, JOB, name)
                    raise GateFailed(here, f"Job {name}", 4, cause, context.directory / "logs")
            if self.clock() > deadline:
                raise GateFailed(
                    here,
                    f"Job {name}",
                    124,
                    "did not finish within its time limit",
                    context.directory / "logs",
                )
            self.sleep(POLL_SECONDS)
        pods = self._patient(name, limit, lambda: self.job_pods(cluster, job))
        pod = pods[-1] if pods else {}
        failed = conditions.get("Failed", {}).get("status") == "True"
        if failed and pod and never_runs(pod, scheduled=False):
            # A pod the kubelet refused ran nothing, so it has no log to read.
            output = ""
        else:
            output = self._patient(name, limit, lambda: self.job_log(cluster, job))
        log = context.directory / "logs" / f"{name}.log"
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(log, output.encode())
        if gate.files:
            self._patient(name, limit, lambda: self.remove(cluster, CONFIG_MAP, name))
        if failed:
            terminated = (_container(pod, GATE_CONTAINER) or {}).get("state", {}).get("terminated")
            status = int((terminated or {}).get("exitCode", 4))
            causes = [
                line.split(": ", 1)[-1]
                for line in output.splitlines()
                if line.startswith(("narwhal-gate: ", "narwhal-host: ", "narwhal: "))
            ]
            reason = conditions["Failed"].get("message") or conditions["Failed"].get("reason", "")
            if not causes and (refused := never_runs(pod, scheduled=False)):
                causes = [refused]
            cause = causes[-1] if causes else (reason or f"exited {status}")
            raise GateFailed(here, f"Job {name}", status, cause, log)
        files = {} if gate.plain else gate_files(output)
        return GateResult(name, log, output, files, pod)

    def gates(
        self, context: StageContext, gates: Sequence[Gate], into: Path | None = None
    ) -> list[GateResult]:
        """Run gate Jobs together; write each one's returned files under `into`."""
        names = self.start_gates(context, gates)
        results = [
            self.finish_gate(context, gate, name) for gate, name in zip(gates, names, strict=True)
        ]
        if isinstance(context, _LiveContext):
            # A live check's Jobs belong to no attempt, so none outlives its check.
            cluster = self.cluster(context)
            for name in names:
                self.remove(cluster, JOB, name)
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
        """Check that the namespace holds the fleet this run deploys, and that each engine's
        node offers the engine pods' resources."""
        cluster = self.cluster(context)
        problems = []
        found = cluster.get(FLEET, fleet_resource.NAME)
        uid = (found or {}).get("metadata", {}).get("uid")
        if found is None:
            problems.append(
                f"namespace {cluster.namespace} holds no {fleet_resource.KIND} "
                f"{fleet_resource.NAME}"
            )
        elif uid != self.env[FLEET_UID]:
            problems.append(
                f"the {fleet_resource.KIND} in {cluster.namespace} has uid {uid}, not "
                f"{self.env[FLEET_UID]}; the fleet was deleted and made again, so start a new run"
            )
        nodes = {}
        resource = self.env[GPU_RESOURCE]
        rdma = self.env.get(RDMA_RESOURCE, "")
        for role, name in engine_nodes(self.env).items():
            node = cluster.get(NODE, name)
            if node is None:
                problems.append(f"the cluster has no node {name} for {role}")
                continue
            allocatable = node.get("status", {}).get("allocatable", {})
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
                "namespace": cluster.namespace,
                "fleet": {"name": fleet_resource.NAME, "uid": uid},
                "nodes": nodes,
            },
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
        # The Job's own variables that the runner sets, which the image's environment is not.
        job_env = [API_KEY_ENV] if env.get(API_KEY_SECRET_ENV) else []
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
        names = self.start_gates(context, gates)
        results = [
            self.finish_gate(context, gate, name) for gate, name in zip(gates, names, strict=True)
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
        revision = env[SIDECAR_IMAGE_ENV].rpartition("@sha256:")[2]
        if env.get(API_KEY_SECRET_ENV):
            # The routers and the Jobs that call engines hold the key in this variable, from
            # the Secret; no file the runner writes holds it.
            fleet.setdefault("engine", {})["engine_api_key_env"] = API_KEY_ENV
        engine = launcher(fleet.get("engine", {}).get("backend"))
        selected = {}
        for role in launches["engines"]:
            selected[role] = select_values(
                "engine", _node(role), fleet, stage_env, by_reference=True
            )
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
        router = select_values("router", None, fleet, stage_env, by_reference=True)
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

    def job_env(
        self,
        role_env: Mapping[str, str],
        *,
        credential: bool,
        extra: Mapping[str, str] | None = None,
    ) -> bytes:
        """A role file for a Job's inputs ConfigMap, which holds no secret. With `credential`,
        for a Job that calls engines, the engine tools' `NARWHAL_ENGINE_API_KEY` comes from the
        Job's own variable, which its Secret sets; without, the file names no key."""
        fleet = _json(self.package() / "fleet.json")
        key = fleet.get("engine", {}).get("engine_api_key_env")
        values = {**role_env, **(extra or {})}
        lines = [
            f"export {name}={shlex.quote(value)}\n"
            for name, value in sorted(values.items())
            if name not in (key, "NARWHAL_ENGINE_API_KEY")
        ]
        if credential and key:
            lines.append(f'export NARWHAL_ENGINE_API_KEY="${{{key}:?the Job has no {key}}}"\n')
        return "".join(lines).encode()

    def engine_prepare(self, context: StageContext) -> Outcome:
        role = context.instance
        package = self.package()
        host = self.node(role)
        role_env = read_environment(package / host / f".env.{role}")
        # The launch names the engines' Secret and reads none: preparing calls no engine.
        pod_inputs = {name: self.env[name] for name in POD_INPUTS if self.env.get(name)}
        files = {
            ".env": self.job_env(role_env, credential=False, extra=pod_inputs),
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

    def engine_objects(self) -> list[dict[str, Any]]:
        """The engines' hook ConfigMaps, Service and StatefulSet, from every prepared launch,
        with each pod held at the placement gate."""
        values = engine_set.chart_values(list(self.launches().values()))
        roles = [f"engine-{n}" for n in range(1, int(values["count"]) + 1)]
        return kubernetes_objects.engines(self.fleet(), values, self.placement(roles))

    def install(self, context: StageContext) -> None:
        """Apply the engines' objects, then delete the engine objects the fleet no longer
        holds. A hook ConfigMap a running engine pod mounts stays until engine-start replaces
        the pod, which keeps the spec it started with."""
        cluster = self.cluster(context)
        desired = self.engine_objects()
        _private(context.directory / "objects.json", _redacted(desired))
        for document in desired:
            self.apply(cluster, document)
        component = {kubernetes_objects.COMPONENT_LABEL: kubernetes_objects.ENGINE}
        live = [
            item
            for kind in (STATEFUL_SET, SERVICE, CONFIG_MAP)
            for item in cluster.list(kind, component)
        ]
        pods = cluster.list(POD, component)
        for deletion in kubernetes_objects.prune(self.owner(), desired, live, pods):
            self.remove(cluster, KINDS[deletion.kind], deletion.name)
        statefulset = cluster.get(STATEFUL_SET, engine_set.STATEFULSET) or {}
        _private(context.directory / "statefulset.json", statefulset.get("status", {}))

    # engine-start

    def engine_start(self, context: StageContext) -> Outcome:
        role = context.instance
        cluster = self.cluster(context)
        launch = context.product("launch", role)
        previous = context.previous
        recorded = (previous or {}).get("products", {}).get("engine_identity") if previous else None
        pod = cluster.get(POD, role)
        statefulset = cluster.get(STATEFUL_SET, engine_set.STATEFULSET) or {}
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
                old = pod["metadata"].get("uid")
                self.remove(cluster, POD, role)
                pod = self._wait_pod(context, role, lambda p: p["metadata"].get("uid") != old)
        self._pin(cluster, self._wait_pod(context, role, lambda p: True), role)
        identity = None
        deadline = self.clock() + START_SECONDS
        while identity is None:
            pod = cluster.get(POD, role)
            if pod is not None:
                running = container_identity(pod, ENGINE_CONTAINER)
                status = _container(pod, ENGINE_CONTAINER) or {}
                if running is not None and status.get("ready"):
                    identity = running
                    break
                waiting = status.get("state", {}).get("waiting") or {}
                cause = never_runs(pod, scheduled=True)
                if cause is None and waiting.get("reason") in IMAGE_FAILURES:
                    cause = waiting.get("message", waiting["reason"])
                if cause is not None:
                    raise GateFailed(
                        self.node(role), f"{role} start", 4, cause, context.directory / "logs"
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
        """Pin a pod held at its scheduling gate to the node discovery measured its engine on,
        then release it to the scheduler.

        A gated pod accepts added node affinity, so each pod of the one StatefulSet lands on
        its own node. A pin an earlier attempt half applied, with the affinity already naming
        this node, only needs the gate removed. A patch whose outcome is unknown is followed by
        a read of the pod, and the pin goes on from what that read shows.
        """
        for attempt in range(WRITE_TRIES):
            try:
                self._pin_once(cluster, pod, role)
                return
            except OutcomeUnknown:
                if attempt + 1 == WRITE_TRIES:
                    raise
                pod = cluster.get(POD, role) or {}
                if pod.get("metadata", {}).get("uid") is None:
                    raise

    def _pin_once(self, cluster: Cluster, pod: Mapping[str, Any], role: str) -> None:
        spec = pod.get("spec", {})
        gates = [gate.get("name") for gate in spec.get("schedulingGates", []) or []]
        if PLACEMENT_GATE not in gates:
            return
        node = self.node(role)
        term = {"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}
        affinity = {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [term]}}
        present = (spec.get("affinity") or {}).get("nodeAffinity")
        uid = pod["metadata"]["uid"]
        # Each patch applies only to the pod that was read, never to a new pod of its name.
        same = {"op": "test", "path": "/metadata/uid", "value": uid}
        if present is None:
            value = {**(spec.get("affinity") or {}), "nodeAffinity": affinity}
            operation = "replace" if "affinity" in spec else "add"
            cluster.patch(
                POD, role, [same, {"op": operation, "path": "/spec/affinity", "value": value}]
            )
        elif present != affinity:
            raise ValueError(
                f"pod {role} has node affinity for another node than {node}; delete the pod so "
                "the StatefulSet makes a new one"
            )
        index = gates.index(PLACEMENT_GATE)
        path = f"/spec/schedulingGates/{index}"
        cluster.patch(
            POD,
            role,
            [
                same,
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
            pod = cluster.get(POD, name)
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
  # The attestation sidecar, another user, reads the launch, which holds no key.
  chmod -R u=rwX,go=rX "$stage"
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
                read_environment(package / node / f".env.{role}"), credential=True
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
            pod = cluster.get(POD, role) or {}
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
            pod = cluster.get(POD, name)
            identity = container_identity(pod or {}, "router")
            if (
                pod is None
                or identity is None
                or not (_container(pod, "router") or {}).get("ready")
            ):
                return None
            found[name] = {"pod_uid": pod["metadata"]["uid"], **identity}
        return found

    def router_objects(self, fleet_file: str) -> list[dict[str, Any]]:
        """The router pair's objects on `fleet_file`, a path of the state volume, from the
        fleet's `routers` block, and HAProxy's."""
        fleet = self.fleet()
        settings = kubernetes_objects.Routers.of(_object(self.env, ROUTERS_ENV), fleet_file)
        return [*kubernetes_objects.routers(fleet, settings), *kubernetes_objects.haproxy(fleet)]

    def router_start(self, context: StageContext) -> Outcome:
        cluster = self.cluster(context)
        fleet_path = self._limits(context)
        fleet = context.outputs("fleet-limits") / "fleet.json"
        engines = {role: context.product("engine_identity", role) for role in self.engines()}
        objects = self.router_objects(fleet_path)
        # The routers are readmitted only while their objects are the ones they run, so a
        # new Secret or router setting restarts them.
        state = {
            "fleet_sha256": _digest(fleet),
            "fleet": fleet_path,
            "engines": engines,
            "objects_sha256": hashlib.sha256(
                json.dumps(objects, sort_keys=True).encode()
            ).hexdigest(),
        }
        previous = context.previous
        if previous is not None and previous["status"] == PASSED:
            kept = self._readmit(context, cluster, previous, state)
            if kept is not None:
                return kept
        _private(context.directory / "objects.json", _redacted(objects))
        for document in objects:
            self.apply(cluster, document)
        deadline = self.clock() + ROUTER_SECONDS
        # When each router pod, by uid, was first found with no node to run on.
        waiting: dict[str, float] = {}
        while (routers := self._routers(cluster)) is None:
            for name in ROUTERS:
                pod = cluster.get(POD, name)
                if pod is None:
                    continue
                cause = never_runs(pod, scheduled=False)
                if cause is None and (pending := unschedulable(pod)):
                    # Routers schedule anywhere the chart allows, so a node may yet come.
                    since = waiting.setdefault(pod["metadata"]["uid"], self.clock())
                    if self.clock() - since > ROUTER_UNSCHEDULABLE_SECONDS:
                        cause = f"{pending}, for {ROUTER_UNSCHEDULABLE_SECONDS}s"
                if cause:
                    raise GateFailed(
                        "cluster", f"router {name}", 4, cause, context.directory / "logs"
                    )
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
        if (
            before["fleet_sha256"] != state["fleet_sha256"]
            or before.get("objects_sha256") != state["objects_sha256"]
            or self._routers(cluster) != recorded
        ):
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
        self._host_http(context, "readmit", "readmit", "120", *engines)
        _private(context.directory / "router.json", state)
        self._router_responses(context, completion=False)
        return Outcome(
            {"router_identity": recorded},
            tuple({"name": name, "host": "cluster", **value} for name, value in recorded.items()),
        )

    def router_urls(self) -> list[str]:
        """Each router pod's own address. HAProxy forwards only the serving routes, so the
        router checks and lifecycle calls reach a router here."""
        fleet = self.fleet()
        domain = f"{kubernetes_objects.ROUTER_SERVICE}.{self.env[NAMESPACE]}.svc"
        return [
            f"http://{pod}.{domain}.{fleet.cluster_domain}:{kubernetes_objects.ROUTER_PORT}"
            for pod in ROUTERS
        ]

    def _host_http(self, context: StageContext, into: str, action: str, *args: str) -> None:
        """Run a `host_http` action at the router that controls the fleet, the one whose
        `/narwhal/targets` answers 200, in a router-check Job."""
        tool = "python -m narwhal.deployment.host_http"
        bases = self.router_urls()
        missing = shlex.quote(f"{HOST_CAUSE}no router controls the fleet at {', '.join(bases)}")
        script = (
            f"for base in {shlex.join(bases)}; do\n"
            f'  if [ "$({tool} status "$base/narwhal/targets")" = 200 ]; then\n'
            f'    exec {tool} {shlex.quote(action)} "$base" {shlex.join(args)}\n'
            "  fi\n"
            "done\n"
            f"echo {missing} >&2\n"
            "exit 1\n"
        )
        command = ["sh", "-c", script]
        result = self.gate(context, Gate("router-check", context.stage, command, plain=True))
        remote.write(context.directory / into, remote.files(result.output))

    def _router_responses(self, context: StageContext, *, completion: bool) -> None:
        fleet = _json(context.outputs("fleet-limits") / "fleet.json")
        body = json.dumps({"model": fleet["model"], "prompt": _COMPLETION_PROMPT, "max_tokens": 32})
        self._host_http(
            context,
            "router",
            "responses",
            *(["--completion", body] if completion else []),
        )

    # Live checks: each returns the recorded value while it still holds, and None once gone.

    def engine_running(self, instance: str, recorded: Any) -> Any:
        pod = self.cluster().get(POD, recorded["pod"])
        if pod is None or pod["metadata"].get("uid") != recorded["pod_uid"]:
            return None
        identity = container_identity(pod, ENGINE_CONTAINER)
        if identity is None or any(identity[k] != recorded[k] for k in identity):
            return None
        return recorded

    def sidecar_running(self, instance: str, recorded: Any) -> Any:
        pod = self.cluster().get(POD, recorded["pod"])
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

    def stop(self, cluster: Cluster) -> list[kubernetes_objects.Deletion]:
        """Delete every workload the fleet controls, keeping the volumes: the routers, HAProxy,
        the engines and then the Jobs, each workload before the Services and ConfigMaps it
        uses. Only objects whose controlling owner is this fleet are deleted."""
        live = [item for kind in STOP_KINDS for item in cluster.list(kind)]
        components = {
            (item["kind"], item["metadata"]["name"]): (item["metadata"].get("labels") or {}).get(
                kubernetes_objects.COMPONENT_LABEL, ""
            )
            for item in live
        }
        deletions = kubernetes_objects.prune(self.owner(), [], live)

        def order(deletion: kubernetes_objects.Deletion) -> tuple[int, int, str]:
            component = components[(deletion.kind, deletion.name)]
            rank = STOP_ORDER.index(component) if component in STOP_ORDER else len(STOP_ORDER)
            return rank, kubernetes_objects.PRUNED_KINDS.index(deletion.kind), deletion.name

        deletions = sorted(deletions, key=order)
        for deletion in deletions:
            self.remove(cluster, KINDS[deletion.kind], deletion.name)
        return deletions

    def down(self) -> list[dict[str, Any]]:
        """Stop the routers, engines and gate Jobs of this run's fleet, keeping the volumes.

        Only a fleet this run deployed is changed: the record must be a Kubernetes run whose
        last passed access found the fleet the environment names, by uid. Reports each process
        the run recorded, and whether it ran until now.
        """
        check_directory(self.directory)
        stopped = []
        with run_record.locked(self.directory):
            record = run_record.read(self.directory)
            if record["target"] != TARGET:
                raise ValueError(f"{self.directory} holds a {record['target']} run")
            entry = record["stages"].get("access")
            if entry is None or not (self.directory / entry["directory"] / "access.json").is_file():
                self.announce(f"{self.directory}: access never ran; this run changed no fleet")
                return []
            access = _json(self.directory / entry["directory"] / "access.json")
            uid = (access.get("fleet") or {}).get("uid")
            if access.get("namespace") != self.env[NAMESPACE] or uid != self.env[FLEET_UID]:
                raise ValueError(
                    f"down changed nothing: the run deployed fleet {uid} in "
                    f"{access.get('namespace')}, and the environment names "
                    f"{self.env[FLEET_UID]} in {self.env[NAMESPACE]}; load the environment the "
                    "run used"
                )
            cluster = self.cluster()
            for entry in record["stages"].values():
                if entry["stage"] not in ("router-start", "sidecar-start", "engine-start"):
                    continue
                for process in entry["processes"]:
                    pod = cluster.get(POD, process.get("pod", process["name"]))
                    recorded = process.get("pod_uid")
                    ran = pod is not None and recorded in (None, pod["metadata"].get("uid"))
                    host = process.get("host", "cluster")
                    stopped.append({"name": process["name"], "host": host, "stopped": ran})
            self.stop(cluster)
        return stopped


def _redacted(objects: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The objects an attempt keeps: every ConfigMap's content by its digest only."""
    kept = copy.deepcopy([dict(item) for item in objects])
    for item in kept:
        if item.get("kind") != "ConfigMap":
            continue
        for field_name in ("data", "binaryData"):
            if field_name in item:
                item[field_name] = {
                    name: hashlib.sha256(str(value).encode()).hexdigest()
                    for name, value in item[field_name].items()
                }
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
        [server_name] = deployment.start_gates(context, [server_gate])
        cluster = deployment.cluster(context)
        deadline = deployment.clock() + JOB_SECONDS["fabric-server"]
        while True:
            job = cluster.get(JOB, server_name) or {}
            if job and "narwhal-listening" in deployment.job_log(cluster, job, finished=False):
                break
            finished = any(
                c.get("status") == "True" for t, c in _conditions(job).items() if t != "Suspended"
            )
            if not job or finished or deployment.clock() > deadline:
                deployment.finish_gate(context, server_gate, server_name, where)
                raise GateFailed(
                    sample.destination.host.id,
                    f"{where} {server[0]} server",
                    4,
                    "the server did not listen",
                    context.directory / "logs",
                )
            deployment.sleep(POLL_SECONDS)
        if getattr(context, "resumed", False) and _finished(cluster.get(JOB, server_name)):
            following = deployment.next_job_name(context, "fabric-client")
            if cluster.get(JOB, following) is None:
                # A resumed sample whose server window passed while no runner ran has no
                # server for its client. Its server goes, and the next pass, which adopts the
                # attempt again, measures the sample with a new one.
                deployment.remove(cluster, JOB, server_name)
                deployment.remove(cluster, CONFIG_MAP, server_name)
                raise Interrupted(
                    f"{where}: the {server[0]} server's window passed while no runner ran"
                )
        [client_name] = deployment.start_gates(context, [client_gate])
        try:
            result = deployment.finish_gate(context, client_gate, client_name, where)
        finally:
            deployment.finish_gate(context, server_gate, server_name, where)
        report = remote.files(result.output).get("report")
        if report is None:
            raise GateOutputError(f"{where}: the {client[0]} client's log holds no report")
        return report + b"\n"


@dataclass
class _KeyContext:
    """The parts of a stage context the key check's Job uses: where its log goes."""

    directory: Path
    stage: str = KEY_CHECK
    instance: str = ""


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
        # A live check's Jobs are deleted once read, so their names need not repeat.
        self.attempt = secrets.randbelow(2**31)


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
