"""Kubernetes launch inputs, and an engine pod simulated under a directory of its own."""

import contextlib
import io
import json
import os
import re
import shutil
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment.launch_engine import pod

IMAGE = "registry.example/engine@sha256:" + "e" * 64
INPUTS = {
    "NARWHAL_ENGINE_IMAGE": IMAGE,
    "NARWHAL_NODE_1_URL": "http://engine-1.engines.example.svc:8000",
    "NARWHAL_KUBERNETES_GPU_RESOURCE": "gpu.example.com/gpu",
    "NARWHAL_KUBERNETES_RDMA_RESOURCE": "rdma.example.com/hca",
    "NARWHAL_KUBERNETES_MODEL_VOLUME": json.dumps(
        {"persistentVolumeClaim": {"claimName": "models", "readOnly": True}}
    ),
    "NARWHAL_KUBERNETES_STATE_CLAIM": "narwhal-state",
    "NARWHAL_KUBERNETES_API_KEY_SECRET": "engine-credential/api-key",
}
RDMA_TRANSFER = {
    "transport": "ucx_rdma",
    "net_devices": "hca0:1",
    "devices": ["/dev/infiniband/uverbs0"],
}
POD_UID = "6f1c2a3b-4d5e-4f60-8a9b-0c1d2e3f4a5b"
NODE = "gpu-node-a"
POD_IP = "198.51.100.7"
SECRET_VALUE = "engine-only-secret"
# Kubernetes expands $(NAME) from the variables defined before it, and turns $$ into $.
EXPANSION = re.compile(r"\$\$|\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


class Executed(Exception):
    """The engine container's step replaced its process with the engine."""


def kubernetes_env(env: dict[str, str]) -> dict[str, str]:
    """Return a container launch's inputs as a Kubernetes launch takes them."""
    env = {**env, **INPUTS}
    # The pod advertises its own address.
    del env["NARWHAL_NODE_1_IP"]
    return env


def expand(value: str, defined: dict[str, str]) -> str:
    return EXPANSION.sub(
        lambda match: "$" if match[0] == "$$" else defined.get(match[1], match[0]), value
    )


class EnginePod:
    """An engine pod's mounts under `root`, filled from a launch's kubernetes.json.

    `run` runs one container's pod step in this process, as the bootstrap would after its
    digest check, with the container's environment and an HCA from `allocate`.
    """

    def __init__(self, root: Path, run: Path, *, hcas: tuple[str, ...] = ("hca0",)) -> None:
        self.root = root
        self.manifests = json.loads((run / "kubernetes.json").read_text())
        spec = self.manifests["podSpec"]
        self.containers = {
            container["name"]: container
            for container in [*spec["initContainers"], *spec["containers"]]
        }
        data = {item["metadata"]["name"]: item["data"] for item in self.manifests["configMaps"]}
        mounts = {
            item["name"]: item["mountPath"] for item in self.containers["engine"]["volumeMounts"]
        }
        for volume in spec["volumes"]:
            if "configMap" in volume:
                target = self.path(mounts[volume["name"]])
                target.mkdir(parents=True)
                for name, text in data[volume["configMap"]["name"]].items():
                    (target / name).write_text(text)
        plan = json.loads((run / "launch.json").read_text())
        shutil.copytree(plan["model_dir"], self.path(pod.MODEL), symlinks=True)
        self.state.mkdir(parents=True)
        self.allocate(hcas)
        self.executed: list[tuple[str, list[str], str | None]] = []
        self.stderr = io.StringIO()

    def path(self, pod_path: str | Path) -> Path:
        return self.root / Path(pod_path).relative_to("/")

    @property
    def state(self) -> Path:
        return self.path(pod.STATE)

    def allocate(self, hcas: tuple[str, ...]) -> None:
        """Hand the container these HCAs, as the RDMA device plugin would."""
        for directory in (self.path(pod.VERBS_DEVICES), self.path(pod.VERBS_CLASS)):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True)
        for index, hca in enumerate(hcas):
            (self.path(pod.VERBS_DEVICES) / f"uverbs{index}").touch()
            device = self.path(pod.VERBS_CLASS) / f"uverbs{index}"
            device.mkdir()
            (device / "ibdev").write_text(hca + "\n")

    def environment(self, name: str) -> dict[str, str]:
        fields = {"status.podIP": POD_IP, "metadata.uid": POD_UID, "spec.nodeName": NODE}
        defined: dict[str, str] = {}
        for entry in self.containers[name]["env"]:
            source = entry.get("valueFrom")
            if source is None:
                defined[entry["name"]] = expand(entry["value"], defined)
            elif "fieldRef" in source:
                defined[entry["name"]] = fields[source["fieldRef"]["fieldPath"]]
            else:
                defined[entry["name"]] = SECRET_VALUE
        return defined

    def command(self, name: str) -> list[str]:
        environment = self.environment(name)
        return [expand(part, environment) for part in self.containers[name]["command"]]

    def run(self, name: str, *, probe: list[str] | None = None) -> int:
        """Run a container's step; `probe` stands in for the runtime probe's arguments."""
        command = self.command(name)
        assert command[:3] == ["python3", "-I", "-c"], command[:3]
        arguments = command[5:]
        if probe is not None:
            arguments = [*arguments[:2], *probe]

        def execv(path: str, argv: list[str]) -> None:
            self.executed.append((path, argv, os.environ.get("UCX_NET_DEVICES")))
            raise Executed

        self.stderr = io.StringIO()
        with (
            patch.object(pod, "ROOT", self.root),
            patch.dict(os.environ, self.environment(name), clear=True),
            patch("os.execv", side_effect=execv),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(self.stderr),
        ):
            try:
                return pod.main(arguments)
            except Executed:
                return 0

    def start(self, probe_output: str) -> None:
        """Run both init containers and the engine container, each of which must pass."""
        for name, probe in (("checkpoint", None), ("runtime-check", printing(probe_output))):
            if self.run(name, probe=probe):
                raise AssertionError(f"{name} failed: {self.stderr.getvalue()}")
        if self.run("engine") or len(self.executed) != 1:
            raise AssertionError(f"the engine did not start: {self.stderr.getvalue()}")


def printing(output: str, code: int = 0) -> list[str]:
    """Probe arguments that print `output` and exit with `code`."""
    return ["-c", f"import sys; sys.stdout.write({output!r}); sys.exit({code})"]
