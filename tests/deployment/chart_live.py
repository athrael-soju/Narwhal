"""The narwhal chart's RBAC and admission policy, as an API server applies them.

Unit tests read the rendered chart but cannot ask an API server what a Role grants or run the
admission policy's CEL. `check(kubectl, namespace, release)` does both on a cluster where the
chart is installed, and returns every case the server decided otherwise:

- `CONTROLLER_RULES` is what the controller's Role and ClusterRole grant, and `FORBIDDEN` a
  sample of what they must not, checked with `kubectl auth can-i`.
- `ADMITTED` are the objects the controller's builders make and a pod of each, which the
  policy must admit; `REFUSED` are pods it must refuse, with the message it names.
- `volume_cases(policy)` pairs each of `MODEL_VOLUMES` with whether
  `fleet_resource.check_model_volume` allows it under that policy, which the admission policy
  must match.

Each object is sent with `kubectl create --dry-run=server`, which runs admission and stores
nothing.

`python -m tests.deployment.chart_live --kubeconfig PATH --namespace NS --release NAME [--policy
JSON]` runs the checks against an installed release whose model volume values are `--policy`.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from narwhal.deployment import fleet_resource
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
CLUSTER_RULES: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        ("", "nodes", "get", ""),
        ("", "nodes", "list", ""),
        ("apiextensions.k8s.io", "customresourcedefinitions", "get", fleet_resource.CRD_NAME),
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
)

NARWHAL = "registry.example/narwhal@sha256:" + "a" * 64
OWNER = objects.Owner("fleet", "0b6f4c1e-5d1a-4c55-9f7e-3a1f0f5e9a11", "example")
CLAIM = {"persistentVolumeClaim": {"claimName": "models", "readOnly": True}}


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
    return {"restartPolicy": "Never", "containers": [_container()], **fields}


def pod_of(document: Mapping[str, Any], name: str) -> dict:
    """The pod a Job, StatefulSet or Deployment makes from its template."""
    template = document["spec"]["template"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": dict(template["metadata"]["labels"])},
        "spec": copy.deepcopy(template["spec"]),
    }


def _in(namespace: str, document: Mapping[str, Any]) -> dict:
    document = copy.deepcopy(dict(document))
    document["metadata"]["namespace"] = namespace
    return document


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


def admitted(
    release: str, engines: Mapping[str, Any] | None = None, state_claim: str = "narwhal-state"
) -> dict[str, dict]:
    """The objects the controller's builders make for a fleet in `release`, and a pod of each
    workload: every gate kind, HAProxy and, with `engines` values, the engine StatefulSet."""
    account = f"{release}-engine"
    fleet = objects.Fleet(
        owner=OWNER,
        narwhal_image=NARWHAL,
        state_claim=state_claim,
        haproxy_image="registry.example/haproxy@sha256:" + "b" * 64,
        fabric_image="registry.example/perftest@sha256:" + "c" * 64,
        engine_account=account,
        rdma_resource="rdma.example.com/hca",
        model_volume=CLAIM,
    )
    found: dict[str, dict] = {}
    workloads = [
        objects.job(fleet, _gate(kind, service_account=kind == objects.ACCOUNT_KIND))
        for kind in objects.GATE_KINDS
    ]
    workloads += [d for d in objects.haproxy(fleet) if d["kind"] == "Deployment"]
    if engines is not None:
        values = copy.deepcopy(dict(engines))
        values["podTemplate"]["serviceAccountName"] = account
        values["sidecarImage"] = NARWHAL
        values["stateClaim"] = state_claim
        workloads += [
            d
            for d in objects.engines(
                objects.Fleet(
                    owner=OWNER,
                    narwhal_image=NARWHAL,
                    state_claim=state_claim,
                    engine_account=account,
                    rdma_resource=values["rdmaResource"],
                    model_volume=values["modelVolume"],
                ),
                _without_key(values),
            )
            if d["kind"] == "StatefulSet"
        ]
    for document in workloads:
        name = f"{document['kind'].lower()}/{document['metadata']['name']}"
        found[name] = document
        found[f"pod of {name}"] = pod_of(document, f"{document['metadata']['name']}-pod")
    # A ROCm engine runs without a seccomp filter, as Docker's ROCm launch does.
    found["an unconfined engine pod with IPC_LOCK"] = _engine_pod(
        release,
        lambda spec: spec.update(
            securityContext={"seccompProfile": {"type": "Unconfined"}},
            containers=[_container(securityContext={"capabilities": {"add": ["IPC_LOCK"]}})],
        ),
    )
    return found


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


def _engine_pod(release: str, change: Callable[[dict], object]) -> dict:
    """A pod labelled as an engine, changed by `change`."""
    pod = _pod("engine-x", objects.ENGINE, _plain(hostNetwork=True))
    pod["spec"]["serviceAccountName"] = f"{release}-engine"
    change(pod["spec"])
    return pod


def _set_container(**fields: Any) -> Callable[[dict], object]:
    return lambda spec: spec["containers"][0].update(fields)


def _model(source: Mapping[str, Any], read_only: bool = True) -> dict:
    spec = _plain(volumes=[{"name": objects.MODEL_VOLUME, **copy.deepcopy(dict(source))}])
    spec["containers"][0]["volumeMounts"] = [
        {"name": objects.MODEL_VOLUME, "mountPath": "/model", "readOnly": read_only}
    ]
    return spec


def refused(release: str) -> dict[str, tuple[dict, str]]:
    """Pods and workloads the policy must refuse, and the words its refusal names."""
    gate = objects.labels(objects.GATE, gate="preflight")
    fabric = objects.labels(objects.GATE, gate="fabric-server")
    host = "uses the host network"
    capability = "adds a capability"
    cases: dict[str, tuple[dict, str]] = {
        "host network, unlabelled": (_pod("plain", None, _plain(hostNetwork=True)), host),
        "host network, router": (_pod("router", "router", _plain(hostNetwork=True)), host),
        "host network, preflight gate": (
            _pod("gate", None, _plain(hostNetwork=True), **gate),
            host,
        ),
        "host network, another app's engine": (
            _pod(
                "other",
                None,
                _plain(hostNetwork=True),
                **{objects.NAME_LABEL: "other", objects.COMPONENT_LABEL: "engine"},
            ),
            host,
        ),
        "IPC_LOCK, preflight gate": (
            _pod(
                "gate",
                None,
                _plain(
                    containers=[_container(securityContext={"capabilities": {"add": ["IPC_LOCK"]}})]
                ),
                **gate,
            ),
            capability,
        ),
        "SYS_ADMIN, engine": (
            _engine_pod(
                release,
                _set_container(
                    securityContext={"capabilities": {"add": ["IPC_LOCK", "SYS_ADMIN"]}}
                ),
            ),
            capability,
        ),
        "NET_RAW, fabric gate": (
            _pod(
                "fabric",
                None,
                _plain(
                    hostNetwork=True,
                    containers=[_container(securityContext={"capabilities": {"add": ["NET_RAW"]}})],
                ),
                **fabric,
            ),
            capability,
        ),
        "privileged engine": (
            _engine_pod(release, _set_container(securityContext={"privileged": True})),
            "privileged",
        ),
        "privileged init container": (
            _pod(
                "init",
                None,
                _plain(
                    initContainers=[_container(name="init", securityContext={"privileged": True})]
                ),
            ),
            "privileged",
        ),
        "host PID, engine": (_engine_pod(release, lambda s: s.update(hostPID=True)), "process"),
        "host IPC": (_pod("ipc", None, _plain(hostIPC=True)), "process or IPC"),
        "unconfined seccomp, gate": (
            _pod(
                "gate",
                None,
                _plain(securityContext={"seccompProfile": {"type": "Unconfined"}}),
                **gate,
            ),
            "seccomp",
        ),
        "unconfined seccomp, container": (
            _pod(
                "gate",
                None,
                _plain(
                    containers=[
                        _container(securityContext={"seccompProfile": {"type": "Unconfined"}})
                    ]
                ),
                **gate,
            ),
            "seccomp",
        ),
        "image by tag": (
            _pod("tag", None, _plain(containers=[_container(image="busybox:1.37")])),
            "registry digest",
        ),
        "host path on an engine": (
            _engine_pod(
                release,
                lambda s: s.update(
                    volumes=[
                        {"name": "data", "hostPath": {"path": "/srv/models", "type": "Directory"}}
                    ]
                ),
            ),
            "volume data",
        ),
        "host path on a pod": (
            _pod("plain", None, _plain(volumes=[{"name": "root", "hostPath": {"path": "/"}}])),
            "volume root",
        ),
        "an NFS volume besides the model": (
            _pod(
                "plain",
                None,
                _plain(
                    volumes=[{"name": "data", "nfs": {"server": "nfs.example.com", "path": "/"}}]
                ),
            ),
            "volume data",
        ),
        "a writable model volume": (_pod("model", None, _model(CLAIM, False)), "read-only"),
    }
    for kind, api in (("Job", "batch/v1"), ("StatefulSet", "apps/v1"), ("Deployment", "apps/v1")):
        labels = objects.labels(objects.ROUTER_SERVICE)
        template = {"metadata": {"labels": labels}, "spec": _plain(hostNetwork=True)}
        if kind != "Job":
            template["spec"]["restartPolicy"] = "Always"
        spec: dict[str, Any] = {"template": template}
        if kind != "Job":
            spec["selector"] = {"matchLabels": labels}
        if kind == "StatefulSet":
            spec["serviceName"] = "x"
        cases[f"a {kind} on the host network"] = (
            {"apiVersion": api, "kind": kind, "metadata": {"name": "host"}, "spec": spec},
            host,
        )
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


def _create(run: Run, namespace: str, document: Mapping[str, Any]) -> tuple[bool, str]:
    result = run(
        ["create", "--dry-run=server", "-o", "name", "-f", "-"],
        json.dumps(_in(namespace, document)),
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
    grants = [(rule, namespace) for rule in sorted(CONTROLLER_RULES)]
    grants += [(rule, None) for rule in sorted(CLUSTER_RULES)]
    denials = [(rule, namespace) for rule in FORBIDDEN]
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
    for name, document in admitted(release, engines).items():
        ok, error = _create(run, namespace, document)
        if not ok:
            found.append(f"refused {name}: {error}")
    for name, (document, words) in refused(release).items():
        ok, error = _create(run, namespace, document)
        if ok:
            found.append(f"admitted {name}")
        elif words not in error:
            found.append(f"refused {name} for another reason: {error}")
    return found


def check_volumes(run: Run, namespace: str, policy: Mapping[str, Sequence[str]]) -> list[str]:
    found = []
    for name, (document, expected) in volume_cases(policy).items():
        ok, error = _create(run, namespace, document)
        if ok != expected:
            found.append(
                f"{'admitted' if ok else 'refused'} model volume {name} under {policy}: {error}"
            )
    return found


def check(
    kubectl: Sequence[str],
    namespace: str,
    release: str,
    policy: Mapping[str, Sequence[str]] = POLICIES[0],
    engines: Mapping[str, Any] | None = None,
) -> list[str]:
    """Every RBAC, admission and model volume case the API server decides otherwise."""
    run = _runner(kubectl)
    return [
        *check_rbac(run, namespace, release),
        # The builders' objects mount a claim as the model volume.
        *(check_admission(run, namespace, release, engines) if allows(policy, CLAIM) else []),
        *check_volumes(run, namespace, policy),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--policy", type=json.loads, default=POLICIES[0])
    parser.add_argument("--engines", help="a JSON file of engine values to admit")
    args = parser.parse_args(argv)
    engines = None
    if args.engines:
        with open(args.engines) as stream:
            engines = json.load(stream)
    problems = check(
        [args.kubectl, "--kubeconfig", args.kubeconfig],
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
