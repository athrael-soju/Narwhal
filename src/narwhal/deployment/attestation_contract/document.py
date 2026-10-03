"""Assemble and write the engine attestation document from checked evidence."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from ...config import EngineContract
from ...engines.attestation import AttestationDocument, parse_process_start
from ..launch_engine.runtime import digest
from .evidence import (
    checked_plan,
    live_container,
    live_native,
    read_json,
    require_binding,
    require_prior_dimensions,
    write_private_json,
)


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


def engine_document(run: Path, startup_log: Path) -> dict:
    plan, checked, plan_hash = checked_plan(run)
    native = plan.get("backend") == "native"
    identity = live_native(run, plan, checked) if native else live_container(run, checked)
    original_dimensions = run / "model-dimensions.json"
    live_dimensions = run / "model-dimensions.live.json"
    dimension_source = live_dimensions if live_dimensions.exists() else original_dimensions
    dimensions = read_json(dimension_source)
    registration = read_json(run / "cache-registration.json")
    require_binding(
        dimensions,
        "Model dimensions",
        plan_sha256=plan_hash,
        image=plan["image"],
        model_config_sha256=plan["model_config_sha256"],
        revision=plan["revision"],
    )
    if dimension_source == live_dimensions:
        binding = (
            {"process": identity, "model_revision": plan["model_revision"]}
            if native
            else {"image_id": checked["image_id"], "container_id": identity}
        )
        require_binding(
            dimensions, "Live model dimensions", launcher_sha256=plan["launcher_sha256"], **binding
        )
        require_prior_dimensions(run, plan, plan_hash, dimensions.get("contract"))
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
    binding = (
        {"process": identity}
        if native
        else {"image_id": checked["image_id"], "container_id": identity}
    )
    require_binding(nixl, "NIXL protocol", plan_sha256=plan_hash, **binding)
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
    model_path = Path(plan["model_dir"]) / "config.json"
    if digest(model_path) != plan["model_config_sha256"]:
        raise ValueError("Model configuration changed after launch preparation")
    architecture = dimensions.get("model_architecture")
    if not isinstance(architecture, str) or not architecture.strip():
        raise ValueError("Capture live model dimensions for the resolved architecture")
    version = read_json(run / "version.json").get("version")
    if version != checked.get("vllm_api_version"):
        raise ValueError("HTTP version differs from the checked runtime")
    process_start = parse_process_start((run / "metrics.txt").read_text())
    if (
        native
        and process_start != read_json(run / "shared-start.json")["process_start_time_seconds"]
    ):
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
    packages = plan["expected_packages"]
    nixl_version = packages.get("nixl") or packages.get("nixl-rocm")
    if not isinstance(nixl_version, str) or not nixl_version:
        raise ValueError("Checked image lacks a pinned NIXL package")
    contract = {
        "vllm_version": version,
        "image_digest": "" if native else checked["image_id"],
        "nixl_version": nixl_version,
        "nixl_connector_version": nixl["nixl_connector_version"],
        "model_architecture": architecture,
        "model_dtype": option(plan["args"], "--dtype"),
        **dimensions["contract"],
        "attention_backend": attention_backends(startup_log.read_text()),
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
    if set(contract) != set(EngineContract().fields()):
        raise ValueError("Derived contract fields differ from the attestation schema")
    if contract["transfer_mode"] not in {"pull", "push"}:
        raise ValueError("Resolved NIXL transfer mode must be pull or push")
    declared = EngineContract(**contract)
    if declared.missing():
        raise ValueError("Derived engine contract is incomplete: " + ", ".join(declared.missing()))
    evidence = {
        "vllm_version": run / "version.json",
        "image_digest": run / "checked.json",
        "nixl_version": run / "launch.json",
        "nixl_connector_version": run / "nixl-connector-version.json",
        "model_architecture": dimension_source,
        "model_dtype": run / "launch.json",
        "kv_heads": dimension_source,
        "head_size": dimension_source,
        "hidden_layers": dimension_source,
        "attention_backend": startup_log,
        "kv_cache_dtype": run / "launch.json",
        "cross_layers_blocks": run / "cache-registration.json",
        "hybrid_kv_cache_manager": run / "cache-layout.json",
        "connector": run / "launch.json",
        "kv_role": run / "launch.json",
        "transfer_mode": run / "transfer-mode.json",
        "speculative_config": run / "launch.json",
        "enforce_handshake_compat": run / "handshake-policy.json",
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
        "cache_capture_sha256": plan.get("cache_capture_sha256", ""),
        "ucx_version": checked.get("ucx_version") or "",
        "peer_release": checked.get("peer_release") is True,
    }
    return {
        "schema": "narwhal.attestation",
        "schema_version": 1,
        "contract": contract,
        "sources": sources,
        "launch": launch,
    }


# Flags whose values change per launch.
_PER_LAUNCH_VALUES = frozenset(("--host", "--port", "--served-model-name", "--kv-events-config"))


def launch_args(args: list[str]) -> list[str]:
    """Return engine arguments with the values of per-launch flags removed."""
    kept: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        kept.append(arg)
        skip = arg in _PER_LAUNCH_VALUES
    return kept


def generate(run: Path, startup_log: Path) -> Path:
    record = engine_document(run, startup_log)
    role = read_json(run / "launch.json")["role"]
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("Serving plan has an invalid engine role")
    destination = run / "engine-attestation.json"
    temporary = destination.with_name(destination.name + f".tmp-{uuid.uuid4().hex}")
    try:
        write_private_json(temporary, record)
        AttestationDocument.load(temporary)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
