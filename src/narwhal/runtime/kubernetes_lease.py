"""A router lease on a `coordination.k8s.io` Lease, for a router pair on Kubernetes.

The fence follows client-go's leader election and never compares one host's clock with another's,
or with the API server's:

- A holder is `valid` until its own monotonic time when it sent its last successful write, plus
  `ttl_s - safety_margin_s`. Past that deadline it stops controlling the fleet whether or not the
  API server answers.
- Another router takes a held Lease only once it has seen one version of it, by uid and
  resourceVersion, stay unchanged for a lease duration on its own monotonic clock, and only by
  an update conditional on that version. Every write the holder lands is a new version, so the
  holder's deadline passes at least `safety_margin_s` before such a takeover.
- A release this router watched can be taken at once: the holder stopped controlling the fleet
  before it sent the release. A release is the same Lease, held and then freed at the same
  epoch, carrying the `RELEASED` annotation that names that holder and epoch. Any other free
  Lease, such as one an operator patched free, may hide a live holder.
- A Lease this router has to create, or a free Lease it never saw released, may hide a holder
  this router never saw. Such a claim holds the Lease at once and becomes valid one lease
  duration later.

A `Conflict` or an `OutcomeUnknown` is never read as a lost or won Lease: the router reads the
Lease again and checks `holderIdentity`, the epoch and the `renewTime` it wrote. `renewTime` and
`acquireTime` carry this router's wall clock for people reading the Lease; no decision reads
them.
"""

from __future__ import annotations

import copy
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from . import kubernetes_api as k
from .lease import DEFAULT_SAFETY_MARGIN_S, LeaseError, LeaseRecord

log = logging.getLogger("narwhal.kubernetes_lease")

FIELD_MANAGER = "narwhal-router"
# One quick retry. A request that waits out a long backoff only delays a fence it cannot prevent.
BACKOFF = k.Backoff(base_s=0.1, cap_s=0.5, attempts=2)
MAX_DURATION_S = 2**31 - 1
# The annotation a release writes, `<epoch>/<holder>`, and every claim removes.
RELEASED = "narwhal.io/released-by"
# Recent renewTime stamps this process wrote, to recognise its own write after a lost reply.
STAMPS_KEPT = 32
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")


class LeaseClient(Protocol):
    """The verbs a Lease needs: `kubernetes_api.ApiClient` in a pod."""

    def get(
        self, kind: k.Kind, name: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None: ...

    def create(self, kind: k.Kind, obj: Mapping[str, Any]) -> dict[str, Any]: ...

    def replace(self, kind: k.Kind, obj: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _Seen:
    """One version of the Lease and the monotonic time this router first read it."""

    uid: str
    version: str
    holder: str
    epoch: int
    duration_s: float
    at: float
    stamp: str
    released_by: str


def check_settings(name: str, ttl_s: float, safety_margin_s: float) -> None:
    """Refuse a Lease name or timing a Kubernetes Lease cannot hold."""
    if not isinstance(name, str) or len(name) > 253 or not _NAME.fullmatch(name):
        raise ValueError(f"Lease name {name!r} is not a DNS subdomain name")
    if not math.isfinite(ttl_s) or ttl_s <= 0 or ttl_s != int(ttl_s) or ttl_s > MAX_DURATION_S:
        raise ValueError("a Kubernetes Lease's ttl must be a whole positive number of seconds")
    if not math.isfinite(safety_margin_s) or not 0 < safety_margin_s < ttl_s:
        raise ValueError(
            "a Kubernetes Lease's safety margin must be positive and less than the ttl: it "
            "separates the holder's renew deadline from the lease duration"
        )


def _micro_time(microseconds: int) -> str:
    seconds, fraction = divmod(microseconds, 1_000_000)
    stamp = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{fraction:06d}Z"


class KubernetesLease:
    """Coordinate one active router through a `coordination.k8s.io` Lease.

    It offers `FileLease`'s interface. Its methods block on the API server for at most the
    client's timeout and retries, so the router calls them off its event loop; `valid` never
    blocks. One lock orders the calls of every thread.
    """

    def __init__(
        self,
        client: LeaseClient,
        name: str,
        holder: str,
        ttl_s: float,
        *,
        safety_margin_s: float = DEFAULT_SAFETY_MARGIN_S,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not holder:
            raise ValueError("lease holder must not be empty")
        check_settings(name, ttl_s, safety_margin_s)
        self.name = name
        self.holder = holder
        self.ttl_s = float(ttl_s)
        self.safety_margin_s = safety_margin_s
        self.epoch = 0
        self._client = client
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._owned = False
        self._valid_until = 0.0
        self._not_before = -math.inf
        # The Lease as this router last wrote it, or read it while holding it.
        self._object: dict[str, Any] = {}
        self._seen: _Seen | None = None
        # The (uid, resourceVersion) of a release this router saw happen.
        self._released: tuple[str, str] | None = None
        self._stamp_us = 0
        self._stamps: deque[str] = deque(maxlen=STAMPS_KEPT)

    @classmethod
    def in_cluster(
        cls,
        name: str,
        holder: str,
        ttl_s: float,
        *,
        safety_margin_s: float = DEFAULT_SAFETY_MARGIN_S,
    ) -> KubernetesLease:
        """The Lease `name` in the pod's namespace, through its ServiceAccount token.

        Each request waits half the renew deadline at most, and two seconds at most.
        """
        check_settings(name, ttl_s, safety_margin_s)
        connection = k.in_cluster()
        timeout_s = min(2.0, max(0.25, (ttl_s - safety_margin_s) / 2))
        client = k.ApiClient(
            connection, field_manager=FIELD_MANAGER, backoff=BACKOFF, timeout_s=timeout_s
        )
        return cls(client, name, holder, ttl_s, safety_margin_s=safety_margin_s)

    # The fence.

    @property
    def owned(self) -> bool:
        """Return whether this process last claimed or renewed the Lease."""
        return self._owned

    def valid(self) -> bool:
        """Return whether this router may control the fleet now, on its monotonic clock."""
        now = self._monotonic_clock()
        return self._owned and self._not_before <= now < self._valid_until

    def serving_delay(self) -> float:
        """Seconds until a held Lease that may hide an unseen holder becomes valid."""
        if not self._owned:
            return 0.0
        return max(0.0, self._not_before - self._monotonic_clock())

    # Reads.

    def read(self) -> LeaseRecord | None:
        """Read the Lease. Its times are this router's estimate, on its own clock."""
        with self._lock:
            found = self._get()
            if found is None:
                return None
            _, seen = found
            observed = self._wall_clock() - (self._monotonic_clock() - seen.at)
            expires = observed + seen.duration_s if seen.holder else observed
            return LeaseRecord(seen.epoch, seen.holder, expires, observed)

    def _get(self) -> tuple[dict[str, Any], _Seen] | None:
        """The Lease and the version of it this router has seen, or None when it is absent."""
        try:
            lease = self._client.get(k.LEASE, self.name)
        except (k.KubernetesError, ValueError) as exc:
            raise LeaseError(f"cannot read Lease {self.name}: {exc}") from exc
        return None if lease is None else (lease, self._observe(lease))

    def _observe(self, lease: Mapping[str, Any]) -> _Seen:
        """Record the version `lease` is at, and when this router first saw it."""
        meta = lease.get("metadata") or {}
        spec = lease.get("spec") or {}
        holder = spec.get("holderIdentity") or ""
        epoch = spec.get("leaseTransitions") or 0
        duration = spec.get("leaseDurationSeconds") or 0
        if not isinstance(holder, str):
            raise LeaseError(f"Lease {self.name} holds a holderIdentity that is not a string")
        for field, value in (("leaseTransitions", epoch), ("leaseDurationSeconds", duration)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise LeaseError(f"Lease {self.name} holds an invalid {field}")
        version = (str(meta.get("uid") or ""), str(meta.get("resourceVersion") or ""))
        seen = self._seen
        if seen is not None and (seen.uid, seen.version) == version:
            return seen
        released_by = str((meta.get("annotations") or {}).get(RELEASED) or "")
        if (
            not holder
            and seen is not None
            and seen.uid == version[0]
            and seen.holder
            and seen.epoch == epoch
            and released_by == f"{epoch}/{seen.holder}"
        ):
            self._released = version
        # The holder's own duration counts when it is longer than this router's.
        self._seen = _Seen(
            *version,
            holder,
            epoch,
            max(float(duration), self.ttl_s),
            self._monotonic_clock(),
            str(spec.get("renewTime") or ""),
            released_by,
        )
        return self._seen

    # Writes.

    def claim(self) -> bool:
        """Take the Lease if it is absent, released, or expired on this router's clock."""
        with self._lock:
            try:
                return self._claim()
            except LeaseError:
                self._owned = False
                raise

    def _claim(self) -> bool:
        found = self._get()
        now = self._monotonic_clock()
        if found is None:
            # A deleted Lease's holder may still be inside its renew deadline.
            longest = max(self.ttl_s, self._seen.duration_s if self._seen else 0.0)
            return self._create(not_before=now + longest)
        lease, seen = found
        if seen.holder == self.holder and seen.stamp in self._stamps:
            # This router's own write, whose reply was lost.
            return self._take(lease, seen.epoch, not_before=-math.inf)
        if seen.holder:
            if now - seen.at < seen.duration_s:
                self._owned = False
                return False
            return self._take(lease, seen.epoch + 1, not_before=-math.inf)
        if self._released == (seen.uid, seen.version):
            return self._take(lease, seen.epoch + 1, not_before=-math.inf)
        # Freed by something other than a release this router saw, perhaps under a live holder.
        return self._take(lease, seen.epoch + 1, not_before=seen.at + seen.duration_s)

    def _create(self, *, not_before: float) -> bool:
        stamp = self._next_stamp()
        body = {
            "apiVersion": k.LEASE.api_version,
            "kind": k.LEASE.kind,
            "metadata": {"name": self.name},
            "spec": self._spec(1, stamp, acquired=True),
        }
        sent = self._monotonic_clock()
        try:
            written = self._client.create(k.LEASE, body)
        except k.Conflict:
            # Another router created it first. Read it, so this router's clock starts.
            self._get()
            self._owned = False
            return False
        except k.OutcomeUnknown:
            found = self._confirm(stamp, 1)
            if found is None:
                self._owned = False
                return False
            written = found
        except (k.KubernetesError, ValueError) as exc:
            raise LeaseError(f"cannot create Lease {self.name}: {exc}") from exc
        self._hold(written, 1, sent, not_before)
        return True

    def _take(self, lease: Mapping[str, Any], epoch: int, *, not_before: float) -> bool:
        stamp = self._next_stamp()
        body = copy.deepcopy(dict(lease))
        # A held Lease carries no release mark, so a later free version without one is no release.
        annotations = (body.get("metadata") or {}).get("annotations")
        if isinstance(annotations, dict):
            annotations.pop(RELEASED, None)
        acquired = (body.get("spec") or {}).get("holderIdentity") != self.holder
        body["spec"] = {**(body.get("spec") or {}), **self._spec(epoch, stamp, acquired=acquired)}
        sent = self._monotonic_clock()
        try:
            written = self._client.replace(k.LEASE, body)
        except k.Conflict:
            # Another router wrote since the read. Read it, so this router's clock restarts.
            self._get()
            self._owned = False
            return False
        except k.NotFound:
            self._owned = False
            return False
        except k.OutcomeUnknown:
            found = self._confirm(stamp, epoch)
            if found is None:
                self._owned = False
                return False
            written = found
        except (k.KubernetesError, ValueError) as exc:
            raise LeaseError(f"cannot claim Lease {self.name}: {exc}") from exc
        self._hold(written, epoch, sent, not_before)
        return True

    def renew(self) -> bool:
        """Keep the Lease, and return whether this router still holds it.

        A failed write that leaves the Lease this router's, such as an unavailable API server,
        returns True while the renew deadline lasts. The deadline moves only with a write that
        landed, counted from when it was sent.
        """
        with self._lock:
            if not self._live():
                return False
            for _ in range(2):
                stamp = self._next_stamp()
                body = copy.deepcopy(self._object)
                body["spec"] = {
                    **(body.get("spec") or {}),
                    **self._spec(self.epoch, stamp, acquired=False),
                }
                sent = self._monotonic_clock()
                try:
                    written = self._client.replace(k.LEASE, body)
                except k.NotFound:
                    return self._lose("the Lease was deleted")
                except k.Conflict:
                    # Another write landed: this router's own lost reply, a change to the
                    # Lease's metadata, or another holder. Read it before deciding.
                    current = self._reread()
                    if current is None:
                        return self._live()
                    if not self._mine(current):
                        return self._lose("another router holds the Lease")
                    self._object = current
                    continue
                except k.OutcomeUnknown:
                    current = self._reread()
                    if current is None:
                        return self._live()
                    if not self._mine(current):
                        return self._lose("another router holds the Lease")
                    self._object = current
                    if (current.get("spec") or {}).get("renewTime") != stamp:
                        return self._live()
                    written = current
                except (k.KubernetesError, ValueError) as exc:
                    log.warning("Lease %s renewal failed: %s", self.name, exc)
                    return self._live()
                self._object = written
                self._valid_until = sent + self.ttl_s - self.safety_margin_s
                try:
                    self._observe(written)
                except LeaseError as exc:
                    log.warning("%s", exc)
                return True
            return self._live()

    def release(self) -> None:
        """Free the Lease after this router stopped controlling the fleet."""
        with self._lock:
            if not self._owned:
                return
            self._owned = False
            self._valid_until = 0.0
            lease = self._object
            for _ in range(2):
                body = copy.deepcopy(lease)
                spec = body.setdefault("spec", {})
                spec["holderIdentity"] = ""
                spec["renewTime"] = self._next_stamp()
                annotations = body.setdefault("metadata", {}).setdefault("annotations", {})
                annotations[RELEASED] = f"{self.epoch}/{self.holder}"
                try:
                    self._observe(self._client.replace(k.LEASE, body))
                    return
                except k.Conflict:
                    current = self._reread()
                    if current is None or not self._mine(current):
                        return
                    lease = current
                except (k.KubernetesError, ValueError, LeaseError) as exc:
                    log.warning("Lease %s release failed: %s", self.name, exc)
                    return

    # Helpers.

    def _spec(self, epoch: int, stamp: str, *, acquired: bool) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "holderIdentity": self.holder,
            "leaseDurationSeconds": int(self.ttl_s),
            "leaseTransitions": epoch,
            "renewTime": stamp,
        }
        if acquired:
            spec["acquireTime"] = stamp
        return spec

    def _hold(self, written: dict[str, Any], epoch: int, sent: float, not_before: float) -> None:
        self._object = written
        self._observe(written)
        self.epoch = epoch
        self._valid_until = sent + self.ttl_s - self.safety_margin_s
        self._not_before = max(self._not_before, not_before)
        self._owned = True

    def _confirm(self, stamp: str, epoch: int) -> dict[str, Any] | None:
        """Read the Lease after a write of unknown outcome: it, if that write landed."""
        found = self._get()
        if found is None:
            return None
        current, _ = found
        spec = current.get("spec") or {}
        if (spec.get("holderIdentity"), spec.get("leaseTransitions"), spec.get("renewTime")) == (
            self.holder,
            epoch,
            stamp,
        ):
            return current
        return None

    def _reread(self) -> dict[str, Any] | None:
        """The Lease, or None when it cannot be read or is absent."""
        try:
            found = self._get()
        except LeaseError as exc:
            log.warning("%s", exc)
            return None
        return None if found is None else found[0]

    def _mine(self, lease: Mapping[str, Any]) -> bool:
        uid = (self._object.get("metadata") or {}).get("uid")
        spec = lease.get("spec") or {}
        return (
            (lease.get("metadata") or {}).get("uid") == uid
            and spec.get("holderIdentity") == self.holder
            and (spec.get("leaseTransitions") or 0) == self.epoch
        )

    def _live(self) -> bool:
        """Whether the renew deadline still holds; past it this router no longer owns the Lease."""
        if self._owned and self._monotonic_clock() < self._valid_until:
            return True
        self._owned = False
        return False

    def _lose(self, why: str) -> bool:
        log.error("Lease %s lost at epoch %d: %s", self.name, self.epoch, why)
        self._owned = False
        self._valid_until = 0.0
        return False

    def _next_stamp(self) -> str:
        """A MicroTime after every one this router wrote, so each write is a new version."""
        self._stamp_us = max(int(self._wall_clock() * 1_000_000), self._stamp_us + 1)
        stamp = _micro_time(self._stamp_us)
        self._stamps.append(stamp)
        return stamp
