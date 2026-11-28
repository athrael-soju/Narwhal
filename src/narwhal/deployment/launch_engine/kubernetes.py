"""Render a Kubernetes launch plan into its engine pod, and check the pod's records.

`prepare --backend kubernetes` writes `kubernetes.json`: two immutable ConfigMaps, one with the
launch's hook files and one with the pod steps, the engine pod's specification, and the service
account, Role and RoleBinding its attestation sidecar reads the pod with. The pod runs every
engine step in the unmodified engine image. An init container hashes the served model, a second
one runs the runtime probe and judges its output as the offline check does, and the engine
container starts once both pass. `check` reads the records they leave on the state volume,
judges the probe output again and writes checked.json.

The attestation sidecar runs in the Narwhal image beside the engine container. The pod shares
its process namespace, so the sidecar binds the engine process to its container, and it reads
the launch directory that checked its pod from the state volume. It runs as its own non-root
user, `NARWHAL_KUBERNETES_SIDECAR_USER`. The engine and runtime-check containers keep only
IPC_LOCK, under the RuntimeDefault seccomp profile unless the launch runs unconfined, so the
engine can neither reach the sidecar's files through /proc nor rewrite the evidence the
sidecar writes. The checkpoint init container hands the pod's directory of the state volume to
the sidecar's user.

The engine API key stays in its Secret. The launch names the Secret, and the pod spec gives the
key to the containers that call the engine, the engine and the sidecar, by `secretKeyRef`, so
no file of the launch directory, which the state volume and the run directory keep, holds it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from ...engines.pod_identity import SERVICE_ACCOUNT
from .. import stages
from . import checkpoint as checkpoint_module
from . import pod
from . import verdict as verdict_module
from .backend import plan_launcher
from .check import check_context, finish_check, judge, require_checked
from .checkpoint import json_digest
from .plan import (
    API_KEY_ENV,
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
SERVICE_ACCOUNT_ENV = "NARWHAL_KUBERNETES_SERVICE_ACCOUNT"
SIDECAR_IMAGE_ENV = "NARWHAL_KUBERNETES_SIDECAR_IMAGE"
RUNTIME_CLASS_ENV = "NARWHAL_KUBERNETES_RUNTIME_CLASS"
# The user and group the attestation sidecar runs as; the chart's settings name it.
SIDECAR_USER_ENV = "NARWHAL_KUBERNETES_SIDECAR_USER"
DEFAULT_SIDECAR_USER = 10002
# Volume sources that serve a staged checkpoint to the pod.
MODEL_VOLUME_SOURCES = ("persistentVolumeClaim", "hostPath", "nfs", "csi")
# The pod steps, by file name in the pod-step ConfigMap; the engine's probe reader joins them.
POD_FILES = {
    "pod.py": Path(pod.__file__),
    "checkpoint.py": Path(checkpoint_module.__file__),
    "verdict.py": Path(verdict_module.__file__),
}
PROBE_READER = "probe_reader.py"
RUNTIME_CHECK_STAGE = "pod-runtime-check"
MANIFESTS = "kubernetes.json"
# The pod's records, copied into the launch directory by pod UID.
POD_RECORDS = "pods"
# Each launch directory that checked one of the role's pods lies on the state volume at
# engines/<role>/launches/<pod UID>, beside the pod's own directory engines/<role>/<pod UID>.
LAUNCHES = "launches"
# Where the attestation sidecar mounts its pod's launch directory.
LAUNCH_MOUNT = Path("/narwhal-launch")
DOWNWARD_ENV = (
    (POD_ADDRESS_ENV, "status.podIP"),
    (pod.POD_UID_ENV, "metadata.uid"),
    (pod.NODE_NAME_ENV, "spec.nodeName"),
)
# The attestation sidecar also takes its pod's name, to read the pod through the API.
SIDECAR_ENV = (*DOWNWARD_ENV, ("NARWHAL_POD_NAME", "metadata.name"))
SIDECAR = "attestation"
# The sidecar's service account token lasts this long; the kubelet renews it before it expires.
TOKEN_SECONDS = 3600
RBAC_API = "rbac.authorization.k8s.io/v1"
# Runs first in each pod container. It checks every delivered file against the SHA-256 the pod
# specification pins, then runs the pod steps. Isolated mode keeps the hook directory, which
# the engine's PYTHONPATH names, off its import path, and -B keeps the pod steps from writing
# bytecode beside the files they check.
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


def api_key_secret(env: Mapping[str, str]) -> dict | None:
    """The Secret and key that hold the engine API key, or None for an engine without one.

    A Kubernetes launch takes the key by this reference only: a launch whose inputs hold the
    key itself is refused, so no launch file can hold it.
    """
    reference = env.get(API_KEY_SECRET_ENV, "")
    if env.get("NARWHAL_ENGINE_API_KEY"):
        raise ValueError(
            "a Kubernetes engine reads its API key from the Secret "
            f"{API_KEY_SECRET_ENV} names; unset NARWHAL_ENGINE_API_KEY so no launch file holds it"
        )
    if not reference:
        return None
    name, _, key = reference.partition("/")
    if not re.fullmatch(DNS_SUBDOMAIN, name) or not re.fullmatch(r"[-._A-Za-z0-9]{1,253}", key):
        raise ValueError(f"set {API_KEY_SECRET_ENV} to <secret name>/<key>")
    return {"name": name, "key": key}


def pod_files(plan: dict) -> dict[str, Path]:
    """Return the source of each file in the pod-step ConfigMap, by file name."""
    engine = plan_launcher(plan)
    modules = {PROBE_READER: engine.probe_reader}
    if engine.pod_inspections is not None:
        modules[f"{pod.INSPECTIONS_MODULE}.py"] = engine.pod_inspections
    files = dict(POD_FILES)
    for name, module in modules.items():
        if module.__file__ is None:
            raise ValueError(f"the engine backend's {name} has no source file")
        files[name] = Path(module.__file__)
    return files


def sidecar_user(env: Mapping[str, str]) -> int:
    """The non-root user the attestation sidecar runs as, `DEFAULT_SIDECAR_USER` unless set."""
    value = env.get(SIDECAR_USER_ENV, "") or str(DEFAULT_SIDECAR_USER)
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 2**31 - 1:
        raise ValueError(f"set {SIDECAR_USER_ENV} to a non-root user ID")
    return int(value)


def sidecar_image(env: dict[str, str]) -> str:
    image = env.get(SIDECAR_IMAGE_ENV, "")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError(
            f"set {SIDECAR_IMAGE_ENV} to the Narwhal image's registry digest, "
            "<repository>@sha256:<hex>"
        )
    return image


def hca_device(record: dict) -> tuple[str, int]:
    """Return the HCA and port of the launch record's one `<HCA>:<port>` transfer device.

    The fleet's RDMA resource hands the pod this HCA, which the pod checks before it sends KV
    through it, at this port.
    """
    net = record["transfer"]["net_devices"]
    match = re.fullmatch(r"([^,:]+):([1-9][0-9]{0,2})", net)
    if match is None:
        raise ValueError(
            f"{record['role']}: a Kubernetes engine's transfer.net_devices names one HCA and its "
            "port, <HCA>:<port>"
        )
    return match[1], int(match[2])


def pod_settings(env: dict[str, str], record: dict, plan: dict) -> dict:
    """Return the plan's pod section from the deployment inputs, the launch record and the plan."""
    gpu, rdma = resource_name(env, GPU_RESOURCE_ENV), resource_name(env, RDMA_RESOURCE_ENV)
    if gpu == rdma:
        raise ValueError(f"{GPU_RESOURCE_ENV} and {RDMA_RESOURCE_ENV} must name two resources")
    claim = env.get(STATE_CLAIM_ENV, "")
    if not re.fullmatch(DNS_SUBDOMAIN, claim):
        raise ValueError(f"set {STATE_CLAIM_ENV} to the state PersistentVolumeClaim's name")
    account = env.get(SERVICE_ACCOUNT_ENV, "")
    if not re.fullmatch(DNS_SUBDOMAIN, account):
        raise ValueError(
            f"set {SERVICE_ACCOUNT_ENV} to the service account the attestation sidecar reads "
            "its pod with"
        )
    # A node whose default container runtime exposes no accelerators runs the engine pod under
    # the RuntimeClass of the runtime that does.
    runtime_class = env.get(RUNTIME_CLASS_ENV, "")
    if runtime_class and not re.fullmatch(DNS_SUBDOMAIN, runtime_class):
        raise ValueError(f"set {RUNTIME_CLASS_ENV} to a RuntimeClass name, or leave it unset")
    secret = api_key_secret(env)
    hca, port = hca_device(record)
    budget = stages.seconds(
        "NARWHAL_STAGE_" + RUNTIME_CHECK_STAGE.upper().replace("-", "_") + "_TIMEOUT_SECONDS",
        stages.seconds("NARWHAL_STAGE_TIMEOUT_SECONDS", 300),
    )
    return {
        "gpu_resource": gpu,
        "gpus": len(record["gpu_ids"]),
        "rdma_resource": rdma,
        "hca": hca,
        "hca_port": port,
        "model_volume": model_volume(env),
        "state_claim": claim,
        "service_account": account,
        "sidecar_image": sidecar_image(env),
        "sidecar_user": sidecar_user(env),
        **({"api_key_secret": secret} if secret is not None else {}),
        **({"runtime_class": runtime_class} if runtime_class else {}),
        # Docker's ROCm launch runs the engine without a seccomp filter, as this pod does.
        "seccomp_unconfined": record["gpu_visibility_env"] == "ROCR_VISIBLE_DEVICES",
        "runtime_check_seconds": budget,
        "pod_files_sha256": {name: digest(path) for name, path in pod_files(plan).items()},
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
    """Return the engine environment, with the downward API values its references use first.

    The engine's credential variable comes from the plan's Secret; container.env never holds it.
    """
    secret = plan["kubernetes"].get("api_key_secret")
    credential = plan_launcher(plan).api_key_env
    env: list[dict] = [
        {"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}
        for name, field in DOWNWARD_ENV
    ]
    values: list[dict] = []
    for name, value in read_env(run / "container.env").items():
        if name == credential:
            raise ValueError(
                f"container.env holds the engine credential {name}; a Kubernetes engine reads "
                f"it from the Secret {API_KEY_SECRET_ENV} names"
            )
        values.append({"name": name, "value": literal(value)})
    if secret is not None:
        values.append({"name": credential, "valueFrom": {"secretKeyRef": dict(secret)}})
    return env + sorted(values, key=lambda item: item["name"])


def pod_spec(run: Path, plan: dict, probe: list[str], maps: tuple[str, str]) -> dict:
    settings = plan["kubernetes"]
    pins = json.dumps(delivered_files(plan, digest(run / "launch.json")), sort_keys=True)

    def command(step: str, *arguments: str) -> list[str]:
        return [literal(part) for part in ("python3", "-I", "-B", "-c", BOOTSTRAP, pins, step)] + [
            literal(argument) for argument in arguments
        ]

    def state(read_only: bool) -> dict:
        return {
            "name": "narwhal-state",
            "mountPath": str(pod.STATE),
            "subPathExpr": f"engines/{plan['role']}/$({pod.POD_UID_ENV})",
            **({"readOnly": True} if read_only else {}),
        }

    hooks = {"name": "narwhal-hooks", "mountPath": HOOK_MOUNT, "readOnly": True}
    model = {"name": "model", "mountPath": MODEL_MOUNT, "readOnly": True}
    delivered = [
        hooks,
        {"name": "narwhal-pod", "mountPath": str(pod.TOOLS), "readOnly": True},
        model,
    ]
    events = (
        [{"name": "kv-events", "mountPath": KV_EVENTS_MOUNT}]
        if plan["kv_events"] is not None
        else []
    )
    engine_volume = {"name": "narwhal-engine", "mountPath": str(pod.ENGINE)}
    serving = [
        *delivered,
        {"name": "cache", "mountPath": "/root/.cache"},
        {"name": "shm", "mountPath": "/dev/shm"},
        *events,
    ]
    env = pod_env(run, plan)
    resources = {
        "limits": {settings["gpu_resource"]: str(settings["gpus"]), settings["rdma_resource"]: "1"}
    }
    profile = "Unconfined" if settings["seccomp_unconfined"] else "RuntimeDefault"
    security = {
        "securityContext": {
            "allowPrivilegeEscalation": False,
            # Docker lifts the engine's locked-memory limit; RDMA memory registration needs it.
            # Without the other capabilities, root in the engine reaches neither the sidecar's
            # files through /proc nor the evidence it writes.
            "capabilities": {"drop": ["ALL"], "add": ["IPC_LOCK"]},
            "seccompProfile": {"type": profile},
        }
    }
    hooks_map, steps_map = maps
    return {
        "hostNetwork": True,
        "dnsPolicy": "ClusterFirstWithHostNet",
        # Service links would add variables, such as one per Service port, to the engine's own.
        "enableServiceLinks": False,
        # The attestation sidecar binds the engine process to its container through /proc.
        "shareProcessNamespace": True,
        "serviceAccountName": settings["service_account"],
        # Only the sidecar mounts the service account's token.
        "automountServiceAccountToken": False,
        **({"runtimeClassName": settings["runtime_class"]} if "runtime_class" in settings else {}),
        "initContainers": [
            {
                "name": "checkpoint",
                "image": plan["image"],
                # It runs as root with CHOWN and FOWNER alone, to hand the pod's directory of
                # the state volume to the sidecar's user.
                "command": command("checkpoint", str(settings["sidecar_user"])),
                "env": env[: len(DOWNWARD_ENV)],
                "volumeMounts": [*delivered, state(False)],
                "securityContext": {
                    "runAsUser": 0,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "FOWNER"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
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
                "volumeMounts": [*serving, engine_volume, state(True)],
                **security,
            },
            sidecar(plan, [hooks, model, *events, engine_volume, state(False)]),
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
            {"name": "narwhal-engine", "emptyDir": {}},
            {
                "name": "narwhal-api",
                "projected": {
                    "defaultMode": 0o444,
                    "sources": [
                        {
                            "serviceAccountToken": {
                                "path": "token",
                                "expirationSeconds": TOKEN_SECONDS,
                            }
                        },
                        {
                            "configMap": {
                                "name": "kube-root-ca.crt",
                                "items": [{"key": "ca.crt", "path": "ca.crt"}],
                            }
                        },
                        {
                            "downwardAPI": {
                                "items": [
                                    {
                                        "path": "namespace",
                                        "fieldRef": {"fieldPath": "metadata.namespace"},
                                    }
                                ]
                            }
                        },
                    ],
                },
            },
        ],
    }


def sidecar_env(plan: dict) -> list[dict]:
    """The sidecar's downward API values and, for an engine with a key, the key from its
    Secret in `API_KEY_ENV`: the sidecar calls the engine it attests."""
    env: list[dict] = [
        {"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}
        for name, field in SIDECAR_ENV
    ]
    secret = plan["kubernetes"].get("api_key_secret")
    if secret is not None:
        env.append({"name": API_KEY_ENV, "valueFrom": {"secretKeyRef": dict(secret)}})
    return env


def sidecar(plan: dict, mounts: list[dict]) -> dict:
    """Return the attestation sidecar container, which runs in the Narwhal image.

    It mounts the launch directory that checked its pod read-only, writes its attestation in
    the pod's directory of the state volume, and reads the engine process's directory in the
    engine volume. It runs as its own non-root user, with no capabilities.
    """
    user = plan["kubernetes"]["sidecar_user"]
    launch = {
        "name": "narwhal-state",
        "mountPath": str(LAUNCH_MOUNT),
        "subPathExpr": f"engines/{plan['role']}/{LAUNCHES}/$({pod.POD_UID_ENV})",
        "readOnly": True,
    }
    account = {"name": "narwhal-api", "mountPath": str(SERVICE_ACCOUNT), "readOnly": True}
    return {
        "name": SIDECAR,
        "image": plan["kubernetes"]["sidecar_image"],
        "command": [
            "python3",
            "-I",
            "-m",
            "narwhal.deployment.attestation_contract.cli",
            "serve",
            "--run",
            str(LAUNCH_MOUNT),
            "--pod-evidence",
            str(pod.STATE),
        ],
        "env": sidecar_env(plan),
        "ports": [{"name": "attestation", "containerPort": plan["attestation_port"]}],
        "volumeMounts": [*mounts, launch, account],
        "securityContext": {
            "runAsUser": user,
            "runAsGroup": user,
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }


def access_objects(plan: dict) -> dict:
    """Return the service account the sidecar runs as, and the Role and RoleBinding that let
    it get the pods of its namespace and nothing else."""
    account = plan["kubernetes"]["service_account"]
    role = f"{account}-pod-reader"
    return {
        "serviceAccount": {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": account},
            "automountServiceAccountToken": False,
        },
        "role": {
            "apiVersion": RBAC_API,
            "kind": "Role",
            "metadata": {"name": role},
            "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}],
        },
        # A service account subject without a namespace names one in the binding's namespace.
        "roleBinding": {
            "apiVersion": RBAC_API,
            "kind": "RoleBinding",
            "metadata": {"name": role},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role},
            "subjects": [{"kind": "ServiceAccount", "name": account}],
        },
    }


def write_manifests(run: Path, plan: dict, probe: list[str]) -> None:
    """Write kubernetes.json from a prepared launch directory."""
    plan_sha256 = digest(run / "launch.json")
    hooks = {path.name: path.read_text() for path in sorted((run / "hook").iterdir())}
    steps = {name: path.read_text() for name, path in sorted(pod_files(plan).items())}
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
        **access_objects(plan),
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
    keeps each record of this plan it reads under pods/<pod UID>/, named by its SHA-256. The
    check reads records, not the pod: it accepts the records of a pod that ran this plan and
    has since stopped.
    """
    if (run / "checked.json").exists():
        require_checked(run, plan)
    names = (pod.CHECKPOINT_RECORD, pod.RUNTIME_RECORD)
    read = {name: read_record(evidence / name) for name in names}
    records = {name: record for name, (_, record) in read.items()}
    plan_sha256 = digest(run / "launch.json")
    uids = set()
    for name, record in records.items():
        uid = record.get("pod_uid")
        if not isinstance(uid, str) or not re.fullmatch(UID, uid):
            raise ValueError(f"{evidence / name} names no pod UID")
        if record.get("plan_sha256") != plan_sha256:
            raise ValueError(f"the engine pod ran another launch plan; inspect {evidence / name}")
        uids.add(uid)
    if len(uids) != 1:
        raise ValueError(f"the records in {evidence} come from more than one pod")
    [uid] = uids
    directory = run / POD_RECORDS / uid
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    kept = {}
    for name, (text, _) in read.items():
        stem = name.removesuffix(".json")
        kept[name] = directory / f"{stem}-{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
        write_once(kept[name], text, f"{kept[name]} differs from its own digest")
    files = delivered_files(plan, plan_sha256)
    for name, record in records.items():
        where = f"inspect {kept[name]}"
        if record.get("files") != files:
            raise ValueError(f"the engine pod's delivered files differ from the plan; {where}")
        if "error" in record:
            raise ValueError(f"engine pod {name.removesuffix('.json')}: {record['error']}; {where}")
    nodes = {record.get("node") for record in records.values()}
    node = next(iter(nodes))
    if len(nodes) != 1 or not isinstance(node, str) or not node:
        raise ValueError(f"the records name more than one node; inspect {directory}")
    model = records[pod.CHECKPOINT_RECORD]
    if model.get("model_tree_sha256") != plan["model_tree_sha256"]:
        raise ValueError(
            "the engine pod's model files differ from the launch plan; "
            f"inspect {kept[pod.CHECKPOINT_RECORD]}"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", str(model.get("status_sha256"))):
        raise ValueError(
            "the checkpoint record holds no model file status; start the pod with this "
            f"release's pod steps, and inspect {kept[pod.CHECKPOINT_RECORD]}"
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
    if (device["hca"], device["port"]) != (
        plan["kubernetes"]["hca"],
        plan["kubernetes"]["hca_port"],
    ):
        raise ValueError(
            f"the runtime record's HCA {device['ucx_net_devices']} differs from the plan's; "
            f"inspect {run / log}"
        )
    context = check_context(
        run, plan, str(log), read_env(run / "container.env"), model_metadata=False
    )
    judged = judge(context, runtime["stdout"])
    if runtime.get("verdict") != judged:
        raise ValueError(
            f"the engine pod's verdict differs from this check of its output; inspect {run / log}"
        )
    identity = {
        "backend": "kubernetes",
        # The image the pod specification names; the check does not observe the running image.
        "planned_image": plan["image"],
        "pod_uid": uid,
        "node": node,
        "transfer_device": device,
        # The status of the model files the checkpoint init container hashed, which the
        # attestation sidecar compares with the files it serves.
        "model_status_sha256": model["status_sha256"],
    }
    finish_check(context, judged, identity)
