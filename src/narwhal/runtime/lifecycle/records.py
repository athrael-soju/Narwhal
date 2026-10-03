"""Lifecycle errors, drain records and readmission outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field


class LifecycleError(ValueError):
    """A lifecycle action violates the fleet contract."""


@dataclass
class DrainRecord:
    """Durable operator state for one engine."""

    iid: str
    state: str
    requested_at: float
    deadline_at: float
    restart_required: bool = True
    wave_id: str = ""
    old_process_start: float | None = None
    new_process_start: float | None = None
    error: str = ""
    checks: list[str] = field(default_factory=list)


@dataclass
class ValidationOutcome:
    """Readmission evidence collected before scheduler mutation."""

    starts: dict[str, float] = field(default_factory=dict)
    checks: dict[str, list[str]] = field(default_factory=dict)
    failures: dict[str, list[str]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Return whether every validation gate passed."""
        return not self.failures

    def ok(self, iid: str, message: str) -> None:
        """Record a passing engine gate."""
        self.checks.setdefault(iid, []).append(message)

    def fail(self, iid: str, message: str) -> None:
        """Record a failed engine gate."""
        self.failures.setdefault(iid, []).append(message)
