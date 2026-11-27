"""Turn the Kubernetes launch plans of a fleet's engines into the values of their StatefulSet.

`kubernetes_objects.engines` runs every engine in one StatefulSet named `engine` whose ordinals
start at 1, so each pod's name is its engine role, the name the plans' engine and attestation
URLs give it.
Each plan renders its own pod in `kubernetes.json`. The plans differ only where the role does:
the commands and environment of the engine's steps, the hook files, and the role in the paths
of the state volume. Everything else must be equal across the plans, or the fleet cannot share
one pod template, and `chart_values` refuses it.

The pod template takes each pod's role from its name, in `NARWHAL_ROLE`. The role's hook and
pod-step ConfigMaps are projected under `<role>/` in one volume each and mounted with
`subPathExpr`, so each container sees exactly the files its plan delivers. Each step container
runs `LAUNCHER`, which checks the role's spec file against the SHA-256 the template pins,
expands it as the kubelet expands a container's command and environment, and replaces itself
with the command the plan rendered. The engine process therefore keeps the container's first
PID and the environment the plan gives it, which the attestation sidecar binds.

Two pod fields are not carried over. The template lists no container ports, because engines on
one node listen on different host ports and a template's ports would be every pod's. The
resource limits, the engine image and the model volume become values of their own, which
`kubernetes_objects.engines` sets where the template holds None.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .checkpoint import json_digest
from .kubernetes import MANIFESTS, SIDECAR

STATEFULSET = "engine"
ROLE_ENV = "NARWHAL_ROLE"
# Each step container reads its role's spec file here.
SPEC_MOUNT = "/narwhal-spec"
SPEC_VOLUME = "narwhal-spec"
# The ConfigMap volumes whose content differs by role.
ROLE_VOLUMES = ("narwhal-hooks", "narwhal-pod")
MODEL_VOLUME = "model"
STATE_VOLUME = "narwhal-state"
SCHEMA = "narwhal.engine-set"
SCHEMA_VERSION = 1
ROLE = re.compile(r"engine-([1-9][0-9]*)")
# The kubelet's expansion of a container's command and environment: `$$` is `$`, and `$(NAME)`
# is the value of a variable the container defines, or stays as written.
LAUNCHER_DEFINITIONS = r"""import hashlib, json, os, re, sys
EXPANSION = re.compile(r"\$\$|\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
def expand(value, defined):
    return EXPANSION.sub(lambda m: "$" if m[0] == "$$" else defined.get(m[1], m[0]), value)
def launch(spec_dir, pins, container, environ, execute):
    role = environ.pop("NARWHAL_ROLE", "")
    path = os.path.join(spec_dir, container + ".json")
    with open(path, "rb") as stream:
        data = stream.read()
    if hashlib.sha256(data).hexdigest() != pins.get(role, {}).get(container):
        sys.exit(f"narwhal: {path} differs from the SHA-256 pinned for {role}")
    spec = json.loads(data)
    defined = {name: environ[name] for name in spec["defined"] if name in environ}
    for item in spec["env"]:
        defined[item["name"]] = environ[item["name"]] = expand(item["value"], defined)
    argv = [expand(part, defined) for part in spec["command"]]
    execute(argv[0], argv, environ)
"""
LAUNCHER = (
    LAUNCHER_DEFINITIONS
    + "launch(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], dict(os.environ), os.execvpe)\n"
)


def escape(value: str) -> str:
    """Keep the kubelet from expanding anything in a value of the template's own."""
    return value.replace("$", "$$")


def role_env() -> dict:
    return {"name": ROLE_ENV, "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}


def _load(run: Path) -> tuple[str, dict, dict]:
    manifests = json.loads((run / MANIFESTS).read_text())
    plan = json.loads((run / "launch.json").read_text())
    if plan.get("backend") != "kubernetes":
        raise ValueError(f"{run} holds no Kubernetes launch plan")
    plan_sha256 = hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()
    if manifests.get("plan_sha256") != plan_sha256:
        raise ValueError(f"{run / MANIFESTS} was rendered from another launch.json")
    return plan["role"], plan, manifests


def _service(role: str, plan: dict) -> tuple[str, str]:
    """The headless Service and namespace the plan's engine URL names: <role>.<svc>.<ns>.svc."""
    host = plan["endpoint"].split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    labels = host.split(".")
    if len(labels) < 4 or labels[0] != role or labels[3] != "svc":
        raise ValueError(
            f"{role}: the engine URL must name the pod in the chart's headless Service, "
            f"http://{role}.<service>.<namespace>.svc:<port>, not {plan['endpoint']}"
        )
    return labels[1], labels[2]


def _neutral(role: str, spec: dict, configs: dict[str, dict]) -> tuple[dict, dict, dict]:
    """Split one role's pod spec into the role-neutral template, the role's spec files and the
    values the chart sets, such as the image and resource limits."""
    spec = copy.deepcopy(spec)
    role_path = f"engines/{role}/"
    files: dict[str, dict] = {}
    extracted: dict[str, Any] = {}
    for container in [*spec["initContainers"], *spec["containers"]]:
        name = container["name"]
        container.pop("ports", None)
        if name != SIDECAR:
            env = container.get("env", [])
            linked = [item for item in env if "valueFrom" in item]
            files[name] = {
                "command": container["command"],
                "env": [item for item in env if "valueFrom" not in item],
                "defined": [item["name"] for item in linked],
            }
            container["env"] = [*linked, role_env()]
            container["command"] = None
            # The chart sets the engine image and the resource limits where these stand.
            extracted.setdefault("image", set()).add(container["image"])
            container["image"] = None
            if "resources" in container:
                extracted.setdefault("resources", set()).add(
                    json.dumps(container["resources"], sort_keys=True)
                )
                container["resources"] = None
            container["volumeMounts"].append(
                {
                    "name": SPEC_VOLUME,
                    "mountPath": SPEC_MOUNT,
                    "subPathExpr": f"$({ROLE_ENV})",
                    "readOnly": True,
                }
            )
        else:
            container["env"] = [*container["env"], role_env()]
        for mount in container["volumeMounts"]:
            expression = mount.get("subPathExpr", "")
            if mount["name"] in (*ROLE_VOLUMES, SPEC_VOLUME):
                if mount["name"] != SPEC_VOLUME and expression:
                    raise ValueError(f"{role}: {name} mounts part of {mount['name']}")
                mount["subPathExpr"] = f"$({ROLE_ENV})"
            elif expression.startswith(role_path):
                mount["subPathExpr"] = f"engines/$({ROLE_ENV})/" + expression[len(role_path) :]
            elif expression:
                raise ValueError(f"{role}: {name} mounts {expression}, outside engines/{role}/")
    volumes = []
    for volume in spec["volumes"]:
        if volume["name"] in ROLE_VOLUMES:
            configs[volume["name"]] = volume["configMap"]
            volume = {"name": volume["name"], "projected": None}
        elif volume["name"] == MODEL_VOLUME:
            extracted["model_volume"] = {k: v for k, v in volume.items() if k != "name"}
            volume = {"name": MODEL_VOLUME}
        elif volume["name"] == STATE_VOLUME:
            extracted["state_claim"] = volume["persistentVolumeClaim"]["claimName"]
        volumes.append(volume)
    volumes.append({"name": SPEC_VOLUME, "projected": None})
    spec["volumes"] = volumes
    return spec, files, extracted


def _one(values: set[str], what: str) -> str:
    if len(values) != 1:
        raise ValueError(f"the engines' pods differ in {what}; one StatefulSet runs one {what}")
    return next(iter(values))


def _projected(sources: list[tuple[str, str, list[str]]]) -> dict:
    """A projected volume that places each ConfigMap's keys under its role's directory."""
    return {
        "defaultMode": 0o444,
        "sources": [
            {
                "configMap": {
                    "name": name,
                    "items": [{"key": key, "path": f"{role}/{key}"} for key in sorted(keys)],
                }
            }
            for role, name, keys in sources
        ],
    }


def spec_map(role: str, data: dict[str, str]) -> str:
    """The name of a role's spec ConfigMap, which follows its content as the map is immutable."""
    return f"narwhal-spec-{role}-{json_digest(data)[:12]}"


def check(
    values: Mapping[str, Any],
    *,
    namespace: str,
    state_claim: str,
    narwhal_image: str,
    api_key_secret: Mapping[str, str] | None,
) -> None:
    """Refuse engine values that cannot run in the fleet they are for.

    The plans fix the engines' namespace, state claim, sidecar image and API key Secret, so
    each must be the one the fleet names. `api_key_secret` is the fleet's `{"name", "key"}`
    reference, or None for a fleet without an engine API key.
    """
    if values["namespace"] != namespace:
        raise ValueError(
            f"the engine URLs name namespace {values['namespace']}; run the fleet there, "
            f"not in {namespace}"
        )
    if values["stateClaim"] != state_claim:
        raise ValueError(
            f"the fleet's state claim is {state_claim} and the engine plans name state claim "
            f"{values['stateClaim']}; prepare the engines with the fleet's state claim"
        )
    if values["sidecarImage"] != narwhal_image:
        raise ValueError(
            "the engines' attestation sidecars run another Narwhal image than the fleet's; "
            "prepare the engines with the fleet's Narwhal image"
        )
    template = values["podTemplate"]
    for container in [*template["initContainers"], *template["containers"]]:
        for item in container.get("env", []):
            reference = (item.get("valueFrom") or {}).get("secretKeyRef")
            if reference is None:
                continue
            if api_key_secret is None or (reference["name"], reference["key"]) != (
                api_key_secret["name"],
                api_key_secret["key"],
            ):
                raise ValueError(
                    f"the engines read their API key from Secret {reference['name']} key "
                    f"{reference['key']}; set the fleet's engine API key to that Secret and "
                    "key, so the routers and gate Jobs send it"
                )


def chart_values(runs: Sequence[Path]) -> dict:
    """Return the chart's `engines` values for the launch directories of every engine role."""
    loaded = sorted((_load(run) for run in runs), key=lambda item: int(item[0].split("-")[1]))
    roles = [role for role, _, _ in loaded]
    if roles != [f"engine-{n}" for n in range(1, len(roles) + 1)]:
        raise ValueError(
            f"the StatefulSet names its pods engine-1 onwards; the plans name {', '.join(roles)}"
        )
    template: dict | None = None
    shapes: dict[str, set[str]] = {"image": set(), "resources": set(), "model": set()}
    claims, services, accesses = set(), set(), set()
    config_maps: dict[str, dict] = {}
    sources: dict[str, list[tuple[str, str, list[str]]]] = {name: [] for name in ROLE_VOLUMES}
    spec_sources: list[tuple[str, str, list[str]]] = []
    pins: dict[str, dict[str, str]] = {}
    sidecars = []
    for role, plan, manifests in loaded:
        services.add(_service(role, plan))
        # Prometheus scrapes each sidecar at its pod's name, for its restart counts.
        sidecars.append(urlsplit(plan["attestation_url"]).netloc)
        maps = {item["metadata"]["name"]: item for item in manifests["configMaps"]}
        configs: dict[str, dict] = {}
        neutral, files, extracted = _neutral(role, manifests["podSpec"], configs)
        shapes["image"] |= extracted["image"]
        shapes["resources"] |= extracted.get("resources", set())
        shapes["model"].add(json.dumps(extracted["model_volume"], sort_keys=True))
        claims.add(extracted["state_claim"])
        accesses.add(
            json.dumps({k: manifests[k] for k in ("serviceAccount", "role", "roleBinding")})
        )
        if template is not None and json_digest(neutral) != json_digest(template):
            raise ValueError(
                f"{role}'s pod differs from engine-1's beyond its role; one StatefulSet runs "
                "every engine, so their plans may differ only in role, ports and engine flags"
            )
        template = neutral
        for volume, reference in configs.items():
            name = reference["name"]
            config_maps.setdefault(name, maps[name])
            if config_maps[name] != maps[name]:
                raise ValueError(f"two plans render ConfigMap {name} with different data")
            sources[volume].append((role, name, list(maps[name]["data"])))
        data = {
            f"{container}.json": json.dumps(file, indent=2, sort_keys=True) + "\n"
            for container, file in files.items()
        }
        pins[role] = {
            name.removesuffix(".json"): hashlib.sha256(text.encode()).hexdigest()
            for name, text in data.items()
        }
        name = spec_map(role, data)
        config_maps[name] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name},
            "immutable": True,
            "data": data,
        }
        spec_sources.append((role, name, list(data)))
    if template is None:
        raise ValueError("name the launch directory of at least one engine")
    resources = json.loads(_one(shapes["resources"], "resource limits"))["limits"]
    model_volume = json.loads(_one(shapes["model"], "model volume"))
    if len(services) != 1:
        raise ValueError("the engine URLs name more than one headless Service or namespace")
    [(service, namespace)] = services
    access = json.loads(_one(accesses, "attestation access objects"))
    plan = loaded[0][1]
    gpu_resource = plan["kubernetes"]["gpu_resource"]
    rdma_resource = plan["kubernetes"]["rdma_resource"]
    if resources != {gpu_resource: str(plan["kubernetes"]["gpus"]), rdma_resource: "1"}:
        raise ValueError("the engine pod's resource limits differ from its plan")
    command = [
        "python3",
        "-I",
        "-B",
        "-c",
        escape(LAUNCHER),
        SPEC_MOUNT,
        escape(json.dumps(pins, sort_keys=True, separators=(",", ":"))),
    ]
    for container in [*template["initContainers"], *template["containers"]]:
        if container["name"] != SIDECAR:
            container["command"] = [*command, container["name"]]
    for volume in template["volumes"]:
        if volume["name"] in ROLE_VOLUMES:
            volume["projected"] = _projected(sources[volume["name"]])
        elif volume["name"] == SPEC_VOLUME:
            volume["projected"] = _projected(spec_sources)
    return {
        "schema": SCHEMA,
        "schemaVersion": SCHEMA_VERSION,
        "count": len(roles),
        "image": _one(shapes["image"], "engine image"),
        "gpuResource": gpu_resource,
        "gpus": plan["kubernetes"]["gpus"],
        "rdmaResource": rdma_resource,
        "modelVolume": model_volume,
        "stateClaim": _one(claims, "state claim"),
        "serviceName": service,
        "namespace": namespace,
        "sidecarImage": plan["kubernetes"]["sidecar_image"],
        "sidecars": sidecars,
        "configMaps": [config_maps[name] for name in sorted(config_maps)],
        "access": access,
        "podTemplate": template,
    }
