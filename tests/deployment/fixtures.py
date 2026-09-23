"""Reusable synthetic engine allocation and serving inputs."""

import hashlib
import json
from pathlib import Path

from tools.deployment.engine_launch import selected_launch
from tools.deployment.launch_engine import digest

ROOT = Path(__file__).resolve().parents[2]


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
