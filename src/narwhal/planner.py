"""The target-state plan loop: compute the split, confirm it, move at once.

The short form: each plan interval, estimate every pool's demand in
instances-worth from offered and backlogged work - never from served work,
which collapses exactly when a pool starves - and take each pool's need as
`ceil(demand / utilization)` (Dynamo's law, pinned-budget form). A pool
under its need moves now; pure rebalancing of surplus waits for
confirmation. All needed instances move in one pass, emptiest first.

This loop replaces Algorithm 2's trigger only. Algorithm 1's placement is
untouched, and its inline step-3 flips are suppressed while the planner
owns the pools (`GlobalScheduler.controller_owns_flips`), because two
controllers over one fleet fight.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

from .monitor import InstanceMonitor
from .scheduler import Flip, GlobalScheduler
from .types import Role

log = logging.getLogger("narwhal.planner")


class Planner:
    """One instance per router, driven from the monitoring pass."""

    def __init__(
        self,
        monitor: InstanceMonitor,
        scheduler: GlobalScheduler,
        *,
        clock: Callable[[], float],
        interval_s: float,
        window_s: float,
        confirmations: int,
        deadband: float = 0.5,
        utilization: float,
        min_arrivals: int,
        demand_floor: float,
        fast_step_s: float = 5.0,
        attainment_floor: float = 0.9,
    ) -> None:
        self.monitor = monitor
        self.scheduler = scheduler
        self._clock = clock
        self.interval_s = interval_s
        self.window_s = window_s
        self.confirmations_needed = confirmations
        self.deadband = deadband
        self.utilization = utilization
        self.min_arrivals = min_arrivals
        self.demand_floor = demand_floor
        self.fast_step_s = fast_step_s
        self.attainment_floor = attainment_floor
        self._hold_p_min = 0
        self._hold_d_min = 0
        self._hold_until = 0.0
        self._arrivals: list[tuple[float, int]] = []  # (at, input_len)
        self._residency: list[tuple[float, float]] = []  # (at, instances-worth)
        self._pending_target: int | None = None
        self._confirmations = 0
        self._next_plan = clock() + interval_s
        self._last_fast = clock()
        self.last_needs: tuple[float, float] = (0.0, 0.0)

    # -- observations ----------------------------------------------------

    def saw_arrival(self, input_len: int) -> None:
        """Called as a request is admitted; offered work is known at arrival."""
        self._arrivals.append((self._clock(), input_len))

    def sample(self) -> None:
        """Called every monitoring pass.

        The decode backlog drains in batch-sized bursts, so a snapshot read
        at plan time sawtooths between zero and twice the mean and the
        planner chases its own actuation. The window average is the signal.
        """
        ceiling = self.scheduler.profiles.mean_max_tokens(self.scheduler.slo.tpot_s)
        if ceiling is None:
            return
        resident = sum(i.decode_tokens() for i in self.monitor.instances.values())
        if ceiling > 0:
            self._residency.append((self._clock(), resident / ceiling))

    # -- the plan --------------------------------------------------------

    def _demand(self, now: float) -> tuple[float, float]:
        """(prefill, decode) demand in instances-worth: offered and backlog."""
        w0 = now - self.window_s
        self._arrivals = [a for a in self._arrivals if a[0] >= w0]
        self._residency = [r for r in self._residency if r[0] >= w0]
        span = min(self.window_s, now) or 1.0
        times = [self.scheduler.profiles.mean_prefill_time(n) for _, n in self._arrivals]
        prefill = (
            sum(t for t in times if t is not None) / span if times and times[0] is not None else 0.0
        )
        decode = (
            sum(v for _, v in self._residency) / len(self._residency) if self._residency else 0.0
        )
        return prefill, decode

    def fast_step(self) -> int:
        """The fast loop: starvation relief between plans.

        Called every monitoring pass, rate-capped internally. Fresh demand
        each check - never the stale plan - and grow-only toward a genuine
        deficit: a pool below its need grows one step when the other pool
        holds surplus; shrinking is the plan loop's job alone; those
        ratchets keep two loops from fighting - one controller, not two.
        """
        now = self._clock()
        if now - self._last_fast < self.fast_step_s:
            return 0
        if len(self._arrivals) < self.min_arrivals:
            return 0
        prefill, decode = self._demand(now)
        n = len(self.monitor.instances)
        need_p = math.ceil(prefill / self.utilization)
        need_d = math.ceil(decode / self.utilization)
        p = len(self.monitor.pool(Role.PREFILL))
        moved = 0
        if p < min(need_p, n - 1) and (n - p) > max(need_d, 1):
            moved = self._move_to(p + 1)
        elif (n - p) < min(need_d, n - 1) and p > max(need_p, 1):
            moved = self._move_to(p - 1)
        if moved:
            self._last_fast = now
        return moved

    def pass_due(self) -> int:
        """One planner pass if the interval elapsed; returns instances moved."""
        now = self._clock()
        if now < self._next_plan:
            return 0
        self._next_plan = now + self.interval_s
        want = self._target(now)
        if want is None:
            return 0
        return self._move_to(want)

    def _target(self, now: float) -> int | None:
        prefill, decode = self._demand(now)
        self.last_needs = (prefill, decode)
        n = len(self.monitor.instances)
        current_p = len(self.monitor.pool(Role.PREFILL))
        # Warmup-hold (Dynamo's load_min_observations): planning on a cold
        # or idle signal is amplified noise.
        if len(self._arrivals) < self.min_arrivals or prefill + decode < self.demand_floor:
            self._pending_target, self._confirmations = None, 0
            return None
        # The closed loop: outcomes trump the model. A
        # mean-capacity estimate cannot see the burst headroom a tail
        # SLO needs, so a correct-on-average model can starve the tail
        # indefinitely. A window under the floor forces one escalation
        # step toward the missing phase; the hold keeps the escalated
        # size while the crisis lasts. Failures arrive as TTFT misses,
        # so stalls escalate the same way.
        if self.attainment_floor > 0.0:
            w0 = now - self.window_s
            recent = [o for o in self.scheduler.outcomes if o[0] >= w0]
            if len(recent) >= self.min_arrivals:
                ttft_frac = sum(1 for _, ok, _ in recent if ok) / len(recent)
                tpot_frac = sum(1 for _, _, ok in recent if ok) / len(recent)
                p_starves = ttft_frac < self.attainment_floor and ttft_frac <= tpot_frac
                if p_starves and current_p < n - 1:
                    self._hold_p_min, self._hold_d_min = current_p + 1, 0
                    self._hold_until = now + 5 * self.interval_s
                    self._pending_target, self._confirmations = None, 0
                    log.info(
                        "PLAN escalate +P: ttft attainment %.0f%% under the %.0f%% floor",
                        ttft_frac * 100,
                        self.attainment_floor * 100,
                    )
                    return current_p + 1
                d_starves = tpot_frac < self.attainment_floor and tpot_frac < ttft_frac
                if d_starves and (n - current_p) < n - 1:
                    self._hold_p_min, self._hold_d_min = 0, (n - current_p) + 1
                    self._hold_until = now + 5 * self.interval_s
                    self._pending_target, self._confirmations = None, 0
                    log.info(
                        "PLAN escalate +D: tpot attainment %.0f%% under the %.0f%% floor",
                        tpot_frac * 100,
                        self.attainment_floor * 100,
                    )
                    return current_p - 1
        need_p = math.ceil(prefill / self.utilization)
        need_d = math.ceil(decode / self.utilization)
        if need_p + need_d > n:
            need_p = max(1, round(need_p * n / (need_p + need_d)))
        want = max(1, min(n - 1, need_p))
        if now < self._hold_until:
            if self._hold_p_min:
                want = max(want, self._hold_p_min)
            if self._hold_d_min:
                want = min(want, n - self._hold_d_min)
        if want == current_p:
            self._pending_target, self._confirmations = None, 0
            return None
        # Asymmetric damping: growing a starving pool acts now; moves that
        # only rebalance surplus wait for confirmation. Starving means
        # materially short - demand past current capacity by `deadband`
        # engines - not ceil-wobble short: at a demand boundary the rounded
        # need flips every window, and the deadband keeps those wobble
        # flips off this act-now path, while real phase shifts (which
        # overshoot any sane deadband) act immediately.
        starving_p = prefill / self.utilization > current_p + self.deadband and want > current_p
        starving_d = (
            decode / self.utilization > (n - current_p) + self.deadband and want < current_p
        )
        if starving_p or starving_d:
            self._pending_target, self._confirmations = None, 0
            return want
        if want != self._pending_target:
            self._pending_target, self._confirmations = want, 1
            return None
        self._confirmations += 1
        if self._confirmations >= self.confirmations_needed:
            self._pending_target, self._confirmations = None, 0
            return want
        return None

    def _move_to(self, want: int) -> int:
        """All needed moves in one pass, emptiest instances first.

        The flip is Arrow §5.5's relabel; each move is recorded on the
        scheduler's flip list so /arrow/state and the scorers see the
        planner's actuation exactly as they see Algorithm 3's.
        """
        now = self._clock()
        moved = 0
        # The plan's destination respects the configured floor; a pinned
        # engine keeps its seat and never appears among the movers.
        want = max(want, self.scheduler.min_prefill)
        while len(self.monitor.pool(Role.PREFILL)) != want:
            growing = Role.PREFILL if len(self.monitor.pool(Role.PREFILL)) < want else Role.DECODE
            source = Role.DECODE if growing is Role.PREFILL else Role.PREFILL
            live = [
                i
                for i in self.monitor.pool(source)
                if i.iid not in self.scheduler.ejected and not self.scheduler._offline(i.iid)
            ]
            # Never empty the source pool - counted over the whole live pool,
            # because a pinned engine holds its seat without being a mover.
            if len(live) <= 1:
                break
            pool = [i for i in live if i.iid not in self.scheduler.pinned]
            if not pool:
                break
            mover = min(pool, key=lambda i: len(i.prefill) + len(i.decode))
            mover.role = growing
            if self.scheduler.flip_offline_s > 0.0:
                self.scheduler.offline_until[mover.iid] = now + self.scheduler.flip_offline_s
            self.scheduler.flips.append(
                Flip(
                    at=now,
                    iid=mover.iid,
                    to=growing,
                    by="planner",
                    prefill_inflight=len(mover.prefill),
                    decode_inflight=len(mover.decode),
                )
            )
            moved += 1
        del self.scheduler.flips[: -self.scheduler._flip_history]
        if moved:
            p = len(self.monitor.pool(Role.PREFILL))
            log.info(
                "PLAN moved %d instance(s) -> %dP%dD | demand P=%.2f D=%.2f",
                moved,
                p,
                len(self.monitor.instances) - p,
                *self.last_needs,
            )
        return moved
