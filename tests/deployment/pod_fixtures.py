"""Kubernetes launch inputs, and an engine pod simulated under a directory of its own."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
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
    "NARWHAL_KUBERNETES_SERVICE_ACCOUNT": "narwhal-attestation",
    "NARWHAL_KUBERNETES_SIDECAR_IMAGE": "registry.example/narwhal@sha256:" + "a" * 64,
    "NARWHAL_NODE_1_ATTESTATION_URL": "http://engine-1.engines.example.svc:8010/v1/attestation",
}
RDMA_TRANSFER = {
    "transport": "ucx_rdma",
    "net_devices": "hca0:1",
    "devices": ["/dev/infiniband/uverbs0"],
}
POD_UID = "6f1c2a3b-4d5e-4f60-8a9b-0c1d2e3f4a5b"
NODE = "gpu-node-a"
POD_IP = "198.51.100.7"
# The engine container's first process, as /proc/self/stat names it: its ID and start ticks.
ENGINE_PID = 4242
ENGINE_TICKS = 123456
ENGINE_PROCESS = f"{ENGINE_PID}-{ENGINE_TICKS}"
SECRET_VALUE = "engine-only-secret"
# Kubernetes expands $(NAME) from the variables defined before it, and turns $$ into $.
EXPANSION = re.compile(r"\$\$|\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


# The kubelet's name for the snapshot of a ConfigMap volume's data.
SNAPSHOT = "..2026_01_01_00_00_00.000000001"


class Executed(Exception):
    """The engine container's step replaced its process with the engine."""


def kubernetes_env(env: dict[str, str]) -> dict[str, str]:
    """Return a container launch's inputs as a Kubernetes launch takes them."""
    env = {**env, **INPUTS}
    # The pod advertises its own address.
    del env["NARWHAL_NODE_1_IP"]
    # The pod reads the engine API key from its Secret; a Kubernetes launch never holds it.
    env.pop("NARWHAL_ENGINE_API_KEY", None)
    return env


def expand(value: str, defined: dict[str, str]) -> str:
    return EXPANSION.sub(
        lambda match: "$" if match[0] == "$$" else defined.get(match[1], match[0]), value
    )


class EnginePod:
    """An engine pod's mounts under `root`, filled from a launch's kubernetes.json.

    Each ConfigMap volume takes the kubelet's layout: the files lie in a snapshot directory,
    `..data` links to it, and each file name links through `..data`.

    `run` runs one container's pod step in this process, as the bootstrap would after its
    digest check, with the container's environment and an HCA from `allocate`. `execute` runs
    the container's command, bootstrap included, in a process of its own.
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
                (target / SNAPSHOT).mkdir(parents=True)
                (target / "..data").symlink_to(SNAPSHOT)
                for name, text in data[volume["configMap"]["name"]].items():
                    (target / SNAPSHOT / name).write_text(text)
                    (target / name).symlink_to(Path("..data") / name)
        plan = json.loads((run / "launch.json").read_text())
        shutil.copytree(plan["model_dir"], self.path(pod.MODEL), symlinks=True)
        self.state.mkdir(parents=True)
        self.path(pod.ENGINE).mkdir(parents=True)
        stat = self.path(pod.PROC) / "self" / "stat"
        stat.parent.mkdir(parents=True)
        # Fields after the command: state, parent, and so on to the start ticks, the 20th.
        fields = ["S", "0", *["0"] * 17, str(ENGINE_TICKS), "0"]
        stat.write_text(f"{ENGINE_PID} (python3) {' '.join(fields)}\n")
        self.allocate(hcas)
        self.executed: list[tuple[str, list[str], str | None]] = []
        # The startup log each engine start sent its output to, and the cache layout record
        # the cache capture hook was told to write.
        self.logs: list[Path] = []
        self.cache_outputs: list[str | None] = []
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
        command = [expand(part, environment) for part in self.containers[name]["command"]]
        if name == "checkpoint":
            # The checkpoint step runs as root in the pod and hands the state directory to the
            # sidecar's user; the test's own user stands in for it here.
            self.sidecar_user = int(command[-1])
            command[-1] = str(os.getuid())
        return command

    def run(self, name: str, *, probe: list[str] | None = None) -> int:
        """Run a container's step; `probe` stands in for the runtime probe's arguments."""
        command = self.command(name)
        assert command[:4] == ["python3", "-I", "-B", "-c"], command[:4]
        arguments = command[6:]
        if probe is not None:
            arguments = [*arguments[:2], *probe]

        def execv(path: str, argv: list[str]) -> None:
            self.executed.append((path, argv, os.environ.get("UCX_NET_DEVICES")))
            self.cache_outputs.append(os.environ.get("NARWHAL_CACHE_OUTPUT"))
            raise Executed

        self.stderr = io.StringIO()
        with (
            patch.object(pod, "ROOT", self.root),
            patch.object(sys, "dont_write_bytecode", True),
            patch.dict(os.environ, self.environment(name), clear=True),
            patch("os.execv", side_effect=execv),
            # The step would send this process's output to the startup log as well.
            patch.object(pod, "tee", side_effect=self.logs.append),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(self.stderr),
        ):
            try:
                return pod.main(arguments)
            except Executed:
                return 0

    def execute(self, name: str, *, probe: list[str] | None = None) -> subprocess.CompletedProcess:
        """Run a container's command in a process of its own, with the pod's mounts under root.

        The bootstrap's pins and its path to pod.py move under root; pod.py places the pod's
        other paths beside its own directory. `probe` stands in for the probe's arguments.
        """
        command = self.command(name)
        assert command[:4] == ["python3", "-I", "-B", "-c"], command[:4]
        bootstrap, pins, step, *arguments = command[4:]
        tools = str(pod.TOOLS / "pod.py")
        assert repr(tools) in bootstrap
        bootstrap = bootstrap.replace(repr(tools), repr(str(self.path(tools))))
        pins = json.dumps({str(self.path(root)): files for root, files in json.loads(pins).items()})
        if probe is not None:
            arguments = [*arguments[:1], *probe]
        return subprocess.run(
            [sys.executable, *command[1:4], bootstrap, pins, step, *arguments],
            env=self.environment(name),
            capture_output=True,
            text=True,
            timeout=60,
        )

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
