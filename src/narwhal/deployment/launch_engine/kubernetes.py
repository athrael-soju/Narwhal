"""Render a Kubernetes launch plan into its engine pod, and check the pod's records.

`prepare --backend kubernetes` writes `kubernetes.json`: two immutable ConfigMaps, one with the
launch's hook files and one with the pod steps in pod.py, and the engine pod's specification.
The pod runs every step in the unmodified engine image. An init container hashes the served
model, a second one runs the runtime probe, and the engine container starts once both pass.
`check` reads the records they leave on the state volume and writes checked.json.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .. import stages
from . import checkpoint as checkpoint_module
from . import pod
from .backend import plan_launcher
from .check import check_context, finish_check, require_checked
from .checkpoint import json_digest
from .plan import (
    HOOK_MOUNT,
    KV_EVENTS_MOUNT,
    MODEL_MOUNT,
    POD_ADDRESS,
    POD_ADDRESS_ENV,
    read_env,
)
from .runtime import digest, write_once, write_private

GPU_RESOURCE_ENV = "NARWHAL_KUBERNETES_GPU_RESOURCE"
RDMA_RESOURCE_ENV = "NARWHAL_KUBERNETES_RDMA_RESOURCE"
MODEL_VOLUME_ENV = "NARWHAL_KUBERNETES_MODEL_VOLUME"
STATE_CLAIM_ENV = "NARWHAL_KUBERNETES_STATE_CLAIM"
API_KEY_SECRET_ENV = "NARWHAL_KUBERNETES_API_KEY_SECRET"
# Volume sources that serve a staged checkpoint to the pod.
MODEL_VOLUME_SOURCES = ("persistentVolumeClaim", "hostPath", "nfs", "csi")
# The pod steps, by file name in the pod-step ConfigMap.
POD_FILES = {"pod.py": Path(pod.__file__), "checkpoint.py": Path(checkpoint_module.__file__)}
RUNTIME_CHECK_STAGE = "pod-runtime-check"
MANIFESTS = "kubernetes.json"
# The pod's records, copied into the launch directory by pod UID.
POD_RECORDS = "pods"
DOWNWARD_ENV = (
    (POD_ADDRESS_ENV, "status.podIP"),
    (pod.POD_UID_ENV, "metadata.uid"),
    (pod.NODE_NAME_ENV, "spec.nodeName"),
)
# Runs first in each pod container. It checks every delivered file against the SHA-256 the pod
# specification pins, then runs the pod steps. Isolated mode keeps the hook directory, which
# the engine's PYTHONPATH names, off its import path.
BOOTSTRAP = f"""import hashlib, json, os, runpy, sys
pins = json.loads(sys.argv[1])
for root, files in sorted(pins.items()):
    present = sorted(name for name in os.listdir(root) if not name.startswith('..'))
    if present != sorted(files):
        sys.exit(f'narwhal: {{root}} holds {{present}}, not {{sorted(files)}}')
    for name, pinned in sorted(files.items()):
        path = os.path.join(root, name)
        with open(path, 'rb') as stream:
            if hashlib.sha256(stream.read()).hexdigest() != pinned:
                sys.exit(f'narwhal: {{path}} differs from its pinned SHA-256')
sys.argv = [{str(pod.TOOLS / "pod.py")!r}, *sys.argv[2:]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
DNS_LABEL = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
DNS_SUBDOMAIN = rf"(?=.{{1,253}}$){DNS_LABEL}(?:\.{DNS_LABEL})*"
RESOURCE_NAME = r"[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?"
UID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def resource_name(env: dict[str, str], variable: str) -> str:
    """Return an extended resource name, `<domain>/<name>` outside the kubernetes.io domain."""
    value = env.get(variable, "")
    domain, _, name = value.partition("/")
    if (
        not re.fullmatch(DNS_SUBDOMAIN, domain)
        or not re.fullmatch(RESOURCE_NAME, name)
        or domain in ("kubernetes.io", "k8s.io")
        or domain.endswith((".kubernetes.io", ".k8s.io"))
    ):
        raise ValueError(
            f"set {variable} to the device plugin's extended resource name, <domain>/<name>"
        )
    return value


def model_volume(env: dict[str, str]) -> dict:
    try:
        source = json.loads(env.get(MODEL_VOLUME_ENV, ""))
    except ValueError:
        source = None
    if (
        not isinstance(source, dict)
        or len(source) != 1
        or next(iter(source)) not in MODEL_VOLUME_SOURCES
        or not isinstance(next(iter(source.values())), dict)
    ):
        raise ValueError(
            f"set {MODEL_VOLUME_ENV} to a JSON volume source with one of "
            f"{', '.join(MODEL_VOLUME_SOURCES)}"
        )
    return source


def api_key_secret(env: dict[str, str]) -> dict | None:
    reference = env.get(API_KEY_SECRET_ENV, "")
    if bool(reference) != bool(env.get("NARWHAL_ENGINE_API_KEY")):
        raise ValueError(
            f"set {API_KEY_SECRET_ENV} to the Secret that holds NARWHAL_ENGINE_API_KEY, "
            "and leave both unset for an engine without a credential"
        )
    if not reference:
        return None
    name, _, key = reference.partition("/")
    if not re.fullmatch(DNS_SUBDOMAIN, name) or not re.fullmatch(r"[-._A-Za-z0-9]{1,253}", key):
        raise ValueError(f"set {API_KEY_SECRET_ENV} to <secret name>/<key>")
    return {"name": name, "key": key}


def pod_settings(env: dict[str, str], record: dict) -> dict:
    """Return the plan's pod section from the deployment inputs and the launch record."""
    gpu, rdma = resource_name(env, GPU_RESOURCE_ENV), resource_name(env, RDMA_RESOURCE_ENV)
    if gpu == rdma:
        raise ValueError(f"{GPU_RESOURCE_ENV} and {RDMA_RESOURCE_ENV} must name two resources")
    claim = env.get(STATE_CLAIM_ENV, "")
    if not re.fullmatch(DNS_SUBDOMAIN, claim):
        raise ValueError(f"set {STATE_CLAIM_ENV} to the state PersistentVolumeClaim's name")
    secret = api_key_secret(env)
    budget = stages.seconds(
        "NARWHAL_STAGE_" + RUNTIME_CHECK_STAGE.upper().replace("-", "_") + "_TIMEOUT_SECONDS",
        stages.seconds("NARWHAL_STAGE_TIMEOUT_SECONDS", 300),
    )
    return {
        "gpu_resource": gpu,
        "gpus": len(record["gpu_ids"]),
        "rdma_resource": rdma,
        "model_volume": model_volume(env),
        "state_claim": claim,
        **({"api_key_secret": secret} if secret is not None else {}),
        # Docker's ROCm launch runs the engine without a seccomp filter, as this pod does.
        "seccomp_unconfined": record["gpu_visibility_env"] == "ROCR_VISIBLE_DEVICES",
        "runtime_check_seconds": budget,
        "pod_files_sha256": {name: digest(path) for name, path in POD_FILES.items()},
    }


def runtime_probe(run: Path, plan: dict, environment: dict[str, str]) -> list[str]:
    """Return the probe arguments the runtime-check init container runs.

    The model's custom-code check reads the prepared copy here, which the pod's checkpoint
    record later shows to equal the files the pod serves.
    """
    context = check_context(run, plan, MANIFESTS, environment)
    return plan_launcher(plan).runtime_probe(context)


def delivered_files(plan: dict, plan_sha256: str) -> dict[str, dict[str, str]]:
    """Return the SHA-256 of each file the pod receives, by mount path, from the plan."""
    hooks = {"launch.json": plan_sha256, "launch_engine.py": plan["launcher_sha256"]}
    if "cache_capture_sha256" in plan:
        hooks["sitecustomize.py"] = plan["cache_capture_sha256"]
    for name, content in plan.get("config_files", {}).items():
        hooks[name] = hashlib.sha256(content.encode()).hexdigest()
    return {
        HOOK_MOUNT: dict(sorted(hooks.items())),
        str(pod.TOOLS): dict(sorted(plan["kubernetes"]["pod_files_sha256"].items())),
    }


def literal(value: str) -> str:
    """Escape a value against Kubernetes variable expansion, except the pod address reference."""
    return POD_ADDRESS.join(part.replace("$", "$$") for part in value.split(POD_ADDRESS))


def config_map(name: str, data: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name},
        "immutable": True,
        "data": data,
    }


def pod_env(run: Path, plan: dict) -> list[dict]:
    """Return the engine environment, with the downward API values its references use first."""
    settings = plan["kubernetes"]
    secret = settings.get("api_key_secret")
    credential = plan_launcher(plan).api_key_env
    env: list[dict] = [
        {"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}
        for name, field in DOWNWARD_ENV
    ]
    for name, value in sorted(read_env(run / "container.env").items()):
        if name == credential:
            if secret is None:
                raise ValueError(f"the engine credential needs {API_KEY_SECRET_ENV}")
            env.append({"name": name, "valueFrom": {"secretKeyRef": dict(secret)}})
        else:
            env.append({"name": name, "value": literal(value)})
    return env


def pod_spec(run: Path, plan: dict, probe: list[str], maps: tuple[str, str]) -> dict:
    settings = plan["kubernetes"]
    pins = json.dumps(delivered_files(plan, digest(run / "launch.json")), sort_keys=True)

    def command(step: str, *arguments: str) -> list[str]:
        return [literal(part) for part in ("python3", "-I", "-c", BOOTSTRAP, pins, step)] + [
            literal(argument) for argument in arguments
        ]

    def state(read_only: bool) -> dict:
        return {
            "name": "narwhal-state",
            "mountPath": str(pod.STATE),
            "subPathExpr": f"engines/{plan['role']}/$({pod.POD_UID_ENV})",
            **({"readOnly": True} if read_only else {}),
        }

    delivered = [
        {"name": "narwhal-hooks", "mountPath": HOOK_MOUNT, "readOnly": True},
        {"name": "narwhal-pod", "mountPath": str(pod.TOOLS), "readOnly": True},
        {"name": "model", "mountPath": MODEL_MOUNT, "readOnly": True},
    ]
    serving = [
        *delivered,
        {"name": "cache", "mountPath": "/root/.cache"},
        {"name": "shm", "mountPath": "/dev/shm"},
        *(
            [{"name": "kv-events", "mountPath": KV_EVENTS_MOUNT}]
            if plan["kv_events"] is not None
            else []
        ),
    ]
    env = pod_env(run, plan)
    resources = {
        "limits": {settings["gpu_resource"]: str(settings["gpus"]), settings["rdma_resource"]: "1"}
    }
    security = (
        {"securityContext": {"seccompProfile": {"type": "Unconfined"}}}
        if settings["seccomp_unconfined"]
        else {}
    )
    hooks_map, steps_map = maps
    return {
        "hostNetwork": True,
        "dnsPolicy": "ClusterFirstWithHostNet",
        # Service links would add variables, such as one per Service port, to the engine's own.
        "enableServiceLinks": False,
        "initContainers": [
            {
                "name": "checkpoint",
                "image": plan["image"],
                "command": command("checkpoint"),
                "env": env[: len(DOWNWARD_ENV)],
                "volumeMounts": [*delivered, state(False)],
            },
            {
                "name": "runtime-check",
                "image": plan["image"],
                "command": command(
                    "runtime-check", f"{settings['runtime_check_seconds']:g}", *probe
                ),
                "env": env,
                "resources": resources,
                "volumeMounts": [*serving, state(False)],
                **security,
            },
        ],
        "containers": [
            {
                "name": "engine",
                "image": plan["image"],
                "command": command("serve", *plan["args"]),
                "env": env,
                "ports": [
                    {"name": "http", "containerPort": urlsplit(plan["endpoint"]).port},
                    {"name": "kv-side-channel", "containerPort": plan["side_channel_port"]},
                ],
                "resources": resources,
                "volumeMounts": [*serving, state(True)],
                **security,
            }
        ],
        "volumes": [
            {"name": "narwhal-hooks", "configMap": {"name": hooks_map, "defaultMode": 0o444}},
            {"name": "narwhal-pod", "configMap": {"name": steps_map, "defaultMode": 0o444}},
            {"name": "model", **settings["model_volume"]},
            {
                "name": "narwhal-state",
                "persistentVolumeClaim": {"claimName": settings["state_claim"]},
            },
            {"name": "cache", "emptyDir": {}},
            {"name": "shm", "emptyDir": {"medium": "Memory"}},
            *([{"name": "kv-events", "emptyDir": {}}] if plan["kv_events"] is not None else []),
        ],
    }


def write_manifests(run: Path, plan: dict, probe: list[str]) -> None:
    """Write kubernetes.json from a prepared launch directory."""
    plan_sha256 = digest(run / "launch.json")
    hooks = {path.name: path.read_text() for path in sorted((run / "hook").iterdir())}
    steps = {name: path.read_text() for name, path in sorted(POD_FILES.items())}
    pinned = delivered_files(plan, plan_sha256)
    for root, data in ((HOOK_MOUNT, hooks), (str(pod.TOOLS), steps)):
        observed = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in data.items()}
        if observed != pinned[root]:
            raise ValueError(f"the files for {root} differ from the launch plan")
    # The pod-step ConfigMap's name follows its content, so engines of one release share it.
    maps = (f"{plan['name']}-hooks", f"narwhal-pod-{json_digest(steps)[:12]}")
    manifests = {
        "plan_sha256": plan_sha256,
        "configMaps": [config_map(maps[0], hooks), config_map(maps[1], steps)],
        "podSpec": pod_spec(run, plan, probe, maps),
    }
    write_private(run / MANIFESTS, json.dumps(manifests, indent=2) + "\n")


def read_record(path: Path) -> tuple[str, dict]:
    text = path.read_text()
    record = json.loads(text)
    if not isinstance(record, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return text, record


def check(run: Path, plan: dict, evidence: Path) -> None:
    """Check an engine pod's records against its plan, and write checked.json.

    `evidence` holds the records of one pod, as its directory on the state volume holds them.
    A failed init container runs again in the same pod and replaces its record, so the check
    keeps each record it reads under pods/<pod UID>/, named by its SHA-256.
    """
    if (run / "checked.json").exists():
        require_checked(run, plan)
    names = (pod.CHECKPOINT_RECORD, pod.RUNTIME_RECORD)
    read = {name: read_record(evidence / name) for name in names}
    records = {name: record for name, (_, record) in read.items()}
    pods = {record.get("pod_uid") for record in records.values()}
    uid = next(iter(pods))
    if len(pods) != 1 or not isinstance(uid, str) or not re.fullmatch(UID, uid):
        raise ValueError(f"the records in {evidence} come from more than one pod")
    directory = run / POD_RECORDS / uid
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    kept = {}
    for name, (text, _) in read.items():
        stem = name.removesuffix(".json")
        kept[name] = directory / f"{stem}-{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
        write_once(kept[name], text, f"{kept[name]} differs from its own digest")
    plan_sha256 = digest(run / "launch.json")
    files = delivered_files(plan, plan_sha256)
    for name, record in records.items():
        where = f"inspect {kept[name]}"
        if record.get("plan_sha256") != plan_sha256:
            raise ValueError(f"the engine pod ran another launch plan; {where}")
        if record.get("files") != files:
            raise ValueError(f"the engine pod's delivered files differ from the plan; {where}")
        if "error" in record:
            raise ValueError(f"engine pod {name.removesuffix('.json')}: {record['error']}; {where}")
    nodes = {record.get("node") for record in records.values()}
    node = next(iter(nodes))
    if len(nodes) != 1 or not isinstance(node, str) or not node:
        raise ValueError(f"the records name more than one node; inspect {directory}")
    if records[pod.CHECKPOINT_RECORD].get("model_tree_sha256") != plan["model_tree_sha256"]:
        raise ValueError(
            "the engine pod's model files differ from the launch plan; "
            f"inspect {kept[pod.CHECKPOINT_RECORD]}"
        )
    runtime = records[pod.RUNTIME_RECORD]
    log = kept[pod.RUNTIME_RECORD].relative_to(run)
    if runtime.get("returncode") != 0 or not isinstance(runtime.get("stdout"), str):
        detail = str(runtime.get("stderr") or "").strip().splitlines()
        raise ValueError(
            f"engine pod runtime check exited {runtime.get('returncode')}: "
            f"{detail[-1] if detail else 'empty stderr'}; inspect {run / log}"
        )
    device = runtime.get("transfer_device")
    if (
        not isinstance(device, dict)
        or set(device) != {"verbs_device", "hca", "port", "ucx_net_devices"}
        or device["ucx_net_devices"] != f"{device['hca']}:{device['port']}"
    ):
        raise ValueError(f"the runtime record names no allocated HCA; inspect {run / log}")
    context = check_context(
        run, plan, str(log), read_env(run / "container.env"), model_metadata=False
    )
    identity = {
        "backend": "kubernetes",
        # The kubelet pulls the image by this digest and reports it as the container's image ID.
        "image_id": "sha256:" + plan["image"].rsplit("@sha256:", 1)[1],
        "pod_uid": uid,
        "node": node,
        "transfer_device": device,
    }
    finish_check(context, runtime["stdout"], identity)
