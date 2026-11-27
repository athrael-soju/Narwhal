"""Every attestation capture of one container or native engine, then its document."""

from __future__ import annotations

import functools
import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..launch_engine.backend import (
    CacheRegistration,
    LiveModelDimensions,
    engine_backend,
    plan_launcher,
)
from .document import generate
from .evidence import capture_model_dimensions, checked_plan, live_identity


@dataclass(frozen=True)
class CaptureContext:
    run: Path
    plan: dict
    # The startup log the document attests, saved by the startup-log capture.
    startup_log: Path


@dataclass(frozen=True)
class Capture:
    name: str
    # The file the capture writes, or the evidence it checks.
    output: str
    action: Callable[[], object]


class CaptureFailed(ValueError):
    def __init__(self, capture: Capture, cause: Exception) -> None:
        reason = f"missing field {cause}" if isinstance(cause, KeyError) else str(cause)
        super().__init__(f"Capture {capture.name} ({capture.output}) failed: {reason}")
        self.capture = capture.name


# Errors a capture raises for its own evidence; anything else is a defect.
CAPTURE_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    subprocess.SubprocessError,
    httpx.HTTPError,
)


def startup_log(run: Path, plan: dict) -> Path:
    """The startup log a document attests: a native engine's snapshot, or the container log."""
    return run / ("startup-attestation.log" if plan.get("backend") == "native" else "startup.log")


def save_startup_log(run: Path) -> Path:
    plan, checked, _ = checked_plan(run)
    identity = live_identity(run, plan, checked)
    destination = startup_log(run, plan)
    if isinstance(identity, str):
        result = subprocess.run(
            ["docker", "logs", identity],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode:
            lines = result.stdout.decode(errors="replace").strip().splitlines()
            raise ValueError(
                f"docker logs exited {result.returncode}: {lines[-1] if lines else 'no output'}"
            )
        data = result.stdout
    else:
        # The engine keeps appending to startup.log; the document attests a fixed copy.
        data = (run / "startup.log").read_bytes()
    temporary = destination.with_name(destination.name + f".tmp-{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def match_representative(engine: CacheRegistration, run: Path, expected: str) -> None:
    actual = engine.registered_layout(run)
    if actual != expected:
        raise ValueError(f"Resolved layout {actual} differs from the representative's {expected}")


def captures(run: Path, representative_layout: str | None = None) -> list[Capture]:
    """The captures of the engine in `run`, in the order `capture` runs them."""
    plan, _, _ = checked_plan(run)
    engine = plan_launcher(plan)
    registration = engine if isinstance(engine, CacheRegistration) else None
    if representative_layout is not None and registration is None:
        label = engine_backend(plan.get("engine")).label
        raise ValueError(f"{label} engines register no cache layout to match a representative's")
    log = startup_log(run, plan)
    dimensions = (
        "model-dimensions.live.json"
        if isinstance(engine, LiveModelDimensions)
        else "model-dimensions.json"
    )
    steps = [
        Capture("startup-log", log.name, lambda: save_startup_log(run)),
        Capture("model-dimensions", dimensions, lambda: capture_model_dimensions(run)),
        *engine.attestation_captures(CaptureContext(run, plan, log)),
    ]
    if representative_layout is not None and registration is not None:
        steps.append(
            Capture(
                "representative-layout",
                "cache-registration.json",
                functools.partial(match_representative, registration, run, representative_layout),
            )
        )
    steps.append(Capture("document", "engine-attestation.json", lambda: generate(run, log)))
    return steps


def capture(run: Path, representative_layout: str | None = None) -> Path:
    """Run every capture of the engine in `run` and write its attestation document."""
    for step in captures(run, representative_layout):
        try:
            step.action()
        except CAPTURE_ERRORS as error:
            raise CaptureFailed(step, error) from error
    return run / "engine-attestation.json"
