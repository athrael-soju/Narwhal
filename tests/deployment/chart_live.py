"""The narwhal chart's RBAC and admission policy, as an API server applies them.

Unit tests read the rendered chart but cannot ask an API server what a Role grants or run the
admission policy's CEL. `check(kubectl, namespace, release)` does both on a cluster where the
chart is installed, and returns every case the server decided otherwise:

- `CONTROLLER_RULES` and `cluster_rules()` are what the controller's Role and ClusterRole grant,
  and `FORBIDDEN` a sample of what they must not, checked with `kubectl auth can-i`.
- `admitted()` are the objects the controller's builders make, sent as the controller, and a
  pod of each, sent as the Kubernetes controller that makes it. The policy must admit them.
- `refused()` are objects the policy must refuse, with the words its refusal names. `forged()`
  are pods and workloads a user with the namespace's `edit` role sends with the labels of the
  controller's objects; the policy must refuse them too, and `relabel` checks that the user
  cannot give an existing pod those labels. `workstation_refused()` are Jobs the user
  `WORKSTATION` sends, who holds the workstation's role and may also create Jobs: none may run
  as the controller's ServiceAccount or mount the state claim. `check_ephemeral` adds an
  ephemeral container to the controller's pod and to a gate Job's pod, which the policy must
  refuse, and to a plain pod, which it must not.
- `volume_cases(policy)` pairs each of `MODEL_VOLUMES` with whether
  `fleet_resource.check_model_volume` allows it under that policy, which the admission policy
  must match.

Each object is sent with `kubectl create --dry-run=server`, which runs admission and stores
nothing. `check` binds `edit` to the user `FORGER`, the fleet-editor Role and
`WORKSTATION_RULES` with Job creation to `WORKSTATION`, and creates the RuntimeClass `UNLISTED`
for its cases, and removes them when it ends.

`check_lifecycle` uninstalls an installed release and installs, upgrades, uninstalls and
installs it again with the admission policy present, and checks that every workload the chart
applies makes its pods each time.

`python -m tests.deployment.chart_live --kubeconfig PATH --namespace NS --release NAME [--policy
JSON] [--lifecycle CHART VALUES --upgrade-set KEY=VALUE]` runs the checks against an installed
release whose model volume values are `--policy`, after the lifecycle when it is given.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from narwhal.deployment import fleet_resource, kubernetes_cli, kubernetes_export
from narwhal.deployment import kubernetes_objects as objects

# (API group, resource, verb, resource name or ""), as the controller's Role grants them in
# its namespace, and its ClusterRole cluster-wide.
FLEETS = (fleet_resource.GROUP, fleet_resource.PLURAL)
CONTROLLER_RULES: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        *((*FLEETS, verb, "") for verb in ("get", "list", "watch")),
        (fleet_resource.GROUP, f"{fleet_resource.PLURAL}/status", "patch", ""),
        *(("batch", "jobs", verb, "") for verb in ("get", "list", "watch", "create", "delete")),
        *(("", "pods", verb, "") for verb in ("get", "list", "watch", "patch", "delete")),
        ("", "pods/log", "get", ""),
        *(
            (group, resource, verb, "")
            for group, resource in (
                ("", "configmaps"),
                ("", "services"),
                ("apps", "statefulsets"),
                ("apps", "deployments"),
            )
            for verb in ("get", "list", "watch", "create", "patch", "delete")
        ),
        ("coordination.k8s.io", "leases", "create", ""),
        *(
            ("coordination.k8s.io", "leases", verb, fleet_resource.CONTROLLER_LEASE)
            for verb in ("get", "update")
        ),
        *(("events.k8s.io", "events", verb, "") for verb in ("create", "patch")),
    }
)


def cluster_rules(namespace: str) -> frozenset[tuple[str, str, str, str]]:
    """What the controller's ClusterRole grants for a release in `namespace`."""
    policy = f"{namespace}-narwhal"
    return frozenset(
        {
            ("", "nodes", "get", ""),
            ("", "nodes", "list", ""),
            ("apiextensions.k8s.io", "customresourcedefinitions", "get", fleet_resource.CRD_NAME),
            ("admissionregistration.k8s.io", "validatingadmissionpolicies", "get", policy),
            ("admissionregistration.k8s.io", "validatingadmissionpolicybindings", "get", policy),
        }
    )


# What the controller must not do, in its namespace unless the entry says cluster-wide.
FORBIDDEN: tuple[tuple[str, str, str, str], ...] = (
    ("", "secrets", "get", ""),
    ("", "secrets", "list", ""),
    ("", "secrets", "watch", ""),
    ("", "pods/exec", "create", ""),
    ("", "pods/attach", "create", ""),
    ("", "pods/portforward", "create", ""),
    ("", "pods", "create", ""),
    ("", "pods/ephemeralcontainers", "patch", ""),
    ("", "serviceaccounts/token", "create", ""),
    ("", "serviceaccounts", "create", ""),
    ("", "persistentvolumeclaims", "delete", ""),
    ("rbac.authorization.k8s.io", "roles", "create", ""),
    ("rbac.authorization.k8s.io", "rolebindings", "create", ""),
    ("rbac.authorization.k8s.io", "roles", "bind", ""),
    ("rbac.authorization.k8s.io", "clusterroles", "escalate", ""),
    (*FLEETS, "update", ""),
    (*FLEETS, "patch", ""),
    (*FLEETS, "create", ""),
    (*FLEETS, "delete", ""),
    (fleet_resource.GROUP, f"{fleet_resource.PLURAL}/status", "update", ""),
    ("coordination.k8s.io", "leases", "get", "another-lease"),
    ("coordination.k8s.io", "leases", "delete", fleet_resource.CONTROLLER_LEASE),
    ("admissionregistration.k8s.io", "validatingadmissionpolicies", "update", ""),
    ("apiextensions.k8s.io", "customresourcedefinitions", "update", fleet_resource.CRD_NAME),
)
CLUSTER_FORBIDDEN: tuple[tuple[str, str, str, str], ...] = (
    ("", "nodes", "patch", ""),
    ("", "nodes", "watch", ""),
    ("", "namespaces", "list", ""),
    ("", "secrets", "list", ""),
    ("apiextensions.k8s.io", "customresourcedefinitions", "list", ""),
    ("apiextensions.k8s.io", "customresourcedefinitions", "get", "other.example.com"),
    ("admissionregistration.k8s.io", "validatingadmissionpolicies", "list", ""),
    ("admissionregistration.k8s.io", "validatingadmissionpolicies", "get", "other-narwhal"),
    ("admissionregistration.k8s.io", "validatingadmissionpolicybindings", "delete", ""),
)

NARWHAL = "registry.example/narwhal@sha256:" + "a" * 64
OWNER = objects.Owner("fleet", "0b6f4c1e-5d1a-4c55-9f7e-3a1f0f5e9a11", "example")
CLAIM = {"persistentVolumeClaim": {"claimName": "models", "readOnly": True}}
RUNTIME_DEFAULT = {"seccompProfile": {"type": "RuntimeDefault"}}
# A user with the namespace's edit role, and a RuntimeClass the chart's values do not list.
FORGER = "chart-live-forger"
# A workstation's identity that may also create Jobs, as the review of task 26 found it could.
WORKSTATION = "chart-live-workstation"
# What an engine pod's checkpoint step adds, to hand the evidence directory to the sidecar.
CHECKPOINT_CAPABILITIES = ("CHOWN", "FOWNER")
# The router pods' component label, which the routers' NetworkPolicy admits.
ROUTER = "router"
UNLISTED = "chart-live-unlisted"
# The Kubernetes controller that makes a pod or ReplicaSet for each owner kind.
MAKERS = {
    "StatefulSet": "statefulset-controller",
    "Job": "job-controller",
    "ReplicaSet": "replicaset-controller",
    "Deployment": "deployment-controller",
}


@dataclass(frozen=True)
class Case:
    """An object, who sends it (the cluster admin when None), and for a refusal the words the
    policy's message names."""

    document: dict
    words: str = ""
    user: str | None = None


def controller(namespace: str, release: str) -> str:
    return f"system:serviceaccount:{namespace}:{release}-controller"


def made_by(document: Mapping[str, Any], owner: str, name: str = "owner") -> Case:
    """`document` with a controlling owner of kind `owner`, sent by the controller that makes
    objects for that kind."""
    made = copy.deepcopy(dict(document))
    api = {"Job": "batch/v1"}.get(owner, "apps/v1")
    made["metadata"]["ownerReferences"] = [
        {
            "apiVersion": api,
            "kind": owner,
            "name": name,
            "uid": "5c1f9a2e-7b3d-4e8f-9a0b-1c2d3e4f5a6b",
            "controller": True,
        }
    ]
    return Case(made, user=f"system:serviceaccount:kube-system:{MAKERS[owner]}")


def _pod(name: str, component: str | None, spec: Mapping[str, Any], **labels: str) -> dict:
    found = dict(labels)
    if component is not None:
        found.update(objects.labels(component))
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": found},
        "spec": copy.deepcopy(dict(spec)),
    }


def _container(**fields: Any) -> dict:
    return {"name": "main", "image": NARWHAL, "command": ["true"], **fields}


def _plain(**fields: Any) -> dict:
    """A pod spec the policy admits, changed by `fields`."""
    return {
        "restartPolicy": "Never",
        "securityContext": copy.deepcopy(RUNTIME_DEFAULT),
        "containers": [_container()],
        **fields,
    }


def pod_of(document: Mapping[str, Any], name: str) -> dict:
    """The pod a Job, StatefulSet or ReplicaSet makes from its template."""
    template = document["spec"]["template"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": dict(template["metadata"]["labels"])},
        "spec": copy.deepcopy(template["spec"]),
    }


def replica_set(deployment: Mapping[str, Any]) -> dict:
    """The ReplicaSet the Deployment controller makes for `deployment`."""
    spec = deployment["spec"]
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {"name": f"{deployment['metadata']['name']}-rs"},
        "spec": {
            "replicas": spec.get("replicas", 1),
            "selector": copy.deepcopy(spec["selector"]),
            "template": copy.deepcopy(spec["template"]),
        },
    }


def _in(namespace: str, document: Mapping[str, Any]) -> dict:
    found = copy.deepcopy(dict(document))
    found["metadata"]["namespace"] = namespace
    return found


def _gate(kind: str, **fields: Any) -> objects.GateJob:
    settings: dict[str, Any] = {
        "name": f"{kind}-job",
        "kind": kind,
        "command": ["true"],
        "deadline_seconds": 60,
        "keep": f"deploy/run/jobs/{kind}",
    }
    if kind in objects.NODE_KINDS:
        settings["node"] = "node-a"
    if kind in objects.HOST_NETWORK_KINDS:
        settings["host_network"] = True
    settings.update(fields)
    return objects.GateJob(**settings)


def _without_key(values: dict) -> dict:
    """Engine values whose pods read no API key, for a fleet without one."""
    template = values["podTemplate"]
    for container in [*template["initContainers"], *template["containers"]]:
        container["env"] = [
            item
            for item in container.get("env", [])
            if "secretKeyRef" not in (item.get("valueFrom") or {})
        ]
    return values


def _engine_pod(release: str, change: Callable[[dict], object] = lambda spec: None) -> dict:
    """A pod labelled as an engine on the host network, changed by `change`."""
    pod = _pod("engine-x", objects.ENGINE, _plain(hostNetwork=True))
    del pod["spec"]["securityContext"]
    pod["spec"]["serviceAccountName"] = f"{release}-engine"
    change(pod["spec"])
    return pod


def _fleet(state_claim: str = "narwhal-state", account: str = "narwhal-engine") -> objects.Fleet:
    """The fleet whose objects the cases build."""
    return objects.Fleet(
        owner=OWNER,
        narwhal_image=NARWHAL,
        state_claim=state_claim,
        haproxy_image="registry.example/haproxy@sha256:" + "b" * 64,
        fabric_image="registry.example/perftest@sha256:" + "c" * 64,
        engine_account=account,
        rdma_resource="rdma.example.com/hca",
        model_volume=CLAIM,
    )


def admitted(
    namespace: str,
    release: str,
    engines: Mapping[str, Any] | None = None,
    state_claim: str = "narwhal-state",
) -> dict[str, Case]:
    """The objects the controller's builders make for a fleet in `release`, sent as the
    controller, and a pod of each workload, sent as the controller of its owner: every gate
    kind, HAProxy and, with `engines` values, the engine StatefulSet."""
    account = f"{release}-engine"
    fleet = _fleet(state_claim, account)
    workloads = [
        objects.job(fleet, _gate(kind, service_account=kind == objects.ACCOUNT_KIND))
        for kind in objects.GATE_KINDS
    ]
    # A Narwhal gate mounts its own directories of the state volume, each by its subPath.
    workloads.append(objects.job(fleet, _gate("preflight", name="preflight-dirs", state=WORK)))
    workloads += [d for d in objects.haproxy(fleet) if d["kind"] == "Deployment"]
    if engines is not None:
        values = copy.deepcopy(dict(engines))
        values["podTemplate"]["serviceAccountName"] = account
        values["sidecarImage"] = NARWHAL
        values["stateClaim"] = state_claim
        engine_fleet = objects.Fleet(
            owner=OWNER,
            narwhal_image=NARWHAL,
            state_claim=state_claim,
            engine_account=account,
            rdma_resource=values["rdmaResource"],
            model_volume=values["modelVolume"],
        )
        workloads += [
            d
            for d in objects.engines(engine_fleet, _without_key(values))
            if d["kind"] == "StatefulSet"
        ]
    found: dict[str, Case] = {}
    sender = controller(namespace, release)
    for document in workloads:
        kind, name = document["kind"], document["metadata"]["name"]
        key = f"{kind.lower()}/{name}"
        found[key] = Case(document, user=sender)
        made = document
        if kind == "Deployment":
            made = replica_set(document)
            found[f"replicaset of {key}"] = made_by(made, "Deployment", name)
            kind, name = "ReplicaSet", made["metadata"]["name"]
        found[f"pod of {key}"] = made_by(pod_of(made, f"{name}-pod"), kind, name)
    if engines is not None:
        [statefulset] = [c.document for c in found.values() if c.document["kind"] == "StatefulSet"]
        found["an engine pod with a non-root sidecar"] = made_by(
            hardened(pod_of(statefulset, "engine-2")), "StatefulSet", "engine"
        )
    # The router pair, as the controller applies it, reading its fleet file's directory.
    router = _workload(
        "StatefulSet",
        "chart-live-router",
        objects.labels(ROUTER),
        _plain(
            volumes=[{"name": "state", "persistentVolumeClaim": {"claimName": state_claim}}],
            containers=[_container(volumeMounts=[_state_mount(LIMITS, read_only=True)])],
        ),
    )
    found["a router StatefulSet"] = Case(router, user=sender)
    found["a router StatefulSet's pod"] = made_by(
        pod_of(router, "chart-live-router-0"), "StatefulSet", "chart-live-router"
    )
    # An export Job, as the controller makes it from its own image, and its pod.
    export = _export(namespace, claim=state_claim)
    found["an export Job"] = Case(export, user=sender)
    found["an export Job's pod"] = made_by(pod_of(export, "export-pod"), "Job", "export")
    # The controller's own Deployment makes ReplicaSets and pods that run as its ServiceAccount
    # and mount the state claim; helm applies the Deployment, which the lifecycle check makes.
    own = _workload(
        "Deployment",
        f"{release}-controller",
        objects.labels("controller"),
        _plain(
            serviceAccountName=f"{release}-controller",
            volumes=[{"name": "state", "persistentVolumeClaim": {"claimName": state_claim}}],
        ),
    )
    own_set = replica_set(own)
    own_set["metadata"]["name"] = f"{release}-controller-chartlive"
    found["the controller Deployment's ReplicaSet"] = made_by(
        own_set, "Deployment", f"{release}-controller"
    )
    found["the controller Deployment's pod"] = made_by(
        pod_of(own_set, f"{release}-controller-chartlive-pod"),
        "ReplicaSet",
        f"{release}-controller-chartlive",
    )
    # A ROCm engine runs without a seccomp filter, as Docker's ROCm launch does.
    found["an unconfined engine pod with IPC_LOCK"] = made_by(
        _engine_pod(
            release,
            lambda spec: spec.update(
                securityContext={"seccompProfile": {"type": "Unconfined"}},
                containers=[_container(securityContext={"capabilities": {"add": ["IPC_LOCK"]}})],
            ),
        ),
        "StatefulSet",
        "engine",
    )
    return found


def hardened(pod: Mapping[str, Any], sidecar_user: int = 10002) -> dict:
    """An engine pod whose attestation sidecar runs as `sidecar_user`, whose engine steps keep
    only IPC_LOCK, and whose checkpoint step keeps only CHOWN and FOWNER, to hand the pod's
    evidence directory to the sidecar."""
    found = copy.deepcopy(dict(pod))
    spec = found["spec"]
    for container in [*spec["initContainers"], *spec["containers"]]:
        if container["name"] == "attestation":
            container["securityContext"] = {
                "runAsUser": sidecar_user,
                "runAsGroup": sidecar_user,
                "runAsNonRoot": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                **RUNTIME_DEFAULT,
            }
        elif container["name"] == "checkpoint":
            container["securityContext"] = {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"], "add": list(CHECKPOINT_CAPABILITIES)},
                **RUNTIME_DEFAULT,
            }
        else:
            container["securityContext"] = {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"], "add": ["IPC_LOCK"]},
                **RUNTIME_DEFAULT,
            }
    return found


def _set_container(**fields: Any) -> Callable[[dict], object]:
    return lambda spec: spec["containers"][0].update(fields)


def _model(source: Mapping[str, Any], read_only: bool = True) -> dict:
    spec = _plain(volumes=[{"name": objects.MODEL_VOLUME, **copy.deepcopy(dict(source))}])
    spec["containers"][0]["volumeMounts"] = [
        {"name": objects.MODEL_VOLUME, "mountPath": "/model", "readOnly": read_only}
    ]
    return spec


def _workload(kind: str, name: str, labels: Mapping[str, str], spec: Mapping[str, Any]) -> dict:
    """A Job, StatefulSet or Deployment of one pod template."""
    api = "batch/v1" if kind == "Job" else "apps/v1"
    template_spec = copy.deepcopy(dict(spec))
    if kind != "Job":
        template_spec["restartPolicy"] = "Always"
    body: dict[str, Any] = {
        "template": {"metadata": {"labels": dict(labels)}, "spec": template_spec}
    }
    if kind != "Job":
        body["selector"] = {"matchLabels": dict(labels)}
    if kind == "StatefulSet":
        body["serviceName"] = "x"
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name}, "spec": body}


HOST = "use the host network"
CAPABILITY = "add a capability"
SUBPATH = "only the controller's own pods mount the state claim's root"
JOB_POD = "only the Job controller makes a pod"
EPHEMERAL = "no one adds an ephemeral container"
# An attempt's work directory and the fleet-limits directory a preflight gate reads.
WORK_DIR = "deploy/run/stages/preflight/attempt-1"
LIMITS = "deploy/run/stages/fleet-limits/attempt-1"
WORK = {WORK_DIR: True, LIMITS: False}


def _state_mount(path: str | None, *, read_only: bool = False) -> dict:
    """A mount of the state claim's volume `state`: by subPath `path`, or its root."""
    mount: dict[str, Any] = {"name": "state", "mountPath": f"/narwhal-state/{path or ''}"}
    if path is not None:
        mount["subPath"] = path
    if read_only:
        mount["readOnly"] = True
    return mount


def _export(
    namespace: str,
    change: Callable[[list[dict]], object] = lambda mounts: None,
    claim: str = "narwhal-state",
) -> dict:
    """An export Job as the controller makes it, its mounts changed by `change`."""
    request = kubernetes_export.Request("0" * 32, {}, "")
    export = kubernetes_export.job(
        OWNER,
        request,
        ["stages/access/attempt-1"],
        image=NARWHAL,
        user=10001,
        group=10001,
        claim=claim,
        runs=f"runs/{namespace}/{OWNER.uid}",
    )
    change(export["spec"]["template"]["spec"]["containers"][0]["volumeMounts"])
    return export


def refused(release: str, namespace: str = "example") -> dict[str, Case]:
    """Objects the policy must refuse whoever sends them, and Jobs it must refuse even from the
    controller of `release` in `namespace`."""
    gate = objects.labels(objects.GATE, gate="preflight")
    fabric = objects.labels(objects.GATE, gate="fabric-server")

    def engine(change: Callable[[dict], object], words: str) -> Case:
        # Sent as the StatefulSet controller, so the pod passes as an engine but for `change`.
        made = made_by(_engine_pod(release, change), "StatefulSet", "engine")
        return Case(made.document, words, made.user)

    def pod(spec: Mapping[str, Any], words: str, **labels: str) -> Case:
        return Case(_pod("plain", None, spec, **labels), words)

    def context(words: str, **fields: Any) -> Case:
        return pod(_plain(securityContext={**RUNTIME_DEFAULT, **fields}), words)

    def container(words: str, **fields: Any) -> Case:
        return pod(_plain(containers=[_container(**fields)]), words)

    fabric_pod = made_by(
        _pod(
            "fabric",
            None,
            _plain(
                hostNetwork=True,
                containers=[_container(securityContext={"capabilities": {"add": ["NET_RAW"]}})],
            ),
            **fabric,
        ),
        "Job",
    )
    cases: dict[str, Case] = {
        "host network, unlabelled": pod(_plain(hostNetwork=True), HOST),
        "host network, router": Case(_pod("router", "router", _plain(hostNetwork=True)), HOST),
        "host network, a preflight gate's pod": made_by(
            _pod("gate", None, _plain(hostNetwork=True), **gate), "Job"
        ),
        "host network, another app's engine": made_by(
            _pod(
                "other",
                None,
                _plain(hostNetwork=True),
                **{objects.NAME_LABEL: "other", objects.COMPONENT_LABEL: "engine"},
            ),
            "StatefulSet",
        ),
        "host network, an engine a ReplicaSet makes": made_by(_engine_pod(release), "ReplicaSet"),
        "IPC_LOCK, a preflight gate's pod": made_by(
            _pod(
                "gate",
                None,
                _plain(
                    containers=[_container(securityContext={"capabilities": {"add": ["IPC_LOCK"]}})]
                ),
                **gate,
            ),
            "Job",
        ),
        "SYS_ADMIN, engine": engine(
            _set_container(securityContext={"capabilities": {"add": ["IPC_LOCK", "SYS_ADMIN"]}}),
            CAPABILITY,
        ),
        "NET_RAW, fabric gate": Case(fabric_pod.document, CAPABILITY, fabric_pod.user),
        "SYS_ADMIN, an engine's checkpoint step": engine(
            lambda s: s.update(
                initContainers=[
                    _container(
                        name="checkpoint",
                        securityContext={"capabilities": {"add": ["CHOWN", "SYS_ADMIN"]}},
                    )
                ]
            ),
            CAPABILITY,
        ),
        "CHOWN, an engine's serving container": engine(
            _set_container(securityContext={"capabilities": {"add": ["CHOWN"]}}), CAPABILITY
        ),
        "CHOWN, a regular container named checkpoint": engine(
            lambda s: s["containers"].append(
                _container(name="checkpoint", securityContext={"capabilities": {"add": ["CHOWN"]}})
            ),
            CAPABILITY,
        ),
        "CHOWN, a checkpoint step outside an engine pod": pod(
            _plain(
                initContainers=[
                    _container(
                        name="checkpoint", securityContext={"capabilities": {"add": ["CHOWN"]}}
                    )
                ]
            ),
            CAPABILITY,
        ),
        "privileged engine": engine(
            _set_container(securityContext={"privileged": True}), "privileged"
        ),
        "privileged init container": pod(
            _plain(initContainers=[_container(name="init", securityContext={"privileged": True})]),
            "privileged",
        ),
        "host PID, engine": engine(lambda s: s.update(hostPID=True), "process"),
        "host IPC": pod(_plain(hostIPC=True), "process or IPC"),
        "unconfined seccomp, pod": context("seccomp", seccompProfile={"type": "Unconfined"}),
        "unconfined seccomp, container": container(
            "seccomp", securityContext={"seccompProfile": {"type": "Unconfined"}}
        ),
        "no seccomp profile": pod(
            {k: v for k, v in _plain().items() if k != "securityContext"}, "seccomp"
        ),
        "a host port": container("host port", ports=[{"containerPort": 8080, "hostPort": 8080}]),
        "an SELinux type": context("SELinux", seLinuxOptions={"type": "spc_t"}),
        "an SELinux user": container(
            "SELinux", securityContext={"seLinuxOptions": {"user": "system_u"}}
        ),
        "an unconfined AppArmor profile": container(
            "AppArmor", securityContext={"appArmorProfile": {"type": "Unconfined"}}
        ),
        "an unmasked /proc": pod(
            _plain(
                hostUsers=False,
                containers=[_container(securityContext={"procMount": "Unmasked"})],
            ),
            "/proc",
        ),
        "an unsafe sysctl": context("sysctls", sysctls=[{"name": "kernel.msgmax", "value": "1"}]),
        "a RuntimeClass the values leave out": pod(
            _plain(runtimeClassName=UNLISTED), "RuntimeClass"
        ),
        "image by tag": container("registry digest", image="busybox:1.37"),
        "host path on an engine": engine(
            lambda s: s.update(
                volumes=[{"name": "data", "hostPath": {"path": "/srv/models", "type": "Directory"}}]
            ),
            "volume data",
        ),
        "host path on a pod": pod(
            _plain(volumes=[{"name": "root", "hostPath": {"path": "/"}}]), "volume root"
        ),
        "an NFS volume besides the model": pod(
            _plain(volumes=[{"name": "data", "nfs": {"server": "nfs.example.com", "path": "/"}}]),
            "volume data",
        ),
        "a writable model volume": Case(_pod("model", None, _model(CLAIM, False)), "read-only"),
    }
    # The controller's own Jobs mount only directories of the state claim, by subPath.
    root = objects.job(_fleet(), _gate("state", name="state-root"))
    root["spec"]["template"]["spec"]["containers"][0]["volumeMounts"].append(
        {"name": objects.STATE_VOLUME, "mountPath": objects.STATE_MOUNT}
    )
    runs = f"runs/{namespace}/{OWNER.uid}"
    sender = controller(namespace, release)
    cases["a gate Job that mounts the state claim's root"] = Case(root, SUBPATH, sender)
    cases["an export Job that mounts a run's salt"] = Case(
        _export(
            namespace,
            lambda mounts: mounts.append(_state_mount(f"{runs}/run.salt", read_only=True)),
        ),
        SUBPATH,
        sender,
    )
    cases["an export Job that mounts a run's credential HMACs"] = Case(
        _export(
            namespace,
            lambda mounts: mounts.append(_state_mount(f"{runs}/credential.json", read_only=True)),
        ),
        SUBPATH,
        sender,
    )
    cases["an export Job that writes the run record"] = Case(
        _export(namespace, lambda mounts: mounts[0].pop("readOnly")), SUBPATH, sender
    )
    annotated = _pod("apparmor", None, _plain())
    annotated["metadata"]["annotations"] = {
        "container.apparmor.security.beta.kubernetes.io/main": "unconfined"
    }
    cases["an unconfined AppArmor annotation"] = Case(annotated, "AppArmor")
    labels = objects.labels(objects.ROUTER_SERVICE)
    for kind in ("Job", "StatefulSet", "Deployment"):
        cases[f"a {kind} on the host network"] = Case(
            _workload(kind, "host", labels, _plain(hostNetwork=True)), HOST
        )
    return cases


def forged(release: str) -> dict[str, Case]:
    """What a user with the namespace's edit role sends with the labels of the controller's
    objects. The policy refuses each."""
    fabric = objects.labels(objects.GATE, gate="fabric-server")
    engine = objects.labels(objects.ENGINE)
    reserved = "only the fleet controller's workloads carry"
    engine_pod = _engine_pod(release)
    owned = made_by(engine_pod, "StatefulSet", "engine").document
    cases = {
        "an engine pod on the host network": Case(engine_pod, HOST),
        "an engine pod naming the engine StatefulSet as its owner": Case(owned, HOST),
        "a fabric gate's pod on the host network": Case(
            _pod("fabric", None, _plain(hostNetwork=True), **fabric), HOST
        ),
        "a fabric gate Job on the host network": Case(
            _workload("Job", "fabric", fabric, _plain(hostNetwork=True)), HOST
        ),
        "an engine StatefulSet on the host network": Case(
            _workload("StatefulSet", "engine-x", engine, _plain(hostNetwork=True)), HOST
        ),
        "a pod with the gate label": Case(_pod("gate", objects.GATE, _plain()), reserved),
        "a checkpoint step with CHOWN and FOWNER": Case(
            _pod(
                "checkpoint",
                None,
                _plain(
                    initContainers=[
                        _container(
                            name="checkpoint",
                            securityContext={
                                "capabilities": {"add": list(CHECKPOINT_CAPABILITIES)}
                            },
                        )
                    ]
                ),
            ),
            CAPABILITY,
        ),
        "a pod with the router label": Case(_pod("router-x", ROUTER, _plain()), reserved),
        "a router StatefulSet": Case(
            _workload("StatefulSet", "router-x", objects.labels(ROUTER), _plain()),
            reserved,
        ),
        "a Deployment with the HAProxy label": Case(
            _workload("Deployment", "proxy", objects.labels(objects.HAPROXY), _plain()), reserved
        ),
        # A pod that would pass for an export Job's pod, whose log the workstation reads.
        "a pod naming an export Job as its controller": Case(
            made_by(_pod("export-x", None, _plain()), "Job", "export-x").document, JOB_POD
        ),
        "a pod with the Job controller's job-name labels": Case(
            _pod(
                "export-y",
                None,
                _plain(),
                **{"job-name": "export-x", "batch.kubernetes.io/job-name": "export-x"},
            ),
            JOB_POD,
        ),
        # The controller's Deployment is the controller's only from its installer, so the
        # forger can make none of its name, as after `helm uninstall`, nor change its template.
        "a Deployment of the controller's name as its ServiceAccount": Case(
            _workload(
                "Deployment",
                f"{release}-controller",
                objects.labels("controller"),
                _plain(serviceAccountName=f"{release}-controller"),
            ),
            ACCOUNT,
        ),
        "a Deployment of the controller's name that mounts the state claim": Case(
            _workload(
                "Deployment",
                f"{release}-controller",
                objects.labels("controller"),
                _plain(
                    volumes=[
                        {"name": "state", "persistentVolumeClaim": {"claimName": "narwhal-state"}}
                    ],
                    containers=[_container(volumeMounts=[_state_mount(None)])],
                ),
            ),
            STATE,
        ),
    }
    return {name: Case(case.document, case.words, FORGER) for name, case in cases.items()}


ACCOUNT = "run as the controller's ServiceAccount"
STATE = "mount the state claim"


def workstation_refused(release: str, state_claim: str = "narwhal-state") -> dict[str, Case]:
    """Jobs and pods that would hand their sender the controller's token or the state claim's
    secrets, sent by a workstation that may create Jobs or a user with `edit`. The policy
    refuses each."""
    account = _plain(serviceAccountName=f"{release}-controller")
    state = {"name": "state", "persistentVolumeClaim": {"claimName": state_claim}}
    mounted = _plain(
        volumes=[state],
        containers=[_container(volumeMounts=[{"name": "state", "mountPath": "/s"}])],
    )
    at_root = _plain(
        volumes=[
            {**state, "persistentVolumeClaim": {**state["persistentVolumeClaim"], "readOnly": True}}
        ],
        containers=[
            _container(volumeMounts=[{"name": "state", "mountPath": "/s", "readOnly": True}])
        ],
    )
    plain: dict[str, str] = {}
    cases = {
        "a Job as the controller's ServiceAccount": Case(
            _workload("Job", "as-controller", plain, account), ACCOUNT, WORKSTATION
        ),
        "a Job that mounts the state claim": Case(
            _workload("Job", "state", plain, mounted), STATE, WORKSTATION
        ),
        "a Job that mounts the state claim's root read-only": Case(
            _workload("Job", "state-ro", plain, at_root), STATE, WORKSTATION
        ),
        "a pod as the controller's ServiceAccount": Case(
            _pod("as-controller", None, account), ACCOUNT, FORGER
        ),
        "a pod that mounts the state claim": Case(_pod("state", None, mounted), STATE, FORGER),
        "a Deployment of another name as the controller's ServiceAccount": Case(
            _workload("Deployment", "as-controller", {"app": "x"}, account), ACCOUNT, FORGER
        ),
        "a ReplicaSet naming the controller's Deployment as its owner": Case(
            made_by(
                replica_set(_workload("Deployment", "x", {"app": "x"}, account)),
                "Deployment",
                f"{release}-controller",
            ).document,
            ACCOUNT,
            FORGER,
        ),
    }
    return cases


# Model volume sources, against which the admission policy and `check_model_volume` agree.
MODEL_VOLUMES: tuple[dict, ...] = (
    CLAIM,
    *(
        {"hostPath": {"path": path, "type": "Directory"}}
        for path in (
            "/srv/models",
            "/srv/models/llama",
            "/srv/models2",
            "/srv",
            "/data/x",
            "/srv/models/../../etc",
            "/srv//models",
            "/srv/models/",
            "/etc/models",
            "/var/lib/kubelet/pods",
            "/proc",
            "/",
        )
    ),
    {"nfs": {"server": "nfs.example.com", "path": "/exports/models", "readOnly": True}},
    {"nfs": {"server": "127.0.0.1", "path": "/etc", "readOnly": True}},
    {"csi": {"driver": "csi.example.com", "readOnly": True}},
    {"csi": {"driver": "hostpath.csi.k8s.io", "readOnly": True}},
)
# The chart's model volume values: its defaults, every source with lists, and hostPath without
# a prefix.
POLICIES: tuple[dict[str, list[str]], ...] = (
    {"modelVolumeSources": ["persistentVolumeClaim"]},
    {
        "modelVolumeSources": ["persistentVolumeClaim", "hostPath", "nfs", "csi"],
        "modelHostPathPrefixes": ["/srv/models", "/data"],
        "modelNfsServers": ["nfs.example.com"],
        "modelCsiDrivers": ["csi.example.com"],
    },
    {"modelVolumeSources": ["hostPath"]},
)


def allows(policy: Mapping[str, Sequence[str]], source: Mapping[str, Any]) -> bool:
    """Whether `check_model_volume` lets a fleet use `source` under the chart's `policy`."""
    fleet = {"spec": {"engines": {"modelVolume": source}}}
    try:
        fleet_resource.check_model_volume(fleet, fleet_resource.VolumePolicy.from_values(policy))
    except ValueError:
        return False
    return True


def volume_cases(policy: Mapping[str, Sequence[str]]) -> dict[str, tuple[dict, bool]]:
    """A gate pod that mounts each model volume read-only, and whether the controller allows
    that volume under `policy`."""
    labels = objects.labels(objects.GATE, gate="discovery")
    return {
        json.dumps(source, sort_keys=True): (
            _pod("model", None, _model(source), **labels),
            allows(policy, source),
        )
        for source in MODEL_VOLUMES
    }


Run = Callable[[Sequence[str], str | None], subprocess.CompletedProcess[str]]


def _runner(kubectl: Sequence[str]) -> Run:
    def run(args: Sequence[str], data: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*kubectl, *args], input=data, capture_output=True, text=True, check=False
        )

    return run


def _create(run: Run, namespace: str, case: Case, dry_run: bool = True) -> tuple[bool, str]:
    user = ["--as", case.user] if case.user else []
    result = run(
        ["create", *(["--dry-run=server"] if dry_run else []), "-o", "name", *user, "-f", "-"],
        json.dumps(_in(namespace, case.document)),
    )
    return result.returncode == 0, result.stderr.strip()


def _can(run: Run, account: str, rule: tuple[str, str, str, str], namespace: str | None) -> bool:
    group, resource, verb, name = rule
    resource, _, subresource = resource.partition("/")
    target = f"{resource}.{group}" if group else resource
    if name:
        target += f"/{name}"
    where = ["--namespace", namespace] if namespace else ["--all-namespaces"]
    if subresource:
        where.append(f"--subresource={subresource}")
    result = run(["auth", "can-i", verb, target, "--as", account, *where], None)
    if result.stdout.strip() not in ("yes", "no"):
        raise RuntimeError(f"kubectl auth can-i {verb} {target}: {result.stderr.strip()}")
    return result.stdout.strip() == "yes"


def check_rbac(run: Run, namespace: str, release: str) -> list[str]:
    account = f"system:serviceaccount:{namespace}:{release}-controller"
    grants: list[tuple[tuple[str, str, str, str], str | None]] = [
        (rule, namespace) for rule in sorted(CONTROLLER_RULES)
    ]
    grants += [(rule, None) for rule in sorted(cluster_rules(namespace))]
    denials: list[tuple[tuple[str, str, str, str], str | None]] = [
        (rule, namespace) for rule in FORBIDDEN
    ]
    denials += [(rule, None) for rule in CLUSTER_FORBIDDEN]
    return [
        f"the controller cannot {rule} in {where or 'the cluster'}"
        for rule, where in grants
        if not _can(run, account, rule, where)
    ] + [
        f"the controller can {rule} in {where or 'the cluster'}"
        for rule, where in denials
        if _can(run, account, rule, where)
    ]


def check_admission(
    run: Run, namespace: str, release: str, engines: Mapping[str, Any] | None = None
) -> list[str]:
    found = []
    for name, case in admitted(namespace, release, engines).items():
        ok, error = _create(run, namespace, case)
        if not ok:
            found.append(f"refused {name}: {error}")
    cases = {
        **refused(release, namespace),
        **forged(release),
        **workstation_refused(release),
    }
    for name, case in cases.items():
        ok, error = _create(run, namespace, case)
        if ok:
            found.append(f"admitted {name}")
        elif case.words not in error:
            found.append(f"refused {name} for another reason: {error}")
    return found


def check_relabel(run: Run, namespace: str) -> list[str]:
    """A pod the forger may create cannot then take the gate label or the Job controller's
    job-name label."""
    case = Case(_pod("chart-live-relabel", None, _plain()), user=FORGER)
    ok, error = _create(run, namespace, case, dry_run=False)
    if not ok:
        return [f"refused the forger's plain pod: {error}"]
    found = []
    try:
        for labels, words in (
            ([f"{objects.NAME_LABEL}=narwhal", f"{objects.COMPONENT_LABEL}={objects.GATE}"], ""),
            (["batch.kubernetes.io/job-name=export-x"], JOB_POD),
        ):
            result = run(
                [
                    "label",
                    "pod",
                    "chart-live-relabel",
                    "--namespace",
                    namespace,
                    "--as",
                    FORGER,
                    *labels,
                ],
                None,
            )
            if result.returncode == 0:
                found.append(f"the forger gave a pod the labels {labels}")
            elif (words or "only the fleet controller's workloads carry") not in result.stderr:
                found.append(f"refused the relabel for another reason: {result.stderr.strip()}")
        return found
    finally:
        run(["delete", "pod", "chart-live-relabel", "--namespace", namespace, "--wait=false"], None)


def _add_ephemeral(run: Run, namespace: str, pod: str) -> tuple[bool, str]:
    """Add an ephemeral container to `pod` as the cluster admin, with --dry-run=server."""
    patch = {"spec": {"ephemeralContainers": [_container(name="debug")]}}
    result = run(
        [
            "patch",
            "pod",
            pod,
            "--namespace",
            namespace,
            "--subresource=ephemeralcontainers",
            "--type=strategic",
            "--dry-run=server",
            "-p",
            json.dumps(patch),
        ],
        None,
    )
    return result.returncode == 0, result.stderr.strip()


def _first_pod(run: Run, namespace: str, selector: str, timeout_s: float = 60) -> str:
    """The name of the first pod `selector` matches, once one exists, or ''."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = run(["get", "pods", "-n", namespace, "-l", selector, "-o", "name"], None)
        names = result.stdout.split()
        if names:
            return names[0].removeprefix("pod/")
        time.sleep(2)
    return ""


def check_ephemeral(run: Run, namespace: str, release: str) -> list[str]:
    """No one adds an ephemeral container to the controller's pod or to a gate Job's pod, and
    anyone may add one to a plain pod."""
    found = []
    own = _first_pod(
        run,
        namespace,
        f"app.kubernetes.io/instance={release},{objects.COMPONENT_LABEL}=controller",
    )
    if not own:
        found.append("found no controller pod to add an ephemeral container to")
    else:
        ok, error = _add_ephemeral(run, namespace, own)
        if ok:
            found.append("added an ephemeral container to the controller's pod")
        elif EPHEMERAL not in error:
            found.append(
                f"refused the controller's ephemeral container for another reason: {error}"
            )
    # A gate Job the controller makes, without the fleet as its owner, so the garbage
    # collector leaves it, on a node no cluster has, so its pod never runs.
    job = objects.job(_fleet(), _gate("state", name="chart-live-ephemeral", node="chart-live-none"))
    del job["metadata"]["ownerReferences"]
    plain = Case(_pod("chart-live-ephemeral-plain", None, _plain()))
    try:
        ok, error = _create(run, namespace, Case(job, user=controller(namespace, release)), False)
        name = _first_pod(run, namespace, "job-name=chart-live-ephemeral") if ok else ""
        if not ok:
            found.append(f"refused the gate Job for the ephemeral check: {error}")
        elif not name:
            found.append("the gate Job made no pod to add an ephemeral container to")
        else:
            ok, error = _add_ephemeral(run, namespace, name)
            if ok:
                found.append("added an ephemeral container to a gate Job's pod")
            elif EPHEMERAL not in error:
                found.append(
                    f"refused the gate pod's ephemeral container for another reason: {error}"
                )
        ok, error = _create(run, namespace, plain, dry_run=False)
        if not ok:
            found.append(f"refused the plain pod for the ephemeral check: {error}")
        else:
            ok, error = _add_ephemeral(run, namespace, "chart-live-ephemeral-plain")
            if not ok:
                found.append(f"refused an ephemeral container on a plain pod: {error}")
    finally:
        run(["delete", "job", "chart-live-ephemeral", "-n", namespace, "--ignore-not-found"], None)
        run(
            ["delete", "pod", "chart-live-ephemeral-plain", "-n", namespace, "--ignore-not-found"],
            None,
        )
    return found


def check_volumes(run: Run, namespace: str, policy: Mapping[str, Sequence[str]]) -> list[str]:
    found = []
    for name, (document, expected) in volume_cases(policy).items():
        ok, error = _create(run, namespace, made_by(document, "Job"))
        if ok != expected:
            found.append(
                f"{'admitted' if ok else 'refused'} model volume {name} under {policy}: {error}"
            )
    return found


def _binding(name: str, namespace: str, kind: str, role: str, user: str) -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": kind, "name": role},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": user}],
    }


@contextmanager
def _fixtures(run: Run, namespace: str, release: str) -> Iterator[None]:
    """The forger's edit role, the workstation's roles with Job creation, and the unlisted
    RuntimeClass, for the duration of the checks."""
    binding = _binding(FORGER, namespace, "ClusterRole", "edit", FORGER)
    workstation_role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": WORKSTATION, "namespace": namespace},
        "rules": [
            *kubernetes_cli.WORKSTATION_RULES,
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create"]},
        ],
    }
    workstation = _binding(WORKSTATION, namespace, "Role", WORKSTATION, WORKSTATION)
    editor = _binding(
        f"{WORKSTATION}-editor", namespace, "Role", f"{release}-fleet-editor", WORKSTATION
    )
    runtime = {
        "apiVersion": "node.k8s.io/v1",
        "kind": "RuntimeClass",
        "metadata": {"name": UNLISTED},
        "handler": "runc",
    }
    for document in (binding, workstation_role, workstation, editor, runtime):
        result = run(["apply", "-f", "-"], json.dumps(document))
        if result.returncode:
            raise RuntimeError(f"cannot create {document['kind']}: {result.stderr.strip()}")
    try:
        yield
    finally:
        for name in (FORGER, WORKSTATION, f"{WORKSTATION}-editor"):
            run(
                ["delete", "rolebinding", name, "--namespace", namespace, "--ignore-not-found"],
                None,
            )
        run(["delete", "role", WORKSTATION, "--namespace", namespace, "--ignore-not-found"], None)
        run(["delete", "runtimeclass", UNLISTED, "--ignore-not-found"], None)


def check(
    kubectl: Sequence[str],
    namespace: str,
    release: str,
    policy: Mapping[str, Sequence[str]] = POLICIES[0],
    engines: Mapping[str, Any] | None = None,
) -> list[str]:
    """Every RBAC, admission and model volume case the API server decides otherwise."""
    run = _runner(kubectl)
    with _fixtures(run, namespace, release):
        return [
            *check_rbac(run, namespace, release),
            # The builders' objects mount a claim as the model volume.
            *(check_admission(run, namespace, release, engines) if allows(policy, CLAIM) else []),
            *check_relabel(run, namespace),
            *check_ephemeral(run, namespace, release),
            *check_volumes(run, namespace, policy),
        ]


def _workloads_created(run: Run, namespace: str, release: str) -> str:
    """'' once every Deployment and StatefulSet of `release` has made all its pods, else what
    is missing, with the events of any pod the API server refused."""
    selector = f"app.kubernetes.io/instance={release}"
    result = run(
        ["get", "deployments,statefulsets", "-n", namespace, "-l", selector, "-o", "json"], None
    )
    if result.returncode:
        return result.stderr.strip()
    missing = [
        f"{item['kind']} {item['metadata']['name']}"
        for item in json.loads(result.stdout)["items"]
        if item["status"].get("replicas", 0) < item["spec"].get("replicas", 1)
        or item["status"].get("observedGeneration", 0) < item["metadata"]["generation"]
    ]
    if not missing:
        return ""
    events = run(
        [
            "get",
            "events",
            "-n",
            namespace,
            "--field-selector",
            "reason=FailedCreate",
            "-o",
            'jsonpath={range .items[*]}{.message}{"\\n"}{end}',
        ],
        None,
    ).stdout.strip()
    return f"{', '.join(missing)} not created: {events or 'no FailedCreate event'}"


def check_lifecycle(
    helm: Sequence[str],
    run: Run,
    namespace: str,
    release: str,
    install: Sequence[str],
    upgrade: Sequence[str],
    wait: float = 90.0,
) -> list[str]:
    """Uninstall the installed `release`, then install it, upgrade it, uninstall it and install
    it again, every step with the namespace's admission policy present. `install` is the chart
    and values arguments of each install, and `upgrade` those of the upgrade, which should
    change the workloads' pod templates. After each install and upgrade, every workload of the
    release must make its pods."""
    policy = f"{namespace}-narwhal"
    steps = (
        ("uninstall", ["uninstall", release, "-n", namespace]),
        ("install", ["install", release, *install, "-n", namespace]),
        ("upgrade", ["upgrade", release, *upgrade, "-n", namespace]),
        ("uninstall", ["uninstall", release, "-n", namespace]),
        ("install again", ["install", release, *install, "-n", namespace]),
    )
    for name, args in steps:
        present = run(["get", "validatingadmissionpolicy", policy, "-o", "name"], None)
        if present.returncode:
            return [f"the admission policy {policy} is missing before {name}"]
        result = subprocess.run([*helm, *args], capture_output=True, text=True, check=False)
        if result.returncode:
            return [f"helm {name} failed: {result.stderr.strip()}"]
        if name == "uninstall":
            continue
        deadline = time.monotonic() + wait
        while (problem := _workloads_created(run, namespace, release)) and (
            time.monotonic() < deadline
        ):
            time.sleep(2)
        if problem:
            return [f"after helm {name}: {problem}"]
    return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--policy", type=json.loads, default=POLICIES[0])
    parser.add_argument("--engines", help="a JSON file of engine values to admit")
    parser.add_argument("--helm", default="helm")
    parser.add_argument(
        "--lifecycle",
        nargs=2,
        metavar=("CHART", "VALUES"),
        help="first uninstall and reinstall the release from CHART and VALUES, as check_lifecycle",
    )
    parser.add_argument(
        "--upgrade-set",
        action="append",
        default=[],
        help="a --set for the lifecycle's upgrade, which should change a pod template",
    )
    args = parser.parse_args(argv)
    kubectl = [args.kubectl, "--kubeconfig", args.kubeconfig]
    if args.lifecycle:
        chart, values = args.lifecycle
        install = [chart, "-f", values]
        upgrade = [*install, *(f"--set={item}" for item in args.upgrade_set)]
        problems = check_lifecycle(
            [args.helm, "--kubeconfig", args.kubeconfig],
            _runner(kubectl),
            args.namespace,
            args.release,
            install,
            upgrade,
        )
        if problems:
            print(*problems, sep="\n")
            return 1
    engines = None
    if args.engines:
        with open(args.engines) as stream:
            engines = json.load(stream)
    problems = check(
        kubectl,
        args.namespace,
        args.release,
        args.policy,
        engines,
    )
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
