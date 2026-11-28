"""A `NarwhalFleet` for tests, with the inputs `kubernetes_cluster.environment` holds, and the
fleets the CRD's CEL rules refuse.

`REFUSED` maps each case to a change of the fleet, the field the API server names and the
check that refuses it. Unit tests cannot run CEL, so `refusals` applies each case to a real
API server and names every case it does not refuse for that reason.
"""

from __future__ import annotations

import copy
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from narwhal.deployment import fleet_resource
from tests.deployment.kubernetes_cluster import (
    CONTROL_SECRET_KEY,
    CONTROL_SECRET_NAME,
    FABRIC_IMAGE,
    IMAGE,
    NAMESPACE,
    NODES,
    TREE,
)

UID = "0f6b1d2c-3a4e-4b5f-8c7d-9e0a1b2c3d4e"
HAPROXY_IMAGE = "registry.example/haproxy@sha256:" + "c" * 64


def fleet() -> dict:
    """A fleet of two engines on two nodes, as the API server stores it."""
    return {
        "apiVersion": fleet_resource.API_VERSION,
        "kind": fleet_resource.KIND,
        "metadata": {"name": fleet_resource.NAME, "namespace": NAMESPACE, "uid": UID},
        "spec": {
            "images": {"haproxy": HAPROXY_IMAGE, "fabric": FABRIC_IMAGE},
            "engines": {
                "image": IMAGE,
                "model": {"name": "served-model", "treeSha256": TREE},
                "modelVolume": {"persistentVolumeClaim": {"claimName": "model", "readOnly": True}},
                "resources": {"gpu": "gpu.example.com/gpu", "rdma": "rdma.example.com/hca"},
                "ports": {
                    "engine": 8100,
                    "attestation": 8200,
                    "sideChannel": 5600,
                    "ucxTcpPortRange": "20000-20099",
                },
                "nodes": [{"node": node, "fabricInterface": "fabric0"} for node in NODES.values()],
                "launch": [{"name": "NARWHAL_ENGINE_BACKEND", "value": "vllm"}],
            },
            "slo": {"ttftS": "4", "tpotS": "0.1", "routerOverheadS": "0.05"},
            "firstTokenMarginS": "0.25",
            "profiling": {
                "prefillLens": [256, 1024, 4096],
                "decodeInputLens": [512, 4096],
                "warmupRequests": 8,
            },
            "calibration": {
                "inputTokens": [256, 4096],
                "samples": 100,
                "observationTimeoutS": "20",
            },
            "fabric": {
                "workload": {
                    "promptTokens": 4096,
                    "handoffsPerS": "4",
                    "burst": 2,
                    "transferBudgetS": "0.5",
                    "headroom": "1.2",
                },
                "testPort": 5201,
            },
            "routers": {"storageClassName": "local", "size": "1Gi"},
            "controlTokenSecret": {"name": CONTROL_SECRET_NAME, "key": CONTROL_SECRET_KEY},
            "haproxy": {"replicas": 1, "clusterDomain": "cluster.local"},
            "stopped": False,
            "retainRuns": 3,
        },
    }


# The model volume sources `fleet()` does not use.
VOLUMES = (
    {"hostPath": {"path": "/srv/models/checkpoint", "type": "Directory"}},
    {"nfs": {"server": "nfs.example.com", "path": "/exports/models", "readOnly": True}},
    {
        "csi": {
            "driver": "csi.example.com",
            "readOnly": True,
            "volumeAttributes": {"share": "models"},
        }
    },
)


def full_fleet() -> dict:
    """`fleet()` with every optional field set."""
    document = fleet()
    spec = document["spec"]
    engines = spec["engines"]
    engines["model"].update(file="gguf/model.gguf", fileSha256="a" * 64)
    engines["apiKeySecret"] = {"name": "engine-key-v2", "key": "api-key"}
    engines["resources"]["runtimeClassName"] = "nvidia"
    engines["nodes"][1]["rdma"] = {"gidIndex": 3, "roceMode": "v2"}
    engines["nodes"][1]["ports"] = {
        "engine": 8101,
        "attestation": 8201,
        "sideChannel": 5601,
        "ucxTcpPortRange": "20100-20199",
    }
    spec["profiling"]["decodeConcurrency"] = [1, 8]
    spec["routers"]["args"] = ["--lease-ttl", "6"]
    spec["timeouts"] = {"defaultSeconds": 600, "stages": {"profile": 7200}}
    spec["until"] = "router-start"
    return document


def changed(change: Callable[[dict], object]) -> dict:
    """`fleet()` after `change` edits it in place."""
    document = copy.deepcopy(fleet())
    change(document)
    return document


def _host(path: str, kind: str = "Directory") -> Callable[[dict], object]:
    return lambda d: _engines(d).update(modelVolume={"hostPath": {"path": path, "type": kind}})


def _launch(name: str, value: str = "x") -> Callable[[dict], object]:
    return lambda d: _engines(d)["launch"].append({"name": name, "value": value})


def _engines(document: dict) -> dict:
    return document["spec"]["engines"]


def _volume(document: dict) -> dict:
    return _engines(document)["modelVolume"]


@dataclass(frozen=True)
class Refusal:
    """A change the API server refuses, at `field`, by `check`: a CEL rule's `message`, or
    the `pattern` or `enum` of the field's schema."""

    change: Callable[[dict], object]
    field: str
    check: str
    message: str = ""


def _rule(change: Callable[[dict], object], field: str, message: str) -> Refusal:
    return Refusal(change, field, "rule", message)


_LAUNCH_SET = (
    "another field of the fleet sets this variable, or it names a credential, which belongs in "
    "engines.apiKeySecret or controlTokenSecret"
)
_SYSTEM = "must not be or lie in a node's system, kubelet or container runtime directory"
_DOTS = "must not hold a . or .. segment"
_HOST_PATH = "spec.engines.modelVolume.hostPath.path"

REFUSED: dict[str, Refusal] = {
    "the control token in the engine key's Secret key": _rule(
        lambda d: _engines(d).update(apiKeySecret=dict(d["spec"]["controlTokenSecret"])),
        "spec",
        fleet_resource.SHARED_SECRET,
    ),
    "another fleet name": _rule(
        lambda d: d["metadata"].update(name="second"),
        "",
        "a namespace holds one NarwhalFleet, and it is named fleet",
    ),
    "two model volume sources": _rule(
        lambda d: _volume(d).update(hostPath={"path": "/models", "type": "Directory"}),
        "spec.engines.modelVolume",
        "set exactly one of persistentVolumeClaim, hostPath, nfs, csi",
    ),
    "a writable model volume": _rule(
        lambda d: _volume(d)["persistentVolumeClaim"].update(readOnly=False),
        "spec.engines.modelVolume.persistentVolumeClaim.readOnly",
        "the model volume is mounted read-only",
    ),
    "the host root": Refusal(_host("/"), _HOST_PATH, "pattern"),
    "a doubled slash": Refusal(_host("//models"), _HOST_PATH, "pattern"),
    "a trailing slash": Refusal(_host("/models/"), _HOST_PATH, "pattern"),
    "a dot-dot host path": _rule(_host("/models/../etc"), _HOST_PATH, _DOTS),
    "a dot host path": _rule(_host("/models/./a"), _HOST_PATH, _DOTS),
    "a process root": _rule(_host("/proc/1/root"), _HOST_PATH, _SYSTEM),
    "node configuration": _rule(_host("/etc/kubernetes"), _HOST_PATH, _SYSTEM),
    "the kubelet's state": _rule(_host("/var/lib/kubelet"), _HOST_PATH, _SYSTEM),
    "a runtime socket": _rule(_host("/run/containerd/containerd.sock"), _HOST_PATH, _SYSTEM),
    "a legacy runtime socket": _rule(_host("/var/run/docker.sock"), _HOST_PATH, _SYSTEM),
    "a runtime's images": _rule(_host("/var/lib/containerd"), _HOST_PATH, _SYSTEM),
    "a parent of the kubelet's state": _rule(_host("/var/lib"), _HOST_PATH, _SYSTEM),
    "devices": _rule(_host("/dev"), _HOST_PATH, _SYSTEM),
    **{
        f"the host directory {path}": _rule(_host(path), _HOST_PATH, _SYSTEM)
        for path in (
            "/var/lib/etcd",
            "/root",
            "/home/admin",
            "/var/log/pods",
            "/var/lib/k0s",
            "/var/snap",
            "/opt/cni/bin",
            "/host",
            "/var/lib/cni",
            "/var/lib/docker",
            "/var/lib/rancher",
            "/opt",
        )
    },
    "a path of any type": Refusal(
        _host("/models", ""), "spec.engines.modelVolume.hostPath.type", "enum"
    ),
    "a socket type": Refusal(
        _host("/models", "Socket"), "spec.engines.modelVolume.hostPath.type", "enum"
    ),
    "a launch input a field sets": _rule(
        _launch("NARWHAL_ENGINE_IMAGE"), "spec.engines.launch[1].name", _LAUNCH_SET
    ),
    "a per-engine launch input a field sets": _rule(
        _launch("NARWHAL_NODE_1_KUBERNETES_NODE"), "spec.engines.launch[1].name", _LAUNCH_SET
    ),
    **{
        f"the credential {name}": _rule(_launch(name), "spec.engines.launch[1].name", _LAUNCH_SET)
        for name in (
            "NARWHAL_SGLANG_API_KEY",
            "NARWHAL_TOKEN",
            "NARWHAL_HF_TOKEN_FILE",
            "NARWHAL_SECRET",
            "NARWHAL_CLIENT_SECRET_FILE",
            "NARWHAL_APIKEY",
            "NARWHAL_ENGINE_APIKEY_FILE",
            "NARWHAL_API_KEYS",
            "NARWHAL_CREDENTIALS",
            "NARWHAL_AWS_CREDENTIALS_FILE",
        )
    },
    "a control character in a launch value": Refusal(
        _launch("NARWHAL_ENGINE_ENV", "{}\n"), "spec.engines.launch[1].value", "pattern"
    ),
    "a control character in a router argument": Refusal(
        lambda d: d["spec"]["routers"].update(args=["--lease-ttl\x1b"]),
        "spec.routers.args[0]",
        "pattern",
    ),
    "a control character in the model name": Refusal(
        lambda d: _engines(d)["model"].update(name="model\r"),
        "spec.engines.model.name",
        "pattern",
    ),
    "a reserved resource domain": _rule(
        lambda d: _engines(d)["resources"].update(gpu="kubernetes.io/gpu"),
        "spec.engines.resources.gpu",
        "the kubernetes.io and k8s.io domains are reserved",
    ),
    "one resource for GPUs and HCAs": _rule(
        lambda d: _engines(d)["resources"].update(rdma="gpu.example.com/gpu"),
        "spec.engines.resources",
        "gpu and rdma must name two resources",
    ),
    "a side channel on the engine port": _rule(
        lambda d: _engines(d)["ports"].update(sideChannel=8100),
        "spec.engines.ports",
        "the engine, attestation and side channel ports must differ",
    ),
    "a UCX port range that is no range": Refusal(
        lambda d: _engines(d)["ports"].update(ucxTcpPortRange="20000"),
        "spec.engines.ports.ucxTcpPortRange",
        "pattern",
    ),
    "an engine port in its own UCX port range": _rule(
        lambda d: _engines(d)["ports"].update(ucxTcpPortRange="8000-8200"),
        "spec.engines.ports",
        fleet_resource.PORTS_IN_RANGE,
    ),
    "an engine's own side channel in its own UCX port range": _rule(
        lambda d: _engines(d)["nodes"][1].update(
            ports={"sideChannel": 20050, "ucxTcpPortRange": "20000-20099"}
        ),
        "spec.engines.nodes[1].ports",
        fleet_resource.PORTS_IN_RANGE,
    ),
    "a side channel port in the launch inputs": _rule(
        _launch("NARWHAL_NODE_2_KV_SIDE_CHANNEL_PORT", "5602"),
        "spec.engines.launch[1].name",
        _LAUNCH_SET,
    ),
    "one port for engine and sidecar": _rule(
        lambda d: _engines(d)["ports"].update(attestation=8100),
        "spec.engines.ports",
        "the engine, attestation and side channel ports must differ",
    ),
    "a model file without its digest": _rule(
        lambda d: _engines(d)["model"].update(file="model.gguf"),
        "spec.engines.model",
        "set file and fileSha256 together",
    ),
    "a dot-dot model file": _rule(
        lambda d: _engines(d)["model"].update(file="a/../model.gguf", fileSha256="a" * 64),
        "spec.engines.model.file",
        _DOTS,
    ),
    "a zero time-to-first-token": _rule(
        lambda d: d["spec"]["slo"].update(ttftS="0"), "spec.slo.ttftS", "must be greater than 0"
    ),
    "a headroom below 1": _rule(
        lambda d: d["spec"]["fabric"]["workload"].update(headroom="0.9"),
        "spec.fabric.workload.headroom",
        "must be at least 1",
    ),
    "a long attribute name": _rule(
        lambda d: _engines(d).update(
            modelVolume={"csi": {"driver": "csi.example.com", "volumeAttributes": {"a" * 254: "x"}}}
        ),
        "spec.engines.modelVolume.csi.volumeAttributes",
        "an attribute name holds at most 253 characters",
    ),
}

# How the API server words each check that is not a CEL rule.
_WORDING = {"pattern": "should match", "enum": "Unsupported value"}


def refusals(kubectl: Sequence[str], namespace: str = NAMESPACE) -> list[str]:
    """Apply each refused fleet to a live API server as a server-side dry run, and name each
    case the server accepts or refuses for another reason. `kubectl` is the command and its
    connection options, such as ["kubectl", "--kubeconfig", path]; `namespace` must exist."""
    failed = []
    for case, refusal in REFUSED.items():
        document = changed(refusal.change)
        document["metadata"]["namespace"] = namespace
        del document["metadata"]["uid"]
        result = subprocess.run(
            [*kubectl, "apply", "--server-side", "--dry-run=server", "-f", "-"],
            input=json.dumps(document),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 or not _names(refusal, result.stderr):
            failed.append(f"{case}: {result.stderr.strip() or 'accepted'}")
    return failed


def _names(refusal: Refusal, stderr: str) -> bool:
    field = f"{refusal.field}: " if refusal.field else ""
    reason = refusal.message if refusal.check == "rule" else _WORDING[refusal.check]
    return any(field in line and reason in line for line in stderr.splitlines())
