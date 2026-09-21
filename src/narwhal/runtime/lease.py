"""File-backed ownership lease for fenced router failover."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ..contracts import LEASE, ContractVersionError, validate_document, versioned


class LeaseError(RuntimeError):
    """A shared lease read or update failed."""


@dataclass(frozen=True)
class LeaseRecord:
    """One holder's epoch and wall-clock expiry."""

    epoch: int
    holder: str
    expires_at: float
    updated_at: float


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
    ) -> None:
        if not holder:
            raise ValueError("lease holder must not be empty")
        if ttl_s <= 0:
            raise ValueError("lease ttl must be positive")
        if safety_margin_s < 0 or safety_margin_s >= ttl_s:
            raise ValueError("lease safety margin must be nonnegative and less than the ttl")
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.holder = holder
        self.ttl_s = ttl_s
        self.safety_margin_s = safety_margin_s
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
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
            with self._locked():
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
    def _locked(self) -> Iterator[TextIO]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.lock_path.open("a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield lock
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
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
        try:
            temporary.write_text(json.dumps(document, sort_keys=True) + "\n")
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
