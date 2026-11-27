"""Ownership leases for fenced router failover, and the file-backed lease of SSH hosts.

`RouterLease` is what the router needs from a lease. `FileLease` implements it on shared
storage, and `kubernetes_lease.KubernetesLease` on a `coordination.k8s.io` Lease.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from ..contracts import LEASE, ContractVersionError, validate_document, versioned

# Seconds between attempts on a held coordination lock. A blocking lock on a network file
# system waits out the client's retry backoff instead, which can outlast the lease itself.
LOCK_RETRY_S = 0.01
# Seconds of relative clock skew between router hosts that a lease reserves before its expiry
# by default. The deployment's lease check fails a pair whose hosts' clocks differ by more.
DEFAULT_SAFETY_MARGIN_S = 1.0
# The file the deployment's lease check leaves in the shared lease directory. A router given
# it refuses to touch the lease while it is missing, as it is in an unmounted directory.
WITNESS_NAME = ".narwhal-lease-witness"
# A lease temporary file older than this many lease TTLs lost its writer mid-write: a live
# writer renames its temporary file within one write.
ORPHAN_TTLS = 10


class LeaseError(RuntimeError):
    """A shared lease read or update failed."""


@dataclass(frozen=True)
class LeaseRecord:
    """One holder's epoch and wall-clock expiry. An empty holder is a free lease."""

    epoch: int
    holder: str
    expires_at: float
    updated_at: float


class RouterLease(Protocol):
    """The lease one router of a pair holds while it controls the fleet.

    `claim` takes a free or expired lease and advances its epoch, `renew` keeps it, and
    `release` frees it after the router has stopped controlling the fleet. `valid` is the fence:
    the router admits traffic only while it is true, and it turns false on the router's own
    monotonic clock before any other router can claim the lease.
    """

    holder: str
    epoch: int

    @property
    def owned(self) -> bool: ...

    def valid(self) -> bool: ...

    def serving_delay(self) -> float:
        """Seconds a held lease waits before it is valid; 0 once it may serve."""
        ...

    def read(self) -> LeaseRecord | None: ...

    def claim(self) -> bool: ...

    def renew(self) -> bool: ...

    def release(self) -> None: ...


class FileLease:
    """Coordinate one active router through a shared POSIX lock and JSON file."""

    def __init__(
        self,
        path: Path,
        holder: str,
        ttl_s: float,
        *,
        safety_margin_s: float = 0.0,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        witness: Path | None = None,
    ) -> None:
        if not holder:
            raise ValueError("lease holder must not be empty")
        if not math.isfinite(ttl_s):
            raise ValueError("lease ttl must be finite")
        if not math.isfinite(safety_margin_s):
            raise ValueError("lease safety margin must be finite")
        if ttl_s <= 0:
            raise ValueError("lease ttl must be positive")
        if safety_margin_s < 0 or safety_margin_s >= ttl_s:
            raise ValueError("lease safety margin must be nonnegative and less than the ttl")
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.witness = witness
        self.holder = holder
        self.ttl_s = ttl_s
        self.safety_margin_s = safety_margin_s
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._sleep = sleep
        self.epoch = 0
        self._valid_until = 0.0
        self._owned = False

    @property
    def owned(self) -> bool:
        """Return whether this process last acquired or renewed the lease."""
        return self._owned

    def valid(self) -> bool:
        """Return whether local ownership remains inside its monotonic deadline."""
        return self._owned and self._monotonic_clock() < self._valid_until

    def serving_delay(self) -> float:
        """A claimed file lease is valid at once."""
        return 0.0

    def remove_orphans(self) -> list[Path]:
        """Remove this lease's temporary files that a writer left when it died mid-write.

        Only regular files named as `_write_unlocked` or `_storage_now` name them, for this
        lease file, and last modified more than `ORPHAN_TTLS` lease TTLs before the lease
        storage's own clock, are removed. Nothing else in the directory is touched. It holds
        the lease lock, so no writer of this lease is mid-write meanwhile.
        """
        name = re.compile(rf"\.{re.escape(self.path.name)}\.[0-9]+\.[0-9a-f]{{32}}\.(tmp|now)")
        with self._locked():
            # File times on shared storage come from its server's clock, not this host's.
            cutoff = self._storage_now() - ORPHAN_TTLS * self.ttl_s
            entries = sorted(self.path.parent.iterdir())
            return [entry for entry in entries if self._orphaned(entry, name, cutoff)]

    @staticmethod
    def _orphaned(entry: Path, name: re.Pattern[str], cutoff: float) -> bool:
        """Remove `entry` if it is an orphaned temporary file, and say whether it was."""
        if not name.fullmatch(entry.name):
            return False
        try:
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                return False
            entry.unlink()
        except FileNotFoundError:
            return False
        return True

    def _storage_now(self) -> float:
        """The lease storage's clock: the modification time of a file created on it now."""
        probe = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.now")
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.close(descriptor)
            return os.stat(probe).st_mtime
        finally:
            with contextlib.suppress(FileNotFoundError):
                probe.unlink()

    def read(self) -> LeaseRecord | None:
        """Read the shared record while holding the coordination lock."""
        try:
            with self._locked():
                return self._read_unlocked()
        except OSError as exc:
            raise LeaseError(f"cannot read lease {self.path}: {exc}") from exc

    def claim(self) -> bool:
        """Acquire an absent or expired lease and advance its epoch."""
        epoch = 0
        valid_until = 0.0
        try:
            with self._locked():
                now = self._wall_clock()
                valid_until = self._monotonic_clock() + self.ttl_s - self.safety_margin_s
                current = self._read_unlocked()
                if current is not None and current.expires_at > now:
                    if current.holder != self.holder:
                        self._owned = False
                        return False
                    epoch = current.epoch
                else:
                    epoch = (current.epoch if current is not None else 0) + 1
                self._write_unlocked(LeaseRecord(epoch, self.holder, now + self.ttl_s, now))
        except (OSError, ContractVersionError, LeaseError, ValueError) as exc:
            self._owned = False
            raise LeaseError(f"cannot claim lease {self.path}: {exc}") from exc
        self.epoch = epoch
        self._valid_until = valid_until
        self._owned = True
        return True

    def renew(self) -> bool:
        """Extend this holder's unexpired epoch, or fence local ownership."""
        if not self._owned:
            return False
        valid_until = 0.0
        try:
            with self._locked(self._valid_until):
                now = self._wall_clock()
                valid_until = self._monotonic_clock() + self.ttl_s - self.safety_margin_s
                current = self._read_unlocked()
                if (
                    current is None
                    or current.holder != self.holder
                    or current.epoch != self.epoch
                    or current.expires_at <= now
                ):
                    self._owned = False
                    return False
                self._write_unlocked(LeaseRecord(self.epoch, self.holder, now + self.ttl_s, now))
        except (OSError, ContractVersionError, LeaseError, ValueError):
            return False
        self._valid_until = valid_until
        return True

    def release(self) -> None:
        """Expire this holder's epoch without disturbing a replacement holder."""
        if not self._owned:
            return
        try:
            with self._locked():
                now = self._wall_clock()
                current = self._read_unlocked()
                if (
                    current is not None
                    and current.holder == self.holder
                    and current.epoch == self.epoch
                ):
                    self._write_unlocked(LeaseRecord(self.epoch, self.holder, now, now))
        except (OSError, ContractVersionError, LeaseError, ValueError):
            pass
        self._owned = False
        self._valid_until = 0.0

    @contextlib.contextmanager
    def _locked(self, deadline: float | None = None) -> Iterator[TextIO]:
        """Hold the coordination lock, waiting until the monotonic deadline at most.

        The default deadline is one lease validity period from now. A renewal passes the
        holder's own deadline, so a lock it cannot take in time fences the holder.
        """
        if deadline is None:
            deadline = self._monotonic_clock() + self.ttl_s - self.safety_margin_s
        # The lease directory is shared storage. A missing one is a missing mount, and creating
        # it would give this host a lease of its own.
        if not self.path.parent.is_dir():
            raise LeaseError(f"lease directory {self.path.parent} does not exist")
        if self.witness is not None and not self.witness.is_file():
            raise LeaseError(
                f"lease witness {self.witness} is missing; the shared lease directory is not "
                "mounted here"
            )
        lock = self.lock_path.open("a+")
        try:
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self._monotonic_clock() >= deadline:
                        raise LeaseError(
                            f"lock {self.lock_path} stayed held past the wait deadline"
                        ) from None
                    self._sleep(LOCK_RETRY_S)
            try:
                yield lock
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()

    def _read_unlocked(self) -> LeaseRecord | None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LeaseError(f"malformed lease document: {exc}") from exc
        validate_document(raw, LEASE)
        required = {"schema", "schema_version", "epoch", "holder", "expires_at", "updated_at"}
        unknown = sorted(set(raw) - required)
        missing = sorted(required - set(raw))
        if unknown or missing:
            detail = []
            if unknown:
                detail.append(f"unknown fields {unknown}")
            if missing:
                detail.append(f"missing fields {missing}")
            raise LeaseError("; ".join(detail))
        epoch = raw["epoch"]
        holder = raw["holder"]
        expires_at = raw["expires_at"]
        updated_at = raw["updated_at"]
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise LeaseError("epoch must be a positive integer")
        if not isinstance(holder, str) or not holder:
            raise LeaseError("holder must be a nonempty string")
        for name, value in (("expires_at", expires_at), ("updated_at", updated_at)):
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise LeaseError(f"{name} must be a number")
            if not math.isfinite(value):
                raise LeaseError(f"{name} must be finite")
        return LeaseRecord(epoch, holder, float(expires_at), float(updated_at))

    def _write_unlocked(self, record: LeaseRecord) -> None:
        document = versioned(
            LEASE,
            {
                "epoch": record.epoch,
                "holder": record.holder,
                "expires_at": record.expires_at,
                "updated_at": record.updated_at,
            },
        )
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        # `remove_orphans` matches exactly this name.
        try:
            temporary.write_text(json.dumps(document, sort_keys=True) + "\n")
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
