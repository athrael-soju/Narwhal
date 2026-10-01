"""Engine availability, quarantine, drains, and scoped breaker recovery evidence."""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable
from typing import Any

from ..types import (
    FAILURE_CLASSES,
    LEG_CLASSES,
    LEG_CONNECTION,
    LEG_INFERENCE_STATUS,
    LEG_KV_HANDOFF,
    LEG_OVERLOAD,
    LEG_STREAM,
    LEG_TIMEOUT,
    LIVENESS,
    Instance,
    Role,
)
from .monitor import InstanceMonitor

log = logging.getLogger("narwhal.scheduler")

# Verdict for each breaker class at `eject_after` consecutive failures.
# Inference failures require a two-leg probe of the failing path.
_VERDICT_BY_CLASS = {
    LEG_CONNECTION: "eject",
    LEG_TIMEOUT: "verify_health",
    LEG_OVERLOAD: "verify_health",
    LEG_INFERENCE_STATUS: "verify_inference",
    LEG_KV_HANDOFF: "verify_inference",
    LEG_STREAM: "verify_inference",
}

# Failure classes cleared by each answer kind. Health and inference
# verification also lift ejection and quarantine.
_EVIDENCE_CLASSES: dict[str, tuple[str, ...]] = {
    "transport": (LEG_CONNECTION, LEG_INFERENCE_STATUS),
    "health": (LEG_CONNECTION, LEG_TIMEOUT, LEG_OVERLOAD, LIVENESS),
    "prefill": (LEG_CONNECTION, LEG_INFERENCE_STATUS, LEG_KV_HANDOFF),
    "decode": (LEG_CONNECTION, LEG_INFERENCE_STATUS, LEG_STREAM),
    "verification": (LEG_CONNECTION, LEG_INFERENCE_STATUS, LEG_KV_HANDOFF, LEG_STREAM),
}
_RECOVERY_EVIDENCE = ("health", "verification")


class EngineAvailability:
    """Endpoint hold-outs and the evidence that permits readmission."""

    def __init__(
        self,
        monitor: InstanceMonitor,
        clock: Callable[[], float],
        *,
        eject_after: int,
        on_change: Callable[[], None],
        on_eject: Callable[[str], None],
        pinned: frozenset[str] = frozenset(),
    ) -> None:
        self.monitor = monitor
        self.pinned = pinned
        self._clock = clock
        self.eject_after = eject_after
        self.refresh_floor_state = on_change
        self.on_eject = on_eject
        self.ejected: dict[str, float] = {}
        self.draining: set[str] = set()
        self.failures: dict[str, Counter[str]] = {klass: Counter() for klass in LEG_CLASSES}
        self.liveness_misses: dict[str, int] = {}
        self._last_sweep = 0.0
        self.quarantined: dict[str, float] = {}
        self.verifying: set[tuple[str, str]] = set()
        self.inference_suspects: set[str] = set()

    def record_failure(self, iid: str, klass: str = LEG_CONNECTION) -> str | None:
        """Record a failed leg and return its breaker verdict, or None below `eject_after`.

        Each class keeps its own consecutive streak.
        """
        if klass not in _VERDICT_BY_CLASS:
            raise ValueError(f"unknown breaker class {klass!r}")
        counter = self.failures[klass]
        counter[iid] += 1
        if iid in self.ejected or counter[iid] < self.eject_after:
            return None
        verdict = _VERDICT_BY_CLASS[klass]
        if verdict == "eject":
            return "eject" if self.eject(iid) else None
        return verdict

    def quarantine(self, iid: str, seconds: float) -> bool:
        """Hold a failed engine out of new placement for `seconds`; return whether it was held.

        An engine whose removal leaves its role unserved stays live.
        """
        if seconds <= 0 or iid in self.ejected:
            return False
        inst = self.monitor.instances.get(iid)
        if inst is None:
            return False
        now = self._clock()
        self._sweep_quarantine(now)
        if not self.role_covered_without(iid):
            return False
        self.quarantined[iid] = max(self.quarantined.get(iid, 0.0), now + seconds)
        if math.isinf(seconds):
            log.info("quarantined %s until inference verification", iid)
        else:
            log.info("quarantined %s for %.1fs after an engine fault", iid, seconds)
        self.refresh_floor_state()
        return True

    def _sweep_quarantine(self, now: float) -> None:
        """Drop quarantines that expired by `now`."""
        for iid in [k for k, until in self.quarantined.items() if until <= now]:
            del self.quarantined[iid]

    def quarantine_list(self) -> list[str]:
        """Remove expired quarantines and return the remaining engine IDs."""
        self._sweep_quarantine(self._clock())
        return sorted(self.quarantined)

    def live_instances(
        self,
        role: Role | None = None,
        *,
        exclude: set[str] | frozenset[str] = frozenset(),
    ) -> list[Instance]:
        """Instances available for new work or a controller move."""
        now = self._clock()
        self._sweep_quarantine(now)
        return [
            inst
            for inst in self.monitor.instances.values()
            if (role is None or inst.role is role)
            and inst.iid not in exclude
            and inst.iid not in self.ejected
            and inst.iid not in self.draining
            and inst.iid not in self.quarantined
        ]

    def eject(self, iid: str) -> bool:
        """Eject an engine after a failed verification probe."""
        if iid in self.ejected:
            return False
        self.ejected[iid] = self._clock()
        if self.on_eject is not None:
            self.on_eject(iid)
        self.quarantined.pop(iid, None)
        self._release_uncovered_holds()
        self.refresh_floor_state()
        return True

    def drain(self, iid: str) -> None:
        """Remove one configured engine from every new placement path."""
        if iid not in self.monitor.instances:
            raise KeyError(iid)
        self.draining.add(iid)
        self.quarantined.pop(iid, None)
        self._release_uncovered_holds()
        self.refresh_floor_state()

    def _release_uncovered_holds(self) -> None:
        """Return held engines whose roles no other live engine places."""
        for iid in list(self.quarantined):
            if not self.role_covered_without(iid):
                del self.quarantined[iid]
                log.warning("released hold on %s: no other live engine places its roles", iid)

    def finish_drain(self, iid: str) -> None:
        """Return a validated engine to placement."""
        if iid not in self.monitor.instances:
            raise KeyError(iid)
        self.draining.discard(iid)
        # Readmission validation counts as verification evidence.
        self.record_answer(iid, "verification")

    def role_pool(self, role: Role, instances: list[Instance]) -> list[Instance]:
        """Return the engines in `instances` that hold `role`, else the unpinned ones."""
        return [i for i in instances if i.role is role] or [
            i for i in instances if i.iid not in self.pinned
        ]

    def role_covered_without(self, iid: str) -> bool:
        """Return whether another live engine places every role that `iid` places.

        An engine places a role's legs when it holds that role or is unpinned.
        """
        inst = self.monitor.instances.get(iid)
        if inst is None:
            return True
        others = self.live_instances(exclude={iid})
        return all(
            self.role_pool(role, others)
            for role in Role
            if inst.role is role or iid not in self.pinned
        )

    def record_answer(self, iid: str, evidence: str) -> None:
        """Clear failure streaks for the paths exercised by the answer.

        Health and inference verification also clear ejection and quarantine.
        """
        try:
            classes = _EVIDENCE_CLASSES[evidence]
        except KeyError:
            raise ValueError(f"unknown breaker evidence {evidence!r}") from None
        for klass in classes:
            if klass == LIVENESS:
                self.liveness_misses.pop(iid, None)
            else:
                self.failures[klass].pop(iid, None)
        if evidence in _RECOVERY_EVIDENCE:
            if evidence == "verification":
                self.inference_suspects.discard(iid)
            if iid not in self.inference_suspects:
                self.ejected.pop(iid, None)
                self.quarantined.pop(iid, None)
        self.refresh_floor_state()

    def breaker_snapshot(self) -> dict[str, Any]:
        """Return per-engine breaker streaks and pending verifications.

        Every configured engine reports every class, zeros included.
        """
        streaks = {
            iid: {
                klass: (
                    self.liveness_misses.get(iid, 0)
                    if klass == LIVENESS
                    else self.failures[klass].get(iid, 0)
                )
                for klass in FAILURE_CLASSES
            }
            for iid in sorted(self.monitor.instances)
        }
        return {
            "failures": streaks,
            "verifying": [{"iid": iid, "kind": kind} for iid, kind in sorted(self.verifying)],
        }

    def sweep_due(self, after_s: float) -> bool:
        """Return whether a liveness sweep is due and advance its timestamp."""
        now = self._clock()
        if now - self._last_sweep < after_s:
            return False
        self._last_sweep = now
        return True

    def probe_due(self, after_s: float) -> list[str]:
        """Return due ejected engines and mark them probed."""
        now = self._clock()
        due = [
            iid
            for iid, at in self.ejected.items()
            if iid not in self.draining and now - at >= after_s
        ]
        for iid in due:
            self.ejected[iid] = now
        return due
