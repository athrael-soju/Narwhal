"""Every attestation capture of one container or native engine, in an attempt directory of its
own, and the published attempt that the sidecar serves."""

from __future__ import annotations

import os
import re
import subprocess
import time
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
from ..launch_engine.runtime import digest
from .document import STARTUP_LOG, generate
from .evidence import (
    capture_model_dimensions,
    checked_plan,
    container_start,
    live_identity,
    read_json,
    write_private_json,
)

# Each capture attempt is a new directory here; failed attempts stay as evidence.
ATTEMPTS = "attestation"
# Names the attempt whose captures passed; the sidecar serves that attempt's document.
PUBLISHED = "published-attestation.json"
# Written into an attempt when one of its captures fails.
FAILURE = "capture-failure.json"
# An attempt's name: its UTC creation time and a random suffix.
ATTEMPT_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
DOCKER_LOGS_SECONDS = 120


@dataclass(frozen=True)
class CaptureContext:
    # The launch directory, read for the engine's launch and start evidence.
    run: Path
    # The attempt directory, which receives every capture.
    attempt: Path
    plan: dict


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
        self.reason = reason


class AlreadyPublished(ValueError):
    def __init__(self, attempt: str) -> None:
        super().__init__(
            f"Attempt {attempt} already published this engine's attestation; capture again "
            "only for a fresh launch"
        )
        self.attempt = attempt


# Errors a capture raises for its evidence or the engine it reads; anything else is a defect.
CAPTURE_ERRORS = (OSError, ValueError, KeyError, subprocess.SubprocessError, httpx.HTTPError)


def save_startup_log(run: Path, attempt: Path) -> Path:
    """Write the startup log of the live engine's current process into the attempt."""
    plan, checked, _ = checked_plan(run)
    identity = live_identity(run, plan, checked)
    if isinstance(identity, str):
        # `docker logs` also returns earlier runs of the container; keep only this one.
        since = container_start(run)["started_at"]
        result = subprocess.run(
            ["docker", "logs", "--since", since, identity],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=DOCKER_LOGS_SECONDS,
            check=False,
        )
        if result.returncode:
            lines = result.stdout.decode(errors="replace").strip().splitlines()
            raise ValueError(
                f"docker logs exited {result.returncode}: {lines[-1] if lines else 'no output'}"
            )
        data = result.stdout
    else:
        # The engine keeps appending to its startup.log; the document attests a fixed copy.
        data = (run / "startup.log").read_bytes()
    destination = attempt / STARTUP_LOG
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    return destination


def captures(run: Path, attempt: Path, representative_layout: str | None = None) -> list[Capture]:
    """The captures of the engine in `run` into `attempt`, in the order `capture` runs them."""
    plan, _, _ = checked_plan(run)
    engine = plan_launcher(plan)
    registration = engine if isinstance(engine, CacheRegistration) else None
    if representative_layout is not None:
        if registration is None:
            label = engine_backend(plan.get("engine")).label
            raise ValueError(
                f"{label} engines register no cache layout to match a representative's"
            )
        # The cache-registration capture registers the layout the live cache resolved.
        layout = registration.cache_layout(run)
        if layout != representative_layout:
            raise ValueError(
                f"Resolved layout {layout} differs from the representative's "
                f"{representative_layout}"
            )
    dimensions = (
        "model-dimensions.live.json"
        if isinstance(engine, LiveModelDimensions)
        else "model-dimensions.json"
    )
    steps = [
        Capture("startup-log", STARTUP_LOG, lambda: save_startup_log(run, attempt)),
        Capture("model-dimensions", dimensions, lambda: capture_model_dimensions(run, attempt)),
        *engine.attestation_captures(CaptureContext(run, attempt, plan)),
    ]
    steps.append(Capture("document", "engine-attestation.json", lambda: generate(run, attempt)))
    return steps


def published_attempt(run: Path) -> Path:
    """The attempt directory whose document the launch directory published."""
    path = run / PUBLISHED
    if not path.is_file():
        raise ValueError(f"{PUBLISHED} is missing; capture the engine's attestation first")
    record = read_json(path)
    name = record.get("attempt")
    if not isinstance(name, str) or ATTEMPT_NAME.fullmatch(name) is None:
        raise ValueError(f"{PUBLISHED} names no capture attempt")
    attempt = run / ATTEMPTS / name
    document = attempt / "engine-attestation.json"
    if digest(document) != record["document_sha256"]:
        raise ValueError(f"{document} differs from the document {PUBLISHED} names")
    return attempt


def new_attempt(run: Path) -> Path:
    attempts = run / ATTEMPTS
    attempts.mkdir(mode=0o700, exist_ok=True)
    attempt = attempts / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    attempt.mkdir(mode=0o700)
    return attempt


def refuse_published(run: Path) -> None:
    if (run / PUBLISHED).exists():
        raise AlreadyPublished(str(read_json(run / PUBLISHED).get("attempt")))


def publish(run: Path, attempt: Path) -> Path:
    """Name `attempt` in the launch directory, unless another attempt published first."""
    if attempt.parent != run / ATTEMPTS or ATTEMPT_NAME.fullmatch(attempt.name) is None:
        raise ValueError(f"{attempt} is not a capture attempt of {run}")
    destination = run / PUBLISHED
    record = {
        "attempt": attempt.name,
        "document_sha256": digest(attempt / "engine-attestation.json"),
    }
    temporary = destination.with_name(destination.name + f".tmp-{uuid.uuid4().hex}")
    try:
        write_private_json(temporary, record)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            refuse_published(run)
            raise
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def capture(run: Path, representative_layout: str | None = None) -> Path:
    """Run every capture of the engine in `run` in a new attempt directory, and publish the
    attempt once its document is written. Return the published document."""
    refuse_published(run)
    attempt = new_attempt(run)
    try:
        steps = captures(run, attempt, representative_layout)
    except BaseException:
        # Nothing was captured: a refused input leaves no attempt behind.
        attempt.rmdir()
        raise
    for step in steps:
        try:
            step.action()
        except CAPTURE_ERRORS as error:
            failed = CaptureFailed(step, error)
            write_private_json(
                attempt / FAILURE,
                {"capture": step.name, "output": step.output, "error": failed.reason},
            )
            raise failed from error
    try:
        publish(run, attempt)
    except AlreadyPublished as error:
        # Another attempt passed and published while this one ran.
        write_private_json(
            attempt / FAILURE,
            {
                "capture": "publish",
                "output": PUBLISHED,
                "error": "another attempt published first",
                "published": error.attempt,
            },
        )
        raise
    return attempt / "engine-attestation.json"
