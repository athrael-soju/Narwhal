from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ...config import EngineContract
from ...deployment.attestation_contract.document import generate
from ...deployment.attestation_contract.evidence import (
    checked_plan,
    live_container,
    live_native,
    read_json,
    require_binding,
    write_private_json,
)
from ...deployment.launch_engine.plan import env_file_name, read_env
from ...deployment.launch_engine.runtime import digest, write_private
from ...engines.host_process import container_start, native_start
from .identity import ATTESTED_FIELDS, server_version
from .plan import TRANSFER_PACKAGES, option
from .runtime import API_KEY_ENV

# /server_info fields that hold credentials.
SECRET_FIELDS = ("api_key", "admin_api_key")
# Server arguments recorded in the contract.
SERVER_FIELDS = (
    "version",
    "dtype",
    "kv_cache_dtype",
    "attention_backend",
    "page_size",
    "disaggregation_transfer_backend",
    "speculative_algorithm",
    "max_running_requests",
)
# Flags whose values change per launch.
_PER_LAUNCH_VALUES = frozenset(
    ("--host", "--port", "--served-model-name", "--kv-events-config", "--model-path")
)


def model_contract(config: dict) -> dict[str, Any]:
    text = config.get("text_config", config)
    heads = text.get("num_attention_heads")
    head_size = text.get("head_dim") or (
        text["hidden_size"] // heads if isinstance(heads, int) and heads > 0 else None
    )
    values = {
        "kv_heads": text.get("num_key_value_heads", heads),
        "head_size": head_size,
        "hidden_layers": text.get("num_hidden_layers"),
    }
    if any(type(value) is not int or value < 1 for value in values.values()):
        raise ValueError("model config lacks positive KV head, head size and layer counts")
    architectures = config.get("architectures") or []
    if not architectures or not isinstance(architectures[0], str):
        raise ValueError("model config names no architecture")
    return {"contract": values, "model_architecture": architectures[0]}


def model_dimensions(run: Path, plan: dict) -> Path:
    destination = run / "model-dimensions.json"
    path = Path(plan["model_dir"]) / "config.json"
    if digest(path) != plan["model_config_sha256"]:
        raise ValueError("Model configuration changed after launch preparation")
    record = {
        **model_contract(json.loads(path.read_text())),
        "sources": {"model_config": f"{path.resolve()} sha256:{digest(path)}"},
        "plan_sha256": digest(run / "launch.json"),
        "image": plan["image"],
        "revision": plan["revision"],
        "model_config_sha256": plan["model_config_sha256"],
    }
    if destination.exists():
        if read_json(destination) != record:
            raise ValueError("Model dimensions differ from the retained capture")
        return destination
    write_private_json(destination, record)
    return destination


def capture_model_dimensions(run: Path) -> Path:
    plan, _, _ = checked_plan(run)
    return model_dimensions(run, plan)


def _live(run: Path, plan: dict, checked: dict) -> object:
    return (
        live_native(run, plan, checked)
        if plan.get("backend") == "native"
        else live_container(run, checked)
    )


def capture_server(run: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    identity = _live(run, plan, checked)
    destination = run / "server-info.json"
    if destination.exists():
        raise ValueError("Server evidence already exists; retain the capture")
    key = read_env(run / env_file_name(plan)).get(API_KEY_ENV, "")
    headers = {"Authorization": f"Bearer {key}"} if key else None
    base = plan["endpoint"].rstrip("/")
    with httpx.Client(timeout=10, headers=headers) as client:
        info = client.get(f"{base}/server_info")
        info.raise_for_status()
    payload = info.json()
    if not isinstance(payload, dict):
        raise ValueError("/server_info returned no object")
    if server_version(payload) != checked["sglang_version"]:
        raise ValueError("HTTP version differs from the checked runtime")
    server = {name: payload.get(name) for name in SERVER_FIELDS}
    record = {
        "server": server,
        "process_start_time_seconds": (
            native_start(identity["pid"])
            if isinstance(identity, dict)
            else container_start(str(identity))
        ),
        "plan_sha256": plan_hash,
        **(
            {"process": identity}
            if plan.get("backend") == "native"
            else {"image_id": checked["image_id"], "container_id": identity}
        ),
    }
    if any(name in json.dumps(record) for name in SECRET_FIELDS):
        raise ValueError("Server evidence must not hold credentials")
    write_private_json(destination, record)
    return destination


def capture_native(run: Path) -> Path:
    plan, _, _ = checked_plan(run)
    if plan.get("backend") != "native":
        raise ValueError("native capture requires a native launch plan")
    model_dimensions(run, plan)
    capture_server(run)
    snapshot = run / "startup-attestation.log"
    write_private(snapshot, (run / "startup.log").read_text())
    return generate(run, snapshot)


def launch_args(args: list[str]) -> list[str]:
    kept: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        kept.append(arg)
        skip = arg in _PER_LAUNCH_VALUES
    return kept


def engine_document(run: Path, startup_log: Path) -> dict:
    plan, checked, plan_hash = checked_plan(run)
    native = plan.get("backend") == "native"
    identity = _live(run, plan, checked)
    dimensions = read_json(run / "model-dimensions.json")
    require_binding(
        dimensions,
        "Model dimensions",
        plan_sha256=plan_hash,
        image=plan["image"],
        model_config_sha256=plan["model_config_sha256"],
        revision=plan["revision"],
    )
    server_path = run / "server-info.json"
    captured = read_json(server_path)
    binding = (
        {"process": identity}
        if native
        else {"image_id": checked["image_id"], "container_id": identity}
    )
    require_binding(captured, "Server evidence", plan_sha256=plan_hash, **binding)
    server = captured["server"]
    transfer = plan["connector"]
    if server.get("version") != checked["sglang_version"]:
        raise ValueError("HTTP version differs from the checked runtime")
    if server.get("disaggregation_transfer_backend") != transfer["transfer_backend"]:
        raise ValueError("Live transfer backend differs from the launch plan")
    if str(server.get("page_size")) != option(plan["args"], "--page-size"):
        raise ValueError("Live page size differs from the launch plan")
    env = read_env(run / env_file_name(plan))
    contract = {
        "engine_version": server["version"],
        "image_digest": "" if native else checked["image_id"],
        "transfer_version": plan["expected_packages"][
            TRANSFER_PACKAGES[transfer["transfer_backend"]]
        ],
        "connector_version": 0,
        "model_architecture": dimensions["model_architecture"],
        "model_dtype": server["dtype"],
        **dimensions["contract"],
        "attention_backend": server["attention_backend"],
        "kv_cache_dtype": server["kv_cache_dtype"],
        "cross_layers_blocks": None,
        "hybrid_kv_cache_manager": None,
        "connector": transfer["transfer_backend"],
        "kv_role": "",
        "transfer_mode": "",
        "speculative_config": server.get("speculative_algorithm") or "disabled",
        "enforce_handshake_compat": None,
    }
    missing = EngineContract(**contract).missing(ATTESTED_FIELDS)
    if missing:
        raise ValueError("Derived engine contract is incomplete: " + ", ".join(missing))
    evidence = {
        "engine_version": server_path,
        "image_digest": run / "checked.json",
        "transfer_version": run / "launch.json",
        "model_architecture": run / "model-dimensions.json",
        "model_dtype": server_path,
        "kv_heads": run / "model-dimensions.json",
        "head_size": run / "model-dimensions.json",
        "hidden_layers": run / "model-dimensions.json",
        "attention_backend": server_path,
        "kv_cache_dtype": server_path,
        "connector": run / "launch.json",
        "speculative_config": server_path,
    }
    if native:
        evidence.pop("image_digest")
    sources = {field: f"{path.resolve()} sha256:{digest(path)}" for field, path in evidence.items()}
    launch = {
        "args": launch_args(plan["args"]),
        "image_id": "" if native else checked["image_id"],
        "expected_packages": plan["expected_packages"],
        "model_config_sha256": plan["model_config_sha256"],
        "model_revision": plan.get("model_revision", ""),
        "launch_sha256": plan.get("launch_sha256", ""),
        "launcher_sha256": plan.get("launcher_sha256", ""),
        "bootstrap": {
            "bootstrap_host": env["NARWHAL_SGLANG_BOOTSTRAP_HOST"],
            "bootstrap_port": int(env["NARWHAL_SGLANG_BOOTSTRAP_PORT"]),
        },
        "role_switch": transfer["role_switch"],
        **(
            {"decode_cuda_graph_memory_gb": transfer["decode_cuda_graph_memory_gb"]}
            if "decode_cuda_graph_memory_gb" in transfer
            else {}
        ),
    }
    return {
        "schema": "narwhal.attestation",
        "schema_version": 1,
        "contract": contract,
        "sources": sources,
        "launch": launch,
    }
