"""Select an eligible engine using its current role and resident request count."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..runtime.standby import control_ready
from ..types import Instance, Phase, Request, Role
from .admission import AdmissionQueue
from .outcomes import RouterHeld
from .records import monitoring_degraded_reason
from .seats import decode_seats, prefill_seats

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter


def placement_hold(router: NarwhalRouter, phase: Phase) -> str:
    """Return why the router holds `phase` placement, or an empty string.

    A lifecycle hold stops both phases. A lost lease, standby or degraded monitoring
    fences prefill only, so a prefilled request still decodes.
    """
    if router.lifecycle_blocked:
        return router.lifecycle_blocked
    if phase is Phase.PREFILL and (router.failover_blocked or not control_ready(router)):
        return (
            router.failover_blocked
            or monitoring_degraded_reason(router)
            or "standby: the primary router is serving"
        )
    return ""


class Dispatcher:
    """Hold phase waiters until an engine has a free seat, then select by current capacity."""

    def __init__(self, router: NarwhalRouter) -> None:
        self.router = router
        self.queues = {
            phase: AdmissionQueue[Instance](
                router.max_concurrent, router.cfg.request_timeout_s, clock=router._clock
            )
            for phase in Phase
        }

    def notify(self) -> None:
        """Hand freed seats in both phase queues to waiters after a release or controller pass."""
        for queue in self.queues.values():
            queue.notify()

    def wake_all(self) -> None:
        """Wake every phase waiter to recheck its hold."""
        for queue in self.queues.values():
            queue.wake_all()

    async def place(
        self,
        request: Request,
        *,
        deadline: float,
        claim: Callable[[Instance], None],
        exclude: set[str] | None = None,
        wait_s: float | None = None,
        check: Callable[[], float | None] | None = None,
    ) -> Instance:
        """Select after waiting, never from `exclude`, and claim the engine in the same pass.

        `claim` runs before the engine is returned, and may raise to refuse it. A waiting
        request ends with `RouterHeld` when a hold of its phase begins. `check` adds the
        caller's recheck to that hold check.
        """
        router = self.router
        phase = request.phase
        role = Role.PREFILL if phase is Phase.PREFILL else Role.DECODE
        seats = prefill_seats if phase is Phase.PREFILL else decode_seats

        def reserve() -> Instance | None:
            if placement_hold(router, phase):
                return None
            live = router.scheduler.live_instances()
            unusable = (exclude or set()) | router.scheduler.decode_exclusion(
                request, exclude or set()
            )
            candidates = {
                inst.iid
                for inst in router.scheduler.role_pool(role, live)
                if not (limit := seats(router, inst.iid))
                or len(inst.prefill if phase is Phase.PREFILL else inst.decode) < limit
            } - unusable
            if not candidates:
                return None
            excluded = set(router.monitor.instances) - candidates
            inst = router.scheduler.schedule(request, exclude=excluded)
            claim(inst)
            return inst

        def recheck() -> float | None:
            hold = placement_hold(router, phase)
            if hold:
                raise RouterHeld(hold)
            return check() if check is not None else None

        router.monitor.waiting[request.rid] = request
        try:
            return await self.queues[phase].acquire(
                reserve, deadline=deadline, wait_s=wait_s, check=recheck
            )
        finally:
            router.monitor.waiting.pop(request.rid, None)
