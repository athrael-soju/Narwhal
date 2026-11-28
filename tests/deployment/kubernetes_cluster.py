"""A stubbed Kubernetes cluster for `narwhal deploy --target kubernetes` and controller tests.

`ObjectStore` is an in-memory Kubernetes API server with the semantics the controller relies on:
resourceVersions and conflicts, `generation`, watches from an event log with 410 Gone once a
resourceVersion has expired, owner-reference garbage collection, finalizers, server-side apply
with field managers and `managedFields`, Jobs that create pods and track them with a finalizer,
a scheduler that honours `schedulingGates`, node selectors and node affinity against the Nodes
the store holds, a kubelet that moves pods through their phases and gives a deleted pod its
grace period, and Leases. A custom resource defined from its CRD (`define_crd`) is pruned to its
structural schema, with the API server's warnings in `last_warnings`. Tests reach it
through its Python verbs or, as `runtime/kubernetes_api.py` does, over HTTP:
`lightkube.Client(config, transport=store.transport())`. A test injects API failures with
`fail()` and advances time with the store's `Clock`; `settle()` runs the controllers, the
scheduler, the kubelet and the garbage collector until the cluster stops changing.

`Rig` holds one fleet's cluster across the runs of a test. The target reaches the store through
`kubernetes_target.ApiCluster` over HTTP, and the store's controllers, scheduler and kubelet
run before every request and on every wait. A gate Job's pod prints the log its command would
print. A router check fails when no router pod runs. A Job that calls an engine fails unless
it holds the engine API key from the fleet's Secret and its role file passes the key on. The
rig also patches the library functions the SSH tests stub: record building, engine set values,
cache groups, the fabric and the attestation document.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import copy
import functools
import hashlib
import hmac
import io
import json
import random
import re
import tarfile
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from narwhal.deployment import (
    fleet_resource,
    kubernetes_gate,
    kubernetes_objects,
    kubernetes_target,
)
from narwhal.deployment.hosts import Host, write_private
from narwhal.deployment.launch_engine.backend import launcher
from narwhal.deployment.role_env import helpers, write_environment
from narwhal.runtime.kubernetes_api import ApiClient, Connection
from narwhal.serving.control_token import ROUTER_TOKEN_ENV

# The API server's refusals.

_REASONS = {
    400: "BadRequest",
    403: "Forbidden",
    404: "NotFound",
    405: "MethodNotAllowed",
    409: "Conflict",
    410: "Expired",
    415: "UnsupportedMediaType",
    422: "Invalid",
    429: "TooManyRequests",
    500: "InternalError",
    503: "ServiceUnavailable",
    504: "Timeout",
}


class ApiError(Exception):
    """A refusal, with the `Status` object the API server sends for it."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        reason: str | None = None,
        kind: str = "",
        name: str = "",
        causes: Sequence[Mapping[str, str]] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason or _REASONS.get(code, "Unknown")
        self.message = message
        self.kind = kind
        self.name = name
        self.causes = [dict(cause) for cause in causes]

    def status(self) -> dict:
        details: dict[str, Any] = {k: v for k, v in (("kind", self.kind), ("name", self.name)) if v}
        if self.causes:
            details["causes"] = self.causes
        status = {
            "kind": "Status",
            "apiVersion": "v1",
            "metadata": {},
            "status": "Failure",
            "message": self.message,
            "reason": self.reason,
            "code": self.code,
        }
        if details:
            status["details"] = details
        return status


# Kinds.


@dataclass(frozen=True)
class Kind:
    """One API resource: where it is served and which server semantics it has."""

    group: str
    version: str
    kind: str
    plural: str
    namespaced: bool = True
    # A `status` subresource: writes to the object leave status alone, and the reverse.
    status: bool = False
    # A change outside metadata and status increments `metadata.generation`.
    generation: bool = False
    # The propagation policy of a delete that names none.
    propagation: str = "Background"
    # A custom resource: its CRD's structural schema prunes unknown fields and gives its lists
    # their server-side apply types, and it takes no strategic merge patch.
    custom: bool = False
    schema: Any = field(default=None, compare=False, hash=False)

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    @property
    def resource(self) -> str:
        """The name the API server's messages use, such as `jobs.batch`."""
        return f"{self.plural}.{self.group}" if self.group else self.plural

    def list_type(self, names: Sequence[str], items: Sequence[Any]) -> tuple[str, ...] | str | None:
        """How server-side apply treats the list at field path `names`: the keys of a map
        list, "set", or None for an atomic list. A custom resource's lists outside its metadata
        are atomic unless its schema says `x-kubernetes-list-type: map` or `set`."""
        if not self.custom or tuple(names[:1]) == ("metadata",):
            return "set" if names[-1] in _SET_LISTS else _list_keys(names[-1], items)
        node = _schema_at(self.schema, names)
        listed = (node or {}).get("x-kubernetes-list-type")
        if listed == "set":
            return "set"
        if listed == "map":
            keys = tuple((node or {}).get("x-kubernetes-list-map-keys") or ())
            if keys and all(isinstance(item, dict) and keys[0] in item for item in items):
                return keys
        return None


POD = Kind("", "v1", "Pod", "pods", status=True)
CONFIG_MAP = Kind("", "v1", "ConfigMap", "configmaps")
SECRET_KIND = Kind("", "v1", "Secret", "secrets")
SERVICE = Kind("", "v1", "Service", "services", status=True)
SERVICE_ACCOUNT = Kind("", "v1", "ServiceAccount", "serviceaccounts")
PERSISTENT_VOLUME_CLAIM = Kind(
    "", "v1", "PersistentVolumeClaim", "persistentvolumeclaims", status=True
)
NODE = Kind("", "v1", "Node", "nodes", namespaced=False, status=True)
NAMESPACE_KIND = Kind("", "v1", "Namespace", "namespaces", namespaced=False, status=True)
STATEFUL_SET = Kind("apps", "v1", "StatefulSet", "statefulsets", status=True, generation=True)
DEPLOYMENT = Kind("apps", "v1", "Deployment", "deployments", status=True, generation=True)
# The API default for a Job delete orphans its pods.
JOB = Kind("batch", "v1", "Job", "jobs", status=True, generation=True, propagation="Orphan")
LEASE = Kind("coordination.k8s.io", "v1", "Lease", "leases")
CRD = Kind(
    "apiextensions.k8s.io",
    "v1",
    "CustomResourceDefinition",
    "customresourcedefinitions",
    namespaced=False,
    status=True,
    generation=True,
)
KINDS = (
    POD,
    CONFIG_MAP,
    SECRET_KIND,
    SERVICE,
    SERVICE_ACCOUNT,
    PERSISTENT_VOLUME_CLAIM,
    NODE,
    NAMESPACE_KIND,
    STATEFUL_SET,
    DEPLOYMENT,
    JOB,
    LEASE,
    CRD,
)


def custom_kind(
    group: str,
    version: str,
    kind: str,
    plural: str,
    *,
    namespaced: bool = True,
    schema: Mapping[str, Any] | None = None,
) -> Kind:
    """A custom resource with a status subresource, as a CRD with `subresources.status` serves.
    `schema` is its structural `openAPIV3Schema`."""
    return Kind(
        group,
        version,
        kind,
        plural,
        namespaced=namespaced,
        status=True,
        generation=True,
        custom=True,
        schema=schema,
    )


def node(name: str, **labels: str) -> dict:
    """A Node as its kubelet registers it, with the `kubernetes.io/hostname` label."""
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name, **labels}},
    }


# Time.

START = datetime(2026, 11, 27, 10, 0, tzinfo=UTC).timestamp()


class Clock:
    """The cluster's time, in seconds since the epoch. Tests move it with `advance`."""

    def __init__(self, start: float = START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _time(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _micro_time(seconds: float) -> str:
    """A Lease's MicroTime."""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Selectors.

_REQUIREMENT = re.compile(
    r"^\s*(?P<not>!)?\s*(?P<key>[A-Za-z0-9./_-]+)\s*"
    r"(?:(?P<op>==|!=|=|\s+in\s+|\s+notin\s+)\s*(?P<value>\([^)]*\)|[A-Za-z0-9._-]*))?\s*$"
)


def _split_requirements(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part for part in parts if part.strip()]


def match_labels(text: str | None) -> Callable[[Mapping[str, str]], bool]:
    """A test for an object's labels, from a selector's string form."""
    if not text:
        return lambda labels: True
    tests: list[Callable[[Mapping[str, str]], bool]] = []
    for requirement in _split_requirements(text):
        match = _REQUIREMENT.match(requirement)
        if match is None:
            raise ApiError(400, f"unable to parse requirement: {requirement!r}")
        key, op, value = match["key"], (match["op"] or "").strip(), match["value"]
        if match["not"]:
            if op:
                raise ApiError(400, f"unable to parse requirement: {requirement!r}")
            tests.append(lambda labels, key=key: key not in labels)
        elif not op:
            tests.append(lambda labels, key=key: key in labels)
        elif op in ("in", "notin"):
            if not (value or "").startswith("("):
                raise ApiError(400, f"unable to parse requirement: {requirement!r}")
            values = {item.strip() for item in value[1:-1].split(",") if item.strip()}
            if op == "in":
                tests.append(lambda labels, key=key, values=values: labels.get(key) in values)
            else:
                tests.append(lambda labels, key=key, values=values: labels.get(key) not in values)
        elif op == "!=":
            tests.append(lambda labels, key=key, value=value: labels.get(key) != value)
        else:
            tests.append(lambda labels, key=key, value=value: labels.get(key) == value)
    return lambda labels: all(test(labels) for test in tests)


def selector_matches(selector: Mapping[str, Any] | None, labels: Mapping[str, str]) -> bool:
    """Whether a `LabelSelector` object (matchLabels, matchExpressions) selects `labels`."""
    if not selector:
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        key, op, values = expression["key"], expression["operator"], expression.get("values", [])
        present = key in labels
        if (
            (op == "In" and labels.get(key) not in values)
            or (op == "NotIn" and present and labels[key] in values)
            or (op == "Exists" and not present)
            or (op == "DoesNotExist" and present)
        ):
            return False
    return True


# The field selectors each kind serves, beyond metadata.name and metadata.namespace.
_FIELD_LABELS = {
    "Pod": {
        "spec.nodeName",
        "spec.restartPolicy",
        "spec.schedulerName",
        "spec.serviceAccountName",
        "status.phase",
        "status.podIP",
    },
    "Job": {"status.successful"},
    "Node": {"spec.unschedulable"},
    "Event": {"regarding.kind", "regarding.name", "regarding.namespace", "reason", "type"},
}


def match_fields(kind: Kind, text: str | None) -> Callable[[Mapping[str, Any]], bool]:
    if not text:
        return lambda obj: True
    tests: list[Callable[[Mapping[str, Any]], bool]] = []
    for requirement in _split_requirements(text):
        match = re.match(r"^\s*([A-Za-z.]+)\s*(==|!=|=)\s*(.*?)\s*$", requirement)
        if match is None:
            raise ApiError(400, f"invalid field selector: {requirement!r}")
        path, op, value = match.groups()
        if path not in {"metadata.name", "metadata.namespace", *_FIELD_LABELS.get(kind.kind, ())}:
            raise ApiError(400, f'field label not supported: "{path}"')

        def test(
            obj: Mapping[str, Any], path: str = path, op: str = op, value: str = value
        ) -> bool:
            current: Any = obj
            for part in path.split("."):
                current = current.get(part) if isinstance(current, Mapping) else None
            if current is None:
                found = ""
            elif isinstance(current, bool):
                found = str(current).lower()
            else:
                found = str(current)
            return (found == value) != (op == "!=")

        tests.append(test)
    return lambda obj: all(test(obj) for test in tests)


# Field sets, as server-side apply tracks them.
#
# A path names one field in FieldsV1 form: `f:<name>` for a map key, `k:{...}` for the item of
# an associative list, `v:<json>` for a member of a set. Lists the table below names merge by
# those keys; every other list is atomic, owned and replaced as a whole.

_LIST_KEYS: dict[str, tuple[str, ...]] = {
    "conditions": ("type",),
    "containerStatuses": ("name",),
    "containers": ("name",),
    "env": ("name",),
    "ephemeralContainers": ("name",),
    "hostAliases": ("ip",),
    "imagePullSecrets": ("name",),
    "initContainerStatuses": ("name",),
    "initContainers": ("name",),
    "ownerReferences": ("uid",),
    "resourceClaims": ("name",),
    "schedulingGates": ("name",),
    "topologySpreadConstraints": ("topologyKey", "whenUnsatisfiable"),
    "volumeDevices": ("devicePath",),
    "volumeMounts": ("mountPath",),
    "volumes": ("name",),
}
_SET_LISTS = {"finalizers"}
# Metadata the server owns: never in a field set, never changed by a client write.
_SERVER_METADATA = (
    "name",
    "namespace",
    "uid",
    "resourceVersion",
    "generation",
    "creationTimestamp",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    "managedFields",
    "selfLink",
    "generateName",
)
_MISSING = object()
FieldPath = tuple[str, ...]


def _list_keys(name: str, items: Sequence[Any]) -> tuple[str, ...] | None:
    maps = [item for item in items if isinstance(item, dict)]
    if not maps or len(maps) != len(items):
        return None
    if name == "ports":
        keys = ("containerPort", "protocol") if "containerPort" in maps[0] else ("port", "protocol")
    else:
        keys = _LIST_KEYS.get(name, ())
    if not keys or not all(keys[0] in item for item in maps):
        return None
    return keys


def _key_of(item: Mapping[str, Any], keys: tuple[str, ...]) -> dict:
    found = {key: item.get(key, "TCP" if key == "protocol" else None) for key in keys}
    return {key: value for key, value in found.items() if value is not None}


def _item_path(item: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    return "k:" + json.dumps(_key_of(item, keys), separators=(",", ":"), sort_keys=True)


def _fields(
    value: Any, kind: Kind, path: FieldPath = (), names: tuple[str, ...] = ()
) -> dict[FieldPath, bool]:
    """Each field path in `value`, mapped to whether it is a leaf: a scalar or atomic list."""
    out: dict[FieldPath, bool] = {}
    if not isinstance(value, dict):
        return out
    for name, child in value.items():
        if child is None:
            continue
        here, at_names = (*path, f"f:{name}"), (*names, name)
        listed = kind.list_type(at_names, child) if isinstance(child, list) else None
        if isinstance(child, dict):
            out[here] = False
            out.update(_fields(child, kind, here, at_names))
        elif listed == "set":
            out[here] = False
            out.update({(*here, f"v:{json.dumps(item)}"): True for item in child})
        elif isinstance(listed, tuple):
            out[here] = False
            for item in child:
                at = (*here, _item_path(item, listed))
                out[at] = False
                out.update(_fields(item, kind, at, at_names))
        else:
            out[here] = True
    return out


def _step(value: Any, element: str) -> Any:
    prefix, _, rest = element.partition(":")
    if prefix == "f":
        if not isinstance(value, dict) or value.get(rest) is None:
            return _MISSING
        return value[rest]
    if not isinstance(value, list):
        return _MISSING
    wanted = json.loads(rest)
    if prefix == "v":
        return wanted if wanted in value else _MISSING
    for item in value:
        if isinstance(item, dict) and all(
            item.get(k, "TCP" if k == "protocol" else None) == v for k, v in wanted.items()
        ):
            return item
    return _MISSING


def _lookup(value: Any, path: FieldPath) -> Any:
    for element in path:
        value = _step(value, element)
        if value is _MISSING:
            break
    return value


def _remove_path(obj: Any, path: FieldPath) -> None:
    parent = _lookup(obj, path[:-1])
    if parent is _MISSING:
        return
    prefix, _, rest = path[-1].partition(":")
    if prefix == "f" and isinstance(parent, dict):
        parent.pop(rest, None)
    elif isinstance(parent, list):
        target = _step(parent, path[-1])
        if target is not _MISSING:
            parent[:] = [item for item in parent if item is not target and item != target]


def _merge_applied(live: Any, applied: Any, kind: Kind, names: tuple[str, ...] = ()) -> Any:
    """`applied` merged into `live`: maps by key, map lists by item key."""
    if isinstance(applied, dict) and isinstance(live, dict):
        out = dict(live)
        for key, value in applied.items():
            if value is not None:
                out[key] = _merge_applied(live.get(key), value, kind, (*names, key))
        return out
    if isinstance(applied, list) and isinstance(live, list):
        keys = kind.list_type(names, [*live, *applied])
        if keys == "set":
            return [*live, *(item for item in applied if item not in live)]
        if isinstance(keys, tuple):
            out = [copy.deepcopy(item) for item in live]
            for item in applied:
                at = next(
                    (i for i, have in enumerate(out) if _key_of(have, keys) == _key_of(item, keys)),
                    None,
                )
                if at is None:
                    out.append(copy.deepcopy(item))
                else:
                    out[at] = _merge_applied(out[at], item, kind, names)
            return out
    return copy.deepcopy(applied)


# Structural schemas: what a CRD's `openAPIV3Schema` keeps of a custom resource.

# ObjectMeta's fields; the API server drops any other under metadata.
_OBJECT_META = {
    "name",
    "generateName",
    "namespace",
    "uid",
    "resourceVersion",
    "generation",
    "creationTimestamp",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    "labels",
    "annotations",
    "ownerReferences",
    "finalizers",
    "managedFields",
    "selfLink",
}


def _schema_at(schema: Mapping[str, Any] | None, names: Sequence[str]) -> Mapping[str, Any] | None:
    node = schema
    for name in names:
        while node is not None and node.get("type") == "array":
            node = node.get("items")
        if node is None:
            return None
        properties = node.get("properties") or {}
        if name in properties:
            node = properties[name]
        elif isinstance(node.get("additionalProperties"), dict):
            node = node["additionalProperties"]
        else:
            return None
    return node


def _prune_value(value: Any, schema: Mapping[str, Any], at: str, warnings: list[str]) -> None:
    if isinstance(value, dict):
        properties = schema.get("properties")
        extra = schema.get("additionalProperties")
        keep = schema.get("x-kubernetes-preserve-unknown-fields") or extra is True
        for key in list(value):
            here = f"{at}.{key}" if at else key
            if properties is not None and key in properties:
                _prune_value(value[key], properties[key], here, warnings)
            elif isinstance(extra, dict):
                _prune_value(value[key], extra, here, warnings)
            elif not keep:
                del value[key]
                warnings.append(f'unknown field "{here}"')
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _prune_value(item, schema["items"], f"{at}[{index}]", warnings)


def prune(schema: Mapping[str, Any], obj: dict) -> list[str]:
    """Drop the fields of a custom resource its structural schema does not know, in place, and
    return the warnings the API server sends for them."""
    warnings: list[str] = []
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        for key in [key for key in meta if key not in _OBJECT_META]:
            del meta[key]
            warnings.append(f'unknown field "metadata.{key}"')
    body = {k: v for k, v in obj.items() if k not in ("apiVersion", "kind", "metadata")}
    _prune_value(body, schema, "", warnings)
    for key in [key for key in obj if key not in ("apiVersion", "kind", "metadata")]:
        if key not in body:
            del obj[key]
    return warnings


# Node selection, as the scheduler and the kubelet's admission check it.


def _requirement(requirement: Mapping[str, Any], present: bool, value: Any) -> bool:
    op, values = requirement.get("operator"), requirement.get("values") or []
    if op == "In":
        return present and value in values
    if op == "NotIn":
        return not present or value not in values
    if op == "Exists":
        return present
    if op == "DoesNotExist":
        return not present
    if op in ("Gt", "Lt") and present and values:
        try:
            number, bound = int(value), int(values[0])
        except (TypeError, ValueError):
            return False
        return number > bound if op == "Gt" else number < bound
    return False


def _term_matches(term: Mapping[str, Any], labels: Mapping[str, str], name: str) -> bool:
    expressions, fields = term.get("matchExpressions") or [], term.get("matchFields") or []
    # An empty term selects no node.
    if not expressions and not fields:
        return False
    for requirement in expressions:
        key = requirement.get("key", "")
        if not _requirement(requirement, key in labels, labels.get(key)):
            return False
    # metadata.name is the one node field; it is not a label.
    return all(
        requirement.get("key") == "metadata.name" and _requirement(requirement, True, name)
        for requirement in fields
    )


def node_fits(spec: Mapping[str, Any], node: Mapping[str, Any]) -> bool:
    """Whether a pod's node selector and required node affinity admit `node`."""
    labels, name = node["metadata"].get("labels") or {}, node["metadata"]["name"]
    if any(labels.get(key) != value for key, value in (spec.get("nodeSelector") or {}).items()):
        return False
    required = ((spec.get("affinity") or {}).get("nodeAffinity") or {}).get(
        "requiredDuringSchedulingIgnoredDuringExecution"
    )
    if required is None:
        return True
    return any(
        _term_matches(term, labels, name) for term in required.get("nodeSelectorTerms") or []
    )


def _encode_fields(paths: set[FieldPath]) -> dict:
    root: dict = {}
    for path in sorted(paths, key=len):
        node = root
        for element in path:
            node = node.setdefault(element, {})

    def mark(node: dict, at: FieldPath) -> None:
        for element, child in node.items():
            here = (*at, element)
            if child:
                mark(child, here)
                if here in paths:
                    child["."] = {}

    mark(root, ())
    return root


def _decode_fields(trie: Mapping[str, Any], at: FieldPath = ()) -> set[FieldPath]:
    out: set[FieldPath] = set()
    for element, child in trie.items():
        if element == ".":
            continue
        here = (*at, element)
        if not child or "." in child:
            out.add(here)
        if child:
            out |= _decode_fields(child, here)
    return out


def _dotted(path: FieldPath) -> str:
    """A path as the API server's conflict messages print it: `.spec.replicas`."""
    out = ""
    for element in path:
        prefix, _, rest = element.partition(":")
        out += f".{rest}" if prefix == "f" else f"[{rest}]"
    return out


@dataclass
class _Manager:
    manager: str
    operation: str
    subresource: str
    api_version: str
    time: str
    fields: set[FieldPath]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.manager, self.operation, self.subresource)

    def entry(self) -> dict:
        entry = {
            "manager": self.manager,
            "operation": self.operation,
            "apiVersion": self.api_version,
            "time": self.time,
            "fieldsType": "FieldsV1",
            "fieldsV1": _encode_fields(self.fields),
        }
        if self.subresource:
            entry["subresource"] = self.subresource
        return entry


def _managers(obj: Mapping[str, Any]) -> list[_Manager]:
    return [
        _Manager(
            entry["manager"],
            entry["operation"],
            entry.get("subresource", ""),
            entry.get("apiVersion", ""),
            entry.get("time", ""),
            _decode_fields(entry.get("fieldsV1") or {}),
        )
        for entry in obj.get("metadata", {}).get("managedFields") or []
    ]


def managers_of(obj: Mapping[str, Any], path: str) -> set[str]:
    """The managers that own a field of `obj` or a field below it, given as `spec.replicas`."""
    prefix = tuple(f"f:{name}" for name in path.split("."))
    return {
        manager.manager
        for manager in _managers(obj)
        if any(owned[: len(prefix)] == prefix for owned in manager.fields)
    }


# Patches.


def _pointer(text: str) -> list[str]:
    if text == "":
        return []
    if not text.startswith("/"):
        raise ValueError(f"invalid JSON pointer {text!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in text[1:].split("/")]


def _resolve(doc: Any, parts: Sequence[str], text: str) -> Any:
    for part in parts:
        if isinstance(doc, dict) and part in doc:
            doc = doc[part]
        elif isinstance(doc, list) and part.isdigit() and int(part) < len(doc):
            doc = doc[int(part)]
        else:
            raise ValueError(f"path {text!r} does not exist")
    return doc


def json_patch(doc: Any, operations: Sequence[Mapping[str, Any]]) -> Any:
    """An RFC 6902 patch of a copy of `doc`, all or nothing."""
    doc = copy.deepcopy(doc)
    for operation in operations:
        op, text = operation.get("op"), str(operation.get("path", ""))
        parts = _pointer(text)
        if op == "test":
            if _resolve(doc, parts, text) != operation.get("value"):
                raise ValueError(f"testing value {text} failed")
            continue
        if op in ("move", "copy"):
            source = _pointer(str(operation["from"]))
            value = copy.deepcopy(_resolve(doc, source, str(operation["from"])))
            if op == "move":
                doc = json_patch(doc, [{"op": "remove", "path": operation["from"]}])
            doc = json_patch(doc, [{"op": "add", "path": text, "value": value}])
            continue
        if not parts:
            if op not in ("add", "replace"):
                raise ValueError(f"cannot {op} the whole document")
            doc = copy.deepcopy(operation["value"])
            continue
        parent = _resolve(doc, parts[:-1], text)
        last = parts[-1]
        if op == "remove" or op == "replace":
            _resolve(parent, [last], text)
            if isinstance(parent, dict):
                del parent[last]
            else:
                del parent[int(last)]
            if op == "remove":
                continue
            op = "add"
        if op == "add":
            value = copy.deepcopy(operation.get("value"))
            if isinstance(parent, dict):
                parent[last] = value
            elif isinstance(parent, list) and last == "-":
                parent.append(value)
            elif isinstance(parent, list) and last.isdigit() and int(last) <= len(parent):
                parent.insert(int(last), value)
            else:
                raise ValueError(f"path {text!r} does not exist")
            continue
        raise ValueError(f"unknown operation {op!r}")
    return doc


def merge_patch(target: Any, patch: Any) -> Any:
    """An RFC 7386 merge patch."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def _directives(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _directives(v) for k, v in value.items() if not k.startswith("$")}
    if isinstance(value, list):
        return [_directives(item) for item in value]
    return copy.deepcopy(value)


def strategic_merge_patch(target: Any, patch: Any, name: str = "") -> Any:
    """A strategic merge patch: lists with merge keys merge by key, and `$patch: delete` and
    `$patch: replace` act as the API server applies them."""
    if not isinstance(patch, dict):
        return _directives(patch)
    if patch.get("$patch") == "replace":
        return _directives(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if key.startswith("$"):
            continue
        if value is None:
            out.pop(key, None)
        elif isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = _strategic_list(out[key], value, key)
        else:
            out[key] = strategic_merge_patch(out.get(key), value, key)
    return out


def _strategic_list(live: list, patch: list, name: str) -> list:
    if name in _SET_LISTS:
        return [*live, *(item for item in patch if item not in live)]
    keys = _list_keys(name, [*live, *patch])
    if keys is None:
        return _directives(patch)
    out = [copy.deepcopy(item) for item in live]
    for item in patch:
        at = next(
            (i for i, have in enumerate(out) if _key_of(have, keys) == _key_of(item, keys)), None
        )
        if item.get("$patch") == "delete":
            if at is not None:
                del out[at]
        elif at is None:
            out.append(_directives(item))
        else:
            out[at] = strategic_merge_patch(out[at], item)
    return out


# Validation.

_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_LABEL_VALUE = re.compile(r"(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?")
_CONFIG_KEY = re.compile(r"[-._a-zA-Z0-9]+")
_MICRO_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,9})?(Z|[+-]\d\d:\d\d)")
# A ConfigMap holds at most 1 MiB.
CONFIG_MAP_BYTES = 1024 * 1024
_RESTART_POLICIES = ("Always", "OnFailure", "Never")
# Pod spec fields an update may change, and the API server's refusal of any other.
_POD_UPDATE = (
    "pod updates may not change fields other than `spec.containers[*].image`,"
    "`spec.initContainers[*].image`,`spec.activeDeadlineSeconds`,`spec.tolerations` (only "
    "additions to existing tolerations),`spec.terminationGracePeriodSeconds` (allow it to be set "
    "to 1 if it was previously negative)"
)
_STATEFUL_SET_MUTABLE = {
    "replicas",
    "ordinals",
    "template",
    "updateStrategy",
    "revisionHistoryLimit",
    "persistentVolumeClaimRetentionPolicy",
    "minReadySeconds",
}
_JOB_MUTABLE = {"parallelism", "activeDeadlineSeconds", "suspend", "ttlSecondsAfterFinished"}


def _qualified_name(key: str) -> str | None:
    prefix, slash, name = key.rpartition("/")
    if slash and (not prefix or len(prefix) > 253 or not _SUBDOMAIN.fullmatch(prefix)):
        return "prefix part must be a lowercase RFC 1123 subdomain"
    if not name or len(name) > 63 or not _LABEL_VALUE.fullmatch(name):
        return "name part must be 63 characters or less, alphanumeric, '-', '_' or '.'"
    return None


def _validate_metadata(kind: Kind, meta: Mapping[str, Any]) -> list[tuple[str, str]]:
    causes = []
    name = meta.get("name", "")
    if kind in (SERVICE, NAMESPACE_KIND):
        if len(name) > 63 or not _DNS_LABEL.fullmatch(name):
            causes.append(("metadata.name", f"Invalid value: {name!r}: a lowercase RFC 1123 label"))
    elif len(name) > 253 or not _SUBDOMAIN.fullmatch(name):
        causes.append(("metadata.name", f"Invalid value: {name!r}: a lowercase RFC 1123 subdomain"))
    if kind == JOB and len(name) > 63:
        causes.append(
            ("metadata.name", f"Invalid value: {name!r}: must be no more than 63 characters")
        )
    for key, value in (meta.get("labels") or {}).items():
        if (problem := _qualified_name(key)) is not None:
            causes.append((f"metadata.labels[{key}]", f"Invalid value: {key!r}: {problem}"))
        if not isinstance(value, str) or len(value) > 63 or not _LABEL_VALUE.fullmatch(value):
            causes.append((f"metadata.labels[{key}]", f"Invalid value: {value!r}: a label value"))
    annotations = meta.get("annotations") or {}
    for key, value in annotations.items():
        if (problem := _qualified_name(key)) is not None:
            causes.append((f"metadata.annotations[{key}]", f"Invalid value: {key!r}: {problem}"))
        if not isinstance(value, str):
            causes.append((f"metadata.annotations[{key}]", "Invalid value: must be a string"))
    if sum(len(k) + len(str(v)) for k, v in annotations.items()) > 256 * 1024:
        causes.append(("metadata.annotations", "Too long: must have at most 262144 bytes"))
    references = meta.get("ownerReferences") or []
    causes += [
        (f"metadata.ownerReferences[{index}].{required}", "Required value")
        for index, reference in enumerate(references)
        for required in ("apiVersion", "kind", "name", "uid")
        if not reference.get(required)
    ]
    if sum(1 for reference in references if reference.get("controller")) > 1:
        causes.append(
            (
                "metadata.ownerReferences",
                "Invalid value: only one reference can have Controller set to true",
            )
        )
    return causes


def _validate_pod_spec(spec: Mapping[str, Any], at: str) -> list[tuple[str, str]]:
    causes = []
    containers = spec.get("containers") or []
    if not containers:
        causes.append((f"{at}.containers", "Required value"))
    names = [c.get("name") for c in [*containers, *(spec.get("initContainers") or [])]]
    for index, container in enumerate(containers):
        if not container.get("name"):
            causes.append((f"{at}.containers[{index}].name", "Required value"))
        if not container.get("image"):
            causes.append((f"{at}.containers[{index}].image", "Required value"))
    if len(set(names)) != len(names):
        causes.append((f"{at}.containers", "Duplicate value: container names must be unique"))
    if spec.get("restartPolicy", "Always") not in _RESTART_POLICIES:
        causes.append((f"{at}.restartPolicy", "Unsupported value"))
    if spec.get("nodeName") and spec.get("schedulingGates"):
        causes.append(
            (
                f"{at}.nodeName",
                "Forbidden: cannot be set until all schedulingGates have been cleared",
            )
        )
    return causes


def _validate_pod_update(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The API server's rules for a pod spec update, scheduling gates and node affinity included."""
    before, after = old.get("spec", {}), new.get("spec", {})
    gates = [gate["name"] for gate in before.get("schedulingGates") or []]
    kept = [gate["name"] for gate in after.get("schedulingGates") or []]
    if any(name not in gates for name in kept) or kept != [name for name in gates if name in kept]:
        added = next((name for name in kept if name not in gates), kept[0] if kept else "")
        return [
            (
                "spec.schedulingGates",
                f"Forbidden: only deletion is allowed, but found new scheduling gate '{added}'",
            )
        ]
    candidate = copy.deepcopy(dict(before))
    if kept:
        candidate["schedulingGates"] = [{"name": name} for name in kept]
    else:
        candidate.pop("schedulingGates", None)
    for name in ("containers", "initContainers"):
        images = {c.get("name"): c.get("image") for c in after.get(name) or []}
        for container in candidate.get(name) or []:
            if container.get("name") in images:
                container["image"] = images[container["name"]]
    if "activeDeadlineSeconds" in after:
        candidate["activeDeadlineSeconds"] = after["activeDeadlineSeconds"]
    # Tolerations may only be added: every existing one must remain.
    tolerations = after.get("tolerations") or []
    if all(toleration in tolerations for toleration in before.get("tolerations") or []):
        if "tolerations" in after:
            candidate["tolerations"] = copy.deepcopy(after["tolerations"])
        else:
            candidate.pop("tolerations", None)
    if gates:
        # A pod held at a scheduling gate may take a node selector and node affinity it did not
        # have, so a controller can pin it before it schedules.
        selector = after.get("nodeSelector")
        if selector is not None and all(
            selector.get(k) == v for k, v in (before.get("nodeSelector") or {}).items()
        ):
            candidate["nodeSelector"] = copy.deepcopy(selector)
        had = (before.get("affinity") or {}).get("nodeAffinity")
        wants = (after.get("affinity") or {}).get("nodeAffinity")
        if had is None or had == wants:
            affinity = dict(candidate.get("affinity") or {})
            if wants is None:
                affinity.pop("nodeAffinity", None)
            else:
                affinity["nodeAffinity"] = copy.deepcopy(wants)
            if affinity or "affinity" in after:
                candidate["affinity"] = affinity
            else:
                candidate.pop("affinity", None)
    if candidate != after:
        return [("spec", f"Forbidden: {_POD_UPDATE}")]
    return []


def _validate(
    kind: Kind, obj: Mapping[str, Any], old: Mapping[str, Any] | None
) -> list[tuple[str, str]]:
    causes = _validate_metadata(kind, obj.get("metadata", {}))
    spec = obj.get("spec") or {}
    if kind == POD:
        causes += (
            _validate_pod_spec(spec, "spec") if old is None else _validate_pod_update(old, obj)
        )
    elif kind == JOB:
        template = spec.get("template") or {}
        causes += _validate_pod_spec(template.get("spec") or {}, "spec.template.spec")
        if (template.get("spec") or {}).get("restartPolicy") not in ("OnFailure", "Never"):
            causes.append(
                (
                    "spec.template.spec.restartPolicy",
                    'Required value: valid values: "OnFailure", "Never"',
                )
            )
        if old is not None:
            before = {k: v for k, v in old.get("spec", {}).items() if k not in _JOB_MUTABLE}
            after = {k: v for k, v in spec.items() if k not in _JOB_MUTABLE}
            for name in sorted(set(before) | set(after)):
                if before.get(name) != after.get(name):
                    causes.append((f"spec.{name}", "Invalid value: field is immutable"))
    elif kind in (STATEFUL_SET, DEPLOYMENT):
        template = spec.get("template") or {}
        causes += _validate_pod_spec(template.get("spec") or {}, "spec.template.spec")
        labels = (template.get("metadata") or {}).get("labels") or {}
        if not selector_matches(spec.get("selector"), labels):
            causes.append(
                (
                    "spec.template.metadata.labels",
                    "Invalid value: `selector` does not match template `labels`",
                )
            )
        if old is not None and kind == STATEFUL_SET:
            before = {
                k: v for k, v in old.get("spec", {}).items() if k not in _STATEFUL_SET_MUTABLE
            }
            after = {k: v for k, v in spec.items() if k not in _STATEFUL_SET_MUTABLE}
            if before != after:
                causes.append(
                    (
                        "spec",
                        "Forbidden: updates to statefulset spec for fields other than 'replicas', "
                        "'ordinals', 'template', 'updateStrategy', 'revisionHistoryLimit', "
                        "'persistentVolumeClaimRetentionPolicy' and 'minReadySeconds' are "
                        "forbidden",
                    )
                )
    elif kind == CONFIG_MAP:
        causes += _validate_config_map(obj, old)
    elif kind == LEASE:
        duration = spec.get("leaseDurationSeconds")
        if duration is not None and (not isinstance(duration, int) or duration <= 0):
            causes.append(("spec.leaseDurationSeconds", "Invalid value: must be greater than 0"))
        transitions = spec.get("leaseTransitions")
        if transitions is not None and (not isinstance(transitions, int) or transitions < 0):
            causes.append(
                ("spec.leaseTransitions", "Invalid value: must be greater than or equal to 0")
            )
        for name in ("acquireTime", "renewTime"):
            if spec.get(name) is not None and not _MICRO_TIME.fullmatch(str(spec[name])):
                causes.append((f"spec.{name}", "Invalid value: must be an RFC 3339 MicroTime"))
    return causes


def _validate_config_map(
    obj: Mapping[str, Any], old: Mapping[str, Any] | None
) -> list[tuple[str, str]]:
    causes = []
    data, binary = obj.get("data") or {}, obj.get("binaryData") or {}
    size = 0
    for key, value in data.items():
        if len(key) > 253 or not _CONFIG_KEY.fullmatch(key):
            causes.append(
                (
                    f"data[{key}]",
                    "Invalid value: a valid config key must consist of alphanumeric "
                    "characters, '-', '_' or '.'",
                )
            )
        size += len(key) + len(str(value).encode())
    for key, value in binary.items():
        if len(key) > 253 or not _CONFIG_KEY.fullmatch(key):
            causes.append(
                (
                    f"binaryData[{key}]",
                    "Invalid value: a valid config key must consist of alphanumeric "
                    "characters, '-', '_' or '.'",
                )
            )
        if key in data:
            causes.append((f"binaryData[{key}]", "Invalid value: duplicate of key present in data"))
        try:
            size += len(key) + len(base64.b64decode(value, validate=True))
        except (binascii.Error, TypeError):
            causes.append((f"binaryData[{key}]", "Invalid value: not base64"))
    if size > CONFIG_MAP_BYTES:
        causes.append(("", f"Too long: must have at most {CONFIG_MAP_BYTES} bytes"))
    if (
        old is not None
        and old.get("immutable")
        and any(obj.get(name) != old.get(name) for name in ("data", "binaryData", "immutable"))
    ):
        causes.append(("data", "Forbidden: field is immutable when `immutable` is set"))
    return causes


def _invalid(kind: Kind, name: str, causes: Sequence[tuple[str, str]]) -> ApiError:
    detail = ", ".join(f"{at}: {message}" if at else message for at, message in causes)
    return ApiError(
        422,
        f'{kind.kind} "{name}" is invalid: {detail}',
        kind=kind.kind,
        name=name,
        causes=[
            {"field": at, "message": message, "reason": "FieldValueInvalid"}
            for at, message in causes
        ],
    )


# The kubelet's outcomes for one pod. `ObjectStore.kubelet` returns one for each pod it runs.


@dataclass(frozen=True)
class Run:
    """The containers start and keep running. A Job's pod stays active."""

    ready: bool = True
    log: str = ""


@dataclass(frozen=True)
class Exit:
    """The containers run, then exit with `code`, having printed `log`."""

    code: int = 0
    log: str = ""


@dataclass(frozen=True)
class Wait:
    """A container cannot start, as with `CreateContainerConfigError`: the pod stays Pending."""

    reason: str
    message: str = ""


@dataclass(frozen=True)
class Reject:
    """The kubelet refuses the pod at admission, as with `OutOfnvidia.com/gpu`: it fails."""

    reason: str
    message: str = ""


Outcome = Run | Exit | Wait | Reject


def default_kubelet(pod: Mapping[str, Any]) -> Outcome:
    """A Job's pod exits 0; every other pod runs. A deleted pod keeps running until its grace
    period ends, as a process that ignores SIGTERM does."""
    meta = pod.get("metadata", {})
    if meta.get("deletionTimestamp"):
        return Run()
    owners = meta.get("ownerReferences") or []
    return Exit(0) if any(owner.get("kind") == "Job" for owner in owners) else Run()


def image_id(image: str) -> str:
    """The image ID the runtime reports: the image's digest form."""
    name, at, digest = image.partition("@")
    repository = re.sub(r":[^/:]+$", "", name)
    return (
        f"{repository}@{digest if at else 'sha256:' + hashlib.sha256(image.encode()).hexdigest()}"
    )


@dataclass(frozen=True)
class Deletion:
    """One client delete, with the propagation policy it sent."""

    kind: str
    namespace: str
    name: str
    propagation: str | None


@dataclass
class _Event:
    rv: int
    kind: Kind
    namespace: str
    type: str
    obj: dict
    old: dict | None


@dataclass
class _Fault:
    verb: str
    kind: Kind | None
    name: str | None
    code: int
    message: str
    times: int
    commit: bool

    def error(self) -> ApiError:
        return ApiError(self.code, self.message)


_FAULT_MESSAGES = {
    409: "the object has been modified; please apply your changes to the latest version and try "
    "again",
    410: "too old resource version",
    429: "too many requests",
    500: "an error on the server prevented the request from succeeding",
    503: "the server is currently unable to handle the request",
    504: "the server was unable to return a response in the time allotted",
}
_SYSTEM = "kube-controller-manager"
# The finalizer the Job controller keeps on each pod until it has counted it.
TRACKING = "batch.kubernetes.io/job-tracking"
_UNKNOWN_FORMAT = (
    "the body of the request was in an unknown format - accepted media types include: "
)
_CUSTOM_PATCHES = (
    "application/json-patch+json, application/merge-patch+json, application/apply-patch+yaml"
)
_GENERATE = "bcdfghjklmnpqrstvwxz2456789"


class StoreTransport(httpx.MockTransport):
    """An httpx transport onto one store. lightkube deep-copies the transport it is given, and
    a copy must still reach the same store."""

    def __deepcopy__(self, memo: dict) -> StoreTransport:
        return self


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: ObjectStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class ObjectStore:
    """An in-memory Kubernetes API server and the controllers a test needs behind it.

    Every verb returns copies. Writes that change nothing keep the object's resourceVersion and
    send no watch event, as the API server does. Controllers, the scheduler, the kubelet and the
    garbage collector run only in `settle()`, so a test sees the cluster between their steps.
    """

    def __init__(
        self, clock: Callable[[], float] | None = None, kinds: Sequence[Kind] = KINDS
    ) -> None:
        self.clock = clock or Clock()
        self._lock = threading.RLock()
        self._objects: dict[Kind, dict[tuple[str, str], dict]] = {}
        self._kinds: dict[tuple[str, str], Kind] = {}
        self._resources: dict[tuple[str, str, str], Kind] = {}
        for kind in kinds:
            self.define(kind)
        self._rv = 0
        self._events: list[_Event] = []
        self._compacted = 0
        self._pages: dict[str, tuple[int, list[dict]]] = {}
        self._logs: dict[tuple[str, str, str], str] = {}
        self._warnings: list[str] = []
        # The warnings of the last call, as the API server sends them in `Warning` headers.
        self.last_warnings: list[str] = []
        self._random = random.Random(0)
        self.faults: list[_Fault] = []
        self.deletes: list[Deletion] = []
        self.validators: dict[Kind, list[Callable[[dict, dict | None], None]]] = {}
        self.kubelet: Callable[[dict], Outcome] = default_kubelet
        self.version = "v1.31.0"
        # A Job delete that does not send Background fails the test: the API default for Jobs
        # orphans their pods.
        self.strict_job_deletes = True

    # Kinds and keys.

    def define(self, kind: Kind) -> Kind:
        self._objects.setdefault(kind, {})
        self._kinds[(kind.api_version, kind.kind)] = kind
        self._resources[(kind.group, kind.version, kind.plural)] = kind
        return kind

    def define_crd(self, crd: Mapping[str, Any]) -> Kind:
        """Serve the custom resource a CustomResourceDefinition describes at its storage
        version, with that version's schema, and store the CRD itself."""
        spec = crd["spec"]
        version = next(v for v in spec["versions"] if v.get("storage"))
        kind = Kind(
            spec["group"],
            version["name"],
            spec["names"]["kind"],
            spec["names"]["plural"],
            namespaced=spec.get("scope", "Namespaced") == "Namespaced",
            status="status" in (version.get("subresources") or {}),
            generation=True,
            custom=True,
            schema=(version.get("schema") or {}).get("openAPIV3Schema"),
        )
        self.define(kind)
        self.create(crd, field_manager="helm")
        return kind

    def _registered(self, kind: Kind) -> Kind:
        """The kind as the store serves it, with its schema."""
        found = self._kinds.get((kind.api_version, kind.kind))
        if found is None:
            raise ApiError(404, f"the server could not find the requested resource {kind.resource}")
        return found

    def kind_of(self, obj: Mapping[str, Any]) -> Kind:
        found = self._kinds.get((obj.get("apiVersion", ""), obj.get("kind", "")))
        if found is None:
            raise ApiError(
                404, f'no matches for kind "{obj.get("kind")}" in version "{obj.get("apiVersion")}"'
            )
        return found

    def _key(self, kind: Kind, namespace: str | None, name: str) -> tuple[str, str]:
        if kind.namespaced and not namespace:
            raise ApiError(400, f"a namespace is required for {kind.resource}")
        if not kind.namespaced and namespace:
            raise ApiError(404, f"{kind.resource} is not namespaced")
        return (namespace or "" if kind.namespaced else "", name)

    def _live(self, kind: Kind, namespace: str | None, name: str) -> dict:
        found = self._objects[kind].get(self._key(kind, namespace, name))
        if found is None:
            raise ApiError(404, f'{kind.resource} "{name}" not found', kind=kind.plural, name=name)
        return found

    def _now(self) -> str:
        return _time(self.clock())

    @property
    def resource_version(self) -> str:
        """The latest resourceVersion the store has written."""
        return str(self._rv)

    # Faults.

    def fail(
        self,
        verb: str,
        kind: Kind | None = None,
        *,
        name: str | None = None,
        code: int = 503,
        message: str | None = None,
        times: int = 1,
        commit: bool = False,
    ) -> None:
        """Refuse the next `times` calls of `verb` on `kind` and `name` with `code`. With
        `commit`, each call takes effect first, as when the connection drops before the reply."""
        text = message or _FAULT_MESSAGES.get(code, "injected failure")
        self.faults.append(_Fault(verb, kind, name, code, text, times, commit))

    def _call(self, verb: str, kind: Kind, name: str | None, action: Callable[[], Any]) -> Any:
        with self._lock:
            fault = next(
                (
                    f
                    for f in self.faults
                    if f.verb == verb
                    and (f.kind is None or f.kind == kind)
                    and (f.name is None or f.name == name)
                ),
                None,
            )
            if fault is not None:
                fault.times -= 1
                if fault.times <= 0:
                    self.faults.remove(fault)
                if not fault.commit:
                    raise fault.error()
            self._warnings = []
            try:
                result = action()
            finally:
                self.last_warnings = self._warnings
            if fault is not None:
                raise fault.error()
            return result

    # Reads.

    def get(self, kind: Kind, namespace: str | None, name: str) -> dict:
        kind = self._registered(kind)
        return self._call(
            "get", kind, name, lambda: copy.deepcopy(self._live(kind, namespace, name))
        )

    def list(
        self,
        kind: Kind,
        namespace: str | None = None,
        *,
        label_selector: str | None = None,
        field_selector: str | None = None,
        limit: int | None = None,
        continue_: str | None = None,
    ) -> dict:
        """A List object. With `limit`, a `continue` token reads the next page of the same
        snapshot; it expires with the resourceVersion it was taken at."""
        kind = self._registered(kind)

        def run() -> dict:
            if continue_:
                if continue_ not in self._pages:
                    raise ApiError(400, "invalid continue token")
                rv, items = self._pages[continue_]
                if rv < self._compacted:
                    raise ApiError(
                        410,
                        "The provided continue parameter is too old to display a consistent list "
                        "result. You can start a new list without the continue parameter.",
                    )
            else:
                rv, items = self._rv, self._select(kind, namespace, label_selector, field_selector)
            page, rest = (items[:limit], items[limit:]) if limit else (items, [])
            metadata: dict[str, Any] = {"resourceVersion": str(rv)}
            if rest:
                token = uuid.uuid4().hex
                self._pages[token] = (rv, rest)
                metadata["continue"] = token
                metadata["remainingItemCount"] = len(rest)
            # The API server leaves apiVersion and kind off a list's items.
            out = [
                {k: v for k, v in item.items() if k not in ("apiVersion", "kind")} for item in page
            ]
            return {
                "apiVersion": kind.api_version,
                "kind": f"{kind.kind}List",
                "metadata": metadata,
                "items": out,
            }

        return self._call("list", kind, None, run)

    def _select(
        self, kind: Kind, namespace: str | None, labels: str | None, fields: str | None
    ) -> list[dict]:
        wanted, matches = match_labels(labels), match_fields(kind, fields)
        found = [
            copy.deepcopy(obj)
            for (ns, _), obj in sorted(self._objects[kind].items())
            if (not namespace or ns == namespace)
            and wanted(obj["metadata"].get("labels") or {})
            and matches(obj)
        ]
        return found

    def watch(
        self,
        kind: Kind,
        namespace: str | None = None,
        *,
        resource_version: str | None = None,
        label_selector: str | None = None,
        field_selector: str | None = None,
        bookmarks: bool = False,
    ) -> list[dict]:
        """The watch events after `resource_version`, as a watch delivers them before its
        timeout. Without one, the current objects arrive as ADDED. An expired resourceVersion
        raises 410 Gone, and the client lists again."""
        kind = self._registered(kind)

        def run() -> list[dict]:
            wanted = match_labels(label_selector)
            matches = match_fields(kind, field_selector)

            def selected(obj: Mapping[str, Any] | None) -> bool:
                return (
                    obj is not None and wanted(obj["metadata"].get("labels") or {}) and matches(obj)
                )

            events: list[dict] = []
            if resource_version in (None, "", "0"):
                events = [
                    {"type": "ADDED", "object": obj}
                    for obj in self._select(kind, namespace, label_selector, field_selector)
                ]
            else:
                try:
                    since = int(resource_version)
                except ValueError:
                    raise ApiError(400, f"invalid resource version {resource_version!r}") from None
                if since < self._compacted:
                    raise ApiError(410, f"too old resource version: {since} ({self._compacted})")
                for event in self._events:
                    if (
                        event.rv <= since
                        or event.kind != kind
                        or (namespace and event.namespace != namespace)
                    ):
                        continue
                    now, before = selected(event.obj), selected(event.old)
                    if event.type == "MODIFIED" and now != before:
                        kind_of_event = "ADDED" if now else "DELETED"
                    else:
                        kind_of_event = (
                            event.type if now or (event.type == "DELETED" and before) else ""
                        )
                    if kind_of_event:
                        events.append({"type": kind_of_event, "object": copy.deepcopy(event.obj)})
            if bookmarks:
                events.append(
                    {
                        "type": "BOOKMARK",
                        "object": {
                            "apiVersion": kind.api_version,
                            "kind": kind.kind,
                            "metadata": {"resourceVersion": str(self._rv)},
                        },
                    }
                )
            return events

        return self._call("watch", kind, None, run)

    @_locked
    def compact(self) -> None:
        """Expire every resourceVersion up to the current one, as etcd compaction does."""
        self._compacted = self._rv
        self._events = []

    def logs(self, namespace: str, pod: str, container: str | None = None) -> str:
        def run() -> str:
            obj = self._live(POD, namespace, pod)
            names = [c["name"] for c in obj.get("spec", {}).get("containers") or []]
            name = container
            if name is None:
                if len(names) != 1:
                    raise ApiError(
                        400,
                        f"a container name must be specified for pod {pod}, "
                        f"choose one of: [{' '.join(names)}]",
                    )
                name = names[0]
            if name not in names:
                raise ApiError(400, f"container {name} is not valid for pod {pod}")
            status = next(
                (
                    s
                    for s in obj.get("status", {}).get("containerStatuses") or []
                    if s.get("name") == name
                ),
                None,
            )
            if status is None or "waiting" in (status.get("state") or {"waiting": {}}):
                raise ApiError(
                    400, f'container "{name}" in pod "{pod}" is waiting to start: ContainerCreating'
                )
            return self._logs.get((namespace, obj["metadata"]["uid"], name), "")

        return self._call("logs", POD, pod, run)

    # Writes.

    def create(
        self, obj: Mapping[str, Any], *, field_manager: str = "test", dry_run: bool = False
    ) -> dict:
        kind = self.kind_of(obj)
        name = obj.get("metadata", {}).get("name")
        return self._call(
            "create", kind, name, lambda: self._create(kind, obj, field_manager, dry_run)
        )

    def replace(
        self,
        obj: Mapping[str, Any],
        *,
        field_manager: str = "test",
        subresource: str = "",
        dry_run: bool = False,
    ) -> dict:
        """An update (PUT). A resourceVersion in `obj` must be the live one."""
        kind = self.kind_of(obj)
        meta = obj.get("metadata", {})

        def run() -> dict:
            old = self._live(kind, meta.get("namespace"), meta.get("name", ""))
            return self._update(
                kind, old, obj, manager=field_manager, subresource=subresource, dry_run=dry_run
            )

        return self._call("replace", kind, meta.get("name"), run)

    def patch(
        self,
        kind: Kind,
        namespace: str | None,
        name: str,
        body: Any,
        *,
        patch_type: str = "merge",
        field_manager: str = "test",
        subresource: str = "",
        dry_run: bool = False,
    ) -> dict:
        """A JSON (`json`), merge (`merge`) or strategic merge (`strategic`) patch, all or
        nothing. A resourceVersion the patch sets must be the live one. A custom resource takes
        no strategic merge patch."""
        kind = self._registered(kind)

        def run() -> dict:
            old = self._live(kind, namespace, name)
            if patch_type == "strategic" and kind.custom:
                raise ApiError(415, _UNKNOWN_FORMAT + _CUSTOM_PATCHES)
            try:
                if patch_type == "json":
                    new = json_patch(old, body)
                elif patch_type == "merge":
                    new = merge_patch(old, body)
                elif patch_type == "strategic":
                    new = strategic_merge_patch(old, body)
                else:
                    raise ApiError(415, f"unsupported patch type {patch_type!r}")
            except (ValueError, KeyError, TypeError, IndexError) as error:
                raise ApiError(
                    422,
                    f"the server rejected our request due to an error in our request: {error}",
                    kind=kind.kind,
                    name=name,
                ) from None
            meta = new.get("metadata", {})
            if (
                meta.get("resourceVersion", old["metadata"]["resourceVersion"])
                != old["metadata"]["resourceVersion"]
            ):
                raise self._conflict(kind, name)
            return self._update(
                kind, old, new, manager=field_manager, subresource=subresource, dry_run=dry_run
            )

        return self._call("patch", kind, name, run)

    def apply(
        self,
        obj: Mapping[str, Any],
        *,
        field_manager: str,
        force: bool = False,
        subresource: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Server-side apply as `field_manager`. A field another manager owns with another value
        is a conflict unless `force` takes it over. A field this manager applied before and no
        longer applies goes, unless another manager owns it."""
        if not field_manager:
            raise ApiError(
                400,
                'PatchOptions.meta.k8s.io "" is invalid: fieldManager: Required value: '
                "is required for apply patch",
            )
        kind = self.kind_of(obj)
        meta = obj.get("metadata", {})
        name = meta.get("name", "")

        def run() -> dict:
            return self._apply(kind, obj, field_manager, force, subresource, dry_run)

        return self._call("apply", kind, name, run)

    def delete(
        self,
        kind: Kind,
        namespace: str | None,
        name: str,
        *,
        propagation_policy: str | None = None,
        preconditions: Mapping[str, str] | None = None,
        grace_period: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Delete the object. Its finalizers hold it, terminating, until they are removed.
        `Background` leaves its dependents to the garbage collector, `Orphan` removes their
        owner references first, and `Foreground` deletes them before the owner. A pod on a
        node terminates for its grace period: `terminationGracePeriodSeconds` unless
        `grace_period` sets it, and 0 forces it out at once."""
        kind = self._registered(kind)

        def run() -> dict:
            self.deletes.append(Deletion(kind.kind, namespace or "", name, propagation_policy))
            if kind == JOB and self.strict_job_deletes and propagation_policy != "Background":
                raise AssertionError(
                    f"Job {name} was deleted with propagationPolicy {propagation_policy!r}; "
                    "every Job delete must send Background, or the Job's pods are orphaned"
                )
            obj = self._live(kind, namespace, name)
            for field_name in ("uid", "resourceVersion"):
                wanted = (preconditions or {}).get(field_name)
                if wanted is not None and wanted != obj["metadata"].get(field_name):
                    raise ApiError(
                        409,
                        f"Precondition failed: {field_name} in precondition: {wanted}, "
                        f"{field_name} in object meta: {obj['metadata'].get(field_name)}",
                        kind=kind.plural,
                        name=name,
                    )
            if propagation_policy not in (None, "Background", "Orphan", "Foreground"):
                raise ApiError(422, f"unsupported propagationPolicy {propagation_policy!r}")
            policy = propagation_policy or kind.propagation
            return self._delete(kind, obj, policy, dry_run, grace_period)

        return self._call("delete", kind, name, run)

    # Out-of-band changes: what another client or an etcd edit does, with no validation.

    @_locked
    def put(self, obj: Mapping[str, Any]) -> dict:
        """Store `obj` as it is, with a new resourceVersion and a watch event."""
        kind = self.kind_of(obj)
        new = copy.deepcopy(dict(obj))
        meta = new.setdefault("metadata", {})
        if kind.namespaced and not meta.get("namespace"):
            raise ValueError("put needs metadata.namespace")
        key = self._key(kind, meta.get("namespace"), meta["name"])
        old = self._objects[kind].get(key)
        meta.setdefault("uid", str(uuid.uuid4()))
        meta.setdefault("creationTimestamp", self._now())
        self._commit(kind, new, old)
        return copy.deepcopy(new)

    @_locked
    def remove(self, kind: Kind, namespace: str | None, name: str) -> None:
        """Remove the object at once, as an etcd edit does, past its finalizers."""
        kind = self._registered(kind)
        obj = self._live(kind, namespace, name)
        self._remove(kind, copy.deepcopy(obj))

    @_locked
    def live(self, kind: Kind, namespace: str | None = None) -> list[dict]:
        """The stored objects themselves, oldest first. Changing one in place is an
        out-of-band edit: it takes no resourceVersion and sends no watch event."""
        kind = self._registered(kind)
        return [
            obj for (ns, _), obj in self._objects[kind].items() if not namespace or ns == namespace
        ]

    @_locked
    def restart_container(self, namespace: str, pod: str, container: str) -> dict:
        """The kubelet restarts one running container: a new container ID and start."""
        old = self._live(POD, namespace, pod)
        new = copy.deepcopy(old)
        for status in new.get("status", {}).get("containerStatuses") or []:
            if status["name"] == container:
                status["lastState"] = {
                    "terminated": {
                        "exitCode": 137,
                        "reason": "Error",
                        "containerID": status.get("containerID"),
                    }
                }
                status["containerID"] = f"containerd://{uuid.uuid4().hex}"
                status["restartCount"] = status.get("restartCount", 0) + 1
                status["state"] = {"running": {"startedAt": self._now()}}
        self._system(POD, old, new, "kubelet")
        return copy.deepcopy(self._live(POD, namespace, pod))

    # The write pipeline.

    def _conflict(self, kind: Kind, name: str) -> ApiError:
        return ApiError(
            409,
            f'Operation cannot be fulfilled on {kind.resource} "{name}": the object has been '
            "modified; please apply your changes to the latest version and try again",
            kind=kind.plural,
            name=name,
        )

    def _commit(self, kind: Kind, obj: dict, old: dict | None) -> None:
        self._rv += 1
        obj["metadata"]["resourceVersion"] = str(self._rv)
        meta = obj["metadata"]
        self._objects[kind][
            (meta.get("namespace", "") if kind.namespaced else "", meta["name"])
        ] = obj
        self._events.append(
            _Event(
                self._rv,
                kind,
                meta.get("namespace", ""),
                "MODIFIED" if old else "ADDED",
                copy.deepcopy(obj),
                copy.deepcopy(old) if old else None,
            )
        )

    def _remove(self, kind: Kind, obj: dict) -> None:
        meta = obj["metadata"]
        key = (meta.get("namespace", "") if kind.namespaced else "", meta["name"])
        old = self._objects[kind].pop(key)
        self._rv += 1
        meta["resourceVersion"] = str(self._rv)
        self._events.append(
            _Event(
                self._rv,
                kind,
                meta.get("namespace", ""),
                "DELETED",
                copy.deepcopy(obj),
                copy.deepcopy(old),
            )
        )

    def _check(self, kind: Kind, obj: dict, old: dict | None) -> None:
        causes = _validate(kind, obj, old)
        for validator in self.validators.get(kind, []):
            try:
                validator(copy.deepcopy(obj), copy.deepcopy(old) if old else None)
            except ValueError as error:
                causes.append(("", str(error)))
        if causes:
            raise _invalid(kind, obj["metadata"].get("name", ""), causes)

    def _prune(self, kind: Kind, obj: dict) -> None:
        if kind.custom and kind.schema is not None:
            self._warnings += prune(kind.schema, obj)

    def _view(self, kind: Kind, obj: Mapping[str, Any] | None, subresource: str) -> dict:
        """The part of `obj` a write to `subresource` manages."""
        if obj is None:
            return {}
        if subresource == "status":
            return {"status": copy.deepcopy(obj["status"])} if obj.get("status") is not None else {}
        out = {
            k: copy.deepcopy(v)
            for k, v in obj.items()
            if k not in ("apiVersion", "kind", "metadata") and not (kind.status and k == "status")
        }
        meta = {
            k: copy.deepcopy(v)
            for k, v in (obj.get("metadata") or {}).items()
            if k not in _SERVER_METADATA
        }
        if meta:
            out["metadata"] = meta
        return out

    def _track(
        self, kind: Kind, old: dict | None, new: dict, manager: str, subresource: str
    ) -> None:
        """Give `manager` an Update entry for the fields this write changed."""
        managers = _managers(new)
        before, after = self._view(kind, old, subresource), self._view(kind, new, subresource)
        old_fields, new_fields = _fields(before, kind), _fields(after, kind)
        changed = {
            path
            for path, leaf in new_fields.items()
            if path not in old_fields or (leaf and _lookup(before, path) != _lookup(after, path))
        }
        if changed:
            mine = next((m for m in managers if m.key == (manager, "Update", subresource)), None)
            if mine is None:
                mine = _Manager(
                    manager, "Update", subresource, new.get("apiVersion", ""), "", set()
                )
                managers.append(mine)
            for other in managers:
                if other is not mine:
                    other.fields -= {path for path in changed if new_fields[path]}
            mine.fields |= changed
            mine.time = self._now()
        self._store_managers(kind, new, managers)

    def _store_managers(self, kind: Kind, obj: dict, managers: list[_Manager]) -> None:
        for manager in managers:
            view = self._view(kind, obj, manager.subresource)
            manager.fields = {
                path for path in manager.fields if _lookup(view, path) is not _MISSING
            }
        entries = [manager.entry() for manager in managers if manager.fields]
        if entries:
            obj["metadata"]["managedFields"] = entries
        else:
            obj["metadata"].pop("managedFields", None)

    @staticmethod
    def _comparable(obj: Mapping[str, Any]) -> dict:
        out = copy.deepcopy(dict(obj))
        meta = out.get("metadata", {})
        meta.pop("resourceVersion", None)
        for entry in meta.get("managedFields") or []:
            entry.pop("time", None)
        return out

    @staticmethod
    def _spec(obj: Mapping[str, Any]) -> dict:
        return {
            k: v for k, v in obj.items() if k not in ("apiVersion", "kind", "metadata", "status")
        }

    def _create(
        self,
        kind: Kind,
        obj: Mapping[str, Any],
        manager: str,
        dry_run: bool,
        managed: Callable[[dict], None] | None = None,
    ) -> dict:
        new = copy.deepcopy(dict(obj))
        meta = new.setdefault("metadata", {})
        if kind.namespaced and not meta.get("namespace"):
            raise ApiError(400, "the object has no namespace; a namespace is required")
        if not kind.namespaced:
            meta.pop("namespace", None)
        if meta.get("resourceVersion"):
            raise ApiError(400, "resourceVersion should not be set on objects to be created")
        if not meta.get("name"):
            if not meta.get("generateName"):
                raise _invalid(
                    kind,
                    "",
                    [("metadata.name", "Required value: name or generateName is required")],
                )
            while True:
                meta["name"] = meta["generateName"] + "".join(
                    self._random.choice(_GENERATE) for _ in range(5)
                )
                if self._key(kind, meta.get("namespace"), meta["name"]) not in self._objects[kind]:
                    break
        key = self._key(kind, meta.get("namespace"), meta["name"])
        if key in self._objects[kind]:
            raise ApiError(
                409,
                f'{kind.resource} "{meta["name"]}" already exists',
                reason="AlreadyExists",
                kind=kind.plural,
                name=meta["name"],
            )
        for name in (
            "uid",
            "deletionTimestamp",
            "deletionGracePeriodSeconds",
            "managedFields",
            "generation",
        ):
            meta.pop(name, None)
        meta["uid"] = str(uuid.uuid4())
        meta["creationTimestamp"] = self._now()
        if kind.generation:
            meta["generation"] = 1
        if kind.status:
            new.pop("status", None)
        self._prune(kind, new)
        self._defaults(kind, new)
        _normalize(kind, new)
        self._check(kind, new, None)
        if managed is None:
            self._track(kind, None, new, manager, "")
        else:
            managed(new)
        if dry_run:
            return new
        self._commit(kind, new, None)
        return copy.deepcopy(new)

    def _defaults(self, kind: Kind, obj: dict) -> None:
        spec = obj.setdefault("spec", {}) if kind in (POD, JOB, STATEFUL_SET, DEPLOYMENT) else None
        if kind == POD and spec is not None:
            spec.setdefault("restartPolicy", "Always")
            spec.setdefault("terminationGracePeriodSeconds", 30)
            obj["status"] = {"phase": "Pending"}
        elif kind == JOB and spec is not None:
            uid, name = obj["metadata"]["uid"], obj["metadata"]["name"]
            spec.setdefault("backoffLimit", 6)
            spec.setdefault("completions", 1)
            spec.setdefault("parallelism", 1)
            spec.setdefault("suspend", False)
            spec.setdefault(
                "selector", {"matchLabels": {"batch.kubernetes.io/controller-uid": uid}}
            )
            labels = (
                spec.setdefault("template", {}).setdefault("metadata", {}).setdefault("labels", {})
            )
            labels.update(
                {
                    "batch.kubernetes.io/controller-uid": uid,
                    "batch.kubernetes.io/job-name": name,
                    "controller-uid": uid,
                    "job-name": name,
                }
            )
            obj["status"] = {}
        elif kind == STATEFUL_SET and spec is not None:
            spec.setdefault("replicas", 1)
            spec.setdefault("podManagementPolicy", "OrderedReady")
            spec.setdefault("updateStrategy", {"type": "RollingUpdate"})
            obj["status"] = {"replicas": 0}
        elif kind == DEPLOYMENT and spec is not None:
            spec.setdefault("replicas", 1)
            obj["status"] = {}

    def _update(
        self,
        kind: Kind,
        old: dict,
        obj: Mapping[str, Any],
        *,
        manager: str,
        subresource: str,
        dry_run: bool,
        managed: Callable[[dict], None] | None = None,
    ) -> dict:
        if subresource not in ("", "status") or (subresource and not kind.status):
            raise ApiError(404, f"{kind.resource} has no {subresource} subresource")
        new = copy.deepcopy(dict(obj))
        meta = new.setdefault("metadata", {})
        name = old["metadata"]["name"]
        if meta.get("resourceVersion") not in (None, "", old["metadata"]["resourceVersion"]):
            raise self._conflict(kind, name)
        if meta.get("uid") not in (None, "", old["metadata"]["uid"]):
            raise ApiError(
                409,
                f"Precondition failed: UID in precondition: {meta['uid']}, "
                f"UID in object meta: {old['metadata']['uid']}",
                kind=kind.plural,
                name=name,
            )
        if (new.get("apiVersion"), new.get("kind")) != (old.get("apiVersion"), old.get("kind")):
            raise ApiError(
                400, f"the object's kind {new.get('kind')!r} does not match {kind.kind!r}"
            )
        if subresource == "status":
            status = new.get("status")
            new = copy.deepcopy(old)
            if status is None:
                new.pop("status", None)
            else:
                new["status"] = status
            meta = new["metadata"]
        elif kind.status:
            if "status" in old:
                new["status"] = copy.deepcopy(old["status"])
            else:
                new.pop("status", None)
        for field_name in _SERVER_METADATA:
            if field_name in old["metadata"]:
                meta[field_name] = copy.deepcopy(old["metadata"][field_name])
            else:
                meta.pop(field_name, None)
        if old["metadata"].get("deletionTimestamp"):
            added = set(meta.get("finalizers") or []) - set(old["metadata"].get("finalizers") or [])
            if added:
                raise _invalid(
                    kind,
                    name,
                    [
                        (
                            "metadata.finalizers",
                            "Forbidden: no new finalizers can be added if the object is being "
                            f"deleted, found new finalizers {sorted(added)}",
                        )
                    ],
                )
        self._prune(kind, new)
        _normalize(kind, new)
        self._check(kind, new, old)
        if kind.generation and self._spec(new) != self._spec(old):
            meta["generation"] = old["metadata"].get("generation", 1) + 1
        if managed is None:
            self._track(kind, old, new, manager, subresource)
        else:
            managed(new)
        return self._write(kind, old, new, dry_run)

    def _write(self, kind: Kind, old: dict, new: dict, dry_run: bool) -> dict:
        if self._comparable(new) == self._comparable(old):
            return copy.deepcopy(old)
        if dry_run:
            return new
        if _removable(new):
            self._remove(kind, new)
            return copy.deepcopy(new)
        self._commit(kind, new, old)
        return copy.deepcopy(new)

    def _system(self, kind: Kind, old: dict, new: dict, manager: str) -> None:
        """A write by a controller, the scheduler or the kubelet: no validation or generation."""
        new["metadata"]["managedFields"] = copy.deepcopy(old["metadata"].get("managedFields") or [])
        self._track(kind, old, new, manager, "")
        if kind.status:
            self._track(kind, old, new, manager, "status")
        self._write(kind, old, new, False)

    def _apply(
        self,
        kind: Kind,
        obj: Mapping[str, Any],
        manager: str,
        force: bool,
        subresource: str,
        dry_run: bool,
    ) -> dict:
        meta = obj.get("metadata", {})
        if not meta.get("name"):
            raise ApiError(400, "metadata.name is required for apply")
        live = self._objects[kind].get(self._key(kind, meta.get("namespace"), meta["name"]))
        if subresource and live is None:
            raise ApiError(
                404,
                f'{kind.resource} "{meta["name"]}" not found',
                kind=kind.plural,
                name=meta["name"],
            )
        applied = self._view(kind, obj, subresource)
        if kind.custom and kind.schema is not None:
            self._warnings += prune(kind.schema, applied)
        fields = _fields(applied, kind)
        managers = _managers(live) if live else []
        key = (manager, "Apply", subresource)
        conflicts = [
            (other, path)
            for path, leaf in fields.items()
            if leaf
            and live is not None
            and _lookup(live, path) is not _MISSING
            and _lookup(live, path) != _lookup(applied, path)
            for other in managers
            if other.key != key and path in other.fields
        ]
        if conflicts and not force:
            detail = "; ".join(
                f'conflict with "{other.manager}"'
                + (f' with subresource "{other.subresource}"' if other.subresource else "")
                + f": {_dotted(path)}"
                for other, path in conflicts
            )
            raise ApiError(
                409,
                f"Apply failed with {len(conflicts)} "
                f"conflict{'s' if len(conflicts) != 1 else ''}: {detail}",
                kind=kind.plural,
                name=meta["name"],
                causes=[
                    {
                        "reason": "FieldManagerConflict",
                        "message": f'conflict with "{other.manager}"',
                        "field": _dotted(path),
                    }
                    for other, path in conflicts
                ],
            )
        for other, path in conflicts:
            other.fields.discard(path)
        mine = next((m for m in managers if m.key == key), None)
        if mine is None:
            mine = _Manager(manager, "Apply", subresource, obj.get("apiVersion", ""), "", set())
            managers.append(mine)
        others = set().union(
            *(m.fields for m in managers if m is not mine and m.subresource == subresource)
        )
        # Shortest first: a dropped list item or map takes the fields under it.
        dropped = sorted(mine.fields - set(fields), key=len)
        mine.fields = set(fields)
        mine.time = self._now()
        mine.api_version = obj.get("apiVersion", "")

        def record(new: dict) -> None:
            self._store_managers(kind, new, managers)

        if live is None:
            created = {
                "apiVersion": obj.get("apiVersion"),
                "kind": obj.get("kind"),
                "metadata": {"name": meta["name"]},
            }
            if meta.get("namespace"):
                created["metadata"]["namespace"] = meta["namespace"]
            return self._create(
                kind, _merge_applied(created, applied, kind), manager, dry_run, managed=record
            )
        if meta.get("resourceVersion") not in (None, "", live["metadata"]["resourceVersion"]):
            raise self._conflict(kind, meta["name"])
        merged = copy.deepcopy(live)
        for path in dropped:
            if not any(owned[: len(path)] == path for owned in others):
                _remove_path(merged, path)
        merged = _merge_applied(merged, applied, kind)
        if subresource == "status":
            merged = {**copy.deepcopy(live), "status": merged.get("status", {})}
        return self._update(
            kind,
            live,
            merged,
            manager=manager,
            subresource=subresource,
            dry_run=dry_run,
            managed=record,
        )

    def _delete(
        self, kind: Kind, obj: dict, policy: str, dry_run: bool, grace_period: int | None = None
    ) -> dict:
        meta = obj["metadata"]
        new = copy.deepcopy(obj)
        finalizers = list(meta.get("finalizers") or [])
        grace = 0
        if kind == POD:
            spec, phase = obj.get("spec") or {}, (obj.get("status") or {}).get("phase")
            grace = spec.get("terminationGracePeriodSeconds", 30)
            if grace_period is not None:
                grace = grace_period
            # A pod no kubelet runs, or one that finished, goes at once.
            if not spec.get("nodeName") or phase in ("Succeeded", "Failed"):
                grace = 0
        if meta.get("deletionTimestamp"):
            # Deleting again may only shorten a pod's grace period.
            current = meta.get("deletionGracePeriodSeconds") or 0
            if dry_run or kind != POD or grace_period is None or grace_period >= current:
                return copy.deepcopy(obj)
        else:
            wanted = {"Orphan": "orphan", "Foreground": "foregroundDeletion"}.get(policy)
            if wanted and wanted not in finalizers:
                finalizers.append(wanted)
        if dry_run:
            return new
        if not finalizers and grace == 0:
            self._remove(kind, new)
            return copy.deepcopy(new)
        if finalizers:
            new["metadata"]["finalizers"] = finalizers
        new["metadata"]["deletionTimestamp"] = _time(self.clock() + grace)
        new["metadata"]["deletionGracePeriodSeconds"] = grace
        if kind.generation and not meta.get("deletionTimestamp"):
            new["metadata"]["generation"] = meta.get("generation", 1) + 1
        self._commit(kind, new, obj)
        return copy.deepcopy(new)

    # The cluster behind the API.

    @_locked
    def settle(self, limit: int = 500) -> None:
        """Run the garbage collector, the Job and StatefulSet controllers, the scheduler and the
        kubelet until nothing changes. Each makes one step per object per pass, so watches see
        every transition."""
        for _ in range(limit):
            before = self._rv
            self._collect()
            self._run_jobs()
            self._run_stateful_sets()
            self._schedule()
            self._run_pods()
            if self._rv == before:
                return
        raise AssertionError(f"the stub cluster did not settle in {limit} passes")

    def _all(self) -> list[tuple[Kind, dict]]:
        return [
            (kind, obj) for kind, objects in self._objects.items() for obj in list(objects.values())
        ]

    def _current(self, kind: Kind, obj: Mapping[str, Any]) -> dict | None:
        meta = obj["metadata"]
        found = self._objects[kind].get(
            (meta.get("namespace", "") if kind.namespaced else "", meta["name"])
        )
        return found if found is not None and found["metadata"]["uid"] == meta["uid"] else None

    def _dependents(self, uid: str) -> list[tuple[Kind, dict]]:
        return [
            (kind, obj)
            for kind, obj in self._all()
            if any(ref.get("uid") == uid for ref in obj["metadata"].get("ownerReferences") or [])
        ]

    def _drop_finalizer(
        self, kind: Kind, obj: dict, finalizer: str, manager: str = "kube-garbage-collector"
    ) -> None:
        new = copy.deepcopy(obj)
        new["metadata"]["finalizers"] = [
            f for f in obj["metadata"].get("finalizers", []) if f != finalizer
        ]
        if not new["metadata"]["finalizers"]:
            del new["metadata"]["finalizers"]
        self._system(kind, obj, new, manager)

    def _collect(self) -> None:
        """The garbage collector, which identifies an owner by apiVersion, kind, name and uid."""
        for kind, stored in self._all():
            obj = self._current(kind, stored)
            if obj is None or not obj["metadata"].get("deletionTimestamp"):
                continue
            uid, finalizers = obj["metadata"]["uid"], obj["metadata"].get("finalizers") or []
            if "orphan" in finalizers:
                for dependent_kind, dependent in self._dependents(uid):
                    new = copy.deepcopy(dependent)
                    refs = [
                        r for r in dependent["metadata"]["ownerReferences"] if r.get("uid") != uid
                    ]
                    if refs:
                        new["metadata"]["ownerReferences"] = refs
                    else:
                        del new["metadata"]["ownerReferences"]
                    self._system(dependent_kind, dependent, new, "kube-garbage-collector")
                self._drop_finalizer(kind, obj, "orphan")
            elif "foregroundDeletion" in finalizers:
                # Every dependent goes, but only those that block owner deletion hold the owner.
                dependents = self._dependents(uid)
                blocking = []
                for dependent_kind, dependent in dependents:
                    blocks = any(
                        ref.get("uid") == uid and ref.get("blockOwnerDeletion")
                        for ref in dependent["metadata"].get("ownerReferences") or []
                    )
                    if blocks:
                        blocking.append(dependent)
                    if not dependent["metadata"].get("deletionTimestamp"):
                        policy = "Foreground" if blocks else "Background"
                        self._delete(dependent_kind, dependent, policy, False)
                if not blocking:
                    self._drop_finalizer(kind, obj, "foregroundDeletion")
        uids = {obj["metadata"]["uid"]: (kind, obj) for kind, obj in self._all()}
        for kind, obj in self._all():
            refs = obj["metadata"].get("ownerReferences") or []
            if not refs or self._current(kind, obj) is None:
                continue
            namespace = obj["metadata"].get("namespace", "")

            def alive(ref: Mapping[str, Any], namespace: str = namespace) -> bool:
                # An owner is the object the reference names in full; one in another
                # namespace does not count.
                owner = uids.get(ref.get("uid", ""))
                if owner is None:
                    return False
                owner_kind, owner_obj = owner
                return (
                    ref.get("apiVersion") == owner_kind.api_version
                    and ref.get("kind") == owner_kind.kind
                    and ref.get("name") == owner_obj["metadata"]["name"]
                    and (
                        not owner_kind.namespaced
                        or owner_obj["metadata"].get("namespace") == namespace
                    )
                )

            live = [ref for ref in refs if alive(ref)]
            if not live and not obj["metadata"].get("deletionTimestamp"):
                self._delete(kind, obj, "Background", False)
            elif live and len(live) < len(refs):
                new = copy.deepcopy(obj)
                new["metadata"]["ownerReferences"] = live
                self._system(kind, obj, new, "kube-garbage-collector")

    def _owned(self, kind: Kind, owner: Mapping[str, Any]) -> list[dict]:
        uid, namespace = owner["metadata"]["uid"], owner["metadata"].get("namespace", "")
        return [
            obj
            for (ns, _), obj in self._objects[kind].items()
            if ns == namespace
            and any(
                r.get("uid") == uid and r.get("controller")
                for r in obj["metadata"].get("ownerReferences") or []
            )
        ]

    @staticmethod
    def _reference(kind: Kind, owner: Mapping[str, Any]) -> dict:
        return {
            "apiVersion": kind.api_version,
            "kind": kind.kind,
            "name": owner["metadata"]["name"],
            "uid": owner["metadata"]["uid"],
            "controller": True,
            "blockOwnerDeletion": True,
        }

    def _condition(
        self,
        conditions: Sequence[Mapping[str, Any]],
        kind: str,
        status: bool,
        reason: str = "",
        message: str = "",
    ) -> list[dict]:
        text = "True" if status else "False"
        out, found = [], False
        for condition in conditions:
            if condition.get("type") != kind:
                out.append(dict(condition))
                continue
            found = True
            same = condition.get("status") == text
            new = {
                "type": kind,
                "status": text,
                "lastTransitionTime": condition.get("lastTransitionTime") if same else self._now(),
            }
            if reason:
                new["reason"] = reason
            if message:
                new["message"] = message
            out.append(new)
        if not found:
            new = {"type": kind, "status": text, "lastTransitionTime": self._now()}
            if reason:
                new["reason"] = reason
            if message:
                new["message"] = message
            out.append(new)
        return out

    def _release(self, pod: dict) -> None:
        self._drop_finalizer(POD, pod, TRACKING, _SYSTEM)

    def _run_jobs(self) -> None:
        jobs = {job["metadata"]["uid"] for job in self._objects[JOB].values()}
        # A pod whose Job is gone keeps no tracking finalizer.
        for pod in list(self._objects[POD].values()):
            refs = pod["metadata"].get("ownerReferences") or []
            if TRACKING in (pod["metadata"].get("finalizers") or []) and not any(
                r.get("kind") == "Job" and r.get("uid") in jobs for r in refs
            ):
                self._release(pod)
        for job in list(self._objects[JOB].values()):
            status = copy.deepcopy(job.get("status") or {})
            pods = self._owned(POD, job)
            finished = any(
                c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
                for c in status.get("conditions") or []
            )
            # The controller counts each finished pod once, then releases its finalizer.
            counted = [
                pod
                for pod in pods
                if pod.get("status", {}).get("phase") in ("Succeeded", "Failed")
                and TRACKING in (pod["metadata"].get("finalizers") or [])
            ]
            if finished or job["metadata"].get("deletionTimestamp"):
                for pod in counted:
                    self._release(pod)
                continue
            spec = job["spec"]
            for pod in counted:
                counter = "succeeded" if pod["status"]["phase"] == "Succeeded" else "failed"
                status[counter] = status.get(counter, 0) + 1
            active = [
                pod
                for pod in pods
                if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")
                and not pod["metadata"].get("deletionTimestamp")
            ]
            started = status.get("startTime")
            deadline = spec.get("activeDeadlineSeconds")
            ended = None
            if status.get("succeeded", 0) >= spec.get("completions", 1):
                ended = ("Complete", "Completed", "")
            elif status.get("failed", 0) > spec.get("backoffLimit", 6):
                ended = (
                    "Failed",
                    "BackoffLimitExceeded",
                    "Job has reached the specified backoff limit",
                )
            elif (
                started
                and deadline is not None
                and self.clock()
                - datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
                >= deadline
            ):
                ended = (
                    "Failed",
                    "DeadlineExceeded",
                    "Job was active longer than specified deadline",
                )
            if ended is not None:
                for pod in active:
                    self._delete(POD, pod, "Background", False)
                active = []
                status["conditions"] = self._condition(
                    status.get("conditions") or [], ended[0], True, ended[1], ended[2]
                )
                if ended[0] == "Complete":
                    status["completionTime"] = self._now()
            elif spec.get("suspend"):
                for pod in active:
                    self._delete(POD, pod, "Background", False)
                active = []
            else:
                wanted = min(
                    spec.get("parallelism", 1),
                    spec.get("completions", 1) - status.get("succeeded", 0),
                )
                for _ in range(wanted - len(active)):
                    active.append(self._job_pod(job))
                if active and not started:
                    status["startTime"] = self._now()
            status["active"] = len(active)
            if not status["active"]:
                status.pop("active")
            if status != (job.get("status") or {}):
                self._system(JOB, job, {**copy.deepcopy(job), "status": status}, _SYSTEM)
            for pod in counted:
                current = self._current(POD, pod)
                if current is not None:
                    self._release(current)

    def _job_pod(self, job: Mapping[str, Any]) -> dict:
        template = copy.deepcopy(job["spec"]["template"])
        meta = template.get("metadata") or {}
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "generateName": f"{job['metadata']['name']}-",
                "namespace": job["metadata"]["namespace"],
                "labels": meta.get("labels") or {},
                "ownerReferences": [self._reference(JOB, job)],
                "finalizers": [TRACKING],
            },
            "spec": template.get("spec") or {},
        }
        if meta.get("annotations"):
            pod["metadata"]["annotations"] = meta["annotations"]
        return self._create(POD, pod, _SYSTEM, False)

    def _run_stateful_sets(self) -> None:
        for sts in list(self._objects[STATEFUL_SET].values()):
            if sts["metadata"].get("deletionTimestamp"):
                continue
            name, spec = sts["metadata"]["name"], sts["spec"]
            template = spec.get("template") or {}
            digest = hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()
            revision = f"{name}-{digest[:10]}"
            start = (spec.get("ordinals") or {}).get("start", 0)
            wanted = {f"{name}-{i}": i for i in range(start, start + spec.get("replicas", 1))}
            owned = {pod["metadata"]["name"]: pod for pod in self._owned(POD, sts)}
            for pod_name, pod in owned.items():
                if pod_name not in wanted and not pod["metadata"].get("deletionTimestamp"):
                    self._delete(POD, pod, "Background", False)
            for pod_name, index in wanted.items():
                if pod_name in owned:
                    continue
                existing = self._objects[POD].get((sts["metadata"]["namespace"], pod_name))
                if existing is not None:
                    refs = existing["metadata"].get("ownerReferences") or []
                    labels = existing["metadata"].get("labels") or {}
                    if not any(r.get("controller") for r in refs) and selector_matches(
                        spec.get("selector"), labels
                    ):
                        new = copy.deepcopy(existing)
                        new["metadata"]["ownerReferences"] = [
                            *refs,
                            self._reference(STATEFUL_SET, sts),
                        ]
                        self._system(POD, existing, new, _SYSTEM)
                    continue
                meta = copy.deepcopy(template.get("metadata") or {})
                labels = {
                    **(meta.get("labels") or {}),
                    "controller-revision-hash": revision,
                    "statefulset.kubernetes.io/pod-name": pod_name,
                    "apps.kubernetes.io/pod-index": str(index),
                }
                pod_spec = copy.deepcopy(template.get("spec") or {})
                pod_spec["hostname"] = pod_name
                if spec.get("serviceName"):
                    pod_spec["subdomain"] = spec["serviceName"]
                pod = {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": pod_name,
                        "namespace": sts["metadata"]["namespace"],
                        "labels": labels,
                        "ownerReferences": [self._reference(STATEFUL_SET, sts)],
                        **({"annotations": meta["annotations"]} if meta.get("annotations") else {}),
                    },
                    "spec": pod_spec,
                }
                self._create(POD, pod, _SYSTEM, False)
            pods = self._owned(POD, sts)
            if (spec.get("updateStrategy") or {}).get("type", "RollingUpdate") == "RollingUpdate":
                stale = sorted(
                    (
                        p
                        for p in pods
                        if (p["metadata"].get("labels") or {}).get("controller-revision-hash")
                        != revision
                        and not p["metadata"].get("deletionTimestamp")
                    ),
                    key=lambda p: int(
                        (p["metadata"].get("labels") or {}).get("apps.kubernetes.io/pod-index", "0")
                    ),
                )
                if stale:
                    self._delete(POD, stale[-1], "Background", False)
                    pods = self._owned(POD, sts)
            ready = [p for p in pods if _pod_ready(p)]
            updated = [
                p
                for p in pods
                if (p["metadata"].get("labels") or {}).get("controller-revision-hash") == revision
            ]
            old_status = sts.get("status") or {}
            status = {
                "observedGeneration": sts["metadata"].get("generation", 1),
                "replicas": len(pods),
                "readyReplicas": len(ready),
                "availableReplicas": len(ready),
                "currentReplicas": len(updated),
                "updatedReplicas": len(updated),
                "updateRevision": revision,
                "currentRevision": revision
                if len(updated) == len(pods)
                else old_status.get("currentRevision", revision),
                "collisionCount": 0,
            }
            if status != old_status:
                self._system(STATEFUL_SET, sts, {**copy.deepcopy(sts), "status": status}, _SYSTEM)

    def _schedule(self) -> None:
        nodes = {node["metadata"]["name"]: node for node in self._objects[NODE].values()}
        for pod in list(self._objects[POD].values()):
            spec, status = pod.get("spec") or {}, pod.get("status") or {}
            if (
                pod["metadata"].get("deletionTimestamp")
                or spec.get("nodeName")
                or status.get("phase", "Pending") != "Pending"
            ):
                continue
            new = copy.deepcopy(pod)
            conditions = status.get("conditions") or []
            if spec.get("schedulingGates"):
                new.setdefault("status", {})["conditions"] = self._condition(
                    conditions,
                    "PodScheduled",
                    False,
                    "SchedulingGated",
                    "Scheduling is blocked due to non-empty scheduling gates",
                )
            else:
                fits = [
                    name
                    for name in sorted(nodes)
                    if not (nodes[name].get("spec") or {}).get("unschedulable")
                    and node_fits(spec, nodes[name])
                ]
                if fits:
                    new["spec"]["nodeName"] = fits[0]
                    new.setdefault("status", {})["conditions"] = self._condition(
                        conditions, "PodScheduled", True
                    )
                else:
                    message = (
                        f"0/{len(nodes)} nodes are available: {len(nodes)} node(s) didn't "
                        "match Pod's node affinity/selector."
                    )
                    new.setdefault("status", {})["conditions"] = self._condition(
                        conditions, "PodScheduled", False, "Unschedulable", message
                    )
            self._system(POD, pod, new, "kube-scheduler")

    def _run_pods(self) -> None:
        """Each node's kubelet: a pod bound to a node the cluster lacks never runs."""
        nodes = {node["metadata"]["name"]: node for node in self._objects[NODE].values()}
        for pod in list(self._objects[POD].values()):
            spec, status = pod.get("spec") or {}, pod.get("status") or {}
            phase = status.get("phase", "Pending")
            host = nodes.get(spec.get("nodeName", ""))
            if host is None:
                continue
            if pod["metadata"].get("deletionTimestamp"):
                self._terminate(pod, phase)
                continue
            if phase not in ("Pending", "Running"):
                continue
            if phase == "Pending" and not node_fits(spec, host):
                rejected = Reject("NodeAffinity", "Pod was rejected: Predicate NodeAffinity failed")
                outcome: Outcome = rejected
            elif phase == "Pending" and (short := self._short(pod, host)):
                outcome = short
            else:
                outcome = self.kubelet(copy.deepcopy(pod))
            new = self._pod_status(pod, phase, outcome)
            if new is not None:
                self._system(POD, pod, new, "kubelet")

    def _short(self, pod: Mapping[str, Any], host: Mapping[str, Any]) -> Reject | None:
        """The kubelet's admission of a pod's extended resources, such as GPUs: it refuses a
        pod whose limits, with those of the pods running on its node, exceed what the node
        offers."""
        allocatable = (host.get("status") or {}).get("allocatable") or {}

        def limits(of: Mapping[str, Any]) -> Counter[str]:
            found: Counter[str] = Counter()
            for container in (of.get("spec") or {}).get("containers") or []:
                for name, value in ((container.get("resources") or {}).get("limits") or {}).items():
                    if "/" in name and str(value).isdigit():
                        found[name] += int(value)
            return found

        wanted = limits(pod)
        used: Counter[str] = Counter()
        for other in self._objects[POD].values():
            if (
                other["metadata"]["uid"] != pod["metadata"]["uid"]
                and (other.get("spec") or {}).get("nodeName") == host["metadata"]["name"]
                and (other.get("status") or {}).get("phase") == "Running"
            ):
                used += limits(other)
        for name, count in sorted(wanted.items()):
            capacity = (
                int(allocatable.get(name, "0")) if str(allocatable.get(name, "0")).isdigit() else 0
            )
            if used[name] + count > capacity:
                return Reject(
                    f"OutOf{name}",
                    f"Pod was rejected: Node didn't have enough resource: {name}, requested: "
                    f"{count}, used: {used[name]}, capacity: {capacity}",
                )
        return None

    def _terminate(self, pod: dict, phase: str) -> None:
        """Stop a deleted pod's containers: when they exit, or when the grace period ends."""
        meta = pod["metadata"]
        if not meta.get("deletionGracePeriodSeconds"):
            return
        outcome = self.kubelet(copy.deepcopy(pod))
        ends = datetime.fromisoformat(meta["deletionTimestamp"].replace("Z", "+00:00")).timestamp()
        if phase == "Running" and not isinstance(outcome, Exit) and self.clock() < ends:
            return
        if phase == "Running":
            stopped = outcome if isinstance(outcome, Exit) else Exit(137)
            new = self._pod_status(pod, "Running", stopped) or copy.deepcopy(pod)
        else:
            new = copy.deepcopy(pod)
            if phase not in ("Succeeded", "Failed"):
                new.setdefault("status", {})["phase"] = "Failed"
        # The kubelet's final delete: the pod goes once its finalizers do.
        new["metadata"]["deletionGracePeriodSeconds"] = 0
        new["metadata"]["deletionTimestamp"] = self._now()
        self._system(POD, pod, new, "kubelet")

    def _pod_status(self, pod: dict, phase: str, outcome: Outcome) -> dict | None:
        new = copy.deepcopy(pod)
        status = new.setdefault("status", {})
        spec = pod.get("spec") or {}
        namespace, uid = pod["metadata"].get("namespace", ""), pod["metadata"]["uid"]
        prior = {s["name"]: s for s in status.get("containerStatuses") or []}
        conditions = status.get("conditions") or []
        if isinstance(outcome, Reject):
            if phase != "Pending":
                return None
            status.update(
                phase="Failed",
                reason=outcome.reason,
                message=outcome.message or f"Pod was rejected: {outcome.reason}",
            )
            return new
        if isinstance(outcome, Wait):
            if phase != "Pending":
                return None
            status["containerStatuses"] = [
                {
                    "name": c["name"],
                    "image": c["image"],
                    "imageID": "",
                    "ready": False,
                    "started": False,
                    "restartCount": 0,
                    "state": {
                        "waiting": {
                            "reason": outcome.reason,
                            **({"message": outcome.message} if outcome.message else {}),
                        }
                    },
                }
                for c in spec.get("containers") or []
            ]
            status["conditions"] = self._condition(
                self._condition(conditions, "PodScheduled", True),
                "Ready",
                False,
                "ContainersNotReady",
            )
            return new
        if phase == "Pending":
            ready = isinstance(outcome, Run) and outcome.ready
            status["phase"] = "Running"
            status.setdefault("startTime", self._now())
            if spec.get("initContainers"):
                status["initContainerStatuses"] = [
                    {
                        "name": c["name"],
                        "image": c["image"],
                        "imageID": image_id(c["image"]),
                        "ready": True,
                        "restartCount": 0,
                        "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
                    }
                    for c in spec["initContainers"]
                ]
            status["containerStatuses"] = [
                {
                    "name": c["name"],
                    "image": c["image"],
                    "imageID": image_id(c["image"]),
                    "containerID": f"containerd://{uuid.uuid4().hex}",
                    "ready": ready,
                    "started": True,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": self._now()}},
                }
                for c in spec.get("containers") or []
            ]
            for kind, value in (
                ("PodScheduled", True),
                ("Initialized", True),
                ("ContainersReady", ready),
                ("Ready", ready),
            ):
                conditions = self._condition(conditions, kind, value)
            status["conditions"] = conditions
            if isinstance(outcome, Run):
                for c in spec.get("containers") or []:
                    self._logs[(namespace, uid, c["name"])] = outcome.log
            return new
        if isinstance(outcome, Run):
            if all(s.get("ready") == outcome.ready for s in prior.values()):
                return None
            for s in status.get("containerStatuses") or []:
                s["ready"] = outcome.ready
            for kind in ("ContainersReady", "Ready"):
                conditions = self._condition(conditions, kind, outcome.ready)
            status["conditions"] = conditions
            return new
        status["phase"] = "Succeeded" if outcome.code == 0 else "Failed"
        for s in status.get("containerStatuses") or []:
            started = (s.get("state") or {}).get("running", {}).get("startedAt", self._now())
            s["ready"] = False
            s["state"] = {
                "terminated": {
                    "exitCode": outcome.code,
                    "reason": "Completed" if outcome.code == 0 else "Error",
                    "startedAt": started,
                    "finishedAt": self._now(),
                    "containerID": s.get("containerID", ""),
                }
            }
            self._logs[(namespace, uid, s["name"])] = outcome.log
        for kind in ("ContainersReady", "Ready"):
            conditions = self._condition(conditions, kind, False, "PodCompleted")
        status["conditions"] = conditions
        return new

    # HTTP.

    def transport(self) -> StoreTransport:
        """The store as an API server behind an httpx transport, for lightkube's `Client`."""
        return StoreTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.last_warnings = []
            try:
                response = self._serve(request)
            except ApiError as error:
                response = httpx.Response(error.code, json=error.status())
            # The API server's warnings, such as the unknown fields it pruned.
            warnings = [("Warning", f"299 - {json.dumps(text)}") for text in self.last_warnings]
            if not warnings:
                return response
            headers = [*response.headers.multi_items(), *warnings]
            return httpx.Response(response.status_code, headers=headers, content=response.content)

    def _serve(self, request: httpx.Request) -> httpx.Response:
        parts = [part for part in request.url.path.split("/") if part]
        params = request.url.params
        if parts == ["version"]:
            major, minor = self.version.lstrip("v").split(".")[:2]
            return httpx.Response(
                200, json={"major": major, "minor": minor, "gitVersion": self.version}
            )
        if parts[:1] == ["api"] and len(parts) >= 2:
            group, version, rest = "", parts[1], parts[2:]
        elif parts[:1] == ["apis"] and len(parts) >= 3:
            group, version, rest = parts[1], parts[2], parts[3:]
        else:
            raise ApiError(404, "the server could not find the requested resource")
        namespace = None
        if len(rest) >= 3 and rest[0] == "namespaces":
            namespace, rest = rest[1], rest[2:]
        if not rest or len(rest) > 3:
            raise ApiError(404, "the server could not find the requested resource")
        plural, name, sub = (
            rest[0],
            (rest[1] if len(rest) > 1 else None),
            (rest[2] if len(rest) > 2 else ""),
        )
        kind = self._resources.get((group, version, plural))
        if (
            kind is None
            or (namespace is not None and not kind.namespaced)
            or (kind.namespaced and name is not None and namespace is None)
        ):
            raise ApiError(404, "the server could not find the requested resource")
        manager = (
            params.get("fieldManager") or request.headers.get("user-agent", "unknown").split("/")[0]
        )
        dry_run = params.get("dryRun") == "All"
        method = request.method
        if method == "GET" and name is None:
            if params.get("watch") in ("true", "1"):
                try:
                    events = self.watch(
                        kind,
                        namespace,
                        resource_version=params.get("resourceVersion"),
                        label_selector=params.get("labelSelector"),
                        field_selector=params.get("fieldSelector"),
                        bookmarks=params.get("allowWatchBookmarks") == "true",
                    )
                except ApiError as error:
                    if error.code != 410:
                        raise
                    events = [{"type": "ERROR", "object": error.status()}]
                body = "".join(json.dumps(event) + "\n" for event in events)
                return httpx.Response(
                    200, content=body.encode(), headers={"Content-Type": "application/json"}
                )
            limit = params.get("limit")
            listed = self.list(
                kind,
                namespace,
                label_selector=params.get("labelSelector"),
                field_selector=params.get("fieldSelector"),
                limit=int(limit) if limit else None,
                continue_=params.get("continue"),
            )
            return httpx.Response(200, json=listed)
        if name is None and method != "POST":
            raise ApiError(405, f"{method} is not supported on a collection")
        if name is not None and sub not in ("", "status", "log"):
            raise ApiError(404, "the server could not find the requested resource")
        if sub == "log":
            if kind != POD or method != "GET":
                raise ApiError(404, "the server could not find the requested resource")
            text = self.logs(namespace or "", name, params.get("container"))
            return httpx.Response(200, text=text)
        if method == "GET":
            return httpx.Response(200, json=self.get(kind, namespace, name or ""))
        if method == "DELETE":
            options = json.loads(request.content) if request.content else {}
            policy = options.get("propagationPolicy") or params.get("propagationPolicy")
            dry_run = dry_run or "All" in (options.get("dryRun") or [])
            grace = options.get("gracePeriodSeconds", params.get("gracePeriodSeconds"))
            result = self.delete(
                kind,
                namespace,
                name or "",
                propagation_policy=policy,
                preconditions=options.get("preconditions"),
                grace_period=None if grace is None else int(grace),
                dry_run=dry_run,
            )
            if result["metadata"].get("deletionTimestamp"):
                return httpx.Response(200, json=result)
            details = {"name": name, "kind": kind.plural, "uid": result["metadata"]["uid"]}
            if kind.group:
                details["group"] = kind.group
            return httpx.Response(
                200,
                json={
                    "kind": "Status",
                    "apiVersion": "v1",
                    "metadata": {},
                    "status": "Success",
                    "details": details,
                },
            )
        body = _body(request)
        if not isinstance(body, dict) and not (method == "PATCH" and isinstance(body, list)):
            raise ApiError(400, "the request body must be a JSON object")
        if method in ("POST", "PUT") or (
            method == "PATCH" and _content_type(request) == "application/apply-patch+yaml"
        ):
            meta = body.setdefault("metadata", {})
            if kind.namespaced:
                if meta.get("namespace") not in (None, namespace):
                    raise ApiError(
                        400,
                        "the namespace of the provided object does not match the namespace "
                        "sent on the request",
                    )
                meta["namespace"] = namespace
            if name is not None and meta.get("name") not in (None, name):
                raise ApiError(400, "the name of the object does not match the name on the URL")
            if name is not None:
                meta["name"] = name
        if method == "POST":
            if (body.get("apiVersion"), body.get("kind")) != (kind.api_version, kind.kind):
                raise ApiError(
                    400, f"the object's kind {body.get('kind')!r} does not match {kind.kind!r}"
                )
            return httpx.Response(
                201, json=self.create(body, field_manager=manager, dry_run=dry_run)
            )
        if method == "PUT":
            return httpx.Response(
                200,
                json=self.replace(body, field_manager=manager, subresource=sub, dry_run=dry_run),
            )
        if method == "PATCH":
            content = _content_type(request)
            if content == "application/apply-patch+yaml":
                if not params.get("fieldManager"):
                    raise ApiError(400, "fieldManager is required for apply patch")
                if (body.get("apiVersion"), body.get("kind")) != (kind.api_version, kind.kind):
                    raise ApiError(
                        400, f"the object's kind {body.get('kind')!r} does not match {kind.kind!r}"
                    )
                result = self.apply(
                    body,
                    field_manager=manager,
                    force=params.get("force") == "true",
                    subresource=sub,
                    dry_run=dry_run,
                )
                return httpx.Response(200, json=result)
            types = {
                "application/json-patch+json": "json",
                "application/merge-patch+json": "merge",
                "application/strategic-merge-patch+json": "strategic",
            }
            if content not in types:
                raise ApiError(
                    415,
                    "the body of the request was in an unknown format - accepted media types "
                    f"include: {', '.join([*types, 'application/apply-patch+yaml'])}",
                )
            result = self.patch(
                kind,
                namespace,
                name or "",
                body,
                patch_type=types[content],
                field_manager=manager,
                subresource=sub,
                dry_run=dry_run,
            )
            return httpx.Response(200, json=result)
        raise ApiError(405, f"{method} is not supported")


def _content_type(request: httpx.Request) -> str:
    return request.headers.get("content-type", "").split(";")[0].strip()


def _body(request: httpx.Request) -> Any:
    text = request.content.decode() if request.content else "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if _content_type(request) != "application/apply-patch+yaml":
            raise ApiError(400, "the request body is not valid JSON") from None
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise ApiError(400, "the request body is not valid YAML") from None


def _normalize(kind: Kind, obj: dict) -> None:
    """Drop the empty lists the API server's serialization omits."""
    if kind == POD and obj.get("spec", {}).get("schedulingGates") == []:
        del obj["spec"]["schedulingGates"]
    meta = obj.get("metadata", {})
    for name in ("finalizers", "ownerReferences"):
        if meta.get(name) == []:
            del meta[name]


def _removable(obj: Mapping[str, Any]) -> bool:
    """A deleted object goes once it has no finalizers and no grace period left."""
    meta = obj["metadata"]
    return bool(
        meta.get("deletionTimestamp")
        and not meta.get("finalizers")
        and not meta.get("deletionGracePeriodSeconds")
    )


def _pod_ready(pod: Mapping[str, Any]) -> bool:
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


# The Kubernetes target's rig, over the store.

NAMESPACE = "fleet"
NODES = {"engine-1": "gpu-a", "engine-2": "gpu-b"}
IMAGE = "registry.example/engine@sha256:" + "e" * 64
NARWHAL_IMAGE = "registry.example/narwhal@sha256:" + "a" * 64
FABRIC_IMAGE = "registry.example/fabric@sha256:" + "b" * 64
HAPROXY_IMAGE = "registry.example/haproxy@sha256:" + "c" * 64
TREE = "f" * 64
# The engine API key, which only the Secret holds.
SECRET = "engine-key-value"
CHART = Path(__file__).resolve().parents[2] / "deploy/helm/narwhal"
SECRET_NAME = "engine-key-v1"
SECRET_KEY = "api-key"
# The routers' control token, which only its Secret holds.
CONTROL_SECRET = "c7" * 32
CONTROL_SECRET_NAME = "router-token-v1"
CONTROL_SECRET_KEY = "token"
GPU = "gpu.example.com/gpu"
RDMA = "rdma.example.com/hca"
STATE_CLAIM = "narwhal-state"
# The engines' ServiceAccount and the sidecars' user, as the chart's settings name them.
ENGINE_ACCOUNT = "narwhal-engine"
SIDECAR_USER = 10002
# The gate kinds whose commands call an engine's API.
ENGINE_CALLERS = {
    "key-check",
    "transfer-measure",
    "sidecar-verify",
    "finalize-fleet",
    "profile",
    "calibrate",
    "preflight",
}
COMPONENT = "app.kubernetes.io/component"
# The cluster's nodes: the engines' nodes, the node a misplaced engine pod lands on, and the
# routers' node.
CLUSTER_NODES = (*NODES.values(), "gpu-c", "cpu-a")
SERVER = "https://kubernetes.stub"


def keyed_kinds() -> set[str]:
    """The gate kinds whose Jobs get the engine API key, as the controller's builder gives it."""
    return set(kubernetes_objects.CREDENTIAL_KINDS)


def environment() -> dict[str, str]:
    """The deployment variables of the stub's two-engine fleet, one fabric interface for both."""
    return {
        "NARWHAL_KUBERNETES_NAMESPACE": NAMESPACE,
        "NARWHAL_KUBERNETES_GPU_RESOURCE": GPU,
        "NARWHAL_KUBERNETES_MODEL_VOLUME": '{"persistentVolumeClaim": {"claimName": "model"}}',
        "NARWHAL_KUBERNETES_STATE_CLAIM": STATE_CLAIM,
        **{f"NARWHAL_NODE_{role[-1]}_KUBERNETES_NODE": node for role, node in NODES.items()},
        **{
            f"NARWHAL_NODE_{n}_URL": f"http://engine-{n}.engines.{NAMESPACE}.svc:8100"
            for n in (1, 2)
        },
        **{
            f"NARWHAL_NODE_{n}_ATTESTATION_URL": f"http://engine-{n}.engines.{NAMESPACE}.svc:8200"
            "/v1/attestation"
            for n in (1, 2)
        },
        "NARWHAL_ENGINE_IMAGE": IMAGE,
        "NARWHAL_ENGINE_MODEL_NAME": "served-model",
        "NARWHAL_MODEL_TREE_SHA256": TREE,
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
    }


def emitted(files: Mapping[str, bytes]) -> str:
    """The log `kubernetes_gate run` prints for `files`."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        kubernetes_gate.emit(dict(files))
    return out.getvalue()


def _returns(command: Sequence[str]) -> dict[str, str]:
    found = {}
    for index, item in enumerate(command):
        if item == "--return":
            name, _, path = command[index + 1].partition("=")
            found[name] = path
    return found


class _Objects(MutableMapping[str, dict]):
    """One kind's objects in the rig's namespace, by name, as test_kubernetes_target reads them.

    Reading gives the stored object itself, so a test's in-place edit is an out-of-band change;
    setting stores an object as it is; deleting removes it at once.
    """

    def __init__(
        self, store: ObjectStore, kind: Kind, keep: Callable[[dict], bool] = lambda obj: True
    ) -> None:
        self.store, self.kind, self.keep = store, kind, keep

    def _found(self) -> dict[str, dict]:
        return {
            obj["metadata"]["name"]: obj
            for obj in self.store.live(self.kind, NAMESPACE)
            if self.keep(obj)
        }

    def __getitem__(self, name: str) -> dict:
        return self._found()[name]

    def __setitem__(self, name: str, obj: dict) -> None:
        new = copy.deepcopy(obj)
        new.setdefault("apiVersion", self.kind.api_version)
        new.setdefault("kind", self.kind.kind)
        new.setdefault("metadata", {}).update(name=name, namespace=NAMESPACE)
        self.store.put(new)
        self.store.settle()

    def __delitem__(self, name: str) -> None:
        if name not in self._found():
            raise KeyError(name)
        self.store.remove(self.kind, NAMESPACE, name)
        self.store.settle()

    def __iter__(self) -> Iterator[str]:
        return iter(self._found())

    def __len__(self) -> int:
        return len(self._found())


def _workload(pod: Mapping[str, Any]) -> bool:
    """A pod the fleet's StatefulSets run, not a Job's."""
    return not any(r.get("kind") == "Job" for r in pod["metadata"].get("ownerReferences") or [])


def _gate_inputs(config_map: Mapping[str, Any]) -> bool:
    return (config_map["metadata"].get("labels") or {}).get(COMPONENT) == kubernetes_objects.GATE


# The kubelet's expansion of a container's command and environment: `$$` is `$`, and `$(NAME)`
# is the value of a variable the container defines before it, or stays as written.
EXPANSION = re.compile(r"\$\$|\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


def kubelet_expand(value: str, defined: Mapping[str, str]) -> str:
    return EXPANSION.sub(lambda m: "$" if m[0] == "$$" else defined.get(m[1], m[0]), value)


def expanded(container: Mapping[str, Any]) -> tuple[list[str], list[dict]]:
    """The command and environment a container's process gets, once the kubelet expands them."""
    defined: dict[str, str] = {}
    env = []
    for item in container.get("env") or []:
        if "value" in item:
            item = {**item, "value": kubelet_expand(item["value"], defined)}
            defined[item["name"]] = item["value"]
        env.append(item)
    return [kubelet_expand(part, defined) for part in container["command"]], env


def entry_of(job: Mapping[str, Any]) -> dict:
    """A gate Job in the shape the target's chart entries had: its name and kind, the command
    and environment its process gets, node, image, resources other than the RDMA device, model
    volume and mounts."""
    spec = job["spec"]["template"]["spec"]
    [container] = spec["containers"]
    command, env = expanded(container)
    volumes = {volume["name"]: volume for volume in spec.get("volumes") or []}
    limits = dict((container.get("resources") or {}).get("limits") or {})
    limits.pop(RDMA, None)
    entry: dict[str, Any] = {
        "name": job["metadata"]["name"],
        "kind": job["metadata"]["labels"][fleet_resource.GATE_LABEL],
        "command": command,
        "deadlineSeconds": job["spec"]["activeDeadlineSeconds"],
        "image": container["image"],
        "env": {item["name"]: item for item in env},
        "keep": next(
            m["subPath"] for m in container["volumeMounts"] if m["mountPath"] == "/narwhal-keep"
        ),
    }
    optional = {
        "node": spec.get("nodeName"),
        "resources": limits or None,
        "hostNetwork": spec.get("hostNetwork"),
        "serviceAccount": bool(spec.get("serviceAccountName")) or None,
        "inputs": (volumes.get(kubernetes_objects.INPUT_VOLUME) or {})
        .get("configMap", {})
        .get("name"),
        "modelVolume": (
            {k: v for k, v in volumes[kubernetes_objects.MODEL_VOLUME].items() if k != "name"}
            if kubernetes_objects.MODEL_VOLUME in volumes
            else None
        ),
    }
    entry.update({name: value for name, value in optional.items() if value is not None})
    return entry


@dataclass
class _Gate:
    """What one gate Job's pod does when the kubelet runs it."""

    entry: dict
    outcome: Outcome | None = None


class Rig:
    """The cluster's state across the runs of one test, and what each Job saw.

    The target reaches the store through `ApiCluster` over HTTP. The store's controllers, its
    scheduler and its kubelet run before every request and on every wait, as a cluster's run
    beside its API server.
    """

    def __init__(self, root: Path) -> None:
        from tests.deployment import fleet_fixtures

        self.root = root
        self.store = ObjectStore()
        for name in CLUSTER_NODES:
            document = self.store.create(node(name))
            if name.startswith("gpu-"):
                document["status"] = {"allocatable": {GPU: "2", RDMA: "2"}}
                self.store.put(document)
        self.store.define_crd(fleet_resource.crd())
        fleet = fleet_fixtures.fleet()
        fleet["metadata"].pop("uid")
        fleet["spec"]["engines"].pop("launch")
        fleet["spec"]["engines"]["apiKeySecret"] = {"name": SECRET_NAME, "key": SECRET_KEY}
        self.fleet = self.store.create(fleet, field_manager="helm")
        for name, key, value in (
            (SECRET_NAME, SECRET_KEY, SECRET),
            (CONTROL_SECRET_NAME, CONTROL_SECRET_KEY, CONTROL_SECRET),
        ):
            self.store.create(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": name, "namespace": NAMESPACE},
                    "data": {key: base64.b64encode(value.encode()).decode()},
                }
            )
        # The controller's settings, as the narwhal chart writes controller.json.
        self.settings = {
            "release": "narwhal",
            "stateClaim": STATE_CLAIM,
            "engineServiceAccount": ENGINE_ACCOUNT,
            "sidecarUser": SIDECAR_USER,
            # The controller's own image, which the chart pins and every Narwhal Job runs.
            "image": NARWHAL_IMAGE,
            "runAsUser": 10001,
            "runAsGroup": 10001,
        }
        self.env = kubernetes_target.settings_env(self.fleet, self.settings)
        self.store.kubelet = self.kubelet
        self.store.validators.setdefault(JOB, []).append(self._created)
        for kind in (STATEFUL_SET, DEPLOYMENT):
            self.store.validators.setdefault(kind, []).append(self._workload_created)
        self.pods: MutableMapping[str, dict] = _Objects(self.store, POD, _workload)
        self.config_maps: MutableMapping[str, dict] = _Objects(self.store, CONFIG_MAP, _gate_inputs)
        # Every gate Job created, oldest first: its chart-shaped entry and what its pod did.
        self.jobs: dict[str, dict] = {}
        # Every gate Job creation the API server stored, a Job made again included.
        self.created: list[dict] = []
        # Every Job, StatefulSet and Deployment as the API server first stores it.
        self.workloads: list[dict] = []
        self._gates: dict[str, _Gate] = {}
        # Each Job's inputs ConfigMap as the Job found it.
        self.inputs: dict[str, dict] = {}
        self.kinds: list[str] = []
        # Each gate Job, StatefulSet and Deployment as it is created: ("Job", kind) or
        # (kind, name).
        self.timeline: list[tuple[str, str]] = []
        # Called on each wait of the target, before the cluster moves on.
        self.on_tick: Callable[[], None] | None = None
        # Each delete's name and the uid it named as its precondition, or None.
        self.preconditions: list[tuple[str, str | None]] = []
        # Until this time on the store's clock, every request meets a 503.
        self.outage_until = 0.0
        # A node selector the router pods take instead of their node, to wait for one.
        self.router_selector: dict[str, str] | None = None
        self.calls: Counter[str] = Counter()
        # Each request's RBAC verb and resource, as `(verb, group, resource)`.
        self.requests: list[tuple[str, str, str]] = []
        self.routes: dict[str, str] = {}
        self.fail_kind: str | None = None
        self.fail_status = 1
        self.truncate_kind: str | None = None
        # Gate kinds whose pods run until a test releases them.
        self.hold: set[str] = set()
        # The Secret and key the engines read their API key from.
        self.secret = {"name": SECRET_NAME, "key": SECRET_KEY}
        # The Secret and key the routers read their control token from.
        self.control_secret = {"name": CONTROL_SECRET_NAME, "key": CONTROL_SECRET_KEY}
        # Jobs by name a test let finish although their kind is held.
        self.released: set[str] = set()
        # Gate kinds whose pods meet an outcome of the test's.
        self.outcomes: dict[str, Outcome] = {}
        # Pods by name whose kubelet gives an outcome of the test's.
        self.pod_outcomes: dict[str, Outcome] = {}
        # The KV cache layout each engine's live cache resolves.
        self.layouts: dict[str, str] = {}
        # Engine pods a scheduler places off their node, once.
        self.misplace: dict[str, str] = {}
        # The host-network TCP ports another workload listens on, by node.
        self.listening: dict[str, tuple[int, ...]] = {}
        # Pods another controller adds its own scheduling gate to, ahead of the placement
        # gate, once, between the run's read and its patch.
        self.gate_ahead: set[str] = set()
        self.transport = StoreTransport(self._handle)
        self._client = ApiClient(
            Connection(SERVER, NAMESPACE),
            field_manager=kubernetes_objects.MANAGER,
            transport=self.transport,
            sleep=lambda seconds: None,
        )

    # The cluster as the target reaches it.

    def cluster(self, env: Mapping[str, str], logs: Path) -> kubernetes_target.ApiCluster:
        return kubernetes_target.ApiCluster(self._client)

    def tick(self, seconds: float) -> None:
        if self.on_tick is not None:
            self.on_tick()
        self.store.clock.advance(seconds)
        self.store.settle()

    def settle(self) -> None:
        self.store.settle()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.store.clock() < self.outage_until:
            # The API server is down: the load balancer in front of it answers.
            self.calls["refused in the outage"] += 1
            return httpx.Response(503, text="no healthy upstream")
        self._before(request)
        self.store.settle()
        self.requests.append(_rbac(request))
        if request.method == "DELETE":
            options = json.loads(request.content or b"{}")
            name = request.url.path.rsplit("/", 1)[-1]
            self.preconditions.append((name, (options.get("preconditions") or {}).get("uid")))
        return self.store.handle(request)

    def _before(self, request: httpx.Request) -> None:
        """Out-of-band changes the test asked for, made just before the request they race."""
        for role, elsewhere in list(self.misplace.items()):
            pod = self.store._objects[POD].get((NAMESPACE, role))
            if pod is not None and pod["spec"].get("schedulingGates"):
                moved = copy.deepcopy(pod)
                moved["spec"].pop("schedulingGates")
                moved["spec"]["nodeName"] = elsewhere
                self.store.put(moved)
                del self.misplace[role]
        parts = request.url.path.split("/")
        if request.method == "PATCH" and parts[-2:-1] == ["pods"] and parts[-1] in self.gate_ahead:
            name = parts[-1]
            if b"schedulingGates" in request.content and b'"remove"' in request.content:
                self.gate_ahead.discard(name)
                pod = copy.deepcopy(self.store.get(POD, NAMESPACE, name))
                pod["spec"]["schedulingGates"].insert(0, {"name": "example.com/other"})
                self.store.put(pod)

    def _created(self, obj: dict, old: dict | None) -> None:
        """Record each Job, and each gate Job's entry, as the API server first stores it."""
        if old is not None:
            return
        self.workloads.append(copy.deepcopy(obj))
        gate = (obj["metadata"].get("labels") or {}).get(fleet_resource.GATE_LABEL)
        # An export Job is no stage's gate; the workstation tests run its pods.
        if gate is None or gate == "export":
            return
        entry = entry_of(obj)
        self.created.append(entry)
        if entry["name"] in self.jobs:
            # A Job made again under its name, after the earlier one went.
            self._gates[entry["name"]] = _Gate(entry)
            self.jobs[entry["name"]] = {"entry": entry, "job": copy.deepcopy(obj)}
            return
        self.kinds.append(entry["kind"])
        self.timeline.append(("Job", entry["kind"]))
        if entry["kind"] == "fabric-client" and "fabric-server" in self.hold:
            # The client starts while its server listens; the server then serves it and ends.
            servers = [
                job
                for job in self.store.live(JOB, NAMESPACE)
                if job["metadata"]["labels"].get(fleet_resource.GATE_LABEL) == "fabric-server"
                and (job.get("status") or {}).get("active")
            ]
            self.calls["client while its server runs"] += bool(servers)
            self.hold.discard("fabric-server")
        self.jobs[entry["name"]] = {"entry": entry, "job": copy.deepcopy(obj)}
        self._gates[entry["name"]] = _Gate(entry)
        if "inputs" in entry:
            found = self.store._objects[CONFIG_MAP].get((NAMESPACE, entry["inputs"]))
            if found is not None:
                self.inputs[entry["name"]] = copy.deepcopy(found)

    def _workload_created(self, obj: dict, old: dict | None) -> None:
        if old is None:
            self.workloads.append(copy.deepcopy(obj))
            self.timeline.append((obj["kind"], obj["metadata"]["name"]))

    def live_jobs(self) -> list[dict]:
        return list(self.store.live(JOB, NAMESPACE))

    def kubelet(self, pod: Mapping[str, Any]) -> Outcome:
        """A gate Job's pod prints its command's log and exits as its gate decides; every other
        pod runs."""
        name = pod["metadata"]["name"]
        job = (pod["metadata"].get("labels") or {}).get("job-name")
        if pod["metadata"].get("deletionTimestamp"):
            # Engines, routers and Jobs stop on SIGTERM.
            return Exit(0)
        if job is None or job not in self._gates:
            return self.pod_outcomes.get(name, Run())
        gate = self._gates[job]
        if gate.entry["kind"] in self.hold and job not in self.released:
            return Run(log=self.log(gate.entry))
        if gate.outcome is None:
            gate.outcome = self.outcomes.get(gate.entry["kind"]) or self.gate_outcome(gate.entry)
        return gate.outcome

    def gate_outcome(self, entry: Mapping[str, Any]) -> Exit:
        """What a gate's command does in the cluster as it stands when its pod starts."""
        routers = all(
            name in self.pods and _pod_ready(self.pods[name]) for name in kubernetes_target.ROUTERS
        )
        unreachable = entry["kind"] == "router-check" and not routers
        unauthorized = not self._keyed(entry)
        failed = entry["kind"] == self.fail_kind or unreachable or unauthorized
        status = self.fail_status if failed else 0
        if unauthorized:
            return Exit(status, "narwhal: the engine answered 401 Unauthorized\n")
        if unreachable:
            return Exit(status, "narwhal-host: no router controls the fleet\n")
        if failed:
            return Exit(status, "narwhal-gate: the check rejected the engine\n")
        return Exit(0, self.log(entry))

    def _keyed(self, entry: Mapping[str, Any]) -> bool:
        """Whether a Job that calls the engines has the key: from the fleet's Secret in its own
        variable, and through its role file to the variable the engine tools read."""
        files = self.files(entry)
        role_files = [data for name, data in files.items() if Path(name).name.startswith(".env")]
        needs = entry["kind"] in ENGINE_CALLERS or any(
            b"${NARWHAL_ENGINE_KEY:?" in data for data in role_files
        )
        if not needs:
            return True
        reference = (entry["env"].get("NARWHAL_ENGINE_KEY") or {}).get("valueFrom", {})
        held = reference.get("secretKeyRef") == self.secret
        exported = all(
            b'NARWHAL_ENGINE_API_KEY="${NARWHAL_ENGINE_KEY:?' in data for data in role_files
        )
        return held and exported

    def files(self, entry: Mapping[str, Any]) -> dict[str, bytes]:
        """The files a gate's inputs ConfigMap gave it."""
        config_map = self.inputs.get(entry["name"]) or {}
        data = config_map.get("binaryData", {})
        if kubernetes_gate.INPUTS in data:
            return unpack(config_map)
        return {name: base64.b64decode(value) for name, value in data.items()}

    # The gates' outputs.

    def outputs(self, entry: Mapping[str, Any]) -> dict[str, bytes]:
        kind = entry["kind"]
        files: dict[str, bytes] = {}
        for name in _returns(entry["command"]):
            if kind == "discovery":
                files[name] = {
                    "observed.json": b'{"packages": {}}',
                    "checkpoint.json": json.dumps(
                        {"model_tree_sha256": TREE, "file_count": 3, "files": []}
                    ).encode(),
                    "model-config.json": b'{"hidden_size": 8}',
                }[name]
            elif name == "launch/":
                files["launch/launch.json"] = json.dumps({"entry": entry["name"]}).encode()
                files["launch/kubernetes.json"] = b"{}"
            elif name == "checked.json":
                files[name] = b'{"identity": {}}'
            elif name in ("published/", "attestation/"):
                files[f"{name}engine-attestation.json"] = b'{"launch": {"plan": "p"}}'
                layout = self.layouts.get(_role(self, entry), "HND")
                files[f"{name}cache-layout.json"] = json.dumps(
                    {"schema_version": 1, "ranks": [{"rank": 0, "kv_cache_layout": layout}]}
                ).encode()
            elif kind == "key-check":
                files[name] = json.dumps(self.key_hmacs(entry)).encode()
            elif name == "verify/":
                files["verify/identity.json"] = b"{}"
            elif name == "host/":
                fleet = {"model": "served-model", "engines": [], "profiles": {"path": "p"}}
                files["host/fleet.json"] = json.dumps(fleet).encode()
                if kind == "profile":
                    files["host/profiles.json"] = b"{}"
                    files["host/profiles.samples.json"] = b"{}"
                if kind == "calibrate":
                    files["host/first-token-calibration.json"] = b'{"candidate_deadline_s": 1.5}'
                if kind == "preflight":
                    files["host/preflight-evidence.json"] = b"{}"
            else:
                files[name] = b"{}"
        return files

    def key_hmacs(self, entry: Mapping[str, Any]) -> dict[str, str]:
        """What a key check reports: the HMAC of the key in the engines' Secret now, and of the
        token in the routers' Secret when its Job holds that, under the salt its inputs gave
        it."""
        salt = bytes.fromhex(self.files(entry)["salt"].decode())
        reported = {"hmac_sha256": self.secret_hmac(self.secret, salt)}
        reference = (entry["env"].get(ROUTER_TOKEN_ENV) or {}).get("valueFrom", {})
        if reference.get("secretKeyRef") == self.control_secret:
            reported["control_hmac_sha256"] = self.secret_hmac(self.control_secret, salt)
        return reported

    def secret_hmac(self, reference: Mapping[str, str], salt: bytes) -> str:
        secret = self.store.get(SECRET_KIND, NAMESPACE, reference["name"])
        value = base64.b64decode(secret["data"][reference["key"]])
        return hmac.new(salt, value, hashlib.sha256).hexdigest()

    def set_key(self, value: str, reference: Mapping[str, str] | None = None) -> None:
        """Edit the key in the engines' Secret, or in the Secret `reference` names, in place."""
        reference = reference or self.secret
        secret = copy.deepcopy(self.store.get(SECRET_KIND, NAMESPACE, reference["name"]))
        secret["data"][reference["key"]] = base64.b64encode(value.encode()).decode()
        self.store.put(secret)

    def log(self, entry: Mapping[str, Any]) -> str:
        kind = entry["kind"]
        if kind == "fabric-probe":
            # The TCP ports another workload listens on, on the node's host network.
            ports = "".join(
                f"{kubernetes_target.LISTENING} {port:04X}\n"
                for port in self.listening.get(entry["node"], ())
            )
            interfaces = 'narwhal-interfaces [{"ifname": "fabric0"}]\n'
            return f"{interfaces}{ports}{kubernetes_target.LISTENING_DONE}\n"
        if kind == "fabric-server":
            return "narwhal-listening\nserver done\n"
        if kind == "fabric-client":
            report = base64.b64encode(b'{"end": {}}').decode()
            return f"client noise\nnarwhal-file report {report}\n"
        if kind == "transfer-measure":
            before, after = b"before 1\n", b"after 2\n"
            record = {
                "metrics_sha256": {
                    "before.prom": hashlib.sha256(before).hexdigest(),
                    "after.prom": hashlib.sha256(after).hexdigest(),
                }
            }
            files = {"before.prom": before, "after.prom": after}
            files["transfer.json"] = json.dumps(record).encode()
            return "".join(
                f"narwhal-file {name} {base64.b64encode(data).decode()}\n"
                for name, data in files.items()
            )
        if kind == "router-check":
            data = base64.b64encode(b'{"status": "ok"}').decode()
            return f"narwhal-file health.json {data}\n"
        log = "starting\n" + emitted(self.outputs(entry))
        if kind == self.truncate_kind:
            return log.rsplit("narwhal-manifest", 1)[0]
        return log

    @contextlib.contextmanager
    def patched(self) -> Iterator[None]:
        target = kubernetes_target
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(target, "KubectlCluster", self.cluster))
            stack.enter_context(patch.object(target, "_sleep", self.tick))
            stack.enter_context(patch.object(target, "_clock", self.store.clock))
            stack.enter_context(
                patch.object(
                    target.kubernetes_objects,
                    "routers",
                    functools.partial(placed_routers, self),
                )
            )
            stack.enter_context(
                patch.object(target.KubernetesDeployment, "_write_records", _write_records)
            )
            stack.enter_context(
                patch.object(target.engine_set, "statefulset_values", self.statefulset_values)
            )
            stack.enter_context(patch.object(target.cache_groups, "derive", self.derive))
            stack.enter_context(patch.object(target.fabric, "load", self.load_fabric))
            stack.enter_context(patch.object(target.fabric, "budget", self.budget))
            stack.enter_context(patch.object(target.fabric, "qualify_edge", self.qualify_edge))
            stack.enter_context(patch.object(target.fabric, "observe", self.observe))
            stack.enter_context(patch.object(target.AttestationDocument, "load", _document))
            stack.enter_context(patch.object(target, "launch_digest", lambda c, launch: "binding"))
            stack.enter_context(patch.object(target, "plan_launcher", lambda plan: _Registration()))
            stack.enter_context(patch.object(target, "CacheRegistration", _Registration))
            yield

    def statefulset_values(self, runs: Sequence[Path]) -> dict:
        """Engine set values as `engine_set.statefulset_values` returns them, for one pod template
        whose containers read the engine API key from the fleet's Secret."""
        digest = hashlib.sha256(b"".join((Path(r) / "launch.json").read_bytes() for r in runs))
        key = {"secretKeyRef": dict(self.secret)}
        return {
            "schema": "narwhal.engine-set",
            "schemaVersion": 1,
            "count": len(runs),
            "image": IMAGE,
            "gpuResource": GPU,
            "gpus": 1,
            "rdmaResource": RDMA,
            "modelVolume": json.loads(self.env["NARWHAL_KUBERNETES_MODEL_VOLUME"]),
            "stateClaim": STATE_CLAIM,
            "serviceName": "engines",
            "namespace": NAMESPACE,
            "sidecarImage": NARWHAL_IMAGE,
            "configMaps": [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": f"hooks-{digest.hexdigest()[:8]}"},
                    "immutable": True,
                    "data": {"launch.json": "{}"},
                }
            ],
            "access": {},
            "podTemplate": {
                "serviceAccountName": ENGINE_ACCOUNT,
                "initContainers": [],
                "containers": [
                    {
                        "name": kubernetes_target.ENGINE_CONTAINER,
                        "image": None,
                        "resources": None,
                        "env": [{"name": "VLLM_API_KEY", "valueFrom": key}],
                    },
                    {
                        "name": kubernetes_target.SIDECAR_CONTAINER,
                        "image": NARWHAL_IMAGE,
                        "env": [{"name": "NARWHAL_ENGINE_KEY", "valueFrom": key}],
                    },
                ],
                "volumes": [{"name": "model"}],
            },
        }

    def derive(self, launch_config, layouts, transfers, package, out) -> dict:
        self.calls["cache-groups"] += 1
        groups = [{"representative": "engine-1", "roles": ["engine-1", "engine-2"]}]
        document = {"groups": groups}
        write_private(Path(out) / "cache-groups.json", json.dumps(document).encode())
        return document

    def load_fabric(self, hosts, manifest, run, env, groups) -> _Plan:
        return _Plan(hosts, json.loads(Path(groups).read_text())["groups"], Path(groups))

    def budget(self, model, launch, workload, *, runtime_layout, transfer) -> dict:
        self.calls["budget"] += 1
        return {"required_gbps": 1.0}

    def qualify_edge(self, plan, shell, edge, budgets, directory: Path, retained=None):
        self.calls[f"fabric-edge[{edge.name}]"] += 1
        directory.mkdir(mode=0o700, parents=True)
        return [
            {
                "sample": "a-to-b",
                "link_sha256": self.routes.setdefault(edge.name, "route-1"),
                "measured_gbps": 100.0,
                "roles": {"engine-1": {"required_gbps": 12.5, "passed": True}},
            }
        ]

    def observe(self, plan, shell, edge) -> list[_Observation]:
        self.calls[f"observe[{edge.name}]"] += 1
        return [_Observation(_Sample("a-to-b"), self.routes.setdefault(edge.name, "route-1"))]


def _rbac(request: httpx.Request) -> tuple[str, str, str]:
    """The RBAC verb, API group and resource a request needs, as the API server authorizes it."""
    parts = [part for part in request.url.path.split("/") if part]
    group = "" if parts[:1] == ["api"] else parts[1]
    rest = parts[2:] if parts[:1] == ["api"] else parts[3:]
    if len(rest) >= 2 and rest[0] == "namespaces":
        rest = rest[2:]
    resource = rest[0] if rest else ""
    named = len(rest) > 1
    if len(rest) > 2:
        resource = f"{resource}/{rest[2]}"
    method = request.method
    if method == "GET":
        verb = "get" if named else "list"
    elif method == "POST":
        verb = "create"
    elif method == "DELETE":
        verb = "delete"
    elif request.headers.get("content-type", "").startswith("application/apply-patch"):
        verb = "apply"
    else:
        verb = "patch"
    return verb, group, resource


_ROUTERS = kubernetes_objects.routers


def placed_routers(
    rig: Rig, fleet: kubernetes_objects.Fleet, settings: kubernetes_objects.Routers
) -> list[dict[str, Any]]:
    """The controller's router pair, on the routers' node or, with `rig.router_selector`, on any
    node the scheduler finds with those labels."""
    documents = _ROUTERS(fleet, settings)
    for document in documents:
        if document["kind"] == "StatefulSet":
            spec = document["spec"]["template"]["spec"]
            if rig.router_selector:
                spec["nodeSelector"] = dict(rig.router_selector)
            else:
                spec["nodeName"] = "cpu-a"
    return documents


def _write_records(self: kubernetes_target.KubernetesDeployment, context, observations) -> None:
    config = context.directory / "config"
    config.mkdir(mode=0o700)
    package = context.directory / "package"
    # One folder for each node, holding each of its engines' files, as `package` writes them.
    for host in self.hosts():
        folder = package / host.id
        folder.mkdir(mode=0o700, parents=True)
        for role in host.roles:
            write_environment(folder / f".env.{role}", {"NARWHAL_ROLE": role})
            write_private(folder / f"engine-launch.{role}.json", b'{"runtime": {}}')
        for helper in helpers(launcher(None)):
            write_private(folder / helper.name, helper.source.read_bytes())
    fleet = {
        "model": "served-model",
        "engines": [],
        "profiles": {"path": "p"},
        "engine": {"engine_api_key_env": "NARWHAL_ENGINE_KEY"},
    }
    write_private(package / "fleet.json", json.dumps(fleet).encode())
    write_private(package / "profiling-limits.json", b"{}")
    write_private(config / "engine-launch.json", b'{"engines": {}}')
    write_environment(config / "deployment.env", {})


def instance_of(entry: Mapping[str, Any]) -> str:
    """The stage instance a gate Job ran for, from its keep directory, or "" for a stage that
    runs once."""
    parts = entry["keep"].split("/")
    if "stages" not in parts:
        return ""
    found = parts[parts.index("stages") + 2]
    return "" if found.startswith("attempt-") else found


def _role(rig: Rig, entry: Mapping[str, Any]) -> str:
    found = instance_of(entry)
    return found if found in NODES else next(iter(NODES))


class _Registration:
    """A launcher that registers the layout its live cache resolved."""

    def cache_layout(self, source: Path) -> str:
        return str(json.loads(Path(source).read_text())["ranks"][0]["kv_cache_layout"])


class _Plan:
    def __init__(self, hosts: list[Host], groups: list[dict], launch: Path) -> None:
        self.hosts = hosts
        self.groups = [_Group(tuple(group["roles"])) for group in groups]
        self.engines = {role: _Engine(launch) for host in hosts for role in host.roles}
        self.layouts: dict[str, bytes] = {}
        self.transfers: dict[str, dict] = {}
        self.workload = None

    def group(self, role: str) -> _Group:
        return next(group for group in self.groups if role in group.roles)

    def edges(self) -> tuple[_Edge, ...]:
        return tuple(_Edge(a, b) for a in self.hosts for b in self.hosts if a != b)


@dataclass(frozen=True)
class _Engine:
    launch: Path


@dataclass(frozen=True)
class _Group:
    roles: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.roles[0]


@dataclass(frozen=True)
class _Edge:
    source: Host
    destination: Host

    @property
    def name(self) -> str:
        return f"{self.source.id}-to-{self.destination.id}"


@dataclass(frozen=True)
class _Sample:
    name: str


@dataclass(frozen=True)
class _Observation:
    sample: _Sample
    fingerprint: str


@dataclass(frozen=True)
class _Document:
    launch: dict
    contract: Any


class _Contract:
    def fields(self) -> dict:
        return {"engine": "contract"}


def _document(path: Path) -> _Document:
    return _Document(json.loads(Path(path).read_text())["launch"], _Contract())


def unpack(config_map: Mapping[str, Any]) -> dict[str, bytes]:
    """The files of a gate's inputs ConfigMap."""
    data = base64.b64decode(config_map["binaryData"][kubernetes_gate.INPUTS])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }
