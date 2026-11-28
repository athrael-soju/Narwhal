"""The fleet controller: runs the Kubernetes stage graph for the namespace's `NarwhalFleet`.

`python -m narwhal.deployment.kubernetes_controller --settings /etc/narwhal/controller.json`
runs in the pod the narwhal chart's controller Deployment makes. It watches the fleet, and each
reconcile is one `Runner.run()` pass of `KUBERNETES_GRAPH` through `kubernetes_target`, which
skips every stage whose inputs, outputs and live facts still hold. A `spec` edit, a new
`<GROUP>/requested-at` annotation and the 60 s resync each queue a reconcile; a status write
queues none.

Only the holder of the controller Lease acts. It claims the Lease with task 25's
`KubernetesLease`, on client-go's timings (a 15 s lease, a 10 s renew deadline, a renewal
every 2 s), and stops acting before another controller can take over: once its renew deadline
passes, the process ends with `os._exit` and touches no Job. Every write also goes through a
transport that refuses it, and ends the process, once the Lease is not valid, and waits at
most `WRITE_TIMEOUT_SECONDS` per phase, so a write never lands after another controller could
hold the Lease. SIGTERM ends the process the same way. The next holder's pass finds the attempt
that was running, which the runner records `interrupted`, and adopts its Jobs by their
recorded names instead of starting them again.

A pass checks the engine API key's HMAC through a key-check Job only after an edit or a new
`requested-at` value, or once `keyCheckPeriodSeconds` from controller.json has passed; other
passes take the HMAC the run directory recorded, in `controller-key-check.json` beside the
credential file.

Each fleet keeps its run directory at `<state>/runs/<namespace>/<fleet uid>/`. A pass prunes
all but the newest `spec.retainRuns` directories of the namespace, and `status.previousRuns`
names the others kept. The directory also holds `controller-stall.json` while the fleet is
stalled on a failed stage.

The controller writes only the fleet's `status`, by a JSON patch that names the fleet's uid,
and only when it changes. `Ready`, `Reconciling` and `Stalled` follow kstatus, so exactly one
of them is true unless the fleet is stopped. `Stopped` reports `spec.stopped`. One condition
per stage, typed `stage.<GROUP>/<stage>`, gives its run-record status as its reason and the
reasons it last ran as its message. The fleet is `Stalled`, and no pass runs, while:

- the installed CRD's schema is not the one this controller was built with;
- the namespace's admission policy or its binding is missing;
- the fleet's inputs are refused, such as a model volume or RuntimeClass outside the cluster
  admin's allow-lists in the controller's settings;
- `spec.routers` asks for a storage class or size the router StatefulSet's volume claim
  template does not hold, which the API server cannot change in place;
- a stage failed or timed out, until the next `spec` edit or a new `requested-at` value.

A live change, such as a replaced engine pod, does not clear a failed stage's stall. What
fails a stage is mostly outside the live facts the run record tracks, such as a node's
resources, an image or a Secret, so a pass started by a live change would usually fail the
same way, and one started on every change would turn the stall into a loop of failing Jobs.
`requested-at` is the retry.

A new `<GROUP>/export-request` value asks for an export Job, which copies finished evidence
out of the run directory for a workstation; see `kubernetes_export`. A thread apart from the
passes makes it, from the controller's own image, user and group in controller.json, and names
it in `status.exports` by the request's nonce, so an export never waits for a stage. Once the
Job's pod succeeds, the thread reads the pod's log itself, from the pod the Job of that UID
controls, and adds the pod's name and the log's SHA-256 and size to the answer. The
workstation imports a log only when it has that digest, so a pod someone else made under the
Job's name, which the chart's admission policy also refuses, exports nothing. The thread takes
one request per fleet every `kubernetes_export.EXPORT_INTERVAL_SECONDS`, runs one export Job
per fleet at a time, answering a request that arrives meanwhile with `kubernetes_export.RUNNING`,
and deletes the Job of each answer `status.exports` drops.

`spec.stopped` cancels a running stage, deletes the routers, HAProxy, the engines and then the
Jobs, and keeps the volumes and the run directory, so clearing it starts again from the record.
The fleet has no finalizer: deleting it leaves removal to owner-reference garbage collection.
The router-state PersistentVolumeClaims and the router Lease have no owner, so they outlive the
fleet, as the state volume does. A fleet made again under the name finds the routers' handoff
state on those claims, and the old router Lease, which expires because nobody renews it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

import httpx

from narwhal.runtime import kubernetes_api, kubernetes_lease
from narwhal.runtime.kubernetes_api import (
    CUSTOM_RESOURCE_DEFINITION,
    POD,
    STATEFUL_SET,
    ApiClient,
    Conflict,
    Informer,
    Kind,
    KubernetesError,
    LogTooLarge,
    NotFound,
    WatchEvent,
    WorkQueue,
)
from narwhal.runtime.kubernetes_lease import KubernetesLease

from . import (
    fleet_resource,
    kubernetes_export,
    kubernetes_objects,
    kubernetes_target,
    run_record,
    stages,
)
from .deploy import InputError
from .fleet_resource import (
    CONTROLLER_LEASE,
    KIND,
    NAME,
    READY,
    RECONCILING,
    REQUESTED_AT,
    STAGES,
    STALLED,
    STOPPED,
    VolumePolicy,
    stage_condition,
)
from .kubernetes_export import EXPORT_REQUEST
from .kubernetes_graph import KUBERNETES_GRAPH
from .kubernetes_objects import Owner, controlled
from .kubernetes_target import FLEET, Cluster, KubernetesDeployment
from .run_record import (
    CANCELLED,
    FAILED,
    INTERRUPTED,
    INVALIDATED,
    PASSED,
    RUNNING,
    TIMEOUT,
    RunLocked,
)
from .runner import StageFailed, Stop, check_directory

log = logging.getLogger("narwhal.kubernetes_controller")

SETTINGS = Path("/etc/narwhal/controller.json")
STATE = Path(kubernetes_target.STATE)
RUNS = "runs"
STALL_FILE = "controller-stall.json"
# The Lease holder's identity: the pod's name, from the downward API.
POD_NAME_ENV = "NARWHAL_POD_NAME"
# client-go's leader election: the lease lasts 15 s, the holder gives up 10 s after its last
# renewal landed, and it renews every 2 s.
LEASE_SECONDS = 15
RENEW_DEADLINE_SECONDS = 10
RETRY_SECONDS = 2.0
# The fence checks the renew deadline this often, apart from renewals that may block.
FENCE_SECONDS = 0.5
# Every fleet is reconciled again this often; pod identities have no maximum age.
RESYNC_SECONDS = 60.0
# A link observation holds this long before a pass probes it again.
MAX_AGE = {"link": 1800.0}
# When the run directory last ran a key check, for the fleet's generation and requested-at.
KEY_CHECK_FILE = "controller-key-check.json"
# How often a pass checks the engine API key's HMAC when nothing else asks for it.
KEY_CHECK_SECONDS = 900.0
# Each write waits at most this long per phase (connect, pool, send, reply), so one sent while
# the Lease is valid lands within 4.8 s, before the 5 s that separate the renew deadline from
# the lease duration run out and another controller may take over.
WRITE_TIMEOUT_SECONDS = 1.2
# The condition message the CRD allows.
MESSAGE_MAX = 32768
# How a reconcile waits for work while it holds the Lease.
WORK_WAIT_SECONDS = 1.0

VALIDATING_ADMISSION_POLICY = Kind(
    "admissionregistration.k8s.io",
    "v1",
    "ValidatingAdmissionPolicy",
    "validatingadmissionpolicies",
    namespaced=False,
)
VALIDATING_ADMISSION_POLICY_BINDING = Kind(
    "admissionregistration.k8s.io",
    "v1",
    "ValidatingAdmissionPolicyBinding",
    "validatingadmissionpolicybindings",
    namespaced=False,
)

# Why the fleet is stalled, ready, reconciling or stopped, as condition reasons.
SCHEMA_STALE = "SchemaStale"
POLICY_MISSING = "AdmissionPolicyMissing"
INPUT_REFUSED = "InputRefused"
ROUTER_STORAGE = "RouterStorageImmutable"
STAGE_FAILED = "StageFailed"
NAME_REFUSED = "NameRefused"
# Ready's reason while a failed `discover` holds the engines it stopped.
ENGINES_STOPPED = "EnginesStopped"

# A stage reports the most telling status among its instances.
_PRECEDENCE = (FAILED, TIMEOUT, INTERRUPTED, CANCELLED, RUNNING, INVALIDATED, PASSED)
_UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Kubernetes quantity suffixes, for comparing a volume size the API server normalised.
_SUFFIXES = {
    "": 1,
    "k": 10**3,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}
_QUANTITY = re.compile(r"([0-9]+(?:\.[0-9]+)?)([kKMGTPE]i?)?")


# Settings.


@dataclass(frozen=True)
class Settings:
    """controller.json, as the narwhal chart writes it: the state claim, the engines'
    ServiceAccount, the sidecars' user, the cluster admin's model volume and RuntimeClass
    allow-lists, the admission policy that bounds the fleet's pods, and the `fleet` label its
    monitoring gives every series, which the deployment status names."""

    values: Mapping[str, Any]
    policy: VolumePolicy
    runtime_classes: tuple[str, ...]
    admission_policy: str
    key_check_period: float = KEY_CHECK_SECONDS
    # The controller's own image, which every Job and sidecar that runs Narwhal code runs, and
    # the user and group export Jobs run as.
    image: str = ""
    user: int = 0
    group: int = 0
    fleet: str = ""

    @classmethod
    def of(cls, values: Mapping[str, Any]) -> Settings:
        problems = [
            f"{name} must name a Kubernetes object"
            for name in ("stateClaim", "engineServiceAccount", "admissionPolicy")
            if not isinstance(values.get(name), str)
            or not kubernetes_target.DNS_NAME.fullmatch(values[name])
        ]
        runtime_classes = values.get("runtimeClassNames") or []
        if not isinstance(runtime_classes, list) or not all(
            isinstance(name, str) for name in runtime_classes
        ):
            problems.append("runtimeClassNames must list RuntimeClass names")
        for name in (
            "modelVolumeSources",
            "modelHostPathPrefixes",
            "modelNfsServers",
            "modelCsiDrivers",
        ):
            found = values.get(name, [])
            if not isinstance(found, list) or not all(isinstance(item, str) for item in found):
                problems.append(f"{name} must list strings")
        period = values.get("keyCheckPeriodSeconds", KEY_CHECK_SECONDS)
        if (
            isinstance(period, bool)
            or not isinstance(period, int | float)
            or not math.isfinite(period)
            or period <= 0
        ):
            problems.append("keyCheckPeriodSeconds must be a positive number of seconds")
        image = values.get("image", "")
        if not (isinstance(image, str) and kubernetes_objects.DIGEST.fullmatch(image)):
            problems.append("image must name the controller's image by registry digest")
        ids = [values.get(name, 0) for name in ("runAsUser", "runAsGroup")]
        if not all(type(n) is int and 0 <= n < 2**31 for n in ids):
            problems.append("runAsUser and runAsGroup must be user and group IDs")
        fleet = values.get("fleet", values.get("release", ""))
        if not isinstance(fleet, str):
            problems.append("fleet must be a string")
        if problems:
            raise ValueError("controller settings: " + "; ".join(problems))
        return cls(
            dict(values),
            VolumePolicy.from_values(values),
            tuple(runtime_classes),
            str(values["admissionPolicy"]),
            float(period),
            str(image),
            int(ids[0]),
            int(ids[1]),
            str(fleet),
        )

    @classmethod
    def load(cls, path: Path) -> Settings:
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read the controller settings {path}: {exc}") from None
        if not isinstance(values, dict):
            raise ValueError(f"{path} must hold one JSON object")
        return cls.of(values)


# The Lease.


class Leadership(Protocol):
    """What the controller needs of its Lease: `KubernetesLease`."""

    @property
    def owned(self) -> bool: ...

    def valid(self) -> bool: ...

    def claim(self) -> bool: ...

    def renew(self) -> bool: ...


class LeaseLost(BaseException):
    """A write the fence refused: the controller Lease is no longer valid, so the process ends
    before the request leaves it."""


class FencedTransport(httpx.BaseTransport):
    """Sends a request that writes only while `allowed()` holds, checked as each attempt is
    sent, retries included. A refused write calls `lost()`, which ends the process."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        allowed: Callable[[], bool],
        lost: Callable[[], Any],
    ) -> None:
        self._inner = inner
        self._allowed = allowed
        self._lost = lost

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET" and not self._allowed():
            log.error("%s %s refused: the controller Lease is lost", request.method, request.url)
            self._lost()
            raise LeaseLost(f"{request.method} {request.url.path}: the controller Lease is lost")
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()

    def __deepcopy__(self, memo: dict[int, Any]) -> FencedTransport:
        return self


def fenced_client(
    connection: kubernetes_api.Connection,
    lease: Leadership,
    *,
    timeout_s: float,
    lost: Callable[[], Any],
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ApiClient:
    """An API client, as the controller's field manager, whose writes the Lease fences."""
    inner = transport or httpx.HTTPTransport(
        verify=connection.ssl_context if connection.ssl_context is not None else True,
        trust_env=False,
    )
    return ApiClient(
        connection,
        field_manager=kubernetes_objects.MANAGER,
        timeout_s=timeout_s,
        transport=FencedTransport(inner, lambda: lease.valid(), lost),
        sleep=sleep,
    )


class FencedCluster(kubernetes_target.ApiCluster):
    """The target's cluster, reading through `client` and writing through `writer`, whose
    short timeout keeps every write inside the Lease."""

    def __init__(self, client: ApiClient, writer: ApiClient) -> None:
        super().__init__(client)
        self.writer = writer

    def create(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]:
        return self.writer.create(kind, obj)

    def apply(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]:
        return self.writer.apply(kind, obj, force=True)

    def patch(
        self, kind: Kind, name: str, operations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return self.writer.patch(kind, name, operations)

    def delete(self, kind: Kind, name: str, *, uid: str | None = None) -> bool:
        return self.writer.delete(kind, name, propagation=kubernetes_objects.BACKGROUND, uid=uid)


# Status.


def _time(seconds: float) -> str:
    return datetime.fromtimestamp(math.floor(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _camel(status: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", status) if part)


def _message(text: str) -> str:
    return text if len(text) <= MESSAGE_MAX else text[: MESSAGE_MAX - 3] + "..."


def _condition(kind: str, status: bool | None, reason: str, message: str) -> dict[str, Any]:
    value = "Unknown" if status is None else ("True" if status else "False")
    return {"type": kind, "status": value, "reason": reason, "message": _message(message)}


def stage_order(until: str | None) -> tuple[str, ...]:
    """The stages a pass up to `until` decides, in order."""
    if until is None:
        return STAGES
    return STAGES[: STAGES.index(until) + 1]


@dataclass(frozen=True)
class Progress:
    """What the run record says about a fleet's stages, as its status reports them."""

    conditions: tuple[dict[str, Any], ...]
    stages: dict[str, dict[str, Any]]
    stage: str
    engines: str
    serving: bool
    # Every recorded instance of the stages up to `until` passed.
    settled: bool


def progress(
    record: Mapping[str, Any] | None, *, until: str | None, engines: int, stopped: bool = False
) -> Progress:
    """The status a run record gives: one condition and one count per stage it holds. A
    stopped fleet serves nothing, whatever its record holds."""
    entries = list((record or {}).get("stages", {}).values())
    order = stage_order(until)
    conditions = []
    counts: dict[str, dict[str, Any]] = {}
    current = ""
    for stage in STAGES:
        found = [entry for entry in entries if entry["stage"] == stage]
        if not found:
            continue
        statuses = {entry["status"] for entry in found}
        worst = next(status for status in _PRECEDENCE if status in statuses)
        passed = [entry for entry in found if entry["status"] == PASSED]
        count: dict[str, Any] = {"passed": len(passed), "total": len(found)}
        if passed:
            count["lastPassedTime"] = _time(max(entry["ended_at"] for entry in passed))
        counts[stage] = count
        reasons = list(dict.fromkeys(r for entry in found for r in entry["reasons"]))
        # The reasons say why the stage ran, not how it ended.
        message = f"ran because: {'; '.join(reasons)}" if reasons else ""
        failed = [entry for entry in found if entry["status"] in (FAILED, TIMEOUT)]
        if failed:
            entry = failed[0]
            text = entry["failure"]["message"]
            message = f"{text}; {message}" if message else text
        if stage not in order:
            # An earlier pass ran it; this one stops before it.
            note = f"from an earlier pass; spec.until stops at {order[-1]}"
            message = f"{message}; {note}" if message else note
        status = True if worst == PASSED else (None if worst == RUNNING else False)
        conditions.append(_condition(stage_condition(stage), status, _camel(worst), message))
        if worst == RUNNING or (not current and worst != PASSED and stage in order):
            current = stage
    if not current and counts:
        # Every stage the pass decides passed: the stage is the last of those, not one beyond
        # `until` that an earlier pass left.
        held = [stage for stage in order if stage in counts] or [s for s in STAGES if s in counts]
        current = held[-1]
    settled = all(
        entry["status"] == PASSED for entry in entries if entry["stage"] in order
    ) and bool(entries)
    checked = (counts.get("engine-check") or {}).get("passed", 0)
    router = [entry for entry in entries if entry["stage"] == kubernetes_target.LAST_STAGE]
    serving = not stopped and bool(router) and all(e["status"] == PASSED for e in router)
    return Progress(
        tuple(conditions),
        counts,
        current,
        f"{checked}/{engines}",
        serving,
        settled,
    )


def _same_status(desired: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    def comparable(status: Mapping[str, Any]) -> dict[str, Any]:
        found = {k: v for k, v in status.items() if k != "conditions"}
        found["conditions"] = {
            c.get("type"): {k: v for k, v in c.items() if k != "lastTransitionTime"}
            for c in status.get("conditions") or []
        }
        return found

    return comparable(desired) == comparable(live)


def _timed(
    conditions: Sequence[Mapping[str, Any]],
    live: Sequence[Mapping[str, Any]],
    generation: int,
    now: str,
) -> list[dict[str, Any]]:
    """Each condition with its observedGeneration, and the lastTransitionTime of the live one
    while its status stays."""
    before = {c.get("type"): c for c in live}
    found = []
    for condition in conditions:
        old = before.get(condition["type"]) or {}
        since = old.get("lastTransitionTime") if old.get("status") == condition["status"] else None
        found.append(
            {**condition, "observedGeneration": generation, "lastTransitionTime": since or now}
        )
    return found


# Router storage, which a StatefulSet cannot change.


def quantity(text: str) -> Decimal | None:
    """The value of a Kubernetes quantity such as 1Gi or 1500M, or None."""
    match = _QUANTITY.fullmatch(text.strip()) if isinstance(text, str) else None
    if match is None:
        return None
    try:
        return Decimal(match[1]) * _SUFFIXES[match[2] or ""]
    except InvalidOperation:
        return None


def router_storage_change(live: Mapping[str, Any], routers: Mapping[str, Any]) -> str | None:
    """Why the routers' volume claim template in `live` cannot become what `routers` asks."""
    templates = (live.get("spec") or {}).get("volumeClaimTemplates") or []
    template = next(
        (
            t
            for t in templates
            if (t.get("metadata") or {}).get("name") == kubernetes_objects.ROUTER_VOLUME
        ),
        None,
    )
    if template is None:
        return None
    spec = template.get("spec") or {}
    changed = []
    held_class = spec.get("storageClassName")
    if held_class != routers.get("storageClassName"):
        changed.append(
            f"spec.routers.storageClassName is {routers.get('storageClassName')}, and the "
            f"router volumes use {held_class}"
        )
    held = str(((spec.get("resources") or {}).get("requests") or {}).get("storage", ""))
    wanted = str(routers.get("size", ""))
    if quantity(held) is None or quantity(held) != quantity(wanted):
        changed.append(f"spec.routers.size is {wanted}, and the router volumes hold {held}")
    if not changed:
        return None
    claims = ", ".join(
        f"{kubernetes_objects.ROUTER_VOLUME}-{pod}" for pod in kubernetes_objects.ROUTER_PODS
    )
    return (
        "; ".join(changed)
        + ". A StatefulSet's volume claim templates cannot change. Put the routers block back, "
        f"or delete the StatefulSet {kubernetes_objects.ROUTER_SET} and the claims {claims}, "
        "which loses the routers' handoff state, and the next pass makes them again"
    )


# The run directories.


def run_directory(state: Path, namespace: str, uid: str) -> Path:
    return state / RUNS / namespace / uid


def prune_runs(state: Path, namespace: str, keep: str, retain: int) -> list[str]:
    """Delete all but the newest `retain` run directories of the namespace, `keep` included,
    and return the uids of the others kept, newest first."""
    parent = state / RUNS / namespace
    found = []
    for path in parent.iterdir() if parent.is_dir() else ():
        if path.name == keep or not _UID.fullmatch(path.name):
            continue
        if path.is_symlink() or not path.is_dir():
            continue
        found.append((path.stat().st_mtime, path.name, path))
    found.sort(reverse=True)
    kept = found[: max(retain - 1, 0)]
    for _, name, path in found[len(kept) :]:
        log.info("pruning the run directory of fleet %s", name)
        shutil.rmtree(path, ignore_errors=True)
    return [name for _, name, _ in kept]


def _read_record(directory: Path) -> dict[str, Any] | None:
    try:
        return run_record.read(directory) if (directory / run_record.RECORD).is_file() else None
    except (OSError, ValueError) as exc:
        log.warning("cannot read the run record in %s: %s", directory, exc)
        return None


def _read_private(path: Path) -> dict[str, Any] | None:
    """A JSON object from a regular file this user owns, never through a symbolic link, or
    None. The caller has checked the directory with `check_directory`."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            log.warning("%s is not a regular file this user owns; ignoring it", path)
            return None
        try:
            found = json.loads(stream.read(1 << 20))
        except ValueError:
            return None
    return found if isinstance(found, dict) else None


def _write_private(directory: Path, name: str, document: Mapping[str, Any]) -> None:
    """Replace one of the controller's files in a checked run directory, atomically."""
    check_directory(directory)
    stages.write_evidence(directory / name, dict(document))


# The controller.


@dataclass
class _Pass:
    """The pass running now: the fleet and generation it started from, its input digests and
    the Stop that ends it early."""

    key: str
    uid: str
    generation: int
    digests: dict[str, str]
    stop: Stop = field(default_factory=Stop)


@dataclass
class _Outcome:
    """How a reconcile left the fleet: the status reasons it reports."""

    stalled: tuple[str, str] | None = None
    # Ready's reason while stalled: the engines stopped, or the stall's own.
    not_ready: str = "Stalled"
    retrying: str | None = None
    stopping: bool = False
    stopped: bool = False


def _stage_failed(message: str) -> _Outcome:
    """A failed stage's stall. One whose failure stopped the engines reports it as Ready's
    reason, since the fleet then serves nothing."""
    down = kubernetes_target.ENGINES_STOPPED in message
    return _Outcome(
        stalled=(STAGE_FAILED, message), not_ready=ENGINES_STOPPED if down else "Stalled"
    )


def _generation(fleet: Mapping[str, Any]) -> int:
    value = (fleet.get("metadata") or {}).get("generation")
    return value if isinstance(value, int) and value > 0 else 1


def _requested(fleet: Mapping[str, Any]) -> str:
    return str(((fleet.get("metadata") or {}).get("annotations") or {}).get(REQUESTED_AT) or "")


def _uid(fleet: Mapping[str, Any]) -> str:
    return str((fleet.get("metadata") or {}).get("uid") or "")


def _spec(fleet: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = fleet.get("spec")
    return spec if isinstance(spec, Mapping) else {}


class Controller:
    """Reconciles the namespace's NarwhalFleet while it holds the controller Lease.

    `pump()` feeds the informer one watch, and `work_once()` takes one queued fleet and
    reconciles it, so a test runs both in one thread on its own clock; `serve()` runs them in
    threads beside the fence. A pass runs in the thread that calls `work_once()`. The informer's
    handler runs in another thread and only queues fleets, or tells a running pass to stop.
    """

    def __init__(
        self,
        client: ApiClient,
        *,
        writer: ApiClient | None = None,
        settings: Settings,
        lease: Leadership,
        state: Path = STATE,
        cluster: Callable[[Mapping[str, str], Path], Cluster] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        exit: Callable[[int], Any] = os._exit,
    ) -> None:
        self.client = client
        self.writer = writer or client
        self.namespace = client.namespace
        self.settings = settings
        self.lease = lease
        self.state = state
        self._cluster = cluster or (lambda env, logs: FencedCluster(client, self.writer))
        self._clock = clock
        self._monotonic = monotonic
        self._exit = exit
        self.queue = WorkQueue(clock=monotonic)
        self.informer = Informer(
            client,
            FLEET,
            namespace=self.namespace,
            handler=self._handle,
            resync_s=RESYNC_SECONDS,
            clock=monotonic,
        )
        self._lock = threading.Lock()
        self._pass: _Pass | None = None
        # The (uid, generation, requested-at) last queued for each fleet key.
        self._seen: dict[str, tuple[str, int, str]] = {}
        # The status last written for each fleet uid, or None when a write's outcome is unknown.
        self._written: dict[str, dict[str, Any] | None] = {}
        # The generation whose pass last reached `until`, by fleet uid.
        self._ready: dict[str, int] = {}
        # The uids of the other run directories kept, by fleet uid.
        self._previous: dict[str, list[str]] = {}
        # Export requests not yet answered, by fleet key, and the value last taken of each.
        self._export_pending: dict[str, dict[str, Any]] = {}
        self._export_seen: dict[str, str] = {}
        self._export_wake = threading.Event()
        # When each fleet's last export request was answered, by fleet uid, on `monotonic`.
        self._export_last: dict[str, float] = {}
        # The answers `status.exports` holds, by fleet uid.
        self._exports: dict[str, list[dict[str, Any]]] = {}
        # One status write at a time: a pass's or an export's.
        self._status_lock = threading.RLock()

    # Leadership.

    def elect(self) -> bool:
        """Claim or renew the Lease once; return whether this controller may act now."""
        try:
            if not (self.lease.owned and self.lease.renew()):
                self.lease.claim()
        except kubernetes_lease.LeaseError as exc:
            log.warning("controller Lease: %s", exc)
        return self.lease.valid()

    def fence(self) -> None:
        """End the process once the Lease is no longer valid: another controller may take it
        over a lease duration later, and by then this one must have stopped acting."""
        if not self.lease.valid():
            log.error("the controller Lease is lost; stopping without touching any Job")
            self._exit(1)

    # Events.

    def pump(self) -> None:
        """Follow one watch of the namespace's fleets, listing again after a 410."""
        self.informer.pump()

    def _handle(self, event: WatchEvent) -> None:
        fleet = event.object
        object_key = kubernetes_api.key(fleet)
        uid, generation, requested = _uid(fleet), _generation(fleet), _requested(fleet)
        with self._lock:
            running = (
                self._pass if self._pass is not None and self._pass.key == object_key else None
            )
        before = self._seen.get(object_key)
        self._take_export(object_key, fleet, deleted=event.type == "DELETED")
        # A fleet gone: deleted, or, as a relist after a 410 finds it, made again under the
        # name with a new uid. Its pass and its state end with it.
        gone = {before[0] if before else "", running.uid if running else ""} - {"", uid}
        if event.type == "DELETED":
            gone.add(uid)
        for old in gone:
            self._written.pop(old, None)
            self._ready.pop(old, None)
            self._previous.pop(old, None)
            self._exports.pop(old, None)
            self._export_last.pop(old, None)
        if running is not None and running.uid in gone:
            # The garbage collector removes what the pass made; it starts nothing more.
            running.stop.cancel(*STAGES)
        if event.type == "DELETED":
            self._seen.pop(object_key, None)
            self.queue.add(object_key)
            return
        seen = (uid, generation, requested)
        if event.type == "MODIFIED" and before == seen:
            # A status write, or a metadata change no reconcile reads.
            return
        if running is not None and running.uid == uid and generation != running.generation:
            self._interrupt(running, fleet)
        # Recorded last: a handler that raises hears the event again.
        self._seen[object_key] = seen
        self.queue.add(object_key)

    def _take_export(self, object_key: str, fleet: Mapping[str, Any], *, deleted: bool) -> None:
        """Queue the fleet's export request when its value is new."""
        with self._lock:
            if deleted:
                self._export_pending.pop(object_key, None)
                self._export_seen.pop(object_key, None)
                return
            annotations = (fleet.get("metadata") or {}).get("annotations") or {}
            value = annotations.get(EXPORT_REQUEST)
            if not isinstance(value, str) or self._export_seen.get(object_key) == value:
                return
            self._export_seen[object_key] = value
            self._export_pending[object_key] = dict(fleet)
        self._export_wake.set()

    def _interrupt(self, running: _Pass, fleet: Mapping[str, Any]) -> None:
        """Stop a pass an edit overtook: cancel the running stage when the edit changes a fact
        that reruns it, or when the fleet is to stop; otherwise at the next instance."""
        if _spec(fleet).get("stopped"):
            running.stop.cancel(*STAGES)
            return
        try:
            digests = self._digests(fleet)
        except (ValueError, KeyError) as exc:
            log.info("the edited fleet is refused (%s); stopping at the next instance", exc)
            running.stop.request()
            return
        changed = {
            fact
            for fact in digests.keys() | running.digests.keys()
            if digests.get(fact) != running.digests.get(fact)
        }
        stages = {rerun.stage for fact in changed for rerun in KUBERNETES_GRAPH.reruns(fact)}
        log.info(
            "fleet edited during a pass: %s changed; cancelling %s",
            ", ".join(sorted(changed)) or "no input",
            ", ".join(sorted(stages)) or "nothing, stopping at the next instance",
        )
        if stages:
            running.stop.cancel(*sorted(stages))
        else:
            running.stop.request()

    def _env(self, fleet: Mapping[str, Any]) -> dict[str, str]:
        return kubernetes_target.settings_env(fleet, self.settings.values)

    def _digests(self, fleet: Mapping[str, Any]) -> dict[str, str]:
        found = kubernetes_target.inputs(self._env(fleet), policy=self.settings.policy)
        return {name: value.digest for name, value in found.items()}

    # Work.

    def work_once(self, timeout_s: float = 0.0) -> bool:
        """Reconcile the next queued fleet; return whether one was reconciled. A controller
        that does not hold a valid Lease does nothing."""
        if not self.lease.valid():
            return False
        object_key = self.queue.get(timeout_s)
        if object_key is None:
            return False
        try:
            if not self.lease.valid():
                self.queue.add(object_key)
                return False
            self.reconcile(object_key)
        finally:
            self.queue.done(object_key)
        return True

    def export_once(self) -> int:
        """Answer every queued export request; return how many were taken. A controller that
        does not hold a valid Lease answers none."""
        if not self.lease.valid():
            return 0
        with self._lock:
            pending, self._export_pending = self._export_pending, {}
            self._export_wake.clear()
        for object_key, fleet in pending.items():
            uid = _uid(fleet)
            last = self._export_last.get(uid)
            if (
                last is not None
                and self._monotonic() - last < kubernetes_export.EXPORT_INTERVAL_SECONDS
            ):
                # Its turn comes later; a newer value taken meanwhile replaces it.
                with self._lock:
                    self._export_pending.setdefault(object_key, fleet)
                continue
            try:
                if self._export(fleet):
                    self._export_last[uid] = self._monotonic()
            except Exception:
                log.exception("export request of %s failed", object_key)
        for fleet in self.informer.items():
            try:
                self._follow_exports(fleet)
            except Exception:
                log.exception("export Jobs of %s not followed", kubernetes_api.key(fleet))
        return len(pending)

    def _follow_exports(self, fleet: Mapping[str, Any]) -> None:
        """Finish each answer whose export Job runs, once its pod has ended."""
        for entry in list(self._exports.get(_uid(fleet)) or []):
            if not entry.get("job") or "sha256" in entry or entry.get("error"):
                continue
            ended = self._export_ended(str(entry["job"]), str(entry["uid"]))
            if ended is not None:
                answer = {k: entry[k] for k in ("nonce", "job", "uid")}
                self._answer(fleet, {**answer, **ended})

    def _export_ended(self, name: str, uid: str) -> dict[str, Any] | None:
        """The pod and the digest and size of its log once the export Job's pod succeeds, an
        error once it cannot, or None while it runs. Only a pod the Job of `uid` controls
        counts, and the Job runs one pod at most: a second is no pod of the Job's own."""
        pods = [
            pod
            for pod in self.client.list(POD, labels={kubernetes_target.JOB_NAME_LABEL: name}).items
            if any(
                reference.get("kind") == "Job"
                and reference.get("uid") == uid
                and reference.get("controller") is True
                for reference in (pod.get("metadata") or {}).get("ownerReferences") or []
            )
        ]
        if len(pods) > 1:
            names = ", ".join(sorted(pod["metadata"]["name"] for pod in pods))
            return {"error": f"more than one pod claims export Job {name}: {names}"[:1024]}
        for pod in pods:
            pod_name = pod["metadata"]["name"]
            phase = (pod.get("status") or {}).get("phase")
            if phase == "Succeeded":
                try:
                    data = self.client.log(
                        pod_name,
                        container=kubernetes_export.EXPORT,
                        limit_bytes=kubernetes_export.EXPORT_LOG_BYTES,
                    )
                except LogTooLarge:
                    limit = kubernetes_export.EXPORT_LOG_BYTES
                    return {"error": f"export Job {name} logged more than {limit} bytes"}
                digest = hashlib.sha256(data).hexdigest()
                return {"pod": pod_name, "sha256": digest, "size": len(data)}
            if phase == "Failed":
                return {"error": f"export Job {name}'s pod {pod_name} failed"}
            if cause := kubernetes_target.never_runs(pod, scheduled=False):
                return {"error": f"export Job {name}: {cause}"[:1024]}
        job = self.client.get(kubernetes_target.JOB, name)
        if job is None or job["metadata"].get("uid") != uid:
            return {"error": f"export Job {name} is gone"}
        conditions = (job.get("status") or {}).get("conditions") or []
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions):
            return {"error": f"export Job {name} failed"}
        return None

    def _export(self, fleet: Mapping[str, Any]) -> bool:
        """Answer the fleet's export request, starting its Job unless one runs; return whether
        it wrote an answer."""
        uid = _uid(fleet)
        value = ((fleet.get("metadata") or {}).get("annotations") or {}).get(EXPORT_REQUEST)
        try:
            request = kubernetes_export.Request.parse(str(value))
        except ValueError as exc:
            log.warning("export request refused: %s", exc)
            nonce = None
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                nonce = json.loads(str(value)).get("nonce")
            if isinstance(nonce, str) and kubernetes_export.NONCE.fullmatch(nonce):
                self._answer(fleet, {"nonce": nonce, "error": _message(str(exc))[:1024]})
                return True
            return False
        status = fleet.get("status") if isinstance(fleet.get("status"), dict) else {}
        answered = self._exports.get(uid, (status or {}).get("exports") or [])
        if any(entry.get("nonce") == request.nonce for entry in answered):
            return False
        if self._export_running(fleet, answered):
            self._answer(fleet, {"nonce": request.nonce, "error": kubernetes_export.RUNNING})
            return True
        directory = run_directory(self.state, self.namespace, uid)
        try:
            if not (directory / run_record.RECORD).is_file():
                raise ValueError("the controller has written no run record for the fleet yet")
            check_directory(directory)
            record = run_record.read(directory)
            attempts = kubernetes_export.wanted(record, request, directory)
            document = kubernetes_export.job(
                Owner.of(fleet),
                request,
                attempts,
                image=self.settings.image,
                user=self.settings.user,
                group=self.settings.group,
                claim=str(self.settings.values["stateClaim"]),
                runs=f"{RUNS}/{self.namespace}/{uid}",
            )
        except ValueError as exc:
            self._answer(fleet, {"nonce": request.nonce, "error": _message(str(exc))[:1024]})
            return True
        name = document["metadata"]["name"]
        try:
            created = self.writer.create(kubernetes_target.JOB, document)
        except Conflict:
            found = self.client.get(kubernetes_target.JOB, name)
            if found is None:
                raise
            created = found
        log.info("export Job %s for request %s: %d attempts", name, request.nonce, len(attempts))
        self._answer(
            fleet, {"nonce": request.nonce, "job": name, "uid": created["metadata"]["uid"]}
        )
        return True

    def _export_running(
        self, fleet: Mapping[str, Any], answers: Sequence[Mapping[str, Any]]
    ) -> bool:
        """Whether one of the fleet's export Jobs still runs, found by the export gate label
        and the fleet's uid among its owners. Deletes each that will deliver nothing more: an
        ended one no answer names, and one whose answer is an error."""
        uid = _uid(fleet)
        by_uid = {str(entry["uid"]): entry for entry in answers if entry.get("uid")}
        running = False
        listed = self.client.list(
            kubernetes_target.JOB, labels={fleet_resource.GATE_LABEL: kubernetes_export.EXPORT}
        )
        for job in listed.items:
            metadata = job.get("metadata") or {}
            if not any(
                reference.get("uid") == uid for reference in metadata.get("ownerReferences") or []
            ):
                continue
            entry = by_uid.get(str(metadata.get("uid")))
            if metadata.get("deletionTimestamp"):
                continue
            if kubernetes_export.finished(job):
                if entry is None:
                    self._delete_export_job(metadata)
            elif entry is not None and entry.get("error"):
                self._delete_export_job(metadata)
            elif entry is None or kubernetes_export.in_flight(entry):
                running = True
        return running

    def _delete_export_job(self, metadata: Mapping[str, Any]) -> None:
        """Delete one export Job, and its pod with it, by its uid."""
        name, uid = str(metadata.get("name") or ""), str(metadata.get("uid") or "")
        if not name or not uid:
            return
        try:
            self.writer.delete(
                kubernetes_target.JOB, name, propagation=kubernetes_objects.BACKGROUND, uid=uid
            )
        except (NotFound, Conflict):
            return
        except KubernetesError as exc:
            log.warning("export Job %s not deleted: %s", name, exc)
            return
        log.info("deleted export Job %s", name)

    def _answer(self, fleet: Mapping[str, Any], answer: dict[str, Any]) -> None:
        """Name the export request's answer in `status.exports`, keeping the latest ones, and
        delete the Job of each answer it drops."""
        uid = _uid(fleet)
        with self._status_lock:
            status = fleet.get("status") if isinstance(fleet.get("status"), dict) else {}
            others = [
                dict(entry)
                for entry in self._exports.get(uid, (status or {}).get("exports") or [])
                if entry.get("nonce") != answer["nonce"]
            ]
            self._exports[uid], dropped = kubernetes_export.kept([*others, answer])
            for entry in dropped:
                if entry.get("job") and entry.get("uid"):
                    self._delete_export_job({"name": entry["job"], "uid": entry["uid"]})
            # The test keeps the write to this fleet; a pass's next status carries the answers.
            operations: list[dict[str, Any]] = [
                {"op": "test", "path": "/metadata/uid", "value": uid}
            ]
            if self._written.get(uid) or status:
                operations.append(
                    {"op": "add", "path": "/status/exports", "value": self._exports[uid]}
                )
            else:
                operations.append(
                    {"op": "add", "path": "/status", "value": {"exports": self._exports[uid]}}
                )
            try:
                written = self.writer.patch(
                    FLEET, fleet["metadata"]["name"], operations, status=True
                )
            except NotFound:
                return
            except (KubernetesError, ValueError) as exc:
                log.warning("export answer not written: %s", exc)
                self._written[uid] = None
                return
            self._written[uid] = dict(written.get("status") or {})

    def reconcile(self, object_key: str) -> None:
        fleet = self.informer.get(object_key)
        if fleet is None:
            # Deleted: owner references take every derived object with it, unordered, so the
            # controller has nothing to remove and no finalizer to hold.
            return
        uid, generation = _uid(fleet), _generation(fleet)
        status = fleet.get("status")
        live: dict[str, Any] = dict(status) if isinstance(status, dict) else {}
        if self._written.get(uid) is None:
            self._written[uid] = dict(live)
            self._exports.setdefault(uid, list(live.get("exports") or []))
            ready = next((c for c in live.get("conditions") or [] if c.get("type") == READY), None)
            if (
                ready
                and ready.get("status") == "True"
                and ready.get("observedGeneration") == generation
            ):
                self._ready.setdefault(uid, generation)
        try:
            outcome = self._reconcile(fleet)
        except Exception as exc:
            log.exception("reconcile of %s failed; trying again", object_key)
            self.queue.add_rate_limited(object_key)
            outcome = _Outcome(retrying=f"{type(exc).__name__}: {exc}")
        else:
            if outcome.retrying:
                self.queue.add_rate_limited(object_key)
            else:
                self.queue.forget(object_key)
        self._publish(fleet, outcome)

    def _blocked(self, fleet: Mapping[str, Any]) -> tuple[str, str] | None:
        """Why no pass may run for the fleet as it stands, or None."""
        if (fleet.get("metadata") or {}).get("name") != NAME:
            return NAME_REFUSED, f"a namespace holds one {KIND}, and it is named {NAME}"
        crd = self.client.get(CUSTOM_RESOURCE_DEFINITION, fleet_resource.CRD_NAME)
        if crd is None or not fleet_resource.crd_current(crd):
            return SCHEMA_STALE, (
                f"the installed CRD {fleet_resource.CRD_NAME} does not hold this controller's "
                f"schema ({fleet_resource.schema_digest()[:12]}); upgrade the narwhal-crds chart "
                "to the narwhal chart's version"
            )
        name = self.settings.admission_policy
        policy = self.client.get(VALIDATING_ADMISSION_POLICY, name)
        binding = self.client.get(VALIDATING_ADMISSION_POLICY_BINDING, name)
        bound = (binding or {}).get("spec") or {}
        if (
            policy is None
            or bound.get("policyName") != name
            or "Deny" not in (bound.get("validationActions") or [])
        ):
            return POLICY_MISSING, (
                f"the admission policy {name} or its binding is missing or does not deny; the "
                "controller creates nothing until the narwhal chart installs them"
            )
        try:
            env = self._env(fleet)
            kubernetes_target.inputs(env, policy=self.settings.policy)
        except InputError as exc:
            return INPUT_REFUSED, "; ".join(exc.problems)
        except (ValueError, KeyError) as exc:
            return INPUT_REFUSED, str(exc)
        runtime = (_spec(fleet).get("engines") or {}).get("resources", {}).get("runtimeClassName")
        if runtime and runtime not in self.settings.runtime_classes:
            return INPUT_REFUSED, (
                f"engines.resources.runtimeClassName: the cluster admin allows only "
                f"{', '.join(self.settings.runtime_classes) or 'no RuntimeClass'} in "
                "runtimeClassNames"
            )
        routers = _spec(fleet).get("routers")
        router_set = self.client.get(STATEFUL_SET, kubernetes_objects.ROUTER_SET)
        if (
            isinstance(routers, Mapping)
            and router_set is not None
            and controlled(Owner.of(fleet), router_set)
            and (why := router_storage_change(router_set, routers))
        ):
            return ROUTER_STORAGE, why
        return None

    def _reconcile(self, fleet: dict[str, Any]) -> _Outcome:
        uid, generation = _uid(fleet), _generation(fleet)
        spec = _spec(fleet)
        directory = run_directory(self.state, self.namespace, uid)
        if spec.get("stopped") and (fleet.get("metadata") or {}).get("name") == NAME:
            # Stopping deletes only what the fleet controls, so it needs no valid input.
            self._stop(directory, uid)
            return _Outcome(stopped=True)
        if blocked := self._blocked(fleet):
            log.warning("fleet stalled: %s: %s", *blocked)
            return _Outcome(stalled=blocked)
        _private_dirs(directory.parent)
        self._previous[uid] = prune_runs(
            self.state, self.namespace, uid, int(spec.get("retainRuns") or 3)
        )
        env = self._env(fleet)
        stall = None
        if directory.exists():
            check_directory(directory)
            stall = _read_private(directory / STALL_FILE)
        if stall and (stall.get("generation"), stall.get("requested_at")) == (
            generation,
            _requested(fleet),
        ):
            return _stage_failed(str(stall.get("message") or ""))
        return self._run(fleet, directory, env)

    def _deployment(self, directory: Path, env: Mapping[str, str]) -> KubernetesDeployment:
        deployment = KubernetesDeployment(
            directory, env, cluster=self._cluster, max_age=MAX_AGE, state_root=self.state
        )
        deployment.announce = log.info
        return deployment

    def _key_check_due(self, directory: Path, fleet: Mapping[str, Any]) -> bool:
        """Whether this pass reads the engine API key's HMAC through a key-check Job: after
        an edit or a new requested-at value, and once `keyCheckPeriodSeconds` passed."""
        if not directory.exists():
            return True
        check_directory(directory)
        last = _read_private(directory / KEY_CHECK_FILE)
        if last is None or (last.get("generation"), last.get("requested_at")) != (
            _generation(fleet),
            _requested(fleet),
        ):
            return True
        at = last.get("at")
        if isinstance(at, bool) or not isinstance(at, int | float):
            return True
        return self._clock() - at >= self.settings.key_check_period

    def _stop(self, directory: Path, uid: str) -> None:
        """Delete the routers, HAProxy, the engines and the Jobs, keeping volumes and record."""
        _private_dirs(directory)
        env = {kubernetes_target.NAMESPACE: self.namespace, kubernetes_target.FLEET_UID: uid}
        deployment = self._deployment(directory, env)
        with run_record.locked(directory):
            deleted = deployment.stop(deployment.cluster())
            # The next pass applies and starts them again, and no status reads them as running.
            deployment.forget_started()
        if deleted:
            log.info("stopped the fleet: deleted %d objects", len(deleted))

    def _run(self, fleet: dict[str, Any], directory: Path, env: dict[str, str]) -> _Outcome:
        uid, generation = _uid(fleet), _generation(fleet)
        spec = _spec(fleet)
        inputs = kubernetes_target.inputs(env, policy=self.settings.policy)
        digests = {name: value.digest for name, value in inputs.items()}
        current = _Pass(kubernetes_api.key(fleet), uid, generation, digests)
        deployment = self._deployment(directory, env)
        deployment.key_check = self._key_check_due(directory, fleet)

        def announce(line: str) -> None:
            log.info("%s", line)
            self._publish(fleet, _Outcome(), directory=directory)

        deployment.announce = announce
        with self._lock:
            self._pass = current
        # An event between reading the fleet and the line above found no pass to stop.
        cached = self.informer.get(kubernetes_api.key(fleet))
        if cached is None or _uid(cached) != uid:
            current.stop.cancel(*STAGES)
        elif _generation(cached) != generation:
            self._interrupt(current, cached)
        try:
            self._publish(fleet, _Outcome(), directory=directory, started=generation)
            try:
                result = deployment.runner().run(inputs, until=spec.get("until"), stop=current.stop)
            finally:
                if deployment.key_checked_at is not None and directory.exists():
                    _write_private(
                        directory,
                        KEY_CHECK_FILE,
                        {
                            "at": self._clock(),
                            "generation": generation,
                            "requested_at": _requested(fleet),
                        },
                    )
                self._deployment_status(deployment)
        except StageFailed as exc:
            entry = exc.record
            if entry["status"] in (FAILED, TIMEOUT):
                message = f"{exc.key} {entry['status']}: {entry['failure']['message']}"
                _write_private(
                    directory,
                    STALL_FILE,
                    {
                        "generation": generation,
                        "requested_at": _requested(fleet),
                        "stage": exc.key,
                        "message": message,
                    },
                )
                log.warning("stalled: %s", message)
                return _stage_failed(message)
            # An interrupted attempt is adopted by the next pass, and never stalls the fleet.
            log.info("retrying: %s", exc)
            return _Outcome(retrying=str(exc))
        except RunLocked as exc:
            return _Outcome(retrying=str(exc))
        except InputError as exc:
            log.warning("stalled: the fleet's inputs are refused: %s", "; ".join(exc.problems))
            return _Outcome(stalled=(INPUT_REFUSED, "; ".join(exc.problems)))
        finally:
            with self._lock:
                self._pass = None
        if result.stopped:
            # An edit, a stop or a deletion ended the pass; its event queued the fleet again.
            return _Outcome(stopping=bool(spec.get("stopped")))
        with contextlib.suppress(FileNotFoundError):
            (directory / STALL_FILE).unlink()
        self._ready[uid] = generation
        return _Outcome()

    def _deployment_status(self, deployment: KubernetesDeployment) -> None:
        """Give the routers the pass's deployment status, passed or not. Best effort, as on SSH
        hosts: a failed write keeps the status before, and the next pass writes it again."""
        try:
            deployment.publish_deployment_status(self.settings.fleet)
        except (KubernetesError, OSError, ValueError, LookupError, TypeError) as exc:
            log.warning("the deployment status was not written: %s", exc)

    # Status.

    def _publish(
        self,
        fleet: Mapping[str, Any],
        outcome: _Outcome,
        *,
        directory: Path | None = None,
        started: int | None = None,
    ) -> None:
        """Write the fleet's status if it changed. A failed write is made again on the next
        change or reconcile, since its outcome may be unknown."""
        with self._status_lock:
            self._publish_now(fleet, outcome, directory=directory, started=started)

    def _publish_now(
        self,
        fleet: Mapping[str, Any],
        outcome: _Outcome,
        *,
        directory: Path | None,
        started: int | None,
    ) -> None:
        uid, generation = _uid(fleet), _generation(fleet)
        cached = self.informer.get(kubernetes_api.key(fleet))
        if cached is None or _uid(cached) != uid:
            # Deleted, or made again under the name: this fleet has no status to write.
            return
        spec = _spec(fleet)
        live = self._written.get(uid) or {}
        if directory is None and uid:
            directory = run_directory(self.state, self.namespace, uid)
        record = _read_record(directory) if directory is not None else None
        engines = len(spec.get("engines", {}).get("nodes") or [])
        stopped = bool(spec.get("stopped"))
        found = progress(
            record, until=spec.get("until"), engines=engines, stopped=stopped or outcome.stopped
        )
        observed = generation
        if outcome.stalled is None and not outcome.stopped and started is None:
            # A pass reports the generation it started from until the next one starts.
            observed = int(live.get("observedGeneration") or generation)
        ready = (
            outcome.stalled is None
            and not stopped
            and not outcome.retrying
            and self._ready.get(uid) == generation
            and found.settled
        )
        if outcome.stalled is not None:
            stalled = _condition(STALLED, True, *outcome.stalled)
            reconciling = _condition(RECONCILING, False, "Stalled", "the fleet waits for an edit")
            ready_condition = _condition(READY, False, outcome.not_ready, outcome.stalled[1])
        elif outcome.stopped:
            stalled = _condition(STALLED, False, "Stopped", "the fleet is stopped")
            reconciling = _condition(RECONCILING, False, "Stopped", "the fleet is stopped")
            ready_condition = _condition(READY, False, "Stopped", "spec.stopped is true")
        elif ready:
            stalled = _condition(STALLED, False, "Ready", "every stage passed")
            reconciling = _condition(RECONCILING, False, "Ready", "every stage passed")
            message = f"every stage up to {found.stage} passed"
            if spec.get("until") and spec["until"] != kubernetes_target.LAST_STAGE:
                message += (
                    f"; spec.until stops the run there, before {kubernetes_target.LAST_STAGE}"
                )
            ready_condition = _condition(READY, True, "Passed", message)
        else:
            stalled = _condition(STALLED, False, "Reconciling", "a pass is under way")
            if outcome.retrying:
                why = ("Retrying", _message(outcome.retrying))
            elif outcome.stopping:
                why = ("Stopping", "the fleet stops")
            else:
                why = ("Running", f"stage {found.stage or 'access'} runs")
            reconciling = _condition(RECONCILING, True, *why)
            ready_condition = _condition(READY, False, why[0], why[1])
        stopped_condition = _condition(
            STOPPED,
            outcome.stopped,
            "Stopped" if outcome.stopped else ("Stopping" if stopped else "Running"),
            "the routers, HAProxy, engines and Jobs are deleted; the volumes stay"
            if outcome.stopped
            else "spec.stopped is false",
        )
        now = _time(self._clock())
        conditions = _timed(
            [ready_condition, reconciling, stalled, stopped_condition, *found.conditions],
            live.get("conditions") or [],
            observed,
            now,
        )
        desired: dict[str, Any] = {
            "observedGeneration": observed,
            "conditions": conditions,
            # A discover that stopped the engines and failed leaves the routers' record
            # passed, with no engine behind them.
            "serving": found.serving
            and not stopped
            and not outcome.stopped
            and outcome.not_ready != ENGINES_STOPPED,
            "engines": "0/" + found.engines.split("/")[1] if outcome.stopped else found.engines,
            "stages": found.stages,
            "previousRuns": list(self._previous.get(uid, [])),
        }
        if found.stage:
            desired["stage"] = found.stage
        if self._exports.get(uid):
            desired["exports"] = list(self._exports[uid])
        if _same_status(desired, live):
            return
        # A JSON patch whose first operation tests the uid lands only on this fleet, never on
        # one made again under the name; a server-side apply takes no uid precondition. It
        # sets the whole status, so a replay after a lost reply writes the same value.
        operations: list[dict[str, Any]] = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "add", "path": "/status", "value": desired},
        ]
        try:
            written = self.writer.patch(FLEET, fleet["metadata"]["name"], operations, status=True)
        except NotFound:
            self._written.pop(uid, None)
            return
        except (KubernetesError, ValueError) as exc:
            log.warning("fleet status not written: %s", exc)
            self._written[uid] = None
            return
        self._written[uid] = dict(written.get("status") or desired)

    # The process.

    def serve(self) -> None:
        """Run until the Lease is lost: claim it, then reconcile in a worker thread while the
        informer follows the fleet, a thread renews the Lease and this thread fences."""
        while not self.elect():
            time.sleep(RETRY_SECONDS)
        log.info("holding the controller Lease")
        stop = threading.Event()
        threads = [
            threading.Thread(target=self.informer.run, args=(stop,), name="informer"),
            threading.Thread(target=self._renew, args=(stop,), name="lease"),
            threading.Thread(target=self._work, args=(stop,), name="worker"),
            threading.Thread(target=self._export_work, args=(stop,), name="exports"),
        ]
        for thread in threads:
            thread.daemon = True
            thread.start()
        while True:
            time.sleep(FENCE_SECONDS)
            self.fence()

    def _renew(self, stop: threading.Event) -> None:
        while not stop.wait(RETRY_SECONDS):
            try:
                self.lease.renew()
            except kubernetes_lease.LeaseError as exc:
                log.warning("controller Lease renewal: %s", exc)

    def _export_work(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._export_wake.wait(WORK_WAIT_SECONDS)
            try:
                self.export_once()
            except Exception:
                log.exception("controller export worker failed")
                time.sleep(RETRY_SECONDS)

    def _work(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.work_once(WORK_WAIT_SECONDS)
            except Exception:
                log.exception("controller worker failed")
                time.sleep(RETRY_SECONDS)


def _private_dirs(path: Path) -> None:
    """Make `path` and its missing parents, each at mode 0700."""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700)


def _exit_now(signum: int, _frame: object) -> None:
    # The next Lease holder adopts every running Job, so this process touches none.
    log.info("signal %d: stopping without touching any Job", signum)
    logging.shutdown()
    os._exit(0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m narwhal.deployment.kubernetes_controller",
        description="Run the stage graph for the namespace's NarwhalFleet.",
    )
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx logs each request at INFO, every poll of a running Job; its warnings stay.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.load(args.settings)
    holder = os.environ.get(POD_NAME_ENV, "")
    if not holder:
        parser.error(f"set {POD_NAME_ENV} to the pod's name")
    connection = kubernetes_api.in_cluster()
    lease_client = ApiClient(
        connection,
        field_manager=kubernetes_objects.MANAGER,
        backoff=kubernetes_lease.BACKOFF,
        timeout_s=RETRY_SECONDS,
    )
    lease = KubernetesLease(
        lease_client,
        CONTROLLER_LEASE,
        holder,
        LEASE_SECONDS,
        safety_margin_s=LEASE_SECONDS - RENEW_DEADLINE_SECONDS,
    )
    # Reads wait the client's default; writes wait less, so each lands inside the Lease.
    client = fenced_client(connection, lease, timeout_s=10.0, lost=lambda: os._exit(1))
    writer = fenced_client(
        connection, lease, timeout_s=WRITE_TIMEOUT_SECONDS, lost=lambda: os._exit(1)
    )
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _exit_now)
    Controller(client, writer=writer, settings=settings, lease=lease, state=args.state).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
