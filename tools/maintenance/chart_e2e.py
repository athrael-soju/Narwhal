"""Run the Helm chart on a local kind cluster with CPU engines, and fail over its router pair.

`make chart-e2e` runs this. It creates a one-node kind cluster, installs the chart with two
llama.cpp engines that start directly from their image, and checks two things:

- a completion through the HAProxy Service reaches the router pair and the engines;
- under a steady stream of completions, deleting the router pod that holds the lease moves the
  lease to the standby, every request succeeds, and no request waits longer than the 10 seconds
  HAProxy holds a request while no router is in rotation (docs/operate/01-Start-Routers.md).

The test stands in for the gate Jobs: once the engines serve, one Job measures their nominal
profiles as `narwhal dev up` does and writes the fleet file to the state volume. The cluster,
its volumes and the Narwhal image it builds are removed at the end, unless `--keep` is given.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from narwhal.config.loading import load as load_fleet
from narwhal.deployment.direct_engine_set import direct_values, fleet_engines

CHART = ROOT / "deploy" / "helm" / "narwhal"
NAMESPACE = "narwhal-e2e"
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
ENGINES = 2
ENGINE_CPUS = 4
# HAProxy holds a request for up to 10 one-second retries while no router is in rotation.
TAKEOVER_WINDOW_S = 10.0
# Seconds one completion of the stream may take once a router serves it.
SERVE_ALLOWANCE_S = 5.0
STREAM_LOOPS = 2
STREAM_BEFORE_S = 15.0
STREAM_AFTER_S = 30.0
# The node's directories behind the ReadWriteMany volumes.
VOLUMES = {"narwhal-state": "1Gi", "narwhal-lease": "64Mi", "narwhal-engine-shared": "1Gi"}
NODE_VOLUMES = "/var/lib/narwhal-e2e"
STORAGE_CLASS = "narwhal-e2e"
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


class Cluster:
    """One kind cluster and the kubectl and helm that reach it."""

    def __init__(self, tools: Path, work: Path) -> None:
        self.tools = tools
        self.name = f"narwhal-e2e-{uuid.uuid4().hex[:8]}"
        self.node = f"{self.name}-control-plane"
        self.kubeconfig = work / "kubeconfig"
        self.work = work

    def kubectl(self, *args: str, capture: bool = True, check: bool = True, **kw: Any) -> str:
        return run(
            [
                str(self.tools / "kubectl"),
                "--kubeconfig",
                str(self.kubeconfig),
                "-n",
                NAMESPACE,
                *list(args),
            ],
            capture=capture,
            check=check,
            **kw,
        )

    def apply(self, document: dict) -> None:
        self.kubectl("apply", "-f", "-", input=json.dumps(document))

    def node_exec(self, *args: str) -> str:
        return run(["docker", "exec", self.node, *args], capture=True)

    def create(self, models: Path) -> None:
        config = {
            "kind": "Cluster",
            "apiVersion": "kind.x-k8s.io/v1alpha4",
            "nodes": [
                {
                    "role": "control-plane",
                    "extraMounts": [
                        {
                            "hostPath": str(models),
                            "containerPath": "/narwhal-models",
                            "readOnly": True,
                        }
                    ],
                }
            ],
        }
        path = self.work / "kind.json"
        path.write_text(json.dumps(config))
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
            ]
        )

    def delete(self) -> None:
        run([str(self.tools / "kind"), "delete", "cluster", "--name", self.name], check=False)

    def load_image(self, tag: str) -> str:
        """Load a local image into the node and return its reference by digest."""
        run([str(self.tools / "kind"), "load", "docker-image", tag, "--name", self.name])
        name = "docker.io/library/" + tag if "/" not in tag.split(":")[0] else tag
        listing = self.node_exec("ctr", "-n", "k8s.io", "images", "ls")
        for line in listing.splitlines():
            fields = line.split()
            if fields and fields[0] == name:
                digest = fields[2]
                break
        else:
            raise Failure(f"the node holds no image {name} after kind load")
        reference = name.rsplit(":", 1)[0] + "@" + digest
        # The kubelet finds an image by the reference a pod names.
        self.node_exec("ctr", "-n", "k8s.io", "images", "tag", name, reference)
        return reference


def volumes(cluster: Cluster) -> None:
    """ReadWriteMany volumes on the node's own disk, one per claim the chart makes."""
    for name in VOLUMES:
        directory = f"{NODE_VOLUMES}/{name}"
        cluster.node_exec("mkdir", "-p", directory)
        # The routers and the profiling Job run as the image's unprivileged user.
        cluster.node_exec("chmod", "0777", directory)
    for name, size in VOLUMES.items():
        cluster.apply(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": f"{NAMESPACE}-{name}"},
                "spec": {
                    "capacity": {"storage": size},
                    "accessModes": ["ReadWriteMany"],
                    "persistentVolumeReclaimPolicy": "Retain",
                    "storageClassName": STORAGE_CLASS,
                    "claimRef": {"namespace": NAMESPACE, "name": name},
                    "hostPath": {"path": f"{NODE_VOLUMES}/{name}", "type": "Directory"},
                },
            }
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
        model_volume={"hostPath": {"path": "/narwhal-models", "type": "Directory"}},
        shared={"storageClassName": STORAGE_CLASS, "size": VOLUMES["narwhal-engine-shared"]},
        namespace=NAMESPACE,
    )
    return {
        "images": {"narwhal": narwhal_image, "haproxy": HAPROXY_IMAGE},
        "directEngines": engines,
        "state": {"storageClassName": STORAGE_CLASS, "size": VOLUMES["narwhal-state"]},
        "lease": {"storageClassName": STORAGE_CLASS, "size": VOLUMES["narwhal-lease"]},
        # kind's default StorageClass gives each router its own volume.
        "routers": {"fleet": "fleet.json", "storageClassName": "standard", "size": "64Mi"},
    }


def fleet(spec: dict, engines: dict) -> dict:
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
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "command": command,
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
        status = json.loads(cluster.kubectl("get", "job", name, "-o", "json"))["status"]
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


def wait_for(what: str, condition: Any, timeout_s: float, interval_s: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval_s)
    raise Failure(f"timed out after {timeout_s:g} s waiting for {what}")


def active_router(cluster: Cluster) -> str | None:
    ready = [pod for pod in ("router-0", "router-1") if router_ready(cluster, pod) == 200]
    return ready[0] if len(ready) == 1 else None


@contextlib.contextmanager
def narwhal_image() -> Iterator[str]:
    tag = f"narwhal-e2e:{uuid.uuid4().hex[:12]}"
    run(["docker", "build", "-q", "-t", tag, str(ROOT)], capture=True)
    try:
        yield tag
    finally:
        run(["docker", "image", "rm", "-f", tag], capture=True, check=False)


def failover(cluster: Cluster, model: dict) -> dict:
    """Stream completions through HAProxy, delete the lease holder's pod, and judge the stream."""
    url = f"http://haproxy.{NAMESPACE}.svc.cluster.local:8000/v1/completions"
    duration = STREAM_BEFORE_S + STREAM_AFTER_S
    image = json.loads(cluster.kubectl("get", "pod", "router-0", "-o", "json"))["spec"][
        "containers"
    ][0]["image"]
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
    lines: list[dict] = []
    log = subprocess.Popen(
        [
            str(cluster.tools / "kubectl"),
            "--kubeconfig",
            str(cluster.kubeconfig),
            "-n",
            NAMESPACE,
            "logs",
            "-f",
            pod,
        ],
        stdout=subprocess.PIPE,
        text=True,
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
    assert log.stdout is not None
    first = json.loads(log.stdout.readline())
    if not first.get("ready"):
        raise Failure(f"no completion through HAProxy: {first}")
    print(f"routed completion through HAProxy in {first['end'] - first['start']:.2f} s")
    holder = active_router(cluster)
    if holder is None:
        raise Failure("no single router answers /ready with 200 before the failover")
    standby = "router-1" if holder == "router-0" else "router-0"
    time.sleep(STREAM_BEFORE_S)
    deleted_at = time.time()
    cluster.kubectl("delete", "pod", holder, "--wait=false")
    print(f"deleted {holder}, the lease holder")
    for line in log.stdout:
        record = json.loads(line)
        if record.get("done"):
            break
        lines.append(record)
    log.wait(timeout=60)
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
    files = cluster.node_exec("ls", f"{NODE_VOLUMES}/narwhal-engine-shared").split()
    routers = {name.split("-")[1] for name in files if name.startswith("narwhal-")}
    result = {
        "requests": len(lines),
        "failed": failed,
        "deleted": holder,
        "new_holder": new_holder,
        "gap_s": round(gap, 3),
        "slowest_s": round(slowest, 3),
        "window_s": TAKEOVER_WINDOW_S,
        "handoff_routers": len(routers),
    }
    print(json.dumps({key: value for key, value in result.items() if key != "failed"}))
    if failed:
        raise Failure(f"{len(failed)} of {len(lines)} requests failed: {failed[:5]}")
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
    return result


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
    spec = json.loads(TEMPLATE.read_text())
    model, runtime = spec["model"], spec["runtime"]
    model_path = fetch_model(model)
    work = Path(tempfile.mkdtemp(prefix="narwhal-chart-e2e-"))
    cluster = Cluster(args.tools, work)
    started = time.monotonic()
    try:
        with narwhal_image() as tag:
            cluster.create(model_path.parent)
            run(
                [
                    str(args.tools / "kubectl"),
                    "--kubeconfig",
                    str(cluster.kubeconfig),
                    "create",
                    "namespace",
                    NAMESPACE,
                ],
                capture=True,
            )
            reference = cluster.load_image(tag)
            volumes(cluster)
            document = values(reference, model, runtime)
            values_path = work / "values.json"
            values_path.write_text(json.dumps(document))
            run(
                [
                    str(args.tools / "helm"),
                    "install",
                    "narwhal",
                    str(CHART),
                    "--kubeconfig",
                    str(cluster.kubeconfig),
                    "--namespace",
                    NAMESPACE,
                    "-f",
                    str(values_path),
                ],
                capture=True,
            )
            # An OnDelete StatefulSet has no rollout status; its pods report readiness.
            selector = "app.kubernetes.io/component=engine"
            wait_for(
                "the engine pods",
                lambda: (
                    len(cluster.kubectl("get", "pods", "-l", selector, "-o", "name").split())
                    == ENGINES
                ),
                120,
            )
            cluster.kubectl(
                "wait", "--for=condition=Ready", "pod", "-l", selector, "--timeout=600s"
            )
            print("engines ready")
            # The routers wait, restarting, for the fleet file the profiling Job writes last.
            profiled = fleet(spec, document["directEngines"])
            cluster.apply(
                pod_job(
                    "profile",
                    reference,
                    [
                        "python3",
                        "-I",
                        "-c",
                        PROFILE_SCRIPT,
                        json.dumps(profiled),
                        json.dumps(runtime),
                        "/narwhal-state/fleet.json",
                    ],
                    {"narwhal-state": "/narwhal-state"},
                )
            )
            print(wait_job(cluster, "profile", 600).strip())
            # New router pods start at once instead of after their restart backoff.
            cluster.kubectl("delete", "pod", "router-0", "router-1", "--wait=true")
            wait_for("a router to hold the lease", lambda: active_router(cluster), 180)
            result = failover(cluster, model)
            result["elapsed_s"] = round(time.monotonic() - started, 1)
            print(json.dumps({"passed": True, "elapsed_s": result["elapsed_s"]}))
    except Failure as error:
        print(f"chart-e2e: {error}", file=sys.stderr)
        with contextlib.suppress(Failure):
            print(cluster.kubectl("get", "pods", "-o", "wide"), file=sys.stderr)
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
