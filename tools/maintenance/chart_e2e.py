"""Run both charts and the fleet controller on a local kind cluster with no GPUs.

`make chart-e2e` runs this. It creates a kind cluster with a control plane and two workers,
installs the CRD chart and then the narwhal chart (the controller, its RBAC, the admission
policy, the NetworkPolicy and monitoring for the cluster's Prometheus Operator), and applies a
`NarwhalFleet` with `kubectl apply --server-side` only, as a GitOps tool would. It prints one
JSON line per check and fails on the first check that does not hold.

Each worker advertises a fake GPU and a fake RDMA extended resource, so the fleet passes
`access`. The stages after it need what kind cannot give: `discover` reads `nvidia-smi` and
`/proc/driver/nvidia` in the engine image, and `engine-prepare` needs the launcher of an
attested backend. So the controller is tested on the stages it can reach:

- with `spec.until: access`, the fleet becomes Ready, and an SLO edit makes it Ready again
  without running `access` again. The API server refuses the fleet with its routers' control
  token in the engine key's Secret key;
- with `spec.until: discover`, the discovery Jobs start and wait on a model volume whose
  directory does not exist yet. An SLO edit leaves them running; a checkpoint digest edit
  cancels the stage and starts it again with new Jobs;
- deleting the controller pod mid-Job leaves the Jobs alone, and the next controller adopts
  them: the same Jobs, the same attempt, no duplicate and no stall;
- `spec.stopped` deletes the routers, HAProxy and Jobs and keeps the volumes, and clearing it
  starts the stage again;
- a namespace editor's forged engine pod is refused by the admission policy;
- `narwhal deploy plan|run|status|down --target kubernetes`, as a ServiceAccount holding only
  the workstation's documented role, follows the fleet without changing its spec, copies its
  evidence through an export Job the controller makes, and cannot create a Job itself;
- deleting the fleet leaves its objects to garbage collection.

The serving path runs on two llama.cpp engines that the chart's `directEngines` starts on
CPUs, as `narwhal dev` starts them on a host. A Job measures their nominal profiles and writes
the fleet file, standing in for the gate Jobs. The router pair and HAProxy come from the
controller's own builders, sent as the controller's ServiceAccount with the fleet as their
owner, because the controller builds them only at `router-start`. The test checks that the
routers run on separate workers and refuse `POST /narwhal/engines` without the control token
(which HAProxy does not forward), sends completions through HAProxy, and under a steady stream
deletes the router pod that holds the router Lease: every request must succeed, and none may
wait longer than the 10 seconds HAProxy holds a request while no router is in rotation.

The cluster, its volumes and the Narwhal image it builds are removed at the end, unless
`--keep` is given.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import itertools
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(ROOT))
sys.path.insert(2, str(ROOT / "tools" / "maintenance"))

import operator_schemas

from narwhal.config.loading import load as load_fleet
from narwhal.deployment import fleet_resource, kubernetes_cli, kubernetes_target
from narwhal.deployment import kubernetes_objects as objects
from narwhal.deployment.direct_engine_set import direct_values, fleet_engines
from narwhal.observability.stack.chart_dashboard import chart_dashboard
from narwhal.serving.control_token import ROUTER_TOKEN_ENV
from tests.deployment import chart_live, fleet_fixtures

CRD_CHART = ROOT / "deploy" / "helm" / "narwhal-crds"
CHART = ROOT / "deploy" / "helm" / "narwhal"
RELEASE = "narwhal"
NAMESPACE = "narwhal-e2e"
CONTROLLER = f"{RELEASE}-controller"
CONTROLLER_ACCOUNT = f"system:serviceaccount:{NAMESPACE}:{CONTROLLER}"
# A user with the namespace's edit role, who forges an engine pod.
EDITOR = "chart-e2e-editor"
# The workstation's ServiceAccount, which holds the documented workstation role alone.
WORKSTATION = "chart-e2e-workstation"
# The field manager the fleet is applied under, as Argo CD or Flux applies it server-side.
GITOPS_MANAGER = "chart-e2e-gitops"
NODE_IMAGE = (
    "kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
)
# llama.cpp's CPU server image (build 12808) and HAProxy, by registry digest.
ENGINE_IMAGE = (
    "ghcr.io/ggml-org/llama.cpp@sha256:"
    "535b0b2eb8e184668698ebe8204f86ed5e57825965fc5b81b9992c556822e6b9"
)
ENGINE_BINARY = "/app/llama-server"
HAPROXY_IMAGE = (
    "docker.io/library/haproxy@sha256:"
    "7816a9801a6846b912bc37e48529808e2fa87c3c47992441940f68e393ebcfeb"
)
# The model and fleet settings of the default `narwhal dev` template.
TEMPLATE = ROOT / "src" / "narwhal" / "dev" / "direct-ernie45-03b-v1.json"
MODEL_CACHE = ROOT / "local" / "cache" / "chart-e2e"
SCHEMAS = ROOT / "local" / "schemas"
ENGINES = 2
ENGINE_CPUS = 4
# The fake extended resources each worker advertises, as device plugins would.
GPU = "gpu.example.com/gpu"
RDMA = "rdma.example.com/hca"
# HAProxy holds a request for up to 10 one-second retries while no router is in rotation.
TAKEOVER_WINDOW_S = 10.0
# Seconds one completion of the stream may take once a router serves it.
SERVE_ALLOWANCE_S = 5.0
STREAM_LOOPS = 2
STREAM_BEFORE_S = 15.0
STREAM_AFTER_S = 30.0
# Every worker mounts one host directory here, so a hostPath volume in it is ReadWriteMany.
NODE_VOLUMES = "/var/lib/narwhal-e2e"
SHARED_VOLUMES = {"narwhal-state": "1Gi", "narwhal-engine-shared": "1Gi"}
NODE_MODELS = "/narwhal-models"
STORAGE_CLASS = "narwhal-e2e"
# The direct engines' model claim, and the fleet's, whose directory appears only on demand.
MODEL_CLAIM = "narwhal-models"
FLEET_MODEL_CLAIM = "fleet-model"
FLEET_MODEL_DIR = f"{NODE_VOLUMES}/fleet-model"
FLEET_FILE = "fleet.json"
# The routers' control token, which the routes that change membership or lifecycle require.
CONTROL_SECRET = {"name": "router-token-v1", "key": "token"}
GATE_SELECTOR = f"{objects.COMPONENT_LABEL}={objects.GATE}"
GATE_KIND_LABEL = fleet_resource.qualified("gate")
FLEET_KINDS = ("statefulsets", "deployments", "jobs", "services", "configmaps", "pods")
PROFILE_SCRIPT = """
import json, os, sys
from pathlib import Path
from narwhal.backends import load
from narwhal.deployment.direct_start import measure
from narwhal.profiling.model import nominal_profile
from narwhal.profiling.store import ProfileStore
fleet, runtime = json.loads(sys.argv[1]), json.loads(sys.argv[2])
start = load(fleet["engine"]["backend"]).direct_start()
store = ProfileStore(Path(fleet["profiles"]["path"]))
for iid, figures in measure(start, fleet, runtime).items():
    store.put(nominal_profile(iid, prefill_token_s=figures.prefill_token_s,
        token_interval_s=figures.token_interval_s, kv_capacity_tokens=figures.kv_capacity_tokens,
        max_requests=figures.max_requests))
pending = Path(sys.argv[3] + ".pending")
pending.write_text(json.dumps(fleet, indent=2) + "\\n")
os.replace(pending, sys.argv[3])
print("profiled", ", ".join(engine["iid"] for engine in fleet["engines"]))
"""
# The client streams completions through HAProxy and prints one JSON line per request.
CLIENT_SCRIPT = """
import json, sys, threading, time, urllib.error, urllib.request
url, model, ready_s, duration_s, loops = sys.argv[1], sys.argv[2], *map(float, sys.argv[3:6])
body = json.dumps({"model": model, "prompt": "The capital of France is", "max_tokens": 8,
                   "temperature": 0}).encode()
def complete():
    request = urllib.request.Request(url, body, {"content-type": "application/json"})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            reply = json.load(response)
            status, error = response.status, ""
            if not reply.get("choices"):
                status, error = 0, "no choices"
    except urllib.error.HTTPError as exc:
        status, error = exc.code, exc.read().decode(errors="replace")[:200]
    except Exception as exc:
        status, error = 0, f"{type(exc).__name__}: {exc}"
    return {"start": started, "end": time.time(), "status": status, "error": error}
deadline = time.time() + ready_s
while True:
    first = complete()
    if first["status"] == 200:
        break
    if time.time() > deadline:
        print(json.dumps({"ready": False, **first}), flush=True)
        sys.exit(1)
    time.sleep(1)
print(json.dumps({"ready": True, **first}), flush=True)
stop = time.time() + duration_s
lock = threading.Lock()
def loop():
    while time.time() < stop:
        result = complete()
        with lock:
            print(json.dumps(result), flush=True)
threads = [threading.Thread(target=loop) for _ in range(int(loops))]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print(json.dumps({"done": True}), flush=True)
"""


class Failure(RuntimeError):
    pass


def run(argv: Sequence[str], *, capture: bool = False, check: bool = True, **kwargs: Any) -> str:
    result = subprocess.run(list(argv), check=False, text=True, capture_output=capture, **kwargs)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise Failure(f"{' '.join(argv[:4])} exited {result.returncode}: {detail[-2000:]}")
    return result.stdout if capture else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_model(model: dict) -> Path:
    """The template's model file in the cache, downloaded and checked against its SHA-256."""
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    path = MODEL_CACHE / model["filename"]
    if path.is_file() and sha256(path) == model["sha256"]:
        return path
    url = (
        f"https://huggingface.co/{model['repository']}/resolve/{model['revision']}/"
        f"{model['filename']}"
    )
    with (
        tempfile.NamedTemporaryFile(dir=MODEL_CACHE, delete=False) as pending,
        urllib.request.urlopen(url, timeout=600) as response,
    ):
        shutil.copyfileobj(response, pending)
    if sha256(Path(pending.name)) != model["sha256"]:
        os.unlink(pending.name)
        raise Failure(f"{url} differs from the template's SHA-256")
    os.replace(pending.name, path)
    return path


def wait_for(
    what: str, condition: Callable[[], Any], timeout_s: float, interval_s: float = 1.0
) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval_s)
    raise Failure(f"timed out after {timeout_s:g} s waiting for {what}")


class Summary:
    """One JSON line per check, and the run's elapsed time."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.checks: list[dict[str, Any]] = []

    def passed(self, name: str, **details: Any) -> dict[str, Any]:
        line = {"check": name, "passed": True, "at_s": self.elapsed(), **details}
        self.checks.append(line)
        print(json.dumps(line), flush=True)
        return line

    def skipped(self, name: str, reason: str) -> None:
        line = {"check": name, "skipped": True, "at_s": self.elapsed(), "reason": reason}
        self.checks.append(line)
        print(json.dumps(line), flush=True)

    def failed(self, error: str) -> None:
        print(json.dumps({"passed": False, "at_s": self.elapsed(), "error": error}), flush=True)

    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 1)


class Cluster:
    """One kind cluster with two workers, and the kubectl and helm that reach it."""

    def __init__(self, tools: Path, work: Path) -> None:
        self.tools = tools
        self.name = f"narwhal-e2e-{uuid.uuid4().hex[:8]}"
        self.workers = [f"{self.name}-worker", f"{self.name}-worker2"]
        self.kubeconfig = work / "kubeconfig"
        self.work = work
        self.volumes = work / "volumes"
        self.created = False

    def kubectl_argv(self) -> list[str]:
        return [str(self.tools / "kubectl"), "--kubeconfig", str(self.kubeconfig), "-n", NAMESPACE]

    def kubectl(self, *args: str, capture: bool = True, check: bool = True, **kw: Any) -> str:
        return run([*self.kubectl_argv(), *args], capture=capture, check=check, **kw)

    def get(self, *args: str) -> Any:
        return json.loads(self.kubectl("get", *args, "-o", "json"))

    def apply(self, document: dict, *, as_controller: bool = False) -> None:
        if as_controller:
            # As the controller writes its objects: server-side, under its field manager.
            self.kubectl(
                "apply",
                "--server-side",
                "--field-manager",
                objects.MANAGER,
                "--as",
                CONTROLLER_ACCOUNT,
                "-f",
                "-",
                input=json.dumps(document),
            )
        else:
            self.kubectl("apply", "-f", "-", input=json.dumps(document))

    def helm(self, *args: str) -> str:
        return run(
            [str(self.tools / "helm"), "--kubeconfig", str(self.kubeconfig), *args], capture=True
        )

    def node_exec(self, node: str, *args: str, check: bool = True) -> str:
        return run(["docker", "exec", node, *args], capture=True, check=check)

    def create(self, models: Path) -> None:
        self.volumes.mkdir(mode=0o755)
        for name in SHARED_VOLUMES:
            (self.volumes / name).mkdir()
            # The controller, the routers and the Jobs run as different users.
            (self.volumes / name).chmod(0o777)
        worker = {
            "role": "worker",
            "extraMounts": [
                {"hostPath": str(models), "containerPath": NODE_MODELS, "readOnly": True},
                {"hostPath": str(self.volumes), "containerPath": NODE_VOLUMES},
            ],
        }
        config = {
            "kind": "Cluster",
            "apiVersion": "kind.x-k8s.io/v1alpha4",
            "nodes": [{"role": "control-plane"}, worker, worker],
        }
        path = self.work / "kind.json"
        path.write_text(json.dumps(config))
        self.created = True
        run(
            [
                str(self.tools / "kind"),
                "create",
                "cluster",
                "--name",
                self.name,
                "--image",
                NODE_IMAGE,
                "--config",
                str(path),
                "--kubeconfig",
                str(self.kubeconfig),
                "--wait",
                "180s",
            ],
            capture=True,
        )

    def delete(self) -> None:
        if not self.created:
            return
        # Files the cluster's users wrote in the shared directory; the host user cannot.
        for name in [*SHARED_VOLUMES, Path(FLEET_MODEL_DIR).name]:
            self.node_exec(self.workers[0], "rm", "-rf", f"{NODE_VOLUMES}/{name}", check=False)
        run([str(self.tools / "kind"), "delete", "cluster", "--name", self.name], check=False)

    def load_image(self, tag: str) -> str:
        """Load a local image into every node and return its reference by digest."""
        run(
            [str(self.tools / "kind"), "load", "docker-image", tag, "--name", self.name],
            capture=True,
        )
        name = "docker.io/library/" + tag if "/" not in tag.split(":")[0] else tag
        reference = ""
        for node in [f"{self.name}-control-plane", *self.workers]:
            listing = self.node_exec(node, "ctr", "-n", "k8s.io", "images", "ls")
            for line in listing.splitlines():
                fields = line.split()
                if fields and fields[0] == name:
                    digest = fields[2]
                    break
            else:
                raise Failure(f"node {node} holds no image {name} after kind load")
            reference = name.rsplit(":", 1)[0] + "@" + digest
            # The kubelet finds an image by the reference a pod names.
            self.node_exec(node, "ctr", "-n", "k8s.io", "images", "tag", name, reference)
        return reference

    def read_state(self, relative: str) -> str:
        """A file on the state volume, read as root on a worker."""
        return self.node_exec(
            self.workers[0], "cat", f"{NODE_VOLUMES}/narwhal-state/{relative}", check=False
        )


def persistent_volume(name: str, size: str, path: str, *, read_only: bool = False) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": f"{NAMESPACE}-{name}"},
        "spec": {
            "capacity": {"storage": size},
            "accessModes": ["ReadOnlyMany" if read_only else "ReadWriteMany"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": STORAGE_CLASS,
            "claimRef": {"namespace": NAMESPACE, "name": name},
            "hostPath": {"path": path, "type": "Directory"},
        },
    }


def claim(name: str, size: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name},
        "spec": {
            "accessModes": ["ReadOnlyMany"],
            "storageClassName": STORAGE_CLASS,
            "resources": {"requests": {"storage": size}},
        },
    }


def prepare(cluster: Cluster) -> None:
    """The namespace, the volumes behind every claim and the workers' fake devices."""
    admin = cluster.kubectl_argv()[:3]
    # kind waits for the control plane only. A pod on a worker whose network and Service
    # proxy are not up yet cannot reach the API server.
    run([*admin, "wait", "--for=condition=Ready", "node", "--all", "--timeout=180s"], capture=True)
    for daemon in ("kindnet", "kube-proxy"):
        run(
            [
                *admin,
                "--namespace=kube-system",
                "rollout",
                "status",
                f"daemonset/{daemon}",
                "--timeout=180s",
            ],
            capture=True,
        )
    run([*admin, "create", "namespace", NAMESPACE], capture=True)
    for name, size in SHARED_VOLUMES.items():
        cluster.apply(persistent_volume(name, size, f"{NODE_VOLUMES}/{name}"))
    cluster.apply(persistent_volume(MODEL_CLAIM, "1Gi", NODE_MODELS, read_only=True))
    cluster.apply(claim(MODEL_CLAIM, "1Gi"))
    # The fleet's model directory does not exist, so a discovery Job waits for its mount.
    cluster.apply(persistent_volume(FLEET_MODEL_CLAIM, "1Gi", FLEET_MODEL_DIR, read_only=True))
    cluster.apply(claim(FLEET_MODEL_CLAIM, "1Gi"))
    # Sent on stdin, so the token is in no argument list.
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": CONTROL_SECRET["name"]},
            "type": "Opaque",
            "stringData": {CONTROL_SECRET["key"]: secrets.token_hex(32)},
        }
    )
    for node in cluster.workers:
        patch = [
            {"op": "add", "path": f"/status/capacity/{name.replace('/', '~1')}", "value": "1"}
            for name in (GPU, RDMA)
        ]
        cluster.kubectl(
            "patch",
            "node",
            node,
            "--subresource=status",
            "--type=json",
            "-p",
            json.dumps(patch),
        )
    for node in cluster.workers:
        wait_for(
            f"{node} to offer {GPU} and {RDMA}",
            lambda node=node: all(
                cluster.get("node", node)["status"].get("allocatable", {}).get(name) == "1"
                for name in (GPU, RDMA)
            ),
            60,
        )


def values(narwhal_image: str, model: dict, runtime: dict) -> dict:
    engines = direct_values(
        backend=runtime["backend"],
        runtime=runtime,
        binary=ENGINE_BINARY,
        model_file=model["filename"],
        served_name=model["served_name"],
        count=ENGINES,
        image=ENGINE_IMAGE,
        resources={
            "requests": {"cpu": str(ENGINE_CPUS), "memory": "2Gi"},
            "limits": {"cpu": str(ENGINE_CPUS), "memory": "2Gi"},
        },
        model_volume={"persistentVolumeClaim": {"claimName": MODEL_CLAIM, "readOnly": True}},
        shared={
            "storageClassName": STORAGE_CLASS,
            "size": SHARED_VOLUMES["narwhal-engine-shared"],
        },
        namespace=NAMESPACE,
    )
    return {
        "images": {"narwhal": narwhal_image},
        "directEngines": engines,
        "state": {"storageClassName": STORAGE_CLASS, "size": SHARED_VOLUMES["narwhal-state"]},
        # The cluster's Prometheus Operator and Grafana watch the fleet.
        "monitoring": {
            "enabled": True,
            "serviceMonitors": {"enabled": True},
            "dashboardConfigMap": {"enabled": True},
            "grafana": {"dashboard": chart_dashboard()},
        },
    }


def install(cluster: Cluster, document: dict, summary: Summary) -> None:
    """The CRD chart, the Prometheus Operator's CRDs, then the narwhal chart."""
    cluster.helm("install", "narwhal-crds", str(CRD_CHART))
    bundle = cluster.work / operator_schemas.BUNDLE
    bundle.write_bytes(operator_schemas.bundle(SCHEMAS))
    run(
        [
            *cluster.kubectl_argv()[:3],
            "apply",
            "--server-side",
            "-f",
            str(bundle),
        ],
        capture=True,
    )
    run(
        [
            *cluster.kubectl_argv()[:3],
            "wait",
            "--for=condition=Established",
            "crd",
            "--all",
            "--timeout=60s",
        ],
        capture=True,
    )
    path = cluster.work / "values.json"
    path.write_text(json.dumps(document))
    cluster.helm("install", RELEASE, str(CHART), "--namespace", NAMESPACE, "-f", str(path))
    cluster.kubectl("rollout", "status", f"deployment/{CONTROLLER}", "--timeout=180s")
    pod = controller_pod(cluster)
    holder = wait_for("the controller to hold its Lease", lambda: lease_holder(cluster) == pod, 120)
    rendered = {
        (item["kind"], item["metadata"]["name"])
        for item in manifests(cluster)
        if item["kind"] in ("ValidatingAdmissionPolicy", "NetworkPolicy", "ClusterRole", "Role")
    }
    summary.passed(
        "install",
        controller=pod,
        lease=bool(holder),
        policies=sorted(f"{kind}/{name}" for kind, name in rendered),
    )
    # Every Prometheus Operator object the chart renders, as the API server stored it.
    monitors = [
        item for item in manifests(cluster) if item["apiVersion"].startswith("monitoring.coreos")
    ]
    missing = [
        f"{item['kind']}/{item['metadata']['name']}"
        for item in monitors
        if not cluster.kubectl(
            "get", item["kind"].lower(), item["metadata"]["name"], "-o", "name", check=False
        ).strip()
    ]
    if not monitors or missing:
        raise Failure(f"the Prometheus Operator objects are not stored: {missing or 'none'}")
    summary.passed(
        "service-monitors",
        operator=operator_schemas.VERSION,
        objects=sorted(f"{item['kind']}/{item['metadata']['name']}" for item in monitors),
    )


def manifests(cluster: Cluster) -> list[dict]:
    import yaml

    text = cluster.helm("get", "manifest", RELEASE, "--namespace", NAMESPACE)
    return [document for document in yaml.safe_load_all(text) if document]


def controller_pod(cluster: Cluster) -> str:
    pods = cluster.get("pods", "-l", f"{objects.COMPONENT_LABEL}=controller")["items"]
    running = [
        pod["metadata"]["name"]
        for pod in pods
        if pod["status"].get("phase") == "Running" and not pod["metadata"].get("deletionTimestamp")
    ]
    return running[0] if len(running) == 1 else ""


def lease_holder(cluster: Cluster) -> str:
    return cluster.kubectl(
        "get",
        "lease",
        fleet_resource.CONTROLLER_LEASE,
        "-o",
        "jsonpath={.spec.holderIdentity}",
        check=False,
    ).strip()


# The fleet.


def fleet_document(narwhal_image: str, cluster: Cluster, **changes: Any) -> dict:
    """The NarwhalFleet the test applies: two engines on the two workers, the fake devices, and
    a model claim whose directory is missing."""
    document = fleet_fixtures.fleet()
    document["metadata"] = {"name": fleet_resource.NAME, "namespace": NAMESPACE}
    spec = document["spec"]
    spec["images"] = {"narwhal": narwhal_image, "haproxy": HAPROXY_IMAGE, "fabric": narwhal_image}
    engines = spec["engines"]
    # The engine image only has to run the discovery Job's Python.
    engines["image"] = narwhal_image
    engines["modelVolume"] = {
        "persistentVolumeClaim": {"claimName": FLEET_MODEL_CLAIM, "readOnly": True}
    }
    engines["resources"] = {"gpu": GPU, "rdma": RDMA}
    engines["nodes"] = [{"node": node, "fabricInterface": "eth0"} for node in cluster.workers]
    # kind's default StorageClass gives each router its own volume.
    spec["routers"] = {"storageClassName": "standard", "size": "64Mi"}
    spec["controlTokenSecret"] = dict(CONTROL_SECRET)
    spec["timeouts"] = {"defaultSeconds": 1800}
    spec["until"] = "access"
    for path, value in changes.items():
        target = spec
        *parents, last = path.split(".")
        for part in parents:
            target = target[part]
        target[last] = value
    return document


class Fleet:
    """The fleet as `kubectl apply` writes it, and what its status and run record say."""

    def __init__(self, cluster: Cluster, image: str) -> None:
        self.cluster = cluster
        self.image = image
        self.changes: dict[str, Any] = {}
        self.path = cluster.work / "fleet.json"
        self.uid = ""

    def apply(self, **changes: Any) -> int:
        """Edit the fleet file and apply it, as a GitOps tool syncs a commit."""
        self.changes.update(changes)
        self.path.write_text(json.dumps(fleet_document(self.image, self.cluster, **self.changes)))
        self.cluster.kubectl(
            "apply", "--server-side", "--field-manager", GITOPS_MANAGER, "-f", str(self.path)
        )
        live = self.get()
        self.uid = live["metadata"]["uid"]
        return int(live["metadata"]["generation"])

    def get(self) -> dict:
        return self.cluster.get("narwhalfleet", fleet_resource.NAME)

    def conditions(self) -> dict[str, dict]:
        status = self.get().get("status") or {}
        return {c["type"]: c for c in status.get("conditions") or []}

    def condition(self, kind: str) -> str:
        return self.conditions().get(kind, {}).get("status", "")

    def ready(self, generation: int) -> bool:
        live = self.get()
        status = live.get("status") or {}
        found = {c["type"]: c["status"] for c in status.get("conditions") or []}
        return status.get("observedGeneration") == generation and found.get("Ready") == "True"

    def record(self) -> dict:
        text = self.cluster.read_state(f"runs/{NAMESPACE}/{self.uid}/run.json")
        return json.loads(text) if text.strip() else {}

    def stage(self, name: str) -> dict:
        return self.record().get("stages", {}).get(name) or {}

    def access_mark(self) -> tuple[Any, Any]:
        """What changes when access runs again: its attempt and its last passed time."""
        passed = ((self.get().get("status") or {}).get("stages") or {}).get("access") or {}
        return self.stage("access").get("attempt"), passed.get("lastPassedTime")

    def not_stalled(self) -> None:
        conditions = self.conditions()
        if conditions.get("Stalled", {}).get("status") == "True":
            raise Failure(f"the fleet stalled: {conditions['Stalled'].get('message')}")


def gate_jobs(cluster: Cluster) -> dict[str, dict]:
    """The live gate Jobs: name to uid, gate kind and node."""
    found = {}
    for job in cluster.get("jobs", "-l", GATE_SELECTOR)["items"]:
        if job["metadata"].get("deletionTimestamp"):
            continue
        labels = job["metadata"].get("labels") or {}
        found[job["metadata"]["name"]] = {
            "uid": job["metadata"]["uid"],
            "gate": labels.get(GATE_KIND_LABEL, ""),
            "node": job["spec"]["template"]["spec"].get("nodeName", ""),
        }
    return found


def no_duplicates(jobs: dict[str, dict]) -> None:
    """Fail when one gate on one node has more than one live Job."""
    counted: dict[tuple[str, str], list[str]] = {}
    for name, job in jobs.items():
        counted.setdefault((job["gate"], job["node"]), []).append(name)
    duplicates = {
        f"{gate}@{node}": names for (gate, node), names in counted.items() if len(names) > 1
    }
    if duplicates:
        raise Failure(f"duplicate gate Jobs: {duplicates}")


def discovery_held(cluster: Cluster) -> dict[str, dict] | None:
    """The discover stage's Jobs once all are made and both discovery pods wait for their
    model volume."""
    jobs = gate_jobs(cluster)
    discovery = [name for name, job in jobs.items() if job["gate"] == "discovery"]
    probes = [name for name, job in jobs.items() if job["gate"] == "fabric-probe"]
    # The stage starts a discovery and an interface probe Job for each engine, one by one.
    if len(discovery) != ENGINES or len(probes) != ENGINES:
        return None
    waiting = 0
    for pod in cluster.get("pods", "-l", GATE_SELECTOR)["items"]:
        owner = (pod["metadata"].get("ownerReferences") or [{}])[0].get("name")
        if owner in discovery and pod["status"].get("phase") == "Pending":
            waiting += 1
    return jobs if waiting == ENGINES else None


def gitops(cluster: Cluster, fleet: Fleet, summary: Summary) -> None:
    """Apply the fleet with kubectl alone and wait until it is Ready up to access."""
    generation = fleet.apply()
    wait_for("the fleet to be Ready up to access", lambda: fleet.ready(generation), 180)
    printed = cluster.kubectl("get", "narwhalfleet", fleet_resource.NAME).strip()
    # The controller writes only status, so the applied spec has no drift.
    diff = subprocess.run(
        [
            *cluster.kubectl_argv(),
            "diff",
            "--server-side",
            "--field-manager",
            GITOPS_MANAGER,
            "-f",
            str(fleet.path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode != 0:
        raise Failure(f"kubectl diff --server-side shows drift on the fleet: {diff.stdout[-2000:]}")
    summary.passed(
        "gitops-ready",
        generation=generation,
        stages=(fleet.get().get("status") or {}).get("stages"),
        columns=printed.splitlines()[-1].split(),
        fleet_drift=False,
    )
    # The engines hold their key, so the API server refuses a control token in its Secret key.
    shared = fleet_document(fleet.image, cluster, **fleet.changes)
    shared["spec"]["engines"]["apiKeySecret"] = dict(CONTROL_SECRET)
    refused = subprocess.run(
        [
            *cluster.kubectl_argv(),
            "apply",
            "--server-side",
            "--dry-run=server",
            "--field-manager",
            GITOPS_MANAGER,
            "-f",
            "-",
        ],
        input=json.dumps(shared),
        capture_output=True,
        text=True,
        check=False,
    )
    if refused.returncode == 0 or fleet_resource.SHARED_SECRET not in refused.stderr:
        raise Failure(
            "the API server did not refuse a control token in the engine key's Secret: "
            f"{(refused.stderr or refused.stdout).strip()}"
        )
    summary.passed("control-token-own-secret", refused=True)
    summary.skipped(
        "controller-serving",
        "the controller cannot pass discover on kind: discovery reads nvidia-smi and "
        "/proc/driver/nvidia in the engine image, and engine-prepare needs an attested "
        "backend's launcher. The serving checks run on direct engines, with the routers and "
        "HAProxy from the controller's builders.",
    )


def edits(cluster: Cluster, fleet: Fleet, summary: Summary) -> dict[str, dict]:
    """Each edit reruns only the stages its fact reruns."""
    access = fleet.access_mark()
    slo = dict(fleet_fixtures.fleet()["spec"]["slo"], ttftS="5")
    generation = fleet.apply(slo=slo)
    wait_for("the fleet to be Ready after an SLO edit", lambda: fleet.ready(generation), 120)
    if fleet.access_mark() != access:
        raise Failure(f"an SLO edit ran access again: {access} became {fleet.access_mark()}")
    summary.passed("edit-slo", generation=generation, access_reran=False)

    generation = fleet.apply(until="discover")
    jobs = wait_for(
        "the discovery Jobs to wait for the model volume", lambda: discovery_held(cluster), 180
    )
    no_duplicates(jobs)
    attempt = fleet.stage("discover").get("attempt")
    if fleet.access_mark() != access:
        raise Failure("extending until to discover ran access again")
    summary.passed(
        "until-discover",
        generation=generation,
        discover_attempt=attempt,
        jobs=sorted(f"{job['gate']}@{job['node']}" for job in jobs.values()),
        access_reran=False,
    )

    # An SLO edit reruns nothing up to discover, so the running stage keeps its Jobs.
    generation = fleet.apply(slo=dict(slo, ttftS="6"))
    time.sleep(15)
    kept = gate_jobs(cluster)
    if kept != jobs or fleet.stage("discover").get("attempt") != attempt:
        raise Failure(f"an SLO edit restarted discover: {sorted(jobs)} became {sorted(kept)}")
    fleet.not_stalled()
    summary.passed("edit-slo-while-discover-runs", generation=generation, discover_restarted=False)

    # The checkpoint digest is a discovery input: the running stage is cancelled and runs again.
    tree = hashlib.sha256(b"chart-e2e checkpoint").hexdigest()
    model = dict(fleet_fixtures.fleet()["spec"]["engines"]["model"], treeSha256=tree)
    generation = fleet.apply(**{"engines.model": model})

    def rerun() -> dict[str, dict] | None:
        fleet.not_stalled()
        held = discovery_held(cluster)
        if not held or set(held) & set(jobs):
            return None
        later = fleet.stage("discover").get("attempt")
        return held if isinstance(later, int) and later > attempt else None

    replaced = wait_for("discover to run again with new Jobs", rerun, 180)
    no_duplicates(replaced)
    if fleet.access_mark() != access:
        raise Failure("a checkpoint edit ran access again")
    fleet.not_stalled()
    summary.passed(
        "edit-checkpoint-while-discover-runs",
        generation=generation,
        discover_attempt=fleet.stage("discover").get("attempt"),
        old_jobs_deleted=True,
        access_reran=False,
    )
    return replaced


def restart(cluster: Cluster, fleet: Fleet, jobs: dict[str, dict], summary: Summary) -> None:
    """Delete the controller pod mid-Job; the next controller adopts the running Jobs."""
    old = controller_pod(cluster)
    attempt = fleet.stage("discover").get("attempt")
    started = time.monotonic()
    cluster.kubectl("delete", "pod", old, "--wait=false")
    new = wait_for(
        "a new controller pod",
        lambda: (pod := controller_pod(cluster)) and pod != old and pod,
        120,
    )
    wait_for("the new controller to hold the Lease", lambda: lease_holder(cluster) == new, 90)

    def adopted() -> bool:
        record = fleet.record()
        stage = record.get("stages", {}).get("discover") or {}
        return new in json.dumps(record.get("lock")) and stage.get("status") == "running"

    wait_for("the new controller to resume discover", adopted, 90)
    takeover = round(time.monotonic() - started, 1)
    # Long enough for the adopted attempt to have made a Job again, if it would.
    time.sleep(10)
    kept = gate_jobs(cluster)
    no_duplicates(kept)
    if kept != jobs:
        raise Failure(
            f"the restarted controller changed the Jobs: {sorted(jobs)} became {sorted(kept)}"
        )
    stage = fleet.stage("discover")
    if stage.get("attempt") != attempt:
        raise Failure(
            f"the restarted controller started attempt {stage.get('attempt')}, not {attempt}"
        )
    if set(stage.get("jobs") or []) - set(kept):
        raise Failure(f"the record names Jobs that do not run: {stage.get('jobs')}")
    fleet.not_stalled()
    summary.passed(
        "controller-restart-adopts",
        old=old,
        new=new,
        takeover_s=takeover,
        attempt=attempt,
        jobs=len(kept),
        duplicates=0,
    )


def stop(cluster: Cluster, fleet: Fleet, summary: Summary) -> None:
    """`spec.stopped` deletes the workloads and keeps the volumes; clearing it runs again."""
    access = fleet.access_mark()
    attempt = fleet.stage("discover").get("attempt")
    before = owned(cluster, fleet.uid)
    fleet.apply(stopped=True)
    wait_for("the fleet to report Stopped", lambda: fleet.condition("Stopped") == "True", 120)
    if left := owned(cluster, fleet.uid):
        raise Failure(f"the stopped fleet still owns {sorted(left.values())}")
    # Their pods end after them.
    wait_for("the stopped fleet's pods to end", lambda: not remaining(cluster, before), 120, 2)
    claims = {item["metadata"]["name"] for item in cluster.get("pvc")["items"]}
    kept = {"narwhal-state", *(f"{objects.ROUTER_VOLUME}-{pod}" for pod in objects.ROUTER_PODS)}
    if not kept <= claims:
        raise Failure(f"the stopped fleet lost claims: {sorted(kept - claims)}")
    summary.passed(
        "stopped",
        deleted=len(before),
        kinds=sorted({name.split("/")[0] for name in before.values()}),
        claims_kept=sorted(kept),
    )
    fleet.apply(stopped=False)

    def again() -> dict[str, dict] | None:
        fleet.not_stalled()
        later = fleet.stage("discover").get("attempt")
        if fleet.condition("Stopped") != "False" or not (
            isinstance(later, int) and later > attempt
        ):
            return None
        return discovery_held(cluster)

    jobs = wait_for("discover to run again after the stop", again, 180)
    no_duplicates(jobs)
    if fleet.access_mark() != access:
        raise Failure("clearing spec.stopped ran access again")
    summary.passed(
        "unstopped", discover_attempt=fleet.stage("discover").get("attempt"), access_reran=False
    )


def owned(cluster: Cluster, uid: str) -> dict[str, str]:
    """The objects whose controlling owner is the fleet, or a workload of the fleet's, by uid."""
    items = cluster.get(",".join(FLEET_KINDS))["items"]
    owners = {uid}
    # Pods belong to the fleet through their Job, StatefulSet or ReplicaSet.
    for item in cluster.get("replicasets")["items"] + items:
        if any(ref.get("uid") in owners for ref in item["metadata"].get("ownerReferences") or []):
            owners.add(item["metadata"]["uid"])
    return {
        item["metadata"]["uid"]: f"{item['kind']}/{item['metadata']['name']}"
        for item in items
        if any(ref.get("uid") in owners for ref in item["metadata"].get("ownerReferences") or [])
    }


def remaining(cluster: Cluster, recorded: dict[str, str]) -> list[str]:
    """The recorded objects that still exist, terminating ones included. A pod whose Job is
    gone no longer leads back to the fleet, so it is found by its uid."""
    items = cluster.get(",".join(FLEET_KINDS))["items"]
    return sorted(recorded[i["metadata"]["uid"]] for i in items if i["metadata"]["uid"] in recorded)


def forged(cluster: Cluster, summary: Summary) -> None:
    """A namespace editor's engine pod on the host network is refused by the admission policy."""
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": EDITOR, "namespace": NAMESPACE},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "edit"},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": EDITOR}],
    }
    cluster.apply(binding)
    refusals = {}
    for name in (
        "an engine pod on the host network",
        "an engine pod naming the engine StatefulSet as its owner",
    ):
        case = chart_live.forged(RELEASE)[name]
        document = copy.deepcopy(case.document)
        document["metadata"]["namespace"] = NAMESPACE
        result = subprocess.run(
            [*cluster.kubectl_argv(), "create", "--as", EDITOR, "-f", "-"],
            input=json.dumps(document),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            cluster.kubectl(
                "delete", "pod", document["metadata"]["name"], "--wait=false", check=False
            )
            raise Failure(f"the admission policy admitted {name} from a namespace editor")
        if case.words not in result.stderr:
            raise Failure(f"{name} was refused for another reason: {result.stderr.strip()}")
        refusals[name] = result.stderr.strip().splitlines()[-1][:300]
    cluster.kubectl("delete", "rolebinding", EDITOR)
    summary.passed("forged-engine-pod-refused", editor=EDITOR, refusals=refusals)


def chart_drift(cluster: Cluster, summary: Summary) -> None:
    """The controller changed nothing the chart rendered."""
    import yaml

    text = yaml.safe_dump_all(manifests(cluster))
    diff = subprocess.run(
        [*cluster.kubectl_argv(), "diff", "--server-side", "--force-conflicts", "-f", "-"],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode != 0:
        raise Failure(f"the chart's objects drifted: {(diff.stdout or diff.stderr)[-3000:]}")
    summary.passed("chart-no-drift", objects=len(manifests(cluster)))


def workstation(cluster: Cluster) -> Path:
    """A kubeconfig for the ServiceAccount `WORKSTATION`, bound to the chart's fleet-editor Role
    and `kubernetes_cli.WORKSTATION_RULES` alone."""
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": WORKSTATION, "namespace": NAMESPACE},
    }
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": WORKSTATION, "namespace": NAMESPACE},
        "rules": list(kubernetes_cli.WORKSTATION_RULES),
    }
    subject = [{"kind": "ServiceAccount", "name": WORKSTATION, "namespace": NAMESPACE}]
    bindings = [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role_name},
            "subjects": subject,
        }
        for name, role_name in (
            (WORKSTATION, WORKSTATION),
            (f"{WORKSTATION}-fleet-editor", f"{RELEASE}-fleet-editor"),
        )
    ]
    documents: list[dict[str, Any]] = [account, role, *bindings]
    for document in documents:
        cluster.apply(document)
    token = cluster.kubectl("create", "token", WORKSTATION, "--duration=1h").strip()
    path = cluster.work / "workstation.kubeconfig"
    shutil.copy(cluster.kubeconfig, path)
    path.chmod(0o600)
    config = [str(cluster.tools / "kubectl"), "--kubeconfig", str(path), "config"]
    run([*config, "set-credentials", WORKSTATION, f"--token={token}"], capture=True)
    run(
        [
            *config,
            "set-context",
            WORKSTATION,
            f"--cluster=kind-{cluster.name}",
            f"--user={WORKSTATION}",
            f"--namespace={NAMESPACE}",
        ],
        capture=True,
    )
    return path


def workstation_cli(cluster: Cluster, fleet: Fleet, summary: Summary) -> None:
    """`narwhal deploy plan|run|status|down --target kubernetes` from a workstation that holds
    only its documented role: it follows the fleet kubectl applied without changing its spec,
    copies the evidence through an export Job the controller makes, and cannot create a Job."""
    # Stop the discovery that waits on the missing model directory, then run up to access.
    fleet.apply(stopped=True)
    wait_for("the fleet to report Stopped", lambda: fleet.condition("Stopped") == "True", 120)
    generation = fleet.apply(stopped=False, until="access")
    wait_for("the fleet to be Ready up to access", lambda: fleet.ready(generation), 180)
    spec = fleet.get()["spec"]
    kubeconfig = workstation(cluster)
    kubectl = [str(cluster.tools / "kubectl"), "--kubeconfig", str(kubeconfig)]
    # The role holds no verb on Jobs, so neither the CLI nor anyone with its token makes one.
    can = run(
        [*kubectl, "--context", WORKSTATION, "auth", "can-i", "create", "jobs"],
        capture=True,
        check=False,
    ).strip()
    if can != "no":
        raise Failure(f"the workstation's role may create Jobs: {can}")
    job = pod_job(f"{WORKSTATION}-job", fleet.image, ["true"], {})
    created = subprocess.run(
        [*kubectl, "--context", WORKSTATION, "create", "--dry-run=server", "-f", "-"],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode == 0 or "forbidden" not in created.stderr.lower():
        raise Failure(f"the workstation's role created a Job: {created.stdout}{created.stderr}")
    env = fleet_resource.spec_env(
        fleet.get(), state_claim="narwhal-state", policy=fleet_resource.VolumePolicy()
    )
    # What only the controller sets.
    for name in (
        kubernetes_target.FLEET_UID,
        kubernetes_target.ENGINE_ACCOUNT,
        kubernetes_target.SIDECAR_USER,
    ):
        env.pop(name, None)
    env.update(
        PATH=os.environ.get("PATH", "/usr/bin:/bin"),
        HOME=str(cluster.work),
        KUBECONFIG=str(kubeconfig),
        NARWHAL_KUBECTL=str(cluster.tools / "kubectl"),
        NARWHAL_KUBERNETES_CONTEXT=WORKSTATION,
    )
    directory = cluster.work / "workstation-run"
    narwhal = Path(sys.executable).with_name("narwhal")
    results: dict[str, dict[str, Any]] = {}
    for action, wanted in (("plan", {0}), ("run", {0}), ("status", {0, 3}), ("down", {2})):
        argv = [str(narwhal), "deploy", action, "--target", "kubernetes", "--run", str(directory)]
        if action == "run":
            argv += ["--timeout", "300"]
        done = subprocess.run(
            [*argv, "--format", "json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        try:
            result = json.loads(done.stdout)
        except ValueError:
            raise Failure(f"deploy {action} printed no result: {done.stderr[-2000:]}") from None
        if done.returncode not in wanted:
            raise Failure(
                f"deploy {action} --target kubernetes exited {done.returncode}: "
                f"{json.dumps(result.get('errors'))} {done.stderr[-2000:]}"
            )
        results[action] = {"exit": done.returncode, "data": result.get("data") or {}}
    if spec != fleet.get()["spec"]:
        raise Failure("the workstation changed the fleet's spec")
    managers = results["run"]["data"].get("managers") or []
    if not any(GITOPS_MANAGER in manager for manager in managers) or results["run"]["data"].get(
        "applied"
    ):
        raise Failure(f"run applied a fleet another manager owns: {managers}")
    evidence = results["run"]["data"].get("evidence") or {}
    if not evidence.get("jobs") or not evidence.get("files") or not evidence.get("complete"):
        raise Failure(f"run copied no evidence through an export Job: {evidence}")
    mirrored = sorted(
        str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file()
    )
    if "run.json" not in mirrored or not any(
        n.startswith("stages/access/attempt-") for n in mirrored
    ):
        raise Failure(f"the run directory mirrors no record or access evidence: {mirrored}")
    if any(n.endswith(("run.salt", "credential.json")) for n in mirrored):
        raise Failure(f"an export copied a secret of the run directory: {mirrored}")
    answers = (fleet.get().get("status") or {}).get("exports") or []
    jobs = json.loads(cluster.kubectl("get", "jobs", "-o", "json", "--show-managed-fields"))
    exports = [
        item
        for item in jobs["items"]
        if item["metadata"].get("labels", {}).get(GATE_KIND_LABEL) == "export"
    ]
    if not answers or not exports:
        raise Failure(f"the controller named no export Job: {answers}")
    for item in exports:
        pod = item["spec"]["template"]["spec"]
        mounts = pod["containers"][0]["volumeMounts"]
        # Who wrote the Job itself; the Job controller writes its status.
        writers = {
            entry["manager"]
            for entry in item["metadata"].get("managedFields") or []
            if entry.get("subresource") != "status"
        }
        if (
            pod.get("automountServiceAccountToken") is not False
            or not all(mount.get("readOnly") for mount in mounts)
            or any(mount["subPath"].count("/") < 3 for mount in mounts)
            or writers != {objects.MANAGER}
        ):
            raise Failure(f"export Job {item['metadata']['name']} is not the controller's: {item}")
    summary.passed(
        "workstation-cli",
        account=WORKSTATION,
        can_create_jobs=False,
        exits={action: found["exit"] for action, found in results.items()},
        managers=managers,
        evidence=evidence,
        export_jobs=len(exports),
        mirrored=len(mirrored),
        spec_unchanged=True,
    )


def delete(cluster: Cluster, fleet: Fleet, summary: Summary) -> None:
    """Deleting the fleet leaves its objects to garbage collection."""
    before = owned(cluster, fleet.uid)
    started = time.monotonic()
    cluster.kubectl("delete", "narwhalfleet", fleet_resource.NAME, "--wait=true", "--timeout=60s")
    deleted_s = round(time.monotonic() - started, 1)
    wait_for(
        "garbage collection of the fleet's objects", lambda: not remaining(cluster, before), 180, 2
    )
    claims = {item["metadata"]["name"] for item in cluster.get("pvc")["items"]}
    if "narwhal-state" not in claims:
        raise Failure("deleting the fleet deleted the state claim")
    summary.passed(
        "delete-garbage-collected",
        owned_before=len(before),
        kinds=sorted({name.split("/")[0] for name in before.values()}),
        fleet_deleted_s=deleted_s,
        gc_s=round(time.monotonic() - started, 1),
    )


# The serving path.


def fleet_file(spec: dict, engines: dict) -> dict:
    """The fleet of the direct engines, as `narwhal dev init` writes it for a host."""
    document = {
        "schema": "narwhal.fleet",
        "schema_version": 2,
        "model": spec["model"]["served_name"],
        "hardware": {"accelerator": "cpu", "accelerators_per_engine": 1, "tensor_parallel": 1},
        "engines": fleet_engines(engines),
        "slo": spec["slo"],
        "controller": spec["controller"],
        **({"serving": spec["serving"]} if "serving" in spec else {}),
        "profiles": {"path": "/narwhal-state/profiles.json"},
        # Relative to the router's working directory on its own volume.
        "recovery": {"state_path": "router-state.json"},
        "engine": {"backend": spec["runtime"]["backend"], "first_token_timeout_s": 10.0},
    }
    with tempfile.TemporaryDirectory() as folder:
        trial = Path(folder) / "fleet.json"
        trial.write_text(json.dumps(document))
        load_fleet(trial)
    return document


def pod_job(name: str, image: str, command: list[str], mounts: dict[str, str]) -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "enableServiceLinks": False,
                    "automountServiceAccountToken": False,
                    "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "command": command,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": claim, "mountPath": path} for claim, path in mounts.items()
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": claim, "persistentVolumeClaim": {"claimName": claim}}
                        for claim in mounts
                    ],
                }
            },
        },
    }


def wait_job(cluster: Cluster, name: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = cluster.get("job", name)["status"]
        if status.get("succeeded"):
            return cluster.kubectl("logs", f"job/{name}")
        if status.get("failed"):
            raise Failure(
                f"job {name} failed: {cluster.kubectl('logs', f'job/{name}', check=False)}"
            )
        time.sleep(2)
    raise Failure(f"job {name} did not finish within {timeout_s:g} s")


def router_ready(cluster: Cluster, pod: str) -> int:
    """The HTTP status of a router's /ready, read inside its own pod."""
    script = (
        "import os, urllib.error, urllib.request\n"
        "url = f\"http://{os.environ['NARWHAL_POD_IP']}:8000/ready\"\n"
        "try:\n    print(urllib.request.urlopen(url, timeout=3).status)\n"
        "except urllib.error.HTTPError as exc:\n    print(exc.code)\n"
        "except OSError:\n    print(0)\n"
    )
    output = cluster.kubectl(
        "exec", pod, "-c", "router", "--", "python3", "-c", script, check=False
    )
    return int(output.strip() or 0) if output.strip().isdigit() else 0


# Sent from inside a router pod: the NetworkPolicy admits only HAProxy, the gate Jobs, the other
# router and monitoring to the routers. Prints the HTTP status of each request.
CONTROL_PROBE = """
import json, os, sys, urllib.error, urllib.request
def post(url, headers):
    request = urllib.request.Request(url, b"{}", {"content-type": "application/json", **headers})
    try:
        return urllib.request.urlopen(request, timeout=5).status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0
own = f"http://{os.environ['NARWHAL_POD_IP']}:8000/narwhal/engines"
peer, balancer = sys.argv[1], sys.argv[2]
wrong = {"Authorization": "Bearer " + "0" * 64}
print(json.dumps({"own": post(own, {}), "own_wrong_token": post(own, wrong),
                  "peer": post(peer, {}), "haproxy": post(balancer, {})}))
"""


def control_routes(cluster: Cluster, summary: Summary) -> None:
    """A control route sent to a router without the control token is refused with 401, and
    HAProxy does not forward it at all."""
    domain = f"{objects.ROUTER_SERVICE}.{NAMESPACE}.svc.cluster.local"
    balancer = (
        f"http://{objects.HAPROXY}.{NAMESPACE}.svc.cluster.local:{objects.HAPROXY_PORT}"
        "/narwhal/engines"
    )
    expected = {"own": 401, "own_wrong_token": 401, "peer": 401, "haproxy": 404}
    found = {}
    for pod in objects.ROUTER_PODS:
        [peer] = [other for other in objects.ROUTER_PODS if other != pod]
        output = cluster.kubectl(
            "exec",
            pod,
            "-c",
            objects.ROUTER,
            "--",
            "python3",
            "-I",
            "-c",
            CONTROL_PROBE,
            f"http://{peer}.{domain}:{objects.ROUTER_PORT}/narwhal/engines",
            balancer,
        )
        found[pod] = json.loads(output.strip().splitlines()[-1])
        if found[pod] != expected:
            raise Failure(f"POST /narwhal/engines from {pod} answered {found[pod]}, not {expected}")
    summary.passed("control-routes-need-token", route="POST /narwhal/engines", statuses=found)


def active_router(cluster: Cluster) -> str | None:
    ready = [pod for pod in objects.ROUTER_PODS if router_ready(cluster, pod) == 200]
    return ready[0] if len(ready) == 1 else None


def serving_objects(cluster: Cluster, fleet: Fleet) -> list[dict]:
    """The router pair and HAProxy, as `router-start` builds them for this fleet."""
    spec = fleet.get()["spec"]
    built = objects.Fleet(
        objects.Owner(fleet_resource.NAME, fleet.uid, NAMESPACE),
        narwhal_image=spec["images"]["narwhal"],
        state_claim="narwhal-state",
        haproxy_image=spec["images"]["haproxy"],
        fabric_image=spec["images"]["fabric"],
        control_token=objects.ApiKey(
            ROUTER_TOKEN_ENV, spec["controlTokenSecret"]["name"], spec["controlTokenSecret"]["key"]
        ),
        engine_account=f"{RELEASE}-engine",
        rdma_resource=spec["engines"]["resources"]["rdma"],
        model_volume=fleet_resource.model_volume(spec["engines"]),
        cluster_domain=spec["haproxy"]["clusterDomain"],
        haproxy_replicas=spec["haproxy"]["replicas"],
    )
    settings = objects.Routers.of(spec["routers"], FLEET_FILE)
    return [*objects.routers(built, settings), *objects.haproxy(built)]


def apply_serving(cluster: Cluster, fleet: Fleet) -> None:
    """Send the serving objects as the controller's ServiceAccount, which the admission policy
    trusts with their labels."""
    for item in serving_objects(cluster, fleet):
        cluster.apply(item, as_controller=True)


def serve(
    cluster: Cluster, fleet: Fleet, template: dict, document: dict, reference: str, summary: Summary
) -> None:
    """Direct engines, their profiles, then the router pair and HAProxy in front of them."""
    selector = f"{objects.COMPONENT_LABEL}=direct-engine"
    wait_for(
        "the engine pods",
        lambda: (
            len(cluster.kubectl("get", "pods", "-l", selector, "-o", "name").split()) == ENGINES
        ),
        120,
    )
    cluster.kubectl("wait", "--for=condition=Ready", "pod", "-l", selector, "--timeout=600s")
    profiled = fleet_file(template, document["directEngines"])
    # It stands in for the controller's gate Jobs, which alone may mount the state claim, so
    # it is sent as the controller.
    cluster.kubectl(
        "create",
        "--as",
        CONTROLLER_ACCOUNT,
        "-f",
        "-",
        input=json.dumps(
            pod_job(
                "profile",
                reference,
                [
                    "python3",
                    "-I",
                    "-c",
                    PROFILE_SCRIPT,
                    json.dumps(profiled),
                    json.dumps(template["runtime"]),
                    f"/narwhal-state/{FLEET_FILE}",
                ],
                {"narwhal-state": "/narwhal-state"},
            )
        ),
    )
    profile = wait_job(cluster, "profile", 600).strip()
    apply_serving(cluster, fleet)
    wait_for("a router to hold the router Lease", lambda: active_router(cluster), 240, 2)
    cluster.kubectl("rollout", "status", f"deployment/{objects.HAPROXY}", "--timeout=180s")
    routers = cluster.get("pods", "-l", f"{objects.COMPONENT_LABEL}={objects.ROUTER}")["items"]
    nodes = {pod["metadata"]["name"]: pod["spec"].get("nodeName") for pod in routers}
    if len(set(nodes.values())) != len(objects.ROUTER_PODS):
        raise Failure(f"the routers share a node: {nodes}")
    owners = {
        f"{kind}/{name}": [
            ref["uid"] for ref in cluster.get(kind, name)["metadata"].get("ownerReferences") or []
        ]
        for kind, name in (
            ("statefulset", objects.ROUTER_SET),
            ("deployment", objects.HAPROXY),
            ("service", objects.ROUTER_SERVICE),
            ("service", objects.HAPROXY),
        )
    }
    if any(uids != [fleet.uid] for uids in owners.values()):
        raise Failure(f"the serving objects are not owned by the fleet: {owners}")
    summary.passed(
        "serving-objects",
        engines=ENGINES,
        profile=profile,
        router_nodes=nodes,
        owned_by_fleet=True,
        sent_as=CONTROLLER_ACCOUNT,
    )


def client_status(cluster: Cluster) -> str | None:
    """`succeeded` or `failed` once the client Job ends, otherwise None."""
    status = cluster.get("job", "client")["status"]
    if status.get("succeeded"):
        return "succeeded"
    if status.get("failed"):
        return "failed"
    return None


def failover(cluster: Cluster, model: dict, summary: Summary) -> dict:
    """Stream completions through HAProxy, delete the lease holder's pod, and judge the stream."""
    url = f"http://{objects.HAPROXY}.{NAMESPACE}.svc.cluster.local:{objects.HAPROXY_PORT}/v1/completions"
    duration = STREAM_BEFORE_S + STREAM_AFTER_S
    image = cluster.get("pod", objects.ROUTER_PODS[0])["spec"]["containers"][0]["image"]
    cluster.apply(
        pod_job(
            "client",
            image,
            [
                "python3",
                "-I",
                "-c",
                CLIENT_SCRIPT,
                url,
                model["served_name"],
                "300",
                str(duration),
                str(STREAM_LOOPS),
            ],
            {},
        )
    )
    pod = wait_for(
        "the client pod",
        lambda: cluster.kubectl(
            "get",
            "pods",
            "-l",
            "job-name=client",
            "-o",
            "jsonpath={.items[0].metadata.name}",
            check=False,
        ).strip(),
        120,
    )
    wait_for(
        "the client pod to run",
        lambda: (
            cluster.kubectl(
                "get", "pod", pod, "-o", "jsonpath={.status.phase}", check=False
            ).strip()
            in ("Running", "Succeeded", "Failed")
        ),
        300,
    )
    lines: list[dict] = []
    # Followed only once the container runs; kubectl gives up on a container still waiting.
    log = subprocess.Popen(
        [*cluster.kubectl_argv(), "logs", "-f", pod],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert log.stdout is not None
    opening = log.stdout.readline()
    if not opening:
        raise Failure(f"the client log ended before its first line; kubectl exited {log.wait()}")
    first = json.loads(opening)
    if not first.get("ready"):
        raise Failure(f"no completion through HAProxy: {first}")
    summary.passed(
        "completion", status=first["status"], seconds=round(first["end"] - first["start"], 2)
    )
    holder = active_router(cluster)
    if holder is None:
        raise Failure("no single router answers /ready with 200 before the failover")
    standby = objects.ROUTER_PODS[1] if holder == objects.ROUTER_PODS[0] else objects.ROUTER_PODS[0]
    time.sleep(STREAM_BEFORE_S)
    deleted_at = time.time()
    cluster.kubectl("delete", "pod", holder, "--wait=false")
    done = False
    # The container log carries the client's stderr too, so a line need not be a record.
    unparsed: list[str] = []
    for line in log.stdout:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            unparsed.append(line.rstrip("\n"))
            print(f"client log line that is not JSON: {line.rstrip()!r}", file=sys.stderr)
            continue
        if not isinstance(record, dict):
            unparsed.append(line.rstrip("\n"))
            continue
        if record.get("done"):
            done = True
            break
        lines.append(record)
    following = log.wait(timeout=60)
    # A stream cut off early would be judged on part of its requests.
    status = wait_for("the client Job to finish", lambda: client_status(cluster), 120)
    if not done or following != 0 or status != "succeeded":
        raise Failure(
            f"the client stream is incomplete: done line {'seen' if done else 'missing'}, "
            f"kubectl logs exited {following}, client Job {status}"
        )
    new_holder = wait_for("the standby to hold the lease", lambda: active_router(cluster), 60)
    if new_holder != standby:
        raise Failure(f"{new_holder} holds the lease after deleting {holder}, not {standby}")
    wait_for(
        f"the replaced {holder} to stand by",
        lambda: router_ready(cluster, holder) == 503,
        180,
    )
    failed = [record for record in lines if record["status"] != 200]
    ends = sorted(record["end"] for record in lines)
    after = [end for end in ends if end >= deleted_at]
    # The longest time the stream went without a completion, wherever it fell.
    gap = max((b - a for a, b in itertools.pairwise(ends)), default=float("inf"))
    slowest = max(record["end"] - record["start"] for record in lines)
    # Each router names its slot files, so files of both routers mean both handed KV over.
    files = cluster.node_exec(
        cluster.workers[0], "ls", f"{NODE_VOLUMES}/narwhal-engine-shared"
    ).split()
    routers = {name.split("-")[1] for name in files if name.startswith("narwhal-")}
    result = {
        "requests": len(lines),
        "non_200": len(failed),
        "unparsed": len(unparsed),
        "deleted": holder,
        "new_holder": new_holder,
        "gap_s": round(gap, 3),
        "slowest_s": round(slowest, 3),
        "window_s": TAKEOVER_WINDOW_S,
        "handoff_routers": len(routers),
    }
    if failed:
        raise Failure(f"{len(failed)} of {len(lines)} requests failed: {failed[:5]}")
    # A traceback means a client thread died, and its requests stopped being counted.
    if any(line.startswith("Traceback") for line in unparsed):
        raise Failure(f"the client raised: {unparsed}")
    if not after:
        raise Failure("no request completed after the lease holder's pod was deleted")
    # A request held for the whole window still needs the time it takes to serve.
    if gap > TAKEOVER_WINDOW_S or slowest > TAKEOVER_WINDOW_S + SERVE_ALLOWANCE_S:
        raise Failure(
            f"the takeover stopped completions for {gap:.1f} s (slowest request "
            f"{slowest:.1f} s), past the {TAKEOVER_WINDOW_S:g} s HAProxy holds a request"
        )
    if len(routers) < 2:
        raise Failure(f"the engines' shared directory holds slot files of {len(routers)} router")
    summary.passed("router-failover", **result)
    return result


@contextlib.contextmanager
def narwhal_image() -> Iterator[str]:
    tag = f"narwhal-e2e:{uuid.uuid4().hex[:12]}"
    run(["docker", "build", "-q", "-t", tag, str(ROOT)], capture=True)
    try:
        yield tag
    finally:
        run(["docker", "image", "rm", "-f", tag], capture=True, check=False)


def diagnose(cluster: Cluster) -> None:
    with contextlib.suppress(Failure):
        print(cluster.kubectl("get", "narwhalfleet,pods,jobs", "-o", "wide"), file=sys.stderr)
    with contextlib.suppress(Failure):
        log = cluster.kubectl("logs", f"deployment/{CONTROLLER}", check=False)
        # The controller's own lines, without its HTTP client's.
        lines = [line for line in log.splitlines() if " httpx " not in line]
        print("\n".join(lines[-60:]), file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tools", type=Path, required=True, help="directory with kind, kubectl and helm"
    )
    parser.add_argument("--keep", action="store_true", help="keep the cluster for inspection")
    args = parser.parse_args()
    for name in ("kind", "kubectl", "helm"):
        if not (args.tools / name).is_file():
            raise SystemExit(f"{args.tools / name} is missing; run make chart-e2e-tools")
    template = json.loads(TEMPLATE.read_text())
    model, runtime = template["model"], template["runtime"]
    model_path = fetch_model(model)
    work = Path(tempfile.mkdtemp(prefix="narwhal-chart-e2e-"))
    cluster = Cluster(args.tools.resolve(), work)
    summary = Summary()
    try:
        with narwhal_image() as tag:
            cluster.create(model_path.parent)
            print(json.dumps({"cluster": cluster.name, "nodes": cluster.workers}), flush=True)
            reference = cluster.load_image(tag)
            prepare(cluster)
            document = values(reference, model, runtime)
            install(cluster, document, summary)
            fleet = Fleet(cluster, reference)
            gitops(cluster, fleet, summary)
            serve(cluster, fleet, template, document, reference, summary)
            control_routes(cluster, summary)
            result = failover(cluster, model, summary)
            jobs = edits(cluster, fleet, summary)
            restart(cluster, fleet, jobs, summary)
            stop(cluster, fleet, summary)
            forged(cluster, summary)
            chart_drift(cluster, summary)
            workstation_cli(cluster, fleet, summary)
            # Garbage collection takes the serving objects too.
            apply_serving(cluster, fleet)
            delete(cluster, fleet, summary)
            print(
                json.dumps(
                    {
                        "passed": True,
                        "elapsed_s": summary.elapsed(),
                        "checks": len(summary.checks),
                        "failover": {
                            key: result[key]
                            for key in ("requests", "non_200", "gap_s", "slowest_s")
                        },
                    }
                ),
                flush=True,
            )
    except Failure as error:
        summary.failed(str(error))
        diagnose(cluster)
        return 1
    finally:
        if args.keep:
            print(f"kept cluster {cluster.name}; kubeconfig {cluster.kubeconfig}")
        else:
            cluster.delete()
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
