"""Synthetic Kubernetes engine launches and the chart values a fleet of them takes.

`python -m tests.deployment.chart_fixtures <values.json> <direct-values.json>` writes the
values `make chart-check` renders the chart with: a fleet of attested engines, and a fleet of
engines that start directly.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
from pathlib import Path

from narwhal.backends import load as load_backend
from narwhal.deployment import direct_engine_set
from narwhal.deployment.launch_engine import engine_set
from narwhal.deployment.launch_engine.backend import cache_capture
from narwhal.deployment.launch_engine.plan import prepare
from narwhal.deployment.launch_engine.runtime import digest
from tests.backends.contract import MODEL_CONFIG, fixtures, launch_record
from tests.deployment.fixtures import launcher_inputs
from tests.deployment.pod_fixtures import INPUTS, RDMA_TRANSFER, kubernetes_env
from tools.observability.chart_dashboard import chart_dashboard

NAMESPACE = "example"
NARWHAL_IMAGE = INPUTS["NARWHAL_KUBERNETES_SIDECAR_IMAGE"]
HAPROXY_IMAGE = "registry.example/haproxy@sha256:" + "b" * 64
FABRIC_IMAGE = "registry.example/perftest@sha256:" + "c" * 64
PROMETHEUS_IMAGE = "registry.example/prometheus@sha256:" + "e" * 64
GRAFANA_IMAGE = "registry.example/grafana@sha256:" + "f" * 64
LLAMACPP_IMAGE = "registry.example/llama.cpp@sha256:" + "d" * 64
# The runtime section of a llama.cpp template, as `narwhal dev` reads it.
LLAMACPP_RUNTIME = {"backend": "llamacpp", "context_tokens": 2048, "slots": 2, "extra_args": []}
# Each engine role after the first listens 100 ports higher, as engines on one node must.
PORT_STEP = 100


def prepare_role(root: Path, number: int, backend: str = "vllm", **environment: str) -> Path:
    """Prepare the Kubernetes launch of engine role `number` in a directory of its own."""
    root.mkdir(parents=True)
    launcher = load_backend(backend).launcher()
    _, env = launcher_inputs(root)
    config = root / "model" / "config.json"
    config.write_text(json.dumps(MODEL_CONFIG))
    env["NARWHAL_MODEL_CONFIG_SHA256"] = digest(config)
    capture = cache_capture(launcher)
    if capture is not None:
        env["NARWHAL_CACHE_CAPTURE_HOOK"] = str(capture.cache_hook)
        env["NARWHAL_CACHE_CAPTURE_HOOK_SHA256"] = digest(capture.cache_hook)
    env = kubernetes_env(env)
    role = f"engine-{number}"
    offset = PORT_STEP * (number - 1)
    ports = {}
    for name in ("NARWHAL_ENGINE_PORT", "NARWHAL_ATTEST_PORT", "NARWHAL_KV_SIDE_CHANNEL_PORT"):
        ports[env[name]] = str(int(env[name]) + offset)
        env[name] = ports[env[name]]
    for name in [name for name in env if name.startswith("NARWHAL_NODE_1_")]:
        value = env.pop(name).replace("engine-1.", f"{role}.")
        for old, new in ports.items():
            value = value.replace(f":{old}", f":{new}")
        env[name.replace("_1_", f"_{number}_", 1)] = value
    env.update(environment)
    record = launch_record(copy.deepcopy(fixtures(backend).launch_runtime), None, RDMA_TRANSFER)
    record["role"] = role
    Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).write_text(json.dumps(record))
    run = root / "launch"
    with contextlib.redirect_stdout(io.StringIO()):
        prepare(run, env, backend="kubernetes")
    return run


def values(engines: dict, **overrides: object) -> dict:
    """Complete chart values around the `engines` values of a fleet."""
    document = {
        "images": {"narwhal": NARWHAL_IMAGE, "haproxy": HAPROXY_IMAGE, "fabric": FABRIC_IMAGE},
        "engines": engines,
        "state": {"storageClassName": "shared", "size": "10Gi"},
        "lease": {"storageClassName": "shared", "size": "1Gi"},
        "routers": {"fleet": "fleet.json", "storageClassName": "local", "size": "1Gi"},
        "credentials": {
            "engineApiKey": {
                "env": "NARWHAL_ENGINE_API_KEY",
                "secretName": "engine-credential",
                "key": "api-key",
            }
        },
        "monitoring": {
            "enabled": True,
            "prometheus": {
                "image": PROMETHEUS_IMAGE,
                "storage": {"storageClassName": "local", "size": "20Gi"},
                "resources": {"requests": {"cpu": "500m", "memory": "2Gi"}},
            },
            "grafana": {
                "image": GRAFANA_IMAGE,
                "dashboard": chart_dashboard(),
                "admin": {
                    "secretName": "grafana-admin",
                    "userKey": "user",
                    "passwordKey": "password",
                },
            },
        },
        "gates": [
            {
                "name": "discover-node-a",
                "kind": "discovery",
                "command": ["python3", "-I", "-m", "narwhal.deployment.discovery"],
                "deadlineSeconds": 3600,
                "node": "node-a",
            },
            {
                "name": "fabric-node-a-node-b-server",
                "kind": "fabric-server",
                "command": ["ib_write_bw"],
                "deadlineSeconds": 300,
                "node": "node-b",
            },
            {
                "name": "preflight",
                "kind": "preflight",
                "command": ["narwhal-check", "--fleet", "/narwhal-state/fleet.json"],
                "deadlineSeconds": 300,
            },
        ],
    }
    document.update(overrides)
    return document


def example(root: Path, count: int = 2, backend: str = "vllm") -> tuple[list[Path], dict]:
    runs = [prepare_role(root / f"engine-{n}", n, backend) for n in range(1, count + 1)]
    return runs, values(engine_set.chart_values(runs))


def direct_engines(count: int = 2, **overrides: object) -> dict:
    """The `directEngines` values of a llama.cpp fleet."""
    arguments: dict = {
        "backend": "llamacpp",
        "runtime": LLAMACPP_RUNTIME,
        "binary": "/app/llama-server",
        "model_file": "model.gguf",
        "served_name": "example-model",
        "count": count,
        "image": LLAMACPP_IMAGE,
        "resources": {
            "requests": {"cpu": "2", "memory": "2Gi"},
            "limits": {"cpu": "2", "memory": "2Gi"},
        },
        "model_volume": {"persistentVolumeClaim": {"claimName": "models", "readOnly": True}},
        "shared": {"storageClassName": "shared", "size": "1Gi"},
        "namespace": NAMESPACE,
    }
    arguments.update(overrides)
    return direct_engine_set.direct_values(**arguments)


def direct_example(count: int = 2) -> dict:
    """Complete chart values of a fleet of direct engines, which take no gate Jobs."""
    return values({}, directEngines=direct_engines(count), gates=[], credentials={})


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        _, document = example(Path(folder))
    Path(sys.argv[1]).write_text(json.dumps(document, indent=2) + "\n")
    Path(sys.argv[2]).write_text(json.dumps(direct_example(), indent=2) + "\n")
