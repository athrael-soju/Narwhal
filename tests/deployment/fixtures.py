import contextlib
import ctypes
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from narwhal.backends import load as load_backend
from narwhal.backends.vllm import captures as launch_captures
from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.launch_engine import check as launch_check
from narwhal.deployment.launch_engine import docker as launch_docker
from narwhal.deployment.launch_engine import start as launch_start
from narwhal.deployment.launch_engine.plan import model_files
from narwhal.deployment.launch_engine.runtime import digest

VLLM_HOOK = load_backend("vllm").launcher().cache_hook
# Launcher modules whose functions call `docker`.
DOCKER_CALLERS = (launch_captures, launch_check, launch_docker, launch_start)


@contextlib.contextmanager
def patched_docker(**kwargs):
    mock = MagicMock(**kwargs)
    with contextlib.ExitStack() as stack:
        for module in DOCKER_CALLERS:
            stack.enter_context(patch.object(module, "docker", mock))
        yield mock


# Stdout of the vLLM runtime check script in narwhal.backends.vllm.check.
def vllm_check_output(
    plan, *, packages=None, prefix_caching=True, api_version="0.29.0", ucx=None, plugin=None
):
    events = plan.get("kv_events")
    settings = {
        "prefix_caching": prefix_caching,
        "kv_events": None
        if events is None
        else {key: events[key] for key in ("endpoint", "replay_endpoint")},
    }
    return (
        ("" if packages is None else json.dumps(packages) + "\n")
        + ("" if plugin is None else "NARWHAL_GGUF_PLUGIN=" + json.dumps(plugin) + "\n")
        + "NARWHAL_TOKENIZER_READY=1\nNARWHAL_CACHE_SETTINGS="
        + json.dumps(settings)
        + "\nNARWHAL_IMAGE_RUNTIME="
        + json.dumps({"vllm_api_version": api_version, "ucx_version": ucx})
    )


def engine_config_modules():

    class FlexibleArgumentParser:
        def parse_args(self, arguments):
            return list(arguments)

    class EngineArgs:
        def __init__(self, arguments):
            self.arguments = arguments

        @staticmethod
        def add_cli_args(parser):
            return parser

        @classmethod
        def from_cli_args(cls, arguments):
            return cls(arguments)

        def create_engine_config(self):
            events = None
            if "--kv-events-config" in self.arguments:
                values = json.loads(self.arguments[self.arguments.index("--kv-events-config") + 1])
                enabled = values.get("enable_kv_cache_events", False)
                events = SimpleNamespace(
                    enable_kv_cache_events=enabled,
                    publisher=values.get("publisher", "zmq" if enabled else "null"),
                    endpoint=values.get("endpoint", "tcp://*:5557"),
                    replay_endpoint=values.get("replay_endpoint"),
                )
            caching = "--no-enable-prefix-caching" not in self.arguments
            return SimpleNamespace(
                cache_config=SimpleNamespace(enable_prefix_caching=caching),
                kv_events_config=events,
            )

    arg_utils = ModuleType("vllm.engine.arg_utils")
    arg_utils.EngineArgs = EngineArgs
    argparse_utils = ModuleType("vllm.utils.argparse_utils")
    argparse_utils.FlexibleArgumentParser = FlexibleArgumentParser
    return {module.__name__: module for module in (arg_utils, argparse_utils)}


@contextlib.contextmanager
def process_group_with_worker():
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "could not enable test child reaping")
    worker = None
    leader = None
    try:
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / "worker.json"
            child_script = (
                "import os, pathlib, signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "ready = pathlib.Path(sys.argv[1]); "
                "pending = ready.with_suffix('.tmp'); "
                "pending.write_text(str(os.getpid())); pending.replace(ready); "
                "time.sleep(60)"
            )
            leader_script = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
                "time.sleep(60)"
            )
            leader = subprocess.Popen(
                [sys.executable, "-c", leader_script, child_script, str(ready)],
                start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            worker = int(ready.read_text())
            yield leader, worker
    finally:
        if worker is None and leader is not None:
            # Setup has retained the unreaped leader PID, including a failed leader.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(leader.pid, signal.SIGKILL)
        if worker is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(worker, signal.SIGKILL)
        if leader is not None:
            if leader.poll() is None:
                leader.kill()
            leader.wait(timeout=5)
        if worker is not None:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(worker, 0)
        elif leader is not None:
            with contextlib.suppress(ChildProcessError):
                while True:
                    os.waitpid(-leader.pid, 0)
        libc.prctl(36, previous.value, 0, 0, 0)


def model_file_fields(plan):
    """Return the model file digests that `prepare` records for `plan`."""
    files = model_files(plan)
    return {"model_tree_sha256": files.tree, "model_status_sha256": files.status}


def launch_document():
    return {
        "schema": "narwhal.engine-launch",
        "schema_version": 1,
        "engines": {
            "engine-1": {
                "accelerator": "synthetic-gpu",
                "gpu_ids": ["0", "1"],
                "tensor_parallel_size": 2,
                "gpu_visibility_env": "ROCR_VISIBLE_DEVICES",
                "accelerator_devices": ["/dev/kfd", "/dev/dri/renderD128", "/dev/dri/renderD129"],
                "network_mode": "host",
                "transfer": {
                    "transport": "ucx_tcp",
                    "net_devices": "${NARWHAL_FABRIC_INTERFACE}",
                    "devices": [],
                },
                "sources": dict.fromkeys(
                    ("allocation", "devices", "transfer"), "synthetic approved launch record"
                ),
            }
        },
    }


def cuda_engine(entry, gpu="0", visible=None):
    entry.update(
        gpu_ids=[gpu],
        tensor_parallel_size=1,
        gpu_visibility_env="CUDA_VISIBLE_DEVICES",
        accelerator_devices=["/dev/nvidiactl", f"/dev/nvidia{gpu}"],
    )
    if visible is not None:
        entry["transfer"]["gpu_tls"] = "cuda"
        entry["environment"] = {"CUDA_VISIBLE_DEVICES": visible, "UCX_NET_DEVICES": "fabric0"}
    return entry


def runtime():
    return {
        "expected_packages": {"vllm": "0.29.0", "nixl": "1.0.0"},
        "model_dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "block_size": 128,
        "context_length": 4096,
        "environment": {"VLLM_ROCM_USE_AITER": "1"},
        "extra_args": ["--enforce-eager"],
    }


def launcher_inputs(root):
    record = selected_launch(launch_document(), "engine-1", {"NARWHAL_FABRIC_INTERFACE": "fabric0"})
    record["runtime"] = runtime()
    model = root / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    source = root / "engine.json"
    source.write_text(json.dumps(record))
    env = {
        "NARWHAL_ENGINE_LAUNCH_CONFIG": str(source),
        "NARWHAL_MODEL_DIR": str(model),
        "NARWHAL_MODEL_CONFIG_SHA256": hashlib.sha256(b"{}").hexdigest(),
        "NARWHAL_ENGINE_IMAGE": "sha256:" + "a" * 64,
        "NARWHAL_CACHE_CAPTURE_HOOK": str(VLLM_HOOK),
        "NARWHAL_CACHE_CAPTURE_HOOK_SHA256": digest(VLLM_HOOK),
        "NARWHAL_ENGINE_PORT": "8000",
        "NARWHAL_ATTEST_PORT": "8010",
        "NARWHAL_KV_SIDE_CHANNEL_PORT": "5600",
        "NARWHAL_UCX_TCP_PORT_RANGE": "39000-39999",
        "NARWHAL_NODE_1_IP": "192.0.2.11",
        "NARWHAL_NODE_1_URL": "http://192.0.2.11:8000",
        "NARWHAL_ENGINE_MODEL_NAME": "synthetic",
        "NARWHAL_DEPLOYMENT_REVISION": "b" * 40,
        "NARWHAL_ENGINE_API_KEY": "engine-only-secret",
        "NARWHAL_NODE_1_SSH_PASSWORD": "management-only-secret",
    }
    return record, env


# The engine-host helpers that a deployment package takes from the wheel.
WHEEL_HELPERS = (
    "backends/vllm/cache_capture_hook.py",
    "backends/trtllm/cache_capture_hook.py",
    "backends/sglang/transfer_measure.py",
)


# A hash-checking lock in the form that `uv export --no-emit-project` writes.
LOCK = """# via fastapi
annotated-types==0.8.0 \\
    --hash=sha256:{a} \\
    --hash=sha256:{b}
    # via pydantic
uvloop==0.22.1 ; sys_platform != 'win32' \\
    --hash=sha256:{a}
""".format(a="a" * 64, b="b" * 64)


def package_inputs(root, version="0.13.0"):
    """Write a stand-in Narwhal wheel with the helper modules and a dependency lock, and return
    the deployment inputs that name them."""
    wheel = Path(root) / f"narwhal_inference-{version}-py3-none-any.whl"
    package = Path(launch_start.__file__).resolve().parents[2]
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in WHEEL_HELPERS:
            archive.writestr(f"narwhal/{name}", (package / name).read_bytes())
    lock = Path(root) / "requirements.lock"
    lock.write_text(LOCK)
    return {
        "NARWHAL_WHEEL": str(wheel),
        "NARWHAL_WHEEL_SHA256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "NARWHAL_REQUIREMENTS": str(lock),
        "NARWHAL_REQUIREMENTS_SHA256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "NARWHAL_TOOLS_COMMIT": "d" * 40,
    }
