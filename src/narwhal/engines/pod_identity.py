"""The live identity of an engine container in a Kubernetes pod.

The attestation sidecar runs as a second container in the engine pod. The pod shares one
process namespace, so the sidecar sees the engine's process: its ID, its start ticks and its
cgroup path, which carries the engine container's runtime ID. The Kubernetes API reports the
same container ID for the engine container, with its image ID and start time. Together they
bind the engine process to its container and image as `docker inspect` binds a container.

The API is reached with the pod's service account token and CA, through the standard library
only. The service account needs `get` on pods in its own namespace.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from .host_process import boot_time

PROC = Path("/proc")
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
API_SERVER = "https://kubernetes.default.svc"
API_TIMEOUT_S = 10.0
# The engine container's name in the pod specification.
ENGINE_CONTAINER = "engine"
# The kernel's boot time has one-second resolution, so two readings of one process start
# can differ by this many seconds.
START_TOLERANCE_S = 2.0
CONTAINER_ID = re.compile(r"([a-z][a-z0-9.-]*)://([0-9a-f]{64})")
IMAGE_DIGEST = re.compile(r"(?:^|@)(sha256:[0-9a-f]{64})$")
# A container's runtime ID ends its cgroup path, as `cri-containerd-<id>.scope`, `crio-<id>`,
# or `<id>` under the cgroupfs driver. crun under the systemd driver on cgroup v2 runs the
# container's processes in a `container` cgroup below the scope.
CGROUP_CONTAINER = re.compile(r"(?:^|[/-])([0-9a-f]{64})(?:\.scope)?(?:/container)?$")


class PodIdentityError(ValueError):
    """The pod, its engine container or the engine process does not bind."""


class EngineNotRunning(PodIdentityError):
    """The pod's engine container has not started, or has stopped."""


@dataclass(frozen=True)
class PodIdentity:
    namespace: str
    pod: str
    pod_uid: str
    node: str
    # The pod address, which is its node's address under host networking.
    pod_ip: str
    container: str
    # The runtime that reports the container, such as containerd, and its container ID.
    runtime: str
    container_id: str
    # The image the pod status reports, and its immutable digest.
    image: str
    image_id: str
    started_at: str
    restart_count: int
    # The engine process in the pod's process namespace: the container's first process.
    pid: int
    start_ticks: int
    process_start_time_seconds: float

    def record(self) -> dict:
        return asdict(self)


def service_account_namespace() -> str:
    path = SERVICE_ACCOUNT / "namespace"
    try:
        return path.read_text().strip()
    except OSError as error:
        raise PodIdentityError(
            f"{path} is unreadable ({error.strerror}); the attestation container mounts the "
            "service account that reads its pod"
        ) from error


def read_pod(namespace: str, name: str) -> dict:
    """Return the pod object through the Kubernetes API, with the service account's token."""
    try:
        token = (SERVICE_ACCOUNT / "token").read_text().strip()
        context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    except OSError as error:
        raise PodIdentityError(
            f"the service account under {SERVICE_ACCOUNT} is unreadable ({error}); run in a pod "
            "whose container mounts its service account token and CA"
        ) from error
    url = f"{API_SERVER}/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(name, safe='')}"
    # The URL is the API server's fixed https address.
    request = urllib.request.Request(  # noqa: S310
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=API_TIMEOUT_S, context=context
        ) as response:
            pod = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise PodIdentityError(
                f"the service account may not get pod {namespace}/{name}; bind it to a Role "
                "with get on pods in its namespace"
            ) from error
        if error.code == 404:
            raise PodIdentityError(f"pod {namespace}/{name} does not exist") from error
        raise PodIdentityError(
            f"reading pod {namespace}/{name} returned HTTP {error.code}"
        ) from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise PodIdentityError(f"reading pod {namespace}/{name} failed: {error}") from error
    if not isinstance(pod, dict) or pod.get("kind") != "Pod":
        raise PodIdentityError(f"the API returned no pod for {namespace}/{name}")
    return pod


def container_status(pod: dict, container: str) -> dict:
    """Return a running container's runtime, ID, image, image digest, start and restarts."""
    name = pod.get("metadata", {}).get("name")
    statuses = [
        status
        for status in pod.get("status", {}).get("containerStatuses") or []
        if status.get("name") == container
    ]
    if len(statuses) != 1:
        raise EngineNotRunning(f"pod {name} reports no status for its {container} container")
    [status] = statuses
    running = (status.get("state") or {}).get("running")
    if not isinstance(running, dict) or not isinstance(running.get("startedAt"), str):
        state = ", ".join(sorted(status.get("state") or {})) or "no state"
        raise EngineNotRunning(f"the {container} container of pod {name} is not running ({state})")
    reference = CONTAINER_ID.fullmatch(str(status.get("containerID", "")))
    if reference is None:
        raise PodIdentityError(
            f"the {container} container of pod {name} reports no <runtime>://<ID> container ID"
        )
    digest = IMAGE_DIGEST.search(str(status.get("imageID", "")))
    if digest is None:
        raise PodIdentityError(
            f"the {container} container of pod {name} reports no sha256 image ID"
        )
    restarts = status.get("restartCount")
    return {
        "runtime": reference[1],
        "container_id": reference[2],
        "image": str(status.get("image", "")),
        "image_id": digest[1],
        "started_at": running["startedAt"],
        "restart_count": restarts if type(restarts) is int else 0,
    }


def process_stat(pid: int) -> tuple[str, int, int]:
    """Return a process's state, parent process ID and start ticks from its stat file."""
    fields = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[1]), int(fields[19])


def cgroup_container(pid: int) -> str | None:
    """Return the container ID that a process's cgroup path names, if it names one."""
    found = set()
    for line in (PROC / str(pid) / "cgroup").read_text().splitlines():
        match = CGROUP_CONTAINER.search(line.split(":", 2)[-1])
        if match is not None:
            found.add(match[1])
    return found.pop() if len(found) == 1 else None


def container_process(container_id: str) -> tuple[int, int]:
    """Return the process ID and start ticks of a container's first process.

    In a pod that shares its process namespace, the container runtime starts each container's
    first process from outside the namespace, so it has parent ID 0. So does each process that
    `kubectl exec` starts in the container, but those start later. A container with no process
    here has ended, although the API can still report it running for a moment.
    """
    first = []
    seen = False
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if cgroup_container(pid) != container_id:
                continue
            state, parent, ticks = process_stat(pid)
        except (OSError, IndexError, ValueError):
            # The process ended while the scan read it.
            continue
        seen = True
        if parent == 0 and state not in ("Z", "X"):
            first.append((pid, ticks))
    if not seen:
        raise EngineNotRunning(
            f"no process of container {container_id} runs in this pod's process namespace"
        )
    earliest = min((ticks for _, ticks in first), default=None)
    started = [process for process in first if process[1] == earliest]
    if len(started) != 1:
        raise PodIdentityError(
            f"container {container_id} shows {len(started)} first processes in this process "
            "namespace; expected one"
        )
    return started[0]


def process_start(ticks: int) -> float:
    """Seconds since the epoch of a process start, from its start ticks."""
    return boot_time() + ticks / os.sysconf("SC_CLK_TCK")


def read_identity(
    namespace: str,
    pod_name: str,
    pod_uid: str,
    image: str,
    container: str = ENGINE_CONTAINER,
) -> PodIdentity:
    """Return the live identity of `container` in this pod, checked through the API and /proc.

    The pod must be the one with UID `pod_uid`, share its process namespace and run the
    container. The pod specification must name `image`, a `<repository>@sha256:<hex>`
    reference, for the container, and the container must run that digest. The container's
    first process must be visible here.
    """
    pod = read_pod(namespace, pod_name)
    metadata, spec = pod.get("metadata", {}), pod.get("spec", {})
    if metadata.get("uid") != pod_uid:
        raise PodIdentityError(
            f"pod {namespace}/{pod_name} has UID {metadata.get('uid')}, not the checked "
            f"pod's {pod_uid}"
        )
    if spec.get("shareProcessNamespace") is not True:
        raise PodIdentityError(
            f"pod {namespace}/{pod_name} does not set shareProcessNamespace: true, so its "
            f"sidecar cannot bind the {container} process to its container"
        )
    specified = [
        entry.get("image")
        for entry in spec.get("containers") or []
        if entry.get("name") == container
    ]
    if specified != [image]:
        named = ", ".join(map(str, specified)) or "none"
        raise PodIdentityError(
            f"pod {namespace}/{pod_name} specifies image {named} for its {container} "
            f"container, not the checked {image}"
        )
    status = container_status(pod, container)
    planned = image.rpartition("@")[2]
    if status["image_id"] != planned:
        raise PodIdentityError(
            f"the {container} container of pod {namespace}/{pod_name} runs image "
            f"{status['image_id']}, not the checked {planned}"
        )
    pid, ticks = container_process(status["container_id"])
    started = process_start(ticks)
    return PodIdentity(
        namespace=namespace,
        pod=pod_name,
        pod_uid=pod_uid,
        node=str(spec.get("nodeName", "")),
        pod_ip=str(pod.get("status", {}).get("podIP", "")),
        container=container,
        pid=pid,
        start_ticks=ticks,
        process_start_time_seconds=started,
        **status,
    )


def live_process(container_id: str, pid: int, ticks: int) -> float:
    """Return the start of process `pid` while it still runs as container `container_id`'s
    first process with the start ticks `ticks`; raise once it has ended or been replaced."""
    try:
        state, parent, observed = process_stat(pid)
        bound = cgroup_container(pid) == container_id
    except (OSError, IndexError, ValueError) as error:
        raise PodIdentityError(f"engine process {pid} has ended") from error
    if not bound or parent != 0 or observed != ticks or state in ("Z", "X"):
        raise PodIdentityError(f"engine process {pid} of container {container_id} has ended")
    return process_start(ticks)


def pod_clock(container_id: str) -> Callable[[], float]:
    """Return the clock of a container's first process, found in this process namespace now.

    The clock raises once that process ends, so a restarted container never stands in for it.
    """
    pid, ticks = container_process(container_id)
    return lambda: live_process(container_id, pid, ticks)
