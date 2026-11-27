"""The Kubernetes objects the fleet controller derives from a `NarwhalFleet`.

Each builder returns complete objects for server-side apply: the engines' StatefulSet, Service
and hook ConfigMaps, a gate Job and its inputs ConfigMap, and HAProxy. They carry the pod specs
the chart's templates rendered before the controller took them over.

Every object names the fleet as its controlling owner, so deleting the fleet leaves its removal
to the garbage collector. The owner reference sets no `blockOwnerDeletion`: nothing waits on a
foreground deletion, and setting it would need `update` on the fleet's finalizers.

Labels and annotations come from an allow-list and never from the fleet's own metadata, so a
GitOps tool that tracks the fleet by a label or annotation never sees the derived objects as
its own. `check_metadata` refuses any other key, and every key in `NEVER_COPIED`, wherever an
object holds metadata.

`prune` names the objects the fleet controls that the desired set no longer holds. Every
deletion sends `propagationPolicy: Background`, as a Job deleted with the API's default
orphans its pods.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from narwhal import __version__

from .fleet_resource import GROUP, KIND, VERSION
from .launch_engine import engine_set
from .launch_engine.kubernetes import MODEL_VOLUME_SOURCES

API_VERSION = f"{GROUP}/{VERSION}"
# The field manager of every apply, and the value of the managed-by label.
MANAGER = "narwhal-controller"

NAME_LABEL = "app.kubernetes.io/name"
COMPONENT_LABEL = "app.kubernetes.io/component"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
VERSION_LABEL = "app.kubernetes.io/version"
GATE_LABEL = f"{GROUP}/gate"
RUN_LABEL = f"{GROUP}/run"
PLACEMENT_GATE = f"{GROUP}/placement"
CONFIG_ANNOTATION = f"{GROUP}/config-sha256"
# The only keys a derived object's labels and annotations may hold.
ALLOWED_LABELS = (NAME_LABEL, COMPONENT_LABEL, MANAGED_BY_LABEL, VERSION_LABEL)
ALLOWED_PREFIX = f"{GROUP}/"
# Keys a derived object must never carry, whatever the fleet holds: Helm's, Argo CD's and
# Flux's tracking metadata. A `(key, value)` entry refuses that value only.
NEVER_COPIED: tuple[str | tuple[str, str], ...] = (
    "app.kubernetes.io/instance",
    (MANAGED_BY_LABEL, "Helm"),
    "helm.sh/chart",
    "argocd.argoproj.io/",
    "meta.helm.sh/",
    "kustomize.toolkit.fluxcd.io/",
    "helm.toolkit.fluxcd.io/",
)

ENGINE = "engine"
GATE = "gate"
HAPROXY = "haproxy"
ROUTER_SERVICE = "routers"
ROUTER_PODS = ("router-0", "router-1")
HAPROXY_PORT = 8000
# Services the controller names itself, which the engines' headless Service may not reuse.
RESERVED_SERVICES = (ROUTER_SERVICE, HAPROXY)

# Gate Jobs, as `kubernetes_target` runs them.
GATE_KINDS = (
    "discovery",
    "prepare",
    "engine-check",
    "state",
    "fabric-probe",
    "transfer-measure",
    "fabric-server",
    "fabric-client",
    "sidecar-verify",
    "finalize-fleet",
    "profile",
    "calibrate",
    "preflight",
    "router-check",
)
# The gates that call engines, or prepare the launch whose checks read the key, get the key.
CREDENTIAL_KINDS = (
    "prepare",
    "transfer-measure",
    "finalize-fleet",
    "profile",
    "calibrate",
    "preflight",
)
# The gates that inspect or measure one node run there.
NODE_KINDS = ("discovery", "fabric-probe", "fabric-server", "fabric-client", "transfer-measure")
# The one gate that reads the engine pods through the API, as their sidecars do.
ACCOUNT_KIND = "sidecar-verify"
KEEP_ENV = "NARWHAL_GATE_KEEP"
KEEP_MOUNT = "/narwhal-keep"
STATE_MOUNT = "/narwhal-state"
INPUT_MOUNT = "/narwhal-input"
MODEL_MOUNT = "/model"
INPUT_VOLUME = "narwhal-input"
STATE_VOLUME = engine_set.STATE_VOLUME
MODEL_VOLUME = engine_set.MODEL_VOLUME
# A ConfigMap holds at most 1 MiB.
INPUTS_BYTES = 1024 * 1024

DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
DNS_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RESOURCE_NAME = re.compile(
    r"([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?"
)
QUANTITY = re.compile(r"[0-9]+(\.[0-9]+)?([KMGTPE]i?)?")
LABEL_VALUE = re.compile(r"(([A-Za-z0-9][-A-Za-z0-9_.]{0,61})?[A-Za-z0-9])?")
# A gate's keep directory on the state volume: under deploy/, with no hidden or parent part.
KEEP = re.compile(r"deploy(/[A-Za-z0-9][A-Za-z0-9._-]*)+")
FILE_NAME = re.compile(r"[-._A-Za-z0-9]+")

BACKGROUND = "Background"
# What prune may delete, in the order it deletes them: workloads before what they mount.
PRUNED_KINDS = ("Deployment", "StatefulSet", "Job", "Service", "ConfigMap")

RESOLVERS = """resolvers kubernetes
    parse-resolv-conf
    resolve_retries 3
    timeout resolve 1s
    timeout retry 1s
    hold valid 1s
    hold obsolete 1s
    hold nx 1s
    hold refused 1s
    hold timeout 1s
    hold other 1s
"""


def _name(value: object, what: str, pattern: re.Pattern[str] = DNS_NAME) -> str:
    if not isinstance(value, str) or len(value) > 253 or not pattern.fullmatch(value):
        raise ValueError(f"{what} must be a Kubernetes name, not {value!r}")
    return value


def _digest(value: object, what: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError(f"{what} must name an image by registry digest, not {value!r}")
    return value


# Owners and metadata.


@dataclass(frozen=True)
class Owner:
    """The fleet every derived object names as its controlling owner."""

    name: str
    uid: str
    namespace: str

    def __post_init__(self) -> None:
        _name(self.name, "the fleet's name")
        _name(self.namespace, "the fleet's namespace", DNS_LABEL)
        if not isinstance(self.uid, str) or not self.uid:
            raise ValueError("the fleet has no uid; build objects only for a stored fleet")

    @classmethod
    def of(cls, fleet: Mapping[str, Any]) -> Owner:
        """The owner of a fleet as the API returns it. Only its name, uid and namespace are
        read; its labels and annotations stay with it."""
        if fleet.get("apiVersion") != API_VERSION or fleet.get("kind") != KIND:
            raise ValueError(f"expected a {API_VERSION} {KIND}")
        metadata = fleet.get("metadata") or {}
        return cls(metadata.get("name", ""), metadata.get("uid", ""), metadata.get("namespace", ""))

    def reference(self) -> dict[str, Any]:
        return {
            "apiVersion": API_VERSION,
            "kind": KIND,
            "name": self.name,
            "uid": self.uid,
            "controller": True,
        }


def labels(component: str, **extra: str) -> dict[str, str]:
    """The selector labels of one component, which pod templates carry, and `extra` keys under
    the group."""
    found = {NAME_LABEL: "narwhal", COMPONENT_LABEL: component}
    found.update({f"{ALLOWED_PREFIX}{key}": value for key, value in extra.items()})
    return found


def _version() -> str:
    """The installed Narwhal version as a label value, which allows no `+`."""
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", __version__)[:63]
    return value.strip("_.-") or "unknown"


def metadata(
    owner: Owner,
    name: str,
    component: str,
    *,
    extra: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """An object's metadata: its name and namespace, the owner reference, and labels and
    annotations from the allow-list only."""
    found = labels(component, **dict(extra or {}))
    found[MANAGED_BY_LABEL] = MANAGER
    found[VERSION_LABEL] = _version()
    document: dict[str, Any] = {
        "name": name,
        "namespace": owner.namespace,
        "labels": found,
        "ownerReferences": [owner.reference()],
    }
    if annotations:
        document["annotations"] = dict(annotations)
    _check_keys(document, name)
    return document


def _denied(key: str, value: object) -> bool:
    for entry in NEVER_COPIED:
        if isinstance(entry, tuple):
            if (key, value) == entry:
                return True
        elif key == entry or (entry.endswith("/") and key.startswith(entry)):
            return True
    return False


def _check_keys(document: Mapping[str, Any], where: str) -> None:
    for field_name in ("labels", "annotations"):
        for key, value in (document.get(field_name) or {}).items():
            if _denied(key, value):
                raise ValueError(f"{where}: {field_name} may never hold {key}={value}")
            allowed = key.startswith(ALLOWED_PREFIX) or (
                field_name == "labels" and key in ALLOWED_LABELS
            )
            if not allowed:
                raise ValueError(f"{where}: {field_name} may not hold {key}")
            if field_name == "labels" and not (
                isinstance(value, str) and LABEL_VALUE.fullmatch(value)
            ):
                raise ValueError(f"{where}: label {key} has the invalid value {value!r}")


def _metadata_blocks(document: Any, path: str = "") -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every metadata mapping an object holds: its own, its pod template's and any other
    template's."""
    if isinstance(document, Mapping):
        for key, value in document.items():
            here = f"{path}.{key}" if path else key
            if key == "metadata" and isinstance(value, Mapping):
                yield here, value
            else:
                yield from _metadata_blocks(value, here)
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from _metadata_blocks(value, f"{path}[{index}]")


def check_metadata(document: Mapping[str, Any]) -> None:
    """Refuse an object whose labels or annotations, anywhere in it, hold a key outside the
    allow-list or in `NEVER_COPIED`."""
    name = f"{document.get('kind')} {(document.get('metadata') or {}).get('name')}"
    for path, block in _metadata_blocks(document):
        _check_keys(block, f"{name} {path}")


def controlled(owner: Owner, document: Mapping[str, Any]) -> bool:
    """Whether the fleet is the object's controlling owner."""
    references = (document.get("metadata") or {}).get("ownerReferences") or []
    return any(
        reference.get("uid") == owner.uid
        and reference.get("kind") == KIND
        and str(reference.get("apiVersion", "")).split("/")[0] == GROUP
        and reference.get("controller") is True
        for reference in references
    )


def _finished(document: dict[str, Any]) -> dict[str, Any]:
    check_metadata(document)
    return document


# Fleet settings.


@dataclass(frozen=True)
class ApiKey:
    """The engines' API credential: a key in a Secret, set in the variable `env`."""

    env: str
    secret: str
    key: str

    def __post_init__(self) -> None:
        if not ENV_NAME.fullmatch(self.env):
            raise ValueError(f"the API key variable {self.env!r} is not a variable name")
        _name(self.secret, "the API key Secret")
        if not FILE_NAME.fullmatch(self.key):
            raise ValueError(f"the API key's Secret key {self.key!r} is not a key name")

    def reference(self) -> dict[str, str]:
        return {"name": self.secret, "key": self.key}

    def variable(self) -> dict[str, Any]:
        return {"name": self.env, "valueFrom": {"secretKeyRef": self.reference()}}


@dataclass(frozen=True)
class Fleet:
    """What every derived object of one fleet shares.

    `engine_account` is the engines' ServiceAccount, which the chart makes. `rdma_resource` and
    `model_volume` are the engines' RDMA resource name and model volume source, which fabric
    and discovery gates use.
    """

    owner: Owner
    narwhal_image: str
    state_claim: str
    haproxy_image: str | None = None
    fabric_image: str | None = None
    api_key: ApiKey | None = None
    engine_account: str | None = None
    rdma_resource: str | None = None
    model_volume: Mapping[str, Any] | None = None
    cluster_domain: str = "cluster.local"
    haproxy_replicas: int = 1

    def __post_init__(self) -> None:
        _digest(self.narwhal_image, "the Narwhal image")
        for image, what in ((self.haproxy_image, "HAProxy"), (self.fabric_image, "fabric")):
            if image is not None:
                _digest(image, f"the {what} image")
        _name(self.state_claim, "the state claim")
        _name(self.cluster_domain, "the cluster domain")
        if self.engine_account is not None:
            _name(self.engine_account, "the engines' ServiceAccount")
        if self.rdma_resource is not None and not RESOURCE_NAME.fullmatch(self.rdma_resource):
            raise ValueError(f"{self.rdma_resource!r} is not a resource name")
        if self.model_volume is not None:
            _volume_source(self.model_volume, "the model volume")
        if not isinstance(self.haproxy_replicas, int) or self.haproxy_replicas < 1:
            raise ValueError("HAProxy needs at least one replica")


def _volume_source(source: Mapping[str, Any], what: str) -> None:
    if (
        not isinstance(source, Mapping)
        or len(source) != 1
        or next(iter(source)) not in MODEL_VOLUME_SOURCES
        or not isinstance(next(iter(source.values())), Mapping)
    ):
        raise ValueError(f"{what} must be one of {', '.join(MODEL_VOLUME_SOURCES)}")


# Engines.


def engine_pod(values: Mapping[str, Any]) -> dict[str, Any]:
    """The engine pod: the role-neutral template `engine_set.chart_values` built, with the
    engine image, resource limits and model volume set where it holds None."""
    spec = copy.deepcopy(dict(values["podTemplate"]))
    limits = {values["gpuResource"]: str(values["gpus"]), values["rdmaResource"]: "1"}
    for container in [*spec["initContainers"], *spec["containers"]]:
        if "image" in container and container["image"] is None:
            container["image"] = values["image"]
        if "resources" in container and container["resources"] is None:
            container["resources"] = {"limits": dict(limits)}
    for volume in spec["volumes"]:
        if volume["name"] == MODEL_VOLUME:
            volume.update(copy.deepcopy(dict(values["modelVolume"])))
    return spec


def engines(
    fleet: Fleet, values: Mapping[str, Any], placement: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """The engines' hook ConfigMaps, headless Service and StatefulSet.

    `values` are `engine_set.chart_values` of every engine's launch. With `placement`, which
    must name a node for every engine role, each pod waits at the placement scheduling gate
    until the target pins it to that node.
    """
    owner = fleet.owner
    engine_set.check(
        values,
        namespace=owner.namespace,
        state_claim=fleet.state_claim,
        narwhal_image=fleet.narwhal_image,
        api_key_secret=fleet.api_key.reference() if fleet.api_key else None,
    )
    service = _name(values["serviceName"], "the engines' Service", DNS_LABEL)
    if service in RESERVED_SERVICES:
        raise ValueError(f"the engines' headless Service may not be named {service}")
    spec = engine_pod(values)
    account = spec.get("serviceAccountName")
    if account != fleet.engine_account:
        raise ValueError(
            f"the engine pods run as ServiceAccount {account} and the fleet's engines run as "
            f"{fleet.engine_account}; prepare the engines with the fleet's ServiceAccount"
        )
    count = int(values["count"])
    if placement:
        roles = [f"engine-{n}" for n in range(1, count + 1)]
        for role in roles:
            if role not in placement:
                raise ValueError(
                    f"placement names no node for {role}; every engine pod runs on the node "
                    "discovery measured it on"
                )
            _name(placement[role], f"{role}'s node")
        spec["schedulingGates"] = [{"name": PLACEMENT_GATE}]
    documents: list[dict[str, Any]] = []
    for item in values["configMaps"]:
        name = _name(item["metadata"]["name"], "an engine ConfigMap")
        document: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata(owner, name, ENGINE),
        }
        document.update({k: item[k] for k in ("immutable", "data", "binaryData") if k in item})
        documents.append(document)
    selector = labels(ENGINE)
    documents.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata(owner, service, ENGINE),
            "spec": {
                "clusterIP": "None",
                # The gate Jobs and routers reach an engine by its pod name before it is ready.
                "publishNotReadyAddresses": True,
                "selector": selector,
            },
        }
    )
    documents.append(
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            # The pods are engine-1 onwards, the names of the engine roles.
            "metadata": metadata(owner, engine_set.STATEFULSET, ENGINE),
            "spec": {
                "serviceName": service,
                "replicas": count,
                "ordinals": {"start": 1},
                "podManagementPolicy": "Parallel",
                # Only a drain and readmission replaces an engine.
                "updateStrategy": {"type": "OnDelete"},
                "selector": {"matchLabels": selector},
                "template": {"metadata": {"labels": dict(selector)}, "spec": spec},
            },
        }
    )
    return [_finished(document) for document in documents]


# Gate Jobs.


def job_name(kind: str, uid: str, stage: str, instance: str, attempt: int, index: int) -> str:
    """The Job of gate `index` in one attempt of a stage instance, the same on every replay.

    A replayed action meets its Job under this name, so a create that conflicts adopts it.
    """
    if kind not in GATE_KINDS:
        raise ValueError(f"{kind!r} is no gate kind")
    seed = json.dumps([uid, stage, instance, attempt, index], separators=(",", ":"))
    return f"{kind}-{hashlib.sha256(seed.encode()).hexdigest()[:12]}"


@dataclass(frozen=True)
class GateJob:
    """One gate Job: its kind, command and time limit, its keep directory on the state volume,
    where it runs and what it mounts.

    `inputs` names the ConfigMap mounted read-only at /narwhal-input. A Narwhal gate mounts the
    whole state volume writable at /narwhal-state; a fabric gate runs the third-party fabric
    image, so it mounts only its keep directory and keeps no capability but IPC_LOCK.
    """

    name: str
    kind: str
    command: Sequence[str]
    deadline_seconds: int
    keep: str
    node: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    resources: Mapping[str, str] = field(default_factory=dict)
    image: str | None = None
    runtime_class: str | None = None
    host_network: bool = False
    inputs: str | None = None
    model_volume: Mapping[str, Any] | None = None
    service_account: bool = False
    run: str | None = None

    def __post_init__(self) -> None:
        _name(self.name, "a Job's name", DNS_LABEL)
        if self.kind not in GATE_KINDS:
            raise ValueError(f"{self.name}: {self.kind!r} is no gate kind")
        if not self.command or not all(isinstance(part, str) for part in self.command):
            raise ValueError(f"{self.name}: the command must be a list of strings")
        if not isinstance(self.deadline_seconds, int) or self.deadline_seconds < 1:
            raise ValueError(f"{self.name}: the time limit must be a positive whole second")
        if not isinstance(self.keep, str) or not KEEP.fullmatch(self.keep):
            raise ValueError(
                f"{self.name}: keep must be a directory under deploy/, not {self.keep!r}"
            )
        if self.node is not None:
            _name(self.node, f"{self.name}'s node")
        elif self.kind in NODE_KINDS:
            raise ValueError(
                f"{self.name}: a {self.kind} Job runs on the node it inspects; name it"
            )
        for name, value in self.env.items():
            if not ENV_NAME.fullmatch(name) or not isinstance(value, str):
                raise ValueError(f"{self.name}: invalid variable {name!r}")
            if name == KEEP_ENV:
                raise ValueError(f"{self.name}: {KEEP_ENV} is the Job's own")
        for name, quantity in self.resources.items():
            if not RESOURCE_NAME.fullmatch(name) or not QUANTITY.fullmatch(str(quantity)):
                raise ValueError(f"{self.name}: invalid resource {name}={quantity!r}")
        if self.image is not None:
            _digest(self.image, f"{self.name}'s image")
        if self.runtime_class is not None:
            _name(self.runtime_class, f"{self.name}'s runtime class")
        if self.inputs is not None:
            _name(self.inputs, f"{self.name}'s inputs ConfigMap")
        if self.model_volume is not None:
            _volume_source(self.model_volume, f"{self.name}'s model volume")
        if self.service_account and self.kind != ACCOUNT_KIND:
            raise ValueError(
                f"{self.name}: only a {ACCOUNT_KIND} Job runs as the engines' ServiceAccount"
            )
        if self.run is not None and not LABEL_VALUE.fullmatch(self.run):
            raise ValueError(f"{self.name}: the run id {self.run!r} is no label value")


def _gate_labels(gate: GateJob) -> dict[str, str]:
    extra = {"gate": gate.kind}
    if gate.run:
        extra["run"] = gate.run
    return extra


def job(fleet: Fleet, gate: GateJob) -> dict[str, Any]:
    """The Job of one gate, which runs once within its time limit.

    Only the gates that call engines get the engine API key, and only a sidecar-verify Job
    gets a ServiceAccount token.
    """
    fabric = gate.kind.startswith("fabric-")
    # A probe reads the host's interfaces and routes; a sample opens the RDMA device.
    rdma = fabric and gate.kind != "fabric-probe"
    if fabric and not fleet.fabric_image:
        raise ValueError(f"gate {gate.name} needs the fabric image, the image with ib_write_bw")
    if rdma and not fleet.rdma_resource:
        raise ValueError(f"gate {gate.name} measures the engines' RDMA resource; name it first")
    if gate.service_account and not fleet.engine_account:
        raise ValueError(f"gate {gate.name} runs as the engines' ServiceAccount; name it first")
    model = gate.model_volume
    # Discovery reads the engines' model volume unless the gate names one.
    if model is None and gate.kind == "discovery":
        model = fleet.model_volume
    env: list[dict[str, Any]] = [{"name": KEEP_ENV, "value": KEEP_MOUNT}]
    env += [{"name": name, "value": gate.env[name]} for name in sorted(gate.env)]
    if fleet.api_key and gate.kind in CREDENTIAL_KINDS:
        env.append(fleet.api_key.variable())
    container: dict[str, Any] = {
        "name": GATE,
        "image": gate.image or (fleet.fabric_image if fabric else fleet.narwhal_image),
        "command": list(gate.command),
        "env": env,
    }
    if rdma or gate.resources:
        limits: dict[str, str] = {}
        if rdma:
            assert fleet.rdma_resource is not None
            limits[fleet.rdma_resource] = "1"
        limits.update({name: str(gate.resources[name]) for name in sorted(gate.resources)})
        container["resources"] = {"limits": limits}
    if fabric:
        capabilities: dict[str, list[str]] = {"drop": ["ALL"]}
        if rdma:
            # RDMA memory registration locks memory.
            capabilities["add"] = ["IPC_LOCK"]
        container["securityContext"] = {
            "allowPrivilegeEscalation": False,
            "capabilities": capabilities,
        }
    mounts: list[dict[str, Any]] = [
        {"name": STATE_VOLUME, "mountPath": KEEP_MOUNT, "subPath": gate.keep}
    ]
    if not fabric:
        mounts.append({"name": STATE_VOLUME, "mountPath": STATE_MOUNT})
    if gate.inputs:
        mounts.append({"name": INPUT_VOLUME, "mountPath": INPUT_MOUNT, "readOnly": True})
    if model:
        mounts.append({"name": MODEL_VOLUME, "mountPath": MODEL_MOUNT, "readOnly": True})
    container["volumeMounts"] = mounts
    volumes: list[dict[str, Any]] = [
        {"name": STATE_VOLUME, "persistentVolumeClaim": {"claimName": fleet.state_claim}}
    ]
    if gate.inputs:
        volumes.append({"name": INPUT_VOLUME, "configMap": {"name": gate.inputs}})
    if model:
        volumes.append({"name": MODEL_VOLUME, **copy.deepcopy(dict(model))})
    spec: dict[str, Any] = {"restartPolicy": "Never", "enableServiceLinks": False}
    if gate.service_account:
        # Verifying a sidecar reads the engine pod through the API, as the sidecar does.
        spec["serviceAccountName"] = fleet.engine_account
        spec["automountServiceAccountToken"] = True
    else:
        spec["automountServiceAccountToken"] = False
    if gate.node:
        spec["nodeName"] = gate.node
    if gate.runtime_class:
        spec["runtimeClassName"] = gate.runtime_class
    if fabric or gate.host_network:
        # A fabric sample runs on the host's fabric interface, as KV does, and discovery reads
        # the host's interfaces.
        spec["hostNetwork"] = True
        spec["dnsPolicy"] = "ClusterFirstWithHostNet"
    spec["containers"] = [container]
    spec["volumes"] = volumes
    extra = _gate_labels(gate)
    return _finished(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": metadata(fleet.owner, gate.name, GATE, extra=extra),
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": gate.deadline_seconds,
                "template": {"metadata": {"labels": labels(GATE, **extra)}, "spec": spec},
            },
        }
    )


def inputs(fleet: Fleet, gate: GateJob, files: Mapping[str, bytes]) -> dict[str, Any]:
    """The ConfigMap a gate's `inputs` names, holding `files`."""
    if gate.inputs is None:
        raise ValueError(f"gate {gate.name} names no inputs ConfigMap")
    for name in files:
        if not FILE_NAME.fullmatch(name):
            raise ValueError(f"gate {gate.name}: {name!r} is not a ConfigMap key")
    data = {name: base64.b64encode(files[name]).decode() for name in sorted(files)}
    if sum(len(name) + len(value) for name, value in data.items()) > INPUTS_BYTES:
        raise ValueError(f"gate {gate.name}: its inputs exceed a ConfigMap's 1 MiB")
    return _finished(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata(fleet.owner, gate.inputs, GATE, extra=_gate_labels(gate)),
            "binaryData": data,
        }
    )


# HAProxy.


def haproxy_config(namespace: str, cluster_domain: str) -> str:
    """deploy/ha/haproxy.cfg, with each router found by its pod's DNS name. A replaced router
    pod is found at its new address within a second."""
    config = resources.files("narwhal.deployment").joinpath("haproxy.cfg").read_text()
    domain = f"{ROUTER_SERVICE}.{namespace}.svc.{cluster_domain}"
    for server, pod in zip("ab", ROUTER_PODS, strict=True):
        placeholder = f"server router-{server} ROUTER_{server.upper()}_ADDRESS:{HAPROXY_PORT}"
        if placeholder not in config:
            raise ValueError(f"deploy/ha/haproxy.cfg has no line {placeholder!r}")
        address = f"server router-{server} {pod}.{domain}:{HAPROXY_PORT}"
        config = config.replace(placeholder, f"{address} resolvers kubernetes init-addr none")
    return f"{config}\n{RESOLVERS}"


def haproxy(fleet: Fleet) -> list[dict[str, Any]]:
    """HAProxy's ConfigMap, Deployment and Service in front of the router pair."""
    if fleet.haproxy_image is None:
        raise ValueError("HAProxy needs its image")
    owner = fleet.owner
    config = haproxy_config(owner.namespace, fleet.cluster_domain)
    selector = labels(HAPROXY)
    digest = hashlib.sha256(config.encode()).hexdigest()
    documents: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata(owner, HAPROXY, HAPROXY),
            "data": {"haproxy.cfg": config},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata(owner, HAPROXY, HAPROXY),
            "spec": {
                "replicas": fleet.haproxy_replicas,
                "selector": {"matchLabels": selector},
                "template": {
                    # A changed configuration rolls the pods.
                    "metadata": {
                        "labels": dict(selector),
                        "annotations": {CONFIG_ANNOTATION: digest},
                    },
                    "spec": {
                        "enableServiceLinks": False,
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": HAPROXY,
                                "image": fleet.haproxy_image,
                                "ports": [{"name": "http", "containerPort": HAPROXY_PORT}],
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": "/usr/local/etc/haproxy",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [{"name": "config", "configMap": {"name": HAPROXY}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata(owner, HAPROXY, HAPROXY),
            "spec": {
                "selector": selector,
                "ports": [{"name": "http", "port": HAPROXY_PORT, "targetPort": "http"}],
            },
        },
    ]
    return [_finished(document) for document in documents]


# Pruning.


@dataclass(frozen=True)
class Deletion:
    """One object to delete, and the delete options to send with it.

    Every deletion propagates in the background, so a deleted Job takes its pods with it. The
    uid precondition keeps a deletion from reaching a newer object of the same name.
    """

    kind: str
    name: str
    uid: str | None = None

    def options(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": BACKGROUND,
        }
        if self.uid:
            document["preconditions"] = {"uid": self.uid}
        return document


def mounted_maps(pods: Iterable[Mapping[str, Any]]) -> set[str]:
    """The ConfigMaps the pods mount, directly or through a projected volume.

    An engine pod keeps the spec it started with until engine-start replaces it, so the hook
    ConfigMaps it mounts stay until then.
    """
    names = set()
    for pod in pods:
        for volume in (pod.get("spec") or {}).get("volumes") or []:
            sources = [volume.get("configMap")] + [
                source.get("configMap")
                for source in (volume.get("projected") or {}).get("sources") or []
            ]
            names |= {source["name"] for source in sources if source and source.get("name")}
    return names


def prune(
    owner: Owner,
    desired: Iterable[Mapping[str, Any]],
    live: Iterable[Mapping[str, Any]],
    pods: Iterable[Mapping[str, Any]] = (),
) -> list[Deletion]:
    """The live objects the fleet controls that `desired` no longer holds, in deletion order.

    Only the kinds in `PRUNED_KINDS` are deleted, and only objects whose controlling owner is
    this fleet, so the chart's objects and another fleet's stay. A ConfigMap one of `pods`
    still mounts stays, and so does an object already being deleted.
    """
    wanted = set()
    for document in desired:
        if not controlled(owner, document):
            raise ValueError(
                f"{document['kind']} {document['metadata']['name']} names another owner"
            )
        wanted.add((document["kind"], document["metadata"]["name"]))
    kept = mounted_maps(pods)
    found = []
    for document in live:
        kind, meta = document.get("kind"), document.get("metadata") or {}
        name = meta.get("name")
        if (
            kind not in PRUNED_KINDS
            or not controlled(owner, document)
            or (kind, name) in wanted
            or meta.get("deletionTimestamp")
            or (kind == "ConfigMap" and name in kept)
        ):
            continue
        found.append(Deletion(str(kind), str(name), meta.get("uid")))
    return sorted(found, key=lambda item: (PRUNED_KINDS.index(item.kind), item.name))
