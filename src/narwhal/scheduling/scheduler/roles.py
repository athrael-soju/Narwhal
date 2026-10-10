"""Change engine roles under pins, floors, dwell and cooldown, and record decisions."""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

from ...types import Instance, Phase, Role
from ..control import Flip
from ..costs import Cost
from ..prefill import resident_prefill_seconds

if TYPE_CHECKING:
    from .placement import GlobalScheduler

log = logging.getLogger("narwhal.scheduler")


class RoleChanges:
    """Live role changes, controller decisions and their retained history."""

    def __init__(self, scheduler: GlobalScheduler, flip_history: int) -> None:
        self.scheduler = scheduler
        # The opening P-to-D change also waits out the cooldown.
        self._last_p2d_flip = scheduler._clock()
        self.panic_bypasses = 0
        self._panic_sustained = 0
        self._last_flip: dict[str, float] = {}
        # Bound telemetry retained for `/narwhal/state`.
        self._flip_history = flip_history
        self.flips: list[Flip] = []
        self.flips_refused: list[tuple[float, str, str]] = []
        # Process-lifetime counters, independent of the bounded histories.
        self._flip_counts: Counter[tuple[str, str]] = Counter()
        self._flip_reversals = 0
        self._flip_refusals = 0
        self._flip_inflight: Counter[str] = Counter()
        self._last_flip_role: dict[str, Role] = {}
        self._decode_floor_restores = 0
        self._last_decision: dict | None = None
        self._decision_counts: Counter[tuple[str, str]] = Counter()

    def decode_floor_snapshot(self) -> dict:
        """Report live decode capacity and floor recovery."""
        live_decode = self.scheduler.decode_live()
        return {
            "min_decode": self.scheduler.min_decode,
            "live_decode": live_decode,
            "below_floor": live_decode < self.scheduler.min_decode,
            "restoration_moves": self._decode_floor_restores,
        }

    def control_snapshot(self) -> dict:
        """Return advisory mode and the most recent controller decision."""
        return {
            "advisory": self.scheduler.advisory,
            "last_decision": dict(self._last_decision) if self._last_decision else None,
            "decisions": {
                f"{by}:{result}": count
                for (by, result), count in sorted(self._decision_counts.items())
            },
            "flips": {f"{by}:{to}": n for (by, to), n in sorted(self._flip_counts.items())},
            "flip_reversals": self._flip_reversals,
            "flips_refused": self._flip_refusals,
            "flip_inflight": {phase.value: self._flip_inflight[phase.value] for phase in Phase},
        }

    def _record_flip_refusal(self, target: Role, reason: str) -> None:
        self._flip_refusals += 1
        self.flips_refused.append((self.scheduler._clock(), target.value, reason))
        del self.flips_refused[: -self._flip_history]

    def _control_event(self, event: str, **fields: object) -> None:
        if self.scheduler.on_control_event is not None:
            self.scheduler.on_control_event(
                {"event": event, "at": self.scheduler._clock(), **fields}
            )

    def record_decision(
        self,
        *,
        prefill: int,
        decode: int,
        by: str,
        reason: str,
        result: str,
        details: dict[str, object] | None = None,
    ) -> None:
        """Retain one controller decision for state and metric output."""
        self._decision_counts[(by, result)] += 1
        self._last_decision = {
            "at": self.scheduler._clock(),
            "prefill": prefill,
            "decode": decode,
            "by": by,
            "reason": reason,
            "result": result,
            "applied": result == "applied",
            **(details or {}),
        }
        self._control_event(
            "controller_decision",
            **{key: value for key, value in self._last_decision.items() if key != "at"},
        )

    def allow_decode_scale_down(
        self, by: str, *, decision_details: dict[str, object] | None = None
    ) -> bool:
        """Return whether one live decode engine may move to prefill."""
        if self.scheduler.decode_live() - 1 >= self.scheduler.min_decode:
            return True
        reason = f"min_decode keeps {self.scheduler.min_decode} live decode engines"
        proposed_prefill = len(self.scheduler.monitor.pool(Role.PREFILL)) + 1
        self.record_decision(
            prefill=proposed_prefill,
            decode=len(self.scheduler.monitor.instances) - proposed_prefill,
            by=by,
            reason=reason,
            result="blocked",
            details=decision_details,
        )
        self._record_flip_refusal(Role.PREFILL, reason)
        return False

    def flip_cost(self, inst: Instance) -> Cost:
        """Return `(indicator, resident work)` for changing an engine's role.

        The indicator is 0 while the other phase's work is still resident.
        """
        profile = self.scheduler.profiles.get(inst.iid)
        if inst.role is Role.PREFILL:
            indicator = 0.0 if inst.decode else 1.0
            resident = (
                resident_prefill_seconds(profile, inst) if profile else float(inst.prefill_tokens())
            )
            return (indicator, resident)
        indicator = 0.0 if inst.prefill else 1.0
        return (indicator, float(inst.decode_tokens()))

    def planned_donor(
        self,
        target: Role,
        *,
        candidate: Instance | None = None,
        bypass_dwell: bool = False,
    ) -> tuple[Instance | None, str]:
        """Return the donor for a change toward `target`, or None and the blocking reason."""
        source = Role.DECODE if target is Role.PREFILL else Role.PREFILL
        pool = [
            inst
            for inst in self.scheduler.live_instances(source)
            if inst.iid not in self.scheduler.pinned and (candidate is None or inst is candidate)
        ]
        if not pool:
            return None, "pins or nominated engine exclude every source candidate"
        if self.scheduler.switch_requires_idle:
            pool = [inst for inst in pool if not inst.prefill and not inst.decode]
            if not pool:
                return None, "an engine-side role switch needs an idle source engine"
        if self.scheduler.th.dwell_s > 0.0 and not bypass_dwell:
            now = self.scheduler._clock()
            pool = [
                inst
                for inst in pool
                if now - self._last_flip.get(inst.iid, float("-inf")) >= self.scheduler.th.dwell_s
            ]
            if not pool:
                return None, "source engine dwell has not elapsed"
        if any(profile.colocated_group for profile in self.scheduler.profiles.all_profiles()):
            roles = {iid: inst.role for iid, inst in self.scheduler.monitor.instances.items()}
            prefill = sum(role is Role.PREFILL for role in roles.values())
            prefill += 1 if target is Role.PREFILL else -1
            pool = [
                inst
                for inst in pool
                if self.scheduler.profiles.profiles_for_split(
                    roles, prefill, len(roles) - prefill, roles | {inst.iid: target}
                )
            ]
            if not pool:
                return None, "measured shared-device profiles exclude every source candidate"
        return min(pool, key=self.flip_cost), ""

    def flip(
        self,
        target: Role,
        by: str = "?",
        *,
        candidate: Instance | None = None,
        bypass_cooldown: bool = False,
        bypass_dwell: bool = False,
        decision_details: dict[str, object] | None = None,
    ) -> Instance | None:
        """Apply a live role change toward `target`; return the moved engine or None.

        Pins, availability and role floors apply even with the bypass flags.
        """
        take_from = Role.DECODE if target is Role.PREFILL else Role.PREFILL
        now = self.scheduler._clock()

        def blocked(reason: str) -> None:
            self._record_flip_refusal(target, reason)
            current_prefill = len(self.scheduler.monitor.pool(Role.PREFILL))
            proposed_prefill = current_prefill + (1 if target is Role.PREFILL else -1)
            self.record_decision(
                prefill=proposed_prefill,
                decode=len(self.scheduler.monitor.instances) - proposed_prefill,
                by=by,
                reason=reason,
                result="blocked",
                details=decision_details,
            )

        if (
            target is Role.DECODE
            and not bypass_cooldown
            and now - self._last_p2d_flip < self.scheduler.th.cooldown_s
        ):
            if not self._panic_now():
                blocked("decode cooldown has not elapsed")
                return None
            self.panic_bypasses += 1
            log.info(
                "decode cooldown bypass: load %.2f >= %.1fx expand, "
                "prefill %.2f <= shrink, sustained %d passes",
                self.scheduler.pool_load(Role.DECODE),
                self.scheduler.th.panic_ratio,
                self.scheduler.pool_load(Role.PREFILL),
                self._panic_sustained,
            )

        # Ejected and quarantined engines add no target-pool capacity.
        live = self.scheduler.live_instances(take_from)
        if len(live) <= 1:
            blocked(f"{take_from.value} floor keeps the last live engine")
            return None
        # Pinned and dwelling engines still count toward the source pool floor.
        if take_from is Role.PREFILL and len(live) - 1 < self.scheduler.min_prefill:
            blocked(f"min_prefill keeps {self.scheduler.min_prefill} live prefill engines")
            return None
        if take_from is Role.DECODE and not self.allow_decode_scale_down(
            by, decision_details=decision_details
        ):
            return None
        chosen, donor_block = self.planned_donor(
            target, candidate=candidate, bypass_dwell=bypass_dwell
        )
        if chosen is None:
            blocked(donor_block)
            return None
        if (
            target is Role.PREFILL
            and self.scheduler.th.flip_resident_guard > 0
            and len(chosen.decode) > self.scheduler.th.flip_resident_guard
        ):
            blocked(
                f"lightest decode donor carries {len(chosen.decode)} residents, "
                f"above flip_resident_guard {self.scheduler.th.flip_resident_guard}"
            )
            return None
        current_prefill = len(self.scheduler.monitor.pool(Role.PREFILL))
        proposed_prefill = current_prefill + (1 if target is Role.PREFILL else -1)
        reason = {
            "decode_floor": "restore min_decode",
            "floor_recovery": "restore min_prefill",
            "reactive": "post-move objective improved",
        }.get(by, by)
        if self.scheduler.advisory:
            self.record_decision(
                prefill=proposed_prefill,
                decode=len(self.scheduler.monitor.instances) - proposed_prefill,
                by=by,
                reason=reason,
                result="advisory",
                details=decision_details,
            )
            self._record_flip_refusal(target, "advisory mode")
            return None
        self._flip_counts[(by, target.value)] += 1
        if self._last_flip_role.get(chosen.iid) not in (None, target):
            self._flip_reversals += 1
        self._last_flip_role[chosen.iid] = target
        self._flip_inflight[Phase.PREFILL.value] += len(chosen.prefill)
        self._flip_inflight[Phase.DECODE.value] += len(chosen.decode)
        chosen.role = target
        self._last_flip[chosen.iid] = now
        if target is Role.DECODE:
            self._last_p2d_flip = now
        self.flips.append(
            Flip(
                at=now,
                iid=chosen.iid,
                to=target,
                by=by,
                prefill_inflight=len(chosen.prefill),
                decode_inflight=len(chosen.decode),
                resident_ids=frozenset(chosen.decode if target is Role.PREFILL else chosen.prefill),
            )
        )
        self.record_decision(
            prefill=proposed_prefill,
            decode=len(self.scheduler.monitor.instances) - proposed_prefill,
            by=by,
            reason=reason,
            result="applied",
            details=decision_details,
        )
        del self.flips[: -self._flip_history]
        log.info(
            "FLIP %s %s -> %s | carrying %dP %dD",
            by,
            chosen.iid,
            target.value,
            len(chosen.prefill),
            len(chosen.decode),
        )
        if self.scheduler.on_flip is not None:
            self.scheduler.on_flip(chosen, target)
        self.scheduler.refresh_floor_state()
        return chosen

    def restore_decode_floor(self) -> Instance | None:
        """Restore one missing decode engine without waiting on cooldown."""
        if self.scheduler.decode_live() >= self.scheduler.min_decode:
            return None
        moved = self.flip(Role.DECODE, "decode_floor", bypass_cooldown=True)
        if moved is None:
            return None
        self._decode_floor_restores += 1
        self._control_event(
            "decode_floor_restored",
            iid=moved.iid,
            live_decode=self.scheduler.decode_live(),
            min_decode=self.scheduler.min_decode,
        )
        return moved

    def settle_drains(self) -> None:
        """Close the drain on any flip whose caught work has finished."""
        now = self.scheduler._clock()
        for f in self.flips:
            if f.drained_s is not None:
                continue
            inst = self.scheduler.monitor.instances.get(f.iid)
            if inst is None:
                continue
            stale = inst.decode if f.to is Role.PREFILL else inst.prefill
            if not f.resident_ids.intersection(stale):
                f.drained_s = now - f.at

    def observe_control_load(self, prefill: float, decode: float) -> None:
        """Count consecutive passes with decode at panic load and prefill at most `shrink`."""
        armed = (
            self.scheduler.th.panic_ratio > 0.0
            and decode >= self.scheduler.th.panic_ratio * self.scheduler.th.expand
            and prefill <= self.scheduler.th.shrink
        )
        self._panic_sustained = self._panic_sustained + 1 if armed else 0

    def _panic_now(self) -> bool:
        """Return whether current loads permit a cooldown bypass.

        Live loads are checked again after the sustained counter matures.
        """
        if self.scheduler.th.panic_ratio <= 0.0:
            return False
        if self._panic_sustained < self.scheduler.th.sustained_intervals:
            return False
        return (
            self.scheduler.pool_load(Role.DECODE)
            >= self.scheduler.th.panic_ratio * self.scheduler.th.expand
            and self.scheduler.pool_load(Role.PREFILL) <= self.scheduler.th.shrink
        )
