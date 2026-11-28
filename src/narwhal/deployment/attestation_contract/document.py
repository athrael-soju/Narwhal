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
from ..launch_engine.plan import check_model_files, model_file_changed
from ..launch_engine.runtime import digest
from .evidence import (
    LiveEngine,
    checked_plan,
    engine_image,
    live_binding,
    live_identity,
    planned_model_config,
    read_json,
    require_binding,
    require_prior_dimensions,
    write_private_json,
)
from .pod_engine import PodEngine, require_identity, require_model_files

# The startup log an attempt captures and its document attests.
STARTUP_LOG = "startup.log"
# Contract fields shared code derives for every backend; the backend supplies the rest.
MODEL_FIELDS = ("model_architecture", "kv_heads", "head_size", "hidden_layers")


@dataclass(frozen=True)
class AttestationContext:
    # The launch directory, which holds the engine's launch and start evidence.
    run: Path
    # The capture attempt directory, which holds every attestation capture.
    attempt: Path
    plan: dict
    checked: dict
    plan_hash: str
    # The live container ID, the native process identity, or the pod's engine container.
    identity: LiveEngine
    dimensions: dict

    @property
    def startup_log(self) -> Path:
        return self.attempt / STARTUP_LOG

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
    # The serving process start the evidence attests, for an engine that reports one.
    process_start: float | None = None


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
    run: Path, attempt: Path, plan: dict, checked: dict, plan_hash: str, identity: LiveEngine
) -> tuple[dict, Path]:
    """Return the attempt's bound model dimensions and their file, live ones when the backend
    reads them from the live engine."""
    capable = isinstance(plan_launcher(plan), LiveModelDimensions)
    source = attempt / ("model-dimensions.live.json" if capable else "model-dimensions.json")
    dimensions = read_json(source)
    require_binding(
        dimensions,
        "Model dimensions",
        plan_sha256=plan_hash,
        image=plan["image"],
        model_config_sha256=plan["model_config_sha256"],
        revision=plan["revision"],
    )
    if capable:
        binding = live_binding(plan, checked, identity, model_revision=True)
        require_binding(
            dimensions, "Live model dimensions", launcher_sha256=plan["launcher_sha256"], **binding
        )
        require_prior_dimensions(run, plan, plan_hash, dimensions.get("contract"))
    if digest(planned_model_config(plan)) != plan["model_config_sha256"]:
        raise ValueError("Model configuration changed after launch preparation")
    if isinstance(identity, PodEngine):
        # The pod's checkpoint init container hashed the model files it serves.
        require_model_files(checked)
    else:
        if model_file_changed(plan):
            raise ValueError("Model file changed after launch preparation")
        check_model_files(plan)
    architecture = dimensions.get("model_architecture")
    if not isinstance(architecture, str) or not architecture.strip():
        raise ValueError("Model dimensions name no architecture")
    return dimensions, source


def planned_launch(plan: dict, image: str) -> dict:
    """The attested launch evidence a launch plan decides, before the live captures add theirs.

    It leaves out what differs between two prepares of one launch record, so a fresh
    launch directory of an unchanged record attests the same launch.
    """
    engine = plan_launcher(plan)
    launch = {
        "args": launch_args(plan["args"], engine.launch_varying_flags),
        "image_id": image,
        "expected_packages": plan["expected_packages"],
        "model_config_sha256": plan["model_config_sha256"],
        **({"model_sha256": plan["model_sha256"]} if "model_sha256" in plan else {}),
        "model_tree_sha256": plan["model_tree_sha256"],
        "model_revision": plan.get("model_revision", ""),
        "launch_sha256": plan.get("launch_sha256", ""),
        "launcher_code_sha256": plan["launcher_code_sha256"],
    }
    if "config_files" in plan:
        launch["config_files"] = plan["config_files"]
    if cache_capture(engine) is not None:
        launch["cache_capture_code_sha256"] = plan["cache_capture_code_sha256"]
    return launch


def engine_document(run: Path, attempt: Path) -> dict:
    """The attestation document of the live engine of `run` from the captures in `attempt`."""
    return attested(run, attempt)[0]


def attested(run: Path, attempt: Path) -> tuple[dict, float | None]:
    """The attestation document of the live engine of `run` from the captures in `attempt`,
    and the serving process start the captures attest, for an engine that reports one."""
    plan, checked, plan_hash = checked_plan(run)
    engine = plan_launcher(plan)
    native = plan.get("backend") == "native"
    identity = live_identity(run, plan, checked)
    dimensions, dimension_source = bound_dimensions(
        run, attempt, plan, checked, plan_hash, identity
    )
    evidence = engine.attestation_evidence(
        AttestationContext(run, attempt, plan, checked, plan_hash, identity, dimensions)
    )
    if evidence.engine_version != checked.get("engine_version"):
        raise ValueError("HTTP version differs from the checked runtime")
    package = engine.transfer_package(plan)
    transfer_version = plan["expected_packages"].get(package) if package else None
    if not isinstance(transfer_version, str) or not transfer_version:
        raise ValueError("Checked image lacks a pinned transfer package")
    image = engine_image(checked, identity)
    values = {
        "engine_version": evidence.engine_version,
        "image_digest": image,
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
        # A pod reports the image it runs; the image check observes a container's.
        "image_digest": (
            require_identity(identity, attempt)
            if isinstance(identity, PodEngine)
            else run / "checked.json"
        ),
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
    launch = planned_launch(plan, image)
    launch.update(evidence.launch)
    document = versioned(ATTESTATION, {"contract": contract, "sources": sources, "launch": launch})
    return document, evidence.process_start


def generate(run: Path, attempt: Path) -> Path:
    plan = read_json(run / "launch.json")
    record = engine_document(run, attempt)
    role = plan["role"]
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("Serving plan has an invalid engine role")
    destination = attempt / "engine-attestation.json"
    temporary = destination.with_name(destination.name + f".tmp-{uuid.uuid4().hex}")
    try:
        write_private_json(temporary, record)
        AttestationDocument.load(temporary)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
