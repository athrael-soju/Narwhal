"""Every attestation capture of one engine, in an attempt directory of its own, and the
published attempt that the sidecar serves.

A container or native engine keeps its attempts in its launch directory. A Kubernetes engine
keeps them in its pod's directory, under the engine container they attest."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from ...command_results import redact_secrets
from ...serving.control_token import ROUTER_TOKEN_ENV
from ..launch_engine import pod as pod_steps
from ..launch_engine.backend import (
    CacheRegistration,
    LiveModelDimensions,
    engine_backend,
    plan_launcher,
)
from ..launch_engine.plan import API_KEY_ENV, launch_headers
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
from .pod_engine import (
    IDENTITY_RECORD,
    PodEngine,
    engine_home,
    engine_record,
    live_pod,
    write_identity,
)

# Each capture attempt is a new directory here; failed attempts stay as evidence.
ATTEMPTS = "attestation"
# Names the attempt whose captures passed; the sidecar serves that attempt's document.
PUBLISHED = "published-attestation.json"
# Written into an attempt when one of its captures fails.
FAILURE = "capture-failure.json"
# Beside a pod's attempts: why the sidecar shared no attempt, which the gates read.
REFUSED = "capture-refused.json"
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
    if isinstance(identity, PodEngine):
        # The engine container's serve step copies the engine's output for this process.
        data = engine_record(identity, pod_steps.STARTUP_LOG).read_bytes()
    elif isinstance(identity, str):
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
    # An engine may print its own arguments, its key among them, as it starts.
    text = data.decode("utf-8", errors="surrogateescape")
    data = redact_secrets(text, credentials(run, plan)).encode("utf-8", errors="surrogateescape")
    destination = attempt / STARTUP_LOG
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    return destination


def credentials(run: Path, plan: dict) -> set[str]:
    """The engine's key and the routers' control token, as far as this process holds them."""
    found = {os.environ.get(API_KEY_ENV, ""), os.environ.get(ROUTER_TOKEN_ENV, "")}
    try:
        headers = launch_headers(run, plan) or {}
    except (OSError, ValueError):
        # A launch with no environment file holds no key of its own to redact.
        headers = {}
    found.add(headers.get("Authorization", "").removeprefix("Bearer "))
    return {value for value in found if value}


def live_cache_layout(run: Path, plan: dict, checked: dict) -> Path:
    """The live engine's KV cache layout record: the one capture-cache kept in the launch
    directory, or the one the cache capture hook writes in a pod's engine process directory."""
    if plan.get("backend") == "kubernetes":
        return engine_record(live_pod(checked), pod_steps.CACHE_LAYOUT)
    return run / "cache-layout.json"


def captures(run: Path, attempt: Path, representative_layout: str | None = None) -> list[Capture]:
    """The captures of the engine in `run` into `attempt`, in the order `capture` runs them."""
    plan, checked, _ = checked_plan(run)
    engine = plan_launcher(plan)
    registration = engine if isinstance(engine, CacheRegistration) else None
    if representative_layout is not None:
        if registration is None:
            label = engine_backend(plan.get("engine")).label
            raise ValueError(
                f"{label} engines register no cache layout to match a representative's"
            )
        # The cache-registration capture registers the layout the live cache resolved.
        layout = registration.cache_layout(live_cache_layout(run, plan, checked))
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
    pod = plan.get("backend") == "kubernetes"
    steps = [
        *(
            [
                Capture(
                    "pod-identity",
                    IDENTITY_RECORD,
                    lambda: write_identity(live_pod(checked), attempt),
                )
            ]
            if pod
            else []
        ),
        Capture("startup-log", STARTUP_LOG, lambda: save_startup_log(run, attempt)),
        Capture("model-dimensions", dimensions, lambda: capture_model_dimensions(run, attempt)),
        *engine.attestation_captures(CaptureContext(run, attempt, plan)),
    ]
    steps.append(Capture("document", "engine-attestation.json", lambda: generate(run, attempt)))
    return steps


def attestation_home(run: Path, plan: dict, pod_evidence: Path | None) -> Path:
    """The directory that holds the engine's capture attempts and its published attempt.

    It is the launch directory of a container or native engine. For a Kubernetes plan it is the
    live engine container's directory in `pod_evidence`, the pod's directory of the state
    volume.
    """
    if not require_pod_evidence(plan, pod_evidence):
        return run
    assert pod_evidence is not None
    _, checked, _ = checked_plan(run)
    return engine_home(pod_evidence, live_pod(checked).identity.container_id)


def require_home(home: Path, attempt: Path) -> None:
    """Require a pod attempt to attest the engine container whose directory holds it; the
    container can start again while the attempt captures."""
    bound = read_json(attempt / IDENTITY_RECORD).get("container_id")
    if bound != home.name:
        raise ValueError(
            f"the attempt attests engine container {bound}, not container {home.name} whose "
            "directory holds it; the engine container started again during the capture"
        )


def require_pod_evidence(plan: dict, pod_evidence: Path | None) -> bool:
    """Return whether the plan keeps its attestation in its engine pod's directory, which
    `pod_evidence` must name exactly for a Kubernetes plan."""
    pod = plan.get("backend") == "kubernetes"
    if pod_evidence is not None and not pod:
        raise ValueError(
            "--pod-evidence names the directory of a Kubernetes engine pod; this plan's engine "
            "runs on a host"
        )
    if pod_evidence is None and pod:
        raise ValueError(
            "a Kubernetes plan's attestation binds to its engine pod and lies in the pod's "
            "directory of the state volume; pass --pod-evidence naming that directory"
        )
    return pod


def published_attempt(home: Path) -> Path:
    """The attempt directory whose document `home` published."""
    path = home / PUBLISHED
    if not path.is_file():
        raise ValueError(f"{PUBLISHED} is missing; capture the engine's attestation first")
    record = read_json(path)
    name = record.get("attempt")
    if not isinstance(name, str) or ATTEMPT_NAME.fullmatch(name) is None:
        raise ValueError(f"{PUBLISHED} names no capture attempt")
    attempt = home / ATTEMPTS / name
    document = attempt / "engine-attestation.json"
    if digest(document) != record["document_sha256"]:
        raise ValueError(f"{document} differs from the document {PUBLISHED} names")
    return attempt


def new_attempt(home: Path) -> Path:
    attempts = home / ATTEMPTS
    attempts.mkdir(mode=0o700, parents=True, exist_ok=True)
    attempt = attempts / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    attempt.mkdir(mode=0o700)
    return attempt


def refuse_published(home: Path) -> None:
    if (home / PUBLISHED).exists():
        raise AlreadyPublished(str(read_json(home / PUBLISHED).get("attempt")))


def _share(path: Path, mode: int, *, directory: bool) -> None:
    """Give `path` to the sidecar's group at `mode`, through a descriptor opened without
    following a link: the engine container can write the pod's directory, so a link there is
    refused, not followed."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else os.O_NONBLOCK)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(
            f"{path} is not the {'directory' if directory else 'file'} the "
            f"capture wrote, or a link: {error}"
        ) from error
    try:
        info = os.fstat(descriptor)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ValueError(
                f"{path} is not the {'directory' if directory else 'file'} the capture wrote"
            )
        os.fchown(descriptor, -1, os.getegid())
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _tree(attempt: Path) -> Iterator[tuple[Path, bool]]:
    """Each directory and file of `attempt`, refusing any link."""
    yield attempt, True
    for root, directories, files in os.walk(attempt, followlinks=False):
        for name in sorted(directories + files):
            path = Path(root) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{path} is a symbolic link; a capture writes none")
            yield path, stat.S_ISDIR(info.st_mode)


def holding(attempt: Path, secrets: Collection[str]) -> list[str]:
    """The files of `attempt` that hold any of `secrets`, by path within it."""
    needles = [secret.encode() for secret in secrets if secret]
    found = []
    for path, directory in _tree(attempt):
        if not directory and any(needle in path.read_bytes() for needle in needles):
            found.append(path.relative_to(attempt).as_posix())
    return found


def share(home: Path, attempt: Path, secrets: Collection[str] = ()) -> None:
    """Let the sidecar's own group read a pod's attempt, and nothing else of the sidecar's.

    The gate Jobs that copy and verify the published attempt run with that group as a
    supplementary one; the engine container, and any other user, reads none of it. An attempt
    whose files still hold the engine's key or the routers' token is not shared: the sidecar
    records why beside the attempts, for the gates to report, and the capture fails.
    """
    for folder in (home.parent, home, home / ATTEMPTS):
        _share(folder, 0o750, directory=True)
    leaked = holding(attempt, secrets)
    if leaked:
        message = (
            f"the capture attempt {attempt.name} holds the engine's credential in "
            f"{', '.join(leaked)}, so it stays private; nothing is published"
        )
        refusal = home / REFUSED
        refusal.unlink(missing_ok=True)
        write_private_json(refusal, {"attempt": attempt.name, "files": leaked, "error": message})
        _share(refusal, 0o640, directory=False)
        raise ValueError(message)
    for path, directory in _tree(attempt):
        _share(path, 0o750 if directory else 0o640, directory=directory)


def publish(home: Path, attempt: Path, *, shared: bool = False) -> Path:
    """Name `attempt` in `home`, unless another attempt published first. A `shared` record
    is readable by the sidecar's group, as `share` leaves the attempt."""
    if attempt.parent != home / ATTEMPTS or ATTEMPT_NAME.fullmatch(attempt.name) is None:
        raise ValueError(f"{attempt} is not a capture attempt of {home}")
    destination = home / PUBLISHED
    record = {
        "attempt": attempt.name,
        "document_sha256": digest(attempt / "engine-attestation.json"),
    }
    temporary = destination.with_name(destination.name + f".tmp-{uuid.uuid4().hex}")
    try:
        write_private_json(temporary, record)
        if shared:
            _share(temporary, 0o640, directory=False)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            refuse_published(home)
            raise
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def capture(
    run: Path, representative_layout: str | None = None, *, pod_evidence: Path | None = None
) -> Path:
    """Run every capture of the engine in `run` in a new attempt directory, and publish the
    attempt once its document is written. Return the published document.

    A Kubernetes plan's capture runs in the engine pod's attestation container, with
    `pod_evidence` naming the pod's directory.
    """
    # A wrong --pod-evidence is refused before the launch directory's check is read.
    require_pod_evidence(read_json(run / "launch.json"), pod_evidence)
    plan, _, _ = checked_plan(run)
    home = attestation_home(run, plan, pod_evidence)
    if pod_evidence is not None:
        # The engine container can write the pod's directory, so a link there is refused.
        for path in (home.parent, home):
            if path.is_symlink():
                raise ValueError(f"{path} is a symbolic link; the sidecar writes none there")
    refuse_published(home)
    attempt = new_attempt(home)
    try:
        steps = captures(run, attempt, representative_layout)
        if pod_evidence is not None:
            steps.append(
                Capture("pod-container", IDENTITY_RECORD, lambda: require_home(home, attempt))
            )
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
        if pod_evidence is not None:
            try:
                share(home, attempt, credentials(run, plan))
            except ValueError as error:
                write_private_json(
                    attempt / FAILURE, {"capture": "share", "output": REFUSED, "error": str(error)}
                )
                raise
        publish(home, attempt, shared=pod_evidence is not None)
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
