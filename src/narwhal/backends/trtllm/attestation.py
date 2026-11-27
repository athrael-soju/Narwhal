from __future__ import annotations

import json
from pathlib import Path

import httpx

from ...deployment.attestation_contract.capture import Capture, CaptureContext
from ...deployment.attestation_contract.document import AttestationContext, AttestationEvidence
from ...deployment.attestation_contract.evidence import (
    checked_plan,
    engine_process_start,
    live_binding,
    live_identity,
    read_json,
    require_binding,
    write_private_json,
)
from ...deployment.launch_engine.plan import launch_headers, option
from .identity import SERVER_INFO_PATH, VERSION_PATH
from .plan import CONFIG_FILE

# /server_info fields kept in the server evidence.
SERVER_FIELDS = ("tokens_per_block", "max_batch_size", "kv_cache_hash_algo")
# Flags whose values change per launch. A native launch's config path names its run directory.
LAUNCH_VARYING_FLAGS = frozenset(("--host", "--port", "--served_model_name", "--config"))


def capture_server(run: Path, out: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    identity = live_identity(run, plan, checked)
    destination = out / "server-info.json"
    if destination.exists():
        raise ValueError("Server evidence already exists; retain the capture")
    headers = launch_headers(run, plan)
    base = plan["endpoint"].rstrip("/")
    with httpx.Client(timeout=10, headers=headers) as client:
        version = client.get(f"{base}{VERSION_PATH}")
        version.raise_for_status()
        info = client.get(f"{base}{SERVER_INFO_PATH}")
        info.raise_for_status()
    reported = version.json().get("version") if isinstance(version.json(), dict) else None
    if reported != checked["engine_version"]:
        raise ValueError("HTTP version differs from the checked runtime")
    payload = info.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{SERVER_INFO_PATH} returned no object")
    record = {
        "server": {"version": reported, **{name: payload.get(name) for name in SERVER_FIELDS}},
        "process_start_time_seconds": engine_process_start(identity),
        "plan_sha256": plan_hash,
        **live_binding(plan, checked, identity),
    }
    write_private_json(destination, record)
    return destination


def attestation_captures(context: CaptureContext) -> list[Capture]:
    run, attempt = context.run, context.attempt
    return [Capture("server-info", "server-info.json", lambda: capture_server(run, attempt))]


def attestation_evidence(context: AttestationContext) -> AttestationEvidence:
    run, plan, checked = context.run, context.plan, context.checked
    server_path = context.attempt / "server-info.json"
    captured = read_json(server_path)
    require_binding(
        captured,
        "Server evidence",
        plan_sha256=context.plan_hash,
        **live_binding(plan, checked, context.identity),
    )
    server = captured["server"]
    config = json.loads(plan["config_files"][CONFIG_FILE])
    blocks = config.get("kv_cache_config", {}).get("tokens_per_block")
    if blocks is not None and server.get("tokens_per_block") != blocks:
        raise ValueError("Live cache block size differs from the launch plan")
    if str(server.get("max_batch_size")) != option(plan["args"], "--max_batch_size"):
        raise ValueError("Live batch limit differs from the launch plan")
    transfer = plan["connector"]
    if config["cache_transceiver_config"]["backend"] != transfer["transfer_backend"]:
        raise ValueError("Launch configuration transfer backend differs from the launch plan")
    contract = {
        "connector_version": 0,
        "model_dtype": config["dtype"],
        "attention_backend": "",
        "kv_cache_dtype": option(plan["args"], "--kv_cache_dtype") or "",
        "cross_layers_blocks": None,
        "hybrid_kv_cache_manager": None,
        "connector": transfer["transfer_backend"],
        "kv_role": "",
        "transfer_mode": "",
        "speculative_config": "disabled"
        if "speculative_config" not in config
        else json.dumps(config["speculative_config"], sort_keys=True),
        "enforce_handshake_compat": None,
    }
    sources = {
        "engine_version": server_path,
        "model_dtype": run / "launch.json",
        "kv_cache_dtype": run / "launch.json",
        "connector": run / "launch.json",
        "speculative_config": run / "launch.json",
    }
    return AttestationEvidence(server.get("version"), contract, sources)
