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
passes, the process ends with `os._exit` and touches no Job. SIGTERM ends it the same way. The
next holder's pass finds the attempt that was running, which the runner records `interrupted`,
and adopts its Jobs by their recorded names instead of starting them again.

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
import json
import logging
import math
import os
import re
import shutil
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from narwhal.runtime import kubernetes_api, kubernetes_lease
from narwhal.runtime.kubernetes_api import (
    CUSTOM_RESOURCE_DEFINITION,
    STATEFUL_SET,
    ApiClient,
    Informer,
    Kind,
    KubernetesError,
    NotFound,
    WatchEvent,
    WorkQueue,
)
from narwhal.runtime.kubernetes_lease import KubernetesLease

from . import fleet_resource, kubernetes_objects, kubernetes_target, run_record, stages
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
from .runner import HelpersSurvived, StageFailed, Stop

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
# The stages whose applied objects `spec.stopped` deletes and no live fact watches: the
# engines' StatefulSet, Service and hooks. Stopping invalidates them, so clearing
# `spec.stopped` applies them again; the engines', sidecars' and routers' live facts rerun
# the rest.
STOP_INVALIDATES = ("install",)
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
HELPERS_SURVIVED = "HelpersSurvived"

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
    allow-lists, and the admission policy that bounds the fleet's pods."""

    values: Mapping[str, Any]
    policy: VolumePolicy
    runtime_classes: tuple[str, ...]
    admission_policy: str

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
        if problems:
            raise ValueError("controller settings: " + "; ".join(problems))
        return cls(
            dict(values),
            VolumePolicy.from_values(values),
            tuple(runtime_classes),
            str(values["admissionPolicy"]),
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


def progress(record: Mapping[str, Any] | None, *, until: str | None, engines: int) -> Progress:
    """The status a run record gives: one condition and one count per stage it holds."""
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
        message = "; ".join(reasons)
        failed = [entry for entry in found if entry["status"] in (FAILED, TIMEOUT)]
        if failed:
            entry = failed[0]
            text = entry["failure"]["message"]
            message = f"{text}; ran because: {message}" if message else text
        status = True if worst == PASSED else (None if worst == RUNNING else False)
        conditions.append(_condition(stage_condition(stage), status, _camel(worst), message))
        if worst == RUNNING or (not current and worst != PASSED and stage in order):
            current = stage
    if not current and counts:
        current = next(stage for stage in reversed(STAGES) if stage in counts)
    settled = all(
        entry["status"] == PASSED for entry in entries if entry["stage"] in order
    ) and bool(entries)
    checked = (counts.get("engine-check") or {}).get("passed", 0)
    router = [entry for entry in entries if entry["stage"] == kubernetes_target.LAST_STAGE]
    serving = bool(router) and all(entry["status"] == PASSED for entry in router)
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


def _forget(directory: Path, names: Sequence[str], why: str) -> None:
    """Mark the passed instances of `names` invalidated in the run record, so the next pass
    runs them again. The caller holds the run lock."""
    record = _read_record(directory)
    if record is None:
        return
    changed = False
    for entry in record["stages"].values():
        if entry["stage"] in names and entry["status"] == PASSED:
            entry.update(status=INVALIDATED, invalidation=why)
            changed = True
    if changed:
        run_record.write(directory, record)


def _stall(directory: Path) -> dict[str, Any] | None:
    path = directory / STALL_FILE
    try:
        found = json.loads(path.read_text()) if path.is_file() else None
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


# The controller.


@dataclass
class _Pass:
    """The pass running now: the fleet and generation it started from, its input digests and
    the Stop that ends it early."""

    uid: str
    generation: int
    digests: dict[str, str]
    stop: Stop = field(default_factory=Stop)


@dataclass
class _Outcome:
    """How a reconcile left the fleet: the status reasons it reports."""

    stalled: tuple[str, str] | None = None
    retrying: str | None = None
    stopping: bool = False
    stopped: bool = False


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
        settings: Settings,
        lease: Leadership,
        state: Path = STATE,
        cluster: Callable[[Mapping[str, str], Path], Cluster] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        exit: Callable[[int], Any] = os._exit,
    ) -> None:
        self.client = client
        self.namespace = client.namespace
        self.settings = settings
        self.lease = lease
        self.state = state
        self._cluster = cluster or (lambda env, logs: kubernetes_target.ApiCluster(client))
        self._clock = clock
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

    # Leadership.

    def elect(self) -> bool:
        """Claim or renew the Lease once; return whether this controller may act now."""
        try:
            if self.lease.owned:
                self.lease.renew()
            else:
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
            running = self._pass if self._pass is not None and self._pass.uid == uid else None
        if event.type == "DELETED":
            self._seen.pop(object_key, None)
            self._written.pop(uid, None)
            self._ready.pop(uid, None)
            if running is not None:
                # The garbage collector removes what the pass made; it starts nothing more.
                running.stop.cancel(*STAGES)
            self.queue.add(object_key)
            return
        seen = (uid, generation, requested)
        if event.type == "MODIFIED" and self._seen.get(object_key) == seen:
            # A status write, or a metadata change no reconcile reads.
            return
        self._seen[object_key] = seen
        if running is not None and generation != running.generation:
            self._interrupt(running, fleet)
        self.queue.add(object_key)

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
        stall = _stall(directory)
        if stall and (stall.get("generation"), stall.get("requested_at")) == (
            generation,
            _requested(fleet),
        ):
            return _Outcome(stalled=(STAGE_FAILED, str(stall.get("message") or "")))
        return self._run(fleet, directory, env)

    def _deployment(self, directory: Path, env: Mapping[str, str]) -> KubernetesDeployment:
        deployment = KubernetesDeployment(directory, env, cluster=self._cluster, max_age=MAX_AGE)
        deployment.announce = log.info
        return deployment

    def _stop(self, directory: Path, uid: str) -> None:
        """Delete the routers, HAProxy, the engines and the Jobs, keeping volumes and record."""
        _private_dirs(directory)
        env = {kubernetes_target.NAMESPACE: self.namespace, kubernetes_target.FLEET_UID: uid}
        deployment = self._deployment(directory, env)
        with run_record.locked(directory):
            deleted = deployment.stop(deployment.cluster())
            _forget(directory, STOP_INVALIDATES, "spec.stopped deleted the objects it applied")
        if deleted:
            log.info("stopped the fleet: deleted %d objects", len(deleted))

    def _run(self, fleet: dict[str, Any], directory: Path, env: dict[str, str]) -> _Outcome:
        uid, generation = _uid(fleet), _generation(fleet)
        spec = _spec(fleet)
        inputs = kubernetes_target.inputs(env, policy=self.settings.policy)
        current = _Pass(uid, generation, {name: value.digest for name, value in inputs.items()})
        deployment = self._deployment(directory, env)

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
            result = deployment.runner().run(inputs, until=spec.get("until"), stop=current.stop)
        except StageFailed as exc:
            entry = exc.record
            if entry["status"] in (FAILED, TIMEOUT):
                message = f"{exc.key} {entry['status']}: {entry['failure']['message']}"
                stages.write_evidence(
                    directory / STALL_FILE,
                    {
                        "generation": generation,
                        "requested_at": _requested(fleet),
                        "stage": exc.key,
                        "message": message,
                    },
                )
                return _Outcome(stalled=(STAGE_FAILED, message))
            # An interrupted attempt is adopted by the next pass, and never stalls the fleet.
            return _Outcome(retrying=str(exc))
        except RunLocked as exc:
            return _Outcome(retrying=str(exc))
        except HelpersSurvived as exc:
            return _Outcome(stalled=(HELPERS_SURVIVED, str(exc)))
        except InputError as exc:
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
        found = progress(record, until=spec.get("until"), engines=engines)
        stopped = bool(spec.get("stopped"))
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
            ready_condition = _condition(READY, False, "Stalled", outcome.stalled[1])
        elif outcome.stopped:
            stalled = _condition(STALLED, False, "Stopped", "the fleet is stopped")
            reconciling = _condition(RECONCILING, False, "Stopped", "the fleet is stopped")
            ready_condition = _condition(READY, False, "Stopped", "spec.stopped is true")
        elif ready:
            stalled = _condition(STALLED, False, "Ready", "every stage passed")
            reconciling = _condition(RECONCILING, False, "Ready", "every stage passed")
            ready_condition = _condition(
                READY, True, "Passed", f"every stage up to {found.stage} passed"
            )
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
            "serving": found.serving and not stopped and not outcome.stopped,
            "engines": "0/" + found.engines.split("/")[1] if outcome.stopped else found.engines,
            "stages": found.stages,
            "previousRuns": list(self._previous.get(uid, [])),
        }
        if found.stage:
            desired["stage"] = found.stage
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
            written = self.client.patch(FLEET, fleet["metadata"]["name"], operations, status=True)
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
    settings = Settings.load(args.settings)
    holder = os.environ.get(POD_NAME_ENV, "")
    if not holder:
        parser.error(f"set {POD_NAME_ENV} to the pod's name")
    connection = kubernetes_api.in_cluster()
    client = ApiClient(connection, field_manager=kubernetes_objects.MANAGER)
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
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _exit_now)
    Controller(client, settings=settings, lease=lease, state=args.state).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
