"""Lifecycle errors, drain records, readmission outcomes and membership change counts."""

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
    # The drain removes the engine from the fleet once it completes.
    leaving: bool = False


# Outcomes of the joins and leaves a router process counts.
JOIN_OUTCOMES = ("joined", "refused")
LEAVE_OUTCOMES = ("left", "refused", "cancelled")


@dataclass
class MembershipCounts:
    """Join and leave outcomes since the router process started; a handoff does not carry them."""

    joins: dict[str, int] = field(default_factory=lambda: dict.fromkeys(JOIN_OUTCOMES, 0))
    leaves: dict[str, int] = field(default_factory=lambda: dict.fromkeys(LEAVE_OUTCOMES, 0))


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
