"""Check installed entry points, package data and HTTP contracts outside the checkout."""

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import importlib.resources
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

import httpx

import narwhal
from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.launch_engine import load as load_engine_launch
from narwhal.deployment.launch_engine import prepare as prepare_engine_launch
from narwhal.observability.artifacts import FILES, stage_artifacts
from narwhal.observability.make_targets import TargetContract
from narwhal.serving.app import create_app


async def check_http():
    """Build an installed app and inspect its model, state and metrics routes."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        cfg = FleetConfig(
            model="package-smoke",
            engines=[EngineSpec("e", "http://127.0.0.1:1")],
            slo=SLO(1, 1),
            profiles_path=root / "profiles.json",
        )
        app = create_app(cfg, journal_path=root / "journal.jsonl")
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://installed"
            ) as client:
                models = await client.get("/v1/models")
                assert models.status_code == 200
                assert models.json()["data"][0]["id"] == "package-smoke"
                state = await client.get("/narwhal/state")
                assert state.status_code == 200
                assert state.json()["schema"] == "narwhal.state"
                metrics = await client.get("/metrics")
                assert metrics.status_code == 200
                assert "narwhal_" in metrics.text
        finally:
            await app.state.router.engines.aclose()


def check_deployment() -> None:
    """Prepare a shared-GPU launch from installed modules without the tools tree."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        model = root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        runtime = {
            "expected_packages": {"vllm": "0.29.0", "nixl": "1.0.0"},
            "model_dtype": "bfloat16",
            "kv_cache_dtype": "auto",
            "block_size": 128,
            "environment": {},
            "extra_args": ["--max-model-len", "4096", "--gpu-memory-utilization", "0.2"],
        }
        allocation = {
            "accelerator": "synthetic-gpu",
            "gpu_ids": ["GPU-test"],
            "tensor_parallel_size": 1,
            "gpu_visibility_env": "CUDA_VISIBLE_DEVICES",
            "accelerator_devices": ["/dev/nvidia0"],
            "network_mode": "host",
            "transfer": {"transport": "ucx_tcp", "net_devices": "lo", "devices": []},
            "sources": dict.fromkeys(("allocation", "devices", "transfer"), "package smoke"),
            "runtime": runtime,
            "shared_device": {
                "group": "local:GPU-test",
                "gpu_uuid": "GPU-test",
                "device_allowance": 0.9,
                "gpu_memory_utilization": 0.2,
            },
        }
        record = selected_launch(
            {
                "schema": "narwhal.engine-launch",
                "schema_version": 1,
                "engines": {"engine-1": allocation},
            },
            "engine-1",
            {},
        )
        source = root / "engine.json"
        source.write_text(json.dumps(record))
        hook = importlib.resources.files("narwhal.deployment").joinpath("cache_capture_hook.py")
        env = {
            "NARWHAL_ENGINE_LAUNCH_CONFIG": str(source),
            "NARWHAL_MODEL_DIR": str(model),
            "NARWHAL_MODEL_CONFIG_SHA256": hashlib.sha256(b"{}").hexdigest(),
            "NARWHAL_ENGINE_IMAGE": "sha256:" + "a" * 64,
            "NARWHAL_CACHE_CAPTURE_HOOK": str(hook),
            "NARWHAL_CACHE_CAPTURE_HOOK_SHA256": hashlib.sha256(hook.read_bytes()).hexdigest(),
            "NARWHAL_ENGINE_PORT": "8001",
            "NARWHAL_ATTEST_PORT": "8101",
            "NARWHAL_NIXL_SIDE_CHANNEL_PORT": "5601",
            "NARWHAL_UCX_TCP_PORT_RANGE": "39000-39999",
            "NARWHAL_NODE_1_IP": "127.0.0.1",
            "NARWHAL_NODE_1_URL": "http://127.0.0.1:8001",
            "NARWHAL_ENGINE_MODEL_NAME": "package-smoke",
            "NARWHAL_DEPLOYMENT_REVISION": "b" * 40,
        }
        run = root / "run"
        prepare_engine_launch(run, env)
        plan = load_engine_launch(run)
        assert (plan["endpoint"], plan["attestation_port"], plan["side_channel_port"]) == (
            "http://127.0.0.1:8001",
            8101,
            5601,
        )
        assert plan["shared_device"]["gpu_memory_utilization"] == 0.2
        assert plan["args"][-2:] == ["--gpu-memory-utilization", "0.2"]


def main(argv=None):
    """Check distribution identity, bundled files and every installed console command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    installed = Path(narwhal.__file__).resolve()
    assert not installed.is_relative_to((args.source_root / "src").resolve()), installed
    distribution = importlib.metadata.distribution("narwhal-inference")
    assert distribution.version == args.expected_version
    resources = importlib.resources.files("narwhal")
    for name in ("py.typed", "fleet.example.json"):
        assert resources.joinpath(name).is_file(), name
    assert (
        resources.joinpath("fleet.example.json").read_bytes()
        == (args.source_root / "config/fleet.example.json").read_bytes()
    )
    deployment = importlib.resources.files("narwhal.deployment")
    for name in (
        "attestation_contract",
        "cache_capture_hook",
        "checkpoint_manifest",
        "deploy_hosts",
        "discover_deployment",
        "engine_launch",
        "fabric_budget",
        "host_access",
        "launch_engine",
        "prepare_host_env",
    ):
        module = importlib.import_module(f"narwhal.deployment.{name}")
        packaged = Path(module.__file__).resolve()
        assert packaged.is_relative_to(Path(deployment).resolve()), name
        assert (
            packaged.read_bytes()
            == (args.source_root / "tools" / "deployment" / f"{name}.py").read_bytes()
        ), name
    native = importlib.import_module("narwhal.deployment.native_engine")
    assert Path(native.__file__).resolve().is_relative_to(Path(deployment).resolve())
    monitoring = importlib.resources.files("narwhal.observability")
    for name in ("compose.yml", *FILES):
        assert (
            monitoring.joinpath(name).read_bytes()
            == (args.source_root / "tools" / "observability" / name).read_bytes()
        ), name
    with tempfile.TemporaryDirectory() as folder:
        mounts = Path(folder) / "mounts"
        stage_artifacts(TargetContract("127.0.0.1:8000", (("e0", "127.0.0.1:8002"),)), mounts)
        for name in FILES.values():
            assert (mounts / name).is_file(), name
        for name in ("router.json", "engines.json"):
            assert (mounts / "prometheus" / "targets" / name).is_file(), name
    entries = sorted(
        (entry for entry in distribution.entry_points if entry.group == "console_scripts"),
        key=lambda entry: entry.name,
    )
    expected_entries = {
        "narwhal-attest",
        "narwhal-check",
        "narwhal-engine",
        "narwhal-observe",
        "narwhal-profile",
        "narwhal-serve",
    }
    assert {entry.name for entry in entries} == expected_entries
    for entry in entries:
        subprocess.run([entry.name, "--help"], check=True, timeout=30, capture_output=True)
    for module in ("narwhal.benchmarking", "narwhal.diagnostics.qualification", "narwhal.fleet"):
        assert importlib.util.find_spec(module) is None, module
    check_deployment()
    asyncio.run(check_http())
    print(f"Installed package passed: {len(entries)} commands, deployment, data and HTTP contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
