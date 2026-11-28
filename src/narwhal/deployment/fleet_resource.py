"""The `NarwhalFleet` custom resource: its API group, its schema, and the deployment inputs a
fleet's spec holds.

`GROUP` is `contracts.API_GROUP`, the one place the API group is written. Every label,
annotation, condition type, scheduling gate and Lease name derives from it, and
`tools/maintenance/crd_schema.py` writes it into the charts. The group is a placeholder under
`example.com`, which RFC 2606 reserves, so nobody else can own it.

`SCHEMA` is the only copy of the schema. The same tool renders the CRD from it, and the unit
suite fails when the rendered chart is stale. Every array has `maxItems`, every string
`maxLength` and every map `maxProperties`, which bounds what a fleet can hold and keeps each
CEL rule within the API server's cost budget.

`spec_env` turns a fleet into the variables `kubernetes_target.inputs()` reads, so input
validation, digests and rerun rules stay in that function.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts import API_GROUP as GROUP
from . import fabric
from .deploy import CALIBRATION, MARGIN, PROFILE, SLO
from .kubernetes_graph import KUBERNETES_GRAPH
from .launch_engine.kubernetes import (
    API_KEY_SECRET_ENV,
    GPU_RESOURCE_ENV,
    MODEL_VOLUME_ENV,
    MODEL_VOLUME_SOURCES,
    RDMA_RESOURCE_ENV,
    RUNTIME_CLASS_ENV,
    STATE_CLAIM_ENV,
)

VERSION = "v1alpha1"
API_VERSION = f"{GROUP}/{VERSION}"
KIND = "NarwhalFleet"
LIST_KIND = f"{KIND}List"
PLURAL = "narwhalfleets"
SINGULAR = "narwhalfleet"
CRD_NAME = f"{PLURAL}.{GROUP}"
# A namespace holds one fleet, and it has this name. A CEL rule refuses any other.
NAME = "fleet"


def qualified(name: str) -> str:
    """A label, annotation or scheduling gate key in the fleet's group: `<GROUP>/<name>`."""
    return f"{GROUP}/{name}"


PLACEMENT_GATE = qualified("placement")
GATE_LABEL = qualified("gate")
CONFIG_SHA256 = qualified("config-sha256")
# On the CRD: the SHA-256 of its spec, which the controller compares with `schema_digest()`.
SCHEMA_SHA256 = qualified("schema-sha256")
# On the fleet: a new value asks a stalled fleet to run again.
REQUESTED_AT = qualified("requested-at")
# A workstation's request for an export Job; see `kubernetes_export`.
EXPORT_REQUEST = qualified("export-request")
# Lease names are DNS subdomains, so the group can follow a label.
CONTROLLER_LEASE = f"controller.{GROUP}"
# The field manager of `narwhal deploy`; a fleet whose spec another manager owns is followed,
# never written.
CLI_MANAGER = "narwhal-cli"
# Keeps a resource when `helm uninstall` removes its release.
KEEP = {"helm.sh/resource-policy": "keep"}

# kstatus conditions, and one condition per stage.
READY = "Ready"
RECONCILING = "Reconciling"
STALLED = "Stalled"
STOPPED = "Stopped"
CONDITIONS = (READY, RECONCILING, STALLED, STOPPED)
STAGE_CONDITION = f"stage.{GROUP}/"
STAGES = KUBERNETES_GRAPH.order()


def stage_condition(stage: str) -> str:
    """The condition type that reports `stage`."""
    if stage not in STAGES:
        raise ValueError(f"{stage} is not a stage of the Kubernetes graph")
    return STAGE_CONDITION + stage


# The engine pods' headless Service. Engine n answers at engine-<n>.<ENGINE_SERVICE>.<ns>.svc.
ENGINE_SERVICE = "engines"
ATTESTATION_PATH = "/v1/attestation"
MODEL_MOUNT = "/model"
NAMESPACE_ENV = "NARWHAL_KUBERNETES_NAMESPACE"
FLEET_UID_ENV = "NARWHAL_KUBERNETES_FLEET_UID"
HAPROXY_IMAGE_ENV = "NARWHAL_KUBERNETES_HAPROXY_IMAGE"
FABRIC_IMAGE_ENV = "NARWHAL_KUBERNETES_FABRIC_IMAGE"
# The `routers` and `haproxy` blocks, as canonical JSON.
ROUTERS_ENV = "NARWHAL_KUBERNETES_ROUTERS"
# The Secret and key of the routers' control token, as <secret name>/<key>.
CONTROL_TOKEN_SECRET_ENV = "NARWHAL_KUBERNETES_CONTROL_TOKEN_SECRET"
# The engines hold their key, so the control token in the same Secret key would let an engine
# change the fleet.
SHARED_SECRET = (
    "controlTokenSecret names the Secret and key of engines.apiKeySecret; keep the control "
    "token in its own Secret"
)
HAPROXY_ENV = "NARWHAL_KUBERNETES_HAPROXY"
TIMEOUT_ENV = "NARWHAL_STAGE_TIMEOUT_SECONDS"

# The `engines.launch` names a fleet refuses: the variables its own fields set, those the
# runner or the pod sets, those that are not inputs, and anything named like a credential.
# The same expression runs in the API server's CEL rule and in `spec_env`, so it keeps to the
# syntax Python and RE2 share.
LAUNCH_REFUSED = (
    r"^NARWHAL_(NODE_[0-9]+_)?("
    r"KUBERNETES_.*|KUBECTL|HELM|ENGINE_(IMAGE|MODEL_NAME|PORT|LAUNCH_CONFIG)|ATTEST_PORT"
    r"|KV_SIDE_CHANNEL_PORT|UCX_TCP_PORT_RANGE"
    r"|MODEL_(TREE_SHA256|PATH|SHA256|DIR)|URL|ATTESTATION_URL|IP|FABRIC_.*|RDMA_.*"
    r"|RUN_DIR|ROUTER_.*|TTFT_S|TPOT_S|FIRST_TOKEN_MARGIN_S|PROFILE_.*|CALIBRATION_.*"
    r"|STAGE_.*|TRIAL_.*|SSH.*|HOSTS|FLEET|LAUNCH_CONFIG|WHEEL.*|REQUIREMENTS.*|TOOLS_COMMIT"
    r"|DEPLOYMENT_.*|ROLE|POD_.*|NODE_NAME"
    # A credential: a word that only credentials use, anywhere in the name, or a word that
    # names one as a whole segment, with any prefix and any suffix such as _FILE.
    r"|.*(SECRET|PASSWORD|PASSWD|CREDENTIAL|APIKEY).*|(.*_)?(TOKENS?|KEYS?|CREDS)(_.*)?"
    r")$"
)
# Characters that must not reach a variable or an argument: C0 and C1 controls and DEL.
CONTROL = r"[\x00-\x1f\x7f-\x9f]"
_PRINTABLE = r"^[^\x00-\x1f\x7f-\x9f]*$"
# A normalised absolute path: one or more segments, no empty segment and no trailing slash.
_NORMALISED = r"^(/[^/\x00-\x1f\x7f-\x9f]+)+$"
# The host directories no model volume may be, or lie in: kernel and device interfaces, node
# configuration, the kubelet's and container runtimes' state, and the directories that hold
# them. Matched by whole segments.
HOST_PATH_REFUSED = (
    r"^/(proc|sys|dev|run|var/run|etc|boot|root|home|host|var/log|var/snap|opt/cni"
    r"|var/lib/(kubelet|etcd|containerd|docker|containers|cri-o|crio|rancher|k0s|cni))(/|$)"
    r"|^/(var|var/lib|opt)$"
)

# Bounds. Each keeps a fleet small and each CEL rule's cost estimate low.
MAX_ENGINES = 64
MAX_LAUNCH = 64
MAX_ROUTER_ARGS = 32
MAX_RETAINED_RUNS = 16
MAX_ATTRIBUTES = 32
WEEK_SECONDS = 7 * 24 * 3600

_DIGEST = r"^[^\s@\x00-\x1f\x7f-\x9f]+@sha256:[0-9a-f]{64}$"
_DNS_LABEL = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
_DNS_SUBDOMAIN = rf"^{_DNS_LABEL}(\.{_DNS_LABEL})*$"
_RESOURCE = r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?/[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$"
# A decimal number as a string: the Kubernetes API conventions keep floating point out of a spec.
_DECIMAL = r"^(0|[1-9][0-9]{0,6})(\.[0-9]{1,6})?$"
_QUANTITY = r"^[0-9]+(\.[0-9]+)?([KMGTPE]i?)?$"
_SHA256 = r"^[0-9a-f]{64}$"
_UID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_TIME = 64
# A path segment that is `.` or `..`.
_DOT_SEGMENT = r"(^|/)\.\.?(/|$)"


def _rule(rule: str, message: str) -> dict:
    return {"rule": rule, "message": message}


def _matches(pattern: str) -> str:
    """A CEL test of `self` against `pattern`, written as a raw CEL string."""
    if "'" in pattern:
        raise ValueError("a CEL pattern must not hold a single quote")
    return f"self.matches(r'{pattern}')"


def _string(
    description: str,
    max_length: int,
    *,
    pattern: str | None = None,
    enum: Sequence[str] = (),
    min_length: int | None = None,
    default: str | None = None,
    rules: Sequence[dict] = (),
    format: str | None = None,
) -> dict:
    node: dict[str, Any] = {"type": "string", "description": description, "maxLength": max_length}
    if min_length is not None:
        node["minLength"] = min_length
    if pattern is not None:
        node["pattern"] = pattern
    if enum:
        node["enum"] = list(enum)
    if format is not None:
        node["format"] = format
    if default is not None:
        node["default"] = default
    if rules:
        node["x-kubernetes-validations"] = list(rules)
    return node


def _integer(description: str, minimum: int, maximum: int, *, default: int | None = None) -> dict:
    node: dict[str, Any] = {
        "type": "integer",
        "description": description,
        "minimum": minimum,
        "maximum": maximum,
    }
    if default is not None:
        node["default"] = default
    return node


def _boolean(description: str, *, default: bool | None = None, rules: Sequence[dict] = ()) -> dict:
    node: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        node["default"] = default
    if rules:
        node["x-kubernetes-validations"] = list(rules)
    return node


def _object(
    description: str,
    properties: Mapping[str, dict],
    *,
    required: Sequence[str] = (),
    rules: Sequence[dict] = (),
) -> dict:
    node: dict[str, Any] = {
        "type": "object",
        "description": description,
        "properties": dict(properties),
    }
    if required:
        node["required"] = list(required)
    if rules:
        node["x-kubernetes-validations"] = list(rules)
    return node


def _array(
    description: str,
    items: dict,
    max_items: int,
    *,
    min_items: int | None = None,
    map_keys: Sequence[str] = (),
    rules: Sequence[dict] = (),
) -> dict:
    node: dict[str, Any] = {"type": "array", "description": description, "items": items}
    if min_items is not None:
        node["minItems"] = min_items
    node["maxItems"] = max_items
    if map_keys:
        node["x-kubernetes-list-type"] = "map"
        node["x-kubernetes-list-map-keys"] = list(map_keys)
    else:
        node["x-kubernetes-list-type"] = "atomic"
    if rules:
        node["x-kubernetes-validations"] = list(rules)
    return node


def _decimal(description: str, *, positive: bool = True, at_least_one: bool = False) -> dict:
    rules = []
    if at_least_one:
        rules.append(_rule("double(self) >= 1.0", "must be at least 1"))
    elif positive:
        rules.append(_rule("double(self) > 0.0", "must be greater than 0"))
    return _string(description, 16, pattern=_DECIMAL, rules=rules)


def _lengths(description: str) -> dict:
    return _array(description, _integer("A token count", 1, 10_000_000), 64, min_items=1)


# Kubernetes reserves the kubernetes.io and k8s.io domains.
_UNRESERVED = _rule(
    "!" + _matches(r"^([^/]*\.)?(kubernetes|k8s)\.io/"),
    "the kubernetes.io and k8s.io domains are reserved",
)


def _read_only() -> dict:
    return _boolean(
        "Always true: engines read the model and never write it",
        default=True,
        rules=[_rule("self", "the model volume is mounted read-only")],
    )


def _absolute(description: str, *, host: bool = False) -> dict:
    rules = [_rule("!" + _matches(_DOT_SEGMENT), "must not hold a . or .. segment")]
    if host:
        rules.append(
            _rule(
                "!" + _matches(HOST_PATH_REFUSED),
                "must not be or lie in a node's system, kubelet or container runtime directory",
            )
        )
    return _string(description, 4096, pattern=_NORMALISED, rules=rules)


_MODEL_VOLUME = _object(
    "The volume that serves the staged checkpoint to every engine pod, read-only. Set "
    "exactly one source.",
    {
        "persistentVolumeClaim": _object(
            "A PersistentVolumeClaim in the fleet's namespace",
            {
                "claimName": _string("The claim's name", 253, pattern=_DNS_SUBDOMAIN),
                "readOnly": _read_only(),
            },
            required=["claimName"],
        ),
        "hostPath": _object(
            "A directory on every engine node. The controller refuses it unless the cluster "
            "admin lists hostPath in the narwhal chart's modelVolumeSources and a prefix that "
            "holds it in modelHostPathPrefixes, which is empty by default.",
            {
                "path": _absolute(
                    "The directory's normalised absolute path on the node", host=True
                ),
                "type": _string(
                    "Directory: the kubelet refuses a path that is not an existing directory",
                    16,
                    enum=["Directory"],
                ),
            },
            required=["path", "type"],
        ),
        "nfs": _object(
            "An NFS export. The controller refuses it unless the cluster admin lists nfs in "
            "the narwhal chart's modelVolumeSources and its server in modelNfsServers.",
            {
                "server": _string(
                    "The NFS server's host name or address", 253, pattern=r"^[A-Za-z0-9.:\[\]-]+$"
                ),
                "path": _absolute("The exported directory"),
                "readOnly": _read_only(),
            },
            required=["server", "path"],
        ),
        "csi": _object(
            "A CSI ephemeral volume. The controller refuses it unless the cluster admin lists "
            "csi in the narwhal chart's modelVolumeSources and its driver in modelCsiDrivers.",
            {
                "driver": _string("The CSI driver's name", 63, pattern=_DNS_SUBDOMAIN),
                "readOnly": _read_only(),
                "volumeAttributes": {
                    "type": "object",
                    "description": "The driver's volume attributes",
                    "maxProperties": MAX_ATTRIBUTES,
                    "additionalProperties": {
                        "type": "string",
                        "maxLength": 1024,
                        "pattern": _PRINTABLE,
                    },
                    "x-kubernetes-validations": [
                        _rule(
                            "self.all(k, k.size() <= 253)",
                            "an attribute name holds at most 253 characters",
                        )
                    ],
                },
            },
            required=["driver"],
        ),
    },
    rules=[
        _rule(
            "[" + ", ".join(f"has(self.{name})" for name in MODEL_VOLUME_SOURCES) + "]"
            ".filter(set, set).size() == 1",
            "set exactly one of " + ", ".join(MODEL_VOLUME_SOURCES),
        )
    ],
)

_LAUNCH = _array(
    "More launch inputs of the engine backend, as the variables discovery reads, such as "
    "NARWHAL_ENGINE_BACKEND or NARWHAL_TRANSFER_NET_DEVICES. A name another field of the "
    "fleet sets, or that names a credential, is refused.",
    _object(
        "One launch input",
        {
            "name": _string(
                "The variable's name",
                128,
                pattern=r"^NARWHAL_[A-Z0-9_]+$",
                rules=[
                    _rule(
                        "!" + _matches(LAUNCH_REFUSED),
                        "another field of the fleet sets this variable, or it names a "
                        "credential, which belongs in engines.apiKeySecret or controlTokenSecret",
                    )
                ],
            ),
            "value": _string("The variable's value", 4096, pattern=_PRINTABLE),
        },
        required=["name", "value"],
    ),
    MAX_LAUNCH,
    map_keys=["name"],
)

# Each port field of an engine, and the variable it becomes: `NARWHAL_<name>` for the fleet,
# `NARWHAL_NODE_<n>_<name>` for one engine, as an SSH host's per-node overrides.
PORT_FIELDS = (
    ("engine", "ENGINE_PORT"),
    ("attestation", "ATTEST_PORT"),
    ("sideChannel", "KV_SIDE_CHANNEL_PORT"),
    ("ucxTcpPortRange", "UCX_TCP_PORT_RANGE"),
)
_PORT_RANGE = r"^[0-9]{1,5}-[0-9]{1,5}$"


_SINGLE_PORTS = ("engine", "attestation", "sideChannel")
# Each port in a list lies outside `self.ucxTcpPortRange`, LOW-HIGH.
_OUTSIDE = (
    "all(p, p < int(self.ucxTcpPortRange.split('-')[0]) "
    "|| p > int(self.ucxTcpPortRange.split('-')[1]))"
)
PORTS_IN_RANGE = (
    "the engine, attestation and side channel ports must lie outside the UCX TCP port range"
)


def _ports(description: str, *, required: bool) -> dict:
    fields = {
        "engine": _integer("The engine's HTTP port", 1, 65535),
        "attestation": _integer("The attestation sidecar's HTTP port", 1, 65535),
        "sideChannel": _integer(
            "The port the engine's KV connector listens on for its peers' handshakes",
            1,
            65535,
        ),
        "ucxTcpPortRange": _string(
            "LOW-HIGH, the ports UCX TCP listeners take", 11, pattern=_PORT_RANGE
        ),
    }
    if not required:
        # An engine's own ports may override some of engines.ports, so the controller checks
        # the ports it resolves; the rule checks those this entry sets.
        held = ", ".join(f"has(self.{name}) ? self.{name} : -1" for name in _SINGLE_PORTS)
        return _object(
            description,
            fields,
            rules=[_rule(f"!has(self.ucxTcpPortRange) || [{held}].{_OUTSIDE}", PORTS_IN_RANGE)],
        )
    ports = ", ".join(f"self.{name}" for name in _SINGLE_PORTS)
    return _object(
        description,
        fields,
        required=list(fields),
        rules=[
            _rule(
                "self.engine != self.attestation && self.engine != self.sideChannel "
                "&& self.attestation != self.sideChannel",
                "the engine, attestation and side channel ports must differ",
            ),
            _rule(f"[{ports}].{_OUTSIDE}", PORTS_IN_RANGE),
        ],
    )


_NODE = _object(
    "One engine: the node its pod runs on, in role order, so the first entry is engine-1. "
    "Two engines may share a node, each with its own ports.",
    {
        "node": _string("The Kubernetes node's name", 253, pattern=_DNS_SUBDOMAIN),
        "fabricInterface": _string(
            "The network interface that carries the engine's KV transfers",
            15,
            pattern=r"^[^\s/:\x00-\x1f\x7f-\x9f]+$",
        ),
        "rdma": _object(
            "The RDMA port settings of a ucx_rdma engine",
            {
                "gidIndex": _integer("The GID index of the HCA port", 0, 255),
                "roceMode": _string(
                    "v2 or v1 for RoCE, or infiniband for a native InfiniBand port",
                    16,
                    enum=["v2", "v1", "infiniband"],
                ),
            },
        ),
        "ports": _ports(
            "This engine's ports in place of engines.ports. Engine pods run on the node's host "
            "network, so engines that share a node need ports no other engine there uses, which "
            "the controller checks.",
            required=False,
        ),
    },
    required=["node", "fabricInterface"],
)

_ENGINES = _object(
    "The engine pods",
    {
        "image": _string("The engine image by registry digest", 512, pattern=_DIGEST),
        "model": _object(
            "The checkpoint the engines serve",
            {
                "name": _string("The served model name", 256, min_length=1, pattern=_PRINTABLE),
                "treeSha256": _string("The checkpoint's model_tree_sha256", 64, pattern=_SHA256),
                "file": _string(
                    "For an engine that serves one model file, its path in the model volume",
                    1024,
                    # Relative and normalised: no leading, doubled or trailing slash.
                    pattern=r"^[^/\x00-\x1f\x7f-\x9f]+(/[^/\x00-\x1f\x7f-\x9f]+)*$",
                    rules=[_rule("!" + _matches(_DOT_SEGMENT), "must not hold a . or .. segment")],
                ),
                "fileSha256": _string("The model file's SHA-256", 64, pattern=_SHA256),
            },
            required=["name", "treeSha256"],
            rules=[
                _rule("has(self.file) == has(self.fileSha256)", "set file and fileSha256 together")
            ],
        ),
        "modelVolume": _MODEL_VOLUME,
        "resources": _object(
            "The device plugins' extended resources, and the engine pods' RuntimeClass",
            {
                "gpu": _string(
                    "The GPU device plugin's extended resource name, <domain>/<name>",
                    316,
                    pattern=_RESOURCE,
                    rules=[_UNRESERVED],
                ),
                "rdma": _string(
                    "The RDMA device plugin's extended resource name. On each engine node it "
                    "holds only the HCA the engine's NARWHAL_TRANSFER_NET_DEVICES names, shared "
                    "by the engine pods and the fabric Jobs",
                    316,
                    pattern=_RESOURCE,
                    rules=[_UNRESERVED],
                ),
                "runtimeClassName": _string(
                    "The RuntimeClass of nodes whose default container runtime exposes no GPUs",
                    253,
                    pattern=_DNS_SUBDOMAIN,
                ),
            },
            required=["gpu", "rdma"],
            rules=[_rule("self.gpu != self.rdma", "gpu and rdma must name two resources")],
        ),
        "ports": _ports(
            "The ports each engine pod serves on, unless its entry of nodes sets its own",
            required=True,
        ),
        "nodes": _array("The engines, one entry each", _NODE, MAX_ENGINES, min_items=2),
        "apiKeySecret": _object(
            "The Secret that holds the engines' API key. Name a new Secret to rotate the key.",
            {
                "name": _string("The Secret's name", 253, pattern=_DNS_SUBDOMAIN),
                "key": _string("The key in the Secret", 253, pattern=r"^[-._A-Za-z0-9]+$"),
            },
            required=["name", "key"],
        ),
        "launch": _LAUNCH,
    },
    required=["image", "model", "modelVolume", "resources", "ports", "nodes"],
)

_IMAGES = _object(
    "The third-party images the fleet runs besides the engine's, each by registry digest. The "
    "routers, gate Jobs and attestation sidecars run the controller's own image, which the "
    "narwhal chart's images.narwhal pins",
    {
        "haproxy": _string("The HAProxy image", 512, pattern=_DIGEST),
        "fabric": _string("The image the fabric Jobs run ib_write_bw from", 512, pattern=_DIGEST),
    },
    required=["haproxy", "fabric"],
)

_SLO = _object(
    "The fleet's service-level objective, in seconds",
    {
        "ttftS": _decimal("The time-to-first-token target"),
        "tpotS": _decimal("The time-per-output-token target"),
        "routerOverheadS": _decimal("The router's own share of each request", positive=False),
    },
    required=["ttftS", "tpotS", "routerOverheadS"],
)

_PROFILING = _object(
    "The profiling sweep",
    {
        "prefillLens": _lengths("The prompt lengths of the prefill sweep"),
        "decodeInputLens": _lengths("The input lengths of the decode sweep"),
        "decodeConcurrency": _lengths("The concurrency levels of the decode sweep"),
        "warmupRequests": _integer("Requests sent before measuring", 0, 100_000),
    },
    required=["prefillLens", "decodeInputLens", "warmupRequests"],
)

_CALIBRATION = _object(
    "The first-token calibration",
    {
        "inputTokens": _lengths("The input lengths calibration measures"),
        "samples": _integer(
            "Samples per group, at least the 100 that preflight accepts", 100, 100_000
        ),
        "observationTimeoutS": _decimal("Seconds calibration waits for one observation"),
    },
    required=["inputTokens", "samples", "observationTimeoutS"],
)

_FABRIC = _object(
    "Fabric qualification",
    {
        "workload": _object(
            "The workload the fabric budget sizes each edge for",
            {
                "promptTokens": _integer("Prompt tokens per KV handoff", 1, 10_000_000),
                "handoffsPerS": _decimal(
                    "Peak remote KV handoffs per second on each directed edge, for each source "
                    "cache group"
                ),
                "burst": _integer("Handoffs in a burst", 1, 1_000_000),
                "transferBudgetS": _decimal("Seconds in which one burst must transfer"),
                "headroom": _decimal("Multiplier on the required rate", at_least_one=True),
            },
            required=["promptTokens", "handoffsPerS", "burst", "transferBudgetS", "headroom"],
        ),
        "testPort": _integer("The TCP port the fabric test listens on", 1, 65535),
    },
    required=["workload", "testPort"],
)

_ROUTERS = _object(
    "The router pair",
    {
        "storageClassName": _string(
            "The storage class of each router's volume", 253, pattern=_DNS_SUBDOMAIN
        ),
        "size": _string("The size of each router's volume", 32, pattern=_QUANTITY),
        "args": _array(
            "More narwhal-serve options, such as --lease-ttl 6",
            _string("One option or value", 256, pattern=_PRINTABLE),
            MAX_ROUTER_ARGS,
        ),
    },
    required=["storageClassName", "size"],
)

_HAPROXY = _object(
    "The HAProxy load balancer in front of the routers",
    {
        "replicas": _integer("HAProxy pods", 1, 16, default=1),
        "clusterDomain": _string(
            "The cluster's DNS domain", 253, pattern=_DNS_SUBDOMAIN, default="cluster.local"
        ),
    },
)

_TIMEOUTS = _object(
    "Seconds a stage may run",
    {
        "defaultSeconds": _integer("Every stage's limit", 1, WEEK_SECONDS),
        "stages": _object(
            "One stage's limit, by stage name",
            {stage: _integer(f"The {stage} stage's limit", 1, WEEK_SECONDS) for stage in STAGES},
        ),
    },
)

_SPEC = _object(
    "The deployment inputs of the fleet",
    {
        "images": _IMAGES,
        "engines": _ENGINES,
        "slo": _SLO,
        "firstTokenMarginS": _decimal(
            "Seconds the first-token deadline adds to the calibrated candidate"
        ),
        "profiling": _PROFILING,
        "calibration": _CALIBRATION,
        "fabric": _FABRIC,
        "routers": _ROUTERS,
        "controlTokenSecret": _object(
            "The Secret that holds the routers' control token, which the routes that change "
            "membership, trust or lifecycle require: at least 32 random bytes, as 64 hex digits "
            "or 43 base64url characters. Name a new Secret to rotate the token.",
            {
                "name": _string("The Secret's name", 253, pattern=_DNS_SUBDOMAIN),
                "key": _string("The key in the Secret", 253, pattern=r"^[-._A-Za-z0-9]+$"),
            },
            required=["name", "key"],
        ),
        "haproxy": _HAPROXY,
        "timeouts": _TIMEOUTS,
        "until": _string("The last stage to run; every stage by default", 63, enum=STAGES),
        "stopped": _boolean(
            "Stop the routers, HAProxy, engines and Jobs, keeping the volumes", default=False
        ),
        "retainRuns": _integer(
            "Run directories kept, the current one included", 1, MAX_RETAINED_RUNS, default=3
        ),
    },
    required=[
        "images",
        "engines",
        "slo",
        "firstTokenMarginS",
        "profiling",
        "calibration",
        "fabric",
        "routers",
        "controlTokenSecret",
    ],
    rules=[
        _rule(
            "!has(self.engines.apiKeySecret) || "
            "self.engines.apiKeySecret.name != self.controlTokenSecret.name || "
            "self.engines.apiKeySecret.key != self.controlTokenSecret.key",
            SHARED_SECRET,
        )
    ],
)

# metav1.Condition, as controller-gen writes it.
_CONDITION = _object(
    "One condition of the fleet",
    {
        "type": _string(
            "The condition's type",
            316,
            pattern=r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?"
            r"(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])$",
        ),
        "status": _string("True, False or Unknown", 7, enum=["True", "False", "Unknown"]),
        "observedGeneration": {
            "type": "integer",
            "format": "int64",
            "minimum": 0,
            "description": "The generation the condition was set from",
        },
        "lastTransitionTime": _string("When the status last changed", _TIME, format="date-time"),
        "reason": _string(
            "A CamelCase reason",
            1024,
            min_length=1,
            pattern=r"^[A-Za-z]([A-Za-z0-9_,:]*[A-Za-z0-9_])?$",
        ),
        "message": _string("What the reason means for this fleet", 32768),
    },
    required=["type", "status", "lastTransitionTime", "reason", "message"],
)

_STAGE_STATUS = _object(
    "One stage's instances",
    {
        "passed": _integer("Instances that passed", 0, 1_000_000),
        "total": _integer("Instances the stage runs", 0, 1_000_000),
        "lastPassedTime": _string("When an instance last passed", _TIME, format="date-time"),
    },
)

_STATUS = _object(
    "What the controller observed. Per-instance detail stays in the run record.",
    {
        "observedGeneration": {
            "type": "integer",
            "format": "int64",
            "minimum": 0,
            "description": "The generation the latest pass started from",
        },
        "conditions": _array(
            "Ready, Reconciling, Stalled, Stopped, and one per stage",
            _CONDITION,
            len(CONDITIONS) + len(STAGES),
            map_keys=["type"],
        ),
        "serving": _boolean("The routers serve through HAProxy"),
        "stage": _string("The stage the latest pass runs or stopped at", 63),
        "engines": _string(
            "Engines checked, of those the fleet names", 16, pattern=r"^[0-9]+/[0-9]+$"
        ),
        "stages": _object(
            "Each stage's instances",
            dict.fromkeys(STAGES, _STAGE_STATUS),
        ),
        "previousRuns": _array(
            "The fleet UIDs of the run directories an earlier fleet of this name left",
            _string("A fleet UID", 36, pattern=_UID),
            MAX_RETAINED_RUNS,
        ),
        "exports": _array(
            f"The export Jobs the controller made for the latest {EXPORT_REQUEST} values",
            _object(
                "One export request's answer: its Job and, once the Job's pod succeeds, the "
                "pod and the SHA-256 and size of the log the controller read from it; or why "
                "the export failed",
                {
                    "nonce": _string("The request's nonce", 32, pattern=r"^[0-9a-f]{32}$"),
                    "job": _string("The export Job's name", 63),
                    "uid": _string("The export Job's UID", 36, pattern=_UID),
                    "pod": _string("The export Job's pod the controller read", 253),
                    "sha256": _string(
                        "The SHA-256 of the pod's log", 64, pattern=r"^[0-9a-f]{64}$"
                    ),
                    "size": _integer("The size of the pod's log in bytes", 0, 2**31 - 1),
                    "error": _string("Why the export failed", 1024),
                },
                required=["nonce"],
            ),
            4,
            map_keys=["nonce"],
        ),
    },
)

SCHEMA = _object(
    "A Narwhal fleet: the engines, routers and gate Jobs of one namespace, and the inputs of "
    "its stage graph",
    {
        "apiVersion": _string("The resource's API version", 64),
        "kind": _string("The resource's kind", 64),
        "metadata": {"type": "object"},
        "spec": _SPEC,
        "status": _STATUS,
    },
    required=["spec"],
    rules=[
        _rule(
            f"self.metadata.name == '{NAME}'",
            f"a namespace holds one {KIND}, and it is named {NAME}",
        )
    ],
)

PRINTER_COLUMNS = [
    {"name": "Serving", "type": "boolean", "jsonPath": ".status.serving"},
    {
        "name": "Ready",
        "type": "string",
        "jsonPath": f'.status.conditions[?(@.type=="{READY}")].status',
    },
    {"name": "Stage", "type": "string", "jsonPath": ".status.stage"},
    {"name": "Engines", "type": "string", "jsonPath": ".status.engines"},
    {"name": "Age", "type": "date", "jsonPath": ".metadata.creationTimestamp"},
]


def crd_spec() -> dict:
    """The CRD's spec."""
    return {
        "group": GROUP,
        "names": {
            "kind": KIND,
            "listKind": LIST_KIND,
            "plural": PLURAL,
            "singular": SINGULAR,
            "categories": ["narwhal"],
        },
        "scope": "Namespaced",
        "versions": [
            {
                "name": VERSION,
                "served": True,
                "storage": True,
                "schema": {"openAPIV3Schema": SCHEMA},
                "subresources": {"status": {}},
                "additionalPrinterColumns": PRINTER_COLUMNS,
            }
        ],
    }


def canonical(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def schema_digest() -> str:
    """The SHA-256 of the CRD's spec, which the CRD carries as its `SCHEMA_SHA256` annotation."""
    return hashlib.sha256(canonical(crd_spec())).hexdigest()


def stored_digest(stored: Mapping[str, Any]) -> str:
    """The SHA-256 of a CRD the API server returned, over the fields `crd_spec()` sets.

    The API server adds defaults, such as `conversion`, so only the fields this module sets
    count. The digest is computed from the stored spec itself; the CRD's `SCHEMA_SHA256`
    annotation is never trusted.
    """
    spec = _block(stored, "spec")
    return hashlib.sha256(canonical({key: spec.get(key) for key in crd_spec()})).hexdigest()


def crd_current(stored: Mapping[str, Any]) -> bool:
    """Whether a stored CRD holds this module's schema. The controller sets `Stalled` when
    it does not."""
    return (
        _block(stored, "metadata").get("name") == CRD_NAME
        and stored_digest(stored) == schema_digest()
    )


def crd() -> dict:
    """The CRD, as the CRD chart installs it apart from the chart's labels."""
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {
            "name": CRD_NAME,
            "annotations": {**KEEP, SCHEMA_SHA256: schema_digest()},
        },
        "spec": crd_spec(),
    }


# Spec to variables.


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _set(env: dict[str, str], name: str, block: Mapping[str, Any], field: str) -> None:
    if block.get(field) is not None:
        env[name] = _text(block[field])


def _block(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    return value if isinstance(value, Mapping) else {}


def _joined(env: dict[str, str], name: str, block: Mapping[str, Any], field: str) -> None:
    if isinstance(block.get(field), list):
        env[name] = ",".join(_text(item) for item in block[field])


def stage_timeout_env(stage: str) -> str:
    """The variable that holds one stage's limit, as `stages.run` reads it."""
    return "NARWHAL_STAGE_" + re.sub(r"[^A-Z0-9]", "_", stage.upper()) + "_TIMEOUT_SECONDS"


def _normalised(path: str) -> bool:
    return bool(re.fullmatch(_NORMALISED, path)) and not re.search(_DOT_SEGMENT, path)


@dataclass(frozen=True)
class VolumePolicy:
    """The cluster admin's allow-lists for a fleet's model volume, as the narwhal chart's
    values hold them. The defaults are the chart's: only a PersistentVolumeClaim."""

    sources: tuple[str, ...] = ("persistentVolumeClaim",)
    host_path_prefixes: tuple[str, ...] = ()
    nfs_servers: tuple[str, ...] = ()
    csi_drivers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = sorted(set(self.sources) - set(MODEL_VOLUME_SOURCES))
        if unknown:
            raise ValueError(f"modelVolumeSources: {', '.join(unknown)} is no model volume source")
        for prefix in self.host_path_prefixes:
            if not _normalised(prefix):
                raise ValueError(
                    f"modelHostPathPrefixes: {prefix!r} is not a normalised absolute path"
                )

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> VolumePolicy:
        """The policy the narwhal chart's values set, with the chart's defaults."""
        default = cls()
        return cls(
            tuple(values.get("modelVolumeSources", default.sources)),
            tuple(values.get("modelHostPathPrefixes", ())),
            tuple(values.get("modelNfsServers", ())),
            tuple(values.get("modelCsiDrivers", ())),
        )


def check_model_volume(fleet: Mapping[str, Any], policy: VolumePolicy) -> None:
    """Refuse a fleet whose model volume the admin's `policy` does not allow.

    The source must be one of `policy.sources`. A hostPath must also pass `_check_host_path`,
    an NFS export must come from one of `policy.nfs_servers`, and a CSI volume must use one of
    `policy.csi_drivers`; each list is empty by default, which turns that source off.
    `spec_env` applies this check for the controller. The narwhal chart's admission policy
    applies the same rules to every pod's model volume; `tests/deployment/chart_live.py` checks
    that the two agree on an API server.
    """
    source = _block(_block(_block(fleet, "spec"), "engines"), "modelVolume")
    if len(source) != 1 or next(iter(source)) not in MODEL_VOLUME_SOURCES:
        raise ValueError(
            "engines.modelVolume: set exactly one of " + ", ".join(MODEL_VOLUME_SOURCES)
        )
    [(kind, settings)] = source.items()
    settings = settings if isinstance(settings, Mapping) else {}
    if kind not in policy.sources:
        raise ValueError(
            f"engines.modelVolume.{kind}: the cluster admin allows only "
            f"{', '.join(policy.sources) or 'no source'} in modelVolumeSources"
        )
    if kind == "hostPath":
        _check_host_path(str(settings.get("path", "")), policy.host_path_prefixes)
    elif kind == "nfs" and settings.get("server") not in policy.nfs_servers:
        raise ValueError(
            f"engines.modelVolume.nfs.server: {settings.get('server')} is not in modelNfsServers"
        )
    elif kind == "csi" and settings.get("driver") not in policy.csi_drivers:
        raise ValueError(
            f"engines.modelVolume.csi.driver: {settings.get('driver')} is not in modelCsiDrivers"
        )


def _check_host_path(path: str, prefixes: Sequence[str]) -> None:
    """Refuse a model hostPath unless it is normalised, outside every directory
    `HOST_PATH_REFUSED` names, and at or below one of `prefixes`, matched by whole segments.
    Without a prefix, hostPath is off."""
    if not _normalised(path):
        raise ValueError(
            f"engines.modelVolume.hostPath.path: {path!r} is not a normalised absolute path"
        )
    if re.search(HOST_PATH_REFUSED, path):
        raise ValueError(
            f"engines.modelVolume.hostPath.path: {path} is or lies in a system, kubelet or "
            "container runtime directory"
        )
    if not prefixes:
        raise ValueError(
            "engines.modelVolume.hostPath: hostPath is off; the cluster admin turns it on by "
            "listing prefixes in modelHostPathPrefixes"
        )
    if not any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes):
        raise ValueError(
            f"engines.modelVolume.hostPath.path: {path} lies outside modelHostPathPrefixes"
        )


def model_volume(spec: Mapping[str, Any]) -> dict:
    """The model volume source of an engines block that `check_model_volume` allowed, with
    `readOnly` forced on and a hostPath's type fixed to Directory."""
    [(kind, settings)] = _block(spec, "modelVolume").items()
    settings = dict(settings) if isinstance(settings, Mapping) else {}
    if kind == "hostPath":
        settings["type"] = "Directory"
    else:
        settings["readOnly"] = True
    return {kind: settings}


def spec_env(fleet: Mapping[str, Any], *, state_claim: str, policy: VolumePolicy) -> dict[str, str]:
    """The variables `kubernetes_target.inputs()` reads, from a `NarwhalFleet` object.

    `state_claim` names the state PersistentVolumeClaim the chart made, and
    `policy` the admin's model volume allow-lists. A field the fleet leaves out
    gives no variable, so `inputs()` names it. `VARIABLE_FACTS` gives each variable's fact.

    The fleet replaces the chart values file, and the controller builds its objects from the
    installed package rather than a chart directory, so no `NARWHAL_KUBERNETES_VALUES` or
    `NARWHAL_KUBERNETES_CHART` comes from a fleet.

    Raises `ValueError` for a fleet with another name, a launch input the fleet refuses, a
    model volume `check_model_volume` refuses, a control token in the engine key's Secret key,
    or a control character in any value.
    """
    metadata = _block(fleet, "metadata")
    if metadata.get("name") != NAME:
        raise ValueError(f"a namespace holds one {KIND}, and it is named {NAME}")
    spec = _block(fleet, "spec")
    engines = _block(spec, "engines")
    refused = sorted(
        str(item.get("name"))
        for item in engines.get("launch") or []
        if not re.fullmatch(r"NARWHAL_[A-Z0-9_]+", str(item.get("name")))
        or re.fullmatch(LAUNCH_REFUSED, str(item.get("name")))
    )
    if refused:
        raise ValueError(f"engines.launch: the fleet sets or refuses {', '.join(refused)}")
    env = {str(item["name"]): _text(item.get("value", "")) for item in engines.get("launch") or []}
    namespace = str(metadata.get("namespace", ""))
    env[NAMESPACE_ENV] = namespace
    env[FLEET_UID_ENV] = str(metadata.get("uid", ""))
    env[STATE_CLAIM_ENV] = state_claim

    images = _block(spec, "images")
    _set(env, HAPROXY_IMAGE_ENV, images, "haproxy")
    _set(env, FABRIC_IMAGE_ENV, images, "fabric")

    _set(env, "NARWHAL_ENGINE_IMAGE", engines, "image")
    model = _block(engines, "model")
    _set(env, "NARWHAL_ENGINE_MODEL_NAME", model, "name")
    _set(env, "NARWHAL_MODEL_TREE_SHA256", model, "treeSha256")
    if model.get("file"):
        env["NARWHAL_MODEL_PATH"] = f"{MODEL_MOUNT}/{model['file']}"
    _set(env, "NARWHAL_MODEL_SHA256", model, "fileSha256")
    if "modelVolume" in engines:
        check_model_volume(fleet, policy)
        env[MODEL_VOLUME_ENV] = json.dumps(model_volume(engines), sort_keys=True)
    resources = _block(engines, "resources")
    _set(env, GPU_RESOURCE_ENV, resources, "gpu")
    _set(env, RDMA_RESOURCE_ENV, resources, "rdma")
    _set(env, RUNTIME_CLASS_ENV, resources, "runtimeClassName")
    ports = _block(engines, "ports")
    for field, name in PORT_FIELDS:
        _set(env, f"NARWHAL_{name}", ports, field)
    secret = _block(engines, "apiKeySecret")
    if secret:
        env[API_KEY_SECRET_ENV] = f"{secret.get('name', '')}/{secret.get('key', '')}"
    for n, node in enumerate(engines.get("nodes") or [], start=1):
        role = f"engine-{n}"
        prefix = f"NARWHAL_NODE_{n}_"
        _set(env, prefix + "KUBERNETES_NODE", node, "node")
        _set(env, prefix + "FABRIC_INTERFACE", node, "fabricInterface")
        rdma = _block(node, "rdma")
        _set(env, prefix + "RDMA_GID_INDEX", rdma, "gidIndex")
        _set(env, prefix + "RDMA_ROCE_MODE", rdma, "roceMode")
        own = _block(node, "ports")
        for field, name in PORT_FIELDS:
            _set(env, prefix + name, own, field)
        resolved = {**ports, **{k: v for k, v in own.items() if v is not None}}
        host = f"{role}.{ENGINE_SERVICE}.{namespace}.svc"
        if resolved.get("engine") is not None:
            env[prefix + "URL"] = f"http://{host}:{resolved['engine']}"
        if resolved.get("attestation") is not None:
            env[prefix + "ATTESTATION_URL"] = f"http://{host}:{resolved['attestation']}" + (
                ATTESTATION_PATH
            )

    slo = _block(spec, "slo")
    for name, field in zip(SLO, ("ttftS", "tpotS", "routerOverheadS"), strict=True):
        _set(env, name, slo, field)
    _set(env, MARGIN, spec, "firstTokenMarginS")
    profiling = _block(spec, "profiling")
    for name, field in zip(
        PROFILE[:3], ("prefillLens", "decodeInputLens", "decodeConcurrency"), strict=True
    ):
        _joined(env, name, profiling, field)
    _set(env, PROFILE[3], profiling, "warmupRequests")
    calibration = _block(spec, "calibration")
    _joined(env, CALIBRATION[0], calibration, "inputTokens")
    _set(env, CALIBRATION[1], calibration, "samples")
    _set(env, CALIBRATION[2], calibration, "observationTimeoutS")
    fabric_block = _block(spec, "fabric")
    workload = _block(fabric_block, "workload")
    for (_, name, _), field in zip(
        fabric.WORKLOAD,
        ("promptTokens", "handoffsPerS", "burst", "transferBudgetS", "headroom"),
        strict=True,
    ):
        _set(env, name, workload, field)
    _set(env, fabric.TEST_PORT, fabric_block, "testPort")

    if "routers" in spec:
        env[ROUTERS_ENV] = canonical(spec["routers"]).decode()
    token = _block(spec, "controlTokenSecret")
    if token:
        env[CONTROL_TOKEN_SECRET_ENV] = f"{token.get('name', '')}/{token.get('key', '')}"
        if env[CONTROL_TOKEN_SECRET_ENV] == env.get(API_KEY_SECRET_ENV):
            raise ValueError(SHARED_SECRET)
    if "haproxy" in spec:
        env[HAPROXY_ENV] = canonical(spec["haproxy"]).decode()
    timeouts = _block(spec, "timeouts")
    _set(env, TIMEOUT_ENV, timeouts, "defaultSeconds")
    for stage, seconds in _block(timeouts, "stages").items():
        env[stage_timeout_env(stage)] = _text(seconds)
    controlled = sorted(name for name, value in env.items() if re.search(CONTROL, value))
    if controlled:
        raise ValueError(f"a control character reaches {', '.join(controlled)}")
    return env


# The fact of each variable `spec_env` gives, by the full name's pattern, following the
# design's table: `None` for a variable that is no input. `kubernetes_target.inputs()` files
# every NARWHAL_ variable it does not claim under `discovery`.
VARIABLE_FACTS: tuple[tuple[str, str | None], ...] = (
    (NAMESPACE_ENV, "cluster"),
    (FLEET_UID_ENV, "cluster"),
    (API_KEY_SECRET_ENV, "credential"),
    (CONTROL_TOKEN_SECRET_ENV, "control_credential"),
    (ROUTERS_ENV, "router"),
    (HAPROXY_ENV, "router"),
    (fabric.TEST_PORT, "fabric_test"),
    (r"NARWHAL_NODE_[1-9][0-9]*_RDMA_(GID_INDEX|ROCE_MODE)", "fabric_test"),
    *((name, "fabric_workload") for _, name, _ in fabric.WORKLOAD),
    *((name, "slo") for name in SLO),
    (MARGIN, "deadline_margin"),
    *((name, "profiling") for name in PROFILE),
    *((name, "calibration") for name in CALIBRATION),
    (r"NARWHAL_STAGE_([A-Z0-9_]+_)?TIMEOUT_SECONDS", None),
    (r"NARWHAL_[A-Z0-9_]+", "discovery"),
)


def variable_fact(name: str) -> str | None:
    """The fact a `spec_env` variable belongs to; the first matching row of `VARIABLE_FACTS`."""
    for pattern, fact in VARIABLE_FACTS:
        if re.fullmatch(pattern, name):
            return fact
    raise ValueError(f"{name} is no variable of a fleet")


# The fleet field each engine's variable comes from, under spec.engines.nodes[<n - 1>], and
# the field each fleet-wide variable comes from.
NODE_FIELDS = {
    "KUBERNETES_NODE": "node",
    "FABRIC_INTERFACE": "fabricInterface",
    "RDMA_GID_INDEX": "rdma.gidIndex",
    "RDMA_ROCE_MODE": "rdma.roceMode",
    **{name: f"ports.{field}" for field, name in PORT_FIELDS},
}
SHARED_FIELDS = {
    "ENGINE_IMAGE": "spec.engines.image",
    "ENGINE_MODEL_NAME": "spec.engines.model.name",
    "MODEL_TREE_SHA256": "spec.engines.model.treeSha256",
    **{name: f"spec.engines.ports.{field}" for field, name in PORT_FIELDS},
}
_VARIABLE = re.compile(r"\bNARWHAL_(?:NODE_([1-9][0-9]*)_)?([A-Z][A-Z0-9_]*[A-Z0-9])\b")
# `NARWHAL_X or NARWHAL_NODE_<n>_X`, as a message on SSH hosts names an input of one engine.
_EITHER = re.compile(r"\bNARWHAL_([A-Z][A-Z0-9_]*[A-Z0-9]) or NARWHAL_NODE_([1-9][0-9]*)_\1\b")


def _launch(name: str) -> bool:
    """Whether `name` is a variable spec.engines.launch may set."""
    return re.fullmatch(LAUNCH_REFUSED, name) is None


def _field(node: str | None, name: str) -> str | None:
    if node is not None and name in NODE_FIELDS:
        return f"spec.engines.nodes[{int(node) - 1}].{NODE_FIELDS[name]}"
    if node is None and name in SHARED_FIELDS:
        return SHARED_FIELDS[name]
    if node is None and name in NODE_FIELDS:
        return f"spec.engines.nodes[].{NODE_FIELDS[name]}"
    variable = f"NARWHAL_{f'NODE_{node}_' if node else ''}{name}"
    return f"the spec.engines.launch entry {variable}" if _launch(variable) else None


def fleet_terms(message: str) -> str:
    """`message` with each deployment variable it names, as a message on SSH hosts names
    them, replaced by the NarwhalFleet field that sets it. A variable no field sets stays."""

    def either(match: re.Match[str]) -> str:
        name, node = match[1], match[2]
        if name in NODE_FIELDS:
            return str(_field(node, name))
        variable = f"NARWHAL_{name}"
        if _launch(variable):
            return f"the spec.engines.launch entry {variable} or NARWHAL_NODE_{node}_{name}"
        return match[0]

    def one(match: re.Match[str]) -> str:
        return _field(match[1], match[2]) or match[0]

    # The text between the pairs is translated variable by variable.
    found = list(_EITHER.finditer(message))
    out = []
    for index, match in enumerate(found):
        text = message[(found[index - 1].end() if index else 0) : match.start()]
        out.append(_VARIABLE.sub(one, text))
        out.append(either(match))
    out.append(_VARIABLE.sub(one, message[found[-1].end() if found else 0 :]))
    return "".join(out)
