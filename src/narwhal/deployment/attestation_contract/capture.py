"""Capture live NIXL, model, transfer and HTTP evidence from a serving engine."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import httpx

from ...engines.attestation import parse_process_start
from ..launch_engine.captures import handshake_policy, registration_layout
from ..launch_engine.docker import run_runtime_script
from ..launch_engine.plan import read_env
from ..launch_engine.runtime import digest, write_private
from .document import generate
from .evidence import (
    checked_plan,
    live_container,
    live_native,
    parse_tagged_capture,
    read_json,
    require_binding,
    require_prior_dimensions,
    write_private_json,
)

NIXL_CAPTURE_TAG = "NARWHAL_NIXL_CAPTURE_V1:"
NIXL_CAPTURE = f"""import contextlib, hashlib, importlib, json, sys
from pathlib import Path
with contextlib.redirect_stdout(sys.stderr):
    module = importlib.import_module("vllm.distributed.kv_transfer.kv_connector.v1.nixl.metadata")
version = module.NIXL_CONNECTOR_VERSION
if type(version) is not int or version < 1:
    raise ValueError("NIXL_CONNECTOR_VERSION must be a positive integer")
source = Path(module.__file__)
record = {{"nixl_connector_version": version, "module": module.__name__,
          "constant": "NIXL_CONNECTOR_VERSION", "module_file": str(source),
          "module_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}}
print("\\n" + {NIXL_CAPTURE_TAG!r} + json.dumps(record), flush=True)
"""


def parse_nixl_capture(output: str) -> dict:
    return parse_tagged_capture(output, NIXL_CAPTURE_TAG, "NIXL")


MODEL_DIMENSIONS_CAPTURE_TAG = "NARWHAL_LIVE_MODEL_DIMENSIONS_V1:"
MODEL_DIMENSIONS_CAPTURE = f"""import hashlib, json, sys
from pathlib import Path
import launch_engine
plan_path = Path(sys.argv[2])
plan_data = plan_path.read_bytes()
plan_hash = hashlib.sha256(plan_data).hexdigest()
if plan_hash != sys.argv[1]:
    raise ValueError("mounted launch plan differs from the checked serving plan")
plan = json.loads(plan_data)
launcher_hash = hashlib.sha256(Path(launch_engine.__file__).read_bytes()).hexdigest()
if launcher_hash != plan["launcher_sha256"]:
    raise ValueError("mounted launcher differs from the checked serving plan")
model_hash = hashlib.sha256(Path(sys.argv[3]).read_bytes()).hexdigest()
if model_hash != plan["model_config_sha256"]:
    raise ValueError("mounted model configuration differs from the checked serving plan")
model = launch_engine.runtime_config(plan).model_config
methods = {{"head_size": "get_head_size", "kv_heads": "get_total_num_kv_heads",
           "hidden_layers": "get_total_num_hidden_layers"}}
values = {{field: getattr(model, method)() for field, method in methods.items()}}
if any(type(value) is not int or value < 1 for value in values.values()):
    raise ValueError("model contract getters must return positive integers")
architecture = model.architecture
if not isinstance(architecture, str) or not architecture.strip():
    raise ValueError("runtime model architecture is empty")
record = {{"contract": values,
          "sources": {{field: f"ModelConfig.{{method}}()" for field, method in methods.items()}},
          "model_architecture": architecture, "use_mla": model.use_mla,
          "model_config_sha256": model_hash, "plan_sha256": plan_hash,
          "launcher_sha256": launcher_hash, "image": plan["image"],
          "revision": plan["revision"]}}
print("\\n" + {MODEL_DIMENSIONS_CAPTURE_TAG!r} + json.dumps(record), flush=True)
"""


def capture_model_dimensions(run: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    native = plan.get("backend") == "native"
    identity = live_native(run, plan, checked) if native else live_container(run, checked)
    destination = run / "model-dimensions.live.json"
    if destination.exists():
        raise ValueError("Live model dimensions already exist; retain the capture")
    if native:
        values = read_env(run / "engine.env")
        command = [
            plan["python_executable"],
            "-c",
            MODEL_DIMENSIONS_CAPTURE,
            plan_hash,
            str(run / "hook/launch.json"),
            plan["model_config_path"],
        ]
    else:
        assert isinstance(identity, str)
        command = [
            "docker",
            "exec",
            "--env",
            "NARWHAL_CAPTURE_CACHE=0",
            identity,
            "python3",
            "-c",
            MODEL_DIMENSIONS_CAPTURE,
            plan_hash,
            "/narwhal-hooks/launch.json",
            "/model/config.json",
        ]
        values = {}
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **values, "NARWHAL_CAPTURE_CACHE": "0"} if native else None,
    )
    log = run / f"model-dimensions.live-{uuid.uuid4().hex}.log"
    write_private(log, result.stdout + "\nSTDERR\n" + result.stderr)
    if result.returncode:
        raise ValueError(f"Live model dimension inspection failed; inspect {log}")
    try:
        record = parse_tagged_capture(
            result.stdout, MODEL_DIMENSIONS_CAPTURE_TAG, "model dimensions"
        )
    except ValueError as exc:
        raise ValueError(f"{exc}; inspect {log}") from exc
    require_binding(
        record,
        "Live model dimensions",
        plan_sha256=plan_hash,
        image=plan["image"],
        revision=plan["revision"],
        model_config_sha256=plan["model_config_sha256"],
        launcher_sha256=plan["launcher_sha256"],
    )
    if (
        not isinstance(record.get("model_architecture"), str)
        or not record["model_architecture"].strip()
    ):
        raise ValueError("Live model dimensions lack the resolved architecture")
    contract = record.get("contract")
    if not isinstance(contract, dict) or any(
        type(contract.get(field)) is not int or contract[field] < 1
        for field in ("head_size", "kv_heads", "hidden_layers")
    ):
        raise ValueError("Live model dimensions contain invalid compatibility getters")
    require_prior_dimensions(run, plan, plan_hash, contract)
    if native:
        record.update(process=identity, model_revision=plan["model_revision"])
    else:
        record.update(image_id=checked["image_id"], container_id=identity)
    record["capture_log_sha256"] = digest(log)
    write_private_json(destination, record)
    return destination


def capture_nixl(run: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    native = plan.get("backend") == "native"
    identity = live_native(run, plan, checked) if native else live_container(run, checked)
    if native:
        command = [plan["python_executable"], "-c", NIXL_CAPTURE]
        values = read_env(run / "engine.env")
    else:
        assert isinstance(identity, str)
        command = ["docker", "exec", identity, "python3", "-c", NIXL_CAPTURE]
        values = {}
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **values, "NARWHAL_CAPTURE_CACHE": "0"} if native else None,
    )
    record = parse_nixl_capture(result.stdout)
    if (
        type(record.get("nixl_connector_version")) is not int
        or record["nixl_connector_version"] < 1
    ):
        raise ValueError("Pinned connector returned an invalid protocol version")
    record["plan_sha256"] = plan_hash
    if native:
        record["process"] = identity
    else:
        record.update(image_id=checked["image_id"], container_id=identity)
    destination = run / "nixl-connector-version.json"
    write_private_json(destination, record)
    return destination


def capture_native_transfer_mode(run: Path) -> Path:
    """Resolve the installed connector class and its NIXL transfer direction."""
    plan, checked, plan_hash = checked_plan(run)
    if plan.get("backend") != "native":
        raise ValueError("native transfer capture requires a native launch plan")
    process = live_native(run, plan, checked)
    script = """import hashlib, inspect, json, sys
from pathlib import Path
from vllm.config import KVTransferConfig
from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
config = KVTransferConfig(**json.loads(sys.argv[1]))
connector = KVConnectorFactory.get_connector_class(config)
name = connector.__name__
if name in ('NixlConnector', 'NixlPullConnector'):
    mode = 'pull'
elif name == 'NixlPushConnector':
    mode = 'push'
else:
    raise ValueError('Unrecognized NIXL connector class: ' + name)
source = Path(inspect.getfile(connector))
print('NARWHAL_TRANSFER_MODE=' + json.dumps({
    'transfer_mode': mode, 'connector_class': connector.__module__ + '.' + name,
    'module_file': str(source), 'module_sha256': hashlib.sha256(source.read_bytes()).hexdigest()
}))
"""
    output = run_runtime_script(
        run, plan, script, [json.dumps(plan["connector"])], "transfer-mode.log"
    )
    record = parse_tagged_capture(output, "NARWHAL_TRANSFER_MODE=", "transfer mode")
    record.update(plan_sha256=plan_hash, process=process)
    destination = run / "transfer-mode.json"
    write_private_json(destination, record)
    return destination


def capture_native_http(run: Path) -> None:
    """Retain the HTTP identity and metrics for the checked native process."""
    plan, checked, _ = checked_plan(run)
    if plan.get("backend") != "native":
        raise ValueError("native HTTP capture requires a native launch plan")
    live_native(run, plan, checked)
    values = read_env(run / "engine.env")
    key = values.get("VLLM_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else None
    with httpx.Client(timeout=10, headers=headers) as client:
        version_response = client.get(plan["endpoint"].rstrip("/") + "/version")
        version_response.raise_for_status()
        metrics_response = client.get(plan["endpoint"].rstrip("/") + "/metrics")
        metrics_response.raise_for_status()
    version = version_response.json()
    if version.get("version") != checked["vllm_api_version"]:
        raise ValueError("native HTTP version differs from the checked runtime")
    if (
        parse_process_start(metrics_response.text)
        != read_json(run / "shared-start.json")["process_start_time_seconds"]
    ):
        raise ValueError("native HTTP process start differs from the launch record")
    write_private_json(run / "version.json", version)
    write_private(run / "metrics.txt", metrics_response.text)


def capture_native(run: Path) -> Path:
    """Capture native engine evidence and return the engine attestation path."""
    plan, checked, _ = checked_plan(run)
    if plan.get("backend") != "native":
        raise ValueError("native capture requires a native launch plan")
    live_native(run, plan, checked)
    cache = run / "cache-layout.json"
    if not cache.is_file():
        raise ValueError("native engine did not capture its live KV cache layout")
    capture_model_dimensions(run)
    capture_nixl(run)
    registration_layout(run, plan, cache, True)
    handshake_policy(run, plan)
    capture_native_transfer_mode(run)
    capture_native_http(run)
    snapshot = run / "startup-attestation.log"
    write_private(snapshot, (run / "startup.log").read_text())
    return generate(run, snapshot)
