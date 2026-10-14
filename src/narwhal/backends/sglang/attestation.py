from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from ...deployment.attestation_contract import document
from ...deployment.attestation_contract.document import AttestationContext, AttestationEvidence
from ...deployment.attestation_contract.evidence import (
    checked_plan,
    config_model_dimensions,
    live_binding,
    live_identity,
    read_json,
    require_binding,
    write_private_json,
)
from ...deployment.launch_engine.plan import env_file_name, launch_headers, option, read_env
from ...deployment.launch_engine.runtime import write_private
from ...engines.host_process import container_start, native_start
from .identity import SERVER_INFO_PATH, server_version

# /server_info fields that hold credentials.
SECRET_FIELDS = ("api_key", "admin_api_key")
# /server_info fields kept in the server evidence.
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
LAUNCH_VARYING_FLAGS = frozenset(
    ("--host", "--port", "--served-model-name", "--kv-events-config", "--model-path")
)


def capture_server(run: Path) -> Path:
    plan, checked, plan_hash = checked_plan(run)
    identity = live_identity(run, plan, checked)
    destination = run / "server-info.json"
    if destination.exists():
        raise ValueError("Server evidence already exists; retain the capture")
    headers = launch_headers(run, plan)
    base = plan["endpoint"].rstrip("/")
    with httpx.Client(timeout=10, headers=headers) as client:
        info = client.get(f"{base}{SERVER_INFO_PATH}")
        info.raise_for_status()
    payload = info.json()
    if not isinstance(payload, dict):
        raise ValueError("/server_info returned no object")
    if server_version(payload) != checked["engine_version"]:
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
        **live_binding(plan, checked, identity),
    }
    if any(name in json.dumps(record) for name in SECRET_FIELDS):
        raise ValueError("Server evidence must not hold credentials")
    write_private_json(destination, record)
    return destination


def capture_native(run: Path) -> Path:
    plan, _, _ = checked_plan(run)
    if plan.get("backend") != "native":
        raise ValueError("native capture requires a native launch plan")
    config_model_dimensions(run, plan)
    capture_server(run)
    snapshot = run / "startup-attestation.log"
    write_private(snapshot, (run / "startup.log").read_text())
    return document.generate(run, snapshot)


# The scheduler logs the request cap it serves with, which can sit below the requested one.
_STARTUP_CAP = re.compile(r"\bmax_running_requests=(\d+)")


def check_request_cap(args: list[str], startup_log: str) -> None:
    requested = option(args, "--max-running-requests")
    served = {int(value) for value in _STARTUP_CAP.findall(startup_log)}
    if requested is not None and served and served != {int(requested)}:
        raise ValueError(
            f"The engine serves at most {min(served)} running requests, below its "
            f"--max-running-requests {requested}. Lower --max-running-requests to "
            f"{min(served)}, or raise the capacity the startup log names."
        )


def attestation_evidence(context: AttestationContext) -> AttestationEvidence:
    run, plan, checked = context.run, context.plan, context.checked
    check_request_cap(plan["args"], context.startup_log.read_text())
    server_path = run / "server-info.json"
    captured = read_json(server_path)
    require_binding(
        captured,
        "Server evidence",
        plan_sha256=context.plan_hash,
        **live_binding(plan, checked, context.identity),
    )
    server = captured["server"]
    transfer = plan["connector"]
    if server.get("disaggregation_transfer_backend") != transfer["transfer_backend"]:
        raise ValueError("Live transfer backend differs from the launch plan")
    if str(server.get("page_size")) != option(plan["args"], "--page-size"):
        raise ValueError("Live page size differs from the launch plan")
    env = read_env(run / env_file_name(plan))
    contract = {
        "connector_version": 0,
        "model_dtype": server["dtype"],
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
    sources = {
        "engine_version": server_path,
        "model_dtype": server_path,
        "attention_backend": server_path,
        "kv_cache_dtype": server_path,
        "connector": run / "launch.json",
        "speculative_config": server_path,
    }
    launch = {
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
    return AttestationEvidence(server.get("version"), contract, sources, launch)
