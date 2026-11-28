"""The workstation side of `narwhal deploy --target kubernetes`: it applies the namespace's
`NarwhalFleet` from the workstation's inputs, follows the fleet controller's status and copies
the controller's evidence into the workstation's run directory.

The fleet is the only thing the workstation writes. `build` turns the deployment variables
into the fleet's spec, the inverse of `fleet_resource.spec_env`, and `Session.apply` applies it
server-side as the field manager `narwhal-cli`, never forcing a conflict. A fleet whose `spec`
another manager owns, such as Helm, Argo CD or Flux, is followed and never written, so the
workstation never takes a field from a GitOps tool.

The run directory stays the workstation's, with the owner and mode checks of an SSH run's. It
names the fleet it follows in `kubernetes-fleet.json` and mirrors the controller's run
directory: its `run.json` and every finished attempt's evidence. The mirror arrives through
export Jobs, never exec, and the workstation never creates one: it sets the fleet's
`<GROUP>/export-request` annotation, the fleet controller makes the Job from its own image and
names it in `status.exports`, then reads the pod's log and adds the pod and the log's SHA-256
there; the workstation imports the pod's log only when it has that digest. `kubernetes_export`
describes the Job, which mounts only the run record and the attempt directories asked for,
read-only, so the salt that keys the record's digests and the key check's salt and HMACs
never reach an export pod. `kubernetes_target.gate_files` checks the files against the log's
manifest, so a truncated or altered log writes nothing, and an exported file never replaces
evidence the run directory already holds. `kubernetes-export.json` keeps which attempts the
run directory holds whole.

The workstation's identity needs the chart's fleet-editor Role, of which it uses get, create,
patch and delete on `narwhalfleets` (`FLEET_RULES`), and `WORKSTATION_RULES`: get on pods, to
check the export Job's pod the controller names and for the live checks of `plan` and
`status`, and get on `pods/log`, to read the export. It needs no verb on Jobs, Secrets, RBAC
or `pods/exec`, so it cannot start a pod of its own.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from narwhal.runtime.kubernetes_api import (
    POD,
    Conflict,
    Kind,
    KubernetesError,
    LogTooLarge,
)

from ..serving.control_token import ROUTER_TOKEN_ENV
from . import discovery, fabric, fleet_resource, kubernetes_export, run_record
from . import kubernetes_target as target
from .deploy import CALIBRATION, MARGIN, PROFILE, SLO, InputError
from .fleet_resource import (
    API_VERSION,
    CLI_MANAGER,
    CONTROL_TOKEN_SECRET_ENV,
    FABRIC_IMAGE_ENV,
    HAPROXY_ENV,
    HAPROXY_IMAGE_ENV,
    KIND,
    LAUNCH_REFUSED,
    MODEL_MOUNT,
    NAME,
    READY,
    REQUESTED_AT,
    ROUTERS_ENV,
    STAGES,
    STALLED,
    STOPPED,
    TIMEOUT_ENV,
    VolumePolicy,
    stage_condition,
    stage_timeout_env,
)
from .kubernetes_graph import KUBERNETES_GRAPH
from .kubernetes_target import FLEET, Cluster, KubectlCluster, KubernetesDeployment
from .launch_engine.kubernetes import (
    API_KEY_SECRET_ENV,
    MODEL_VOLUME_SOURCES,
    SIDECAR_IMAGE_ENV,
    api_key_secret,
)
from .launch_engine.plan import API_KEY_ENV
from .runner import Decision, check_directory

# The workstation's own files in the run directory, which no export writes.
FLEET_FILE = "kubernetes-fleet.json"
KUBECTL_LOGS = "kubectl"
LIVE_LOGS = "live"
LOCAL_ONLY = (FLEET_FILE, "kubernetes-export.json", run_record.LOCK, KUBECTL_LOGS, LIVE_LOGS)
# The controller's files no export reads: its lock, the salt that keys the run record's
# digests, and the key check's salt and HMAC.
NEVER_EXPORTED = (
    run_record.LOCK,
    run_record.SALT,
    target.CREDENTIAL_FILE,
    "controller-key-check.json",
)
# Where the controller keeps a fleet's run directory on the state volume.
RUNS = "runs"
DEFAULT_STATE_CLAIM = "narwhal-state"
# The engines' ServiceAccount the chart names `<release>-engine`; inputs() checks only its
# form, and no fleet holds it.
_ENGINE_ACCOUNT = "narwhal-engine"
# The controller's own image, which the chart pins; inputs() checks only its form.
_NARWHAL_IMAGE = "narwhal@sha256:" + "0" * 64
_PLACEHOLDER_UID = "00000000-0000-4000-8000-000000000000"

# The export Jobs, which the controller makes on request.
EXPORT_LOG_BYTES = kubernetes_export.EXPORT_LOG_BYTES
EXPORT_RESERVE = kubernetes_export.EXPORT_RESERVE
EXPORT_SECONDS = kubernetes_export.EXPORT_SECONDS
# Export Jobs one export asks for at most; the next export carries on.
EXPORT_ROUNDS = 32
SUMMARY = kubernetes_export.SUMMARY
TEMPORARY = kubernetes_export.TEMPORARY
# Which attempts the run directory holds whole, by the fleet's uid.
EXPORT_FILE = "kubernetes-export.json"

# Following the fleet, and the time functions it waits with.
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic
POLL_SECONDS = 5.0
JOB_POLL_SECONDS = 1.0
FOLLOW_SECONDS = 24 * 3600
STOP_SECONDS = 900

# The RBAC the workstation's identity needs, beside the chart's fleet-editor Role, which holds
# the fleet rule.
FLEET_RULES: tuple[dict[str, Any], ...] = (
    {
        "apiGroups": [fleet_resource.GROUP],
        "resources": [fleet_resource.PLURAL],
        "verbs": ["get", "create", "patch", "delete"],
    },
)
WORKSTATION_RULES: tuple[dict[str, Any], ...] = (
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
)

# What a stall's reason means for the command's result.
_INPUT_STALLS = ("InputRefused", "NameRefused", "RouterStorageImmutable")
STAGE_FAILED = "StageFailed"

_CONFLICT = re.compile(
    r'conflict with "([^"]+)"(?: with subresource "[^"]+")?(?: using [^:\s]+)?: (\.?[^\s;]+)'
)


class ExportError(ValueError):
    """An export Job did not return the evidence, or returned it unchecked."""


class ExportBusy(ExportError):
    """The controller ran another export Job of the fleet, so it started none for the request."""


class FleetConflict(RuntimeError):
    """The apply met fields another field manager owns."""

    def __init__(self, message: str, fields: Sequence[tuple[str, str]]) -> None:
        super().__init__(message)
        self.fields = list(fields)


# The workstation's client.


class Workstation(Cluster, Protocol):
    """The verbs the workstation sends, in the fleet's namespace, as `narwhal-cli`."""

    def apply_fleet(self, fleet: Mapping[str, Any], *, dry_run: bool) -> dict[str, Any]: ...

    def annotate(self, kind: Kind, name: str, annotations: Mapping[str, str]) -> dict[str, Any]: ...


class KubectlWorkstation(KubectlCluster):
    """`KubectlCluster` as the workstation's field manager: its fleet applies never force a
    conflict, and every fleet it reads keeps its `managedFields`."""

    def _call(
        self, argv: list[str], data: bytes | None, timeout: float, *, write: bool
    ) -> tuple[subprocess.CompletedProcess[bytes], Path]:
        """Run kubectl, keeping the command and its standard error in a log of its own."""
        self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._count += 1
        log = self.log_dir / f"{self._count:03d}-kubectl-{argv[1]}.log"
        try:
            result = subprocess.run(
                argv,
                input=data,
                stdin=None if data is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            target.write_private(log, f"$ {shlex.join(argv)}\ntimed out\n".encode())
            raise target._kubectl_error(argv, 124, "timed out", log, write=write) from None
        target.write_private(
            log, f"$ {shlex.join(argv)}\nexit {result.returncode}\n".encode() + result.stderr
        )
        return result, log

    def _run(self, argv: list[str], data: str | None, timeout: float, *, write: bool) -> str:
        result, log = self._call(
            argv, None if data is None else data.encode(), timeout, write=write
        )
        stdout = result.stdout.decode(errors="replace")
        if not result.returncode:
            return stdout
        cause = result.stderr.decode(errors="replace") or stdout
        if "Apply failed with" in cause:
            raise Conflict(409, "Conflict", " ".join(cause.split("Please review")[0].split()))
        if " is invalid" in cause:
            raise KubernetesError(422, "Invalid", cause.strip().removeprefix("Error from server"))
        raise target._kubectl_error(argv, result.returncode, cause, log, write=write)

    def log(self, pod: str, container: str) -> bytes:
        """The container's log byte for byte, as the export's digest needs it."""
        context = ["--context", self.context] if self.context else []
        argv = [self.kubectl_path, "logs", *context, "--namespace", self.namespace]
        argv += [f"pod/{pod}", "--container", container, f"--limit-bytes={target.LOG_BYTES + 1}"]
        result, log = self._call(argv, None, 120, write=False)
        if result.returncode:
            cause = result.stderr.decode(errors="replace")
            raise target._kubectl_error(argv, result.returncode, cause, log, write=False)
        if len(result.stdout) > target.LOG_BYTES:
            raise LogTooLarge(
                0, "LogTooLarge", f"pod {pod} logged more than {target.LOG_BYTES} bytes"
            )
        return result.stdout

    def get(self, kind: Kind, name: str) -> dict[str, Any] | None:
        if kind != FLEET:
            return super().get(kind, name)
        output = self.kubectl(
            "get",
            self._resource(kind),
            name,
            "--ignore-not-found",
            "--show-managed-fields",
            "--output",
            "json",
        )
        return json.loads(output) if output.strip() else None

    def apply_fleet(self, fleet: Mapping[str, Any], *, dry_run: bool) -> dict[str, Any]:
        args = ["apply", "--server-side", f"--field-manager={CLI_MANAGER}"]
        if dry_run:
            args.append("--dry-run=server")
        output = self.kubectl(
            *args,
            "--filename",
            "-",
            "--show-managed-fields",
            "--output",
            "json",
            data=json.dumps(fleet),
            write=True,
        )
        return dict(json.loads(output))

    def annotate(self, kind: Kind, name: str, annotations: Mapping[str, str]) -> dict[str, Any]:
        output = self.kubectl(
            "patch",
            self._resource(kind),
            name,
            "--type",
            "merge",
            f"--field-manager={CLI_MANAGER}",
            "--patch",
            json.dumps({"metadata": {"annotations": dict(annotations)}}),
            "--output",
            "json",
            write=True,
        )
        return dict(json.loads(output))


Connect = Callable[[Mapping[str, str], Path], Workstation]
# How the workstation reaches the cluster; tests reach the stub API server instead.
connect: Connect = KubectlWorkstation


# The fleet from the workstation's inputs.


@dataclass(frozen=True)
class Built:
    """The fleet `build` made, and the variables it left out, by name only."""

    fleet: dict[str, Any]
    dropped: tuple[str, ...] = ()


def _put(block: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, "", [], {}):
        block[key] = value


def _number(env: Mapping[str, str], name: str, problems: list[str]) -> int | None:
    value = env.get(name, "")
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{1,9}", value):
        problems.append(f"{name} must be a whole number, not {value!r}")
        return None
    return int(value)


def _numbers(env: Mapping[str, str], name: str, problems: list[str]) -> list[int] | None:
    value = env.get(name, "")
    if not value:
        return None
    items = [item.strip() for item in value.split(",")]
    if not all(re.fullmatch(r"[0-9]{1,9}", item) for item in items):
        problems.append(f"{name} must be whole numbers separated by commas, not {value!r}")
        return None
    return [int(item) for item in items]


def _object(env: Mapping[str, str], name: str, problems: list[str]) -> dict | None:
    value = env.get(name, "")
    if not value:
        return None
    try:
        found = json.loads(value)
    except ValueError:
        found = None
    if not isinstance(found, dict):
        problems.append(f"{name} must hold one JSON object")
        return None
    return found


# The variables `build` reads as fields of the fleet, by exact name, and per engine.
_FIELDS = (
    HAPROXY_IMAGE_ENV,
    FABRIC_IMAGE_ENV,
    "NARWHAL_ENGINE_IMAGE",
    "NARWHAL_ENGINE_MODEL_NAME",
    "NARWHAL_MODEL_TREE_SHA256",
    "NARWHAL_MODEL_PATH",
    "NARWHAL_MODEL_SHA256",
    target.MODEL_VOLUME,
    target.GPU_RESOURCE,
    target.RDMA_RESOURCE,
    target.RUNTIME_CLASS,
    *(f"NARWHAL_{name}" for _, name in fleet_resource.PORT_FIELDS),
    API_KEY_SECRET_ENV,
    CONTROL_TOKEN_SECRET_ENV,
    "NARWHAL_FABRIC_INTERFACE",
    "NARWHAL_RDMA_GID_INDEX",
    "NARWHAL_RDMA_ROCE_MODE",
    *SLO,
    MARGIN,
    *PROFILE,
    *CALIBRATION,
    *(name for _, name, _ in fabric.WORKLOAD),
    fabric.TEST_PORT,
    ROUTERS_ENV,
    HAPROXY_ENV,
    TIMEOUT_ENV,
)
_PER_ENGINE = re.compile(
    r"NARWHAL_NODE_[1-9][0-9]*_(KUBERNETES_NODE|FABRIC_INTERFACE|RDMA_GID_INDEX|RDMA_ROCE_MODE"
    r"|URL|ATTESTATION_URL|ENGINE_PORT|ATTEST_PORT|KV_SIDE_CHANNEL_PORT|UCX_TCP_PORT_RANGE)"
)
# How the workstation reaches the cluster and where the export Jobs read: none is a field.
_WORKSTATION = (target.NAMESPACE, target.CONTEXT, target.KUBECTL, target.STATE_CLAIM)
# The fields every engine shares; a per-engine value that differs cannot be a fleet's.
_SHARED = re.compile(
    r"NARWHAL_NODE_[1-9][0-9]*_(ENGINE_IMAGE|ENGINE_MODEL_NAME|MODEL_TREE_SHA256|MODEL_PATH"
    r"|MODEL_SHA256)"
)


def build(env: Mapping[str, str], *, until: str | None = None) -> Built:
    """The `NarwhalFleet` the workstation's deployment variables describe, the inverse of
    `fleet_resource.spec_env`.

    Every variable a field of the fleet holds becomes that field. Any other `NARWHAL_`
    variable the Kubernetes target reads as an input becomes an `engines.launch` entry unless
    the fleet refuses its name, as it refuses the names of credentials; a refused one is left
    out and named in `dropped`. Raises `InputError` naming every missing or malformed input,
    as `kubernetes_target.inputs()` finds them in the fleet the controller would read.
    """
    problems: list[str] = []
    problems.extend(
        f"unset {name}; the NarwhalFleet now holds the settings it gave"
        for name in target.RETIRED
        if name in env
    )
    for check in (api_key_secret, target.control_token_secret):
        try:
            check(env)
        except ValueError as error:
            problems.append(str(error))
    if not target.DNS_LABEL.fullmatch(env.get(target.NAMESPACE, "")):
        problems.append(f"set {target.NAMESPACE} to the namespace the fleet runs in")
    if SIDECAR_IMAGE_ENV in env:
        problems.append(
            f"unset {SIDECAR_IMAGE_ENV}; the routers, gate Jobs and sidecars run the "
            "controller's own image, the narwhal chart's images.narwhal"
        )
    if until is not None and until not in STAGES:
        problems.append(f"--until {until}: the Kubernetes stage graph has no stage {until}")

    images: dict[str, Any] = {}
    for key, name in (
        ("haproxy", HAPROXY_IMAGE_ENV),
        ("fabric", FABRIC_IMAGE_ENV),
    ):
        _put(images, key, env.get(name))
    model: dict[str, Any] = {}
    _put(model, "name", env.get("NARWHAL_ENGINE_MODEL_NAME"))
    _put(model, "treeSha256", env.get("NARWHAL_MODEL_TREE_SHA256"))
    if path := env.get("NARWHAL_MODEL_PATH"):
        if path.startswith(MODEL_MOUNT + "/"):
            model["file"] = path[len(MODEL_MOUNT) + 1 :]
        else:
            problems.append(f"NARWHAL_MODEL_PATH must lie in the model volume, {MODEL_MOUNT}")
    _put(model, "fileSha256", env.get("NARWHAL_MODEL_SHA256"))
    engines: dict[str, Any] = {}
    _put(engines, "image", env.get("NARWHAL_ENGINE_IMAGE"))
    _put(engines, "model", model)
    _put(engines, "modelVolume", _object(env, target.MODEL_VOLUME, problems))
    resources: dict[str, Any] = {}
    _put(resources, "gpu", env.get(target.GPU_RESOURCE))
    _put(resources, "rdma", env.get(target.RDMA_RESOURCE))
    _put(resources, "runtimeClassName", env.get(target.RUNTIME_CLASS))
    _put(engines, "resources", resources)

    def port_fields(prefix: str) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for key, name in fleet_resource.PORT_FIELDS:
            if key == "ucxTcpPortRange":
                _put(found, key, env.get(prefix + name))
            else:
                _put(found, key, _number(env, prefix + name, problems))
        return found

    ports = port_fields("NARWHAL_")
    _put(engines, "ports", ports)
    if reference := env.get(API_KEY_SECRET_ENV):
        secret, _, key = reference.partition("/")
        engines["apiKeySecret"] = {"name": secret, "key": key}
    roles = target.engine_nodes(env)
    if [target._node(role) for role in roles] != list(range(1, len(roles) + 1)):
        problems.append("number the engines' NARWHAL_NODE_<n>_KUBERNETES_NODE from 1, with no gap")
    nodes = []
    for role, node in roles.items():
        n = target._node(role)
        entry: dict[str, Any] = {"node": node}
        _put(entry, "fabricInterface", discovery.value(env, n, "FABRIC_INTERFACE"))
        rdma: dict[str, Any] = {}
        gid = discovery.value(env, n, "RDMA_GID_INDEX")
        if gid:
            if re.fullmatch(r"[0-9]{1,3}", gid):
                rdma["gidIndex"] = int(gid)
            else:
                problems.append(f"{role}: the RDMA GID index must be a whole number")
        _put(rdma, "roceMode", discovery.value(env, n, "RDMA_ROCE_MODE"))
        _put(entry, "rdma", rdma)
        # An engine's own ports, as engines that share a node need, where they differ.
        own = {k: v for k, v in port_fields(f"NARWHAL_NODE_{n}_").items() if ports.get(k) != v}
        _put(entry, "ports", own)
        nodes.append(entry)
    _put(engines, "nodes", nodes)

    spec: dict[str, Any] = {"images": images, "engines": engines}
    if reference := env.get(CONTROL_TOKEN_SECRET_ENV):
        secret, _, key = reference.partition("/")
        spec["controlTokenSecret"] = {"name": secret, "key": key}
    slo: dict[str, Any] = {}
    for key, name in zip(("ttftS", "tpotS", "routerOverheadS"), SLO, strict=True):
        _put(slo, key, env.get(name))
    _put(spec, "slo", slo)
    _put(spec, "firstTokenMarginS", env.get(MARGIN))
    profiling: dict[str, Any] = {}
    for key, name in zip(
        ("prefillLens", "decodeInputLens", "decodeConcurrency"), PROFILE[:3], strict=True
    ):
        _put(profiling, key, _numbers(env, name, problems))
    _put(profiling, "warmupRequests", _number(env, PROFILE[3], problems))
    _put(spec, "profiling", profiling)
    calibration: dict[str, Any] = {}
    _put(calibration, "inputTokens", _numbers(env, CALIBRATION[0], problems))
    _put(calibration, "samples", _number(env, CALIBRATION[1], problems))
    _put(calibration, "observationTimeoutS", env.get(CALIBRATION[2]))
    _put(spec, "calibration", calibration)
    workload: dict[str, Any] = {}
    for (_, name, kind), key in zip(
        fabric.WORKLOAD,
        ("promptTokens", "handoffsPerS", "burst", "transferBudgetS", "headroom"),
        strict=True,
    ):
        _put(workload, key, _number(env, name, problems) if kind is int else env.get(name))
    fabric_block: dict[str, Any] = {}
    _put(fabric_block, "workload", workload)
    _put(fabric_block, "testPort", _number(env, fabric.TEST_PORT, problems))
    _put(spec, "fabric", fabric_block)
    _put(spec, "routers", _object(env, ROUTERS_ENV, problems))
    _put(spec, "haproxy", _object(env, HAPROXY_ENV, problems))
    timeouts: dict[str, Any] = {}
    _put(timeouts, "defaultSeconds", _number(env, TIMEOUT_ENV, problems))
    stages = {
        stage: seconds
        for stage in STAGES
        if (seconds := _number(env, stage_timeout_env(stage), problems)) is not None
    }
    _put(timeouts, "stages", stages)
    _put(spec, "timeouts", timeouts)
    if until is not None:
        spec["until"] = until
    # The controller stops a fleet only while `down` asks it to.
    spec["stopped"] = False

    launch, dropped = [], []
    consumed = set(_FIELDS) | set(_WORKSTATION) | {stage_timeout_env(stage) for stage in STAGES}
    for name, value in sorted(env.items()):
        if name in consumed or _PER_ENGINE.fullmatch(name) or target.fact_of(name) is None:
            continue
        if name in target.RETIRED or name in (API_KEY_ENV, ROUTER_TOKEN_ENV):
            continue
        if _SHARED.fullmatch(name):
            shared = name.split("_", 3)[3]
            if value != env.get(f"NARWHAL_{shared}", value):
                problems.append(
                    f"{name}: a NarwhalFleet gives every engine the same NARWHAL_{shared}; "
                    "unset the per-engine value"
                )
            continue
        if re.fullmatch(LAUNCH_REFUSED, name):
            dropped.append(name)
            continue
        launch.append({"name": name, "value": value})
    if len(launch) > fleet_resource.MAX_LAUNCH:
        problems.append(
            f"engines.launch holds at most {fleet_resource.MAX_LAUNCH} variables, not {len(launch)}"
        )
    _put(engines, "launch", launch)
    fleet = {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {"name": NAME, "namespace": env.get(target.NAMESPACE, "")},
        "spec": spec,
    }
    if not problems:
        problems.extend(_derived_problems(fleet, env))
    if problems:
        raise InputError(list(dict.fromkeys(problems)))
    return Built(fleet, tuple(dropped))


def permissive(fleets: Sequence[Mapping[str, Any]]) -> VolumePolicy:
    """A volume policy that admits the model volumes of `fleets`: the cluster admin's own
    allow-lists stay with the controller, which refuses a fleet outside them."""
    prefixes, servers, drivers = [], [], []
    for fleet in fleets:
        source = ((fleet.get("spec") or {}).get("engines") or {}).get("modelVolume") or {}
        if not isinstance(source, Mapping):
            continue
        for kind, settings in source.items():
            settings = settings if isinstance(settings, Mapping) else {}
            if kind == "hostPath" and fleet_resource._normalised(str(settings.get("path", ""))):
                prefixes.append(str(settings["path"]))
            elif kind == "nfs":
                servers.append(str(settings.get("server", "")))
            elif kind == "csi":
                drivers.append(str(settings.get("driver", "")))
    return VolumePolicy(MODEL_VOLUME_SOURCES, tuple(prefixes), tuple(servers), tuple(drivers))


def fleet_env(
    fleet: Mapping[str, Any], env: Mapping[str, str], policy: VolumePolicy
) -> dict[str, str]:
    """The variables the controller reads from `fleet`, as `settings_env` gives them, with the
    workstation's state claim and stand-ins for the controller's other settings."""
    stored = {
        **dict(fleet),
        "metadata": {
            **dict(fleet.get("metadata") or {}),
            "uid": (fleet.get("metadata") or {}).get("uid") or _PLACEHOLDER_UID,
        },
    }
    claim = env.get(target.STATE_CLAIM) or DEFAULT_STATE_CLAIM
    found = fleet_resource.spec_env(stored, state_claim=claim, policy=policy)
    found[target.ENGINE_ACCOUNT] = _ENGINE_ACCOUNT
    found[SIDECAR_IMAGE_ENV] = _NARWHAL_IMAGE
    return found


def _derived_problems(fleet: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    """What `kubernetes_target.inputs()` refuses in the fleet the controller would read, and
    the engine addresses the workstation sets that the fleet would serve elsewhere."""
    problems: list[str] = []
    try:
        derived = fleet_env(fleet, env, permissive([fleet]))
    except ValueError as error:
        return [str(error)]
    for name, value in env.items():
        address = re.fullmatch(r"NARWHAL_NODE_[1-9][0-9]*_(ATTESTATION_)?URL", name)
        if address and derived.get(name, value) != value:
            problems.append(
                f"{name}: the fleet serves this engine at {derived[name]}; unset it or set "
                "it to that"
            )
    try:
        target.inputs(derived, policy=permissive([fleet]))
    except InputError as error:
        problems.extend(error.problems)
    return problems


# Field managers.


def spec_managers(fleet: Mapping[str, Any] | None) -> list[str]:
    """The field managers other than `narwhal-cli`'s apply that own a field of the fleet's
    `spec`, such as Helm, Argo CD or Flux. The controller's status writes own no spec field."""
    found = []
    for entry in ((fleet or {}).get("metadata") or {}).get("managedFields") or []:
        if entry.get("subresource"):
            continue
        if "f:spec" not in (entry.get("fieldsV1") or {}):
            continue
        if entry.get("manager") == CLI_MANAGER and entry.get("operation") == "Apply":
            continue
        name = str(entry.get("manager") or "an unnamed manager")
        if entry.get("operation") != "Apply":
            name += f" ({entry.get('operation')})"
        found.append(name)
    return sorted(set(found))


def conflicts(error: KubernetesError) -> list[tuple[str, str]]:
    """The managers and fields an apply's conflict names, as the API server reports them."""
    return [(manager, path) for manager, path in _CONFLICT.findall(error.message)]


# Status.


def conditions(fleet: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    status = (fleet or {}).get("status") or {}
    return {str(item.get("type")): dict(item) for item in status.get("conditions") or []}


def summary(fleet: Mapping[str, Any]) -> dict[str, Any]:
    """The fleet's identity and kstatus conditions, as the command results report them."""
    metadata = fleet.get("metadata") or {}
    status = fleet.get("status") or {}
    found = conditions(fleet)
    return {
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "generation": metadata.get("generation"),
        "observed_generation": status.get("observedGeneration"),
        "stage": status.get("stage"),
        "engines": status.get("engines"),
        "serving": bool(status.get("serving")),
        "conditions": [
            {key: found[kind].get(key) for key in ("type", "status", "reason", "message")}
            for kind in fleet_resource.CONDITIONS
            if kind in found
        ],
    }


def _true(found: Mapping[str, Mapping[str, Any]], kind: str) -> bool:
    return (found.get(kind) or {}).get("status") == "True"


def _current(fleet: Mapping[str, Any]) -> bool:
    """Whether the status reports the fleet's current generation."""
    generation = int((fleet.get("metadata") or {}).get("generation") or 0)
    observed = int(((fleet.get("status") or {}).get("observedGeneration")) or 0)
    return observed >= generation


@dataclass(frozen=True)
class Followed:
    """Where following the fleet ended: `ready`, `stalled`, `stopped`, `gone` or `timeout`."""

    outcome: str
    fleet: dict[str, Any] | None
    stall: dict[str, Any] | None = None


@dataclass
class Exported:
    """What one export brought into the run directory."""

    files: int = 0
    rounds: int = 0
    large: list[str] = field(default_factory=list)
    unnamed: int = 0
    more: bool = False

    def data(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "jobs": self.rounds,
            "too_large": list(self.large),
            "unnamed": self.unnamed,
            "complete": not self.more,
        }


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Session:
    """One command's work on the fleet a run directory follows."""

    def __init__(
        self,
        directory: Path,
        env: Mapping[str, str],
        *,
        announce: Callable[[str], None] | None = None,
        logs: Path | None = None,
    ) -> None:
        """`logs` holds each kubectl call's log. By default each command logs to its own
        directory under `kubectl/` of the run directory, named by when it started."""
        self.directory = directory
        self.env = dict(env)
        if logs is None:
            started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            logs = directory / KUBECTL_LOGS / f"{started}-{os.getpid()}"
        self.ws: Workstation = connect(self.env, logs)
        self.announce = announce or (lambda line: print(line, flush=True))
        self.sleep: Callable[[float], None] = _sleep
        self.clock: Callable[[], float] = _clock

    # The run directory.

    def binding(self) -> dict[str, Any] | None:
        """The fleet the run directory follows, or None for a directory that follows none."""
        path = self.directory / FLEET_FILE
        if not path.is_file():
            return None
        found = json.loads(path.read_text())
        if not isinstance(found, dict) or not target.UID.fullmatch(str(found.get("uid", ""))):
            raise ValueError(f"{path} names no fleet; start a new run directory")
        return found

    def check_target(self) -> None:
        """Refuse a run directory that holds another target's run."""
        if (self.directory / run_record.RECORD).is_file() and self.binding() is None:
            record = run_record.read(self.directory)
            raise ValueError(
                f"{self.directory} holds a {record['target']} run that follows no "
                f"{KIND}; start a new run directory"
            )

    def check_bound(self, live: Mapping[str, Any] | None) -> None:
        """Refuse a fleet other than the one the run directory follows."""
        bound = self.binding()
        if bound is None:
            return
        namespace = self.env.get(target.NAMESPACE)
        if bound.get("namespace") != namespace:
            raise ValueError(
                f"{self.directory} follows the {KIND} in {bound.get('namespace')}, not in "
                f"{namespace}; load the environment the run used"
            )
        uid = ((live or {}).get("metadata") or {}).get("uid")
        if uid != bound["uid"]:
            found = f"holds the fleet {uid}" if live is not None else "holds no fleet"
            raise ValueError(
                f"{self.directory} follows the fleet {bound['uid']}, and {namespace} now "
                f"{found}; start a new run directory"
            )

    def bind(self, live: Mapping[str, Any]) -> None:
        if self.binding() is not None:
            return
        metadata = live["metadata"]
        document = {
            "namespace": metadata["namespace"],
            "name": metadata["name"],
            "uid": metadata["uid"],
            "context": self.env.get(target.CONTEXT, ""),
        }
        target._private(self.directory / FLEET_FILE, document)

    # The fleet.

    def live(self) -> dict[str, Any] | None:
        return self.ws.get(FLEET, NAME)

    def apply(self, fleet: Mapping[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        """Apply the fleet as `narwhal-cli`, or raise `FleetConflict` naming the fields
        another manager owns."""
        try:
            return self.ws.apply_fleet(fleet, dry_run=dry_run)
        except Conflict as error:
            found = conflicts(error)
            if not found:
                raise
            fields = ", ".join(f"{path} ({manager})" for manager, path in found)
            raise FleetConflict(
                f"other field managers own fields this apply sets: {fields}; change them "
                "through those managers, or remove their fields from the fleet",
                found,
            ) from None

    def retry(self, live: Mapping[str, Any]) -> str | None:
        """Ask the controller to run a fleet stalled on a failed stage again, by a new
        `requested-at` value; return when the stall began, to tell it from the next one."""
        stall = conditions(live).get(STALLED) or {}
        if stall.get("status") != "True" or stall.get("reason") != STAGE_FAILED:
            return None
        self.ws.annotate(FLEET, NAME, {REQUESTED_AT: _now()})
        self.announce(f"The fleet was stalled: {stall.get('message')}; it runs again.")
        return str(stall.get("lastTransitionTime") or "")

    def stop(self, live: Mapping[str, Any]) -> dict[str, Any]:
        """Apply the fleet's own spec with `spec.stopped` set; `narwhal-cli` owns the spec."""
        spec = dict(live.get("spec") or {})
        spec["stopped"] = True
        fleet = {
            "apiVersion": API_VERSION,
            "kind": KIND,
            "metadata": {"name": NAME, "namespace": live["metadata"]["namespace"]},
            "spec": spec,
        }
        return self.apply(fleet)

    def delete(self, uid: str) -> bool:
        return self.ws.delete(FLEET, NAME, uid=uid)

    def follow(
        self,
        *,
        uid: str,
        stopped: bool,
        timeout_s: float,
        stale_stall: str | None = None,
    ) -> Followed:
        """Follow the fleet's status until it is ready, stalled, stopped with `stopped`, gone,
        or `timeout_s` passes. Each stage's progress is printed as it changes, and the
        evidence is exported each time a stage instance passes. A stall whose transition time
        is `stale_stall` began before a retry, and is waited out."""
        deadline = self.clock() + timeout_s
        shown: dict[str, tuple[Any, ...]] = {}
        exported: tuple[Any, ...] | None = None
        while True:
            live = self.live()
            if live is None or (live.get("metadata") or {}).get("uid") != uid:
                return Followed("gone", live)
            status = live.get("status") or {}
            found = conditions(live)
            self._progress(status, found, shown)
            passed = tuple(
                sorted(
                    (stage, count.get("lastPassedTime"), count.get("passed"))
                    for stage, count in (status.get("stages") or {}).items()
                )
            )
            if passed and passed != exported:
                exported = passed
                self._export_quietly()
            if _current(live):
                if stopped and _true(found, STOPPED):
                    return Followed("stopped", live)
                if not stopped and _true(found, READY):
                    return Followed("ready", live)
                stall = found.get(STALLED) or {}
                if (
                    not stopped
                    and stall.get("status") == "True"
                    and (stale_stall is None or stall.get("lastTransitionTime") != stale_stall)
                ):
                    return Followed("stalled", live, stall)
            if self.clock() >= deadline:
                return Followed("timeout", live)
            self.sleep(POLL_SECONDS)

    def _progress(
        self,
        status: Mapping[str, Any],
        found: Mapping[str, Mapping[str, Any]],
        shown: dict[str, tuple[Any, ...]],
    ) -> None:
        counts = status.get("stages") or {}
        for stage in STAGES:
            condition = found.get(stage_condition(stage))
            if condition is None:
                continue
            count = counts.get(stage) or {}
            state = (condition.get("reason"), count.get("passed", 0), count.get("total", 0))
            if shown.get(stage) == state:
                continue
            shown[stage] = state
            reason, passed, total = state
            self.announce(f"{stage}: {str(reason).lower()} ({passed}/{total} passed)")

    def _export_quietly(self) -> None:
        try:
            self.export()
        except (ExportError, KubernetesError, OSError) as error:
            self.announce(f"Evidence not copied yet: {error}")

    # Evidence.

    def held(self, uid: str) -> dict[str, list[int]]:
        """The attempts the run directory holds whole, by stage instance directory."""
        path = self.directory / EXPORT_FILE
        if not path.is_file():
            return {}
        found = json.loads(path.read_text())
        held = found.get("held") if isinstance(found, dict) and found.get("uid") == uid else None
        if not isinstance(held, dict):
            return {}
        return {str(name): [int(n) for n in numbers] for name, numbers in held.items()}

    def _save_held(self, uid: str, held: Mapping[str, list[int]]) -> None:
        path = self.directory / EXPORT_FILE
        if path.is_symlink():
            raise ExportError(f"{path} is not a regular file of the run")
        pending = path.with_name(f".{path.name}.{os.getpid()}.export")
        with contextlib.suppress(FileNotFoundError):
            pending.unlink()
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"uid": uid, "held": {k: sorted(v) for k, v in held.items()}}, stream)
        os.replace(pending, path)

    def export(self) -> Exported:
        """Bring the controller's run record and every finished attempt the run directory
        lacks, one export Job at a time, each within `EXPORT_LOG_BYTES`."""
        check_directory(self.directory)
        live = self.live()
        if live is None:
            raise ExportError(f"{self.env.get(target.NAMESPACE)} holds no {KIND}")
        self.check_bound(live)
        if not (live.get("status") or {}).get("stages"):
            # The controller writes its run record as its first pass starts.
            return Exported()
        uid = live["metadata"]["uid"]
        held = self.held(uid)
        after = ""
        done = Exported()
        while done.rounds < EXPORT_ROUNDS:
            request = kubernetes_export.Request(uuid.uuid4().hex, held, after)
            files, report = self._export_round(request)
            done.rounds += 1
            self._write(files)
            done.files += len(files)
            done.large = sorted({*done.large, *report["large"]})
            done.unnamed = int(report["unnamed"])
            done.more = bool(report["more"])
            for attempt in report["complete"]:
                parent, _, number = attempt.rpartition("/attempt-")
                held.setdefault(parent, [])
                if int(number) not in held[parent]:
                    held[parent].append(int(number))
            # A file that did not fit is resumed after the last one sent of its attempt.
            after = str(report["last"]) if report["deferred"] else ""
            self._save_held(uid, held)
            if not done.more:
                break
        if done.files:
            try:
                run_record.read(self.directory)
            except ValueError as error:
                raise ExportError(f"the mirrored run record is not valid: {error}") from None
        return done

    def _export_round(
        self, request: kubernetes_export.Request
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        """Ask the controller for one export Job and read what its pod printed. While another
        export Job of the fleet runs, ask again under a new nonce, for as long as one export
        Job may run."""
        deadline = self.clock() + EXPORT_SECONDS + 2 * target.GRACE_SECONDS
        while True:
            try:
                value = request.value()
            except ValueError as error:
                raise ExportError(str(error)) from None
            self.ws.annotate(FLEET, NAME, {kubernetes_export.EXPORT_REQUEST: value})
            try:
                answer = self._answer(request.nonce)
                break
            except ExportBusy:
                if self.clock() >= deadline:
                    raise ExportError(
                        f"another export Job of the {KIND} ran for longer than "
                        f"{EXPORT_SECONDS} s; try again later"
                    ) from None
            self.sleep(kubernetes_export.EXPORT_INTERVAL_SECONDS)
            request = dataclasses.replace(request, nonce=uuid.uuid4().hex)
        name = str(answer["job"])
        log = self._job_log(answer)
        output = log.decode(errors="replace")
        try:
            files = target.gate_files(output)
        except ValueError as error:
            raise ExportError(f"export Job {name}: {error}") from None
        reports = [line for line in output.splitlines() if line.startswith(SUMMARY)]
        try:
            report = json.loads(reports[-1][len(SUMMARY) :])
            report = {
                "more": bool(report["more"]),
                "large": [str(item) for item in report["large"]],
                "unnamed": int(report["unnamed"]),
                "deferred": bool(report["deferred"]),
                "complete": [str(item) for item in report["complete"]],
                "last": str(report["last"]),
            }
        except (IndexError, ValueError, KeyError, TypeError):
            raise ExportError(f"export Job {name} reported no summary") from None
        for attempt in report["complete"]:
            parent, _, number = attempt.rpartition("/attempt-")
            if not kubernetes_export.INSTANCE_DIRECTORY.fullmatch(parent) or not number.isdigit():
                raise ExportError(f"export Job {name} reported {attempt!r} as an attempt")
        if report["last"] and not kubernetes_export.safe_path(report["last"]):
            raise ExportError(f"export Job {name} reported {report['last']!r} as a file")
        for path in files:
            if not exportable(path):
                raise ExportError(f"export Job {name} returned {path}, which no export writes")
        return files, report

    def _answer(self, nonce: str) -> dict[str, Any]:
        """The controller's answer to the export request `nonce`, from the fleet's status,
        once it names the pod that ran the Job and its log's digest."""
        deadline = self.clock() + EXPORT_SECONDS + 2 * target.GRACE_SECONDS
        while True:
            live = self.live()
            if live is None:
                raise ExportError(f"the {KIND} is gone")
            for entry in (live.get("status") or {}).get("exports") or []:
                if entry.get("nonce") != nonce:
                    continue
                if entry.get("error") == kubernetes_export.RUNNING:
                    raise ExportBusy(entry["error"])
                if entry.get("error"):
                    raise ExportError(f"the controller exported nothing: {entry['error']}")
                if all(entry.get(key) for key in ("job", "uid", "pod", "sha256")):
                    return dict(entry)
            if self.clock() >= deadline:
                raise ExportError(
                    f"the controller finished no export in {EXPORT_SECONDS} s; see its log"
                )
            self.sleep(JOB_POLL_SECONDS)

    def _job_log(self, answer: Mapping[str, Any]) -> bytes:
        """The log of the pod the controller read, when it is the log the controller read:
        a pod the Job of the answer's UID controls, whose log has the answer's SHA-256 and
        size. The workstation reads no Job."""
        name, pod_name = str(answer["job"]), str(answer["pod"])
        pod = self.ws.get(POD, pod_name) or {}
        metadata = pod.get("metadata") or {}
        if (
            not any(
                reference.get("kind") == "Job"
                and reference.get("uid") == answer["uid"]
                and reference.get("controller") is True
                for reference in metadata.get("ownerReferences") or []
            )
            or (metadata.get("labels") or {}).get(target.JOB_NAME_LABEL) != name
        ):
            raise ExportError(f"pod {pod_name} is not the pod of export Job {name}")
        log = self.ws.log(pod_name, kubernetes_export.EXPORT)
        if len(log) > EXPORT_LOG_BYTES:
            raise ExportError(f"export Job {name} logged more than {EXPORT_LOG_BYTES} bytes")
        if len(log) != answer.get("size") or hashlib.sha256(log).hexdigest() != answer["sha256"]:
            raise ExportError(
                f"pod {pod_name}'s log differs from the one the controller read; nothing of "
                "it is imported"
            )
        return log

    def _write(self, files: Mapping[str, bytes]) -> None:
        """Write the exported files into the run directory, each private and whole, the run
        record last, so it never names evidence the directory lacks. Only the run record is
        ever replaced: evidence the directory holds stays as it was copied."""
        check_directory(self.directory)
        for name in files:
            if not exportable(name):
                raise ExportError(f"{name} is no file an export writes")
        for name in sorted(files, key=lambda item: (item == run_record.RECORD, item)):
            parts = name.split("/")
            here = self.directory
            for part in parts[:-1]:
                here = here / part
                if here.is_symlink() or (here.exists() and not here.is_dir()):
                    raise ExportError(f"{here} is not a directory of the run")
                here.mkdir(mode=0o700, exist_ok=True)
            path = here / parts[-1]
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ExportError(f"{path} is not a regular file of the run")
            if path.exists() and name != run_record.RECORD:
                if path.read_bytes() == files[name]:
                    continue
                raise ExportError(f"{path} holds other evidence; an export never replaces it")
            pending = path.with_name(f".{path.name}.{os.getpid()}.export")
            with contextlib.suppress(FileNotFoundError):
                pending.unlink()
            descriptor = os.open(
                pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(files[name])
            os.replace(pending, path)

    # The controller's record, as the workstation reads it.

    def deployment(self, fleet: Mapping[str, Any]) -> KubernetesDeployment:
        """A dry deployment of the mirrored record, which reads the cluster and starts no Job,
        for the live checks of `plan` and `status`."""
        env = fleet_env(fleet, self.env, permissive([fleet]))
        for name in (target.CONTEXT, target.KUBECTL):
            if self.env.get(name):
                env[name] = self.env[name]
        deployment = KubernetesDeployment(
            self.directory,
            env,
            dry=True,
            cluster=lambda _env, _logs: self.ws,
        )
        deployment.announce = self.announce
        return deployment

    def plan(
        self, fleet: Mapping[str, Any], live: Mapping[str, Any] | None, *, until: str
    ) -> tuple[list[Decision], list[str]]:
        """What `run` would have the controller run for `fleet`, decided from the inputs that
        change against the live fleet and from the mirrored run record.

        The controller keys its record's digests under a salt that stays in the cluster, so
        the workstation compares the two fleets' inputs itself: each changed input reruns the
        stages the graph names for it, and every stage instance that has not passed runs.
        """
        order = STAGES[: STAGES.index(until) + 1]
        record = None
        if (self.directory / run_record.RECORD).is_file():
            record = run_record.read(self.directory)
        notes: list[str] = []
        reruns: dict[str, list[str]] = {}
        if live is None:
            changed = ["the namespace holds no fleet yet"]
        else:
            policy = permissive([fleet, live])
            try:
                before = target.inputs(fleet_env(live, self.env, policy), policy=policy)
            except (InputError, ValueError):
                before = {}
            after = target.inputs(fleet_env(_stored(fleet, live), self.env, policy), policy=policy)
            changed = []
            for name, value in after.items():
                if name in before and before[name].digest == value.digest:
                    continue
                for rerun in KUBERNETES_GRAPH.reruns(name):
                    reruns.setdefault(rerun.stage, []).append(f"{name} changed")
            if not _current(live):
                notes.append(
                    "the controller has not started a pass for the fleet's latest edit; the "
                    "plan compares with the run record of its last pass"
                )
        entries = list((record or {}).get("stages", {}).values()) if live is not None else []
        decisions = []
        recorded = {entry["stage"] for entry in entries}
        for position, stage in enumerate(order):
            found = [entry for entry in entries if entry["stage"] == stage]
            if not found and recorded & set(STAGES[position + 1 :]):
                # A pass that went on past the stage found no instance of it, such as
                # transfer-measure on a backend that measures no handoffs.
                continue
            keys = [run_record.key(stage, entry["instance"]) for entry in found] or [stage]
            for index, name in enumerate(keys):
                reasons = list(changed or reruns.get(stage, []))
                if not found:
                    reasons = reasons or ["it has not run"]
                elif found[index]["status"] != run_record.PASSED:
                    reasons.append(f"its last attempt is {found[index]['status']}")
                decisions.append(Decision(name, bool(reasons), tuple(reasons)))
        if record is not None:
            notes.append(
                f"the plan reads the run record mirrored in {self.directory}, which narwhal "
                "deploy status copies again; a stage that reruns can change what it produces "
                "and rerun the stages after it, and the controller checks the live pods and "
                "routes on each pass"
            )
        return decisions, notes


def _stored(fleet: Mapping[str, Any], live: Mapping[str, Any] | None) -> dict[str, Any]:
    """`fleet` with the live fleet's metadata, as the API server would store it."""
    if live is None:
        return dict(fleet)
    return {**dict(fleet), "metadata": dict(live.get("metadata") or {})}


def failure_of(directory: Path) -> tuple[str, dict[str, Any]] | None:
    """The first failed or timed-out stage instance of the mirrored record, with its entry."""
    if not (directory / run_record.RECORD).is_file():
        return None
    record = run_record.read(directory)
    for name, entry in record["stages"].items():
        if entry["status"] in (run_record.FAILED, run_record.TIMEOUT) and entry["failure"]:
            return name, entry
    return None


def exportable(name: str) -> bool:
    """Whether an export may write `name`: a run-relative file of plain parts, none of the
    workstation's own files or the controller's secrets, compared without case, since a
    workstation's file system may ignore it."""
    if not kubernetes_export.safe_path(name):
        return False
    parts = name.split("/")
    reserved = {item.lower() for item in (*LOCAL_ONLY, *NEVER_EXPORTED)}
    if parts[0].lower() in reserved:
        return False
    if any(re.fullmatch(TEMPORARY, part) for part in parts):
        return False
    return name == run_record.RECORD or (
        len(parts) > 3 and parts[0] == "stages" and any(p.startswith("attempt-") for p in parts)
    )


def stall_result(stall: Mapping[str, Any], failed: Any) -> tuple[str, str, int]:
    """The command result of a stalled fleet: a stage's rejected check is a failed gate, a
    refused input an invalid one, and anything else an error."""
    reason = str(stall.get("reason") or "")
    if reason in _INPUT_STALLS:
        return "invalid_input", "invalid_input", 2
    if reason == STAGE_FAILED and failed is not None:
        _, entry = failed
        if entry["status"] == run_record.TIMEOUT:
            return "error", "stage_timeout", 4
        if (entry["failure"].get("context") or {}).get("exit_status") == 1:
            return "failed_gate", "gate_failed", 1
    return "error", "operation_failed", 4


@contextlib.contextmanager
def locked(directory: Path) -> Iterator[None]:
    """The run directory's lock, which keeps two workstation commands from writing the mirror
    at once."""
    with run_record.locked(directory):
        yield
