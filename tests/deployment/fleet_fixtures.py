"""A `NarwhalFleet` for tests, with the inputs `kubernetes_cluster.environment` sets, and the
fleets the CRD's CEL rules refuse.

`REFUSED` maps each rule to a change of the fleet that breaks it and a regular expression the
API server's message matches. Unit tests cannot run CEL, so a test on a real API server
applies each one.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from narwhal.deployment import fleet_resource
from tests.deployment.kubernetes_cluster import (
    FABRIC_IMAGE,
    IMAGE,
    NAMESPACE,
    NARWHAL_IMAGE,
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
            "images": {"narwhal": NARWHAL_IMAGE, "haproxy": HAPROXY_IMAGE, "fabric": FABRIC_IMAGE},
            "engines": {
                "image": IMAGE,
                "model": {"name": "served-model", "treeSha256": TREE},
                "modelVolume": {"persistentVolumeClaim": {"claimName": "model", "readOnly": True}},
                "resources": {"gpu": "gpu.example.com/gpu", "rdma": "rdma.example.com/hca"},
                "ports": {"engine": 8100, "attestation": 8200},
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
            "haproxy": {"replicas": 1, "clusterDomain": "cluster.local"},
            "stopped": False,
            "retainRuns": 3,
        },
    }


def changed(change: Callable[[dict], object]) -> dict:
    """`fleet()` after `change` edits it in place."""
    document = copy.deepcopy(fleet())
    change(document)
    return document


def _engines(document: dict) -> dict:
    return document["spec"]["engines"]


def _volume(document: dict) -> dict:
    return _engines(document)["modelVolume"]


# Each CEL rule: a change that breaks it, and an expression the API server's message matches.
REFUSED: dict[str, tuple[Callable[[dict], object], str]] = {
    "another fleet name": (
        lambda d: d["metadata"].update(name="second"),
        "a namespace holds one NarwhalFleet",
    ),
    "no model volume source": (
        lambda d: _engines(d).update(modelVolume={}),
        # Kubernetes 1.30 applies an empty object as null, and refuses its type first.
        "set exactly one of|must be of type object",
    ),
    "two model volume sources": (
        lambda d: _volume(d).update(hostPath={"path": "/models"}),
        "set exactly one of",
    ),
    "a writable model volume": (
        lambda d: _volume(d)["persistentVolumeClaim"].update(readOnly=False),
        "mounted read-only",
    ),
    "the host root": (
        lambda d: _engines(d).update(modelVolume={"hostPath": {"path": "/"}}),
        "below /",
    ),
    "a dot-dot host path": (
        lambda d: _engines(d).update(modelVolume={"hostPath": {"path": "/models/../etc"}}),
        "segment",
    ),
    "a launch input a field sets": (
        lambda d: _engines(d)["launch"].append({"name": "NARWHAL_ENGINE_IMAGE", "value": "x"}),
        "another field of the fleet sets this variable",
    ),
    "a per-engine launch input a field sets": (
        lambda d: _engines(d)["launch"].append(
            {"name": "NARWHAL_NODE_1_KUBERNETES_NODE", "value": "x"}
        ),
        "another field of the fleet sets this variable",
    ),
    "a credential in a launch input": (
        lambda d: _engines(d)["launch"].append({"name": "NARWHAL_SGLANG_API_KEY", "value": "x"}),
        "names a credential",
    ),
    "a reserved resource domain": (
        lambda d: _engines(d)["resources"].update(gpu="kubernetes.io/gpu"),
        "reserved",
    ),
    "one resource for GPUs and HCAs": (
        lambda d: _engines(d)["resources"].update(rdma="gpu.example.com/gpu"),
        "two resources",
    ),
    "one port for engine and sidecar": (
        lambda d: _engines(d)["ports"].update(attestation=8100),
        "two ports",
    ),
    "a model file without its digest": (
        lambda d: _engines(d)["model"].update(file="model.gguf"),
        "together",
    ),
    "a dot-dot model file": (
        lambda d: _engines(d)["model"].update(file="../model.gguf", fileSha256="a" * 64),
        "segment",
    ),
    "a zero time-to-first-token": (
        lambda d: d["spec"]["slo"].update(ttftS="0"),
        "greater than 0",
    ),
    "a headroom below 1": (
        lambda d: d["spec"]["fabric"]["workload"].update(headroom="0.9"),
        "at least 1",
    ),
    "a long attribute name": (
        lambda d: _engines(d).update(
            modelVolume={"csi": {"driver": "csi.example.com", "volumeAttributes": {"a" * 254: "x"}}}
        ),
        "at most 253",
    ),
}
