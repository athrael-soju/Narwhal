"""Role-control contracts and prefill-floor breach accounting."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from ..types import Role

log = logging.getLogger("narwhal.scheduler")


@dataclass
class Flip:
    """Role change with the work resident at actuation time."""

    at: float
    iid: str
    to: Role
    by: str
    prefill_inflight: int
    decode_inflight: int
    # Drain time ends when work from the previous role has left.
    drained_s: float | None = None
    resident_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class SLO:
    """Latency targets used by placement and control."""

    ttft_s: float
    tpot_s: float

    def __post_init__(self) -> None:
        for name in ("ttft_s", "tpot_s"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value}")


@dataclass
class Thresholds:
    """Pool-load and timing thresholds for role changes.

    `expand` and `shrink` are pool loads, so 1.0 is exactly at target.
    `cooldown_s` guards P->D only.
    """

    expand: float = 1.0
    shrink: float = 0.5
    cooldown_s: float = 10.0
    # Require repeated crossings before the monitoring loop changes a role.
    sustained_intervals: int = 3
    # Minimum residence time before another role change.
    dwell_s: float = 0.0
    # Sustained decode pressure may bypass cooldown while prefill remains below
    # shrink. Zero disables the bypass.
    panic_ratio: float = 0.0
    # Refuse a decode-to-prefill flip whose donor carries more resident decode
    # streams than this. Heavy flips trigger a delayed decode-latency storm on
    # the donor's legacy lanes; zero disables the guard.
    flip_resident_guard: int = 0

    def __post_init__(self) -> None:
        for name in ("expand", "shrink", "cooldown_s", "dwell_s", "panic_ratio"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value}")


class PrefillFloor:
    """Track breach edges only after the configured prefill floor was first met."""

    def __init__(
        self,
        clock: Callable[[], float],
        live: Callable[[], int],
        quarantine_list: Callable[[], list[str]],
        ejected: dict[str, float],
        minimum: int,
        on_event: Callable[[dict], None] | None,
    ) -> None:
        self._clock = clock
        self.prefill_live = live
        self.quarantine_list = quarantine_list
        self.ejected = ejected
        self.minimum = minimum
        self.on_event = on_event
        self._breach_since: float | None = None
        self._breaches = 0
        self._below_s = 0.0
        self._met = live() >= minimum

    def refresh(self) -> None:
        """Update prefill-floor breach accounting.

        Counting begins after the fleet first reaches the configured floor.
        """
        now = self._clock()
        below = self.prefill_live() < self.minimum
        if not below:
            self._met = True
            if self._breach_since is None:
                return
            duration = now - self._breach_since
            self._below_s += duration
            self._breach_since = None
            log.info("prefill pool restored to min_prefill after %.2fs", duration)
            if self.on_event is not None:
                self.on_event(
                    {
                        "event": "below_floor_recovered",
                        "at": now,
                        "duration_s": duration,
                        "live_prefill": self.prefill_live(),
                        "min_prefill": self.minimum,
                    }
                )
        elif self._met and self._breach_since is None:
            self._breach_since = now
            self._breaches += 1
            log.warning(
                "prefill pool below min_prefill: %d live, floor %d",
                self.prefill_live(),
                self.minimum,
            )
            if self.on_event is not None:
                self.on_event(
                    {
                        "event": "below_floor",
                        "at": now,
                        "live_prefill": self.prefill_live(),
                        "min_prefill": self.minimum,
                        "ejected": sorted(self.ejected),
                        "quarantined": self.quarantine_list(),
                    }
                )

    def snapshot(self) -> dict:
        """Return the `below_floor` state exposed by `/narwhal/state`."""
        return {
            "active": self._breach_since is not None,
            "live_prefill": self.prefill_live(),
            "since": self._breach_since,
            "breaches": self._breaches,
            "cumulative_s": round(
                self._below_s
                + ((self._clock() - self._breach_since) if self._breach_since is not None else 0.0),
                4,
            ),
        }
