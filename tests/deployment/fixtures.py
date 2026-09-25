"""Reusable synthetic engine allocation and serving inputs."""

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
from pathlib import Path

from tools.deployment.engine_launch import selected_launch
from tools.deployment.launch_engine import digest

ROOT = Path(__file__).resolve().parents[2]


@contextlib.contextmanager
def process_group_with_worker():
    """Reap a synthetic leader and its SIGTERM-ignoring worker after each test."""
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


def runtime():
    return {
        "expected_packages": {"vllm": "0.29.0", "nixl": "1.0.0"},
        "model_dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "block_size": 128,
        "environment": {"VLLM_ROCM_USE_AITER": "1"},
        "extra_args": ["--max-model-len", "4096", "--enforce-eager"],
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
        "NARWHAL_CACHE_CAPTURE_HOOK": str(ROOT / "tools/deployment/cache_capture_hook.py"),
        "NARWHAL_CACHE_CAPTURE_HOOK_SHA256": digest(
            ROOT / "tools/deployment/cache_capture_hook.py"
        ),
        "NARWHAL_ENGINE_PORT": "8000",
        "NARWHAL_ATTEST_PORT": "8010",
        "NARWHAL_NIXL_SIDE_CHANNEL_PORT": "5600",
        "NARWHAL_UCX_TCP_PORT_RANGE": "39000-39999",
        "NARWHAL_NODE_1_IP": "192.0.2.11",
        "NARWHAL_NODE_1_URL": "http://192.0.2.11:8000",
        "NARWHAL_ENGINE_MODEL_NAME": "synthetic",
        "NARWHAL_DEPLOYMENT_REVISION": "b" * 40,
        "NARWHAL_ENGINE_API_KEY": "engine-only-secret",
        "NARWHAL_NODE_1_SSH_PASSWORD": "management-only-secret",
    }
    return record, env
