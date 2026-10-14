from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import httpx

from ...deployment.attestation_contract import document
from ...deployment.attestation_contract.document import AttestationContext, AttestationEvidence
from ...deployment.attestation_contract.evidence import (
    checked_plan,
    live_binding,
    live_container,
    live_native,
    parse_tagged_capture,
    read_json,
    read_shared_start,
    require_binding,
    require_prior_dimensions,
    write_private_json,
)
from ...deployment.launch_engine.docker import run_runtime_script
from ...deployment.launch_engine.plan import launch_headers, read_env
from ...deployment.launch_engine.runtime import digest, write_private
from .captures import handshake_policy, registration_layout
from .identity import parse_process_start

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
    record.update(live_binding(plan, checked, identity, model_revision=True))
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
    record.update(live_binding(plan, checked, identity))
    destination = run / "nixl-connector-version.json"
    write_private_json(destination, record)
    return destination


def capture_native_transfer_mode(run: Path) -> Path:
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
    plan, checked, _ = checked_plan(run)
    if plan.get("backend") != "native":
        raise ValueError("native HTTP capture requires a native launch plan")
    live_native(run, plan, checked)
    with httpx.Client(timeout=10, headers=launch_headers(run, plan)) as client:
        version_response = client.get(plan["endpoint"].rstrip("/") + "/version")
        version_response.raise_for_status()
        metrics_response = client.get(plan["endpoint"].rstrip("/") + "/metrics")
        metrics_response.raise_for_status()
    version = version_response.json()
    if version.get("version") != checked["engine_version"]:
        raise ValueError("native HTTP version differs from the checked runtime")
    if (
        parse_process_start(metrics_response.text)
        != read_shared_start(run)["process_start_time_seconds"]
    ):
        raise ValueError("native HTTP process start differs from the launch record")
    write_private_json(run / "version.json", version)
    write_private(run / "metrics.txt", metrics_response.text)


def capture_native(run: Path) -> Path:
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
    return document.generate(run, snapshot)


def option(args: list[str], name: str) -> str:
    if args.count(name) != 1:
        raise ValueError(f"Serving plan requires one {name} argument")
    index = args.index(name)
    if index + 1 == len(args):
        raise ValueError(f"Serving plan has no value for {name}")
    return args[index + 1]


def attention_backends(log: str) -> str:
    names = set()
    for pattern in (
        r"\bUsing (?:AttentionBackendEnum\.)?([A-Za-z][A-Za-z0-9_]*) (?:attention )?backend\b",
        r"\bOverriding with ([A-Za-z][A-Za-z0-9_]*) out of potential backends\b",
    ):
        names.update(re.findall(pattern, log))
    if not names:
        raise ValueError(
            "Serving startup log has no resolved attention backend; retain the complete Docker log"
        )
    return ",".join(sorted(names))


def nixl_package(plan: dict) -> str | None:
    packages = plan["expected_packages"]
    return next((name for name in ("nixl", "nixl-rocm") if packages.get(name)), None)


def attestation_evidence(context: AttestationContext) -> AttestationEvidence:
    run, plan, checked, plan_hash = context.run, context.plan, context.checked, context.plan_hash
    native, identity = context.native, context.identity
    registration = read_json(run / "cache-registration.json")
    require_binding(registration, "Cache registration", plan_sha256=plan_hash, image=plan["image"])
    cache = read_json(run / "cache-layout.json")
    require_binding(
        cache,
        "Live cache layout",
        plan_sha256=plan_hash,
        image=plan["image"],
        model_config_sha256=plan["model_config_sha256"],
        launch_config_sha256=plan["launch_sha256"],
    )
    nixl = read_json(run / "nixl-connector-version.json")
    require_binding(
        nixl, "NIXL protocol", plan_sha256=plan_hash, **live_binding(plan, checked, identity)
    )
    transfer = read_json(run / "transfer-mode.json")
    transfer_binding = {"process": identity} if native else {"image_id": checked["image_id"]}
    require_binding(transfer, "Transfer mode", plan_sha256=plan_hash, **transfer_binding)
    handshake = read_json(run / "handshake-policy.json")
    require_binding(
        handshake,
        "Handshake policy",
        plan_sha256=plan_hash,
        image=plan["image"],
        enforce_handshake_compat=True,
    )
    if handshake.get("connector_config") != plan["connector"]:
        raise ValueError("Handshake policy uses another connector configuration")
    version = read_json(run / "version.json").get("version")
    process_start = parse_process_start((run / "metrics.txt").read_text())
    if native and process_start != read_shared_start(run)["process_start_time_seconds"]:
        raise ValueError("HTTP process start differs from the native launch record")
    ranks = cache.get("ranks", [])
    tp = int(option(plan["args"], "--tensor-parallel-size"))
    if sorted(rank.get("rank") for rank in ranks) != list(range(tp)):
        raise ValueError("Live cache layout lacks a TP rank")
    kinds = [{layer["kind"] for layer in rank["layers"]} for rank in ranks]
    if any(group != kinds[0] for group in kinds):
        raise ValueError("Cache kinds differ between TP ranks")
    if (
        registration["kv_cache_layout"] not in {rank.get("kv_cache_layout") for rank in ranks}
        or len({rank.get("kv_cache_layout") for rank in ranks}) != 1
    ):
        raise ValueError("Resolved cache registration differs from the live cache layout")
    if transfer["transfer_mode"] not in {"pull", "push"}:
        raise ValueError("Resolved NIXL transfer mode must be pull or push")
    contract = {
        "connector_version": nixl["nixl_connector_version"],
        "model_dtype": option(plan["args"], "--dtype"),
        "attention_backend": attention_backends(context.startup_log.read_text()),
        "kv_cache_dtype": option(plan["args"], "--kv-cache-dtype"),
        "cross_layers_blocks": registration["cross_layers_blocks"],
        "hybrid_kv_cache_manager": len(kinds[0]) > 1,
        "connector": plan["connector"]["kv_connector"],
        "kv_role": plan["connector"]["kv_role"],
        "transfer_mode": transfer["transfer_mode"],
        "speculative_config": (
            option(plan["args"], "--speculative-config")
            if "--speculative-config" in plan["args"]
            else "disabled"
        ),
        "enforce_handshake_compat": handshake["enforce_handshake_compat"],
    }
    sources = {
        "engine_version": run / "version.json",
        "connector_version": run / "nixl-connector-version.json",
        "model_dtype": run / "launch.json",
        "attention_backend": context.startup_log,
        "kv_cache_dtype": run / "launch.json",
        "cross_layers_blocks": run / "cache-registration.json",
        "hybrid_kv_cache_manager": run / "cache-layout.json",
        "connector": run / "launch.json",
        "kv_role": run / "launch.json",
        "transfer_mode": run / "transfer-mode.json",
        "speculative_config": run / "launch.json",
        "enforce_handshake_compat": run / "handshake-policy.json",
    }
    launch = {
        "ucx_version": checked.get("ucx_version") or "",
        "peer_release": checked.get("peer_release") is True,
    }
    return AttestationEvidence(version, contract, sources, launch)


# Flags whose values change per launch.
LAUNCH_VARYING_FLAGS = frozenset(("--host", "--port", "--served-model-name", "--kv-events-config"))
