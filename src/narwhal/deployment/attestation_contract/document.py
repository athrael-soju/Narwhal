from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ...config import EngineContract
from ...contracts import ATTESTATION, versioned
from ...engines.attestation import AttestationDocument
from ..launch_engine.backend import (
    LiveModelDimensions,
    cache_capture,
    engine_backend,
    plan_launcher,
)
from ..launch_engine.runtime import digest
from .evidence import (
    checked_plan,
    live_binding,
    live_identity,
    read_json,
    require_binding,
    require_prior_dimensions,
    write_private_json,
)

# Contract fields shared code derives for every backend; the backend supplies the rest.
MODEL_FIELDS = ("model_architecture", "kv_heads", "head_size", "hidden_layers")


@dataclass(frozen=True)
class AttestationContext:
    run: Path
    plan: dict
    checked: dict
    plan_hash: str
    # The live container ID, or the native process identity.
    identity: dict | str
    startup_log: Path
    dimensions: dict

    @property
    def native(self) -> bool:
        return self.plan.get("backend") == "native"


@dataclass(frozen=True)
class AttestationEvidence:
    engine_version: object
    # Fields besides the engine version, image, transfer version and model dimensions.
    contract: dict
    # The evidence file behind the engine version and each field in contract.
    sources: dict[str, Path]
    launch: dict = field(default_factory=dict)


def launch_args(args: list[str], per_launch: frozenset[str]) -> list[str]:
    """Return engine arguments with the values of the `per_launch` flags removed."""
    kept: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        kept.append(arg)
        skip = arg in per_launch
    return kept


def bound_dimensions(
    run: Path, plan: dict, checked: dict, plan_hash: str, identity: dict | str
) -> tuple[dict, Path]:
    """Return the bound model dimensions and their file, live ones when the backend has them."""
    live = run / "model-dimensions.live.json"
    capable = isinstance(plan_launcher(plan), LiveModelDimensions)
    source = live if capable and live.exists() else run / "model-dimensions.json"
    dimensions = read_json(source)
    require_binding(
        dimensions,
        "Model dimensions",
        plan_sha256=plan_hash,
        image=plan["image"],
        model_config_sha256=plan["model_config_sha256"],
        revision=plan["revision"],
    )
    if source == live:
        binding = live_binding(plan, checked, identity, model_revision=True)
        require_binding(
            dimensions, "Live model dimensions", launcher_sha256=plan["launcher_sha256"], **binding
        )
        require_prior_dimensions(run, plan, plan_hash, dimensions.get("contract"))
    if digest(Path(plan["model_dir"]) / "config.json") != plan["model_config_sha256"]:
        raise ValueError("Model configuration changed after launch preparation")
    architecture = dimensions.get("model_architecture")
    if not isinstance(architecture, str) or not architecture.strip():
        raise ValueError(
            "Capture live model dimensions for the resolved architecture"
            if capable
            else "Model dimensions name no architecture"
        )
    return dimensions, source


def engine_document(run: Path, startup_log: Path) -> dict:
    plan, checked, plan_hash = checked_plan(run)
    engine = plan_launcher(plan)
    native = plan.get("backend") == "native"
    identity = live_identity(run, plan, checked)
    dimensions, dimension_source = bound_dimensions(run, plan, checked, plan_hash, identity)
    evidence = engine.attestation_evidence(
        AttestationContext(run, plan, checked, plan_hash, identity, startup_log, dimensions)
    )
    if evidence.engine_version != checked.get("engine_version"):
        raise ValueError("HTTP version differs from the checked runtime")
    package = engine.transfer_package(plan)
    transfer_version = plan["expected_packages"].get(package) if package else None
    if not isinstance(transfer_version, str) or not transfer_version:
        raise ValueError("Checked image lacks a pinned transfer package")
    values = {
        "engine_version": evidence.engine_version,
        "image_digest": "" if native else checked["image_id"],
        "transfer_version": transfer_version,
        "model_architecture": dimensions["model_architecture"],
        **dimensions["contract"],
        **evidence.contract,
    }
    names = EngineContract().fields()
    if set(values) != set(names):
        raise ValueError("Derived contract fields differ from the attestation schema")
    contract = {name: values[name] for name in names}
    attested = engine_backend(plan.get("engine")).identity.contract_fields
    missing = EngineContract(**contract).missing(attested)
    if missing:
        raise ValueError("Derived engine contract is incomplete: " + ", ".join(missing))
    paths = {
        "image_digest": run / "checked.json",
        "transfer_version": run / "launch.json",
        **dict.fromkeys(MODEL_FIELDS, dimension_source),
        **evidence.sources,
    }
    if native:
        paths.pop("image_digest")
    sources = {
        name: f"{paths[name].resolve()} sha256:{digest(paths[name])}"
        for name in names
        if name in paths
    }
    launch = {
        "args": launch_args(plan["args"], engine.launch_varying_flags),
        "image_id": "" if native else checked["image_id"],
        "expected_packages": plan["expected_packages"],
        "model_config_sha256": plan["model_config_sha256"],
        "model_revision": plan.get("model_revision", ""),
        "launch_sha256": plan.get("launch_sha256", ""),
        "launcher_code_sha256": plan["launcher_code_sha256"],
    }
    if cache_capture(engine) is not None:
        launch["cache_capture_code_sha256"] = plan["cache_capture_code_sha256"]
    launch.update(evidence.launch)
    return versioned(ATTESTATION, {"contract": contract, "sources": sources, "launch": launch})


def generate(run: Path, startup_log: Path) -> Path:
    plan = read_json(run / "launch.json")
    record = engine_document(run, startup_log)
    role = plan["role"]
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
