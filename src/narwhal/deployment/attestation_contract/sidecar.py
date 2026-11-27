from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

import httpx

from ...engines.attestation import (
    AttestationDocument,
    attested_launch,
    fetch_engine_identity,
    verify_attestation,
)
from ...engines.attestation import main as attest_main
from ...engines.pod_identity import (
    ENGINE_CONTAINER,
    START_TOLERANCE_S,
    EngineNotRunning,
    container_status,
    read_pod,
)
from ..launch_engine import pod as pod_steps
from ..launch_engine.backend import engine_backend
from ..launch_engine.plan import env_file_name, launch_headers, read_env
from ..launch_engine.runtime import digest, write_private
from .capture import PUBLISHED, capture, published_attempt, require_pod_evidence
from .document import attested
from .evidence import checked_plan, live_container, live_native, read_json
from .pod_engine import (
    CONTAINERS,
    IDENTITY_RECORD,
    PodEngine,
    engine_home,
    live_pod,
    pod_path,
)

# How often a pod's sidecar looks again for the check of its pod and its engine's readiness.
POD_POLL_S = 5.0


def residency_arguments(plan: dict, checked: dict) -> list[str]:
    events = plan.get("kv_events")
    if events is None or not checked.get("prefix_caching") or not checked.get("kv_events"):
        return []
    model = plan["args"][plan["args"].index("--served-model-name") + 1]
    return ["--kv-events", events["socket_dir"], "--model", model]


def attestation_url(plan: dict) -> tuple[SplitResult, str, int]:
    """The sidecar route of the plan's role, with its host and port.

    A Kubernetes plan pins the route; other plans read NARWHAL_NODE_<n>_ATTESTATION_URL.
    """
    role = plan.get("role", "")
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("Serving plan has an invalid engine role")
    name = f"NARWHAL_NODE_{role.split('-')[1]}_ATTESTATION_URL"
    pinned = plan.get("backend") == "kubernetes"
    url = urlsplit(plan.get("attestation_url", "") if pinned else os.environ.get(name, ""))
    if url.scheme != "http" or url.path != "/v1/attestation" or not url.hostname or not url.port:
        raise ValueError(f"{name} must name the sidecar route")
    return url, url.hostname, url.port


def require_live_process_start(run: Path, plan: dict, process_start: float) -> None:
    """Require the live engine to report the process start its captures attest; an engine
    process can start again inside a container that keeps running."""
    reader = engine_backend(plan.get("engine")).identity
    live = asyncio.run(
        fetch_engine_identity(plan["endpoint"], headers=launch_headers(run, plan), reader=reader)
    )
    if live.process_start_time_seconds != process_start:
        raise ValueError(
            f"The engine reports process start {live.process_start_time_seconds}, not the "
            f"{process_start} its attestation captured; the engine process started again, "
            "so prepare, check and start a fresh launch"
        )


def hca_arguments(run: Path, plan: dict, engine: PodEngine | None = None) -> list[str]:
    """Return `--hca` when the plan sends KV through its one HCA port, even on its own host.

    UCX then has no shared-memory device and no GPU IPC, with `cuda_copy` on CUDA or
    `rocm_copy` on ROCm, so KV between engines on one host crosses their HCAs, and preflight
    checks that through the port's transmit counter. A Kubernetes engine sends through the HCA
    its pod was allocated, `engine`'s.
    """
    path = run / env_file_name(plan)
    if digest(path) != plan.get("env_sha256"):
        raise ValueError(f"{path.name} differs from the serving plan; prepare a fresh launch")
    env = read_env(path)
    if plan.get("backend") == "kubernetes":
        if engine is None:
            raise ValueError("a Kubernetes engine sends KV through the HCA of its pod")
        env = engine.environment(env)
    transports = set(env.get("UCX_TLS", "").split(","))
    devices = env.get("UCX_NET_DEVICES", "").split(",")
    if (
        "rc" in transports
        and len({"cuda_copy", "rocm_copy"} & transports) == 1
        and not {"cuda", "cuda_ipc", "rocm", "rocm_ipc"} & transports
        and env.get("UCX_SHM_DEVICES") == "none"
        and len(devices) == 1
        and re.fullmatch(r"[A-Za-z0-9_.-]+:[1-9][0-9]{0,2}", devices[0])
    ):
        return ["--hca", devices[0]]
    return []


def engine_ready(run: Path, plan: dict) -> bool:
    try:
        with httpx.Client(timeout=5, headers=launch_headers(run, plan)) as client:
            return client.get(plan["endpoint"].rstrip("/") + "/health").status_code == 200
    except httpx.HTTPError:
        return False


def checked_pod(run: Path) -> dict:
    """Return the check of this pod from its launch directory; raise when the launch directory
    holds another plan than the pod runs, or the check of another pod."""
    hooks = pod_path(pod_steps.HOOKS) / "launch.json"
    if digest(run / "launch.json") != digest(hooks):
        raise ValueError(f"{run} holds another launch plan than the {hooks} this pod runs")
    _, checked, _ = checked_plan(run)
    own = os.environ.get(pod_steps.POD_UID_ENV, "")
    if checked["pod_uid"] != own:
        raise ValueError(
            f"{run / 'checked.json'} checks pod {checked['pod_uid']}, not this pod {own}; "
            "check this pod's records in a launch directory of its own"
        )
    return checked


class _Waiting:
    """Prints each new reason a pod's sidecar waits for, once."""

    def __init__(self) -> None:
        self.reported = ""

    def __call__(self, reason: str) -> None:
        if reason != self.reported:
            print(f"narwhal-attest: {reason}", flush=True)
            self.reported = reason
        time.sleep(POD_POLL_S)


def await_launch(run: Path, pod_evidence: Path) -> None:
    """Wait until the launch directory holds its plan and the check of this pod.

    The operator places the launch directory that checked the pod's records on the state
    volume once the pod's init containers have recorded them, so it appears after the pod
    starts. A plan whose engine runs on a host is refused once it appears.
    """
    wait = _Waiting()
    reason = f"waiting for {run}, the launch directory that checks this pod's records"
    while True:
        try:
            plan = read_json(run / "launch.json")
            break
        except (OSError, ValueError):
            # Absent, or still being copied.
            wait(reason)
    require_pod_evidence(plan, pod_evidence)
    while not (run / "checked.json").is_file():
        wait(reason)


def await_pod_engine(run: Path, plan: dict) -> PodEngine:
    """Wait until the engine container runs and answers /health, and return it."""
    wait = _Waiting()
    while True:
        try:
            engine = live_pod(checked_pod(run))
        except EngineNotRunning as error:
            wait(f"waiting for the engine: {error}")
            continue
        if engine_ready(run, plan):
            return engine
        wait(f"waiting for {plan['endpoint']} to answer /health")


def pod_attempt(run: Path, plan: dict, pod_evidence: Path) -> tuple[Path, PodEngine]:
    """Return the published attempt of the pod's live engine container, captured now unless the
    container published one before this sidecar started, and the engine container."""
    engine = await_pod_engine(run, plan)
    home = engine_home(pod_evidence, engine.identity.container_id)
    if not (home / PUBLISHED).exists():
        print(
            f"narwhal-attest: capturing the attestation of engine container "
            f"{engine.identity.container_id}",
            flush=True,
        )
        capture(run, pod_evidence=pod_evidence)
    return published_attempt(home), engine


def serve(run: Path, pod_evidence: Path | None = None) -> int:
    """Serve the published attestation of the live engine.

    In a Kubernetes engine pod, the sidecar first waits for the check of its pod and the
    engine's readiness, and captures the engine container's attestation unless that container
    published one already. It stops once the container's first process ends.
    """
    if pod_evidence is not None:
        await_launch(run, pod_evidence)
    plan = read_json(run / "launch.json")
    backend = engine_backend(plan.get("engine"))
    if require_pod_evidence(plan, pod_evidence):
        assert pod_evidence is not None
        attempt, engine = pod_attempt(run, plan, pod_evidence)
        plan, checked, _ = checked_plan(run)
        process = ["--engine-pod-container", engine.identity.container_id]
        _, _, port = attestation_url(plan)
        # Under host networking the pod's address is its node's.
        host = engine.identity.pod_ip
        pod_engine: PodEngine | None = engine
    else:
        pod_engine = None
        plan, checked, _ = checked_plan(run)
        if plan.get("backend") == "native":
            process = ["--engine-pid", str(live_native(run, plan, checked)["pid"])]
        else:
            process = ["--engine-container", live_container(run, checked)]
        _, host, port = attestation_url(plan)
        attempt = published_attempt(run)
    destination = attempt / "engine-attestation.json"
    AttestationDocument.load(destination)
    document, process_start = attested(run, attempt)
    if read_json(destination) != document:
        raise ValueError("Attestation document differs from current serving evidence")
    if process_start is not None:
        require_live_process_start(run, plan, process_start)
    arguments = [
        "--document",
        str(destination),
        "--engine-base",
        plan["endpoint"],
        "--host",
        host,
        "--port",
        str(port),
        "--backend",
        backend.name,
    ]
    # A pod's sidecar follows its engine container's first process, and stops once it ends.
    if not backend.identity.reports_process_start or plan.get("backend") == "kubernetes":
        arguments += process
    return attest_main(
        arguments + residency_arguments(plan, checked) + hca_arguments(run, plan, pod_engine)
    )


def pod_verification(pod_evidence: Path, checked: dict) -> tuple[Path, dict]:
    """Return the attestation directory of the pod's live engine container and the identity
    its published attempt records, checked against the pod the Kubernetes API reports."""
    recorded = [
        read_json(published_attempt(published.parent) / IDENTITY_RECORD)
        for published in sorted((pod_evidence / CONTAINERS).glob(f"*/{PUBLISHED}"))
    ]
    pods = {(record["namespace"], record["pod"], record["pod_uid"]) for record in recorded}
    if not pods:
        raise ValueError(
            f"no engine container of pod {checked['pod_uid']} has published an attestation in "
            f"{pod_evidence / CONTAINERS}"
        )
    if len(pods) != 1 or next(iter(pods))[2] != checked["pod_uid"]:
        raise ValueError(f"{pod_evidence} holds attestations of pods other than the checked one")
    [(namespace, name, uid)] = pods
    pod = read_pod(namespace, name)
    if pod.get("metadata", {}).get("uid") != uid:
        raise ValueError(f"pod {namespace}/{name} is no longer the checked pod {uid}")
    live = container_status(pod, ENGINE_CONTAINER)
    home = engine_home(pod_evidence, live["container_id"])
    if not (home / PUBLISHED).exists():
        raise ValueError(
            f"engine container {live['container_id']} of pod {namespace}/{name} has published "
            "no attestation; its sidecar captures one once the engine answers /health"
        )
    record = read_json(published_attempt(home) / IDENTITY_RECORD)
    for field in ("container_id", "image_id", "started_at"):
        if live[field] != record[field]:
            raise ValueError(
                f"the engine container's {field} is {live[field]}, not the {record[field]} its "
                "attestation binds"
            )
    return home, record


def verify(run: Path, pod_evidence: Path | None = None) -> Path:
    """Check the running sidecar against the live engine and the document's contract, keeping
    the sidecar's responses and the live identity in a new attestation-check-* directory.

    For a Kubernetes plan, the pod's live engine container must be the one its attestation
    binds: the Kubernetes API reports its container ID, image ID and start, and the sidecar
    serves its process start.
    """
    plan = read_json(run / "launch.json")
    recorded = None
    if require_pod_evidence(plan, pod_evidence):
        assert pod_evidence is not None
        _, checked, _ = checked_plan(run)
        home, recorded = pod_verification(pod_evidence, checked)
    else:
        home = run
    document = AttestationDocument.load(published_attempt(home) / "engine-attestation.json")
    url, _, _ = attestation_url(plan)
    route = url.geturl()
    base = f"{url.scheme}://{url.netloc}"
    out = Path(tempfile.mkdtemp(prefix="attestation-check-", dir=home))
    responses = {}
    with httpx.Client(timeout=15) as client:
        for name, path in (("health", "/health"), ("attestation", "/v1/attestation")):
            response = client.get(base + path)
            write_private(out / f"{name}.json", response.text)
            write_private(out / f"{name}.status", f"{response.status_code}\n")
            responses[name] = response
    for name, response in responses.items():
        if response.status_code != 200:
            raise ValueError(f"Sidecar {name} returned HTTP {response.status_code}; inspect {out}")
    reader = engine_backend(plan.get("engine")).identity
    headers = launch_headers(run, plan)
    if reader.reports_process_start:
        live = fetch_engine_identity(plan["endpoint"], headers=headers)
    else:
        # The engine publishes no process start; its sidecar reads it on the host.
        live = fetch_engine_identity(
            plan["endpoint"], headers=headers, reader=reader, attestation_url=route
        )
    identity = asyncio.run(live)
    write_private(out / "identity.json", json.dumps(asdict(identity), indent=2) + "\n")
    payload = responses["attestation"].json()
    failures = verify_attestation(payload, document.contract, identity)
    if recorded is not None:
        failures += pod_failures(payload, recorded)
    if failures:
        raise ValueError(
            "Sidecar attestation fails against the live engine: " + "; ".join(failures)
        )
    return out


def pod_failures(payload: dict, recorded: dict) -> list[str]:
    """The ways a pod sidecar's attestation differs from the engine container it binds."""
    failures = []
    launch = attested_launch(payload)
    if launch.get("image_id") != recorded["image_id"]:
        failures.append(
            f"launch.image_id is {launch.get('image_id')!r}, the engine container runs "
            f"{recorded['image_id']!r}"
        )
    engine = payload.get("engine")
    started = engine.get("process_start_time_seconds") if isinstance(engine, dict) else None
    if (
        not isinstance(started, int | float)
        or isinstance(started, bool)
        or abs(started - recorded["process_start_time_seconds"]) > START_TOLERANCE_S
    ):
        failures.append(
            f"engine process started at {started}, not with engine container "
            f"{recorded['container_id']} at {recorded['process_start_time_seconds']}"
        )
    return failures
