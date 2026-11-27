"""A stubbed Kubernetes cluster for `narwhal deploy --target kubernetes` tests.

`FakeCluster` stands in for `kubernetes_target.Cluster`. A release upgrade checks its values
against the chart's schema, then creates the engine pods, the router pods and one finished Job
per gate entry, with the log its command would print. A router check fails when no router pod
runs. While the chart's values configure an engine API key, a Job that calls an engine fails
when the chart gives it no key or its role file names none. A pod patch is refused as the API
server refuses it: node affinity only on a pod held at a scheduling gate, and a failed `test`
operation leaves the pod as it was.

`Rig` holds the cluster's state across runs and patches the library functions the SSH tests
also stub: record building, chart values, cache groups, the fabric and the attestation
document.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import re
import tarfile
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from narwhal.deployment import kubernetes_gate, kubernetes_target
from narwhal.deployment.hosts import Host, write_private
from narwhal.deployment.launch_engine.backend import launcher
from narwhal.deployment.role_env import helpers, write_environment
from tests.deployment import chart_schema

NAMESPACE = "fleet"
NODES = {"engine-1": "gpu-a", "engine-2": "gpu-b"}
IMAGE = "registry.example/engine@sha256:" + "e" * 64
NARWHAL_IMAGE = "registry.example/narwhal@sha256:" + "a" * 64
FABRIC_IMAGE = "registry.example/fabric@sha256:" + "b" * 64
TREE = "f" * 64
SECRET = "engine-key-value"
GATES = Path(__file__).resolve().parents[2] / "deploy/helm/narwhal/templates/gates.yaml"
# The gate kinds whose commands call an engine's API.
ENGINE_CALLERS = {"transfer-measure", "finalize-fleet", "profile", "calibrate", "preflight"}


def keyed_kinds() -> set[str]:
    """The gate kinds the chart gives the engine API key, as its gate template lists them."""
    [names] = re.findall(r"has \.kind \(list ([^)]*)\)", GATES.read_text())
    return set(re.findall(r'"([^"]+)"', names))


def environment(root: Path) -> dict[str, str]:
    values = root / "values.json"
    values.write_text(
        json.dumps(
            {
                "images": {
                    "narwhal": NARWHAL_IMAGE,
                    "haproxy": "registry.example/haproxy@sha256:" + "c" * 64,
                    "fabric": FABRIC_IMAGE,
                },
                "state": {"storageClassName": "shared", "size": "10Gi"},
                "lease": {"storageClassName": "shared", "size": "1Gi"},
                "routers": {"fleet": "fleet.json", "storageClassName": "local", "size": "1Gi"},
                "credentials": {
                    "engineApiKey": {"env": "NARWHAL_ENGINE_KEY", "secretName": "k", "key": "v"}
                },
            }
        )
    )
    chart = root / "chart"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("name: narwhal\n")
    return {
        "NARWHAL_KUBERNETES_NAMESPACE": NAMESPACE,
        "NARWHAL_KUBERNETES_VALUES": str(values),
        "NARWHAL_KUBERNETES_CHART": str(chart),
        "NARWHAL_KUBERNETES_GPU_RESOURCE": "gpu.example.com/gpu",
        "NARWHAL_KUBERNETES_MODEL_VOLUME": '{"persistentVolumeClaim": {"claimName": "model"}}',
        "NARWHAL_KUBERNETES_STATE_CLAIM": "narwhal-state",
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


def _pod(name: str, node: str, containers: Sequence[str], **labels: str) -> dict:
    return {
        "metadata": {"name": name, "uid": str(uuid.uuid4()), "labels": dict(labels)},
        "spec": {"nodeName": node, "volumes": []},
        "status": {
            "containerStatuses": [
                {
                    "name": container,
                    "containerID": f"containerd://{uuid.uuid4().hex}",
                    "imageID": IMAGE,
                    "ready": True,
                    "state": {"running": {"startedAt": "2026-11-27T10:00:00Z"}},
                }
                for container in containers
            ]
        },
    }


class Rig:
    """The cluster's state across the runs of one test, and what each Job saw."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = environment(root)
        self.pods: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.logs: dict[str, str] = {}
        self.config_maps: dict[str, dict] = {}
        # Each Job's inputs ConfigMap, kept after the runner deletes it.
        self.inputs: dict[str, dict] = {}
        self.releases: list[dict] = []
        self.kinds: list[str] = []
        self.calls: Counter[str] = Counter()
        self.routes: dict[str, str] = {}
        self.revision = "rev-1"
        self.fail_kind: str | None = None
        self.fail_status = 1
        self.truncate_kind: str | None = None
        # The KV cache layout each engine's live cache resolves.
        self.layouts: dict[str, str] = {}
        # Engine pods a scheduler places off their node, once.
        self.misplace: dict[str, str] = {}
        self.server = "v1.31.0"
        self.current_context = "fleet-admin@cluster-a"
        # Helm refuses the next upgrade that changes the engines, once.
        self.refuse_engines = False
        self.api_server = "https://cluster-a.example:6443"
        # The (verb, resource) pairs the identity may not use.
        self.denied: set[tuple[str, str]] = set()
        # Pods whose scheduling gate removal the connection drops once, after their affinity.
        self.drop_ungate: set[str] = set()
        # Pods another controller adds its own scheduling gate to, ahead of the placement
        # gate, once, between the run's read and its patch.
        self.gate_ahead: set[str] = set()

    def engine_pod(self, role: str) -> dict:
        """A new engine pod: held at the placement gate, so it runs no container yet."""
        pod = _pod(role, "", (), **{"controller-revision-hash": self.revision})
        pod["spec"]["schedulingGates"] = [{"name": kubernetes_target.PLACEMENT_GATE}]
        # A scheduler that ignores the gate would put it here.
        pod["spec"]["nodeName"] = self.misplace.pop(role, "")
        if pod["spec"]["nodeName"]:
            pod["spec"].pop("schedulingGates")
            pod["status"] = _pod(role, "", ("engine", "attestation"))["status"]
        return pod

    def cluster(self, env: Mapping[str, str], logs: Path) -> FakeCluster:
        return FakeCluster(self, env, logs)

    def release(self) -> dict:
        """The values the release runs now."""
        return self.releases[-1] if self.releases else {}

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
                layout = self.layouts.get(_role(entry), "HND")
                files[f"{name}cache-layout.json"] = json.dumps(
                    {"schema_version": 1, "ranks": [{"rank": 0, "kv_cache_layout": layout}]}
                ).encode()
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

    def log(self, entry: Mapping[str, Any]) -> str:
        kind = entry["kind"]
        if kind == "fabric-probe":
            return 'narwhal-interfaces [{"ifname": "fabric0"}]\n'
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
            stack.enter_context(patch.object(target, "Cluster", self.cluster))
            # A pod that never starts fails its wait at once rather than in half an hour.
            stack.enter_context(patch.object(target, "POLL_SECONDS", 0.001))
            stack.enter_context(patch.object(target, "START_SECONDS", 0.05))
            stack.enter_context(patch.object(target, "ROUTER_SECONDS", 0.05))
            stack.enter_context(
                patch.object(target.KubernetesDeployment, "_write_records", _write_records)
            )
            stack.enter_context(patch.object(target.engine_set, "chart_values", self.chart_values))
            stack.enter_context(patch.object(target.cache_groups, "derive", self.derive))
            stack.enter_context(patch.object(target.fabric, "load", self.load_fabric))
            stack.enter_context(patch.object(target.fabric, "budget", self.budget))
            stack.enter_context(patch.object(target.fabric, "qualify_edge", self.qualify_edge))
            stack.enter_context(patch.object(target.fabric, "observe", self.observe))
            stack.enter_context(patch.object(target.AttestationDocument, "load", _document))
            stack.enter_context(patch.object(target, "launch_digest", lambda c, launch: "binding"))
            stack.enter_context(patch.object(target.KubernetesDeployment, "_dashboard", str))
            stack.enter_context(patch.object(target, "plan_launcher", lambda plan: _Registration()))
            stack.enter_context(patch.object(target, "CacheRegistration", _Registration))
            yield

    def chart_values(self, runs: Sequence[Path]) -> dict:
        """Engine set values in the shape the chart's schema requires."""
        digest = hashlib.sha256(b"".join((Path(r) / "launch.json").read_bytes() for r in runs))
        return {
            "schema": "narwhal.engine-set",
            "schemaVersion": 1,
            "count": len(runs),
            "image": IMAGE,
            "gpuResource": "gpu.example.com/gpu",
            "gpus": 1,
            "rdmaResource": "rdma.example.com/hca",
            "modelVolume": {"persistentVolumeClaim": {"claimName": "model"}},
            "stateClaim": "narwhal-state",
            "serviceName": "engines",
            "namespace": NAMESPACE,
            "sidecarImage": NARWHAL_IMAGE,
            "configMaps": [{"metadata": {"name": f"hooks-{digest.hexdigest()[:8]}"}, "data": {}}],
            "access": {
                "serviceAccount": {"metadata": {"name": "narwhal-attestation"}},
                "role": {"metadata": {"name": "narwhal-attestation"}},
                "roleBinding": {"metadata": {"name": "narwhal-attestation"}},
            },
            "podTemplate": {"initContainers": [], "containers": [], "volumes": []},
        }

    def derive(self, launch_config, layouts, transfers, package, out) -> dict:
        self.calls["cache-groups"] += 1
        groups = [{"representative": "engine-1", "roles": ["engine-1", "engine-2"]}]
        document = {"groups": groups}
        write_private(Path(out) / "cache-groups.json", json.dumps(document).encode())
        return document

    def load_fabric(self, hosts, manifest, run, env, groups) -> _Plan:
        return _Plan(hosts, json.loads(Path(groups).read_text())["groups"])

    def budget(self, model, launch, workload, *, runtime_layout, transfer) -> dict:
        self.calls["budget"] += 1
        return {"required_gbps": 1.0}

    def qualify_edge(self, plan, shell, edge, budgets, directory: Path, retained=None):
        self.calls[f"fabric-edge[{edge.name}]"] += 1
        directory.mkdir(mode=0o700, parents=True)
        return [{"sample": "a-to-b", "link_sha256": self.routes.setdefault(edge.name, "route-1")}]

    def observe(self, plan, shell, edge) -> list[_Observation]:
        self.calls[f"observe[{edge.name}]"] += 1
        return [_Observation(_Sample("a-to-b"), self.routes.setdefault(edge.name, "route-1"))]


class FakeCluster:
    """`kubernetes_target.Cluster` over the rig's state."""

    def __init__(self, rig: Rig, env: Mapping[str, str], logs: Path) -> None:
        self.rig = rig
        self.namespace = env.get(kubernetes_target.NAMESPACE, "")
        self.release = env.get(kubernetes_target.RELEASE) or "narwhal"
        self.context = env.get(kubernetes_target.CONTEXT, "")
        self.logs_dir = logs

    def current_context(self) -> str:
        return self.context or self.rig.current_context

    def api_server(self) -> str:
        return self.rig.api_server

    def _refused(self, verb: str, cause: str) -> kubernetes_target.ClusterError:
        return kubernetes_target.ClusterError(["kubectl", verb], 1, cause, self.logs_dir)

    def version(self) -> dict:
        return {"serverVersion": {"gitVersion": self.rig.server}, "clientVersion": {}}

    def helm_version(self) -> str:
        return "v3.16.0"

    def can_i(self, verb: str, resource: str) -> bool:
        self.rig.calls[f"can-i {verb} {resource}"] += 1
        return (verb, resource) not in self.rig.denied

    def release_values(self) -> dict:
        return json.loads(json.dumps(self.rig.release()))

    def kubectl(self, *args: str, data: str | None = None, timeout: float = 120) -> str:
        if args[:2] == ("config", "current-context"):
            return self.rig.current_context + "\n"
        if args[:2] == ("config", "view"):
            return self.rig.api_server
        if args[:2] == ("get", "namespace"):
            return f"namespace/{NAMESPACE}\n"
        if args[:2] == ("get", "node"):
            allocatable = {"gpu.example.com/gpu": "2"}
            return json.dumps({"status": {"allocatable": allocatable}})
        raise AssertionError(f"unexpected kubectl {args}")

    def get(self, kind: str, name: str) -> dict | None:
        rig = self.rig
        if kind == "pod":
            return copy.deepcopy(rig.pods.get(name))
        if kind == "job":
            return rig.jobs.get(name)
        if kind == "statefulset":
            return {"status": {"updateRevision": rig.revision}}
        if kind == "configmap":
            return rig.config_maps.get(name)
        raise AssertionError(kind)

    def items(self, kind: str, selector: str) -> list[dict]:
        if selector.startswith("job-name="):
            pod = self.rig.jobs.get(selector.removeprefix("job-name="), {}).get("pod")
            return [pod] if pod else []
        if selector == "app.kubernetes.io/component=engine":
            return [pod for name, pod in self.rig.pods.items() if name.startswith("engine-")]
        raise AssertionError(selector)

    def apply(self, document: Mapping[str, Any]) -> None:
        self.rig.config_maps[document["metadata"]["name"]] = dict(document)

    def delete(self, kind: str, name: str) -> None:
        self.rig.calls[f"delete {kind}"] += 1
        if kind == "pod":
            self.rig.pods.pop(name)
            self.rig.pods[name] = self.rig.engine_pod(name)
        elif kind == "configmap":
            self.rig.config_maps.pop(name, None)

    def logs(self, job: str) -> str:
        return self.rig.logs[job]

    def patch(self, kind: str, name: str, operations: Sequence[Mapping[str, Any]]) -> None:
        """A JSON patch of a pod, all or nothing. A gated pod takes node affinity it did not
        have; the scheduler places it on that node once no gate holds it."""
        rig = self.rig
        if name in rig.gate_ahead:
            rig.gate_ahead.discard(name)
            rig.pods[name]["spec"]["schedulingGates"].insert(0, {"name": "example.com/other"})
        pod = copy.deepcopy(rig.pods[name])
        spec = pod["spec"]
        for operation in operations:
            op, path = operation["op"], operation["path"]
            gates = spec.get("schedulingGates") or []
            if path == "/spec/affinity":
                if not gates:
                    raise self._refused("patch", "pod updates may not change fields other than ...")
                if (op == "add") == ("affinity" in spec):
                    raise self._refused("patch", f"{op} /spec/affinity: wrong operation")
                before = (spec.get("affinity") or {}).get("nodeAffinity")
                if before is not None and before != operation["value"].get("nodeAffinity"):
                    raise self._refused("patch", "a gated pod's node affinity may only be added")
                spec["affinity"] = copy.deepcopy(operation["value"])
            elif path.startswith("/spec/schedulingGates/"):
                index = int(path.split("/")[3])
                if index >= len(gates):
                    raise self._refused("patch", f"{path}: no such index")
                if op == "test":
                    if gates[index]["name"] != operation["value"]:
                        raise self._refused("patch", f"testing {path} failed")
                elif op == "remove":
                    if name in rig.drop_ungate:
                        rig.drop_ungate.discard(name)
                        raise self._refused("patch", "the connection to the server was lost")
                    del gates[index]
                else:
                    raise AssertionError(operation)
            else:
                raise AssertionError(operation)
        if "schedulingGates" in spec and not spec["schedulingGates"]:
            del spec["schedulingGates"]
            terms = (
                (spec.get("affinity") or {})
                .get("nodeAffinity", {})
                .get("requiredDuringSchedulingIgnoredDuringExecution", {})
                .get("nodeSelectorTerms", [])
            )
            # Unpinned, the scheduler takes whichever node fits.
            spec["nodeName"] = terms[0]["matchFields"][0]["values"][0] if terms else "gpu-any"
            pod["status"] = _pod(name, "", ("engine", "attestation"))["status"]
        rig.pods[name] = pod
        rig.calls[f"pinned {name}"] += 1

    def upgrade(self, chart: Path, values: Mapping[str, Any]) -> None:
        chart_schema.check(values)
        rig = self.rig
        if rig.refuse_engines and values.get("engines") != rig.release().get("engines"):
            rig.refuse_engines = False
            raise self._refused("upgrade", "UPGRADE FAILED: an immutable field changed")
        rig.releases.append(json.loads(json.dumps(values)))
        engines = values.get("engines") or {}
        for n in range(1, int(engines.get("count", 0)) + 1):
            role = f"engine-{n}"
            if role not in rig.pods:
                rig.pods[role] = rig.engine_pod(role)
        if not engines:
            for role in [name for name in rig.pods if name.startswith("engine-")]:
                del rig.pods[role]
        if values["routers"]["enabled"]:
            for name in kubernetes_target.ROUTERS:
                rig.pods.setdefault(name, _pod(name, "cpu-a", ("router",)))
        else:
            for name in kubernetes_target.ROUTERS:
                rig.pods.pop(name, None)
        routers = all(name in rig.pods for name in kubernetes_target.ROUTERS)
        keyed = keyed_kinds() if (values.get("credentials") or {}).get("engineApiKey") else None
        for entry in values["gates"]:
            if entry["name"] in rig.jobs:
                continue
            rig.kinds.append(entry["kind"])
            if "inputs" in entry:
                rig.inputs[entry["name"]] = rig.config_maps[entry["inputs"]]
            unreachable = entry["kind"] == "router-check" and not routers
            unauthorized = keyed is not None and not self._keyed(entry, keyed)
            failed = entry["kind"] == rig.fail_kind or unreachable or unauthorized
            status = rig.fail_status if failed else 0
            pod = {
                "metadata": {"name": entry["name"] + "-pod", "uid": str(uuid.uuid4())},
                "status": {
                    "containerStatuses": [
                        {
                            "name": "gate",
                            "imageID": entry.get("image", NARWHAL_IMAGE),
                            "state": {"terminated": {"exitCode": status}},
                        }
                    ]
                },
            }
            condition = "Failed" if failed else "Complete"
            rig.jobs[entry["name"]] = {
                "status": {"conditions": [{"type": condition, "status": "True"}]},
                "pod": pod,
                "entry": dict(entry),
            }
            if unauthorized:
                rig.logs[entry["name"]] = "narwhal: the engine answered 401 Unauthorized\n"
            elif unreachable:
                rig.logs[entry["name"]] = "narwhal-host: no router answered at haproxy:8000\n"
            elif failed:
                rig.logs[entry["name"]] = "narwhal-gate: the check rejected the engine\n"
            else:
                rig.logs[entry["name"]] = rig.log(entry)

    def _keyed(self, entry: Mapping[str, Any], keyed: set[str]) -> bool:
        """Whether a Job that needs the engine API key has it: from the chart, and through
        its role file to the variable the engine tools read."""
        config_map = self.rig.config_maps[entry["inputs"]] if "inputs" in entry else {}
        data = config_map.get("binaryData", {})
        if kubernetes_gate.INPUTS in data:
            files = unpack(config_map)
        else:
            files = {name: base64.b64decode(value) for name, value in data.items()}
        role_files = [data for name, data in files.items() if Path(name).name.startswith(".env")]
        needs = entry["kind"] in ENGINE_CALLERS or any(
            b"${NARWHAL_ENGINE_KEY:?" in data for data in role_files
        )
        if not needs:
            return True
        exported = all(
            b'NARWHAL_ENGINE_API_KEY="${NARWHAL_ENGINE_KEY:?' in data for data in role_files
        )
        return entry["kind"] in keyed and exported


def _write_records(self: kubernetes_target.KubernetesDeployment, context, observations) -> None:
    config = context.directory / "config"
    config.mkdir(mode=0o700)
    package = context.directory / "package"
    for role, node in NODES.items():
        folder = package / node
        folder.mkdir(mode=0o700, parents=True)
        write_environment(
            folder / f".env.{role}",
            {"NARWHAL_ROLE": role, "NARWHAL_ENGINE_KEY": SECRET, "NARWHAL_ENGINE_API_KEY": SECRET},
        )
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


def _role(entry: Mapping[str, Any]) -> str:
    return next(role for role in NODES if role in entry["name"])


class _Registration:
    """A launcher that registers the layout its live cache resolved."""

    def cache_layout(self, source: Path) -> str:
        return str(json.loads(Path(source).read_text())["ranks"][0]["kv_cache_layout"])


class _Plan:
    def __init__(self, hosts: list[Host], groups: list[dict]) -> None:
        self.hosts = hosts
        self.groups = [_Group(tuple(group["roles"])) for group in groups]
        self.engines = {role: _Engine(Path(__file__)) for host in hosts for role in host.roles}
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
